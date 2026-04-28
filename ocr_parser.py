"""
OCR parser for Marvel Contest of Champions battlegroup reservation screenshots.

This parser is intentionally built for Fly's small machine:
- Tesseract + OpenCV, not EasyOCR/PyTorch.
- Panel-relative crops, not whole-image OCR.
- Extracts only:
    1. battlegroup number
    2. player names marked RESERVED
"""

from __future__ import annotations

import io
import re
from difflib import SequenceMatcher
from typing import Any

import cv2
import numpy as np
import pytesseract
from PIL import Image, ImageOps


RESERVED_TARGET = "RESERVED"


def _pil_from_bytes(image_bytes: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(image_bytes))
    image = ImageOps.exif_transpose(image)
    return image.convert("RGB")


def _clean_line(text: str) -> str:
    text = text.replace("|", "I")
    text = text.replace("—", "-").replace("–", "-")
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _find_panel_bounds(img: np.ndarray) -> tuple[int, int, int, int]:
    """
    Find the central grey MCOC panel.

    This makes the crop resilient to screenshots with different phone aspect ratios.
    Returns (x, y, w, h). Falls back to the whole image if detection fails.
    """
    h, w = img.shape[:2]

    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    # The background is very dark. The game panel is consistently brighter.
    # Keep this threshold deliberately low so the full panel body is captured.
    _, mask = cv2.threshold(gray, 32, 255, cv2.THRESH_BINARY)

    kernel = np.ones((19, 19), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates: list[tuple[int, int, int, int, int]] = []
    img_area = w * h

    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        area = cw * ch
        if area < img_area * 0.15:
            continue
        if cw < w * 0.40 or ch < h * 0.40:
            continue

        aspect = cw / max(1, ch)
        if not (1.05 <= aspect <= 2.20):
            continue

        candidates.append((area, x, y, cw, ch))

    if not candidates:
        return (0, 0, w, h)

    # Largest reasonable grey region should be the UI panel.
    _, x, y, cw, ch = max(candidates, key=lambda item: item[0])

    # Lightly expand, but stay inside image.
    pad_x = int(cw * 0.015)
    pad_y = int(ch * 0.015)
    x1 = max(0, x - pad_x)
    y1 = max(0, y - pad_y)
    x2 = min(w, x + cw + pad_x)
    y2 = min(h, y + ch + pad_y)

    return (x1, y1, x2 - x1, y2 - y1)


def _crop_rel_box(img: np.ndarray, box: tuple[int, int, int, int], left: float, top: float, right: float, bottom: float) -> np.ndarray:
    bx, by, bw, bh = box
    x1 = bx + int(bw * left)
    y1 = by + int(bh * top)
    x2 = bx + int(bw * right)
    y2 = by + int(bh * bottom)

    h, w = img.shape[:2]
    x1 = max(0, min(w, x1))
    x2 = max(0, min(w, x2))
    y1 = max(0, min(h, y1))
    y2 = max(0, min(h, y2))

    if x2 <= x1 or y2 <= y1:
        return img[0:1, 0:1]

    return img[y1:y2, x1:x2]


def _prep_for_text(crop: np.ndarray, scale: int = 4) -> np.ndarray:
    """
    Prepare pale grey/white UI text on dark background.

    v3 used adaptive thresholding over a large crop; that created noise and made
    Tesseract merge junk into RESERVED rows. v4 uses simple high-contrast text
    isolation after panel-relative cropping.
    """
    if crop.size == 0:
        return crop

    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    # Mild contrast normalization without making the dark row background noisy.
    gray = cv2.equalizeHist(gray)

    # Otsu usually separates pale text from the dark row background cleanly.
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Remove tiny specks but do not over-dilate letters.
    kernel = np.ones((2, 2), np.uint8)
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)

    return thresh


def _ocr_text(crop: np.ndarray, psm: int = 6, whitelist: str | None = None) -> str:
    processed = _prep_for_text(crop)
    config = f"--oem 3 --psm {psm} -c preserve_interword_spaces=1"
    if whitelist:
        config += f" -c tessedit_char_whitelist={whitelist}"
    try:
        return pytesseract.image_to_string(processed, config=config)
    except Exception:
        return ""


