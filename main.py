import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from ocr_parser import parse_battlegroup_image

# ---------- Paths / persistent files ----------

BASE_PATH = "/data" if os.getenv("FLY_APP_NAME") else "."
os.makedirs(BASE_PATH, exist_ok=True)

DATA_FILE = os.path.join(BASE_PATH, "reservations.json")
CONFIG_FILE = os.path.join(BASE_PATH, "config.json")
BACKUP_FILE = os.path.join(BASE_PATH, "backup.json")

DEFAULT_DATA = {"battlegroups": {}}
DEFAULT_CONFIG = {"log_channel_id": None, "scan_channel_id": None}

# Pending scans are intentionally in-memory. If the bot restarts, re-upload the screenshot.
pending_scans: Dict[str, dict] = {}


# ---------- JSON helpers ----------

def safe_load_json(path: str, default: dict) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default.copy()


def safe_write_json(path: str, data: dict) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).casefold()


def load_data() -> dict:
    data = safe_load_json(DATA_FILE, DEFAULT_DATA)
    if "battlegroups" not in data or not isinstance(data["battlegroups"], dict):
        data = DEFAULT_DATA.copy()
    return data


def save_data(data: dict) -> None:
    safe_write_json(DATA_FILE, data)


def load_config() -> dict:
    config = safe_load_json(CONFIG_FILE, DEFAULT_CONFIG)
    for key, value in DEFAULT_CONFIG.items():
        config.setdefault(key, value)
    return config


def save_config(config: dict) -> None:
    safe_write_json(CONFIG_FILE, config)


# ---------- Data operations ----------

def ensure_bg(data: dict, bg: int) -> dict:
    bg_key = str(bg)
    groups = data.setdefault("battlegroups", {})
    group = groups.setdefault(bg_key, {"reserved": {}})
    group.setdefault("reserved", {})
    return group


def add_reserved_players(
    *,
    bg: int,
    names: List[str],
    source_message_id: int,
    image_url: str,
    detected_by: str,
    replace: bool = False,
) -> int:
    data = load_data()
    group = ensure_bg(data, bg)

    if replace:
        group["reserved"] = {}

    added_or_updated = 0
    for name in names:
        cleaned = re.sub(r"\s+", " ", name).strip()
        if not cleaned:
            continue

        key = canonical_name(cleaned)
        group["reserved"][key] = {
            "name": cleaned,
            "last_seen": utc_now(),
            "source_message_id": str(source_message_id),
            "image_url": image_url,
            "detected_by": detected_by,
        }
        added_or_updated += 1

    save_data(data)
    return added_or_updated


def remove_player_everywhere(player: str) -> int:
    data = load_data()
    target = canonical_name(player)
    removed = 0

    for group in data.get("battlegroups", {}).values():
        reserved = group.get("reserved", {})
        if target in reserved:
            del reserved[target]
            removed += 1

    save_data(data)
    return removed


def rename_player_everywhere(old: str, new: str) -> int:
    data = load_data()
    old_key = canonical_name(old)
    new_clean = re.sub(r"\s+", " ", new).strip()
    new_key = canonical_name(new_clean)
    renamed = 0

    for group in data.get("battlegroups", {}).values():
        reserved = group.get("reserved", {})
        if old_key in reserved:
            record = reserved.pop(old_key)
            record["name"] = new_clean
            record["renamed_at"] = utc_now()
            reserved[new_key] = record
            renamed += 1

    save_data(data)
    return renamed


def get_bg_reserved(bg: int) -> List[str]:
    data = load_data()
    group = data.get("battlegroups", {}).get(str(bg), {})
    records = group.get("reserved", {})
    names = [v.get("name", k) for k, v in records.items()]
    return sorted(names, key=str.casefold)


def all_reserved_by_bg() -> Dict[str, List[str]]:
    data = load_data()
    result = {}
    for bg, group in data.get("battlegroups", {}).items():
        records = group.get("reserved", {})
        result[bg] = sorted([v.get("name", k) for k, v in records.items()], key=str.casefold)
    return dict(sorted(result.items(), key=lambda item: int(item[0]) if item[0].isdigit() else item[0]))


# ---------- Discord setup ----------

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


