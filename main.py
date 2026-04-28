import asyncio
import io
import json
import os
import secrets
import shlex
import traceback
from dataclasses import asdict
from typing import Optional

import discord

from ocr_parser import parse_battlegroup_image
from storage import (
    clear_bg,
    load_config,
    load_data,
    remove_player,
    rename_player,
    save_config,
    save_data,
    save_reservations,
    wipe_all,
)

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("BOT_PREFIX", "!")
MAX_MESSAGE = 1850

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
scan_lock = asyncio.Lock()
pending_scans = {}


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = (message.content or "").strip()
    if not content.startswith(PREFIX):
        return

    body = content[len(PREFIX):].strip()
    if not body:
        return

    command, args_text = split_command(body)
    command = command.lower()

    try:
        if command == "scan":
            await cmd_scan(message, args_text)
        elif command == "confirm":
            await cmd_confirm(message, args_text)
        elif command == "reject":
            await cmd_reject(message, args_text)
        elif command == "list":
            await cmd_list(message)
        elif command == "viewbg":
            await cmd_viewbg(message, args_text)
        elif command == "clearbg":
            await cmd_clearbg(message, args_text)
        elif command == "clear":
            await cmd_clear_player(message, args_text)
        elif command == "rename":
            await cmd_rename(message, args_text)
        elif command == "wipe":
            await cmd_wipe(message, args_text)
        elif command == "exportdata":
            await cmd_exportdata(message)
        elif command == "importdata":
            await cmd_importdata(message)
        elif command == "setlogchannel":
            await cmd_set_channel(message, args_text, "log_channel_id", "log channel")
        elif command == "setscanchannel":
            await cmd_set_channel(message, args_text, "scan_channel_id", "scan channel")
        elif command == "config":
            await cmd_config(message)
        elif command in {"help", "commands"}:
            await cmd_help(message)
    except Exception as error:
        print(traceback.format_exc())
        await send_code(message.channel, f"Error: {type(error).__name__}: {error}")


def split_command(text: str) -> tuple[str, str]:
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def parse_bg_arg(args_text: str) -> tuple[Optional[int], bool]:
    bg = None
    debug = False
    for token in args_text.split():
        clean = token.lower().strip()
        if clean == "debug":
            debug = True
            continue
        if clean.startswith("bg"):
            clean = clean[2:]
        if clean.isdigit():
            number = int(clean)
            if 1 <= number <= 3:
                bg = number
    return bg, debug


async def cmd_scan(message: discord.Message, args_text: str):
    config = load_config()
    scan_channel_id = config.get("scan_channel_id")
    if scan_channel_id and message.channel.id != int(scan_channel_id):
        await message.reply(f"Scans are set to <#{scan_channel_id}>.")
        return

    bg_override, debug = parse_bg_arg(args_text)
    image_bytes = await find_image_for_scan(message)
    if image_bytes is None:
        await message.reply("No image found. Attach a screenshot, reply to one, or send the scan command right after the screenshot.")
        return

    async with message.channel.typing():
        async with scan_lock:
            result = await asyncio.to_thread(parse_battlegroup_image, image_bytes, bg_override)

    scan_id = secrets.token_hex(3).upper()
    pending_scans[scan_id] = {
        "battlegroup": result.battlegroup,
        "reserved_names": result.reserved_names,
        "author_id": message.author.id,
    }

    output = format_scan_result(scan_id, result, debug)
    await send_code(message.channel, output)


async def find_image_for_scan(message: discord.Message) -> Optional[bytes]:
    image = await first_image_bytes(message.attachments)
    if image is not None:
        return image

    if message.reference and message.reference.resolved:
        resolved = message.reference.resolved
        if isinstance(resolved, discord.Message):
            image = await first_image_bytes(resolved.attachments)
            if image is not None:
                return image

    async for old in message.channel.history(limit=10, before=message):
        if old.author.bot:
            continue
        image = await first_image_bytes(old.attachments)
        if image is not None:
            return image

    return None


async def first_image_bytes(attachments) -> Optional[bytes]:
    for attachment in attachments:
        name = (attachment.filename or "").lower()
        content_type = attachment.content_type or ""
        is_image = content_type.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp"))
        if is_image:
            return await attachment.read()
    return None


