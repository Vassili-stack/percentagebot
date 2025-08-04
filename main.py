import discord
from discord.ext import commands
import re
import json
import asyncio
import os

BASE_PATH = "/data" if os.getenv("FLY_APP_NAME") else "."

LOG_FILE = os.path.join(BASE_PATH, "log_channel.json")
LIMITS_FILE = os.path.join(BASE_PATH, "limits.json")
DATA_FILE = os.path.join(BASE_PATH, "assignments.json")


LOG_CHANNEL_ID = None
LOG_FILE = "log_channel.json"
if os.path.exists(LOG_FILE):
    try:
        with open(LOG_FILE, "r") as f:
            LOG_CHANNEL_ID = json.load(f).get("id")
    except json.JSONDecodeError:
        LOG_CHANNEL_ID = None

YOUR_DEV_IDS = [670782330352435201]  #Your Discord ID

# Intents and bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Configurable limits
LIMITS_FILE = "limits.json"


def load_limits():
    if os.path.exists(LIMITS_FILE):
        try:
            with open(LIMITS_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print("⚠️ Failed to load limits.json. Using default limits.")
    return {"a": 0.3, "b": 1, "c": 1, "d": 1}


limits = load_limits()
DATA_FILE = "assignments.json"

# In-memory storage
user_recent_result = {}
player_assignments = {}

# Load persisted data
if os.path.exists(DATA_FILE):
    try:
        with open(DATA_FILE, "r") as f:
            player_assignments = json.load(f)
            required_keys = {"percent", "a", "b", "c", "d", "note"}
            player_assignments = {
                k: v
                for k, v in player_assignments.items()
                if isinstance(v, dict) and required_keys.issubset(v)
            }
    except json.JSONDecodeError:
        print("⚠️ Warning: Failed to load JSON. Starting fresh.")
        player_assignments = {}


# Save helper
def save_data():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(player_assignments, f, indent=2)
    except IOError as e:
        print(f"Failed to save data: {e}")


# Reverse percent to a/b/c/d breakdown
def reverse_engineer(percent, overrides={}):
    total = percent / 100 * 3.3

    # Step 1: Resolve A
    a = float(overrides["a"]) if "a" in overrides else min(
        limits["a"], total * 0.2)
    rem = total - a

    # Step 2: Resolve B
    b = float(overrides["b"]) if "b" in overrides else min(
        limits["b"], rem * 0.33)
    rem -= b

    # Step 3: Resolve C
    c = float(overrides["c"]) if "c" in overrides else min(
        limits["c"], rem * 0.5
    )  # updated: use 0.5 instead of 0.33 to prioritize earlier variables
    rem -= c

    # Step 4: Resolve D
    d = float(overrides["d"]) if "d" in overrides else min(
        limits["d"], max(0, rem))

    return round(a, 3), round(b, 3), round(c, 3), round(d, 3)


@bot.command()
@commands.has_permissions(administrator=True)
async def calculate(ctx, *, input_str: str = None):
    if not input_str or input_str.strip() == "":
        return await ctx.send(
            "❌ Please provide input in the form `a=... b=... c=... d=...`.")

    # Match floats including `.5`, `1.`, `2`, `-0.3`
    matches = re.findall(r"(\w+)\s*=\s*(-?\d*\.?\d+)", input_str)
    if not matches:
        return await ctx.send("❌ Format error. Use: a=... b=... c=... d=...")

    # Detect duplicates
    seen = set()
    duplicates = [k for k, _ in matches if k in seen or seen.add(k)]
    if duplicates:
        return await ctx.send(f"❌ Duplicate keys: {', '.join(duplicates)}")

    try:
        variables = {k.lower(): float(v) for k, v in matches}
    except ValueError:
        return await ctx.send("❌ Invalid number detected.")

    required = {"a", "b", "c", "d"}
    if not required.issubset(variables):
        missing = required - variables.keys()
        return await ctx.send(f"❌ Missing: {', '.join(missing)}")

    extra_keys = set(variables.keys()) - required
    if extra_keys:
        return await ctx.send(f"❌ Unexpected keys: {', '.join(extra_keys)}")

    a, b, c, d = variables["a"], variables["b"], variables["c"], variables["d"]
    errors = []

    if not (0 <= a <= limits['a']):
        errors.append(f"a must be in [0, {limits['a']}]")
    for var in ['b', 'c', 'd']:
        if not (0 <= variables[var] <= limits[var]):
            errors.append(f"{var} must be in [0, {limits[var]}]")

    if errors:
        return await ctx.send("⚠️ Input Errors:\n" + "\n".join(errors))

    result = (a + b + c + d) / 3.3
    percent = result * 100
    user_recent_result[ctx.author.id] = {
        "percent": percent,
        "a": a,
        "b": b,
        "c": c,
        "d": d
    }
    await ctx.send(f"✅ Valid input. Score: {percent:.2f}%")


@bot.command()
@commands.has_permissions(administrator=True)
@commands.cooldown(1, 5, commands.BucketType.user)
async def assign(ctx, *args):
    if len(args) == 1:
        player = args[0].lower()
        data = user_recent_result.get(ctx.author.id)
        if not data:
            return await ctx.send(
                "❌ No recent result found. Use !calculate first.")
        # This came from direct input, so no note
        player_assignments[player] = {**data}
        save_data()
        await log_admin_action(
            ctx,
            f"📌 Assigned {data['percent']:.2f}% to `{player}` from `!calculate` result."
        )
        return await ctx.send(f"📌 Assigned {data['percent']:.2f}% to {player}."
                              )

    elif len(args) >= 2:
        try:
            percent = float(args[0])
            if not (0 <= percent <= 100):
                return await ctx.send("❌ Percentage must be 0–100.")

            player = args[1].lower()
            arg_str = " ".join(args[2:])

            if arg_str.strip():
                override_matches = re.findall(r"(\w+)\s*=\s*(-?\d+(?:\.\d+)?)",
                                              arg_str)

                if not override_matches:
                    return await ctx.send(
                        "❌ Invalid override format. Use a=... b=... etc.")

                # Detect duplicate keys
                keys_seen = set()
                duplicates = [
                    k for k, _ in override_matches
                    if k.lower() in keys_seen or keys_seen.add(k.lower())
                ]
                if duplicates:
                    return await ctx.send(
                        f"❌ Duplicate override keys: {', '.join(duplicates)}")

                valid_keys = {"a", "b", "c", "d"}
                overrides = {}
                invalid_keys = []

                for raw_k, raw_v in override_matches:
                    k = raw_k.lower()
                    if k not in valid_keys:
                        invalid_keys.append(raw_k)
                        continue
                    val = float(raw_v)
                    if not (0 <= val <= limits[k]):
                        return await ctx.send(
                            f"❌ {k} must be in [0, {limits[k]}]")
                    overrides[k] = val

                if invalid_keys:
                    return await ctx.send(
                        f"❌ Invalid override keys: {', '.join(invalid_keys)}. Allowed: a, b, c, d"
                    )
            else:
                overrides = {}

            a, b, c, d = reverse_engineer(percent, overrides)
            player_assignments[player] = {
                "percent": percent,
                "a": a,
                "b": b,
                "c": c,
                "d": d,
                "note": "overridden" if overrides else "approx"
            }
            save_data()
            note = "ℹ️ Some values were overridden." if overrides else "ℹ️ Approximated breakdown."

            await log_admin_action(
                ctx,
                f"📌 Assigned {percent:.2f}% to `{player}` with {'overrides' if overrides else 'no overrides'}."
            )
            return await ctx.send(
                f"📌 Assigned {percent:.2f}% to {player}.\n{note}")

        except ValueError:
            return await ctx.send(
                "❌ Use: !assign [percent] [player] [optional overrides]")

    else:
        return await ctx.send(
            "❌ Use !assign [player] or !assign [value] [player] [optional overrides]"
        )


@bot.command()
async def view(ctx, player: str = None):
    if not player or player.strip() == "":
        return await ctx.send("❌ Please specify a player name.")

    key = player.lower()
    data = player_assignments.get(key)
    if not data:
        return await ctx.send(f"❌ No data found for {player}.")

    percent = data['percent']
    color = discord.Color.green() if percent > 80 else discord.Color.orange(
    ) if percent > 60 else discord.Color.red()

    embed = discord.Embed(
        title=f"📋 {player.title()} \n📊 Performance: {percent:.2f}%",
        color=color)
    embed.add_field(name="Roster (a)",
                    value=f"{data['a']} / {limits['a']}",
                    inline=True)
    embed.add_field(name="Ingame (b)",
                    value=f"{data['b']} / {limits['b']}",
                    inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="Discord (c)",
                    value=f"{data['c']} / {limits['c']}",
                    inline=True)
    embed.add_field(name="Game Sense (d)",
                    value=f"{data['d']} / {limits['d']}",
                    inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    # ✅ New: Show custom comment/note if present
    comment = data.get("comment")
    if comment:
        embed.add_field(name="🗒️ Note", value=comment, inline=False)

    note = data.get("note")
    if note == "overridden":
        embed.set_footer(text="Some values were manually overridden.")
    elif note == "approx":
        embed.set_footer(text="Values were auto-estimated based on percent.")

    await ctx.send(embed=embed)


@bot.command()
async def top(ctx):
    if not player_assignments:
        return await ctx.send("📭 No player data available.")

    sorted_players = sorted(player_assignments.items(),
                            key=lambda x: x[1].get("percent", 0),
                            reverse=True)

    embed = discord.Embed(title="🏆 Top Players", color=discord.Color.gold())
    required_keys = {"a", "b", "c", "d", "percent"}

    for i, (player, data) in enumerate(sorted_players[:10], start=1):
        if not required_keys.issubset(data):
            continue  # skip broken data entries

        note = data.get("note")
        note_text = "📌 Overridden" if note == "overridden" else (
            "🔧 Approximated" if note == "approx" else "")

        full_text = (
            f"a: {data['a']}   b: {data['b']}   c: {data['c']}   d: {data['d']}"
        )
        if note_text:
            full_text += f"\n{note_text}"

        comment = data.get("comment")
        if comment:
            full_text += f"\n💬 {comment}"

        embed.add_field(name=f"{i}. {player.title()} ({data['percent']:.2f}%)",
                        value=full_text,
                        inline=False)

    await ctx.send(embed=embed)


@bot.command()
async def helpme(ctx):
    embed = discord.Embed(title="📘 Command List",
                          description="All commands, organized by purpose",
                          color=discord.Color.blue())

    # ── Calculation & Assignment ──
    embed.add_field(name="**────────── Calculation & Assignment ──────────**",
                    value="\u200b",
                    inline=False)

    embed.add_field(
        name="🔹 !calculate a=[val] b=[val] c=[val] d=[val]",
        value=
        "Calculate performance % from manual input.\n→ Must include all variables.",
        inline=False)

    embed.add_field(name="🔹 !assign [player]",
                    value="Assigns your last calculated result to a player.",
                    inline=False)

    embed.add_field(
        name="🔹 !assign [percent] [player] [optional a= b= ...]",
        value=
        "Manually assigns a score to a player, with optional breakdown override.",
        inline=False)

    embed.add_field(
        name="🔹 !adjust [player] [a|b|c|d]=[val]",
        value="Modifies one variable and auto-recalculates performance.",
        inline=False)

    embed.add_field(
        name="🔹 !recent",
        value="Shows your most recent calculated result (used for `!assign`).",
        inline=False)

    embed.add_field(
        name="🔹 !preview [percent] [optional a= b= ...]",
        value="Preview the breakdown that would be assigned, without saving.",
        inline=False)

    # ── Viewing ──
    embed.add_field(name="**────────────── Viewing ──────────────**",
                    value="\u200b",
                    inline=False)

    embed.add_field(
        name="🔹 !view [player]",
        value="Shows detailed stats and breakdown for that player.",
        inline=False)

    embed.add_field(name="🔹 !top",
                    value="Displays the top 10 players by performance score.",
                    inline=False)

    embed.add_field(
        name="🔹 !note [player] [message]",
        value="Adds a human-written comment to the player's view card.",
        inline=False)

    embed.add_field(name="🔹 !list",
                    value="Lists all tracked players in the database.",
                    inline=False)

    # ── Data Management ──
    embed.add_field(name="**─────────── Data Management ───────────**",
                    value="\u200b",
                    inline=False)

    embed.add_field(
        name="🔹 !rename [old] [new]",
        value="Renames a player entry without reassigning or resetting values.",
        inline=False)

    embed.add_field(name="🔹 !clear [player]",
                    value="Deletes one player’s data entry.",
                    inline=False)

    embed.add_field(name="🔹 !wipe",
                    value="⚠️ Clears **ALL** player data after confirmation.",
                    inline=False)

    embed.add_field(name="🔹 !exportdata",
                    value="Exports all current data to `backup.json`.",
                    inline=False)

    embed.add_field(
        name="🔹 !importdata",
        value=
        "⚠️ Imports data from `backup.json`, overwriting existing records.",
        inline=False)

    embed.add_field(name="🔹 !clearnote [player]",
                    value="Removes any comment/note attached to a player.",
                    inline=False)

    # ── Setup ──
    embed.add_field(name="**──────────────── Setup ────────────────**",
                    value="\u200b",
                    inline=False)

    embed.add_field(
        name="🔹 !setlogchannel [channel_id]",
        value=
        "Sets where admin actions (assign, adjust, clear, etc.) are logged.",
        inline=False)

    embed.add_field(name="🔹 !setcap a=...` (a–d only)",
                    value="Sets where the variable caps for a-d respectively.",
                    inline=False)

    embed.add_field(name="🔹 !viewcaps",
                    value="Displays current caps",
                    inline=False)

    # ── Variable Limits ──
    embed.add_field(name="**──────────── Variable Limits ────────────**",
                    value="a ∈ [0, 0.3]   |   b, c, d ∈ [0, 1]",
                    inline=False)

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def adjust(ctx, player: str, *, override: str):
    key = player.lower()
    if key not in player_assignments:
        return await ctx.send(f"❌ No data found for {player}.")

    match = re.match(r"([a-dA-D])\s*=\s*([\d.]+)", override)
    if not match:
        return await ctx.send("❌ Format: !adjust [player] [a|b|c|d]=[value]")

    var, val = match.group(1).lower(), float(match.group(2))
    if val < 0 or val > limits[var]:
        return await ctx.send(f"❌ {var} must be in [0, {limits[var]}]")

    # Update the variable
    player_assignments[key][var] = round(val, 3)

    # Recalculate percentage
    a = player_assignments[key]["a"]
    b = player_assignments[key]["b"]
    c = player_assignments[key]["c"]
    d = player_assignments[key]["d"]
    new_percent = ((a + b + c + d) / 3.3) * 100
    player_assignments[key]["percent"] = round(new_percent, 2)

    # Preserve override status if already present; otherwise fallback to approx
    existing_note = player_assignments[key].get("note")
    if existing_note == "overridden":
        player_assignments[key]["note"] = "overridden"
    else:
        player_assignments[key]["note"] = "approx"

    save_data()

    await log_admin_action(
        ctx,
        f"🔧 Adjusted `{player}`: set `{var}` = `{val}` → new % = `{new_percent:.2f}`"
    )

    await ctx.send(
        f"🔧 Updated {player}'s `{var}` to `{val}`. New score: `{new_percent:.2f}%`."
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def clear(ctx, player: str):
    key = player.lower()
    if key not in player_assignments:
        return await ctx.send(f"❌ No data found for {player}.")

    del player_assignments[key]
    save_data()

    await log_admin_action(ctx, f"🗑️ Cleared all data for `{player}`.")

    await ctx.send(f"🗑️ Cleared data for {player}.")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send(
            "🚫 You do not have permission to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send("❌ Missing required argument.")
    elif isinstance(error, commands.CommandOnCooldown):
        return await ctx.send(
            f"⏳ `{ctx.command}` is on cooldown. Try again in {round(error.retry_after, 1)}s."
        )
    elif isinstance(error, commands.BadArgument):
        return await ctx.send("❌ Invalid argument format.")
    elif isinstance(error, commands.CommandNotFound):
        return  # ignore silently
    else:
        print(f"Unhandled error: {error}")
        raise error  # still raise to keep traceback


@assign.error
async def assign_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        if ctx.author.id in YOUR_DEV_IDS:
            await ctx.reinvoke()
        else:
            await ctx.send(
                f"⏳ `!assign` is on cooldown. Try again in {round(error.retry_after, 1)}s."
            )


@bot.command()
@commands.has_permissions(administrator=True)
async def exportdata(ctx):
    with open("backup.json", "w") as f:
        json.dump(player_assignments, f, indent=2)
    await ctx.send(file=discord.File("backup.json"))


@bot.command()
@commands.has_permissions(administrator=True)
async def rename(ctx, old: str, new: str):
    old_key, new_key = old.lower(), new.lower()
    if old_key not in player_assignments:
        return await ctx.send(f"❌ No data found for {old}.")
    if new_key in player_assignments:
        return await ctx.send(f"❌ {new} already exists.")

    player_assignments[new_key] = player_assignments.pop(old_key)
    save_data()

    await log_admin_action(ctx, f"🔁 Renamed `{old}` → `{new}`.")

    await ctx.send(f"🔁 Renamed {old} → {new}.")


@bot.command()
async def recent(ctx):
    data = user_recent_result.get(ctx.author.id)
    if not data:
        return await ctx.send("❌ No recent result found.")
    await ctx.send(
        f"🧾 Your last result: {data['percent']:.2f}%\n"
        f"a: {data['a']}  b: {data['b']}  c: {data['c']}  d: {data['d']}")


@bot.command()
@commands.has_permissions(administrator=True)
async def note(ctx, player: str, *, message: str = None):
    key = player.lower()
    if key not in player_assignments:
        return await ctx.send(f"❌ No data found for {player}.")
    if not message or message.strip() == "":
        return await ctx.send("❌ Please provide a message for the note.")

    trimmed = message.strip()
    if len(trimmed) > 300:
        trimmed = trimmed[:300]
        await ctx.send("⚠️ Note was too long. Truncated to 300 characters.")

    player_assignments[key]["comment"] = trimmed
    save_data()

    await ctx.send(f"📝 Note added to {player}: “{trimmed}”")
    await log_admin_action(
        ctx, f"📝 `{ctx.author}` added note to `{player}`: {trimmed}")


@bot.command()
@commands.has_permissions(administrator=True)
async def wipe(ctx):

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    await ctx.send(
        "⚠️ Are you **sure** you want to wipe **all** player data? Type `CONFIRM` to proceed or `CANCEL` to abort."
    )

    try:
        reply = await bot.wait_for("message", timeout=20.0, check=check)
        if reply.content.strip().upper() == "CONFIRM":
            player_assignments.clear()
            save_data()
            await ctx.send("🧹 All player data wiped successfully.")
        else:
            await ctx.send("❌ Wipe operation canceled.")
    except asyncio.TimeoutError:
        await ctx.send("⏳ Timed out. Wipe canceled.")


@bot.command()
@commands.has_permissions(administrator=True)
async def setlogchannel(ctx, channel_id: int):
    channel = bot.get_channel(channel_id)
    if not channel:
        return await ctx.send(
            "❌ Invalid channel ID or bot cannot see this channel.")
    try:
        await channel.send("✅ Log channel set successfully.")
    except discord.Forbidden:
        return await ctx.send(
            "❌ I don't have permission to send messages to that channel.")

    global LOG_CHANNEL_ID
    LOG_CHANNEL_ID = channel_id
    with open(LOG_FILE, "w") as f:
        json.dump({"id": channel_id}, f)
    await ctx.send(f"📌 Log channel set to <#{channel_id}>.")


async def log_admin_action(ctx, action: str):
    if LOG_CHANNEL_ID is None:
        return
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if not channel:
        return
    try:
        embed = discord.Embed(
            title="🛠️ Admin Action Logged",
            description=
            f"**{ctx.author}** used `{ctx.command}`\n\n**Action:** {action}",
            color=discord.Color.dark_gray())
        embed.set_footer(text=f"User ID: {ctx.author.id} • {ctx.command}")
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass  # Silently fail if bot loses permission


@bot.command()
@commands.has_permissions(administrator=True)
async def importdata(ctx):
    await ctx.send(
        "⚠️ This will **overwrite** all current player data with data from `backup.json`.\nType `CONFIRM` to proceed or `CANCEL` to abort."
    )

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        reply = await bot.wait_for("message", timeout=20.0, check=check)
        if reply.content.strip().upper() != "CONFIRM":
            return await ctx.send("❌ Import operation canceled.")

        with open("backup.json", "r") as f:
            data = json.load(f)

        required_keys = {"percent", "a", "b", "c", "d", "note"}
        valid_data = {
            k: v
            for k, v in data.items()
            if isinstance(v, dict) and required_keys.issubset(v)
        }

        if not valid_data:
            return await ctx.send("❌ No valid player data found in backup.")

        player_assignments.clear()
        player_assignments.update(valid_data)
        save_data()

        await ctx.send(f"📥 Imported {len(valid_data)} players from backup.")
        await log_admin_action(
            ctx, f"📥 Imported {len(valid_data)} players from backup.json.")

    except FileNotFoundError:
        await ctx.send("❌ `backup.json` not found.")
    except json.JSONDecodeError:
        await ctx.send("❌ Failed to decode `backup.json` — invalid JSON.")
    except TimeoutError:
        await ctx.send("⏳ Timed out. Import canceled.")
    except Exception as e:
        await ctx.send("❌ An unexpected error occurred during import.")
        print(f"Import error: {e}")


@bot.command()
@commands.has_permissions(administrator=True)
async def clearnote(ctx, player: str):
    key = player.lower()
    if key not in player_assignments:
        return await ctx.send(f"❌ No data found for {player}.")

    if "comment" not in player_assignments[key]:
        return await ctx.send(f"ℹ️ {player} has no note to clear.")

    del player_assignments[key]["comment"]
    save_data()
    await ctx.send(f"🧽 Cleared note for {player}.")
    await log_admin_action(ctx, f"🧽 Cleared note for `{player}`.")


@bot.command()
async def list(ctx):
    if not player_assignments:
        return await ctx.send("📭 No players found.")

    players = sorted(player_assignments.keys())
    chunk = ", ".join(player.title() for player in players)
    embed = discord.Embed(title="🧾 Player List",
                          description=chunk,
                          color=discord.Color.blue())
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def preview(ctx, percent: float, *, override_str: str = ""):
    if not (0 <= percent <= 100):
        return await ctx.send("❌ Percent must be between 0 and 100.")

    override_matches = re.findall(r"(\w+)\s*=\s*(-?\d+(?:\.\d+)?)",
                                  override_str)
    overrides = {}

    if override_matches:
        keys_seen = set()
        for k, v in override_matches:
            k_lower = k.lower()
            if k_lower in keys_seen:
                return await ctx.send(f"❌ Duplicate override key: `{k}`")
            if k_lower not in {"a", "b", "c", "d"}:
                return await ctx.send(f"❌ Invalid override key: `{k}`")
            val = float(v)
            if not (0 <= val <= limits[k_lower]):
                return await ctx.send(
                    f"❌ {k_lower} must be in [0, {limits[k_lower]}]")
            overrides[k_lower] = val
            keys_seen.add(k_lower)

    a, b, c, d = reverse_engineer(percent, overrides)
    embed = discord.Embed(
        title="🧮 Preview Breakdown",
        description=
        f"For `{percent:.2f}%` with {'overrides' if overrides else 'no overrides'}",
        color=discord.Color.purple())
    embed.add_field(name="Roster (a)",
                    value=f"{a} / {limits['a']}",
                    inline=True)
    embed.add_field(name="Ingame (b)",
                    value=f"{b} / {limits['b']}",
                    inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="Discord (c)",
                    value=f"{c} / {limits['c']}",
                    inline=True)
    embed.add_field(name="Game Sense (d)",
                    value=f"{d} / {limits['d']}",
                    inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def setcap(ctx, *, arg: str):
    match = re.match(r"([a-dA-D])\s*=\s*([\d.]+)", arg)
    if not match:
        return await ctx.send("❌ Format: !setcap [a|b|c|d]=[value]")

    var, val = match.group(1).lower(), float(match.group(2))
    if not (0 <= val <= 10):  # Arbitrary max, for safety
        return await ctx.send("❌ Value must be between 0 and 10.")

    limits[var] = round(val, 3)

    with open(LIMITS_FILE, "w") as f:
        json.dump(limits, f, indent=2)

    await ctx.send(f"📐 Limit for `{var}` updated to `{val}`.")


@bot.command()
async def viewcaps(ctx):
    limits = load_limits()
    formatted = "\n".join(f"{k} ∈ [0, {v}]" for k, v in limits.items())
    await ctx.send(f"📐 Current Variable Caps:\n{formatted}")


@bot.command()
async def john(ctx):
    await ctx.send("gotti")


@bot.command()
async def niko(ctx):
    await ctx.send("needs a job")


# Startup


token = os.getenv("BOT_TOKEN")
if not token:
    raise EnvironmentError("BOT_TOKEN environment variable not set.")

bot.run(token)