async def log_admin_action(ctx_or_message, action: str) -> None:
    config = load_config()
    channel_id = config.get("log_channel_id")
    if not channel_id:
        return

    try:
        channel = await bot.fetch_channel(int(channel_id))
        author = getattr(ctx_or_message, "author", None)
        if hasattr(ctx_or_message, "message"):
            author = ctx_or_message.message.author
        author_text = str(author) if author else "Unknown"
        await channel.send(f"`{utc_now()}` **{author_text}** — {action}")
    except Exception as e:
        print(f"[LOG ERROR] {e}")


def attachment_is_image(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))


def build_scan_message(scan_id: str, parsed: dict) -> str:
    bg = parsed.get("battlegroup")
    reserved = parsed.get("reserved", [])
    raw_header = parsed.get("raw_header", "")

    lines = [
        f"Scan ID: {scan_id}",
        f"Battlegroup: {bg if bg is not None else 'NOT FOUND'}",
        "",
        "Reserved detected:",
    ]

    if reserved:
        lines.extend(f"- {name}" for name in reserved)
    else:
        lines.append("- None detected")

    lines.extend([
        "",
        f"Header OCR: {raw_header or '(blank)'}",
        "",
        f"Save: !confirm {scan_id}",
        f"Save and replace that BG: !confirm {scan_id} replace",
        f"Reject: !reject {scan_id}",
    ])

    return "```txt\n" + "\n".join(lines) + "\n```"


async def parse_attachment_to_pending(message: discord.Message, attachment: discord.Attachment) -> Optional[Tuple[str, dict]]:
    if not attachment_is_image(attachment):
        return None

    image_bytes = await attachment.read()
    parsed = parse_battlegroup_image(image_bytes)

    scan_id = uuid.uuid4().hex[:6].upper()
    pending_scans[scan_id] = {
        "parsed": parsed,
        "message_id": message.id,
        "channel_id": message.channel.id,
        "image_url": attachment.url,
        "created_by": str(message.author),
        "created_at": utc_now(),
    }

    return scan_id, parsed


# ---------- Events ----------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Commands get exclusive handling so !scan does not also auto-scan.
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    config = load_config()
    scan_channel_id = config.get("scan_channel_id")

    if scan_channel_id and message.channel.id == int(scan_channel_id) and message.attachments:
        for attachment in message.attachments:
            try:
                result = await parse_attachment_to_pending(message, attachment)
                if result is None:
                    continue
                scan_id, parsed = result
                await message.channel.send(build_scan_message(scan_id, parsed))
            except Exception as e:
                await message.channel.send(f"❌ OCR failed on `{attachment.filename}`: `{e}`")
                print(f"[OCR ERROR] {e}")


# ---------- Setup commands ----------

@bot.command()
@commands.has_permissions(administrator=True)
async def setlogchannel(ctx, channel_id: int):
    try:
        channel = await bot.fetch_channel(channel_id)
    except discord.NotFound:
        return await ctx.send("❌ Channel not found.")
    except discord.Forbidden:
        return await ctx.send("❌ I cannot access that channel.")

    config = load_config()
    config["log_channel_id"] = channel.id
    save_config(config)
    await ctx.send(f"✅ Log channel set to {channel.mention}.")


@bot.command()
@commands.has_permissions(administrator=True)
async def setscanchannel(ctx, channel_id: int):
    try:
        channel = await bot.fetch_channel(channel_id)
    except discord.NotFound:
        return await ctx.send("❌ Channel not found.")
    except discord.Forbidden:
        return await ctx.send("❌ I cannot access that channel.")

    config = load_config()
    config["scan_channel_id"] = channel.id
    save_config(config)
    await ctx.send(f"✅ Scan channel set to {channel.mention}. Images posted there will be OCR-scanned.")


@bot.command()
async def viewsetup(ctx):
    config = load_config()
    await ctx.send(
        "```txt\n"
        f"Log channel: {config.get('log_channel_id')}\n"
        f"Scan channel: {config.get('scan_channel_id')}\n"
        "```"
    )


# ---------- OCR commands ----------

