import csv
import io
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

from PIL import Image, ImageOps, ImageFilter, ImageEnhance, ImageStat


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


BAD_NAME_WORDS = {
    "RESERVED", "RESERVE", "ASSIGNED", "LEGEND", "VETERAN", "KO", "PTS",
    "HEALTH", "INFO", "ITEMS", "ATTACK", "TACTICS", "BONUS", "BUFF",
    "BATTLEGROUP", "BATTLE", "GROUP", "ALLIANCE", "STRAW", "HAT",
    "FIGHT", "INFIGHT", "KILLED", "COMBAT",
}

RESERVED_FRAGMENTS = {
    "RESERVED", "RESERVE", "RESERVD", "RESERVEO", "RESERUED", "RESEVED",
    "RESEVVED", "RFSERVED", "RESFRVED", "RERVED", "ERVED", "SERVED",
    "RVED", "RVE", "RVEL", "RVD", "RSERVED", "RESRVED", "REERVED",
}

STATUS_WORDS = set(RESERVED_FRAGMENTS).union({
    "ASSIGNED", "IN", "FIGHT", "INFIGHT", "K", "KO", "PTS",
})


def parse_battlegroup_image(image_bytes: bytes, battlegroup_override: Optional[int] = None) -> ScanResult:
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    image = normalize_input_size(image)
    panel = find_panel_box(image)

    header_text = ""
    battlegroup = battlegroup_override
    if battlegroup is None:
        header_box = relative_box(panel, 0.24, 0.025, 0.76, 0.155)
        header_text = ocr_text(image, header_box, psm=7, scale=3, mode="gray")
        if not header_text:
            header_text = ocr_text(image, header_box, psm=7, scale=3, mode="binary")
        battlegroup = extract_battlegroup(header_text)

    rows: list[RowDebug] = []
    reserved_names: list[str] = []

    for index, row in enumerate(row_boxes(panel), start=1):
        result = parse_row(image, row, index)
        rows.append(result)
        if result.reserved and result.name:
            reserved_names.append(result.name)

    return ScanResult(
        battlegroup=battlegroup,
        reserved_names=unique_keep_order(reserved_names),
        header_text=header_text,
        rows=rows,
        panel_box=panel,
    )


def normalize_input_size(image: Image.Image) -> Image.Image:
    # 1600 keeps name text readable while staying realistic for a 512 MB Fly machine.
    max_width = 1600
    if image.width <= max_width:
        return image
    ratio = max_width / image.width
    return image.resize((max_width, int(image.height * ratio)), Image.Resampling.LANCZOS)


def find_panel_box(image: Image.Image) -> tuple[int, int, int, int]:
    # The MCOC panel is centered, but the exact screenshot can include extra side UI.
    # These bounds intentionally include the full list area and ignore outer space background.
    w, h = image.size
    aspect = w / max(1, h)
    if aspect >= 1.85:
        return (int(w * 0.190), int(h * 0.025), int(w * 0.800), int(h * 0.955))
    if aspect >= 1.55:
        return (int(w * 0.170), int(h * 0.020), int(w * 0.830), int(h * 0.960))
    return (int(w * 0.120), int(h * 0.020), int(w * 0.880), int(h * 0.960))


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


def row_boxes(panel: tuple[int, int, int, int]) -> list[dict[str, tuple[int, int, int, int]]]:
    # Rows are stable relative to the panel even when the right-side cells differ.
    # Crops are deliberately text-column-only: portraits and item boxes cause OCR noise.
    starts = [0.218, 0.396, 0.574, 0.752]
    rows = []
    for y in starts:
        rows.append({
            "name": relative_box(panel, 0.168, y + 0.000, 0.505, y + 0.078),
            "status": relative_box(panel, 0.168, y + 0.053, 0.455, y + 0.130),
            "full": relative_box(panel, 0.160, y - 0.014, 0.520, y + 0.146),
        })
    return rows


