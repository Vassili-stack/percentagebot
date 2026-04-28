from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from ocr_parser import parse_battlegroup_screenshot
from storage import (
    CONFIG_PATH,
    RESERVATIONS_PATH,
    load_config,
    load_reservations,
    merge_bg_reservations,
    remove_player,
    rename_player,
    save_config,
    save_reservations,
    wipe_all,
)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Pending scans live in memory. Confirm soon after scanning.
PENDING_SCANS: dict[str, dict[str, Any]] = {}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_image_attachment(att: discord.Attachment) -> bool:
    if att.content_type and att.content_type.startswith("image/"):
        return True
    return Path(att.filename.lower()).suffix in IMAGE_EXTS


async def get_scan_attachments(ctx: commands.Context) -> list[discord.Attachment]:
    attachments = [att for att in ctx.message.attachments if is_image_attachment(att)]
    if attachments:
        return attachments

    if ctx.message.reference and ctx.message.reference.resolved:
        ref = ctx.message.reference.resolved
        if isinstance(ref, discord.Message):
            return [att for att in ref.attachments if is_image_attachment(att)]

    return []


async def log_action(guild: discord.Guild | None, message: str) -> None:
    if guild is None:
        return

    config = load_config()
    channel_id = config.get("log_channel_id")
    if not channel_id:
        return

    channel = guild.get_channel(int(channel_id))
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(message)
        except discord.HTTPException:
            pass


def scan_channel_allowed(ctx: commands.Context) -> bool:
    config = load_config()
    channel_id = config.get("scan_channel_id")
    if not channel_id:
        return True
    return int(channel_id) == ctx.channel.id


def format_scan_result(scan_id: str, result: dict[str, Any], debug: bool = False) -> str:
    bg = result.get("battlegroup")
    names = result.get("reserved", [])

    lines = [
        f"Scan ID: {scan_id}",
        f"Battlegroup: {bg if bg is not None else 'not detected'}",
        "",
        "Reserved detected:",
    ]

    if names:
        lines.extend(f"- {name}" for name in names)
    else:
        lines.append("- None detected")

    lines.extend([
        "",
        f"Header OCR: {result.get('header_ocr', '').strip() or '[empty]'}",
    ])

    if debug or not names:
        row_lines = result.get("row_ocr_lines", [])
        lines.append("")
        lines.append("Row OCR lines:")
        if row_lines:
            lines.extend(f"- {line}" for line in row_lines[:20])
        else:
            lines.append("- [empty]")

    lines.extend([
        "",
        f"Save: !confirm {scan_id}",
        f"Save and replace that BG: !confirm {scan_id} replace",
        f"Reject: !reject {scan_id}",
    ])

    return "```txt\n" + "\n".join(lines) + "\n```"


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} ({bot.user.id if bot.user else 'unknown id'})")


@bot.command(name="scan")
async def scan(ctx: commands.Context, *args: str) -> None:
    """Scan an attached/replied screenshot for battlegroup reservations."""
    if not scan_channel_allowed(ctx):
        await ctx.reply("Scans are restricted to the configured scan channel.", mention_author=False)
        return

    attachments = await get_scan_attachments(ctx)
    if not attachments:
        await ctx.reply("Attach an image to `!scan`, or reply to an image with `!scan`.", mention_author=False)
        return

    debug = any(arg.lower() == "debug" for arg in args)

    for attachment in attachments:
        try:
            image_bytes = await attachment.read()
            result = await asyncio.to_thread(parse_battlegroup_screenshot, image_bytes)
        except Exception as exc:
            await ctx.reply(f"OCR failed: `{type(exc).__name__}: {exc}`", mention_author=False)
            continue

        scan_id = secrets.token_hex(3).upper()
        PENDING_SCANS[scan_id] = {
            "result": result,
            "user_id": ctx.author.id,
            "channel_id": ctx.channel.id,
            "message_id": ctx.message.id,
            "attachment": attachment.filename,
            "created_at": utc_now_iso(),
        }

        await ctx.send(format_scan_result(scan_id, result, debug=debug))


