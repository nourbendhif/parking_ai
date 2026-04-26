"""
Smart Parking System - Raspberry Pi Configuration
"""
import os
import secrets

# ─── Environment ───────────────────────────────────────────────────────────────
ENV        = os.environ.get("FLASK_ENV", "development")
DEBUG      = ENV != "production"
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# ─── Database ──────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DB_DIR       = os.path.join(BASE_DIR, "..", "database")
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(DB_DIR, 'parking.db')}")
os.makedirs(DB_DIR, exist_ok=True)

CAPTURES_DIR = os.path.join(DB_DIR, "captures")
LOGS_DIR     = os.path.join(DB_DIR, "logs")
os.makedirs(CAPTURES_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# ─── ZeroMQ / PC Connection ────────────────────────────────────────────────────
PC_IP              = os.environ.get("PC_IP",   "127.0.0.1")
PC_PORT            = int(os.environ.get("PC_PORT", 5555))
ZEROMQ_ENDPOINT_PC = f"tcp://{PC_IP}:{PC_PORT}"
ZMQ_TIMEOUT_MS     = int(os.environ.get("ZMQ_TIMEOUT_MS", 10_000))  # 10 s

# ─── GPIO (Raspberry Pi hardware) ─────────────────────────────────────────────
ULTRASONIC_TRIGGER = int(os.environ.get("ULTRASONIC_TRIGGER", 23))
ULTRASONIC_ECHO    = int(os.environ.get("ULTRASONIC_ECHO",    24))
DISTANCE_THRESHOLD = float(os.environ.get("DISTANCE_THRESHOLD", 2.0))  # meters

SERVO_PIN         = int(os.environ.get("SERVO_PIN",          18))
SERVO_OPEN_ANGLE  = int(os.environ.get("SERVO_OPEN_ANGLE",   90))
SERVO_CLOSE_ANGLE = int(os.environ.get("SERVO_CLOSE_ANGLE",   0))
SERVO_OPEN_SECS   = float(os.environ.get("SERVO_OPEN_SECS",  3.0))

# ─── Camera ────────────────────────────────────────────────────────────────────
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))
CAMERA_MODE  = os.environ.get("CAMERA_MODE", "pc")  # "pc" | "rpi"

# ─── Simulation ────────────────────────────────────────────────────────────────
SIMULATION_MODE = os.environ.get("SIMULATION_MODE", "true").lower() == "true"

# ─── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# ─── Web ───────────────────────────────────────────────────────────────────────
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 5000))
