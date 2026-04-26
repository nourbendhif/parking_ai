"""
Smart Parking System - PC AI Server Configuration
"""
import os

# ─── Environment ───────────────────────────────────────────────────────────────
ENV = os.environ.get("ZEROMQ_MODE", "development")
PRODUCTION = ENV == "production"

# ─── Network ───────────────────────────────────────────────────────────────────
# Bind to all interfaces by default so remote RPi connections work when no .env is loaded.
PC_IP   = os.environ.get("PC_IP",   "0.0.0.0")
PC_PORT = int(os.environ.get("PC_PORT", 5555))
ZEROMQ_ENDPOINT_PC = f"tcp://{PC_IP}:{PC_PORT}"

# ─── AI Model ──────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.environ.get("MODEL_PATH", os.path.join(BASE_DIR, "model", "model.pt"))

YOLO_CONF = float(os.environ.get("YOLO_CONF", 0.50))
YOLO_IOU  = float(os.environ.get("YOLO_IOU",  0.45))
PLATE_PAD = int(os.environ.get("PLATE_PAD",   10))

# ─── OCR ───────────────────────────────────────────────────────────────────────
OCR_LANGUAGES = ["ar", "en"]
OCR_CONF      = float(os.environ.get("OCR_CONF", 0.45))
OCR_DEVICE    = os.environ.get("OCR_DEVICE", "cpu")   # "cpu" | "cuda"

# ─── Camera ────────────────────────────────────────────────────────────────────
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))

# ─── Storage ───────────────────────────────────────────────────────────────────
CAPTURES_DIR = os.path.join(BASE_DIR, "..", "database", "captures")
os.makedirs(CAPTURES_DIR, exist_ok=True)

# ─── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_DIR   = os.path.join(BASE_DIR, "..", "database", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ─── Simulation ────────────────────────────────────────────────────────────────
SIMULATION_MODE = os.environ.get("SIMULATION_MODE", "false").lower() == "true"
