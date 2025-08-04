import discord
from discord.ext import commands
from discord.ui import View, Button
import re
import json
import asyncio
import os





# Limits


# Paths for data files
BASE_PATH = "/data" if os.getenv("FLY_APP_NAME") else "."

LOG_FILE = os.path.join(BASE_PATH, "log_channel.json")
LIMITS_FILE = os.path.join(BASE_PATH, "limits.json")
DATA_FILE = os.path.join(BASE_PATH, "assignments.json")
BACKUP_FILE = os.path.join(BASE_PATH, "backup.json")

def safe_load_json(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default



log_data = safe_load_json(LOG_FILE, {})
LOG_CHANNEL_ID = log_data.get("id")


YOUR_DEV_IDS = [670782330352435201]  #Your Discord ID

# Intents and bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Mutable wrapper
limits = {}

def load_limits():
    return safe_load_json(LIMITS_FILE, {"a": 0.3, "b": 1, "c": 1, "d": 1})

def reload_limits():
    limits.clear()
    limits.update(load_limits())

# Initial load
reload_limits()


def compute_total_weight():
    return sum(limits.values())


# Data Management
def parse_override_string(input_str, limits):
    matches = re.findall(r"(\w+)\s*=\s*(-?\d+(?:\.\d+)?)", input_str)
    if not matches:
        raise ValueError("Invalid override format. Use a=... b=... etc.")

    overrides = {}
    keys_seen = set()
    valid_keys = {"a", "b", "c", "d"}

    for raw_k, raw_v in matches:
        k = raw_k.lower()
        if k in keys_seen:
            raise ValueError(f"Duplicate override key: {k}")
        if k not in valid_keys:
            raise ValueError(f"Invalid override key: {k}. Allowed: a, b, c, d")

        val = float(raw_v)
        if not (0 <= val <= limits[k]):
            raise ValueError(f"{k} must be in [0, {limits[k]}]")

        overrides[k] = val
        keys_seen.add(k)

    return overrides


# In-memory storage
user_recent_result = {}
player_assignments = {}
undo_stack = {}

# Load persisted data
player_assignments = safe_load_json(DATA_FILE, {})

required_keys = {"percent", "a", "b", "c", "d", "note"}
player_assignments = {
    k: v
    for k, v in player_assignments.items()
    if isinstance(v, dict) and required_keys.issubset(v)
}



# Save helper
def save_data():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(player_assignments, f, indent=2)
    except IOError as e:
        print(f"Failed to save data: {e}")


# Reverse percent to a/b/c/d breakdown
def reverse_engineer(percent, overrides={}):
    if not (0 <= percent <= 100):
        raise ValueError("Percent must be between 0 and 100.")

    total = percent / 100 * compute_total_weight()

    try:
        # Step 1: Resolve A
        a = float(overrides["a"]) if "a" in overrides else min(limits["a"], total * 0.2)
        rem = total - a

        # Step 2: Resolve B
        b = float(overrides["b"]) if "b" in overrides else min(limits["b"], rem * 0.33)
        rem -= b

        # Step 3: Resolve C
        c = float(overrides["c"]) if "c" in overrides else min(limits["c"], rem * 0.5)
        rem -= c

        # Step 4: Resolve D
        d = float(overrides["d"]) if "d" in overrides else min(limits["d"], max(0, rem))
    except KeyError as e:
        raise ValueError(f"Invalid override key: {e.args[0]}")

    # Validate raw (pre-rounding) sum against target
    raw_total = a + b + c + d
    if abs(raw_total - total) > 0.01:
        raise ValueError(
            f"Breakdown total ({raw_total:.4f}) does not match expected total from percent ({total:.4f}). "
            "Likely due to conflicting overrides or weight overflow."
        )

    # Round only for return (public interface)
    a, b, c, d = round(a, 3), round(b, 3), round(c, 3), round(d, 3)
    return a, b, c, d



def parse_all_variables(input_str, limits):
    # Parse input string and ensure all required variables are present
    overrides = parse_override_string(input_str, limits)
    required = {"a", "b", "c", "d"}

    missing = required - overrides.keys()
    if missing:
        raise ValueError(f"Missing: {', '.join(missing)}")

    extra = overrides.keys() - required
    if extra:
        raise ValueError(f"Unexpected keys: {', '.join(extra)}")

    return overrides



class PaginatedView(View):
    def __init__(self, ctx, pages, timeout=60):
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.pages = pages
        self.current = 0
        self.message = None  # to be set externally after sending

        self.prev_button = Button(label="◀️ Prev", style=discord.ButtonStyle.secondary)
        self.next_button = Button(label="Next ▶️", style=discord.ButtonStyle.secondary)
        self.prev_button.callback = self.go_prev
        self.next_button.callback = self.go_next
        self.add_item(self.prev_button)
        self.add_item(self.next_button)

    async def go_prev(self, interaction: discord.Interaction):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("❌ You can't control this pagination.", ephemeral=True)

        self.current = (self.current - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    async def go_next(self, interaction: discord.Interaction):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("❌ You can't control this pagination.", ephemeral=True)

        self.current = (self.current + 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

        try:
            if self.message:
                await self.message.edit(view=self)
        except discord.HTTPException:
            pass  # ignore if message was deleted or can't be edited


@bot.command()
@commands.has_permissions(administrator=True)
async def calculate(ctx, *, input_str: str = None):
    if not input_str or input_str.strip() == "":
        return await ctx.send(
            "❌ Please provide input in the form `a=... b=... c=... d=...`."
        )

    try:
        variables = parse_all_variables(input_str, limits)
    except ValueError as ve:
        return await ctx.send(f"❌ {ve}")

    errors = []
    for k, val in variables.items():
        if not (0 <= val <= limits[k]):
            errors.append(f"{k} must be in [0, {limits[k]}]")

    if errors:
        return await ctx.send("⚠️ Input Errors:\n" + "\n".join(errors))

    a, b, c, d = variables["a"], variables["b"], variables["c"], variables["d"]
    result = (a + b + c + d) / compute_total_weight()
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
            return await ctx.send("❌ No recent result found. Use !calculate first.")

        # ✅ Backup existing assignment
        if player in player_assignments:
            undo_stack[player] = dict(player_assignments[player])

        player_assignments[player] = {**data}
        save_data()

        await log_admin_action(
            ctx,
            f"📌 Assigned {data['percent']:.2f}% to `{player}` from `!calculate` result."
        )
        return await ctx.send(f"📌 Assigned {data['percent']:.2f}% to {player}.")

    elif len(args) >= 2:
        try:
            percent = float(args[0])
            if not (0 <= percent <= 100):
                return await ctx.send("❌ Percentage must be 0–100.")
        except ValueError:
            return await ctx.send("❌ Invalid percent value.")

        player = args[1].lower()
        arg_str = " ".join(args[2:])

        # ✅ Parse and validate overrides
        if arg_str.strip():
            try:
                overrides = parse_override_string(arg_str, limits)
            except ValueError as ve:
                return await ctx.send(f"❌ {ve}")
        else:
            overrides = {}

        # ✅ Compute breakdown and validate total
        try:
            a, b, c, d = reverse_engineer(percent, overrides)
        except ValueError as ve:
            return await ctx.send(f"❌ {ve}")

        # ✅ Backup existing assignment
        if player in player_assignments:
            undo_stack[player] = dict(player_assignments[player])

        player_assignments[player] = {
            "percent": percent,
            "a": a,
            "b": b,
            "c": c,
            "d": d,
            "note": "overridden" if overrides else "approx"
        }
        save_data()

        note_msg = (
            "ℹ️ Some values were overridden."
            if overrides else
            "ℹ️ Approximated breakdown."
        )

        await log_admin_action(
            ctx,
            f"📌 Assigned {percent:.2f}% to `{player}` with {'overrides' if overrides else 'no overrides'}."
        )
        return await ctx.send(f"📌 Assigned {percent:.2f}% to {player}.\n{note_msg}")

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
@commands.has_permissions(administrator=True)
async def undoassign(ctx, player: str):
    key = player.lower()
    if key not in undo_stack:
        return await ctx.send(f"❌ No previous assignment to undo for {player}.")

    player_assignments[key] = undo_stack.pop(key)
    save_data()

    await ctx.send(f"↩️ Reverted last assignment for {player}.")
    await log_admin_action(ctx, f"↩️ Undid last assignment for `{player}`.")


@bot.command()
async def top(ctx):
    if not player_assignments:
        return await ctx.send("📭 No player data available.")

    sorted_players = sorted(
        player_assignments.items(),
        key=lambda x: x[1].get("percent", 0),
        reverse=True
    )

    required_keys = {"a", "b", "c", "d", "percent"}
    pages = []
    per_page = 10
    total = len(sorted_players)

    for start in range(0, total, per_page):
        embed = discord.Embed(title="🏆 Top Players", color=discord.Color.gold())
        end = start + per_page

        for i, (player, data) in enumerate(sorted_players[start:end], start=start + 1):
            if not required_keys.issubset(data):
                continue

            note = data.get("note")
            note_text = (
                "📌 Overridden" if note == "overridden" else
                "🔧 Approximated" if note == "approx" else
                ""
            )

            full_text = f"a: {data['a']}   b: {data['b']}   c: {data['c']}   d: {data['d']}"
            if note_text:
                full_text += f"\n{note_text}"

            comment = data.get("comment")
            if comment:
                full_text += f"\n💬 {comment}"

            embed.add_field(
                name=f"{i}. {player.title()} ({data['percent']:.2f}%)",
                value=full_text,
                inline=False
            )

        embed.set_footer(text=f"Page {start // per_page + 1} of {((total - 1) // per_page) + 1}")
        pages.append(embed)

    view = PaginatedView(ctx, pages)
    view.message = await ctx.send(embed=pages[0], view=view)



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
        name="🔹 !undoassign [player]",
        value="Undoes the last assignment made to a specific player, if a previous one exists.",
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
    formatted_caps = "   |   ".join(f"{k} ∈ [0, {v}]" for k, v in limits.items())
    embed.add_field(name="**──────────── Variable Limits ────────────**",
                    value=formatted_caps,
                    inline=False)


    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def adjust(ctx, player: str, *, override: str):
    key = player.lower()
    if key not in player_assignments:
        return await ctx.send(f"❌ No data found for {player}.")

    try:
        overrides = parse_override_string(override, limits)
    except ValueError as ve:
        return await ctx.send(f"❌ {ve}")

    if len(overrides) != 1:
        return await ctx.send("❌ You can only adjust **one** variable at a time.")

    var, val = next(iter(overrides.items()))

    # Update the variable
    player_assignments[key][var] = round(val, 3)

    # Recalculate percentage
    a = player_assignments[key]["a"]
    b = player_assignments[key]["b"]
    c = player_assignments[key]["c"]
    d = player_assignments[key]["d"]
    new_percent = ((a + b + c + d) / compute_total_weight()) * 100
    player_assignments[key]["percent"] = round(new_percent, 2)

    # Preserve existing note only if it already exists
    if "note" in player_assignments[key]:
        existing_note = player_assignments[key]["note"]
        player_assignments[key]["note"] = existing_note  # redundant, but explicit
    else:
        player_assignments[key].pop("note", None)  # ensure no note field exists

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
        return await ctx.send("🚫 You do not have permission to use this command.")

    elif isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send("❌ Missing required argument.")

    elif isinstance(error, commands.CommandOnCooldown):
        if ctx.author.guild_permissions.administrator:
            await ctx.reinvoke()
        else:
            return await ctx.send(
                f"⏳ `{ctx.command}` is on cooldown. Try again in {round(error.retry_after, 1)}s."
            )

    elif isinstance(error, commands.BadArgument):
        return await ctx.send("❌ Invalid argument format.")

    elif isinstance(error, commands.CommandNotFound):
        return  # silently ignore unknown commands

    else:
        print(f"Unhandled error: {error}")
        raise error  # re-raise for logging/debugging



@assign.error
async def assign_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        if ctx.author.guild_permissions.administrator:
            await ctx.reinvoke()
        else:
            await ctx.send(f"⏳ `!assign` is on cooldown. Try again in {round(error.retry_after, 1)}s.")



@bot.command()
@commands.has_permissions(administrator=True)
async def exportdata(ctx):
    try:
        with open(BACKUP_FILE, "w") as f:
            json.dump(player_assignments, f, indent=2)
        await ctx.send(file=discord.File(BACKUP_FILE))
    except Exception as e:
        await ctx.send("❌ Failed to export backup.")
        print(f"Export error: {e}")


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
    try:
        channel = await bot.fetch_channel(channel_id)
    except discord.NotFound:
        return await ctx.send("❌ Channel not found. Please check the ID.")
    except discord.Forbidden:
        return await ctx.send("❌ I don't have permission to view that channel.")
    except discord.HTTPException:
        return await ctx.send("❌ Failed to fetch the channel due to a Discord API error.")

    if not isinstance(channel, discord.TextChannel):
        return await ctx.send("❌ That ID does not refer to a text channel.")

    try:
        await channel.send("✅ Log channel set successfully.")
    except discord.Forbidden:
        return await ctx.send("❌ I don't have permission to send messages to that channel.")

    global LOG_CHANNEL_ID
    LOG_CHANNEL_ID = channel_id

    try:
        with open(LOG_FILE, "w") as f:
            json.dump({"id": channel_id}, f)
    except IOError as e:
        return await ctx.send(f"❌ Failed to write log file: {e}")

    await ctx.send(f"📌 Log channel set to {channel.mention}.")


async def log_admin_action(ctx, action: str):
    if LOG_CHANNEL_ID is None:
        print("[Log Skipped] LOG_CHANNEL_ID not set.")
        return

    try:
        channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            print(f"[Log Skipped] Channel ID {LOG_CHANNEL_ID} is not a TextChannel.")
            return
    except discord.NotFound:
        print(f"[Log Error] Channel ID {LOG_CHANNEL_ID} not found.")
        return
    except discord.Forbidden:
        print(f"[Log Error] Missing permission to fetch channel ID {LOG_CHANNEL_ID}.")
        return
    except discord.HTTPException as e:
        print(f"[Log Error] Discord API error fetching channel: {e}")
        return

    embed = discord.Embed(
        title="🛠️ Admin Action Logged",
        description=f"**{ctx.author}** used `{ctx.command}`\n\n**Action:** {action}",
        color=discord.Color.dark_gray()
    )
    embed.set_footer(text=f"User ID: {ctx.author.id} • {ctx.command}")

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        print(f"[Log Error] Cannot send messages to channel ID {LOG_CHANNEL_ID}.")


@bot.command()
@commands.has_permissions(administrator=True)
async def importdata(ctx):
    if not ctx.message.attachments:
        return await ctx.send("❌ Please attach a `.json` file containing the backup data.")

    attachment = ctx.message.attachments[0]
    if not attachment.filename.endswith(".json"):
        return await ctx.send("❌ The attached file must be a `.json` file.")

    await ctx.send(
        f"⚠️ This will **overwrite** all current player data with the contents of `{attachment.filename}`.\n"
        "Type `CONFIRM` to proceed or `CANCEL` to abort."
    )

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        reply = await bot.wait_for("message", timeout=20.0, check=check)
        if reply.content.strip().upper() != "CONFIRM":
            return await ctx.send("❌ Import operation canceled.")

        file_bytes = await attachment.read()
        raw_data = json.loads(file_bytes.decode("utf-8"))

        if not isinstance(raw_data, dict):
            return await ctx.send("❌ Backup file format is invalid. Expected a dictionary.")

        required_keys = {"percent", "a", "b", "c", "d", "note"}
        valid_data = {
            k: v for k, v in raw_data.items()
            if isinstance(v, dict) and required_keys.issubset(v)
        }

        if not valid_data:
            return await ctx.send("❌ No valid player entries found in the uploaded file.")

        player_assignments.clear()
        player_assignments.update(valid_data)
        save_data()

        await ctx.send(f"📥 Successfully imported {len(valid_data)} players from file.")
        await log_admin_action(ctx, f"📥 Imported {len(valid_data)} players from `{attachment.filename}`.")

    except asyncio.TimeoutError:
        await ctx.send("⏳ Timed out. Import canceled.")
    except json.JSONDecodeError:
        await ctx.send("❌ The attached file is not valid JSON.")
    except Exception as e:
        await ctx.send("❌ An unexpected error occurred during import.")
        print(f"[IMPORT ERROR] {e}")

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
    per_page = 10
    pages = []

    for start in range(0, len(players), per_page):
        chunk = players[start:start + per_page]
        formatted = "\n".join(f"{i + 1}. {p.title()}" for i, p in enumerate(chunk, start=start))
        embed = discord.Embed(
            title="🧾 Player List",
            description=formatted,
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Page {start // per_page + 1} of {((len(players) - 1) // per_page) + 1}")
        pages.append(embed)

    view = PaginatedView(ctx, pages)
    view.message = await ctx.send(embed=pages[0], view=view)



@bot.command()
@commands.has_permissions(administrator=True)
async def preview(ctx, percent: float, *, override_str: str = ""):
    if not (0 <= percent <= 100):
        return await ctx.send("❌ Percent must be between 0 and 100.")

    try:
        overrides = parse_override_string(override_str, limits)
        a, b, c, d = reverse_engineer(percent, overrides)
    except ValueError as ve:
        return await ctx.send(f"❌ {ve}")

    embed = discord.Embed(
        title="🧮 Preview Breakdown",
        description=(
            f"For `{percent:.2f}%` with {'overrides' if overrides else 'no overrides'}"
        ),
        color=discord.Color.purple()
    )
    embed.add_field(name="Roster (a)", value=f"{a} / {limits['a']}", inline=True)
    embed.add_field(name="Ingame (b)", value=f"{b} / {limits['b']}", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="Discord (c)", value=f"{c} / {limits['c']}", inline=True)
    embed.add_field(name="Game Sense (d)", value=f"{d} / {limits['d']}", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def setcap(ctx, *, arg: str):
    match = re.match(r"^([a-dA-D])\s*=\s*([\d.]+)$", arg.strip())
    if not match:
        return await ctx.send("❌ Format: `!setcap a=...` (a–d only)")

    var = match.group(1).lower()
    try:
        val = float(match.group(2))
    except ValueError:
        return await ctx.send("❌ Value must be a number.")

    if not (0 <= val <= 10):  # Safety upper bound
        return await ctx.send("❌ Value must be between 0 and 10.")

    limits[var] = round(val, 3)

    try:
        with open(LIMITS_FILE, "w") as f:
            json.dump(limits, f, indent=2)
        reload_limits()
    except Exception as e:
        return await ctx.send(f"❌ Failed to update limits file: {e}")

    # ✅ Revalidate all stored players
    violations = []
    for player, data in player_assignments.items():
        if var not in data:
            continue
        original = data[var]
        if original > limits[var]:
            # Truncate the value
            data[var] = round(limits[var], 3)

            # Recalculate percent
            a, b, c, d = data["a"], data["b"], data["c"], data["d"]
            new_percent = ((a + b + c + d) / compute_total_weight()) * 100
            data["percent"] = round(new_percent, 2)

            # Update note to reflect change
            data["note"] = "revalidated"
            violations.append((player, original, limits[var]))

    if violations:
        save_data()

        summary = "\n".join(
            f"🔧 `{p}`: `{var}` capped from {orig} → {new}"
            for p, orig, new in violations[:10]
        )
        if len(violations) > 10:
            summary += f"\n...and {len(violations) - 10} more."

        await ctx.send(
            f"📐 Limit for `{var}` updated to `{val}`.\n"
            f"⚠️ {len(violations)} players exceeded the new cap and were auto-adjusted:\n{summary}"
        )
    else:
        await ctx.send(f"📐 Limit for `{var}` updated to `{val}`.\n✅ No revalidation needed.")




@bot.command()
async def viewcaps(ctx):
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