def parse_row(image: Image.Image, boxes: dict[str, tuple[int, int, int, int]], row_index: int) -> RowDebug:
    full_box = boxes["full"]
    name_box = boxes["name"]
    status_box = boxes["status"]

    raw_parts = []
    all_lines = []

    # Primary pass: OCR the combined name and status region so line order can be used.
    full_lines = ocr_lines(image, full_box, psm=6, scale=4, mode="gray")
    raw_parts.append("FULL_GRAY: " + join_lines(full_lines))
    all_lines.extend(full_lines)

    reserved = lines_have_reserved(full_lines)
    name = name_from_reserved_context(full_lines)

    # Second pass: binary often reads RESERVED better than grayscale.
    if not reserved or not name:
        full_lines_bin = ocr_lines(image, full_box, psm=6, scale=4, mode="binary")
        raw_parts.append("FULL_BIN: " + join_lines(full_lines_bin))
        all_lines.extend(full_lines_bin)
        if not reserved:
            reserved = lines_have_reserved(full_lines_bin)
        if not name:
            name = name_from_reserved_context(full_lines_bin)

    # Status-only fallback: catches rows where the full crop smears the status word.
    if not reserved:
        status_lines = []
        for threshold in [105, 125, 145, "auto"]:
            lines = ocr_lines(
                image,
                status_box,
                psm=7,
                scale=5,
                mode="binary",
                threshold=threshold,
                whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            )
            status_lines.extend(lines)
        raw_parts.append("STATUS: " + join_lines(status_lines))
        all_lines.extend(status_lines)
        reserved = lines_have_reserved(status_lines)

    # Name-only fallback. Run only when the row is known or strongly suspected to be reserved.
    if reserved and not name:
        name_lines = []
        for mode in ["gray", "binary", "soft"]:
            lines = ocr_lines(image, name_box, psm=7, scale=5, mode=mode)
            name_lines.extend(lines)
            name = extract_best_name(lines)
            if name:
                break
        raw_parts.append("NAME: " + join_lines(name_lines))
        all_lines.extend(name_lines)

    # If name still fails, try the whole crop but prefer a line above a reserved-looking line.
    if reserved and not name:
        name = extract_best_name(all_lines)

    debug_lines = unique_keep_order(clean_ocr_lines("\n".join(all_lines)))
    return RowDebug(
        row=row_index,
        raw_text="\n".join([part for part in raw_parts if part.strip()]),
        cleaned_lines=debug_lines,
        reserved=reserved,
        name=name,
        box=full_box,
    )


def ocr_text(
    image: Image.Image,
    box: tuple[int, int, int, int],
    psm: int,
    scale: int,
    mode: str,
    threshold="auto",
    whitelist: Optional[str] = None,
) -> str:
    crop = image.crop(clamp_box(box, image.size))
    prepared = prep_text_crop(crop, scale=scale, mode=mode, threshold=threshold)
    return run_tesseract(prepared, psm=psm, whitelist=whitelist)


def ocr_lines(
    image: Image.Image,
    box: tuple[int, int, int, int],
    psm: int,
    scale: int,
    mode: str,
    threshold="auto",
    whitelist: Optional[str] = None,
) -> list[str]:
    text = ocr_text(image, box, psm=psm, scale=scale, mode=mode, threshold=threshold, whitelist=whitelist)
    return clean_ocr_lines(text)


def clamp_box(box: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int, int, int]:
    w, h = size
    x1, y1, x2, y2 = box
    return (max(0, x1), max(0, y1), min(w, x2), min(h, y2))


def prep_text_crop(crop: Image.Image, scale: int, mode: str, threshold="auto") -> Image.Image:
    gray = crop.convert("L")
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray = ImageEnhance.Contrast(gray).enhance(2.2)
    gray = gray.filter(ImageFilter.SHARPEN)

    if scale > 1:
        gray = gray.resize((gray.width * scale, gray.height * scale), Image.Resampling.LANCZOS)

    if mode == "gray":
        return ImageOps.invert(gray)

    if mode == "soft":
        soft = ImageOps.invert(gray)
        soft = ImageEnhance.Contrast(soft).enhance(1.4)
        return soft

    if threshold == "auto":
        stat = ImageStat.Stat(gray)
        mean = stat.mean[0]
        threshold = max(92, min(170, int(mean + 28)))

    # Game text is light on a dark background. Tesseract prefers black text on white.
    return gray.point(lambda p: 0 if p > int(threshold) else 255)


def run_tesseract(image: Image.Image, psm: int, whitelist: Optional[str] = None) -> str:
    env = os.environ.copy()
    env["OMP_THREAD_LIMIT"] = "1"

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as img_file:
        image.save(img_file.name, optimize=False)
        image_path = img_file.name

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
    if whitelist:
        cmd.extend(["-c", "tessedit_char_whitelist=" + whitelist])

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
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
    compact = re.sub(r"\s+", "", compact)
    match = re.search(r"BATTLEGROUP([123])", compact)
    if match:
        return int(match.group(1))
    match = re.search(r"BATTLEGROUP([123])", compact)
    if match:
        return int(match.group(1))
    match = re.search(r"GROUP([123])", compact)
    if match:
        return int(match.group(1))
    return None


