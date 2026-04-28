import io
import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional

import cv2
import numpy as np
import pytesseract
from PIL import Image


def _pil_to_bgr(image_bytes: bytes) -> np.ndarray:
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    rgb = np.array(image)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _crop_relative(img: np.ndarray, x1: float, y1: float, x2: float, y2: float) -> np.ndarray:
    h, w = img.shape[:2]
    return img[int(h * y1): int(h * y2), int(w * x1): int(w * x2)]


def _preprocess_for_text(img: np.ndarray, scale: float = 3.0) -> np.ndarray:
    """Lightweight preprocessing suitable for Fly's small VM."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    if scale != 1.0:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    # Improve local contrast without making the background explode.
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    # White UI text on dark background. Otsu usually handles the grey panel variations better
    # than a hard-coded threshold across different screenshot resolutions.
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    return thresh


def _ocr_text(img: np.ndarray, config: str) -> str:
    text = pytesseract.image_to_string(img, config=config)
    return re.sub(r"\s+", " ", text).strip()


def _clean_header_text(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9 ]+", " ", text).strip()


def _extract_battlegroup(img: np.ndarray) -> Dict[str, Optional[object]]:
    # Top-center crop. Wide enough for both the alliance title and BATTLEGROUP line.
    header = _crop_relative(img, 0.32, 0.04, 0.72, 0.18)
    processed = _preprocess_for_text(header, scale=3.0)

    raw = _ocr_text(processed, config="--oem 3 --psm 6")
    cleaned = _clean_header_text(raw).upper()

    # Normal path: BATTLEGROUP 1 / BATTLEGROUP 2 / BATTLEGROUP 3
    match = re.search(r"BATTLE\s*GROUP\s*(\d+)", cleaned)
    if not match:
        match = re.search(r"BATTLEGROUP\s*(\d+)", cleaned)
    if not match:
        # Fallback: if OCR catches only the trailing digit near header.
        digits = re.findall(r"\b([1-9])\b", cleaned)
        if digits:
            return {"battlegroup": int(digits[-1]), "raw_header": raw}
        return {"battlegroup": None, "raw_header": raw}

    return {"battlegroup": int(match.group(1)), "raw_header": raw}


def _word_rows_from_data(data: dict, min_conf: int = 25) -> List[dict]:
    words = []
    n = len(data.get("text", []))

    for i in range(n):
        text = str(data["text"][i]).strip()
        if not text:
            continue

        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1

        if conf < min_conf:
            continue

        x = int(data["left"][i])
        y = int(data["top"][i])
        w = int(data["width"][i])
        h = int(data["height"][i])
        words.append({"text": text, "x": x, "y": y, "w": w, "h": h, "cy": y + h / 2})

    words.sort(key=lambda item: (item["cy"], item["x"]))

    rows: List[List[dict]] = []
    for word in words:
        placed = False
        for row in rows:
            row_cy = sum(w["cy"] for w in row) / len(row)
            # Scaled image: text-line centers usually stay within ~18-25 px.
            if abs(word["cy"] - row_cy) <= 24:
                row.append(word)
                placed = True
                break
        if not placed:
            rows.append([word])

    line_objs = []
    for row in rows:
        row.sort(key=lambda item: item["x"])
        text = " ".join(item["text"] for item in row)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        line_objs.append({
            "text": text,
            "x": min(item["x"] for item in row),
            "y": min(item["y"] for item in row),
            "cy": sum(item["cy"] for item in row) / len(row),
        })

    line_objs.sort(key=lambda item: item["cy"])
    return line_objs


def _normalize_status(text: str) -> str:
    return re.sub(r"[^A-Z]", "", text.upper())


def _looks_reserved(text: str) -> bool:
    norm = _normalize_status(text)
    if "RESERVED" in norm:
        return True
    # Tesseract sometimes returns clipped variants like RESERVEO, RESERVEDI, etc.
    return SequenceMatcher(None, norm, "RESERVED").ratio() >= 0.72


def _looks_non_name(text: str) -> bool:
    upper = text.upper()
    if _looks_reserved(text):
        return True
    if "ASSIGNED" in upper:
        return True
    if re.search(r"\d+(?:\.\d+)?\s*%", text):
        return True
    return False


def _clean_player_name(text: str) -> str:
    # Keep unusual player-name characters, but remove obvious OCR/control junk.
    text = text.replace("|", "I")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" .,:;'-_")
    return text


def _extract_reserved_names(img: np.ndarray) -> Dict[str, object]:
    # Text column only: exclude icons on the left and champion cards on the right.
    # This relative crop covers the name/status column in all supplied screenshot variants.
    text_col = _crop_relative(img, 0.27, 0.18, 0.58, 0.86)
    processed = _preprocess_for_text(text_col, scale=3.0)

    data = pytesseract.image_to_data(
        processed,
        output_type=pytesseract.Output.DICT,
        config="--oem 3 --psm 6",
    )

    lines = _word_rows_from_data(data)
    reserved = []

    for idx, line in enumerate(lines):
        if not _looks_reserved(line["text"]):
            continue

        # Name line is the nearest meaningful line above RESERVED.
        name = None
        for prev in reversed(lines[:idx]):
            vertical_gap = line["cy"] - prev["cy"]
            if vertical_gap > 95:  # scaled crop; prevents jumping into previous player row
                break
            if not _looks_non_name(prev["text"]):
                name = _clean_player_name(prev["text"])
                break

        if name and name not in reserved:
            reserved.append(name)

    return {"reserved": reserved, "raw_lines": [line["text"] for line in lines]}


def parse_battlegroup_image(image_bytes: bytes) -> dict:
    img = _pil_to_bgr(image_bytes)

    bg_result = _extract_battlegroup(img)
    reserved_result = _extract_reserved_names(img)

    return {
        "battlegroup": bg_result["battlegroup"],
        "reserved": reserved_result["reserved"],
        "raw_header": bg_result["raw_header"],
        "raw_lines": reserved_result["raw_lines"],
    }
