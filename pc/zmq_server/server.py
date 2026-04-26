"""
Smart Parking System - PC ZeroMQ AI Server
Verbose terminal output: connection status, received orders, detection results.
Optional live camera window (toggle with --no-camera or SHOW_CAMERA=false).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime

import cv2
import numpy as np
import zmq

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from pc import config as cfg
from pc.detection.ai_processor import get_processor

# ── Coloured terminal helpers ─────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BLUE   = "\033[94m"
MAGENTA = "\033[95m"
DIM    = "\033[2m"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log_info(msg: str):
    print(f"{DIM}[{_ts()}]{RESET} {GREEN}✔{RESET}  {msg}")


def log_warn(msg: str):
    print(f"{DIM}[{_ts()}]{RESET} {YELLOW}⚠{RESET}  {msg}")


def log_error(msg: str):
    print(f"{DIM}[{_ts()}]{RESET} {RED}✖{RESET}  {msg}")


def log_recv(msg: str):
    print(f"{DIM}[{_ts()}]{RESET} {CYAN}↓{RESET}  {CYAN}{msg}{RESET}")


def log_send(msg: str):
    print(f"{DIM}[{_ts()}]{RESET} {MAGENTA}↑{RESET}  {MAGENTA}{msg}{RESET}")


def log_section(title: str):
    print(f"\n{BOLD}{BLUE}{'─'*60}{RESET}")
    print(f"{BOLD}{BLUE}  {title}{RESET}")
    print(f"{BOLD}{BLUE}{'─'*60}{RESET}\n")


# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
log = logging.getLogger("zmq_server")

# ── Camera preview control ────────────────────────────────────────────────────
SHOW_CAMERA = os.environ.get("SHOW_CAMERA", "true").lower() == "true"
if "--no-camera" in sys.argv:
    SHOW_CAMERA = False

_camera_window_open = False


class AIServer:
    """ZeroMQ REP server with rich terminal diagnostics and optional camera preview."""

    def __init__(self):
        self.ctx      = zmq.Context()
        self.socket   = self.ctx.socket(zmq.REP)
        self.endpoint = cfg.ZEROMQ_ENDPOINT_PC
        self.running  = False
        self._stats   = {
            "requests":      0,
            "errors":        0,
            "avg_ms":        0.0,
            "pings":         0,
            "detections":    0,
            "captures":      0,
            "clients_seen":  set(),
        }
        self._connected_clients: set[str] = set()

        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    # ─── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        self.socket.bind(self.endpoint)

        log_section("ParkSense AI — PC ZeroMQ Server v2.0")
        log_info(f"Endpoint     : {BOLD}{self.endpoint}{RESET}")
        log_info(f"Model path   : {cfg.MODEL_PATH}")
        log_info(f"OCR langs    : {cfg.OCR_LANGUAGES}")
        log_info(f"OCR device   : {cfg.OCR_DEVICE}")
        log_info(f"YOLO conf    : {cfg.YOLO_CONF}")
        log_info(f"Sim mode     : {YELLOW if cfg.SIMULATION_MODE else GREEN}"
                 f"{cfg.SIMULATION_MODE}{RESET}")
        log_info(f"Camera prev  : {GREEN if SHOW_CAMERA else DIM}{SHOW_CAMERA}{RESET}"
                 f"{DIM}  (set SHOW_CAMERA=false or pass --no-camera to disable){RESET}")
        print()

        # Pre-load AI model
        log_info("Loading AI models…")
        self.processor = get_processor()
        log_info(f"{GREEN}AI models ready ✓{RESET}")
        print()

        log_info(f"{GREEN}{BOLD}🚀 Server ONLINE — waiting for RPi connections…{RESET}")
        log_info(f"{DIM}Press Ctrl+C to shut down{RESET}\n")

        self.running = True

        while self.running:
            try:
                self._handle_request()
            except zmq.ZMQError as e:
                if self.running:
                    log_error(f"ZMQ error: {e}")
            except Exception as e:
                if self.running:
                    log_error(f"Unexpected error: {e}")
                    import traceback
                    traceback.print_exc()

        self._cleanup()

    def _shutdown(self, *_):
        print()
        log_warn("Shutdown signal received…")
        self.running = False
        self.socket.close(linger=0)
        if SHOW_CAMERA:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

    def _cleanup(self):
        try:
            self.socket.close(linger=0)
        except Exception:
            pass
        try:
            self.ctx.term()
        except Exception:
            pass

        log_section("Server Shutdown — Final Stats")
        log_info(f"Total requests  : {self._stats['requests']}")
        log_info(f"Pings           : {self._stats['pings']}")
        log_info(f"Detections      : {self._stats['detections']}")
        log_info(f"Captures        : {self._stats['captures']}")
        log_info(f"Errors          : {self._stats['errors']}")
        log_info(f"Avg latency     : {self._stats['avg_ms']:.1f} ms")
        log_info(f"Unique clients  : {len(self._stats['clients_seen'])}")
        print()

    # ─── Request handling ───────────────────────────────────────────────────────

    def _handle_request(self):
        """Wait for a message, process it, send response — with full terminal output."""
        # Poll with timeout so we can check self.running
        if not self.socket.poll(timeout=1000):
            return

        raw = self.socket.recv()
        t0  = time.time()

        # Log receipt
        client_addr = "RPi"   # ZMQ REP doesn't expose IP directly
        log_recv(f"Request received ({len(raw)} bytes)")

        try:
            request  = json.loads(raw)
            command  = request.get("command", "process")
            log_recv(f"Command: {BOLD}{command.upper()}{RESET}")

            response = self._process_request(request)

        except json.JSONDecodeError as e:
            log_error(f"Invalid JSON: {e}")
            response = {"success": False, "error": f"Invalid JSON: {e}", "detections": []}
            self._stats["errors"] += 1
        except Exception as e:
            log_error(f"Request processing error: {e}")
            import traceback
            traceback.print_exc()
            response = {"success": False, "error": str(e), "detections": []}
            self._stats["errors"] += 1

        elapsed = time.time() - t0
        self._update_stats(elapsed)

        response["server_ms"] = int(elapsed * 1000)

        # Log response
        resp_size = len(json.dumps(response))
        log_send(f"Response sent   ({resp_size} bytes, {elapsed*1000:.0f}ms)")
        if response.get("success"):
            dets = response.get("detections", [])
            if dets:
                for d in dets:
                    plate = d.get("text", "N/A")
                    conf  = d.get("conf", 0)
                    log_send(f"  → Plate: {BOLD}{plate}{RESET}  "
                             f"conf={GREEN}{conf:.1%}{RESET}")
            elif command != "ping":
                log_send(f"  → No plates detected")
        else:
            log_error(f"  → Error: {response.get('error', 'unknown')}")

        self.socket.send(json.dumps(response).encode())

        # Optional camera preview
        if SHOW_CAMERA and response.get("annotated_b64"):
            self._show_camera_preview(response["annotated_b64"], response.get("detections", []))

    def _process_request(self, request: dict) -> dict:
        command = request.get("command", "process")

        # ── PING ──────────────────────────────────────────────────────────────
        if command == "ping":
            self._stats["pings"] += 1
            log_info(f"{GREEN}Ping from RPi — connection healthy ✓{RESET}")
            return {
                "success": True,
                "pong":    True,
                "mode":    "simulation" if cfg.SIMULATION_MODE else "real",
                "server_info": {
                    "model_loaded": self.processor is not None,
                    "simulation":   cfg.SIMULATION_MODE,
                    "endpoint":     self.endpoint,
                    "uptime_reqs":  self._stats["requests"],
                }
            }

        # ── STATS ─────────────────────────────────────────────────────────────
        if command == "stats":
            return {"success": True, "stats": {
                k: list(v) if isinstance(v, set) else v
                for k, v in self._stats.items()
            }}

        # ── CAPTURE (PC camera) ───────────────────────────────────────────────
        if command == "capture":
            self._stats["captures"] += 1
            log_info("PC camera capture requested…")
            frame = self._capture_from_camera()
            if frame is None:
                log_error("Camera not available")
                return {"success": False, "error": "Camera not available", "detections": []}
            log_info(f"Frame captured: {frame.shape[1]}×{frame.shape[0]}")
            result = self.processor.process_image(frame)
            if request.get("save", False) and result.get("success"):
                saved = self._save_annotated(frame, result)
                result["saved_path"] = saved
                log_info(f"Saved to: {saved}")
            self._stats["detections"] += 1
            return result

        # ── PROCESS (image from RPi) ──────────────────────────────────────────
        img_b64 = request.get("image_b64")
        if not img_b64:
            # Fallback to PC camera
            log_warn("No image provided — falling back to PC camera")
            frame = self._capture_from_camera()
            if frame is None:
                return {"success": False, "error": "No image and no camera", "detections": []}
        else:
            log_info(f"Decoding image (b64 len={len(img_b64)})…")
            img_bytes = base64.b64decode(img_b64)
            arr       = np.frombuffer(img_bytes, np.uint8)
            frame     = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                log_error("Image decode failed")
                return {"success": False, "error": "Image decode failed", "detections": []}
            log_info(f"Image decoded: {frame.shape[1]}×{frame.shape[0]}")

        log_info("Running AI detection pipeline…")
        result = self.processor.process_image(frame)

        if result.get("success"):
            n_det = len(result.get("detections", []))
            log_info(f"{GREEN}Detection complete:{RESET} {n_det} plate(s) found "
                     f"in {result.get('processing_ms', 0)} ms")
            self._stats["detections"] += 1
        else:
            log_error(f"Detection failed: {result.get('error')}")

        if request.get("save", False) and result.get("success"):
            saved = self._save_annotated(frame, result)
            result["saved_path"] = saved

        return result

    # ─── Camera preview ──────────────────────────────────────────────────────

    def _show_camera_preview(self, b64: str, detections: list):
        """Show annotated detection result in an OpenCV window."""
        global _camera_window_open
        try:
            img_bytes = base64.b64decode(b64)
            arr       = np.frombuffer(img_bytes, np.uint8)
            frame     = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                return

            # Add HUD overlay
            h, w = frame.shape[:2]
            ts_str = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
            n_det  = len(detections)

            # Semi-transparent top bar
            overlay = frame.copy()
            cv2.rectangle(overlay, (0, 0), (w, 36), (10, 10, 20), -1)
            frame = cv2.addWeighted(overlay, 0.7, frame, 0.3, 0)

            cv2.putText(frame, f"ParkSense AI  |  {ts_str}  |  {n_det} plate(s)",
                        (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 212, 180), 1,
                        cv2.LINE_AA)

            cv2.namedWindow("ParkSense AI — Camera Preview", cv2.WINDOW_NORMAL)
            cv2.imshow("ParkSense AI — Camera Preview", frame)
            _camera_window_open = True

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q') or key == 27:   # q or Esc
                global SHOW_CAMERA
                SHOW_CAMERA = False
                cv2.destroyAllWindows()
                _camera_window_open = False
                log_warn("Camera preview closed (press Esc/Q). "
                         "Restart with SHOW_CAMERA=true to re-enable.")
        except Exception as e:
            log_warn(f"Camera preview error: {e}")

    # ─── Helpers ────────────────────────────────────────────────────────────────

    def _capture_from_camera(self) -> np.ndarray | None:
        cap = cv2.VideoCapture(cfg.CAMERA_INDEX)
        if not cap.isOpened():
            return None
        ret, frame = cap.read()
        cap.release()
        return frame if ret else None

    def _save_annotated(self, frame: np.ndarray, result: dict) -> str:
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(cfg.CAPTURES_DIR, f"capture_{ts}.jpg")
        os.makedirs(cfg.CAPTURES_DIR, exist_ok=True)
        cv2.imwrite(path, frame)
        return path

    def _update_stats(self, elapsed: float):
        self._stats["requests"] += 1
        n = self._stats["requests"]
        self._stats["avg_ms"] = (
            self._stats["avg_ms"] * (n - 1) + elapsed * 1000
        ) / n


if __name__ == "__main__":
    AIServer().start()
