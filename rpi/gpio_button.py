"""
Smart Parking System - GPIO Button Handler
Optional physical button on RPi GPIO pin that triggers Capture & Detect.
Runs as a background thread alongside the Flask app.
"""
from __future__ import annotations

import logging
import threading
import time
import requests

log = logging.getLogger(__name__)

# ── Try to import GPIO ────────────────────────────────────────────────────────
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    GPIO_AVAILABLE = False
    log.info("RPi.GPIO not available – GPIO button handler disabled")

# Default button pin (can override via env)
import os
BUTTON_PIN     = int(os.environ.get("BUTTON_PIN", 25))   # GPIO 25
DEBOUNCE_MS    = int(os.environ.get("BUTTON_DEBOUNCE_MS", 300))
FLASK_BASE_URL = os.environ.get("FLASK_BASE_URL", "http://127.0.0.1:5000")

_button_thread: threading.Thread | None = None
_running = False


def _button_loop():
    """Poll the GPIO button and POST to /api/gpio/capture when pressed."""
    global _running

    if not GPIO_AVAILABLE:
        log.warning("GPIO not available – button loop exiting")
        return

    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # active LOW
    log.info("🔘 GPIO button handler active (pin=%d, URL=%s)", BUTTON_PIN, FLASK_BASE_URL)

    last_press = 0.0

    while _running:
        try:
            # Active LOW: button pressed = GPIO.LOW
            if GPIO.input(BUTTON_PIN) == GPIO.LOW:
                now = time.time()
                if now - last_press > DEBOUNCE_MS / 1000.0:
                    last_press = now
                    log.info("🔘 Physical button pressed – triggering capture")
                    _post_capture()
                time.sleep(0.05)
            else:
                time.sleep(0.05)
        except Exception as e:
            log.error("Button loop error: %s", e)
            time.sleep(0.5)

    GPIO.cleanup([BUTTON_PIN])
    log.info("GPIO button handler stopped")


def _post_capture():
    """POST to the Flask /api/gpio/capture endpoint."""
    try:
        # We need a session cookie – use a local service account token
        # For simplicity on localhost, we use the internal session bypass
        # (Flask is on the same machine, so we call it directly)
        resp = requests.post(
            f"{FLASK_BASE_URL}/api/gpio/capture",
            timeout=15,
            headers={"X-GPIO-Button": "1"},   # marker header
        )
        if resp.ok:
            data = resp.json()
            log.info("Button detect result: plate=%s authorized=%s",
                     data.get("plate"), data.get("authorized"))
        else:
            log.warning("Button capture returned HTTP %d", resp.status_code)
    except requests.RequestException as e:
        log.error("Button capture POST failed: %s", e)


def start_button_handler():
    """Start the GPIO button monitoring thread."""
    global _button_thread, _running
    if not GPIO_AVAILABLE:
        return
    if _running:
        return
    _running       = True
    _button_thread = threading.Thread(target=_button_loop, daemon=True, name="gpio-button")
    _button_thread.start()
    log.info("GPIO button handler thread started")


def stop_button_handler():
    """Stop the GPIO button monitoring thread."""
    global _running
    _running = False
