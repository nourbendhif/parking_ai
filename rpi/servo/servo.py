"""
Smart Parking System - Servo Motor Driver (SG90)
Falls back to simulation when RPi.GPIO is unavailable.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    GPIO_AVAILABLE = False
    log.warning("RPi.GPIO not available – servo will simulate")


class ServoMotor:
    """SG90 servo with simulation fallback."""

    def __init__(self, pin: int = 18, open_angle: int = 90,
                 close_angle: int = 0, open_secs: float = 3.0,
                 simulate: bool = False):
        self.pin         = pin
        self.open_angle  = open_angle
        self.close_angle = close_angle
        self.open_secs   = open_secs
        self.simulate    = simulate or not GPIO_AVAILABLE
        self._pwm        = None
        self._lock       = threading.Lock()
        self._state      = "closed"  # "open" | "closed"

        if not self.simulate:
            if not GPIO.getmode():
                GPIO.setmode(GPIO.BCM)
            GPIO.setup(pin, GPIO.OUT)
            self._pwm = GPIO.PWM(pin, 50)   # 50 Hz
            self._pwm.start(0)
            log.info("Servo init (pin=%d open=%d° close=%d°)", pin, open_angle, close_angle)
        else:
            log.info("Servo SIMULATION (open=%d° close=%d°)", open_angle, close_angle)

    # ─── Public API ─────────────────────────────────────────────────────────────

    def open_gate(self, auto_close: bool = True):
        """Open gate, optionally auto-close after `open_secs`."""
        with self._lock:
            self.set_angle(self.open_angle)
            self._state = "open"
            log.info("🔓 Gate OPENED")

        if auto_close:
            t = threading.Timer(self.open_secs, self.close_gate)
            t.daemon = True
            t.start()

    def close_gate(self):
        with self._lock:
            self.set_angle(self.close_angle)
            self._state = "closed"
            log.info("🔒 Gate CLOSED")

    def set_angle(self, angle: int):
        angle = max(0, min(180, angle))
        if self.simulate:
            self._state = "open" if angle >= self.open_angle else "closed"
            log.debug("SIM servo angle → %d°", angle)
            return
        duty = angle / 18.0 + 2.5
        self._pwm.ChangeDutyCycle(duty)
        time.sleep(0.5)
        self._pwm.ChangeDutyCycle(0)   # Stop jitter

    @property
    def is_open(self) -> bool:
        return self._state == "open"

    def cleanup(self):
        if self._pwm:
            self._pwm.stop()
        if not self.simulate and GPIO_AVAILABLE:
            GPIO.cleanup([self.pin])