@bot.command()
@commands.has_permissions(administrator=True)
async def scan(ctx):
    """Scan attached image(s), or image(s) from the message being replied to."""
    target_message = ctx.message

    if not target_message.attachments and ctx.message.reference:
        try:
            ref = ctx.message.reference
            target_message = await ctx.channel.fetch_message(ref.message_id)
        except Exception:
            return await ctx.send("❌ Could not read the replied-to message.")

    if not target_message.attachments:
        return await ctx.send("❌ Attach an image to `!scan`, or reply to an image with `!scan`.")

    any_scanned = False
    for attachment in target_message.attachments:
        try:
            result = await parse_attachment_to_pending(target_message, attachment)
            if result is None:
                continue
            scan_id, parsed = result
            any_scanned = True
            await ctx.send(build_scan_message(scan_id, parsed))
        except Exception as e:
            await ctx.send(f"❌ OCR failed on `{attachment.filename}`: `{e}`")
            print(f"[OCR ERROR] {e}")

    if not any_scanned:
        await ctx.send("❌ No image attachments found.")


@bot.command()
@commands.has_permissions(administrator=True)
async def confirm(ctx, scan_id: str, mode: str = "merge"):
    scan_id = scan_id.upper().strip()
    pending = pending_scans.get(scan_id)
    if not pending:
        return await ctx.send("❌ Unknown or expired scan ID. Re-scan the image.")

    parsed = pending["parsed"]
    bg = parsed.get("battlegroup")
    reserved = parsed.get("reserved", [])

    if bg is None:
        return await ctx.send("❌ Cannot confirm: battlegroup was not detected.")
    if not reserved:
        return await ctx.send("❌ Cannot confirm: no reserved players were detected.")

    replace = mode.casefold() == "replace"
    if mode.casefold() not in {"merge", "replace"}:
        return await ctx.send("❌ Use `!confirm [scan_id]` or `!confirm [scan_id] replace`.")

    count = add_reserved_players(
        bg=int(bg),
        names=reserved,
        source_message_id=pending["message_id"],
        image_url=pending["image_url"],
        detected_by=str(ctx.author),
        replace=replace,
    )

    del pending_scans[scan_id]
    verb = "replaced" if replace else "merged"
    await ctx.send(f"✅ Saved BG{bg}: {count} reserved player(s) {verb}.")
    await log_admin_action(ctx, f"Confirmed OCR scan `{scan_id}` for BG{bg}; {count} player(s) {verb}.")


@bot.command()
@commands.has_permissions(administrator=True)
async def reject(ctx, scan_id: str):
    scan_id = scan_id.upper().strip()
    if pending_scans.pop(scan_id, None):
        await ctx.send(f"🗑️ Rejected scan `{scan_id}`.")
    else:
        await ctx.send("❌ Unknown or expired scan ID.")


# ---------- Viewing commands ----------

@bot.command(name="list")
async def list_reserved(ctx):
    grouped = all_reserved_by_bg()
    if not any(grouped.values()):
        return await ctx.send("No reserved players saved yet.")

    lines = []
    for bg, names in grouped.items():
        lines.append(f"BATTLEGROUP {bg}")
        if names:
            lines.extend(f"- {name}" for name in names)
        else:
            lines.append("- None")
        lines.append("")

    await ctx.send("```txt\n" + "\n".join(lines).strip() + "\n```")


@bot.command()
async def viewbg(ctx, bg: int):
    names = get_bg_reserved(bg)
    if not names:
        return await ctx.send(f"No reserved players saved for BG{bg}.")

    lines = [f"BATTLEGROUP {bg}", ""]
    lines.extend(f"- {name}" for name in names)
    await ctx.send("```txt\n" + "\n".join(lines) + "\n```")


# ---------- Data management commands ----------

@bot.command()
@commands.has_permissions(administrator=True)
async def clear(ctx, *, player: str):
    removed = remove_player_everywhere(player)
    if removed == 0:
        return await ctx.send(f"❌ `{player}` was not found.")
    await ctx.send(f"✅ Removed `{player}` from {removed} battlegroup(s).")
    await log_admin_action(ctx, f"Cleared `{player}` from {removed} battlegroup(s).")


@bot.command()
@commands.has_permissions(administrator=True)
async def clearbg(ctx, bg: int):
    data = load_data()
    group = ensure_bg(data, bg)
    count = len(group.get("reserved", {}))
    group["reserved"] = {}
    save_data(data)
    await ctx.send(f"✅ Cleared {count} reserved player(s) from BG{bg}.")
    await log_admin_action(ctx, f"Cleared BG{bg} reserved list.")