def _reserved_score(text: str) -> float:
    normalized = re.sub(r"[^A-Z]", "", text.upper())
    if not normalized:
        return 0.0
    if RESERVED_TARGET in normalized:
        return 1.0
    return SequenceMatcher(None, normalized, RESERVED_TARGET).ratio()


def _is_reserved_line(text: str) -> bool:
    normalized = re.sub(r"[^A-Z]", "", text.upper())
    if not normalized:
        return False

    if "RESERVED" in normalized:
        return True

    # Common Tesseract distortions from these screenshots:
    # RESERVED -> RVEL / RVELD / RESERWED / RESERVEO / RESEAVED / EOE RVEL
    strong_fragments = [
        "RESER", "ESERV", "SERVE", "ERVED", "RVED", "RVEL", "RVEI",
        "RESV", "RESEV", "ESEV", "EOERVEL", "EORVEL"
    ]
    if any(fragment in normalized for fragment in strong_fragments):
        return True

    # Use fuzzy match only for short-ish status-like strings.
    if 4 <= len(normalized) <= 14 and {"R", "E", "V"}.issubset(set(normalized)):
        return _reserved_score(normalized) >= 0.52

    return False


def _clean_player_name(text: str) -> str:
    text = _clean_line(text)

    # Remove OCR variants of RESERVED if they got glued to the name line.
    parts = []
    for part in text.split():
        if _is_reserved_line(part):
            continue
        parts.append(part)
    text = " ".join(parts)

    banned_words = [
        "HEALTH", "INFO", "ITEMS", "ATTACK", "TACTICS", "LEGEND", "VETERAN",
        "ASSIGNED", "RESERVED", "KO", "K.O"
    ]
    for word in banned_words:
        text = re.sub(rf"\b{re.escape(word)}\b", "", text, flags=re.IGNORECASE)

    # Percent rows are not reservations.
    if re.search(r"\d+(?:\.\d+)?\s*%", text):
        return ""

    # Keep username punctuation, but trim obvious OCR edge junk.
    text = text.strip(" \t\r\n:;,.`'\"[]{}()<>«»")
    text = re.sub(r"\s+", " ", text).strip()

    # Filter tiny garbage like "II" from false RESERVED same-line matches.
    if len(text) < 3:
        return ""

    return text