def format_scan_result(scan_id: str, result, debug: bool) -> str:
    bg = result.battlegroup if result.battlegroup is not None else "not detected"
    lines = [
        f"Scan ID: {scan_id}",
        f"Battlegroup: {bg}",
        "",
        "Reserved detected:",
    ]

    if result.reserved_names:
        lines.extend(f"- {name}" for name in result.reserved_names)
    else:
        lines.append("- None detected")

    if debug:
        lines.extend([
            "",
            f"Panel box: {result.panel_box}",
            f"Header OCR: {result.header_text or '(manual or empty)'}",
            "",
            "Row debug:",
        ])
        for row in result.rows:
            detected = row.name if row.name else "none"
            lines.append(f"Row {row.row}: reserved={row.reserved} name={detected}")
            if row.cleaned_lines:
                for item in row.cleaned_lines:
                    lines.append(f"  - {item}")
            else:
                lines.append("  - no text")

    lines.extend([
        "",
        f"Save: {PREFIX}confirm {scan_id}",
        f"Save and replace that BG: {PREFIX}confirm {scan_id} replace",
        f"Reject: {PREFIX}reject {scan_id}",
    ])
    return "\n".join(lines)


async def cmd_confirm(message: discord.Message, args_text: str):
    parts = args_text.split()
    if not parts:
        await message.reply("Use: !confirm SCANID")
        return

    scan_id = parts[0].upper()
    replace = any(part.lower() == "replace" for part in parts[1:])
    scan = pending_scans.get(scan_id)
    if not scan:
        await message.reply("That scan ID is not pending.")
        return

    bg = scan.get("battlegroup")
    names = scan.get("reserved_names") or []
    if bg is None:
        await message.reply("Battlegroup was not detected. Re-scan with something like !scan bg2.")
        return
    if not names:
        await message.reply("No reserved names were detected, so nothing was saved.")
        return

    save_reservations(int(bg), names, replace=replace)
    pending_scans.pop(scan_id, None)

    mode = "replaced" if replace else "saved"
    await send_code(message.channel, f"BG{bg} {mode}:\n" + "\n".join(f"- {n}" for n in names))
    await log_action(message, f"Confirmed scan {scan_id} for BG{bg} with {len(names)} reserved players.")


async def cmd_reject(message: discord.Message, args_text: str):
    scan_id = args_text.strip().upper()
    if not scan_id:
        await message.reply("Use: !reject SCANID")
        return
    if pending_scans.pop(scan_id, None):
        await message.reply(f"Rejected scan {scan_id}.")
    else:
        await message.reply("That scan ID is not pending.")


async def cmd_list(message: discord.Message):
    data = load_data()
    groups = data.get("battlegroups", {})
    if not groups:
        await send_code(message.channel, "No reservations saved yet.")
        return

    lines = []
    for bg_key in sorted(groups, key=lambda x: int(x) if str(x).isdigit() else 999):
        names = groups.get(bg_key, [])
        lines.append(f"BG{bg_key}: {len(names)} reserved")
        for name in names:
            lines.append(f"- {name}")
        lines.append("")
    await send_code(message.channel, "\n".join(lines).strip())


async def cmd_viewbg(message: discord.Message, args_text: str):
    bg = parse_single_bg(args_text)
    if bg is None:
        await message.reply("Use: !viewbg 2")
        return

    names = load_data().get("battlegroups", {}).get(str(bg), [])
    if not names:
        await send_code(message.channel, f"BG{bg}: no reserved players saved.")
        return
    await send_code(message.channel, f"BG{bg}:\n" + "\n".join(f"- {n}" for n in names))


def parse_single_bg(text: str) -> Optional[int]:
    text = text.strip().lower()
    if text.startswith("bg"):
        text = text[2:]
    if text.isdigit():
        number = int(text)
        if 1 <= number <= 3:
            return number
    return None


async def cmd_clearbg(message: discord.Message, args_text: str):
    bg = parse_single_bg(args_text)
    if bg is None:
        await message.reply("Use: !clearbg 2")
        return
    clear_bg(bg)
    await message.reply(f"Cleared BG{bg}.")
    await log_action(message, f"Cleared BG{bg}.")