def clean_common_ocr(text: str) -> str:
    replacements = {
        "BATILEGROUP": "BATTLEGROUP",
        "BATTLEGR0UP": "BATTLEGROUP",
        "BATTIEGROUP": "BATTLEGROUP",
        "BATTLEGROUF": "BATTLEGROUP",
        "BATTLEGRQUP": "BATTLEGROUP",
        "BATTLE GROUP": "BATTLEGROUP",
        "BATILE GROUP": "BATTLEGROUP",
    }
    out = text
    for bad, good in replacements.items():
        out = out.replace(bad, good).replace(bad.lower(), good)
    return out


def clean_ocr_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        line = line.replace(chr(124), "I")
        line = line.replace("‘", "'").replace("’", "'")
        line = line.replace("“", '"').replace("”", '"')
        line = re.sub(r"\s+", " ", line)
        line = line.strip(" .,:;`\"()[]{}<>«»")
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
    if lines:
        joined = " ".join(lines)
        if looks_like_reserved(joined):
            return True
    return False


def looks_like_reserved(line: str) -> bool:
    cleaned = re.sub(r"[^A-Za-z]", "", line).upper()
    if not cleaned:
        return False
    if cleaned in RESERVED_FRAGMENTS:
        return True
    for fragment in RESERVED_FRAGMENTS:
        if len(fragment) >= 4 and fragment in cleaned:
            return True
    if len(cleaned) >= 5 and SequenceMatcher(None, cleaned, "RESERVED").ratio() >= 0.58:
        return True
    return False


def name_from_reserved_context(lines: list[str]) -> Optional[str]:
    cleaned = clean_ocr_lines("\n".join(lines))
    candidates = []
    for index, line in enumerate(cleaned):
        if looks_like_reserved(line):
            same_line = remove_status_text(line)
            same_line = cleanup_name(same_line)
            if is_plausible_name(same_line):
                candidates.append(same_line)
            for back in [1, 2]:
                pos = index - back
                if pos >= 0:
                    prev = cleanup_name(remove_status_text(cleaned[pos]))
                    if is_plausible_name(prev):
                        candidates.append(prev)
        else:
            # Handles lines like "Name RESERVED" if Tesseract joins them.
            if contains_reserved_word(line):
                maybe = cleanup_name(remove_status_text(line))
                if is_plausible_name(maybe):
                    candidates.append(maybe)
    if candidates:
        candidates.sort(key=name_score, reverse=True)
        return candidates[0]
    return None


def contains_reserved_word(line: str) -> bool:
    upper = re.sub(r"[^A-Za-z]", "", line).upper()
    return "RESERVED" in upper or "RESERVE" in upper


def extract_best_name(lines: list[str]) -> Optional[str]:
    candidates = []
    for line in clean_ocr_lines("\n".join(lines)):
        candidate = cleanup_name(remove_status_text(line))
        if is_plausible_name(candidate):
            candidates.append(candidate)
    if not candidates:
        return None
    candidates.sort(key=name_score, reverse=True)
    return candidates[0]


def remove_status_text(line: str) -> str:
    result = line
    for word in sorted(STATUS_WORDS, key=len, reverse=True):
        result = re.sub(re.escape(word), "", result, flags=re.IGNORECASE)
    result = re.sub(r"\b\d+(?:\.\d+)?%\b", "", result)
    result = re.sub(r"\b[\d,]+\s*PTS\b", "", result, flags=re.IGNORECASE)
    result = re.sub(r"K\.O\.", "", result, flags=re.IGNORECASE)
    return result.strip()


def cleanup_name(name: str) -> str:
    name = name.replace("—", "-").replace("–", "-")
    name = name.replace(chr(124), "I")
    name = name.replace("©", "©").replace("®", "®")
    name = re.sub(r"\s+", " ", name).strip()
    name = name.strip(" .,:;`\"()[]{}<>«»")
    return name


def is_plausible_name(name: str) -> bool:
    if not name or len(name) < 2:
        return False
    letters_digits = sum(ch.isalnum() for ch in name)
    if letters_digits < 2:
        return False
    compact = re.sub(r"[^A-Za-z]", "", name).upper()
    if compact:
        if any(bad in compact for bad in BAD_NAME_WORDS):
            return False
        if compact in {"RVED", "RVE", "RV", "RVEL", "CN", "OO", "QR", "NO", "TEXT"}:
            return False
    return True


def name_score(name: str) -> tuple[int, int, int, int]:
    alnum = sum(ch.isalnum() for ch in name)
    specials = sum(ch in "._~-©®×•" for ch in name)
    uppercase = sum(ch.isupper() for ch in name)
    spaces = name.count(" ")
    return (alnum, specials, uppercase, -spaces)


def unique_keep_order(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        value = cleanup_name(value)
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def join_lines(lines: list[str]) -> str:
    return " / ".join([line for line in lines if line])
