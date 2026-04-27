"""
Smart Parking System - Main Flask Application
Updated: payments, parking slots admin, fuzzy Arabic OCR matching,
auto-detect on sensor trigger, user vehicle registration, theme toggle,
admin user/role management, earnings stats, GPIO capture button.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from rpi import config as cfg
from rpi.backend.models import (Payment, ParkingEvent, ParkingSlot,
                                 Reservation, SystemLog, SystemSettings,
                                 User, Vehicle, db)

logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
log = logging.getLogger("app")

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "..", "..", "web", "templates"),
    static_folder  =os.path.join(os.path.dirname(__file__), "..", "..", "web", "static"),
)
app.config.update(
    SQLALCHEMY_DATABASE_URI       = cfg.DATABASE_URL,
    SQLALCHEMY_TRACK_MODIFICATIONS= False,
    SECRET_KEY                    = cfg.SECRET_KEY,
    DEBUG                         = cfg.DEBUG,
    MAX_CONTENT_LENGTH            = 16 * 1024 * 1024,
)

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view             = "login"
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(uid):
    return User.query.get(int(uid))


# ── Hardware / AI singletons ──────────────────────────────────────────────────
_zmq_client  = None
_ultrasonic  = None
_servo       = None
_sim_running = False
_sim_thread  = None

# Auto-detect state (sensor triggered)
_auto_detect_enabled  = True
_last_auto_detect_ts  = 0
_AUTO_DETECT_COOLDOWN = 8   # seconds between auto-detects

# Latest detection result (for SSE stream to send to dashboard)
_latest_detection = None


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


def get_configured_servo():
    servo = get_servo()
    try:
        servo.open_secs = float(SystemSettings.get("gate_open_secs", str(cfg.SERVO_OPEN_SECS)))
    except (ValueError, TypeError):
        servo.open_secs = cfg.SERVO_OPEN_SECS
    return servo


def _get_active_slots() -> list[str]:
    """Return list of active slot codes from DB."""
    slots = ParkingSlot.query.filter_by(is_active=True).order_by(ParkingSlot.slot_code).all()
    return [s.slot_code for s in slots]


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


def _get_all_registered_plates() -> list[str]:
    """Return all registered plate strings for fuzzy matching."""
    return [v.license_plate for v in Vehicle.query.with_entities(Vehicle.license_plate).all()]


def _process_vehicle_detection(image_b64: str | None, trigger: str = "api") -> dict:
    sim_mode = SystemSettings.get("simulation_mode", "true") == "true"
    cam_mode = SystemSettings.get("camera_mode", "pc")

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

    detections = result.get("detections", [])
    raw_text   = detections[0]["text"] if detections else None
    confidence = detections[0]["conf"] if detections else None
    annotated  = result.get("annotated_b64")

    # ── Fuzzy plate matching ─────────────────────────────────────────────────
    plate_text  = None
    fuzzy_score = 0.0
    vehicle_row = None

    if raw_text:
        all_plates  = _get_all_registered_plates()
        # Try exact match first
        exact = Vehicle.query.filter_by(
            license_plate=raw_text.strip().upper()
        ).first()

        if exact:
            plate_text  = exact.license_plate
            vehicle_row = exact
            fuzzy_score = 1.0
        else:
            # Fuzzy match for Arabic OCR errors
            try:
                from pc.detection.ai_processor import get_processor
                processor = get_processor()
                matched, score = processor.fuzzy_match_plate(raw_text, all_plates)
                fuzzy_score = score
                if matched:
                    plate_text  = matched
                    vehicle_row = Vehicle.query.filter_by(license_plate=matched).first()
                    log.info("Fuzzy plate match: '%s' → '%s' (score=%.2f)",
                             raw_text, matched, score)
                else:
                    plate_text = raw_text.strip().upper()
            except Exception:
                plate_text = raw_text.strip().upper()

    # ── Authorization check ───────────────────────────────────────────────────
    gate_opened = False
    event_type  = "denied"
    notes       = "No plate detected"

    if plate_text:
        if vehicle_row and vehicle_row.is_authorized:
            event_type  = "entry"
            notes       = f"Authorized: {vehicle_row.owner_name or plate_text}"
            if fuzzy_score < 1.0:
                notes += f" [fuzzy match {fuzzy_score:.0%}]"
            gate_opened = True
            get_configured_servo().open_gate()
            _log_event("gate", "INFO", f"Gate opened for {plate_text}")
        else:
            notes = f"Unauthorized plate: {plate_text}"
            if raw_text and raw_text.upper() != plate_text:
                notes += f" (OCR: {raw_text})"
            _log_event("gate", "WARNING", notes)

    event = ParkingEvent(
        license_plate  = plate_text,
        event_type     = event_type,
        success        = bool(plate_text),
        confidence     = confidence,
        processing_ms  = result.get("processing_ms") or result.get("server_ms"),
        gate_opened    = gate_opened,
        annotated_image= annotated,
        simulated      = sim_mode,
        notes          = notes,
        vehicle_id     = vehicle_row.id if vehicle_row else None,
    )
    db.session.add(event)
    db.session.commit()

    detection_result = {
        "success"      : True,
        "plate"        : plate_text,
        "raw_ocr"      : raw_text,
        "fuzzy_score"  : fuzzy_score,
        "confidence"   : confidence,
        "authorized"   : gate_opened,
        "gate_opened"  : gate_opened,
        "event_id"     : event.id,
        "annotated_b64": annotated,
        "simulated"    : sim_mode,
        "notes"        : notes,
    }
    
    # Store latest detection for SSE stream
    global _latest_detection
    _latest_detection = detection_result
    
    return detection_result


def _simulate_detection() -> dict:
    import cv2, numpy as np
    plates = ["195 تونس 4705", "88 صفاقس 1234", "XYZ-7890", "DEF-5678", "12 سوسة 9876"]
    plate  = random.choice(plates)
    conf   = round(random.uniform(0.80, 0.99), 2)
    w, h   = 640, 480
    x1, y1 = random.randint(100, 300), random.randint(150, 250)
    x2, y2 = x1 + 180, y1 + 50

    frame = np.zeros((h, w, 3), np.uint8)
    frame[:] = (30, 30, 50)

    # Use PIL for Arabic-safe drawing
    try:
        from pc.detection.ai_processor import annotate_image_pil, _get_pil_font
        from PIL import Image, ImageDraw
        detections = [{"box": [x1, y1, x2, y2], "conf": conf, "text": plate}]
        frame = annotate_image_pil(frame, detections)
        pil_img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        draw    = ImageDraw.Draw(pil_img)
        font    = _get_pil_font(22)
        draw.text((10, h - 34), "SIMULATION", font=font, fill=(255, 50, 50))
        frame = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    except Exception:
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(frame, "SIMULATION", (10, h - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

    _, buf  = cv2.imencode(".jpg", frame)
    b64_img = base64.b64encode(buf).decode()
    time.sleep(random.uniform(0.1, 0.4))

    return {
        "success"      : True,
        "detections"   : [{"box": [x1, y1, x2, y2], "conf": conf, "text": plate}],
        "annotated_b64": b64_img,
        "processing_ms": random.randint(80, 350),
        "simulated"    : True,
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


# ── Sensor auto-detect thread ─────────────────────────────────────────────────

def _sensor_monitor_loop():
    """Background thread: auto-detect when sensor sees a vehicle."""
    global _last_auto_detect_ts
    log.info("🔍 Sensor monitor loop started")
    with app.app_context():
        while True:
            try:
                auto_enabled = SystemSettings.get("auto_detect", "true") == "true"
                if auto_enabled:
                    sensor = get_sensor()
                    if sensor.is_vehicle_detected():
                        now = time.time()
                        if now - _last_auto_detect_ts > _AUTO_DETECT_COOLDOWN:
                            _last_auto_detect_ts = now
                            log.info("🚗 Auto-detect triggered by sensor")
                            _process_vehicle_detection(None, trigger="auto_sensor")
            except Exception as e:
                log.debug("Sensor monitor error: %s", e)
            time.sleep(1.5)


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


# ── Earnings helpers ──────────────────────────────────────────────────────────

def _earnings_stats() -> dict:
    from sqlalchemy import func
    now   = datetime.utcnow()
    today = now.date()
    week_start  = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    def _sum(since: datetime) -> float:
        row = db.session.query(func.sum(Payment.amount)).filter(
            Payment.status == "paid",
            Payment.paid_at >= since,
        ).scalar()
        return round(row or 0.0, 2)

    return {
        "today"  : _sum(datetime.combine(today, datetime.min.time())),
        "week"   : _sum(datetime.combine(week_start, datetime.min.time())),
        "month"  : _sum(datetime.combine(month_start, datetime.min.time())),
    }


def _slot_stats() -> dict:
    """Stats for parking slot occupancy and reservation status with real-time accuracy."""
    now = datetime.utcnow()
    active_slots = ParkingSlot.query.filter_by(is_active=True).order_by(ParkingSlot.slot_code).all()
    total        = len(active_slots)

    # Current reservations (active and overlapping now)
    current_reservations = (Reservation.query
                            .filter(Reservation.status == "active",
                                    Reservation.start_time <= now,
                                    Reservation.end_time   >= now)
                            .all())
    reservation_map = {r.slot_id: r for r in current_reservations}

    # Latest gate entry events by plate (last 7 days for accuracy)
    recent_entries = (ParkingEvent.query
                      .filter(ParkingEvent.gate_opened == True,
                              ParkingEvent.license_plate.isnot(None),
                              ParkingEvent.timestamp >= now - timedelta(days=7))
                      .order_by(ParkingEvent.timestamp.desc())
                      .all())
    
    # Track entry/exit for each plate
    plate_entries = {}
    for event in recent_entries:
        if event.license_plate not in plate_entries:
            plate_entries[event.license_plate] = event

    slot_details = []
    counts = {"available": 0, "reserved": 0, "occupied": 0}

    for slot in active_slots:
        status = "available"
        title  = "Available"
        reservation = reservation_map.get(slot.slot_code)
        
        if reservation:
            status = "reserved"
            title = f"Reserved - {reservation.vehicle.owner_name if reservation.vehicle else 'Unknown'}"
            vehicle = reservation.vehicle
            
            # Check if vehicle has entered after reservation start
            if vehicle and vehicle.license_plate in plate_entries:
                entry = plate_entries[vehicle.license_plate]
                # Vehicle is occupied if entry happened within reservation period
                if reservation.start_time <= entry.timestamp <= reservation.end_time:
                    status = "occupied"
                    title = f"Occupied - {vehicle.license_plate}"
                elif entry.timestamp > reservation.end_time:
                    # Entry is after reservation ended, still check if it's a recent entry
                    if entry.timestamp >= now - timedelta(hours=4):
                        status = "occupied"
                        title = f"Occupied - {vehicle.license_plate}"
        
        counts[status] += 1
        slot_details.append({
            "slot_code": slot.slot_code,
            "status"   : status,
            "title"    : title,
        })

    available_now = counts["available"]

    entered_today = (ParkingEvent.query
                     .filter(ParkingEvent.gate_opened == True,
                             ParkingEvent.timestamp >= datetime.utcnow().date())
                     .count())

    return {
        "total"        : total,
        "available_now": available_now,
        "reserved_now" : counts["reserved"],
        "occupied_now" : counts["occupied"],
        "slot_details" : slot_details,
        "entered_today": entered_today,
    }


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
    events_today   = ParkingEvent.query.filter(
        ParkingEvent.timestamp >= datetime.utcnow().date()
    ).count()
    auth_today     = ParkingEvent.query.filter(
        ParkingEvent.timestamp >= datetime.utcnow().date(),
        ParkingEvent.gate_opened == True,
    ).count()

    sim_mode = SystemSettings.get("simulation_mode", "true") == "true"
    cam_mode = SystemSettings.get("camera_mode", "pc")
    auto_detect = SystemSettings.get("auto_detect", "true") == "true"

    # User's own vehicles
    if current_user.is_admin:
        user_vehicles = Vehicle.query.all()
        total_vehicles = Vehicle.query.count()
        total_users    = User.query.count()
    else:
        user_vehicles  = current_user.vehicles.all()
        total_vehicles = len(user_vehicles)
        total_users    = 1

    return render_template("dashboard.html",
        recent_events =recent_events,
        user_vehicles =user_vehicles,
        stats={
            "total_vehicles": total_vehicles,
            "total_users"   : total_users,
            "events_today"  : events_today,
            "auth_today"    : auth_today,
        },
        sim_mode    =sim_mode,
        cam_mode    =cam_mode,
        auto_detect =auto_detect,
        slot_stats  =_slot_stats(),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES – User Vehicles
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/my-vehicles")
@login_required
def my_vehicles():
    vehicles = current_user.vehicles.all()
    return render_template("my_vehicles.html", vehicles=vehicles)


@app.route("/my-vehicles/add", methods=["POST"])
@login_required
def add_my_vehicle():
    plate = request.form.get("license_plate", "").strip().upper()
    if not plate:
        flash("License plate is required.", "danger")
        return redirect(url_for("my_vehicles"))

    existing = Vehicle.query.filter_by(license_plate=plate).first()
    if existing:
        if existing.user_id and existing.user_id != current_user.id:
            flash("This plate is already registered by another user.", "danger")
        elif existing.user_id == current_user.id:
            flash("You already have this plate registered.", "warning")
        else:
            # Claim unowned plate
            existing.user_id    = current_user.id
            existing.owner_name = current_user.username
            db.session.commit()
            flash(f"Vehicle {plate} linked to your account.", "success")
        return redirect(url_for("my_vehicles"))

    v = Vehicle(
        license_plate=plate,
        owner_name   =request.form.get("owner_name", current_user.username),
        vehicle_type =request.form.get("vehicle_type", "car"),
        color        =request.form.get("color", ""),
        user_id      =current_user.id,
        is_authorized=True,
    )
    db.session.add(v)
    db.session.commit()
    _log_event("vehicle", "INFO", f"User '{current_user.username}' registered plate {plate}")
    flash(f"Vehicle {plate} registered.", "success")
    return redirect(url_for("my_vehicles"))


@app.route("/my-vehicles/<int:vid>/delete", methods=["POST"])
@login_required
def delete_my_vehicle(vid):
    v = Vehicle.query.get_or_404(vid)
    if v.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    db.session.delete(v)
    db.session.commit()
    flash("Vehicle removed.", "success")
    return redirect(url_for("my_vehicles"))


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES – Reservations
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/reservations")
@login_required
def reservations():
    if current_user.is_admin:
        vehicles = Vehicle.query.order_by(Vehicle.license_plate).all()
        upcoming = (Reservation.query
                    .order_by(Reservation.start_time.desc())
                    .limit(50).all())
    else:
        vehicles = current_user.vehicles.order_by(Vehicle.license_plate).all()
        upcoming = (current_user.reservations
                    .order_by(Reservation.start_time.desc())
                    .limit(25).all())

    vehicles_json = [v.to_dict() for v in vehicles]
    active_slots  = _get_active_slots()

    # Parking rate from settings
    rate_per_hour = float(SystemSettings.get("rate_per_hour", "2.0"))

    return render_template("reservations.html",
        vehicles       =vehicles,
        vehicles_json  =vehicles_json,
        available_slots=active_slots,
        upcoming       =upcoming,
        rate_per_hour  =rate_per_hour,
    )


@app.route("/reservations/<int:rid>/cancel", methods=["POST"])
@login_required
def cancel_reservation(rid):
    r = Reservation.query.get_or_404(rid)
    if r.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    r.status = "cancelled"
    db.session.commit()
    flash("Reservation cancelled.", "success")
    return redirect(url_for("reservations"))


@app.route("/reservations/<int:rid>/modify", methods=["POST"])
@admin_required
def modify_reservation(rid):
    r = Reservation.query.get_or_404(rid)
    data = request.form
    try:
        if data.get("start_time"):
            r.start_time = datetime.fromisoformat(data["start_time"])
        if data.get("end_time"):
            r.end_time   = datetime.fromisoformat(data["end_time"])
        if data.get("slot_id"):
            r.slot_id    = data["slot_id"].upper()
        if data.get("status"):
            r.status     = data["status"]
        db.session.commit()
        flash("Reservation updated.", "success")
    except ValueError as e:
        flash(f"Invalid data: {e}", "danger")
    return redirect(url_for("reservations"))


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

    earnings = _earnings_stats()
    slot_st  = _slot_stats()

    return render_template("admin_dashboard.html",
        user_count   =User.query.count(),
        vehicle_count=Vehicle.query.count(),
        event_count  =ParkingEvent.query.count(),
        log_count    =SystemLog.query.count(),
        chart_data   =json.dumps(last7),
        sim_mode     =SystemSettings.get("simulation_mode", "true") == "true",
        earnings     =earnings,
        slot_stats   =slot_st,
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


@app.route("/admin/users/<int:uid>/edit", methods=["POST"])
@admin_required
def admin_edit_user(uid):
    user = User.query.get_or_404(uid)
    data = request.form
    new_username = data.get("username", "").strip()
    new_email    = data.get("email",    "").strip()
    new_role     = data.get("role",     user.role)
    new_password = data.get("password", "").strip()

    if new_username and new_username != user.username:
        if User.query.filter_by(username=new_username).first():
            flash("Username already taken.", "danger")
            return redirect(url_for("admin_users"))
        user.username = new_username

    if new_email and new_email != user.email:
        if User.query.filter_by(email=new_email).first():
            flash("Email already in use.", "danger")
            return redirect(url_for("admin_users"))
        user.email = new_email

    if new_role in ("admin", "user"):
        user.role = new_role

    if new_password and len(new_password) >= 6:
        user.set_password(new_password)
        flash("Password updated.", "info")

    db.session.commit()
    flash(f"User '{user.username}' updated.", "success")
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
            "auto_detect"       : "true" if request.form.get("auto_detect") else "false",
            "rate_per_hour"     : request.form.get("rate_per_hour", "2.0"),
            "gate_open_secs"    : request.form.get("gate_open_secs", str(cfg.SERVO_OPEN_SECS)),
        }
        # Handle parking slots
        slot_count = int(request.form.get("parking_slot_count", "6"))
        slot_prefix= request.form.get("slot_prefix", "A")
        _sync_parking_slots(slot_count, slot_prefix)

        for k, v in settings_map.items():
            SystemSettings.set(k, v)

        if settings_map["simulation_mode"] == "true":
            _start_simulation()
        else:
            _stop_simulation()

        flash("Settings saved.", "success")
        return redirect(url_for("admin_settings"))

    settings     = {s.key: s.value for s in SystemSettings.query.all()}
    active_slots = ParkingSlot.query.filter_by(is_active=True).all()
    all_slots    = ParkingSlot.query.order_by(ParkingSlot.slot_code).all()

    return render_template("admin_settings.html",
                           settings=settings,
                           sim_running=_sim_running,
                           active_slots=active_slots,
                           all_slots=all_slots)


def _sync_parking_slots(count: int, prefix: str = "A"):
    """Create or deactivate parking slots to match requested count."""
    existing = {s.slot_code: s for s in ParkingSlot.query.all()}

    desired_codes = []
    alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    idx = 0
    letter_idx = 0
    num = 1
    while len(desired_codes) < count:
        code = f"{alpha[letter_idx]}{num}"
        desired_codes.append(code)
        num += 1
        if num > 9:
            num = 1
            letter_idx += 1
            if letter_idx >= len(alpha):
                break

    for code in desired_codes:
        if code in existing:
            existing[code].is_active = True
        else:
            db.session.add(ParkingSlot(slot_code=code, is_active=True))

    # Deactivate extras
    for code, slot in existing.items():
        if code not in desired_codes:
            slot.is_active = False

    db.session.commit()


# ── Payments admin ────────────────────────────────────────────────────────────

@app.route("/admin/payments")
@admin_required
def admin_payments():
    page     = request.args.get("page", 1, type=int)
    payments = (Payment.query
                .order_by(Payment.created_at.desc())
                .paginate(page=page, per_page=25))
    earnings = _earnings_stats()
    return render_template("admin_payments.html", payments=payments, earnings=earnings)


@app.route("/admin/payments/<int:pid>/mark-paid", methods=["POST"])
@admin_required
def admin_mark_paid(pid):
    p = Payment.query.get_or_404(pid)
    p.status  = "paid"
    p.paid_at = datetime.utcnow()
    db.session.commit()
    flash("Payment marked as paid.", "success")
    return redirect(url_for("admin_payments"))


@app.route("/admin/payments/<int:pid>/delete", methods=["POST"])
@admin_required
def admin_delete_payment(pid):
    """Delete a payment record (soft delete by marking as deleted)."""
    p = Payment.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    flash("Payment deleted successfully.", "success")
    return redirect(url_for("admin_payments"))


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


# ── GPIO button capture ───────────────────────────────────────────────────────

@app.route("/api/gpio/capture", methods=["POST"])
@login_required
def api_gpio_capture():
    """Triggered by physical GPIO button on RPi."""
    result = _process_vehicle_detection(None, trigger="gpio_button")
    return jsonify(result)


# ── Sensor ────────────────────────────────────────────────────────────────────

@app.route("/api/sensor/status")
@login_required
def api_sensor_status():
    sensor = get_sensor()
    dist   = sensor.get_distance()
    return jsonify({
        "distance_m"      : dist,
        "vehicle_detected": sensor.is_vehicle_detected(),
        "threshold_m"     : cfg.DISTANCE_THRESHOLD,
        "simulated"       : sensor.simulate,
        "timestamp"       : datetime.utcnow().isoformat(),
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
    get_configured_servo().open_gate()
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
    auto_detect = SystemSettings.get("auto_detect", "true") == "true"

    pc_online = False
    pc_error  = None
    if not sim_mode:
        try:
            client = get_zmq()
            if client:
                ping_res  = client.ping()
                pc_online = ping_res.get("success", False)
                if not pc_online:
                    pc_error = ping_res.get("error", "ping failed")
        except Exception as e:
            pc_error = str(e)

    recent = (ParkingEvent.query
              .order_by(ParkingEvent.timestamp.desc())
              .limit(5).all())

    return jsonify({
        "simulation_mode" : sim_mode,
        "camera_mode"     : cam_mode,
        "maintenance_mode": maintenance,
        "auto_detect"     : auto_detect,
        "pc_online"       : pc_online,
        "pc_endpoint"     : cfg.ZEROMQ_ENDPOINT_PC,
        "pc_error"        : pc_error,
        "sim_loop_running": _sim_running,
        "sensor": {
            "distance_m"      : sensor.get_distance(),
            "vehicle_detected": sensor.is_vehicle_detected(),
            "simulated"       : sensor.simulate,
        },
        "gate": {
            "is_open"  : servo.is_open,
            "simulated": servo.simulate,
        },
        "recent_events": [e.to_dict() for e in recent],
        "slot_stats"   : _slot_stats(),
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


@app.route("/api/stats/earnings")
@admin_required
def api_stats_earnings():
    days = int(request.args.get("days", 30))
    from sqlalchemy import func
    data = []
    for i in range(days - 1, -1, -1):
        day   = datetime.utcnow().date() - timedelta(days=i)
        total = db.session.query(func.sum(Payment.amount)).filter(
            Payment.status  == "paid",
            Payment.paid_at >= day,
            Payment.paid_at <  day + timedelta(days=1)
        ).scalar() or 0.0
        data.append({"date": day.strftime("%b %d"), "amount": round(total, 2)})
    return jsonify(data)


@app.route("/api/stats/slots")
@login_required
def api_stats_slots():
    return jsonify(_slot_stats())


@app.route("/api/vehicles")
@login_required
def api_vehicles():
    if current_user.is_admin:
        vehicles = Vehicle.query.order_by(Vehicle.license_plate).all()
    else:
        vehicles = current_user.vehicles.order_by(Vehicle.license_plate).all()
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


# ── Reservations API ──────────────────────────────────────────────────────────

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
            "payment"  : r.payment.to_dict() if r.payment else None,
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
    notes      = str(data.get("notes")     or "").strip() or None
    pay_method = str(data.get("payment_method") or "cash").lower()

    if pay_method not in ("cash", "card"):
        pay_method = "cash"

    if not vehicle_id:
        return jsonify({"success": False, "error": "Please select a vehicle."}), 400
    if not slot_id:
        return jsonify({"success": False, "error": "Please select a parking slot."}), 400
    if not start_time or not end_time:
        return jsonify({"success": False, "error": "Please set start and end times."}), 400

    vehicle = Vehicle.query.get(vehicle_id)
    if not vehicle:
        return jsonify({"success": False, "error": "Vehicle not found."}), 404

    if not current_user.is_admin and vehicle.user_id != current_user.id:
        abort(403)

    try:
        start_dt = datetime.fromisoformat(start_time)
        end_dt   = datetime.fromisoformat(end_time)
    except ValueError:
        return jsonify({"success": False, "error": "Invalid date format."}), 400

    if end_dt <= start_dt:
        return jsonify({"success": False, "error": "End time must be after start time."}), 400
    if end_dt <= datetime.utcnow():
        return jsonify({"success": False, "error": "Reservation must be in the future."}), 400

    # Check slot is valid
    slot_obj = ParkingSlot.query.filter_by(slot_code=slot_id, is_active=True).first()
    if not slot_obj:
        return jsonify({"success": False, "error": f"Slot {slot_id} is not available."}), 400

    overlap = (Reservation.query
               .filter(Reservation.slot_id    == slot_id,
                       Reservation.status     == "active",
                       Reservation.start_time <  end_dt,
                       Reservation.end_time   >  start_dt)
               .first())
    if overlap:
        return jsonify({"success": False,
                        "error": f"Slot {slot_id} is already booked during that time."}), 409

    # Calculate amount
    hours  = (end_dt - start_dt).total_seconds() / 3600
    rate   = float(SystemSettings.get("rate_per_hour", "2.0"))
    amount = round(hours * rate, 2)

    reservation = Reservation(
        user_id   =current_user.id,
        vehicle_id=vehicle.id,
        slot_id   =slot_id,
        start_time=start_dt,
        end_time  =end_dt,
        status    ="active",
        reference =reference,
        notes     =notes,
    )
    db.session.add(reservation)
    db.session.flush()   # get reservation.id

    payment = Payment(
        reservation_id=reservation.id,
        user_id       =current_user.id,
        amount        =amount,
        method        =pay_method,
        status        ="pending",
    )
    db.session.add(payment)
    db.session.commit()

    _log_event("reservation", "INFO",
               f"Reservation {reservation.id} by {current_user.username} "
               f"slot={slot_id} amount={amount} method={pay_method}")

    return jsonify({
        "success"    : True,
        "reservation": reservation.to_dict(),
        "amount"     : amount,
        "method"     : pay_method,
    })


# ── SSE stream ────────────────────────────────────────────────────────────────

@app.route("/api/stream")
@login_required
def api_stream():
    def generate():
        last_detection_id = None
        while True:
            try:
                sensor  = get_sensor()
                servo   = get_servo()
                payload = {
                    "distance_m"      : sensor.get_distance(),
                    "vehicle_detected": sensor.is_vehicle_detected(),
                    "gate_open"       : servo.is_open,
                    "ts"              : datetime.utcnow().isoformat(),
                    "latest_detection": None,  # Will be populated if new detection
                }
                
                # Include latest detection if new
                global _latest_detection
                if _latest_detection and _latest_detection.get("event_id") != last_detection_id:
                    payload["latest_detection"] = _latest_detection
                    last_detection_id = _latest_detection.get("event_id")
                
                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as e:
                log.debug("SSE error: %s", e)
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

    if not User.query.filter_by(username="admin").first():
        admin = User(username="admin", email="admin@parking.local", role="admin")
        admin.set_password("admin123")
        db.session.add(admin)

    defaults = {
        "simulation_mode"   : "true",
        "camera_mode"       : "pc",
        "sim_interval"      : "15",
        "distance_threshold": "2.0",
        "yolo_conf"         : "0.50",
        "maintenance_mode"  : "false",
        "auto_detect"       : "true",
        "rate_per_hour"     : "2.0",
        "gate_open_secs"    : str(cfg.SERVO_OPEN_SECS),
    }
    for k, v in defaults.items():
        if not SystemSettings.query.filter_by(key=k).first():
            db.session.add(SystemSettings(key=k, value=v))

    # Default parking slots
    if not ParkingSlot.query.first():
        for letter in "AB":
            for num in range(1, 4):
                db.session.add(ParkingSlot(slot_code=f"{letter}{num}", is_active=True))

    # Demo vehicles
    demo = [
        ("195 تونس 4705", "Ahmed Ben Ali",  "car"),
        ("88 صفاقس 1234",  "Fatima Zahra",   "car"),
        ("XYZ-7890",       "Mohamed Slim",   "truck"),
    ]
    for plate, owner, vtype in demo:
        if not Vehicle.query.filter_by(license_plate=plate).first():
            db.session.add(Vehicle(license_plate=plate, owner_name=owner,
                                   vehicle_type=vtype))

    db.session.commit()
    log.info("Database initialized ✓")


if __name__ == "__main__":
    with app.app_context():
        init_db()
        sim_mode = SystemSettings.get("simulation_mode", "true") == "true"
        if sim_mode:
            _start_simulation()
        # Start sensor monitor
        t = threading.Thread(target=_sensor_monitor_loop, daemon=True)
        t.start()

    app.run(host=cfg.HOST, port=cfg.PORT, debug=cfg.DEBUG,
            threaded=True, use_reloader=False)
