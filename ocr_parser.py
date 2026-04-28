import io
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

from PIL import Image, ImageOps, ImageFilter


@dataclass
class RowDebug:
    row: int
    raw_text: str
    cleaned_lines: list[str]
    reserved: bool
    name: Optional[str]
    box: tuple[int, int, int, int]


@dataclass
class ScanResult:
    battlegroup: Optional[int]
    reserved_names: list[str]
    header_text: str
    rows: list[RowDebug]
    panel_box: tuple[int, int, int, int]


STATUS_WORDS = [
    "RESERVED",
    "RESERVE",
    "RESEKVED",
    "RE5ERVED",
    "ASSIGNED",
    "KO",
    "K.O",
]

BAD_NAME_WORDS = {
    "RESERVED",
    "RESERVE",
    "ASSIGNED",
    "LEGEND",
    "VETERAN",
    "KO",
    "K.O",
    "HEALTH",
    "INFO",
    "ITEMS",
    "ATTACK",
    "TACTICS",
    "BATTLEGROUP",
}


def parse_battlegroup_image(image_bytes: bytes, battlegroup_override: Optional[int] = None) -> ScanResult:
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    image = normalize_input_size(image)
    panel = find_panel_box(image)

    header_text = ""
    battlegroup = battlegroup_override
    if battlegroup is None:
        header_box = relative_box(panel, 0.28, 0.035, 0.72, 0.155)
        header_text = run_tesseract(prep_text_crop(image.crop(header_box), scale=3), psm=7)
        battlegroup = extract_battlegroup(header_text)

    rows = []
    reserved_names = []

    for index, row_box in enumerate(row_boxes(panel), start=1):
        raw_text = run_tesseract(prep_text_crop(image.crop(row_box), scale=4), psm=6)
        lines = clean_ocr_lines(raw_text)
        reserved = lines_have_reserved(lines)
        name = extract_name_from_lines(lines) if reserved else None

        if reserved and name:
            reserved_names.append(name)

        rows.append(RowDebug(
            row=index,
            raw_text=raw_text,
            cleaned_lines=lines,
            reserved=reserved,
            name=name,
            box=row_box,
        ))

    return ScanResult(
        battlegroup=battlegroup,
        reserved_names=unique_keep_order(reserved_names),
        header_text=header_text,
        rows=rows,
        panel_box=panel,
    )


def normalize_input_size(image: Image.Image) -> Image.Image:
    # Large phone screenshots make Tesseract use more memory. This keeps text readable
    # while preventing unnecessary OCR memory spikes.
    max_width = 1600
    if image.width <= max_width:
        return image
    ratio = max_width / image.width
    new_size = (max_width, int(image.height * ratio))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def find_panel_box(image: Image.Image) -> tuple[int, int, int, int]:
    small = image.copy()
    small.thumbnail((520, 520), Image.Resampling.BILINEAR)
    sx = image.width / small.width
    sy = image.height / small.height

    pix = small.load()
    xs = []
    ys = []

    for y in range(0, small.height, 2):
        for x in range(0, small.width, 2):
            r, g, b = pix[x, y]
            avg = (r + g + b) // 3
            spread = max(r, g, b) - min(r, g, b)
            # The central game panel is neutral gray. Space background is darker
            # and usually blue or purple shifted.
            if 32 <= avg <= 95 and spread <= 26:
                xs.append(x)
                ys.append(y)

    if not xs or not ys:
        return default_panel_box(image)

    left = int(max(0, min(xs) * sx))
    top = int(max(0, min(ys) * sy))
    right = int(min(image.width, max(xs) * sx))
    bottom = int(min(image.height, max(ys) * sy))

    if right - left < image.width * 0.35 or bottom - top < image.height * 0.35:
        return default_panel_box(image)

    pad_x = int((right - left) * 0.015)
    pad_y = int((bottom - top) * 0.015)
    return (
        max(0, left - pad_x),
        max(0, top - pad_y),
        min(image.width, right + pad_x),
        min(image.height, bottom + pad_y),
    )


def default_panel_box(image: Image.Image) -> tuple[int, int, int, int]:
    return (
        int(image.width * 0.21),
        int(image.height * 0.045),
        int(image.width * 0.78),
        int(image.height * 0.94),
    )


def relative_box(panel: tuple[int, int, int, int], x1: float, y1: float, x2: float, y2: float) -> tuple[int, int, int, int]:
    px1, py1, px2, py2 = panel
    w = px2 - px1
    h = py2 - py1
    return (
        int(px1 + w * x1),
        int(py1 + h * y1),
        int(px1 + w * x2),
        int(py1 + h * y2),
    )


