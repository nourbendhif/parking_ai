# ParkSense AI v2.0 — Smart Parking Management System

A production-ready smart parking system with AI-powered license plate detection,
real-time dashboard, full simulation mode, and PC ↔ Raspberry Pi synchronization.

---

## 🏗 Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                      Web Browser                         │
│           http://rpi-ip:5000  (Dashboard + Admin)        │
└────────────────────────┬─────────────────────────────────┘
                         │ HTTP / SSE
┌────────────────────────▼─────────────────────────────────┐
│             Raspberry Pi  (Flask App)                     │
│  • Web server + REST API                                  │
│  • SQLite database                                        │
│  • HC-SR04 ultrasonic sensor                             │
│  • SG90 servo motor (gate)                               │
│  • Simulation engine                                      │
└────────────────────────┬─────────────────────────────────┘
                         │ ZeroMQ TCP (port 5555)
┌────────────────────────▼─────────────────────────────────┐
│                   PC  (AI Server)                         │
│  • YOLO v8 license plate detection                       │
│  • EasyOCR text extraction (Arabic + English)            │
│  • Optional: PC webcam capture                           │
└──────────────────────────────────────────────────────────┘
```

---

## ⚡ Quick Start (Simulation Mode — No Hardware Needed)

```bash
# 1. Clone / unzip the project
cd smart_parking

# 2. Install RPi dependencies (works on any PC too)
pip install -r requirements_rpi.txt

# 3. Copy environment file
cp .env.example .env
# Edit .env if needed (defaults work out of the box)

# 4. Run the web server
python run_rpi.py
```

Open **http://localhost:5000** → Login: `admin / admin123`

The system starts in **simulation mode** by default — no Raspberry Pi,
no camera, no model.pt required. Everything is simulated inside the browser.

---

## 🔧 Full Production Setup

### Step 1 — PC AI Server

```bash
# On the PC with GPU/camera
pip install -r requirements_pc.txt

# Place your YOLO model
cp your_model.pt pc/model/model.pt

# Configure PC IP in .env
# PC_IP=192.168.1.100  ← your PC's LAN IP

# Start the AI server
python run_pc.py
```

### Step 2 — Raspberry Pi Web Server

```bash
# On the RPi
pip install -r requirements_rpi.txt
pip install RPi.GPIO

# Edit .env:
# PC_IP=192.168.1.100  ← same as above
# SIMULATION_MODE=false
# CAMERA_MODE=pc        # or rpi

python run_rpi.py
```

### Step 3 — Hardware Wiring

**Ultrasonic Sensor (HC-SR04):**
```
VCC  → 5V  (Pin 2)
GND  → GND (Pin 6)
TRIG → GPIO 23 (Pin 16)
ECHO → GPIO 24 (Pin 18)  [use 1kΩ + 2kΩ voltage divider]
```

**Servo Motor (SG90):**
```
Red   → 5V  (Pin 4)
Brown → GND (Pin 9)
Orange → GPIO 18 (Pin 12) [PWM]
```

---

## 📁 Project Structure

```
smart_parking/
├── run_rpi.py              ← RPi entry point (start here)
├── run_pc.py               ← PC AI server entry point
├── .env.example            ← Environment config template
├── requirements_rpi.txt    ← RPi Python dependencies
├── requirements_pc.txt     ← PC Python dependencies
│
├── rpi/                    ← Raspberry Pi code
│   ├── config.py           ← RPi configuration
│   ├── backend/
│   │   ├── app.py          ← Flask app (25+ routes, REST API)
│   │   └── models.py       ← SQLAlchemy models (6 tables)
│   ├── sensors/
│   │   └── ultrasonic.py   ← HC-SR04 driver + simulation
│   ├── servo/
│   │   └── servo.py        ← SG90 driver + simulation
│   └── zmq_client/
│       └── client.py       ← ZeroMQ client (RPi → PC)
│
├── pc/                     ← PC AI server code
│   ├── config.py           ← PC configuration
│   ├── detection/
│   │   └── ai_processor.py ← YOLO + EasyOCR pipeline
│   ├── zmq_server/
│   │   └── server.py       ← ZeroMQ REP server
│   └── model/
│       └── model.pt        ← Your YOLO model (place here)
│
├── web/                    ← Frontend
│   ├── templates/          ← Jinja2 HTML templates
│   │   ├── base.html       ← Layout (sidebar + topbar)
│   │   ├── login.html
│   │   ├── register.html
│   │   ├── dashboard.html  ← Live dashboard + detection
│   │   ├── admin_dashboard.html ← Analytics + sim controls
│   │   ├── admin_users.html
│   │   ├── admin_vehicles.html
│   │   ├── admin_events.html
│   │   ├── admin_logs.html
│   │   ├── admin_settings.html
│   │   └── error.html
│   └── static/
│       ├── css/style.css   ← Full dark theme stylesheet
│       └── js/app.js       ← Core JS (clock, modals, polling)
│
└── database/               ← Auto-created at runtime
    ├── parking.db          ← SQLite database
    ├── captures/           ← Saved detection images
    └── logs/               ← Log files
