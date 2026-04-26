"""
Smart Parking System - ZeroMQ Client (RPi → PC)
Fixed: proper socket lifecycle, no _socket reference bug, clear error reporting.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Optional

import numpy as np
import zmq

log = logging.getLogger(__name__)

_client: Optional["AIClient"] = None


def get_client() -> "AIClient":
    global _client
    if _client is None:
        from rpi import config as cfg
        _client = AIClient(cfg.ZEROMQ_ENDPOINT_PC, cfg.ZMQ_TIMEOUT_MS)
    return _client


class AIClient:
    """Non-blocking ZeroMQ REQ/REP client with automatic reconnection."""

    def __init__(self, endpoint: str, timeout_ms: int = 10_000):
        self.endpoint   = endpoint
        self.timeout_ms = timeout_ms
        self._ctx       = zmq.Context()
        self._connected = False
        self._last_error: str = ""

    # ─── Connection ─────────────────────────────────────────────────────────────

    def _create_socket(self) -> zmq.Socket:
        """Create a fresh REQ socket each call (REQ/REP requires this on retry)."""
        socket = self._ctx.socket(zmq.REQ)
        socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        socket.setsockopt(zmq.LINGER,   0)
        socket.setsockopt(zmq.CONNECT_TIMEOUT, min(self.timeout_ms, 3000))
        socket.connect(self.endpoint)
        log.debug("ZMQ socket created → %s", self.endpoint)
        return socket

    def is_connected(self) -> bool:
        try:
            result = self.ping()
            self._connected = result.get("success", False)
            if not self._connected:
                self._last_error = result.get("error", "ping failed")
        except Exception as e:
            self._connected = False
            self._last_error = str(e)
        return self._connected

    def ping(self) -> dict:
        return self._send({"command": "ping"})

    def get_last_error(self) -> str:
        return self._last_error

    # ─── Image processing ───────────────────────────────────────────────────────

    def process_image(self, frame: np.ndarray, save: bool = False) -> dict:
        import cv2
        _, buf  = cv2.imencode(".jpg", frame)
        b64_str = base64.b64encode(buf).decode()
        return self._send({"command": "process", "image_b64": b64_str, "save": save})

    def process_b64(self, b64_str: str, save: bool = False) -> dict:
        return self._send({"command": "process", "image_b64": b64_str, "save": save})

    def capture_from_pc(self) -> dict:
        """Ask PC to capture from its own camera."""
        return self._send({"command": "capture", "save": True})

    # ─── Internal ───────────────────────────────────────────────────────────────

    def _send(self, payload: dict) -> dict:
        """
        Send a request and return the response.
        Creates a fresh socket for every attempt to avoid REQ state-machine issues.
        """
        message = json.dumps(payload).encode()

        for attempt in range(2):
            socket = None
            try:
                socket = self._create_socket()
                socket.send(message)
                raw = socket.recv()
                result = json.loads(raw)
                self._connected = True
                self._last_error = ""
                return result

            except zmq.Again as e:
                self._last_error = f"Timeout (attempt {attempt + 1}): {e}"
                log.warning("ZMQ receive timeout attempt %d: %s", attempt + 1, e)

            except zmq.ZMQError as e:
                self._last_error = f"ZMQ error (attempt {attempt + 1}): {e}"
                log.warning("ZMQ send error attempt %d: %s", attempt + 1, e)

            except Exception as e:
                self._last_error = f"Unexpected error: {e}"
                log.error("ZMQ unexpected error: %s", e)

            finally:
                if socket is not None:
                    try:
                        socket.close(linger=0)
                    except Exception:
                        pass

            # Small backoff between attempts
            if attempt == 0:
                time.sleep(0.2)

        self._connected = False
        return {
            "success":    False,
            "error":      self._last_error or "ZeroMQ unreachable after 2 attempts",
            "detections": [],
        }

    def close(self):
        try:
            self._ctx.term()
            log.info("ZMQ client closed")
        except Exception:
            pass