@bot.command(name="confirm")
async def confirm(ctx: commands.Context, scan_id: str | None = None, mode: str | None = None) -> None:
    if not scan_id:
        await ctx.reply("Use `!confirm [scan_id]` or `!confirm [scan_id] replace`.", mention_author=False)
        return

    scan_id = scan_id.upper()
    pending = PENDING_SCANS.get(scan_id)
    if not pending:
        await ctx.reply("No pending scan with that ID.", mention_author=False)
        return

    result = pending["result"]
    bg = result.get("battlegroup")
    names = result.get("reserved", [])

    if bg is None:
        await ctx.reply("Cannot save: battlegroup was not detected.", mention_author=False)
        return

    if not names:
        await ctx.reply("Cannot save: no reserved players were detected.", mention_author=False)
        return

    replace = bool(mode and mode.lower() == "replace")
    meta = {
        "confirmed_by": str(ctx.author),
        "confirmed_at": utc_now_iso(),
        "source_message_id": str(pending.get("message_id")),
        "source_attachment": pending.get("attachment"),
    }

    merge_bg_reservations(int(bg), names, replace=replace, meta=meta)
    del PENDING_SCANS[scan_id]

    action = "replaced" if replace else "saved"
    await ctx.reply(f"BG{bg} reservations {action}: {', '.join(names)}", mention_author=False)
    await log_action(ctx.guild, f"`{ctx.author}` confirmed scan `{scan_id}` for BG{bg}: {', '.join(names)}")


@bot.command(name="reject")
async def reject(ctx: commands.Context, scan_id: str | None = None) -> None:
    if not scan_id:
        await ctx.reply("Use `!reject [scan_id]`.", mention_author=False)
        return

    scan_id = scan_id.upper()
    if PENDING_SCANS.pop(scan_id, None):
        await ctx.reply(f"Rejected scan `{scan_id}`.", mention_author=False)
    else:
        await ctx.reply("No pending scan with that ID.", mention_author=False)


@bot.command(name="list")
async def list_all(ctx: commands.Context) -> None:
    data = load_reservations()
    bgs = data.get("battlegroups", {})

    if not bgs:
        await ctx.reply("No reservations saved.", mention_author=False)
        return

    lines: list[str] = []
    for bg_key in sorted(bgs.keys(), key=lambda value: int(value) if value.isdigit() else value):
        names = bgs[bg_key].get("reserved", [])
        lines.append(f"Battlegroup {bg_key}:")
        if names:
            lines.extend(f"- {name}" for name in names)
        else:
            lines.append("- None")
        lines.append("")

    await ctx.send("```txt\n" + "\n".join(lines).strip() + "\n```")


@bot.command(name="viewbg")
async def view_bg(ctx: commands.Context, bg: str | None = None) -> None:
    if not bg:
        await ctx.reply("Use `!viewbg [number]`.", mention_author=False)
        return

    data = load_reservations()
    bg_data = data.get("battlegroups", {}).get(str(bg))
    if not bg_data:
        await ctx.reply(f"No saved reservations for BG{bg}.", mention_author=False)
        return

    names = bg_data.get("reserved", [])
    lines = [f"Battlegroup {bg}:"]
    lines.extend(f"- {name}" for name in names) if names else lines.append("- None")
    await ctx.send("```txt\n" + "\n".join(lines) + "\n```")


@bot.command(name="rename")
async def rename(ctx: commands.Context, old_name: str | None = None, *, new_name: str | None = None) -> None:
    if not old_name or not new_name:
        await ctx.reply("Use `!rename [old] [new]`. For names with spaces, quote the old name: `!rename \"old name\" new name`.", mention_author=False)
        return

    changed = rename_player(old_name, new_name)
    await ctx.reply(f"Renamed {changed} matching entr{'y' if changed == 1 else 'ies'}.", mention_author=False)
    await log_action(ctx.guild, f"`{ctx.author}` renamed `{old_name}` to `{new_name}`")


@bot.command(name="clear")
async def clear(ctx: commands.Context, *, player_name: str | None = None) -> None:
    if not player_name:
        await ctx.reply("Use `!clear [player]`.", mention_author=False)
        return

    removed = remove_player(player_name)
    await ctx.reply(f"Removed {removed} matching entr{'y' if removed == 1 else 'ies'} for `{player_name}`.", mention_author=False)
    await log_action(ctx.guild, f"`{ctx.author}` cleared `{player_name}`")


@bot.command(name="wipe")
async def wipe(ctx: commands.Context, confirmation: str | None = None) -> None:
    if confirmation != "CONFIRM":
        await ctx.reply("This clears all saved reservations. Use `!wipe CONFIRM`.", mention_author=False)
        return

    wipe_all()
    await ctx.reply("All saved reservations wiped.", mention_author=False)
    await log_action(ctx.guild, f"`{ctx.author}` wiped all reservations")


