# bot.py - Full-featured moderation + utilities bot
# Features: moderation, setup, welcome/goodbye, warnings, timeouts, infractions,
# auto-mod (links/spam/caps), XP & levels, tickets, reaction roles, premium flag,
# interactive /help (buttons). Uses discord.py 2.x (app_commands + ui).
import asyncio
import random


import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import View, Button
from dotenv import load_dotenv
import os, json, re, time, datetime, asyncio, traceback, aiofiles

# ----------------------------------------
# Load environment
# ----------------------------------------
load_dotenv()
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("No TOKEN found in .env. Create .env with TOKEN=your_token_here")

# ----------------------------------------
# Files and persistence helpers
# ----------------------------------------
DATA_FILES = {
    "config": "config.json",
    "warnings": "warnings.json",
    "timeouts": "timeouts.json",
    "xp": "xp.json",
    "reaction": "reaction_panels.json",
    "tickets": "tickets.json"
}

def ensure_file(filename, default):
    if not os.path.exists(filename):
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)

# create defaults if missing
ensure_file(DATA_FILES["config"], {
    "welcome_channel": None,
    "goodbye_channel": None,
    "log_channel": None,
    "filters": {"anti_link": True, "anti_spam": True, "caps_filter": True},
    "level_rewards": {},  # "5": role_id
    "premium_guilds": []   # list of guild ids (as strings) that have premium features enabled
})
for k in ("warnings","timeouts","xp","reaction","tickets"):
    ensure_file(DATA_FILES[k], {})

# async read/write helpers (use aiofiles for safe async writes)
async def load_json(fname):
    async with aiofiles.open(fname, "r", encoding="utf-8") as f:
        text = await f.read()
        return json.loads(text) if text.strip() else {}

async def save_json(fname, data):
    async with aiofiles.open(fname, "w", encoding="utf-8") as f:
        await f.write(json.dumps(data, indent=4))

