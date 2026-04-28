"""
OCR parser for Marvel Contest of Champions battlegroup reservation screenshots.

Goal:
- Read battlegroup number.
- Read only players marked RESERVED.
- Ignore percent rows, assigned rows, portraits, item boxes, and champion cards.

This intentionally uses Tesseract, not EasyOCR, so it can run on a small Fly.io VM.
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


def _crop_rel(img: np.ndarray, left: float, top: float, right: float, bottom: float) -> np.ndarray:
    h, w = img.shape[:2]
    x1 = max(0, min(w, int(w * left)))
    y1 = max(0, min(h, int(h * top)))
    x2 = max(0, min(w, int(w * right)))
    y2 = max(0, min(h, int(h * bottom)))
    return img[y1:y2, x1:x2]


def _prep_for_text(crop: np.ndarray, scale: int = 3) -> np.ndarray:
    """Make pale UI text easier for Tesseract."""
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)

    # Upscale before thresholding. This matters for Discord/mobile screenshots.
    gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    # Local contrast normalization.
    gray = cv2.equalizeHist(gray)

    # Light denoise without destroying thin glyphs.
    gray = cv2.GaussianBlur(gray, (3, 3), 0)

    # White text on dark background: binary keeps text as white.
    thresh = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        -2,
    )

    return thresh


def _ocr_text(crop: np.ndarray, psm: int = 6) -> str:
    processed = _prep_for_text(crop)
    config = f"--oem 3 --psm {psm}"
    return pytesseract.image_to_string(processed, config=config)


def _ocr_words(crop: np.ndarray, psm: int = 6) -> list[dict[str, Any]]:
    processed = _prep_for_text(crop)
    config = f"--oem 3 --psm {psm}"
    data = pytesseract.image_to_data(processed, output_type=pytesseract.Output.DICT, config=config)

    words: list[dict[str, Any]] = []
    count = len(data.get("text", []))

    for i in range(count):
        text = (data["text"][i] or "").strip()
        if not text:
            continue

        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1.0

        # Keep low-confidence words, because stylized usernames are often low-confidence.
        # Drop only Tesseract's explicit no-confidence garbage where possible.
        if conf < -0.5:
            continue

        x = int(data["left"][i])
        y = int(data["top"][i])
        w = int(data["width"][i])
        h = int(data["height"][i])

        words.append({
            "text": text,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "cx": x + w / 2,
            "cy": y + h / 2,
            "conf": conf,
        })

    return words


def _group_words_into_lines(words: list[dict[str, Any]]) -> list[str]:
    if not words:
        return []

    words = sorted(words, key=lambda item: (item["cy"], item["x"]))
    median_height = np.median([max(1, item["h"]) for item in words])
    y_tolerance = max(14, float(median_height) * 0.75)

    line_groups: list[list[dict[str, Any]]] = []

    for word in words:
        placed = False
        for group in line_groups:
            group_y = np.mean([g["cy"] for g in group])
            if abs(word["cy"] - group_y) <= y_tolerance:
                group.append(word)
                placed = True
                break
        if not placed:
            line_groups.append([word])

    line_groups.sort(key=lambda group: np.mean([g["cy"] for g in group]))

    lines: list[str] = []
    for group in line_groups:
        group.sort(key=lambda item: item["x"])
        text = " ".join(item["text"] for item in group)
        text = _clean_line(text)
        if text:
            lines.append(text)

    return lines


def _clean_line(text: str) -> str:
    text = text.replace("|", "I")
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _reserved_score(text: str) -> float:
    normalized = re.sub(r"[^A-Z]", "", text.upper())
    if not normalized:
        return 0.0

    if RESERVED_TARGET in normalized:
        return 1.0

    # Common OCR distortions: RESERWED, RESERVEO, RESEPVED, etc.
    return SequenceMatcher(None, normalized, RESERVED_TARGET).ratio()


def _is_reserved_line(text: str) -> bool:
    normalized = re.sub(r"[^A-Z]", "", text.upper())
    if "RES" in normalized and ("VED" in normalized or "VE" in normalized):
        return True
    return _reserved_score(text) >= 0.68


def _strip_reserved(text: str) -> str:
    # Remove likely OCR variants of RESERVED while leaving the nearby name intact.
    parts = text.split()
    kept: list[str] = []
    for part in parts:
        if _reserved_score(part) >= 0.68:
            continue
        kept.append(part)
    return _clean_player_name(" ".join(kept))


def _clean_player_name(text: str) -> str:
    text = _clean_line(text)

    # Remove UI labels that can leak in from icon badges or buttons.
    banned_exact = {
        "HEALTH", "INFO", "ITEMS", "ATTACK", "TACTICS", "ATTACK TACTICS",
        "LEGEND", "VETERAN", "K.O.", "KO", "ASSIGNED", "RESERVED",
    }

    if text.upper() in banned_exact:
        return ""

    # Strip OCR fragments from badges if they get glued to the name.
    text = re.sub(r"\b(LEGEND|VETERAN|ASSIGNED|RESERVED|HEALTH|INFO|ITEMS)\b", "", text, flags=re.IGNORECASE)

    # Remove percentage rows entirely.
    if re.search(r"\d+(?:\.\d+)?\s*%", text):
        return ""

    # Trim obvious edge junk, but keep symbols that can exist in player names.
    text = text.strip(" \t\r\n:;,.`'\"[]{}()")
    text = re.sub(r"\s+", " ", text).strip()

    # Guard against false positives from tiny OCR crumbs.
    if len(text) < 2:
        return ""

    return text


def _extract_bg_from_text(text: str) -> int | None:
    compact = text.upper().replace("O", "0")

    patterns = [
        r"BATTLEGROUP\s*(\d+)",
        r"BATTLE\s*GROUP\s*(\d+)",
        r"BATTLEGRO[U0]P\s*(\d+)",
        r"GROUP\s*(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                pass
    return None


def _extract_battlegroup(img: np.ndarray) -> tuple[int | None, str]:
    # Header is stable and centered. Include a bit extra right-side space for the green speaker icon.
    header_crop = _crop_rel(img, 0.34, 0.045, 0.68, 0.18)
    header_text = _ocr_text(header_crop, psm=6)
    battlegroup = _extract_bg_from_text(header_text)

    if battlegroup is None:
        # Fallback: slightly larger header crop.
        header_crop_2 = _crop_rel(img, 0.25, 0.035, 0.75, 0.20)
        header_text_2 = _ocr_text(header_crop_2, psm=6)
        battlegroup = _extract_bg_from_text(header_text_2)
        header_text = header_text + "\n" + header_text_2

    return battlegroup, _clean_line(header_text.replace("\n", " « "))


def _extract_reserved_names(img: np.ndarray) -> tuple[list[str], list[str]]:
    # This is the operating range from the screenshots:
    # - starts right of the portraits
    # - ends before the item/champion boxes
    # - starts below the tab buttons
    # - ends around the final row
    text_crop = _crop_rel(img, 0.295, 0.205, 0.565, 0.825)

    words = _ocr_words(text_crop, psm=6)
    lines = _group_words_into_lines(words)

    # Fallback to raw line OCR if image_to_data groups poorly.
    if not any(_is_reserved_line(line) for line in lines):
        raw = _ocr_text(text_crop, psm=6)
        fallback_lines = [_clean_line(line) for line in raw.splitlines() if _clean_line(line)]
        if fallback_lines:
            lines = fallback_lines

    reserved: list[str] = []

    for index, line in enumerate(lines):
        if not _is_reserved_line(line):
            continue

        # Case: Tesseract returns "QueLoQueMiLoco RESERVED" on one line.
        same_line_name = _strip_reserved(line)
        if same_line_name and not _is_reserved_line(same_line_name):
            candidate = same_line_name
        else:
            # Normal case: previous OCR line is the player name.
            candidate = ""
            for prev_index in range(index - 1, -1, -1):
                prev = lines[prev_index]
                if _is_reserved_line(prev):
                    continue
                cleaned = _clean_player_name(prev)
                if cleaned:
                    candidate = cleaned
                    break

        candidate = _clean_player_name(candidate)
        if candidate and candidate not in reserved:
            reserved.append(candidate)

    return reserved, lines


def parse_battlegroup_screenshot(image_bytes: bytes) -> dict[str, Any]:
    pil = _pil_from_bytes(image_bytes)
    img = np.array(pil)

    battlegroup, header_ocr = _extract_battlegroup(img)
    reserved, row_ocr_lines = _extract_reserved_names(img)

    return {
        "battlegroup": battlegroup,
        "reserved": reserved,
        "header_ocr": header_ocr,
        "row_ocr_lines": row_ocr_lines,
    }
