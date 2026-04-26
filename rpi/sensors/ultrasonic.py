"""
Smart Parking System - Ultrasonic Sensor Driver (HC-SR04)
Falls back to simulation when RPi.GPIO is unavailable.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Optional

log = logging.getLogger(__name__)

# Try to import GPIO; graceful fallback
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    GPIO_AVAILABLE = False
    log.warning("RPi.GPIO not available – ultrasonic will simulate")


class UltrasonicSensor:
    """HC-SR04 distance sensor with simulation fallback."""

    SPEED_OF_SOUND = 343.0   # m/s at 20°C

    def __init__(self, trigger_pin: int = 23, echo_pin: int = 24,
                 threshold: float = 2.0, simulate: bool = False):
        self.trigger_pin = trigger_pin
        self.echo_pin    = echo_pin
        self.threshold   = threshold
        self.simulate    = simulate or not GPIO_AVAILABLE
        self._sim_state  = "clear"   # "clear" | "vehicle"
        self._sim_dist   = 5.0

        if not self.simulate:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(trigger_pin, GPIO.OUT)
            GPIO.setup(echo_pin,    GPIO.IN)
            GPIO.output(trigger_pin, False)
            time.sleep(0.1)
            log.info("Ultrasonic sensor init (trigger=%d echo=%d threshold=%.1f m)",
                     trigger_pin, echo_pin, threshold)

    # ─── Public API ─────────────────────────────────────────────────────────────

    def get_distance(self) -> float:
        """Return distance in metres (-1.0 on error)."""
        if self.simulate:
            return self._sim_get_distance()
        return self._real_get_distance()

    def is_vehicle_detected(self) -> bool:
        dist = self.get_distance()
        return 0 < dist <= self.threshold

    def set_sim_vehicle(self, present: bool):
        """Simulation control: toggle virtual vehicle."""
        if present:
            self._sim_dist  = round(random.uniform(0.3, self.threshold - 0.1), 2)
            self._sim_state = "vehicle"
        else:
            self._sim_dist  = round(random.uniform(self.threshold + 0.5, 6.0), 2)
            self._sim_state = "clear"

    def cleanup(self):
        if not self.simulate and GPIO_AVAILABLE:
            GPIO.cleanup([self.trigger_pin, self.echo_pin])

    # ─── Real hardware ──────────────────────────────────────────────────────────

    def _real_get_distance(self) -> float:
        try:
            # Send 10µs trigger pulse
            GPIO.output(self.trigger_pin, True)
            time.sleep(0.00001)
            GPIO.output(self.trigger_pin, False)

            pulse_start = time.time()
            timeout     = pulse_start + 0.04   # 40 ms max

            while GPIO.input(self.echo_pin) == 0:
                pulse_start = time.time()
                if pulse_start > timeout:
                    return -1.0

            pulse_end = time.time()
            timeout   = pulse_end + 0.04
            while GPIO.input(self.echo_pin) == 1:
                pulse_end = time.time()
                if pulse_end > timeout:
                    return -1.0

            duration = pulse_end - pulse_start
            return round(duration * self.SPEED_OF_SOUND / 2, 3)
        except Exception as e:
            log.error("Ultrasonic read error: %s", e)
            return -1.0

    # ─── Simulation ─────────────────────────────────────────────────────────────

    def _sim_get_distance(self) -> float:
        # Add small noise
        noise = random.uniform(-0.05, 0.05)
        return round(max(0.01, self._sim_dist + noise), 3)
