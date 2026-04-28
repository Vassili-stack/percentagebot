import json
import os
from copy import deepcopy
from typing import Any

DATA_DIR = os.getenv("DATA_DIR", "/data")
RESERVATIONS_FILE = os.path.join(DATA_DIR, "reservations.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

DEFAULT_DATA = {
    "battlegroups": {}
}

DEFAULT_CONFIG = {
    "log_channel_id": None,
    "scan_channel_id": None
}


def ensure_data_dir() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError:
        pass


def load_json(path: str, default: dict[str, Any]) -> dict[str, Any]:
    ensure_data_dir()
    try:
        with open(path, "r", encoding="utf-8") as file:
            loaded = json.load(file)
            if isinstance(loaded, dict):
                return loaded
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        pass
    return deepcopy(default)


def save_json(path: str, data: dict[str, Any]) -> None:
    ensure_data_dir()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_data() -> dict[str, Any]:
    data = load_json(RESERVATIONS_FILE, DEFAULT_DATA)
    data.setdefault("battlegroups", {})
    return data


def save_data(data: dict[str, Any]) -> None:
    save_json(RESERVATIONS_FILE, data)


def load_config() -> dict[str, Any]:
    config = load_json(CONFIG_FILE, DEFAULT_CONFIG)
    config.setdefault("log_channel_id", None)
    config.setdefault("scan_channel_id", None)
    return config


def save_config(config: dict[str, Any]) -> None:
    save_json(CONFIG_FILE, config)


def save_reservations(bg: int, names: list[str], replace: bool = False) -> dict[str, Any]:
    data = load_data()
    bg_key = str(bg)
    current = data["battlegroups"].setdefault(bg_key, [])

    if replace:
        data["battlegroups"][bg_key] = unique_keep_order(names)
    else:
        data["battlegroups"][bg_key] = unique_keep_order(current + names)

    save_data(data)
    return data


def remove_player(name: str) -> bool:
    data = load_data()
    changed = False
    target = name.casefold()
    for bg_key, names in data.get("battlegroups", {}).items():
        filtered = [n for n in names if n.casefold() != target]
        if len(filtered) != len(names):
            data["battlegroups"][bg_key] = filtered
            changed = True
    if changed:
        save_data(data)
    return changed


def rename_player(old: str, new: str) -> bool:
    data = load_data()
    changed = False
    old_key = old.casefold()
    for bg_key, names in data.get("battlegroups", {}).items():
        updated = []
        for name in names:
            if name.casefold() == old_key:
                updated.append(new)
                changed = True
            else:
                updated.append(name)
        data["battlegroups"][bg_key] = unique_keep_order(updated)
    if changed:
        save_data(data)
    return changed


def clear_bg(bg: int) -> bool:
    data = load_data()
    bg_key = str(bg)
    existed = bg_key in data.get("battlegroups", {})
    if existed:
        data["battlegroups"][bg_key] = []
        save_data(data)
    return existed


def wipe_all() -> None:
    save_data(deepcopy(DEFAULT_DATA))


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