async def cmd_clear_player(message: discord.Message, args_text: str):
    name = args_text.strip().strip('"')
    if not name:
        await message.reply('Use: !clear "Player Name"')
        return
    changed = remove_player(name)
    await message.reply("Removed player." if changed else "Player was not found.")
    if changed:
        await log_action(message, f"Removed player: {name}")


async def cmd_rename(message: discord.Message, args_text: str):
    try:
        parts = shlex.split(args_text)
    except ValueError:
        parts = []
    if len(parts) < 2:
        await message.reply('Use: !rename "Old Name" "New Name"')
        return
    old, new = parts[0], parts[1]
    changed = rename_player(old, new)
    await message.reply("Renamed player." if changed else "Old name was not found.")
    if changed:
        await log_action(message, f"Renamed player: {old} to {new}")


async def cmd_wipe(message: discord.Message, args_text: str):
    if args_text.strip().lower() != "confirm":
        await message.reply("Use !wipe confirm to delete all saved reservations.")
        return
    wipe_all()
    await message.reply("All saved reservations wiped.")
    await log_action(message, "Wiped all reservation data.")


async def cmd_exportdata(message: discord.Message):
    data = load_data()
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    file = discord.File(io.BytesIO(payload), filename="reservations_backup.json")
    await message.channel.send("Exported reservation data.", file=file)


async def cmd_importdata(message: discord.Message):
    if not message.attachments:
        await message.reply("Attach a JSON backup to import.")
        return

    attachment = message.attachments[0]
    raw = await attachment.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        await message.reply("That attachment is not valid JSON.")
        return

    if not isinstance(data, dict) or not isinstance(data.get("battlegroups"), dict):
        await message.reply("Backup must contain a battlegroups object.")
        return

    save_data(data)
    await message.reply("Imported reservation data.")
    await log_action(message, "Imported reservation data from backup.")


async def cmd_set_channel(message: discord.Message, args_text: str, key: str, label: str):
    channel_id = extract_channel_id(args_text)
    if channel_id is None:
        await message.reply(f"Use: !set{label.replace(' ', '')} CHANNEL_ID")
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            channel = None

    if channel is None:
        await message.reply("I cannot access that channel.")
        return

    config = load_config()
    config[key] = channel_id
    save_config(config)
    await message.reply(f"Set {label} to <#{channel_id}>.")


def extract_channel_id(text: str) -> Optional[int]:
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        return int(digits)
    return None


async def cmd_config(message: discord.Message):
    config = load_config()
    lines = [
        "Current config:",
        f"Log channel: {format_channel(config.get('log_channel_id'))}",
        f"Scan channel: {format_channel(config.get('scan_channel_id'))}",
    ]
    await send_code(message.channel, "\n".join(lines))


def format_channel(channel_id):
    if channel_id:
        return f"<#{channel_id}>"
    return "not set"


async def cmd_help(message: discord.Message):
    text = """
OCR commands
!scan bg2
!scan bg2 debug
!confirm SCANID
!confirm SCANID replace
!reject SCANID

Viewing
!list
!viewbg 2

Data management
!rename "Old Name" "New Name"
!clear "Player Name"
!clearbg 2
!wipe confirm
!exportdata
!importdata

Setup
!setscanchannel CHANNEL_ID
!setlogchannel CHANNEL_ID
!config
""".strip()
    await send_code(message.channel, text)


async def log_action(message: discord.Message, text: str):
    config = load_config()
    channel_id = config.get("log_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return
    await channel.send(f"{message.author} used {message.content}\n{text}")


async def send_code(channel, text: str):
    if len(text) <= MAX_MESSAGE:
        await channel.send(f"```txt\n{text}\n```")
        return

    chunks = []
    current = []
    current_len = 0
    for line in text.splitlines():
        added = len(line) + 1
        if current_len + added > MAX_MESSAGE:
            chunks.append("\n".join(current))
            current = [line]
            current_len = added
        else:
            current.append(line)
            current_len += added
    if current:
        chunks.append("\n".join(current))

    for chunk in chunks:
        await channel.send(f"```txt\n{chunk}\n```")


if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

bot.run(TOKEN)
