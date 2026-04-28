from __future__ import annotations

import io
import os
import re
import subprocess
import tempfile
from difflib import SequenceMatcher
from typing import Any, Optional

from PIL import Image, ImageEnhance, ImageOps

RESERVED_TARGET = "RESERVED"


def _pil_from_bytes(image_bytes: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(image_bytes))
    image = ImageOps.exif_transpose(image)
    return image.convert("RGB")


def _clean_line(text: str) -> str:
    text = text.replace("—", "-").replace("–", "-")
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _find_panel_bounds(image: Image.Image) -> tuple[int, int, int, int]:
    width, height = image.size
    small_w = 512
    small_h = max(1, int(height * small_w / max(1, width)))
    small = image.resize((small_w, small_h), Image.Resampling.BILINEAR).convert("RGB")
    pixels = small.load()

    xs = []
    ys = []
    y_start = int(small_h * 0.02)
    y_end = int(small_h * 0.96)
    x_start = int(small_w * 0.10)
    x_end = int(small_w * 0.92)

    for y in range(y_start, y_end):
        for x in range(x_start, x_end):
            red, green, blue = pixels[x, y]
            mx = max(red, green, blue)
            mn = min(red, green, blue)
            avg = (red + green + blue) // 3
            if mx - mn < 28 and 30 < avg < 120:
                xs.append(x)
                ys.append(y)

    if not xs:
        return (0, 0, width, height)

    left = int(min(xs) * width / small_w)
    top = int(min(ys) * height / small_h)
    right = int(max(xs) * width / small_w)
    bottom = int(max(ys) * height / small_h)

    panel_w = right - left
    panel_h = bottom - top
    if panel_w < width * 0.35 or panel_h < height * 0.35:
        return (0, 0, width, height)

    pad_x = int(panel_w * 0.01)
    pad_y = int(panel_h * 0.01)
    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(width, right + pad_x)
    bottom = min(height, bottom + pad_y)

    return (left, top, right - left, bottom - top)


def _crop_rel(image: Image.Image, panel: tuple[int, int, int, int], left: float, top: float, right: float, bottom: float) -> Image.Image:
    px, py, pw, ph = panel
    width, height = image.size
    x1 = max(0, min(width, px + int(pw * left)))
    y1 = max(0, min(height, py + int(ph * top)))
    x2 = max(0, min(width, px + int(pw * right)))
    y2 = max(0, min(height, py + int(ph * bottom)))
    if x2 <= x1 or y2 <= y1:
        return image.crop((0, 0, 1, 1))
    return image.crop((x1, y1, x2, y2))


def _prep_for_tesseract(crop: Image.Image, scale: int = 4, threshold: int = 132) -> Image.Image:
    gray = ImageOps.grayscale(crop)
    gray = ImageEnhance.Contrast(gray).enhance(2.1)
    new_size = (max(1, gray.width * scale), max(1, gray.height * scale))
    gray = gray.resize(new_size, Image.Resampling.LANCZOS)

    # Turn pale UI text into black text on a white page.
    return gray.point(lambda p: 0 if p > threshold else 255, mode="1").convert("L")


def _ocr(crop: Image.Image, psm: int = 6, whitelist: Optional[str] = None, timeout: float = 5.0) -> str:
    processed = _prep_for_tesseract(crop)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_name = tmp.name
            processed.save(tmp_name, format="PNG")

        cmd = [
            "tesseract",
            tmp_name,
            "stdout",
            "-l",
            "eng",
            "--oem",
            "1",
            "--psm",
            str(psm),
            "-c",
            "load_system_dawg=0",
            "-c",
            "load_freq_dawg=0",
        ]
        if whitelist:
            cmd.extend(["-c", "tessedit_char_whitelist=" + whitelist])

        env = os.environ.copy()
        env["OMP_THREAD_LIMIT"] = "1"
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        return proc.stdout or ""
    except subprocess.TimeoutExpired:
        return ""
    except Exception:
        return ""
    finally:
        if tmp_name:
            try:
                os.remove(tmp_name)
            except OSError:
                pass


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

    fragments = [
        "RESER",
        "ESERV",
        "SERVE",
        "ERVED",
        "RVED",
        "RVEL",
        "RVEI",
        "RESEV",
        "ESEV",
        "RESV",
        "ERVEDD",
        "RESERVEO",
    ]
    if any(fragment in normalized for fragment in fragments):
        return True

    if 4 <= len(normalized) <= 15 and "R" in normalized and "E" in normalized and "V" in normalized:
        return _reserved_score(normalized) >= 0.52

    return False


