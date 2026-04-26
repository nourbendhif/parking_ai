"""
Smart Parking System - AI Processor
Handles YOLO detection + EasyOCR license-plate extraction.
Falls back to simulation mode when hardware/model is absent.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import random
import string
import time
from typing import Optional

import cv2
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# ── Lazy imports (heavy) ──────────────────────────────────────────────────────
_yolo  = None
_ocr   = None
_ready = False


def _load_models(model_path: str, ocr_languages: list, ocr_device: str) -> bool:
    global _yolo, _ocr, _ready
    try:
        from ultralytics import YOLO
        log.info("Loading YOLO model: %s", model_path)
        _yolo = YOLO(model_path)
        log.info("YOLO loaded ✓")
    except Exception as e:
        log.warning("YOLO load failed (%s) – using simulation", e)
        _yolo = None

    try:
        import easyocr
        log.info("Loading EasyOCR (languages=%s device=%s)…", ocr_languages, ocr_device)
        gpu = ocr_device == "cuda"
        _ocr = easyocr.Reader(ocr_languages, gpu=gpu)
        log.info("EasyOCR loaded ✓")
    except Exception as e:
        log.warning("EasyOCR load failed (%s) – text will be simulated", e)
        _ocr = None

    _ready = _yolo is not None
    return _ready


class AIProcessor:
    """Core AI processing pipeline: YOLO → OCR → annotate."""

    def __init__(self, config=None):
        if config is None:
            from pc import config as cfg
            config = cfg

        self.cfg         = config
        self.conf        = config.YOLO_CONF
        self.ocr_conf    = config.OCR_CONF
        self.plate_pad   = config.PLATE_PAD
        self.simulate    = config.SIMULATION_MODE

        if not self.simulate:
            _load_models(config.MODEL_PATH, config.OCR_LANGUAGES, config.OCR_DEVICE)

        log.info("AIProcessor ready (simulation=%s yolo=%s ocr=%s)",
                 self.simulate, _yolo is not None, _ocr is not None)

    # ─── Public API ─────────────────────────────────────────────────────────────

    def process_image(self, image: np.ndarray) -> dict:
        """
        Full pipeline.
        Returns:
          {
            "success": bool,
            "detections": [{"box": [x1,y1,x2,y2], "conf": float, "text": str}],
            "annotated_b64": str,   # base64 JPEG
            "processing_ms": int
          }
        """
        t0 = time.time()

        if self.simulate or (_yolo is None):
            result = self._simulate(image)
        else:
            result = self._real(image)

        result["processing_ms"] = int((time.time() - t0) * 1000)
        return result

    def process_b64(self, b64_str: str) -> dict:
        """Decode base64 image then run process_image."""
        try:
            img_bytes = base64.b64decode(b64_str)
            arr = np.frombuffer(img_bytes, np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError("Could not decode image bytes")
            return self.process_image(frame)
        except Exception as e:
            log.error("process_b64 failed: %s", e)
            return {"success": False, "error": str(e), "detections": []}

    # ─── Real pipeline ────────────────────────────────────────────────────────

    def _real(self, image: np.ndarray) -> dict:
        detections = self.detect_plates(image)
        for d in detections:
            d["text"] = self.extract_text(image, d["box"])
        annotated = self.annotate_image(image.copy(), detections)
        return {
            "success": True,
            "detections": detections,
            "annotated_b64": _to_b64(annotated),
        }

    def detect_plates(self, image: np.ndarray) -> list:
        if _yolo is None:
            return []
        results = _yolo(image, conf=self.conf, verbose=False)
        boxes = []
        for r in results:
            for box in r.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                boxes.append({"box": [x1, y1, x2, y2], "conf": float(box.conf[0]), "text": ""})
        return boxes

    def extract_text(self, image: np.ndarray, box: list) -> str:
        if _ocr is None:
            return ""
        x1, y1, x2, y2 = box
        pad = self.plate_pad
        h, w = image.shape[:2]
        roi = image[max(0, y1-pad):min(h, y2+pad), max(0, x1-pad):min(w, x2+pad)]
        if roi.size == 0:
            return ""
        try:
            results = _ocr.readtext(roi)
            texts = [r[1] for r in results if r[2] >= self.ocr_conf]
            return " ".join(texts).strip().upper()
        except Exception as e:
            log.warning("OCR error: %s", e)
            return ""

    def annotate_image(self, image: np.ndarray, detections: list) -> np.ndarray:
        for d in detections:
            x1, y1, x2, y2 = d["box"]
            conf = d.get("conf", 0)
            text = d.get("text", "")
            cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 0), 2)
            label = f"{text} ({conf:.0%})" if text else f"{conf:.0%}"
            cv2.putText(image, label, (x1, max(y1-8, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        return image

    # ─── Simulation pipeline ─────────────────────────────────────────────────

    def _simulate(self, image: np.ndarray) -> dict:
        h, w = image.shape[:2]
        # Random plausible plate box
        bw = random.randint(w//5, w//3)
        bh = int(bw * 0.25)
        x1 = random.randint(w//4, w//2)
        y1 = random.randint(h//3, h//2)
        x2, y2 = x1 + bw, y1 + bh

        plate = _fake_plate()
        conf  = round(random.uniform(0.82, 0.99), 2)

        detections = [{"box": [x1, y1, x2, y2], "conf": conf, "text": plate}]
        annotated  = self.annotate_image(image.copy(), detections)

        # Overlay "SIMULATION" watermark
        cv2.putText(annotated, "SIMULATION", (10, h - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

        return {
            "success": True,
            "detections": detections,
            "annotated_b64": _to_b64(annotated),
            "simulated": True,
        }


# ── Singleton ─────────────────────────────────────────────────────────────────

_processor: Optional[AIProcessor] = None


def get_processor() -> AIProcessor:
    global _processor
    if _processor is None:
        _processor = AIProcessor()
    return _processor


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_b64(image: np.ndarray) -> str:
    _, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(buf).decode("utf-8")


def _fake_plate() -> str:
    letters = "ABCDEFGHJKLMNPRSTUVWXYZ"
    digits  = string.digits
    # Tunisian / Arabic style: XX-NNNNN or similar
    patterns = [
        lambda: f"{''.join(random.choices(letters,k=3))}-{''.join(random.choices(digits,k=4))}",
        lambda: f"{''.join(random.choices(digits,k=3))}-TN-{''.join(random.choices(digits,k=3))}",
        lambda: f"{''.join(random.choices(letters,k=2))} {''.join(random.choices(digits,k=5))}",
    ]
    return random.choice(patterns)()
