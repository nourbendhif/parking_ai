#!/usr/bin/env python3
"""
Smart Parking System – PC AI Server Entry Point
Run this on the PC with GPU/camera and model.pt.

Usage:
  python run_pc.py                           # Default (port 5555, camera preview ON)
  python run_pc.py --no-camera               # Disable live camera window
  SHOW_CAMERA=false python run_pc.py         # Same via env var
  PC_PORT=5556 python run_pc.py              # Custom port
  SIMULATION_MODE=true python run_pc.py      # Simulate AI (no model needed)
  MODEL_PATH=/path/to/model.pt python run_pc.py
"""
import os
import sys

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

from pc import config as cfg


def main():
    # Camera flag must be set BEFORE importing server (it reads env at import time)
    if "--no-camera" in sys.argv:
        os.environ["SHOW_CAMERA"] = "false"

    from pc.zmq_server.server import AIServer
    try:
        AIServer().start()
    except KeyboardInterrupt:
        pass  # Server prints its own shutdown message


if __name__ == "__main__":
    main()