@bot.command(name="exportdata")
async def export_data(ctx: commands.Context) -> None:
    if not RESERVATIONS_PATH.exists():
        save_reservations({"battlegroups": {}})
    await ctx.reply(file=discord.File(str(RESERVATIONS_PATH), filename="reservations.json"), mention_author=False)


@bot.command(name="importdata")
async def import_data(ctx: commands.Context) -> None:
    attachments = ctx.message.attachments
    if not attachments:
        await ctx.reply("Attach a `reservations.json` file to `!importdata`.", mention_author=False)
        return

    attachment = attachments[0]
    try:
        raw = await attachment.read()
        import json
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or "battlegroups" not in data:
            raise ValueError("JSON must contain a battlegroups object.")
        save_reservations(data)
    except Exception as exc:
        await ctx.reply(f"Import failed: `{type(exc).__name__}: {exc}`", mention_author=False)
        return

    await ctx.reply("Imported reservations data.", mention_author=False)
    await log_action(ctx.guild, f"`{ctx.author}` imported reservations data")


@bot.command(name="setscanchannel")
@commands.has_permissions(manage_guild=True)
async def set_scan_channel(ctx: commands.Context, channel_id: int | None = None) -> None:
    if channel_id is None:
        await ctx.reply("Use `!setscanchannel [channel_id]`.", mention_author=False)
        return

    channel = ctx.guild.get_channel(channel_id) if ctx.guild else None
    if not isinstance(channel, discord.TextChannel):
        await ctx.reply("I cannot access that text channel.", mention_author=False)
        return

    config = load_config()
    config["scan_channel_id"] = channel_id
    save_config(config)
    await ctx.reply(f"Scan channel set to {channel.mention}.", mention_author=False)


@bot.command(name="setlogchannel")
@commands.has_permissions(manage_guild=True)
async def set_log_channel(ctx: commands.Context, channel_id: int | None = None) -> None:
    if channel_id is None:
        await ctx.reply("Use `!setlogchannel [channel_id]`.", mention_author=False)
        return

    channel = ctx.guild.get_channel(channel_id) if ctx.guild else None
    if not isinstance(channel, discord.TextChannel):
        await ctx.reply("I cannot access that text channel.", mention_author=False)
        return

    config = load_config()
    config["log_channel_id"] = channel_id
    save_config(config)
    await ctx.reply(f"Log channel set to {channel.mention}.", mention_author=False)


@bot.command(name="clearscanchannel")
@commands.has_permissions(manage_guild=True)
async def clear_scan_channel(ctx: commands.Context) -> None:
    config = load_config()
    config.pop("scan_channel_id", None)
    save_config(config)
    await ctx.reply("Scan channel restriction cleared.", mention_author=False)


@bot.command(name="help")
async def help_cmd(ctx: commands.Context) -> None:
    await ctx.send(
        "```txt\n"
        "OCR Reservation Bot\n\n"
        "Scanning:\n"
        "!scan                 Scan an attached image\n"
        "!scan debug           Scan and show row OCR lines\n"
        "!confirm [id]         Save detected reservations\n"
        "!confirm [id] replace Save and replace that battlegroup\n"
        "!reject [id]          Discard pending scan\n\n"
        "Viewing:\n"
        "!list                 Show all saved reservations\n"
        "!viewbg [number]      Show one battlegroup\n\n"
        "Data management:\n"
        "!rename [old] [new]   Rename a saved player\n"
        "!clear [player]       Remove a saved player from all BGs\n"
        "!wipe CONFIRM         Clear all data\n"
        "!exportdata           Export reservations.json\n"
        "!importdata           Import attached reservations.json\n\n"
        "Setup:\n"
        "!setscanchannel [id]  Restrict scans to one channel\n"
        "!clearscanchannel     Remove scan-channel restriction\n"
        "!setlogchannel [id]   Set admin log channel\n"
        "```"
    )


@scan.error
@confirm.error
@reject.error
@list_all.error
@view_bg.error
@rename.error
@clear.error
@wipe.error
@export_data.error
@import_data.error
@set_scan_channel.error
@set_log_channel.error
@clear_scan_channel.error
async def command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("You do not have permission to use that command.", mention_author=False)
        return
    await ctx.reply(f"Command error: `{type(error).__name__}: {error}`", mention_author=False)


bot.run(TOKEN)
