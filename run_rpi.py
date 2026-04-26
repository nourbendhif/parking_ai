#!/usr/bin/env python3
"""
Smart Parking System – RPi Entry Point
Run this on the Raspberry Pi (or PC for testing).

Usage:
  python run_rpi.py                    # Development (simulation on)
  SIMULATION_MODE=false python run_rpi.py  # Production with real hardware
  PORT=8080 python run_rpi.py          # Custom port
"""
import os
import sys

# ── Ensure project root is on path ────────────────────────────────────────────
ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(dotenv_path: str = ".env") -> None:
    path = os.path.join(ROOT, dotenv_path)
    if not os.path.isfile(path):
        return

    with open(path, encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()
sys.path.insert(0, ROOT)

from rpi import config as cfg
from rpi.backend.app import app, init_db, _start_simulation
from rpi.backend.models import SystemSettings


def main():
    print("=" * 60)
    print("  ParkSense AI – Smart Parking System v2.0")
    print("=" * 60)
    print(f"  Environment   : {cfg.ENV}")
    print(f"  Host          : {cfg.HOST}:{cfg.PORT}")
    print(f"  Database      : {cfg.DATABASE_URL}")
    print(f"  PC Endpoint   : {cfg.ZEROMQ_ENDPOINT_PC}")
    print(f"  Simulation    : {cfg.SIMULATION_MODE}")
    print("=" * 60)

    with app.app_context():
        init_db()
        sim_mode = SystemSettings.get("simulation_mode", "true") == "true"
        if sim_mode:
            print("  ⚡ Simulation loop starting…")
            _start_simulation()

    print(f"\n  🚀 Web interface: http://localhost:{cfg.PORT}")
    print("     Default login: admin / admin123\n")

    try:
        app.run(
            host=cfg.HOST,
            port=cfg.PORT,
            debug=cfg.DEBUG,
            threaded=True,
            use_reloader=False,   # Disable reloader to prevent double sim loop
        )
    except KeyboardInterrupt:
        print("\n  Shutting down…")


if __name__ == "__main__":
    main()