```

---

## 🌐 REST API Reference

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/detect` | User | Trigger detection (JSON or file upload) |
| POST | `/api/simulate_event` | Admin | Fire manual simulation event |
| GET | `/api/status` | User | Full system status (sensor, gate, mode) |
| GET | `/api/stream` | User | SSE real-time stream (2s updates) |
| GET | `/api/sensor/status` | User | Ultrasonic sensor reading |
| POST | `/api/sensor/sim` | Admin | Set simulated vehicle presence |
| POST | `/api/gate/open` | Admin | Open gate manually |
| POST | `/api/gate/close` | Admin | Close gate manually |
| GET | `/api/gate/status` | User | Gate state |
| GET | `/api/events/recent` | User | Recent detection events |
| GET | `/api/logs/recent` | Admin | Recent system logs |
| GET | `/api/stats/events` | User | Events chart data (last N days) |
| GET | `/api/vehicles` | User | All registered vehicles |

### Example: Trigger Detection

```bash
# From file
curl -X POST http://localhost:5000/api/detect \
  -H "Content-Type: application/json" \
  -d '{"image_b64": "<base64_jpeg_string>"}' \
  -b "session=..."

# Response
{
  "success": true,
  "plate": "ABC-1234",
  "confidence": 0.94,
  "authorized": true,
  "gate_opened": true,
  "event_id": 42,
  "annotated_b64": "...",
  "simulated": false
}
```

---

## 🎛 Admin Panel Features

| Feature | Path |
|---------|------|
| Analytics + Charts | `/admin/dashboard` |
| Simulation Control | `/admin/dashboard` (bottom panel) |
| User Management | `/admin/users` |
| Vehicle Registry | `/admin/vehicles` |
| Event History + CSV Export | `/admin/events` |
| Live + Historical Logs | `/admin/logs` |
| System Settings | `/admin/settings` |
| Connection Diagnostics | `/admin/settings` (bottom) |

---

## 🔬 Simulation Mode

Simulation mode lets you test the **full system** without any hardware:

1. Go to **Admin → Settings** → Enable **Simulation Mode**
2. Set auto-fire interval (default: 15 seconds)
3. Use **Admin → Analytics** → **Simulation Control Center** to:
   - Manually fire detection events
   - Place/remove virtual vehicles
   - Open/close virtual gate
4. Watch results live on the dashboard

The simulation:
- Generates realistic random license plates
- Produces annotated camera frames (drawn programmatically)
- Checks the database for authorization
- Opens the gate if authorized
- Logs everything to the database

---

## 🔐 Default Credentials

| Role | Username | Password |
|------|----------|----------|
| Admin | `admin` | `admin123` |

**Change the password immediately in production!**

---

## 🚀 Production Deployment

```bash
# Install gunicorn
pip install gunicorn

# Run with gunicorn (RPi)
gunicorn -w 2 -b 0.0.0.0:5000 --threads 4 "rpi.backend.app:app"

# Or with systemd (recommended)
# Create /etc/systemd/system/parksense.service
```

**systemd service example:**
```ini
[Unit]
Description=ParkSense AI
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/smart_parking
ExecStart=/usr/bin/python3 run_rpi.py
Restart=always
EnvironmentFile=/home/pi/smart_parking/.env

[Install]
WantedBy=multi-user.target
```

---

## 🐛 Troubleshooting

| Problem | Solution |
|---------|----------|
| PC not connecting | Check `PC_IP` in `.env`, ensure port 5555 is open |
| Camera not found | Set `CAMERA_INDEX=1` or check `ls /dev/video*` |
| RPi.GPIO error | Install `sudo apt install python3-rpi.gpio` |
| Model not found | Place `model.pt` in `pc/model/` or set `MODEL_PATH` |
| Database locked | Delete `database/parking.db` and restart |
| Gate not moving | Check `SERVO_PIN` and PWM permissions |

---

## 📦 Tech Stack

| Layer | Technology |
|-------|-----------|
| Web Framework | Flask 2.3 |
| Database | SQLite (via SQLAlchemy) |
| AI Detection | YOLOv8 (Ultralytics) |
| OCR | EasyOCR (Arabic + English) |
| Messaging | ZeroMQ (REQ/REP) |
| Real-time | Server-Sent Events (SSE) |
| Frontend | Vanilla JS + Chart.js |
| Styling | Custom CSS (dark industrial theme) |
| Hardware | RPi.GPIO (HC-SR04 + SG90) |
