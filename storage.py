from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

RESERVATIONS_PATH = DATA_DIR / "reservations.json"
CONFIG_PATH = DATA_DIR / "config.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def load_reservations() -> dict[str, Any]:
    data = _read_json(RESERVATIONS_PATH, {"battlegroups": {}})
    data.setdefault("battlegroups", {})
    return data


def save_reservations(data: dict[str, Any]) -> None:
    data.setdefault("battlegroups", {})
    _write_json(RESERVATIONS_PATH, data)


def load_config() -> dict[str, Any]:
    data = _read_json(CONFIG_PATH, {})
    if not isinstance(data, dict):
        return {}
    return data


def save_config(data: dict[str, Any]) -> None:
    _write_json(CONFIG_PATH, data)


def merge_bg_reservations(bg: int, names: list[str], *, replace: bool = False, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    data = load_reservations()
    bg_key = str(bg)
    data["battlegroups"].setdefault(bg_key, {"reserved": [], "meta": {}})

    current = [] if replace else list(data["battlegroups"][bg_key].get("reserved", []))
    for name in names:
        if name not in current:
            current.append(name)

    data["battlegroups"][bg_key]["reserved"] = current
    if meta:
        data["battlegroups"][bg_key]["meta"] = meta

    save_reservations(data)
    return data


def remove_player(name: str) -> int:
    data = load_reservations()
    removed = 0
    target = name.casefold()
    for bg_data in data.get("battlegroups", {}).values():
        old = list(bg_data.get("reserved", []))
        new = [item for item in old if item.casefold() != target]
        removed += len(old) - len(new)
        bg_data["reserved"] = new
    save_reservations(data)
    return removed


def rename_player(old_name: str, new_name: str) -> int:
    data = load_reservations()
    changed = 0
    target = old_name.casefold()
    for bg_data in data.get("battlegroups", {}).values():
        updated = []
        for item in bg_data.get("reserved", []):
            if item.casefold() == target:
                if new_name not in updated:
                    updated.append(new_name)
                changed += 1
            else:
                if item not in updated:
                    updated.append(item)
        bg_data["reserved"] = updated
    save_reservations(data)
    return changed


def wipe_all() -> None:
    save_reservations({"battlegroups": {}})