def _extract_bg_from_text(text: str) -> int | None:
    """
    v3 bug: it replaced O -> 0 globally, turning BATTLEGROUP into BATTLEGR0UP,
    then the normal BATTLEGROUP regex stopped matching.

    v4 normalizes 0 -> O for the word part instead.
    """
    raw = text.upper()
    letters = raw.replace("0", "O")

    patterns = [
        r"BATTLEGROUP\s*([1-9])",
        r"BATTLE\s*GROUP\s*([1-9])",
        r"BAT[A-Z]{2,12}GROUP\s*([1-9])",
        r"BATI[A-Z]{2,12}GROUP\s*([1-9])",  # BATILEGROUP-type OCR
        r"GROUP\s*([1-9])",
    ]

    for pattern in patterns:
        match = re.search(pattern, letters, flags=re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue

    return None


def _extract_battlegroup(img: np.ndarray, panel: tuple[int, int, int, int]) -> tuple[int | None, str]:
    # Crop around the big BATTLEGROUP title, not the alliance line.
    crops = [
        _crop_rel_box(img, panel, 0.28, 0.035, 0.76, 0.145),
        _crop_rel_box(img, panel, 0.20, 0.025, 0.82, 0.170),
    ]

    texts: list[str] = []
    for crop in crops:
        txt = _ocr_text(crop, psm=6)
        texts.append(_clean_line(txt))
        bg = _extract_bg_from_text(txt)
        if bg is not None:
            return bg, _clean_line(" « ".join(texts))

    return None, _clean_line(" « ".join(texts))


def _row_specs() -> list[tuple[float, float]]:
    """
    Row top/bottom values relative to the detected panel.

    Works across the screenshot forms provided:
    - four-tab version
    - three-tab version
    - percent/reserved/assigned rows
    """
    return [
        (0.235, 0.385),
        (0.395, 0.545),
        (0.555, 0.705),
        (0.715, 0.865),
    ]


def _extract_name_from_row_text(row_text: str) -> str:
    lines = [_clean_line(line) for line in row_text.splitlines()]
    lines = [line for line in lines if line]

    if not lines:
        return ""

    # Prefer the first non-status line. In this UI, username is above status.
    for line in lines:
        if _is_reserved_line(line):
            continue
        cleaned = _clean_player_name(line)
        if cleaned:
            return cleaned

    # Fallback: if Tesseract put "Name RESERVED" on one line, remove RESERVED-like parts.
    for line in lines:
        cleaned = _clean_player_name(line)
        if cleaned:
            return cleaned

    return ""


def _extract_reserved_names(img: np.ndarray, panel: tuple[int, int, int, int]) -> tuple[list[str], list[str]]:
    reserved: list[str] = []
    debug_lines: list[str] = []

    for row_index, (top, bottom) in enumerate(_row_specs(), start=1):
        # Full text area for this row. Starts right of portrait/badge and ends before item boxes.
        row_crop = _crop_rel_box(img, panel, 0.155, top, 0.505, bottom)

        # Separate status crop. This prevents "RESERVED" from being paired with a prior row.
        status_crop = _crop_rel_box(img, panel, 0.155, top + 0.070, 0.350, min(bottom, top + 0.135))

        # Separate name crop. PSM 7 is better for a single username line.
        name_crop = _crop_rel_box(img, panel, 0.155, top + 0.025, 0.505, top + 0.082)

        row_text = _ocr_text(row_crop, psm=6)
        status_text = _ocr_text(
            status_crop,
            psm=7,
            whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        )
        name_text = _ocr_text(name_crop, psm=7)

        row_line = _clean_line(row_text)
        status_line = _clean_line(status_text)
        name_line = _clean_line(name_text)

        debug_lines.append(f"row {row_index}: name=[{name_line or 'empty'}] status=[{status_line or 'empty'}] full=[{row_line or 'empty'}]")

        is_reserved = _is_reserved_line(status_line) or _is_reserved_line(row_line)
        if not is_reserved:
            continue

        candidate = _clean_player_name(name_line)
        if not candidate:
            candidate = _extract_name_from_row_text(row_text)

        if candidate and candidate not in reserved:
            reserved.append(candidate)

    # Fallback for screenshots whose panel detection/crop lands slightly off:
    # use a broad text-column crop, but keep row-local pairing from above as primary.
    if not reserved:
        broad = _crop_rel_box(img, panel, 0.150, 0.225, 0.520, 0.875)
        broad_text = _ocr_text(broad, psm=6)
        lines = [_clean_line(line) for line in broad_text.splitlines() if _clean_line(line)]
        debug_lines.extend(f"broad: {line}" for line in lines[:16])

        for i, line in enumerate(lines):
            if not _is_reserved_line(line):
                continue

            candidate = ""
            # Prefer same-line "Name RESERVED" only if it leaves a real name.
            cleaned_same = _clean_player_name(line)
            if cleaned_same:
                candidate = cleaned_same

            if not candidate:
                for j in range(i - 1, -1, -1):
                    if _is_reserved_line(lines[j]):
                        continue
                    cleaned_prev = _clean_player_name(lines[j])
                    if cleaned_prev:
                        candidate = cleaned_prev
                        break

            if candidate and candidate not in reserved:
                reserved.append(candidate)

    return reserved, debug_lines


def parse_battlegroup_screenshot(image_bytes: bytes) -> dict[str, Any]:
    pil = _pil_from_bytes(image_bytes)
    img = np.array(pil)

    panel = _find_panel_bounds(img)

    battlegroup, header_ocr = _extract_battlegroup(img, panel)
    reserved, row_ocr_lines = _extract_reserved_names(img, panel)

    px, py, pw, ph = panel
    panel_debug = f"panel x={px} y={py} w={pw} h={ph}"

    return {
        "battlegroup": battlegroup,
        "reserved": reserved,
        "header_ocr": header_ocr,
        "row_ocr_lines": [panel_debug] + row_ocr_lines,
    }