def row_boxes(panel: tuple[int, int, int, int]) -> list[tuple[int, int, int, int]]:
    # Crop only the text column. This deliberately excludes portraits, item boxes,
    # and most background noise.
    y_starts = [0.235, 0.410, 0.585, 0.760]
    boxes = []
    for y in y_starts:
        boxes.append(relative_box(panel, 0.165, y, 0.505, y + 0.125))
    return boxes


def prep_text_crop(crop: Image.Image, scale: int = 4) -> Image.Image:
    gray = crop.convert("L")
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray = gray.filter(ImageFilter.SHARPEN)
    if scale > 1:
        gray = gray.resize((gray.width * scale, gray.height * scale), Image.Resampling.LANCZOS)
    # Light text on dark background. Keep only strong foreground strokes.
    threshold = 118
    bw = gray.point(lambda p: 255 if p > threshold else 0)
    return bw


def run_tesseract(image: Image.Image, psm: int = 6) -> str:
    env = os.environ.copy()
    env["OMP_THREAD_LIMIT"] = "1"

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as img_file:
        image.save(img_file.name)
        image_path = img_file.name

    try:
        cmd = [
            "tesseract",
            image_path,
            "stdout",
            "--oem",
            "1",
            "--psm",
            str(psm),
            "-l",
            "eng",
        ]
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=8,
            env=env,
        )
        return completed.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""
    finally:
        try:
            os.remove(image_path)
        except OSError:
            pass


def extract_battlegroup(text: str) -> Optional[int]:
    compact = clean_common_ocr(text).upper()
    match = re.search(r"BATTLEGROUP\s*(\d)", compact)
    if match:
        return int(match.group(1))

    # Some OCR outputs omit spacing or misread one nearby character.
    for digit in range(1, 4):
        if f"BATTLEGROUP{digit}" in compact:
            return digit
    return None


def clean_common_ocr(text: str) -> str:
    return (
        text.replace("BATILEGROUP", "BATTLEGROUP")
        .replace("BATTLEGR0UP", "BATTLEGROUP")
        .replace("BATTIEGROUP", "BATTLEGROUP")
        .replace("BATTLEGROUF", "BATTLEGROUP")
    )


def clean_ocr_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        line = line.replace(chr(124), "")
        line = line.replace("‘", "'").replace("’", "'")
        line = re.sub(r"\s+", " ", line)
        line = line.strip(" .,:;`\"()[]{}")
        if not line:
            continue
        if len(line) == 1 and not line.isalnum():
            continue
        lines.append(line)
    return lines


def lines_have_reserved(lines: list[str]) -> bool:
    for line in lines:
        if looks_like_reserved(line):
            return True
    return False


def looks_like_reserved(line: str) -> bool:
    cleaned = re.sub(r"[^A-Za-z]", "", line).upper()
    if "RESERVED" in cleaned:
        return True
    if cleaned in {"RESERVE", "RESEKVED", "RE5ERVED", "RESERVEO", "RESERUED"}:
        return True
    if SequenceMatcher(None, cleaned, "RESERVED").ratio() >= 0.72:
        return True
    return False


def extract_name_from_lines(lines: list[str]) -> Optional[str]:
    candidates = []
    for line in lines:
        candidate = remove_status_text(line)
        candidate = cleanup_name(candidate)
        if is_plausible_name(candidate):
            candidates.append(candidate)

    if not candidates:
        return None

    # Prefer the first plausible player-name line in the row crop.
    return candidates[0]


def remove_status_text(line: str) -> str:
    result = line
    for word in STATUS_WORDS:
        result = re.sub(word, "", result, flags=re.IGNORECASE)
    return result.strip()


def cleanup_name(name: str) -> str:
    name = name.replace("—", "-").replace("–", "-")
    name = re.sub(r"\s+", " ", name).strip()
    name = name.strip(" .,:;`\"()[]{}")
    return name


def is_plausible_name(name: str) -> bool:
    if not name:
        return False
    if len(name) < 2:
        return False

    letters_digits = sum(ch.isalnum() for ch in name)
    if letters_digits < 2:
        return False

    upper = re.sub(r"[^A-Za-z]", "", name).upper()
    if not upper:
        return True
    for bad in BAD_NAME_WORDS:
        if bad in upper:
            return False
    return True


def unique_keep_order(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out
