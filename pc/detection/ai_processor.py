"""
Smart Parking System - AI Processor
Handles YOLO detection + EasyOCR license-plate extraction.
Fixed: Arabic OCR annotation (PIL rendering), fuzzy plate matching for Arabic.
Falls back to simulation mode when hardware/model is absent.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import random
import re
import string
import time
import unicodedata
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# ── Arabic normalization map ──────────────────────────────────────────────────
# Maps visually/phonetically similar Arabic letters for fuzzy matching
_AR_NORM = {
    'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ٱ': 'ا',
    'ة': 'ه',
    'ى': 'ي',
    'ؤ': 'و',
    'ئ': 'ي',
    'ٮ': 'ب',
    'ڡ': 'ف',
    'ک': 'ك',
    'گ': 'ك',
    'ڪ': 'ك',
    'ں': 'ن',
    'ڒ': 'ر',
    'ڙ': 'ز',
}

# Tunisian plate patterns
# Matches: 1-4 digits, Arabic/Latin letters (1-6 chars), 1-7 digits
_TN_PLATE_RE = re.compile(
    r'(\d{1,4})\s*([A-Za-z\u0600-\u06FF\u0750-\u077F]{1,6})\s*(\d{1,7})'
)
# Alternative: just Arabic letters + digits in any order (for lenient matching)
_TN_PLATE_LENIENT_RE = re.compile(
    r'([A-Za-z\u0600-\u06FF\u0750-\u077F]+)\s*(\d+)'
)


def normalize_arabic(text: str) -> str:
    """Normalize Arabic text for fuzzy comparison."""
    result = []
    for ch in text:
        result.append(_AR_NORM.get(ch, ch))
    return ''.join(result)


def arabic_similarity(a: str, b: str) -> float:
    """
    Return a similarity score 0-1 between two plate strings.
    Handles Arabic character confusion errors.
    """
    a_norm = normalize_arabic(a.upper().strip())
    b_norm = normalize_arabic(b.upper().strip())

    if a_norm == b_norm:
        return 1.0

    # Check if numeric parts match and text part is similar (Levenshtein-lite)
    ma = _TN_PLATE_RE.search(a_norm)
    mb = _TN_PLATE_RE.search(b_norm)

    if ma and mb:
        # Numbers must match exactly, letters fuzzy
        if ma.group(1) == mb.group(1) and ma.group(3) == mb.group(3):
            letters_a = normalize_arabic(ma.group(2))
            letters_b = normalize_arabic(mb.group(2))
            # Simple char-by-char overlap
            matches = sum(ca == cb for ca, cb in zip(letters_a, letters_b))
            max_len = max(len(letters_a), len(letters_b), 1)
            return 0.6 + 0.4 * (matches / max_len)

    # Generic edit-distance similarity
    return _edit_similarity(a_norm, b_norm)


def _edit_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    la, lb = len(a), len(b)
    dp = list(range(lb + 1))
    for i in range(1, la + 1):
        prev = dp[:]
        dp[0] = i
        for j in range(1, lb + 1):
            cost = 0 if a[i-1] == b[j-1] else 1
            dp[j] = min(dp[j] + 1, dp[j-1] + 1, prev[j-1] + cost)
    dist = dp[lb]
    return 1.0 - dist / max(la, lb)


# ── PIL-based annotation (fixes Arabic ????? rendering) ──────────────────────

# Try to find a font that supports Arabic (prioritize Arabic-specific fonts)
_ARABIC_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansArabic-Regular.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/unifont/unifont.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",  # Windows
    "C:\\Windows\\Fonts\\segoeui.ttf",  # Windows
]

_pil_font_cache: dict = {}


def _get_pil_font(size: int = 20) -> ImageFont.FreeTypeFont:
    """Get a font that supports Arabic characters."""
    if size in _pil_font_cache:
        return _pil_font_cache[size]
    
    for path in _ARABIC_FONTS:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                _pil_font_cache[size] = font
                log.debug(f"Loaded font: {path} (size {size})")
                return font
            except Exception as e:
                log.debug(f"Failed to load {path}: {e}")
                continue
    
    log.debug("Using default PIL font")
    font = ImageFont.load_default()
    _pil_font_cache[size] = font
    return font


def _reshape_arabic(text: str) -> str:
    """Apply Arabic reshaping + bidi for correct display."""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        # Reshape the text for proper Arabic rendering
        reshaped = arabic_reshaper.reshape(text)
        # Apply bidirectional algorithm for correct text direction
        display_text = get_display(reshaped)
        return display_text
    except ImportError:
        log.debug("arabic_reshaper or bidi not available, returning text as-is")
        return text
    except Exception as e:
        log.debug(f"Arabic reshaping error: {e}")
        return text


def annotate_image_pil(image: np.ndarray, detections: list) -> np.ndarray:
    """
    Annotate image using PIL so Arabic text renders correctly
    instead of showing ????? boxes.
    Handles both Arabic and Latin characters properly.
    """
    pil_img = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    draw    = ImageDraw.Draw(pil_img)
    font    = _get_pil_font(22)
    small   = _get_pil_font(16)

    for d in detections:
        x1, y1, x2, y2 = d["box"]
        conf  = d.get("conf", 0)
        text  = d.get("text", "")

        # Box (green rectangle)
        draw.rectangle([x1, y1, x2, y2], outline=(0, 220, 100), width=2)

        # Prepare label text with proper Arabic handling
        if text:
            # Check if text contains Arabic
            has_arabic = any('\u0600' <= ch <= '\u06FF' or '\u0750' <= ch <= '\u077F' for ch in text)
            if has_arabic:
                # Apply Arabic reshaping for proper display
                label = _reshape_arabic(text)
            else:
                label = text
            conf_str = f" ({conf:.0%})"
        else:
            label = f"{conf:.0%}"
            conf_str = ""

        # Get text bounding box to size the label background
        try:
            bbox = font.getbbox(label + conf_str)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except (AttributeError, TypeError):
            try:
                tw, th = font.getsize(label + conf_str)
            except:
                tw, th = 150, 25

        # Draw label background (green bar)
        label_y = max(y1 - th - 8, 0)
        draw.rectangle(
            [x1, label_y, x1 + tw + 8, label_y + th + 4],
            fill=(0, 180, 80)
        )
        
        # Draw text on label
        full_label = label + conf_str
        draw.text((x1 + 4, label_y + 2), full_label, font=font, fill=(0, 0, 0))

    result = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    return result


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
    """Core AI processing pipeline: YOLO → OCR → PIL annotate (Arabic-safe)."""

    SIMILARITY_THRESHOLD = 0.72   # plate match threshold for fuzzy Arabic

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
        t0 = time.time()

        if self.simulate or (_yolo is None):
            result = self._simulate(image)
        else:
            result = self._real(image)

        result["processing_ms"] = int((time.time() - t0) * 1000)
        return result

    def process_b64(self, b64_str: str) -> dict:
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

    def fuzzy_match_plate(self, ocr_text: str, registered_plates: list) -> tuple[str | None, float]:
        """
        Find the best matching registered plate for an OCR result.
        Returns (matched_plate, score) or (None, 0).
        Handles Arabic OCR confusion errors (e.g. تويبت ≈ تونس).
        """
        if not ocr_text or not registered_plates:
            return None, 0.0

        best_plate = None
        best_score = 0.0

        for plate in registered_plates:
            score = arabic_similarity(ocr_text, plate)
            if score > best_score:
                best_score = score
                best_plate = plate

        if best_score >= self.SIMILARITY_THRESHOLD:
            return best_plate, best_score
        return None, best_score

    # ─── Real pipeline ────────────────────────────────────────────────────────

    def _real(self, image: np.ndarray) -> dict:
        detections = self.detect_plates(image)
        for d in detections:
            d["text"] = self.extract_text(image, d["box"])
        annotated = annotate_image_pil(image.copy(), detections)
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

        # Pre-process ROI for better Arabic OCR
        roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        roi_up   = cv2.resize(roi_gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        _, roi_thresh = cv2.threshold(roi_up, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        roi_final = cv2.cvtColor(roi_thresh, cv2.COLOR_GRAY2BGR)

        try:
            # Try enhanced ROI first, fall back to original
            results = _ocr.readtext(roi_final)
            if not results:
                results = _ocr.readtext(roi)
            
            # FIRST PASS: Collect everything regardless of confidence
            # (we'll filter later in _fix_tn_plate_order)
            all_raw_texts = []
            for r in results:
                text = r[1].strip()
                conf = r[2]
                if text:  # Keep any non-empty text
                    all_raw_texts.append(text)
                    log.debug(f"OCR chunk: '{text}' (confidence: {conf:.2f})")
            
            if all_raw_texts:
                raw = " ".join(all_raw_texts).strip()
            else:
                raw = ""
            
            log.debug(f"OCR raw concatenated: '{raw}'")
            
            # Fix Tunisian plate text direction (numbers + Arabic + numbers)
            result = _fix_tn_plate_order(raw)
            log.debug(f"Final extracted plate: '{result}'")
            return result
        except Exception as e:
            log.warning("OCR error: %s", e)
            return ""

    # ─── Simulation pipeline ─────────────────────────────────────────────────

    def _simulate(self, image: np.ndarray) -> dict:
        h, w = image.shape[:2]
        bw = random.randint(w//5, w//3)
        bh = int(bw * 0.25)
        x1 = random.randint(w//4, w//2)
        y1 = random.randint(h//3, h//2)
        x2, y2 = x1 + bw, y1 + bh

        plate = _fake_plate()
        conf  = round(random.uniform(0.82, 0.99), 2)

        detections = [{"box": [x1, y1, x2, y2], "conf": conf, "text": plate}]

        # Use PIL annotation so Arabic shows correctly
        annotated = annotate_image_pil(image.copy(), detections)

        # Overlay "SIMULATION" watermark using PIL too
        pil_img = Image.fromarray(cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB))
        draw    = ImageDraw.Draw(pil_img)
        font    = _get_pil_font(24)
        draw.text((10, h - 34), "SIMULATION", font=font, fill=(255, 50, 50))
        annotated = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

        return {
            "success": True,
            "detections": detections,
            "annotated_b64": _to_b64(annotated),
            "simulated": True,
        }


def _fix_tn_plate_order(text: str) -> str:
    """
    Fix Tunisian plate component ordering and extract full text.
    Tunisian plates: <number> <Arabic city> <number>
    Handles mixed Arabic+Latin text, even if not in strict order.
    
    Examples:
        "نت 223349" → "نت 223349"
        "223349 نت" → "223349 نت"
        "223349 نت 5678" → "223349 نت 5678"
    """
    if not text:
        return text

    text = text.strip()
    
    # Try strict pattern first (best case): digits + Arabic + digits
    m = _TN_PLATE_RE.search(text)
    if m:
        n1, letters, n2 = m.group(1), m.group(2), m.group(3)
        log.debug(f"Strict pattern matched: {n1} {letters} {n2}")
        return f"{n1} {letters} {n2}"
    
    # Second attempt: Look for ALL digits + Arabic anywhere in text
    # Extract all digit sequences and Arabic sequences
    all_digits = re.findall(r'\d+', text)
    all_arabic = re.findall(r'[\u0600-\u06FF\u0750-\u077F]+', text)
    
    if all_digits and all_arabic:
        # Reconstruct in canonical order
        arabic_part = ' '.join(all_arabic)
        digits_parts = all_digits
        if len(digits_parts) >= 2:
            # Has leading and trailing digits
            result = f"{digits_parts[0]} {arabic_part} {digits_parts[-1]}"
            log.debug(f"Reconstructed from parts: {result}")
            return result
        elif len(digits_parts) == 1:
            # Only one digit sequence - try to guess position
            # Default: put digits after Arabic (common in RTL rendering)
            result = f"{all_digits[0]} {arabic_part}"
            log.debug(f"Single digit sequence: {result}")
            return result.strip()
    
    # Fallback: lenient pattern for Arabic+digits in any order
    # This catches cases like "نت 223349" or "223349 نت"
    m_lenient = _TN_PLATE_LENIENT_RE.search(text)
    if m_lenient:
        letters = m_lenient.group(1)
        digits = m_lenient.group(2)
        log.debug(f"Lenient pattern matched: {letters} {digits}")
        
        # Check if there are leading numbers before the Arabic
        leading_digits = re.match(r'^(\d+)\s+', text)
        if leading_digits:
            # Pattern: <digits> <Arabic> <digits>
            leading = leading_digits.group(1)
            # Extract remaining digits after Arabic
            remaining_match = re.search(r'[\u0600-\u06FF\u0750-\u077F]+\s*(\d+)', text)
            if remaining_match:
                trailing = remaining_match.group(1)
                result = f"{leading} {letters} {trailing}"
                log.debug(f"Complex pattern: {result}")
                return result
        # Pattern: <Arabic> <digits>
        result = f"{letters} {digits}".strip()
        log.debug(f"Simple pattern: {result}")
        return result
    
    # Last resort: just return cleaned text
    log.debug(f"No pattern matched, returning as-is: {text}")
    return text


# ── Singleton ─────────────────────────────────────────────────────────────────

_processor: Optional[AIProcessor] = None


def get_processor() -> AIProcessor:
    global _processor
    if _processor is None:
        _processor = AIProcessor()
    return _processor


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_b64(image: np.ndarray) -> str:
    _, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return base64.b64encode(buf).decode("utf-8")


def _fake_plate() -> str:
    """Generate realistic Tunisian plate."""
    letters  = "ABCDEFGHJKLMNPRSTUVWXYZ"
    digits   = string.digits
    ar_cities = ["تونس", "صفاقس", "سوسة", "بنزرت", "قابس", "نابل", "القيروان"]
    patterns = [
        lambda: f"{''.join(random.choices(digits,k=3))} {random.choice(ar_cities)} {''.join(random.choices(digits,k=4))}",
        lambda: f"{''.join(random.choices(digits,k=2))} {random.choice(ar_cities)} {''.join(random.choices(digits,k=3))}",
        lambda: f"{''.join(random.choices(letters,k=3))}-{''.join(random.choices(digits,k=4))}",
    ]
    return random.choice(patterns)()