# synchronous read helpers (used during startup)
def load_json_sync(fname):
    with open(fname, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json_sync(fname, data):
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

# ----------------------------------------
# Bot & intents
# ----------------------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------------------------------
# Utility: logging to configured channel
# ----------------------------------------
def get_config_sync():
    return load_json_sync(DATA_FILES["config"])

async def log_action(guild: discord.Guild, title: str, description: str = None, color=discord.Color.blurple()):
    try:
        cfg = get_config_sync()
        ch_id = cfg.get("log_channel")
        if not ch_id:
            return
        ch = guild.get_channel(int(ch_id))
        if not ch:
            return
        embed = discord.Embed(title=title, description=description or "", color=color, timestamp=discord.utils.utcnow())
        await ch.send(embed=embed)
    except Exception:
        traceback.print_exc()

# ----------------------------------------
# On ready: sync commands
# ----------------------------------------
# --- Auto Rotating Status System ---
async def cycle_status():
    """Automatically change bot status every 60 seconds."""
    statuses = [
        ("Watching", "over the server 👀"),
        ("Listening", "to your commands 🎧"),
        ("Playing", "with moderation tools 🛠️"),
        ("Competing", "for best bot award 🏆"),
        ("Watching", "for rule breakers ⚔️"),
        ("Listening", "to feedback 💬")
    ]

    while True:
        status_type, status_text = random.choice(statuses)

        if status_type.lower() == "playing":
            activity = discord.Game(name=status_text)
        elif status_type.lower() == "listening":
            activity = discord.Activity(type=discord.ActivityType.listening, name=status_text)
        elif status_type.lower() == "watching":
            activity = discord.Activity(type=discord.ActivityType.watching, name=status_text)
        elif status_type.lower() == "competing":
            activity = discord.Activity(type=discord.ActivityType.competing, name=status_text)
        else:
            activity = discord.Game(name=status_text)

        await bot.change_presence(status=discord.Status.online, activity=activity)
        await asyncio.sleep(60)  # Change every 60 seconds

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    bot.loop.create_task(cycle_status())

    try:
        synced = await bot.tree.sync()
        print(f"📚 Synced {len(synced)} slash commands.")
    except Exception as e:
        print("⚠️ Slash sync failed:", e)

# ----------------------------------------
# Welcome / Goodbye events
# ----------------------------------------
@bot.event
async def on_member_join(member: discord.Member):
    cfg = get_config_sync()
    wc = cfg.get("welcome_channel")
    if wc:
        ch = member.guild.get_channel(int(wc))
        if ch:
            await ch.send(f"🎉 Welcome to the server, {member.mention}!")
    # custom DM from config? use welcome_dm_message key if desired
    dm_msg = cfg.get("welcome_dm_message") if "welcome_dm_message" in cfg else None
    if dm_msg:
        try:
            msg = dm_msg.replace("{user}", member.name).replace("{server}", member.guild.name)
            await member.send(msg)
        except:
            pass
    else:
        # default DM
        try:
            await member.send(f"👋 Hi {member.name}, welcome to **{member.guild.name}**!")
        except:
            pass
    await log_action(member.guild, "Member Joined", f"{member} ({member.id})")

@bot.event
async def on_member_remove(member: discord.Member):
    cfg = get_config_sync()
    gc = cfg.get("goodbye_channel")
    if gc:
        ch = member.guild.get_channel(int(gc))
        if ch:
            await ch.send(f"👋 {member.name} has left the server.")
    await log_action(member.guild, "Member Left", f"{member} ({member.id})")

# ----------------------------------------
# Auto-moderation: links, spam, caps
# ----------------------------------------
user_message_timestamps = {}  # for simple spam detection: user_id -> [timestamps]

@bot.event
async def on_message(message: discord.Message):
    # ignore bots
    if message.author.bot:
        return

    cfg = get_config_sync()
    guild = message.guild
    content = message.content or ""

    # Anti-link
    if cfg.get("filters", {}).get("anti_link", True):
        if re.search(r"https?://", content, re.IGNORECASE):
            try:
                await message.delete()
            except:
                pass
            await log_action(guild, "AutoMod - Link Removed", f"{message.author.mention} posted a link.")
            try:
                await message.author.send("⚠️ Links are not allowed in this server.")
            except:
                pass
            return

    # Caps filter
    if cfg.get("filters", {}).get("caps_filter", True):
        # check if message has >10 characters and is mostly uppercase letters
        stripped = re.sub(r'[^A-Za-z]', '', content)
        if len(stripped) >= 10 and stripped.isupper():
            try:
                await message.delete()
            except:
                pass
            await log_action(guild, "AutoMod - Caps Removed", f"{message.author.mention} used excessive caps.")
            try:
                await message.author.send("🧢 Please avoid using excessive caps.")
            except:
                pass
            return

    # Anti-spam: >5 messages within 5 seconds
    if cfg.get("filters", {}).get("anti_spam", True):
        uid = message.author.id
        now = time.time()
        arr = user_message_timestamps.get(uid, [])
        arr = [t for t in arr if now - t < 5]
        arr.append(now)
        user_message_timestamps[uid] = arr
        if len(arr) > 5:
            try:
                await message.delete()
            except:
                pass
            await log_action(guild, "AutoMod - Spam", f"{message.author.mention} is spamming.")
            try:
                await message.author.send("⛔ Please slow down — you are sending messages too quickly.")
            except:
                pass
            return

    # XP processing (only for non-bot users)
    await process_xp_on_message(message)

    # process commands after checks
    await bot.process_commands(message)

# ----------------------------------------
# Persistence helpers (sync & async wrappings)
# ----------------------------------------
def get_sync(fname_key):
    return load_json_sync(DATA_FILES[fname_key])

async def get_async(fname_key):
    return await load_json(DATA_FILES[fname_key])

def save_sync_key(fname_key, data):
    save_json_sync(DATA_FILES[fname_key], data)

async def save_async_key(fname_key, data):
    await save_json(DATA_FILES[fname_key], data)

# ----------------------------------------
# Warnings system
# ----------------------------------------
def load_warnings_sync():
    return get_sync("warnings")

def save_warnings_sync(obj):
    save_sync_key("warnings", obj)

async def load_warnings():
    return await get_async("warnings")

async def save_warnings(obj):
    await save_async_key("warnings", obj)

# warn command (slash)
@bot.tree.command(name="warn", description="Warn a member (moderators only)")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(member="Member to warn", reason="Reason for warning")
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    try:
        warnings = await load_warnings()
        uid = str(member.id)
        warnings.setdefault(uid, [])
        entry = {"moderator": str(interaction.user), "reason": reason, "time": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}
        warnings[uid].append(entry)
        await save_warnings(warnings)

        await interaction.response.send_message(f"⚠️ Warned {member.mention}. Reason: {reason}")
        await log_action(interaction.guild, "Warn Issued", f"{member.mention} warned by {interaction.user}: {reason}", color=discord.Color.gold())

        try:
            await member.send(f"⚠️ You were warned in **{interaction.guild.name}**. Reason: {reason}")
        except:
            pass

        # check auto-ban (5 warnings)
        await check_auto_ban(interaction.guild, member, warnings)

    except Exception as e:
        traceback.print_exc()
        await interaction.response.send_message("❌ Failed to warn (see logs).")

@bot.tree.command(name="warnings", description="Show warnings for a member")
@app_commands.checks.has_permissions(manage_messages=True)
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    warnings = await load_warnings()
    uid = str(member.id)
    arr = warnings.get(uid, [])
    if not arr:
        await interaction.response.send_message(f"✅ {member.mention} has no warnings.")
        return
    embed = discord.Embed(title=f"⚠️ Warnings for {member}", color=discord.Color.orange())
    for i, w in enumerate(arr, start=1):
        embed.add_field(name=f"Warning #{i}", value=f"**Moderator:** {w['moderator']}\n**Reason:** {w['reason']}\n**Date:** {w['time']}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clearwarns", description="Clear all warnings for a member")
@app_commands.checks.has_permissions(manage_messages=True)
async def clearwarns(interaction: discord.Interaction, member: discord.Member):
    warnings = await load_warnings()
    uid = str(member.id)
    if uid in warnings:
        warnings.pop(uid, None)
        await save_warnings(warnings)
        await interaction.response.send_message(f"✅ Cleared warnings for {member.mention}.")
        await log_action(interaction.guild, "Clear Warnings", f"{member.mention} cleared by {interaction.user}")
    else:
        await interaction.response.send_message("⚠️ That user has no warnings.")

# helper for tiered discipline
async def check_auto_ban(guild: discord.Guild, member: discord.Member, warnings_data=None):
    # if warnings_data provided, use it; otherwise load
    if warnings_data is None:
        warnings_data = await load_warnings()
    uid = str(member.id)
    total = len(warnings_data.get(uid, []))
    if total >= 5:
        try:
            await guild.ban(member, reason="Exceeded 5 warnings (Auto-ban)")
            await log_action(guild, "Auto-Ban", f"{member} was auto-banned after {total} warnings.", color=discord.Color.red())
            try:
                await member.send(f"🚫 You were automatically banned from **{guild.name}** after receiving {total} warnings.")
            except:
                pass
            # clear warnings after ban
            warnings_data.pop(uid, None)
            await save_warnings(warnings_data)
        except discord.Forbidden:
            await log_action(guild, "Auto-Ban Failed", f"Missing permissions to ban {member}.", color=discord.Color.orange())
        except Exception:
            traceback.print_exc()

# ----------------------------------------
# Timeout system + tracking
# ----------------------------------------
def parse_duration_to_seconds(s: str):
    # supports formats like 10s 5m 1h 2d
    try:
        val = int(s[:-1])
        unit = s[-1].lower()
        mult = {'s':1,'m':60,'h':3600,'d':86400}
        return val * mult.get(unit, 0)
    except Exception:
        return None

# async load/save timeouts
async def load_timeouts():
    return await get_async("timeouts")
async def save_timeouts(data):
    await save_async_key("timeouts", data)

@bot.tree.command(name="timeout", description="Temporarily timeout a member")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(member="Member to timeout", duration="Duration like 10s, 5m, 1h, 1d", reason="Reason")
async def timeout_cmd(interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided"):
    sec = parse_duration_to_seconds(duration)
    if sec is None or sec <= 0:
        await interaction.response.send_message("❌ Invalid duration format. Use like `10s`, `5m`, `1h`, `1d`.")
        return
    until = discord.utils.utcnow() + datetime.timedelta(seconds=sec)

    try:
        await member.timeout(until, reason=reason)
        await interaction.response.send_message(f"⏳ {member.mention} timed out for {duration}. Reason: {reason}")
        await log_action(interaction.guild, "Timeout", f"{member} timed out by {interaction.user} for {duration}. Reason: {reason}", color=discord.Color.orange())

        # store timeout history
        tdata = await load_timeouts()
        uid = str(member.id)
        tdata.setdefault(uid, [])
        tdata[uid].append({"moderator": str(interaction.user), "duration": duration, "reason": reason, "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})
        await save_timeouts(tdata)

        # DM user
        try:
            await member.send(f"⏳ You were timed out in **{interaction.guild.name}** for {duration}. Reason: {reason}")
        except:
            pass

        # if timeouts >=3 -> auto warn
        if len(tdata[uid]) >= 3:
            warnings = await load_warnings()
            warnings.setdefault(uid, [])
            warnings[uid].append({"moderator":"Auto-Mod", "reason":"3 timeouts (auto-warn)", "time": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})
            await save_warnings(warnings)
            await log_action(interaction.guild, "Auto-Warn", f"{member} auto-warned for 3 timeouts.", color=discord.Color.gold())
            try:
                await member.send("⚠️ You have been auto-warned for receiving 3 timeouts.")
            except:
                pass
            # after auto-warn, check auto-ban
            await check_auto_ban(interaction.guild, member, warnings)
    except discord.Forbidden:
        await interaction.response.send_message("⚠️ Missing permissions to timeout that user.")
    except Exception as e:
        traceback.print_exc()
        await interaction.response.send_message("❌ Error while timing out the user.")

@bot.tree.command(name="untimeout", description="Remove timeout from a user")
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout_cmd(interaction: discord.Interaction, member: discord.Member):
    try:
        await member.timeout(None)
        await interaction.response.send_message(f"✅ Timeout removed for {member.mention}.")
        await log_action(interaction.guild, "Timeout Removed", f"{member} timeout removed by {interaction.user}")
        try:
            await member.send(f"✅ Your timeout has been removed in **{interaction.guild.name}**.")
        except:
            pass
    except discord.Forbidden:
        await interaction.response.send_message("⚠️ Missing permissions to modify that user.")
    except Exception:
        traceback.print_exc()
        await interaction.response.send_message("❌ Error while removing timeout.")

@bot.tree.command(name="timeouts", description="Show timeout history for a user")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeouts_cmd(interaction: discord.Interaction, member: discord.Member):
    tdata = await load_timeouts()
    uid = str(member.id)
    arr = tdata.get(uid, [])
    if not arr:
        await interaction.response.send_message(f"✅ {member.mention} has no timeouts recorded.")
        return
    embed = discord.Embed(title=f"⏳ Timeouts for {member}", color=discord.Color.orange())
    for i, r in enumerate(arr[-10:], start=1):
        embed.add_field(name=f"#{i}", value=f"**Moderator:** {r['moderator']}\n**Duration:** {r['duration']}\n**Reason:** {r['reason']}\n**When:** {r['timestamp']}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----------------------------------------
# XP & Leveling system
# ----------------------------------------
# Simple design:
# xp.json stores guild_member_key -> {"xp": int, "level": int}
# Level formula: level increases when xp >= 50*level^2 + 50*level (same as earlier)
async def load_xp_data():
    return await get_async("xp")

async def save_xp_data(d):
    await save_async_key("xp", d)

def xp_required_for_level(lvl):
    return 50 * (lvl ** 2) + 50 * lvl

async def process_xp_on_message(message: discord.Message):
    # award xp for chat activity (non-bot)
    if message.author.bot or not message.guild:
        return
    key = f"{message.guild.id}-{message.author.id}"
    xp_db = await load_xp_data()
    user = xp_db.get(key, {"xp":0, "level":0})
    # basic cooldown (per-user in-memory)
    now = time.time()
    # using a simple attribute on bot is OK
    cooldowns = getattr(bot, "_xp_cooldowns", {})
    last = cooldowns.get(key, 0)
    if now - last < 30:  # 30s cooldown
        return
    gain = 8 + int((time.time() * 1000) % 9)  # simple pseudo-random small gain
    user["xp"] = user.get("xp",0) + gain
    new_level = user.get("level",0)
    while user["xp"] >= xp_required_for_level(new_level):
        new_level += 1
    if new_level > user.get("level",0):
        user["level"] = new_level
        # announce level up in the channel
        try:
            await message.channel.send(f"🎉 {message.author.mention} leveled up to **Level {new_level}**!")
            # check role rewards in config
            cfg = get_config_sync()
            rewards = cfg.get("level_rewards", {})
            role_id = rewards.get(str(new_level))
            if role_id:
                role = message.guild.get_role(int(role_id))
                if role:
                    try:
                        await message.author.add_roles(role)
                        try:
                            await message.author.send(f"🏅 You received the **{role.name}** role for reaching Level {new_level}!")
                        except:
                            pass
                    except:
                        pass
        except:
            pass
    xp_db[key] = user
    await save_xp_data(xp_db)
    cooldowns[key] = now
    bot._xp_cooldowns = cooldowns

# slash to show xp/level
@bot.tree.command(name="xp", description="Show your XP and level")
async def xp_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    xp_db = await load_xp_data()
    key = f"{interaction.guild.id}-{member.id}"
    user = xp_db.get(key, {"xp":0, "level":0})
    xp = user.get("xp",0)
    level = user.get("level",0)
    next_req = xp_required_for_level(level)
    embed = discord.Embed(title=f"🏆 XP for {member}", color=discord.Color.blurple())
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="XP", value=str(xp), inline=True)
    embed.add_field(name="XP for next level", value=str(next_req), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----------------------------------------
# Reaction Role system (button panels + reaction fallback)
# reaction_panels.json schema:
# { "<message_id>": {"guild_id": "<gid>", "type": "button"|"reaction", "roles": {"emoji_str": role_id}} }
# ----------------------------------------
def load_reaction_sync():
    return get_sync("reaction")
def save_reaction_sync(data):
    save_sync_key("reaction", data)

async def load_reaction_async():
    return await get_async("reaction")
async def save_reaction_async(data):
    await save_async_key("reaction", data)

class ReactionRoleView(View):
    def __init__(self, message_id):
        super().__init__(timeout=None)
        self.msg_id = str(message_id)
        # dynamically add buttons per stored panel
        panels = load_reaction_sync()
        panel = panels.get(self.msg_id, {})
        roles = panel.get("roles", {})
        for emoji, role_id in roles.items():
            try:
                # button custom_id encoding
                btn = Button(emoji=emoji if len(emoji) <= 2 else None, label="" if len(emoji) <= 2 else emoji, style=discord.ButtonStyle.secondary, custom_id=f"rr|{self.msg_id}|{role_id}")
                self.add_item(btn)
            except Exception:
                pass

@bot.event
async def on_interaction(interaction: discord.Interaction):
    # handle reaction-role button callback
    if interaction.type.value != 3 and interaction.data is None:
        return
    # check custom_id
    data = interaction.data or {}
    cid = data.get("custom_id")
    if not cid:
        return
    if isinstance(cid, str) and cid.startswith("rr|"):
        await interaction.response.defer(ephemeral=True)
        try:
            _, msg_id, role_id = cid.split("|")
            guild = interaction.guild
            member = interaction.user
            role = guild.get_role(int(role_id))
            if not role:
                await interaction.followup.send("⚠️ That role no longer exists.", ephemeral=True)
                return
            if role in member.roles:
                await member.remove_roles(role)
                await interaction.followup.send(f"❎ Removed **{role.name}**.", ephemeral=True)
                await log_action(guild, "Reaction Role Removed", f"{member} removed role {role.name}")
            else:
                await member.add_roles(role)
                await interaction.followup.send(f"✅ Added **{role.name}**!", ephemeral=True)
                await log_action(guild, "Reaction Role Added", f"{member} added role {role.name}")
        except Exception:
            traceback.print_exc()
        return

# legacy reaction add/remove
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    panels = load_reaction_sync()
    panel = panels.get(str(payload.message_id))
    if not panel or panel.get("type") != "reaction":
        return
    guild = bot.get_guild(payload.guild_id)
    emoji_str = str(payload.emoji)
    role_id = panel.get("roles", {}).get(emoji_str) or panel.get("roles", {}).get(payload.emoji.name)
    if not role_id:
        return
    role = guild.get_role(int(role_id))
    member = guild.get_member(payload.user_id)
    if role and member:
        try:
            await member.add_roles(role)
            await log_action(guild, "Reaction Role Added", f"{member} added role {role.name}")
        except:
            pass

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    panels = load_reaction_sync()
    panel = panels.get(str(payload.message_id))
    if not panel or panel.get("type") != "reaction":
        return
    guild = bot.get_guild(payload.guild_id)
    emoji_str = str(payload.emoji)
    role_id = panel.get("roles", {}).get(emoji_str) or panel.get("roles", {}).get(payload.emoji.name)
    if not role_id:
        return
    role = guild.get_role(int(role_id))
    member = guild.get_member(payload.user_id)
    if role and member:
        try:
            await member.remove_roles(role)
            await log_action(guild, "Reaction Role Removed", f"{member} removed role {role.name}")
        except:
            pass

# commands to create reaction panel
@bot.tree.command(name="reactionpanel", description="Create a reaction role panel (button or reaction)")
@app_commands.describe(panel_type="button or reaction", message="Message content for the panel")
@app_commands.checks.has_permissions(manage_roles=True)
async def reactionpanel(interaction: discord.Interaction, panel_type: str = "button", message: str = "Pick your roles!"):
    # send a message and register panel
    if panel_type not in ("button","reaction"):
        await interaction.response.send_message("Panel type must be 'button' or 'reaction'.", ephemeral=True)
        return
    sent = await interaction.channel.send(embed=discord.Embed(title="Reaction Roles", description=message, color=discord.Color.blurple()))
    panels = load_reaction_sync()
    panels[str(sent.id)] = {"guild_id": str(interaction.guild.id), "type": panel_type, "roles": {}}
    save_reaction_sync(panels)
    if panel_type == "button":
        try:
            await sent.edit(view=ReactionRoleView(sent.id))
        except:
            pass
    await interaction.response.send_message(f"✅ Reaction panel created (message id: {sent.id}). Use /addreactionrole to add roles.", ephemeral=True)

@bot.tree.command(name="addreactionrole", description="Add an emoji->role mapping to a panel")
@app_commands.describe(message_id="The panel message id", emoji="Emoji to use", role="Role to assign")
@app_commands.checks.has_permissions(manage_roles=True)
async def addreactionrole(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
    panels = load_reaction_sync()
    panel = panels.get(str(message_id))
    if not panel:
        await interaction.response.send_message("❌ Panel not found.", ephemeral=True)
        return
    panel["roles"][emoji] = role.id
    save_reaction_sync(panels)
    # if button panel, try to rebuild view
    if panel["type"] == "button":
        try:
            # fetch message
            channel = interaction.channel
            try:
                msg = await channel.fetch_message(int(message_id))
            except:
                # try searching all channels (expensive); best practice: run command in same channel as panel
                msg = None
                for ch in interaction.guild.text_channels:
                    try:
                        candidate = await ch.fetch_message(int(message_id))
                        msg = candidate
                        break
                    except:
                        pass
            if msg:
                await msg.edit(view=ReactionRoleView(message_id))
        except:
            traceback.print_exc()
    else:
        # reaction panel, add reaction to message if bot can access it
        try:
            # find the message similarly
            msg = None
            for ch in interaction.guild.text_channels:
                try:
                    candidate = await ch.fetch_message(int(message_id))
                    msg = candidate
                    break
                except:
                    pass
            if msg:
                await msg.add_reaction(emoji)
        except:
            pass
    await interaction.response.send_message(f"✅ Linked {emoji} → {role.mention} in panel {message_id}", ephemeral=True)

# ----------------------------------------
# Ticket system (button to create ticket; per-guild ticket category optional)
# tickets.json stores active tickets metadata if needed
# ----------------------------------------
def load_tickets_sync():
    return get_sync("tickets")
def save_tickets_sync(data):
    save_sync_key("tickets", data)

async def load_tickets_async():
    return await get_async("tickets")
async def save_tickets_async(data):
    await save_async_key("tickets", data)

class TicketCreateView(View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Create Ticket 🎫", style=discord.ButtonStyle.green, custom_id="create_ticket_btn")
    async def create_button(self, interaction: discord.Interaction, button: Button):
        guild = interaction.guild
        cfg = get_config_sync()
        category_id = cfg.get("ticket_category")
        category = guild.get_channel(int(category_id)) if category_id else None
        # make unique channel name
        base = f"ticket-{interaction.user.name}".lower().replace(" ", "-")
        suffix = str(int(time.time()))[-4:]
        channel_name = f"{base}-{suffix}"
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True)
        }
        staff_role_id = cfg.get("staff_role")
        if staff_role_id:
            staff_role = guild.get_role(int(staff_role_id))
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        channel = await guild.create_text_channel(channel_name, overwrites=overwrites, category=category)
        # send initial message with close button
        close_view = TicketCloseView()
        em = discord.Embed(title="🎟️ Ticket Created", description=f"{interaction.user.mention} — a staff member will be with you shortly.\nClick Close when finished.", color=discord.Color.green())
        await channel.send(content=interaction.user.mention, embed=em, view=close_view)
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)
        await log_action(guild, "Ticket Created", f"Ticket {channel.name} created by {interaction.user}")

class TicketCloseView(View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Close Ticket 🔒", style=discord.ButtonStyle.red, custom_id="close_ticket_btn")
    async def close_button(self, interaction: discord.Interaction, button: Button):
        channel = interaction.channel
        # gather last messages as transcript
        messages = []
        try:
            async for m in channel.history(limit=1000, oldest_first=True):
                timestamp = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
                messages.append(f"[{timestamp}] {m.author}: {m.content}")
        except:
            pass
        transcript = "\n".join(messages)
        # post transcript to log channel if exists
        cfg = get_config_sync()
        log_id = cfg.get("log_channel")
        if log_id:
            log_ch = channel.guild.get_channel(int(log_id))
            if log_ch:
                try:
                    await log_ch.send(embed=discord.Embed(title="Ticket Transcript", description=f"Ticket closed: {channel.name}", color=discord.Color.dark_grey()))
                    if transcript:
                        # if transcript is long, send as file
                        if len(transcript) > 1900:
                            await log_ch.send(file=discord.File(fp=discord.utils.snowflake_time(1), filename="transcript.txt"))
                        else:
                            await log_ch.send(f"```{transcript[:1900]}```")
                except:
                    pass
        try:
            await channel.delete()
        except:
            pass

# command to create a ticket panel with create button
@bot.tree.command(name="ticketpanel", description="Create a ticket creation panel (staff role optional)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(message="Intro text for the ticket panel")
async def ticketpanel(interaction: discord.Interaction, message: str = "Click the button to create a ticket"):
    view = TicketCreateView()
    await interaction.response.send_message(embed=discord.Embed(title="Support Ticket", description=message, color=discord.Color.green()), view=view)
    await log_action(interaction.guild, "Ticket Panel Created", f"{interaction.user} created a ticket panel.")

# ----------------------------------------
# Setup commands: setwelcome/setgoodbye/setlog/staff role/ticket category/level reward/premium
# ----------------------------------------
@bot.tree.command(name="setwelcome", description="Set welcome channel")
@app_commands.checks.has_permissions(administrator=True)
async def setwelcome(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_config_sync()
    cfg["welcome_channel"] = str(channel.id)
    save_json_sync(DATA_FILES["config"], cfg)
    await interaction.response.send_message(f"✅ Welcome channel set to {channel.mention}")

@bot.tree.command(name="setgoodbye", description="Set goodbye channel")
@app_commands.checks.has_permissions(administrator=True)
async def setgoodbye(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_config_sync()
    cfg["goodbye_channel"] = str(channel.id)
    save_json_sync(DATA_FILES["config"], cfg)
    await interaction.response.send_message(f"✅ Goodbye channel set to {channel.mention}")

@bot.tree.command(name="setlog", description="Set mod-log channel")
@app_commands.checks.has_permissions(administrator=True)
async def setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_config_sync()
    cfg["log_channel"] = str(channel.id)
    save_json_sync(DATA_FILES["config"], cfg)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}")

@bot.tree.command(name="setstaffrole", description="Set staff role for ticket visibility")
@app_commands.checks.has_permissions(administrator=True)
async def setstaffrole(interaction: discord.Interaction, role: discord.Role):
    cfg = get_config_sync()
    cfg["staff_role"] = str(role.id)
    save_json_sync(DATA_FILES["config"], cfg)
    await interaction.response.send_message(f"✅ Staff role set to {role.name}")

@bot.tree.command(name="setticketcategory", description="Set ticket category (optional)")
@app_commands.checks.has_permissions(administrator=True)
async def setticketcategory(interaction: discord.Interaction, category: discord.CategoryChannel):
    cfg = get_config_sync()
    cfg["ticket_category"] = str(category.id)
    save_json_sync(DATA_FILES["config"], cfg)
    await interaction.response.send_message(f"✅ Ticket category set to {category.name}")

@bot.tree.command(name="setlevelreward", description="Set a role reward for reaching a level")
@app_commands.checks.has_permissions(administrator=True)
async def setlevelreward(interaction: discord.Interaction, level: int, role: discord.Role):
    cfg = get_config_sync()
    lr = cfg.get("level_rewards", {})
    lr[str(level)] = str(role.id)
    cfg["level_rewards"] = lr
    save_json_sync(DATA_FILES["config"], cfg)
    await interaction.response.send_message(f"✅ Role {role.name} will be awarded at level {level}.")

@bot.tree.command(name="setpremium", description="Mark this guild as premium (admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def setpremium(interaction: discord.Interaction, enabled: bool):
    cfg = get_config_sync()
    pg = cfg.get("premium_guilds", [])
    gid = str(interaction.guild.id)
    if enabled:
        if gid not in pg:
            pg.append(gid)
    else:
        if gid in pg:
            pg.remove(gid)
    cfg["premium_guilds"] = pg
    save_json_sync(DATA_FILES["config"], cfg)
    await interaction.response.send_message(f"✅ Premium set to {enabled} for this server.")

# ----------------------------------------
# Infractions summary combining warnings + timeouts
# ----------------------------------------
@bot.tree.command(name="infractions", description="Show combined warnings and timeouts for a user")
@app_commands.checks.has_permissions(moderate_members=True)
async def infractions(interaction: discord.Interaction, member: discord.Member):
    warnings = await load_warnings()
    tdata = await load_timeouts()
    uid = str(member.id)
    warns = warnings.get(uid, [])
    touts = tdata.get(uid, [])
    total_warns = len(warns)
    total_timeouts = len(touts)
    if total_warns == 0 and total_timeouts == 0:
        await interaction.response.send_message(f"✅ {member.mention} has a clean record.", ephemeral=True)
        return
    embed = discord.Embed(title=f"📜 Infractions for {member}", color=discord.Color.red() if total_warns+total_timeouts>3 else discord.Color.orange())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Total Warnings", value=str(total_warns), inline=True)
    embed.add_field(name="Total Timeouts", value=str(total_timeouts), inline=True)
    if warns:
        text = ""
        for i, w in enumerate(warns[-5:], start=1):
            text += f"**#{i}** {w['reason']} (by {w['moderator']}) — {w['time']}\n"
        embed.add_field(name="Recent Warnings", value=text[:1024], inline=False)
    if touts:
        text = ""
        for i, t in enumerate(touts[-5:], start=1):
            text += f"**#{i}** {t['reason']} — {t['duration']} (by {t['moderator']}) — {t['timestamp']}\n"
        embed.add_field(name="Recent Timeouts", value=text[:1024], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----------------------------------------
# Help command (slash) with buttons
# ----------------------------------------
class HelpView(View):
    def __init__(self):
        super().__init__(timeout=120)

    async def update_embed(self, interaction: discord.Interaction, title: str, desc: str, color):
        embed = discord.Embed(title=title, description=desc, color=color)
        embed.set_footer(text="Use buttons to switch sections • Expires in 2 minutes")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Moderation ⚔️", style=discord.ButtonStyle.blurple)
    async def mod_btn(self, interaction: discord.Interaction, button: Button):
        desc = (
            "• `/kick`, `/ban`, `/unban`\n"
            "• `/warn`, `/warnings`, `/clearwarns`\n"
            "• `/timeout`, `/untimeout`, `/timeouts`\n"
            "• `/infractions` — Show combined record\n"
            "• Auto-warn after 3 timeouts\n"
            "• Auto-ban after 5 warnings"
        )
        await self.update_embed(interaction, "⚔️ Moderation Commands", desc, discord.Color.red())

    @discord.ui.button(label="Utility 🧰", style=discord.ButtonStyle.green)
    async def util_btn(self, interaction: discord.Interaction, button: Button):
        desc = (
            "• `/xp` — Show XP & level\n"
            "• `/serverinfo` — Server info\n"
            "• `/userinfo` — User info\n"
            "• Ticket & reaction role support (see below)"
        )
        await self.update_embed(interaction, "🧰 Utility Commands", desc, discord.Color.green())

    @discord.ui.button(label="Setup & Premium ⚙️", style=discord.ButtonStyle.gray)
    async def setup_btn(self, interaction: discord.Interaction, button: Button):
        desc = (
            "• `/setwelcome`, `/setgoodbye`, `/setlog` — Configure channels\n"
            "• `/setstaffrole`, `/setticketcategory` — Ticket settings\n"
            "• `/setlevelreward` — reward role for reaching a level\n"
            "• `/setpremium` — enable premium features for this server"
        )
        await self.update_embed(interaction, "⚙️ Setup & Premium", desc, discord.Color.blurple())

    @discord.ui.button(label="Close ❌", style=discord.ButtonStyle.red)
    async def close_btn(self, interaction: discord.Interaction, button: Button):
        try:
            await interaction.message.delete()
        except:
            pass

@bot.tree.command(name="help", description="Show help and command categories")
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(title="🛠️ Bot Help", description="Click buttons to browse categories.", color=discord.Color.blurple())
    view = HelpView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# ----------------------------------------
# Additional utility slash commands (serverinfo, userinfo)
# ----------------------------------------
@bot.tree.command(name="serverinfo", description="Show information about this server")
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=f"📊 {g.name}", color=discord.Color.blurple())
    embed.add_field(name="Owner", value=str(g.owner), inline=True)
    embed.add_field(name="Members", value=str(g.member_count), inline=True)
    embed.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d"), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="userinfo", description="Show user information")
@app_commands.describe(member="User to show info for")
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    embed = discord.Embed(title=f"👤 {member}", color=discord.Color.green())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=str(member.id), inline=True)
    embed.add_field(name="Joined", value=str(member.joined_at) if member.joined_at else "Unknown", inline=True)
    embed.add_field(name="Created", value=str(member.created_at), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----------------------------------------
# Start the bot
# ----------------------------------------
if __name__ == "__main__":
    bot.run(TOKEN)