def _clean_player_name(text: str) -> str:
    text = _clean_line(text)
    if not text:
        return ""

    words = []
    for word in text.split():
        if _is_reserved_line(word):
            continue
        words.append(word)
    text = " ".join(words)

    banned = [
        "HEALTH",
        "INFO",
        "ITEMS",
        "ATTACK",
        "TACTICS",
        "LEGEND",
        "VETERAN",
        "ASSIGNED",
        "RESERVED",
        "BATTLEGROUP",
        "STRAW",
        "ALLIANCE",
        "KO",
        "K.O",
    ]
    for word in banned:
        text = re.sub(r"\b" + re.escape(word) + r"\b", "", text, flags=re.IGNORECASE)

    if re.search(r"\d+(?:\.\d+)?\s*%", text):
        return ""

    text = text.strip(" \t\r\n:;,.`'\"[]{}()<>«»")
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) < 3:
        return ""
    if len(text) > 40:
        return ""
    return text


def _extract_name_from_row(row_text: str) -> str:
    lines = [_clean_line(line) for line in row_text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    for line in lines:
        if _is_reserved_line(line):
            continue
        cleaned = _clean_player_name(line)
        if cleaned:
            return cleaned

    for line in lines:
        cleaned = _clean_player_name(line)
        if cleaned:
            return cleaned

    return ""


def _extract_bg_from_text(text: str) -> Optional[int]:
    raw = text.upper().replace("0", "O")
    patterns = [
        r"BATTLEGROUP\s*([1-9])",
        r"BATTLE\s*GROUP\s*([1-9])",
        r"GROUP\s*([1-9])",
        r"BG\s*([1-9])",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


def _extract_battlegroup(image: Image.Image, panel: tuple[int, int, int, int]) -> tuple[Optional[int], str]:
    header = _crop_rel(image, panel, 0.26, 0.035, 0.78, 0.145)
    text = _ocr(header, psm=6, timeout=4.0)
    clean = _clean_line(text)
    return _extract_bg_from_text(clean), clean


def _row_specs() -> list[tuple[float, float]]:
    return [
        (0.235, 0.385),
        (0.395, 0.545),
        (0.555, 0.705),
        (0.715, 0.865),
    ]


def _extract_reserved_names(image: Image.Image, panel: tuple[int, int, int, int]) -> tuple[list[str], list[str]]:
    reserved = []
    debug_lines = []

    for row_index, bounds in enumerate(_row_specs(), start=1):
        top, bottom = bounds
        row_crop = _crop_rel(image, panel, 0.155, top + 0.018, 0.545, bottom - 0.018)
        row_text = _ocr(row_crop, psm=6, timeout=5.0)
        cleaned_row = _clean_line(row_text)
        debug_lines.append("row " + str(row_index) + ": " + (cleaned_row or "empty"))

        if not _is_reserved_line(row_text):
            continue

        name = _extract_name_from_row(row_text)
        if name and name not in reserved:
            reserved.append(name)

    return reserved, debug_lines


def parse_battlegroup_screenshot(image_bytes: bytes) -> dict[str, Any]:
    image = _pil_from_bytes(image_bytes)
    panel = _find_panel_bounds(image)
    bg, header_ocr = _extract_battlegroup(image, panel)
    reserved, row_lines = _extract_reserved_names(image, panel)

    px, py, pw, ph = panel
    panel_debug = "panel x=" + str(px) + " y=" + str(py) + " w=" + str(pw) + " h=" + str(ph)

    return {
        "battlegroup": bg,
        "reserved": reserved,
        "header_ocr": header_ocr,
        "row_ocr_lines": [panel_debug] + row_lines,
    }