@bot.command()
@commands.has_permissions(administrator=True)
async def rename(ctx, *, text: str):
    if "->" not in text:
        return await ctx.send("❌ Use: `!rename old name -> new name`")

    old, new = [part.strip() for part in text.split("->", 1)]
    if not old or not new:
        return await ctx.send("❌ Use: `!rename old name -> new name`")

    count = rename_player_everywhere(old, new)
    if count == 0:
        return await ctx.send(f"❌ `{old}` was not found.")

    await ctx.send(f"✅ Renamed `{old}` → `{new}` in {count} battlegroup(s).")
    await log_admin_action(ctx, f"Renamed `{old}` → `{new}` in {count} battlegroup(s).")


@bot.command()
@commands.has_permissions(administrator=True)
async def wipe(ctx):
    def check(m: discord.Message):
        return m.author == ctx.author and m.channel == ctx.channel

    await ctx.send("⚠️ This clears ALL saved reserved-player data. Type `CONFIRM` to proceed, or anything else to cancel.")
    try:
        reply = await bot.wait_for("message", timeout=20.0, check=check)
    except asyncio.TimeoutError:
        return await ctx.send("Timed out. Wipe canceled.")

    if reply.content.strip().upper() != "CONFIRM":
        return await ctx.send("Wipe canceled.")

    save_data(DEFAULT_DATA.copy())
    await ctx.send("✅ All reserved-player data wiped.")
    await log_admin_action(ctx, "Wiped all reserved-player data.")


@bot.command()
@commands.has_permissions(administrator=True)
async def exportdata(ctx):
    data = load_data()
    payload = {
        "__meta__": {
            "exported_at": utc_now(),
            "exported_by": str(ctx.author),
            "schema": "reserved-battlegroups-v1",
        },
        **data,
    }
    safe_write_json(BACKUP_FILE, payload)
    await ctx.send(file=discord.File(BACKUP_FILE, filename="backup.json"))


@bot.command()
@commands.has_permissions(administrator=True)
async def importdata(ctx):
    if not ctx.message.attachments:
        return await ctx.send("❌ Attach a JSON backup file to `!importdata`.")

    attachment = ctx.message.attachments[0]
    if not attachment.filename.lower().endswith(".json"):
        return await ctx.send("❌ Backup must be a `.json` file.")

    try:
        raw = await attachment.read()
        payload = json.loads(raw.decode("utf-8"))
    except Exception as e:
        return await ctx.send(f"❌ Could not read JSON: `{e}`")

    if "battlegroups" not in payload:
        return await ctx.send("❌ Invalid backup: missing `battlegroups`.")

    imported = {"battlegroups": payload["battlegroups"]}
    save_data(imported)
    await ctx.send("✅ Imported backup and replaced current data.")
    await log_admin_action(ctx, "Imported reservation backup.")


@bot.command()
async def helpme(ctx):
    text = """
OCR
!scan
  Scan attached image(s), or reply to an image with !scan.
!confirm [scan_id]
  Save detected reserved players into their battlegroup.
!confirm [scan_id] replace
  Replace that battlegroup's saved reserved list with the scan result.
!reject [scan_id]
  Reject a pending scan.

Viewing
!list
  List all saved reserved players by battlegroup.
!viewbg [number]
  View one battlegroup.

Data Management
!rename old name -> new name
  Rename a saved player across all battlegroups.
!clear [player]
  Remove a player across all battlegroups.
!clearbg [number]
  Clear one battlegroup.
!wipe
  Clear all saved data after confirmation.
!exportdata
  Export current data as backup.json.
!importdata
  Import a JSON backup attached to the command.

Setup
!setscanchannel [channel_id]
  Set the channel where uploaded images are automatically scanned.
!setlogchannel [channel_id]
  Set the admin log channel.
!viewsetup
  Show saved setup values.
""".strip()
    await ctx.send(f"```txt\n{text}\n```")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("❌ You need administrator permission to use that command.")
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send("❌ Missing required argument. Use `!helpme`.")
    if isinstance(error, commands.BadArgument):
        return await ctx.send("❌ Invalid argument type. Use `!helpme`.")
    if isinstance(error, commands.CommandNotFound):
        return

    print(f"[COMMAND ERROR] {repr(error)}")
    await ctx.send(f"❌ Error: `{error}`")


bot.run(TOKEN)
