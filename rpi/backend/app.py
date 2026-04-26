"""
Smart Parking System - Main Flask Application
Fixed: reservation reference/notes, pc_endpoint in /api/status, vehicles_json tojson.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from flask_login import (LoginManager, current_user, login_required,
                         login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from rpi import config as cfg
from rpi.backend.models import (ParkingEvent, Reservation, SystemLog,
                                 SystemSettings, User, Vehicle, db)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
log = logging.getLogger("app")

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "..", "..", "web", "templates"),
    static_folder  =os.path.join(os.path.dirname(__file__), "..", "..", "web", "static"),
)
app.config.update(
    SQLALCHEMY_DATABASE_URI      = cfg.DATABASE_URL,
    SQLALCHEMY_TRACK_MODIFICATIONS = False,
    SECRET_KEY                   = cfg.SECRET_KEY,
    DEBUG                        = cfg.DEBUG,
    MAX_CONTENT_LENGTH           = 16 * 1024 * 1024,
)

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view          = "login"
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(uid):
    return User.query.get(int(uid))


# ── Hardware / AI singletons ──────────────────────────────────────────────────
_zmq_client   = None
_ultrasonic   = None
_servo        = None
_sim_running  = False
_sim_thread   = None

AVAILABLE_SLOTS = ["A1", "A2", "A3", "B1", "B2", "B3"]


def get_zmq():
    global _zmq_client
    if _zmq_client is None:
        try:
            from rpi.zmq_client.client import AIClient
            _zmq_client = AIClient(cfg.ZEROMQ_ENDPOINT_PC, cfg.ZMQ_TIMEOUT_MS)
        except Exception as e:
            log.warning("ZMQ client init failed: %s", e)
    return _zmq_client


def get_sensor():
    global _ultrasonic
    if _ultrasonic is None:
        from rpi.sensors.ultrasonic import UltrasonicSensor
        _ultrasonic = UltrasonicSensor(
            trigger_pin=cfg.ULTRASONIC_TRIGGER,
            echo_pin   =cfg.ULTRASONIC_ECHO,
            threshold  =cfg.DISTANCE_THRESHOLD,
            simulate   =cfg.SIMULATION_MODE,
        )
    return _ultrasonic


def get_servo():
    global _servo
    if _servo is None:
        from rpi.servo.servo import ServoMotor
        _servo = ServoMotor(
            pin        =cfg.SERVO_PIN,
            open_angle =cfg.SERVO_OPEN_ANGLE,
            close_angle=cfg.SERVO_CLOSE_ANGLE,
            open_secs  =cfg.SERVO_OPEN_SECS,
            simulate   =cfg.SIMULATION_MODE,
        )
    return _servo


# ── Decorators ────────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log_event(component: str, level: str, message: str):
    entry = SystemLog(level=level, component=component, message=message)
    db.session.add(entry)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def _process_vehicle_detection(image_b64: str | None, trigger: str = "api") -> dict:
    sim_mode = SystemSettings.get("simulation_mode", "true") == "true"
    cam_mode = SystemSettings.get("camera_mode", "pc")

    # ── AI Processing ─────────────────────────────────────────────────────────
    if sim_mode:
        result = _simulate_detection()
    else:
        client = get_zmq()
        if client is None:
            return {"success": False, "error": "AI server unavailable"}

        if image_b64:
            result = client.process_b64(image_b64, save=True)
        elif cam_mode == "pc":
            result = client.capture_from_pc()
        else:
            result = _capture_rpi_and_send(client)

    if not result.get("success"):
        _log_event("ai", "ERROR", f"Detection failed: {result.get('error')}")
        return result

    detections  = result.get("detections", [])
    plate_text  = detections[0]["text"] if detections else None
    confidence  = detections[0]["conf"] if detections else None
    annotated   = result.get("annotated_b64")

    # ── Authorization check ───────────────────────────────────────────────────
    gate_opened = False
    event_type  = "denied"
    notes       = "No plate detected"
    vehicle_row = None

    if plate_text:
        vehicle_row = Vehicle.query.filter_by(
            license_plate=plate_text.strip().upper()
        ).first()

        if vehicle_row and vehicle_row.is_authorized:
            event_type  = "entry"
            notes       = f"Authorized: {vehicle_row.owner_name}"
            gate_opened = True
            get_servo().open_gate()
            _log_event("gate", "INFO", f"Gate opened for {plate_text}")
        else:
            notes = f"Unauthorized plate: {plate_text}"
            _log_event("gate", "WARNING", notes)

    # ── Record event ──────────────────────────────────────────────────────────
    event = ParkingEvent(
        license_plate=plate_text,
        event_type   =event_type,
        success      =bool(plate_text),
        confidence   =confidence,
        processing_ms=result.get("processing_ms") or result.get("server_ms"),
        gate_opened  =gate_opened,
        annotated_image=annotated,
        simulated    =sim_mode,
        notes        =notes,
        vehicle_id   =vehicle_row.id if vehicle_row else None,
    )
    db.session.add(event)
    db.session.commit()

    return {
        "success"      : True,
        "plate"        : plate_text,
        "confidence"   : confidence,
        "authorized"   : gate_opened,
        "gate_opened"  : gate_opened,
        "event_id"     : event.id,
        "annotated_b64": annotated,
        "simulated"    : sim_mode,
        "notes"        : notes,
    }


def _simulate_detection() -> dict:
    import cv2
    import numpy as np

    plates = ["ABC-1234", "TN-99-456", "XYZ-7890", "DEF-5678", "GHI-1111"]
    plate  = random.choice(plates)
    conf   = round(random.uniform(0.80, 0.99), 2)
    w, h   = 640, 480
    x1, y1 = random.randint(100, 300), random.randint(150, 250)
    x2, y2 = x1 + 180, y1 + 50

    frame = np.zeros((h, w, 3), np.uint8)
    frame[:] = (30, 30, 50)
    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
    cv2.putText(frame, f"{plate} ({conf:.0%})", (x1, y1 - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    cv2.putText(frame, "SIMULATION", (10, h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

    _, buf  = cv2.imencode(".jpg", frame)
    b64_img = base64.b64encode(buf).decode()

    time.sleep(random.uniform(0.1, 0.4))

    return {
        "success"    : True,
        "detections" : [{"box": [x1, y1, x2, y2], "conf": conf, "text": plate}],
        "annotated_b64": b64_img,
        "processing_ms": random.randint(80, 350),
        "simulated"  : True,
    }


def _capture_rpi_and_send(client) -> dict:
    try:
        import cv2
        cap = cv2.VideoCapture(cfg.CAMERA_INDEX)
        if not cap.isOpened():
            return {"success": False, "error": "RPi camera not available"}
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return {"success": False, "error": "Camera capture failed"}
        return client.process_image(frame, save=True)
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Simulation background thread ──────────────────────────────────────────────

def _simulation_loop():
    global _sim_running
    log.info("🔄 Simulation loop started")
    with app.app_context():
        while _sim_running:
            interval = int(SystemSettings.get("sim_interval", "15"))
            time.sleep(interval)
            if not _sim_running:
                break
            try:
                _process_vehicle_detection(None, trigger="simulation")
                log.info("🚗 Sim event fired")
            except Exception as e:
                log.error("Sim loop error: %s", e)


def _start_simulation():
    global _sim_running, _sim_thread
    if _sim_running:
        return
    _sim_running = True
    _sim_thread  = threading.Thread(target=_simulation_loop, daemon=True)
    _sim_thread.start()


def _stop_simulation():
    global _sim_running
    _sim_running = False


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES – Auth
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        remember = request.form.get("remember") == "on"
        user     = User.query.filter_by(username=username).first()
        if user and user.check_password(password) and user.is_active:
            login_user(user, remember=remember)
            user.last_login = datetime.utcnow()
            db.session.commit()
            _log_event("auth", "INFO", f"User '{username}' logged in")
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Invalid credentials", "danger")
        _log_event("auth", "WARNING", f"Failed login for '{username}'")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    _log_event("auth", "INFO", f"User '{current_user.username}' logged out")
    logout_user()
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email    = request.form.get("email",    "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        if password != confirm:
            flash("Passwords do not match", "danger")
        elif User.query.filter_by(username=username).first():
            flash("Username already taken", "danger")
        elif User.query.filter_by(email=email).first():
            flash("Email already registered", "danger")
        else:
            user = User(username=username, email=email, role="user")
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            _log_event("auth", "INFO", f"New user registered: '{username}'")
            flash("Account created! Please log in.", "success")
            return redirect(url_for("login"))
    return render_template("register.html")


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES – Dashboard
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/dashboard")
@login_required
def dashboard():
    recent_events  = (ParkingEvent.query
                      .order_by(ParkingEvent.timestamp.desc())
                      .limit(10).all())
    total_vehicles = Vehicle.query.count()
    total_users    = User.query.count()
    events_today   = ParkingEvent.query.filter(
        ParkingEvent.timestamp >= datetime.utcnow().date()
    ).count()
    auth_today     = ParkingEvent.query.filter(
        ParkingEvent.timestamp >= datetime.utcnow().date(),
        ParkingEvent.gate_opened == True,
    ).count()

    sim_mode = SystemSettings.get("simulation_mode", "true") == "true"
    cam_mode = SystemSettings.get("camera_mode", "pc")

    return render_template("dashboard.html",
        recent_events=recent_events,
        stats={
            "total_vehicles": total_vehicles,
            "total_users"   : total_users,
            "events_today"  : events_today,
            "auth_today"    : auth_today,
        },
        sim_mode=sim_mode,
        cam_mode=cam_mode,
    )


@app.route("/reservations")
@login_required
def reservations():
    if current_user.is_admin:
        vehicles = Vehicle.query.order_by(Vehicle.license_plate).all()
        upcoming = (Reservation.query
                    .order_by(Reservation.start_time.desc())
                    .limit(25).all())
    else:
        vehicles = current_user.vehicles.order_by(Vehicle.license_plate).all()
        upcoming = (current_user.reservations
                    .order_by(Reservation.start_time.desc())
                    .limit(25).all())

    # Serialise to JSON-safe list for the template
    vehicles_json = [v.to_dict() for v in vehicles]

    return render_template("reservations.html",
        vehicles      =vehicles,
        vehicles_json =vehicles_json,
        available_slots=AVAILABLE_SLOTS,
        upcoming      =upcoming,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES – Admin
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/admin")
@admin_required
def admin():
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    last7 = []
    for i in range(6, -1, -1):
        day   = datetime.utcnow().date() - timedelta(days=i)
        count = ParkingEvent.query.filter(
            ParkingEvent.timestamp >= day,
            ParkingEvent.timestamp <  day + timedelta(days=1)
        ).count()
        last7.append({"date": day.strftime("%b %d"), "count": count})

    return render_template("admin_dashboard.html",
        user_count   =User.query.count(),
        vehicle_count=Vehicle.query.count(),
        event_count  =ParkingEvent.query.count(),
        log_count    =SystemLog.query.count(),
        chart_data   =json.dumps(last7),
        sim_mode     =SystemSettings.get("simulation_mode", "true") == "true",
    )


@app.route("/admin/users", methods=["GET"])
@admin_required
def admin_users():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_create_user():
    data = request.form
    user = User(username=data["username"], email=data["email"],
                role=data.get("role", "user"))
    user.set_password(data["password"])
    db.session.add(user)
    db.session.commit()
    flash(f"User '{user.username}' created.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/toggle", methods=["POST"])
@admin_required
def admin_toggle_user(uid):
    user = User.query.get_or_404(uid)
    if user.id == current_user.id:
        flash("Cannot deactivate yourself.", "warning")
    else:
        user.is_active = not user.is_active
        db.session.commit()
        flash(f"User {'activated' if user.is_active else 'deactivated'}.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/vehicles")
@admin_required
def admin_vehicles():
    vehicles = Vehicle.query.order_by(Vehicle.created_at.desc()).all()
    return render_template("admin_vehicles.html", vehicles=vehicles)


@app.route("/admin/vehicles/create", methods=["POST"])
@admin_required
def admin_create_vehicle():
    data  = request.form
    plate = data["license_plate"].strip().upper()
    if Vehicle.query.filter_by(license_plate=plate).first():
        flash("Plate already registered.", "danger")
    else:
        v = Vehicle(
            license_plate=plate,
            owner_name   =data.get("owner_name", ""),
            vehicle_type =data.get("vehicle_type", "car"),
            color        =data.get("color", ""),
            is_authorized=data.get("is_authorized") == "on",
        )
        db.session.add(v)
        db.session.commit()
        flash(f"Vehicle {plate} registered.", "success")
    return redirect(url_for("admin_vehicles"))


@app.route("/admin/vehicles/<int:vid>/toggle", methods=["POST"])
@admin_required
def admin_toggle_vehicle(vid):
    v = Vehicle.query.get_or_404(vid)
    v.is_authorized = not v.is_authorized
    db.session.commit()
    flash(f"Vehicle {'authorized' if v.is_authorized else 'blocked'}.", "success")
    return redirect(url_for("admin_vehicles"))


@app.route("/admin/events")
@admin_required
def admin_events():
    page   = request.args.get("page", 1, type=int)
    events = (ParkingEvent.query
              .order_by(ParkingEvent.timestamp.desc())
              .paginate(page=page, per_page=25))
    return render_template("admin_events.html", events=events)


@app.route("/admin/logs")
@admin_required
def admin_logs():
    page  = request.args.get("page",  1, type=int)
    level = request.args.get("level", "")
    q     = SystemLog.query.order_by(SystemLog.timestamp.desc())
    if level:
        q = q.filter_by(level=level)
    logs  = q.paginate(page=page, per_page=50)
    return render_template("admin_logs.html", logs=logs, current_level=level)


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        settings_map = {
            "camera_mode"       : request.form.get("camera_mode", "pc"),
            "simulation_mode"   : "true" if request.form.get("simulation_mode") else "false",
            "sim_interval"      : request.form.get("sim_interval",        "15"),
            "distance_threshold": request.form.get("distance_threshold",  "2.0"),
            "yolo_conf"         : str(int(request.form.get("yolo_conf", "50")) / 100),
            "maintenance_mode"  : "true" if request.form.get("maintenance_mode") else "false",
        }
        for k, v in settings_map.items():
            SystemSettings.set(k, v)

        if settings_map["simulation_mode"] == "true":
            _start_simulation()
        else:
            _stop_simulation()

        flash("Settings saved.", "success")
        return redirect(url_for("admin_settings"))

    settings = {s.key: s.value for s in SystemSettings.query.all()}
    return render_template("admin_settings.html",
                           settings=settings, sim_running=_sim_running)


# ═══════════════════════════════════════════════════════════════════════════════
#  REST API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/detect", methods=["POST"])
@login_required
def api_detect():
    data    = request.get_json(silent=True) or {}
    img_b64 = data.get("image_b64") or request.form.get("image_b64")
    if "image" in request.files:
        img_b64 = base64.b64encode(request.files["image"].read()).decode()
    result = _process_vehicle_detection(img_b64, trigger="api")
    return jsonify(result)


@app.route("/api/simulate_event", methods=["POST"])
@admin_required
def api_simulate_event():
    result = _process_vehicle_detection(None, trigger="manual_sim")
    return jsonify(result)


# ── Sensor ────────────────────────────────────────────────────────────────────

@app.route("/api/sensor/status")
@login_required
def api_sensor_status():
    sensor = get_sensor()
    dist   = sensor.get_distance()
    return jsonify({
        "distance_m"     : dist,
        "vehicle_detected": sensor.is_vehicle_detected(),
        "threshold_m"    : cfg.DISTANCE_THRESHOLD,
        "simulated"      : sensor.simulate,
        "timestamp"      : datetime.utcnow().isoformat(),
    })


@app.route("/api/sensor/sim", methods=["POST"])
@admin_required
def api_sensor_sim():
    data    = request.get_json() or {}
    present = data.get("vehicle_present", False)
    get_sensor().set_sim_vehicle(present)
    return jsonify({"ok": True, "vehicle_present": present})


# ── Gate ──────────────────────────────────────────────────────────────────────

@app.route("/api/gate/open", methods=["POST"])
@admin_required
def api_gate_open():
    get_servo().open_gate()
    _log_event("gate", "INFO", f"Manual gate open by {current_user.username}")
    return jsonify({"ok": True, "state": "open"})


@app.route("/api/gate/close", methods=["POST"])
@admin_required
def api_gate_close():
    get_servo().close_gate()
    _log_event("gate", "INFO", f"Manual gate close by {current_user.username}")
    return jsonify({"ok": True, "state": "closed"})


@app.route("/api/gate/status")
@login_required
def api_gate_status():
    servo = get_servo()
    return jsonify({"is_open": servo.is_open, "simulated": servo.simulate})


# ── System status ─────────────────────────────────────────────────────────────

@app.route("/api/status")
@login_required
def api_status():
    sensor      = get_sensor()
    servo       = get_servo()
    sim_mode    = SystemSettings.get("simulation_mode", "true") == "true"
    cam_mode    = SystemSettings.get("camera_mode", "pc")
    maintenance = SystemSettings.get("maintenance_mode", "false") == "true"

    pc_online  = False
    pc_error   = None
    if not sim_mode:
        try:
            client    = get_zmq()
            if client is not None:
                ping_res  = client.ping()
                pc_online = ping_res.get("success", False)
                if not pc_online:
                    pc_error = ping_res.get("error", "ping failed")
                    if not pc_error:
                        pc_error = client.get_last_error()
            else:
                pc_error = "ZMQ client not initialised"
        except Exception as e:
            pc_error = str(e)

    recent = (ParkingEvent.query
              .order_by(ParkingEvent.timestamp.desc())
              .limit(5).all())

    return jsonify({
        "simulation_mode" : sim_mode,
        "camera_mode"     : cam_mode,
        "maintenance_mode": maintenance,
        "pc_online"       : pc_online,
        "pc_endpoint"     : cfg.ZEROMQ_ENDPOINT_PC,
        "pc_error"        : pc_error,
        "sim_loop_running": _sim_running,
        "sensor": {
            "distance_m"     : sensor.get_distance(),
            "vehicle_detected": sensor.is_vehicle_detected(),
            "simulated"      : sensor.simulate,
        },
        "gate": {
            "is_open"  : servo.is_open,
            "simulated": servo.simulate,
        },
        "recent_events": [e.to_dict() for e in recent],
        "timestamp"    : datetime.utcnow().isoformat(),
    })


# ── Stats ──────────────────────────────────────────────────────────────────────

@app.route("/api/stats/events")
@login_required
def api_stats_events():
    days = int(request.args.get("days", 7))
    data = []
    for i in range(days - 1, -1, -1):
        day   = datetime.utcnow().date() - timedelta(days=i)
        total = ParkingEvent.query.filter(
            ParkingEvent.timestamp >= day,
            ParkingEvent.timestamp <  day + timedelta(days=1)
        ).count()
        auth  = ParkingEvent.query.filter(
            ParkingEvent.timestamp >= day,
            ParkingEvent.timestamp <  day + timedelta(days=1),
            ParkingEvent.gate_opened == True,
        ).count()
        data.append({"date": day.strftime("%b %d"), "total": total, "authorized": auth})
    return jsonify(data)


@app.route("/api/vehicles")
@login_required
def api_vehicles():
    vehicles = Vehicle.query.order_by(Vehicle.license_plate).all()
    return jsonify([v.to_dict() for v in vehicles])


@app.route("/api/events/recent")
@login_required
def api_recent_events():
    limit  = min(int(request.args.get("limit", 10)), 100)
    events = (ParkingEvent.query
              .order_by(ParkingEvent.timestamp.desc())
              .limit(limit).all())
    return jsonify([e.to_dict() for e in events])


@app.route("/api/logs/recent")
@admin_required
def api_recent_logs():
    limit = min(int(request.args.get("limit", 50)), 200)
    logs  = (SystemLog.query
             .order_by(SystemLog.timestamp.desc())
             .limit(limit).all())
    return jsonify([l.to_dict() for l in logs])


# ── Reservations ──────────────────────────────────────────────────────────────

@app.route("/api/reservations/events")
@login_required
def api_reservation_events():
    query = Reservation.query.filter(Reservation.status == "active")
    if not current_user.is_admin:
        query = query.filter_by(user_id=current_user.id)

    now          = datetime.utcnow()
    reservations = query.filter(Reservation.end_time >= now).all()

    events = []
    for r in reservations:
        vehicle = r.vehicle
        title   = f"{r.slot_id} – {vehicle.license_plate if vehicle else 'Unknown'}"
        events.append({
            "id"       : r.id,
            "title"    : title,
            "start"    : r.start_time.isoformat(),
            "end"      : r.end_time.isoformat() if r.end_time else None,
            "status"   : r.status,
            "slot_id"  : r.slot_id,
            "reference": getattr(r, "reference", None),
            "vehicle"  : vehicle.to_dict() if vehicle else None,
        })

    return jsonify(events)


@app.route("/api/reservations/book", methods=["POST"])
@login_required
def api_reservation_book():
    data       = request.get_json(silent=True) or {}
    vehicle_id = data.get("vehicle_id")
    slot_id    = str(data.get("slot_id", "")).strip().upper()
    start_time = data.get("start_time")
    end_time   = data.get("end_time")
    reference  = str(data.get("reference") or "").strip().upper() or None
    notes      = str(data.get("notes") or "").strip() or None

    if not vehicle_id:
        return jsonify({"success": False, "error": "Please select a vehicle."}), 400
    if not slot_id:
        return jsonify({"success": False, "error": "Please select a parking slot."}), 400
    if not start_time:
        return jsonify({"success": False, "error": "Please set a start time."}), 400
    if not end_time:
        return jsonify({"success": False, "error": "Please set an end time."}), 400

    vehicle = Vehicle.query.get(vehicle_id)
    if not vehicle:
        return jsonify({"success": False, "error": "Selected vehicle not found."}), 404

    if not current_user.is_admin and vehicle.user_id != current_user.id:
        abort(403)

    try:
        start_dt = datetime.fromisoformat(start_time)
        end_dt   = datetime.fromisoformat(end_time)
    except ValueError:
        return jsonify({"success": False, "error": "Invalid date format. Use YYYY-MM-DDTHH:MM"}), 400

    if end_dt <= start_dt:
        return jsonify({"success": False, "error": "End time must be after start time."}), 400

    if end_dt <= datetime.utcnow():
        return jsonify({"success": False, "error": "Reservation must be for a future time."}), 400

    overlap = (Reservation.query
               .filter(
                   Reservation.slot_id    == slot_id,
                   Reservation.status     == "active",
                   Reservation.start_time <  end_dt,
                   Reservation.end_time   >  start_dt,
               ).first())
    if overlap:
        return jsonify({"success": False,
                        "error": f"Slot {slot_id} is already booked during that time."}), 409

    # Build reservation — support optional reference column gracefully
    kwargs = dict(
        user_id   =current_user.id,
        vehicle_id=vehicle.id,
        slot_id   =slot_id,
        start_time=start_dt,
        end_time  =end_dt,
        status    ="active",
    )
    # Add reference/notes only if the model column exists
    reservation = Reservation(**kwargs)
    if hasattr(reservation, "reference"):
        reservation.reference = reference
    if hasattr(reservation, "notes"):
        reservation.notes = notes

    db.session.add(reservation)
    db.session.commit()

    ref_str = f" ref={reference}" if reference else ""
    _log_event("reservation", "INFO",
               f"Reservation {reservation.id} booked by {current_user.username} "
               f"for slot {slot_id}{ref_str}")

    return jsonify({"success": True, "reservation": reservation.to_dict()})


# ── SSE stream ────────────────────────────────────────────────────────────────

@app.route("/api/stream")
@login_required
def api_stream():
    def generate():
        while True:
            try:
                sensor  = get_sensor()
                servo   = get_servo()
                payload = {
                    "distance_m"     : sensor.get_distance(),
                    "vehicle_detected": sensor.is_vehicle_detected(),
                    "gate_open"      : servo.is_open,
                    "ts"             : datetime.utcnow().isoformat(),
                }
                yield f"data: {json.dumps(payload)}\n\n"
            except Exception:
                pass
            time.sleep(2)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ═══════════════════════════════════════════════════════════════════════════════
#  Error handlers
# ═══════════════════════════════════════════════════════════════════════════════

@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message="Access denied"), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Page not found"), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, message="Internal server error"), 500


# ═══════════════════════════════════════════════════════════════════════════════
#  DB Init + Seed
# ═══════════════════════════════════════════════════════════════════════════════

def init_db():
    db.create_all()

    # Default admin
    if not User.query.filter_by(username="admin").first():
        admin = User(username="admin", email="admin@parking.local", role="admin")
        admin.set_password("admin123")
        db.session.add(admin)

    # Default settings
    defaults = {
        "simulation_mode"   : "true",
        "camera_mode"       : "pc",
        "sim_interval"      : "15",
        "distance_threshold": "2.0",
        "yolo_conf"         : "0.50",
        "maintenance_mode"  : "false",
    }
    for k, v in defaults.items():
        if not SystemSettings.query.filter_by(key=k).first():
            db.session.add(SystemSettings(key=k, value=v))

    # Demo vehicles
    demo = [
        ("ABC-1234",  "Ahmed Ben Ali",  "car"),
        ("TN-99-456", "Fatima Zahra",   "car"),
        ("XYZ-7890",  "Mohamed Slim",   "truck"),
    ]
    for plate, owner, vtype in demo:
        if not Vehicle.query.filter_by(license_plate=plate).first():
            db.session.add(Vehicle(license_plate=plate, owner_name=owner,
                                   vehicle_type=vtype))

    db.session.commit()
    log.info("Database initialized ✓")


# ═══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    with app.app_context():
        init_db()
        sim_mode = SystemSettings.get("simulation_mode", "true") == "true"
        if sim_mode:
            _start_simulation()

    app.run(host=cfg.HOST, port=cfg.PORT, debug=cfg.DEBUG,
            threaded=True, use_reloader=False)
