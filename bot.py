# bot.py - All-in-one moderation + XP + tickets + reaction-roles + premium + rotating status
# Requires: discord.py 2.x, python-dotenv, aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import View, Button
from dotenv import load_dotenv
import os, json, re, time, datetime, random, asyncio, aiohttp, traceback

# ---------------------------
# Load token
# ---------------------------
load_dotenv()
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("No TOKEN in .env. Add TOKEN=your_bot_token")

# ---------------------------
# File helpers & defaults
# ---------------------------
def ensure_json(fname, default):
    if not os.path.exists(fname):
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)
    with open(fname, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(fname, data):
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

# Files
CONFIG_FILE = "config.json"
WARN_FILE = "warnings.json"
TIMEOUTS_FILE = "timeouts.json"
XP_FILE = "xp.json"
REACTION_FILE = "reaction_roles.json"
TICKETS_DIR = "tickets"

# Ensure files exist with safe default structures
config = ensure_json(CONFIG_FILE, {"guilds": {}})
warnings_data = ensure_json(WARN_FILE, {})
timeouts_data = ensure_json(TIMEOUTS_FILE, {})
xp_data = ensure_json(XP_FILE, {})
reaction_panels = ensure_json(REACTION_FILE, {})

if not os.path.isdir(TICKETS_DIR):
    os.makedirs(TICKETS_DIR, exist_ok=True)

# ---------------------------
# Bot & intents
# ---------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------
# Utilities
# ---------------------------
def guild_config(guild_id: int):
    gid = str(guild_id)
    if gid not in config["guilds"]:
        config["guilds"][gid] = {
            "welcome_channel": None,
            "goodbye_channel": None,
            "log_channel": None,
            "welcome_dm": "👋 Welcome {user} to {server}!",
            "premium": False,
            "level_rewards": {},  # level: role_id
            "filters": {"anti_link": True, "anti_spam": True, "caps_filter": True},
            "auto_role": None,
            "ticket_category": None,
            "staff_role": None
        }
        save_json(CONFIG_FILE, config)
    return config["guilds"][gid]

def now_iso():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def log_action(guild: discord.Guild, embed_or_text):
    try:
        gcfg = guild_config(guild.id)
        lid = gcfg.get("log_channel")
        if not lid:
            return
        ch = guild.get_channel(int(lid))
        if not ch:
            return
        if isinstance(embed_or_text, discord.Embed):
            asyncio.create_task(ch.send(embed=embed_or_text))
        else:
            asyncio.create_task(ch.send(embed=discord.Embed(description=str(embed_or_text), color=discord.Color.blurple())))
    except Exception:
        print("log_action error:", traceback.format_exc())

# ---------------------------
# XP & Leveling
# ---------------------------
def load_xp():
    global xp_data
    xp_data = ensure_json(XP_FILE, {})

def save_xp():
    save_json(XP_FILE, xp_data)

def xp_to_level(xp):
    lvl = 0
    while xp >= (50 * lvl * lvl + 50 * lvl):
        lvl += 1
    return lvl

def xp_add_message(member: discord.Member):
    if member.bot: return
    gid = str(member.guild.id)
    uid = str(member.id)
    key = f"{gid}-{uid}"
    entry = xp_data.get(key, {"xp": 0, "level": 0})
    gain = random.randint(8, 16)
    entry["xp"] += gain
    new_lvl = xp_to_level(entry["xp"])
    if new_lvl > entry.get("level", 0):
        entry["level"] = new_lvl
        # Level up announcements (try system channel, fallback to guild default, else ignore)
        try:
            gcfg = guild_config(member.guild.id)
            msg = f"🎉 {member.mention} leveled up to **{new_lvl}**!"
            # send to log channel if exists otherwise system channel
            dest = None
            if gcfg.get("log_channel"):
                dest = member.guild.get_channel(int(gcfg["log_channel"]))
            if not dest and member.guild.system_channel:
                dest = member.guild.system_channel
            if dest:
                asyncio.create_task(dest.send(msg))
        except Exception:
            pass
        # role rewards
        gcfg = guild_config(member.guild.id)
        rewards = gcfg.get("level_rewards", {})
        rid = rewards.get(str(new_lvl))
        if rid:
            role = member.guild.get_role(int(rid))
            if role:
                try:
                    asyncio.create_task(member.add_roles(role))
                except:
                    pass
    xp_data[key] = entry
    save_xp()

# ---------------------------
# Ticket system (button)
# ---------------------------
class TicketCloseView(View):
    def __init__(self, channel_id):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.client.get_channel(self.channel_id)
        if not channel:
            await interaction.response.send_message("Channel not found.", ephemeral=True)
            return
        # transcript
        msgs = []
        async for m in channel.history(limit=1000, oldest_first=True):
            t = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
            content = m.content or ""
            msgs.append(f"[{t}] {m.author}: {content}")
        transcript = "\n".join(msgs)
        tfile = os.path.join(TICKETS_DIR, f"{channel.guild.id}_{channel.id}.txt")
        with open(tfile, "w", encoding="utf-8") as f:
            f.write(transcript)
        # send transcript to log channel if set
        gcfg = guild_config(channel.guild.id)
        lid = gcfg.get("log_channel")
        if lid:
            logch = channel.guild.get_channel(int(lid))
            if logch:
                try:
                    await logch.send(f"📄 Ticket {channel.name} closed by {interaction.user}. Transcript:", file=discord.File(tfile))
                except:
                    pass
        await channel.delete()

class TicketCreateView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="create_ticket_btn")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        gcfg = guild_config(guild.id)
        category_id = gcfg.get("ticket_category")
        category = guild.get_channel(int(category_id)) if category_id else None
        staff_role_id = gcfg.get("staff_role")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True)
        }
        if staff_role_id:
            r = guild.get_role(int(staff_role_id))
            if r:
                overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        base = f"ticket-{interaction.user.name}".lower()
        suffix = random.randint(1000, 9999)
        name = f"{base}-{suffix}"
        channel = await guild.create_text_channel(name, overwrites=overwrites, category=category)
        view = TicketCloseView(channel.id)
        await channel.send(f"{interaction.user.mention} Ticket created. Staff will be with you soon.", view=view)
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)
        log_action(guild, f"🎫 Ticket created: {channel.name} by {interaction.user}")

# ---------------------------
# Reaction role system
# ---------------------------
def save_reaction_panels():
    save_json(REACTION_FILE, reaction_panels)

class ReactionRoleView(View):
    def __init__(self, message_id):
        super().__init__(timeout=None)
        self.message_id = str(message_id)
        panel = reaction_panels.get(self.message_id, {})
        for emoji, rid in panel.get("roles", {}).items():
            try:
                btn = Button(emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=f"rr|{self.message_id}|{rid}")
                self.add_item(btn)
            except Exception:
                pass

@bot.event
async def on_interaction(interaction: discord.Interaction):
    try:
        data = getattr(interaction, "data", None)
        if not data:
            return
        cid = data.get("custom_id")
        if not cid:
            return
        if cid.startswith("rr|"):
            await interaction.response.defer(ephemeral=True)
            _, mid, rid = cid.split("|")
            guild = interaction.guild
            member = interaction.user
            role = guild.get_role(int(rid))
            if role in member.roles:
                await member.remove_roles(role)
                await interaction.followup.send(f"❎ Removed **{role.name}**.", ephemeral=True)
                log_action(guild, f"🎭 Reaction role removed: {member} - {role.name}")
            else:
                await member.add_roles(role)
                await interaction.followup.send(f"✅ Added **{role.name}**.", ephemeral=True)
                log_action(guild, f"🎭 Reaction role added: {member} - {role.name}")
    except Exception:
        print("on_interaction error:", traceback.format_exc())

@bot.event
async def on_raw_reaction_add(payload):
    try:
        if str(payload.message_id) not in reaction_panels: return
        panel = reaction_panels[str(payload.message_id)]
        if panel.get("type") != "reaction": return
        emoji = str(payload.emoji)
        rid = panel.get("roles", {}).get(emoji) or panel.get("roles", {}).get(payload.emoji.name)
        if not rid: return
        g = bot.get_guild(payload.guild_id)
        if not g: return
        member = g.get_member(payload.user_id)
        if not member: return
        role = g.get_role(int(rid))
        if role:
            await member.add_roles(role)
            log_action(g, f"🎭 Reaction role added: {member} - {role.name}")
    except Exception:
        print("on_raw_reaction_add error:", traceback.format_exc())

@bot.event
async def on_raw_reaction_remove(payload):
    try:
        if str(payload.message_id) not in reaction_panels: return
        panel = reaction_panels[str(payload.message_id)]
        if panel.get("type") != "reaction": return
        emoji = str(payload.emoji)
        rid = panel.get("roles", {}).get(emoji) or panel.get("roles", {}).get(payload.emoji.name)
        if not rid: return
        g = bot.get_guild(payload.guild_id)
        if not g: return
        member = g.get_member(payload.user_id)
        if not member: return
        role = g.get_role(int(rid))
        if role:
            await member.remove_roles(role)
            log_action(g, f"🎭 Reaction role removed: {member} - {role.name}")
    except Exception:
        print("on_raw_reaction_remove error:", traceback.format_exc())

# ---------------------------
# Warnings, Timeouts, Tiered discipline
# ---------------------------
def load_warnings():
    global warnings_data
    warnings_data = ensure_json(WARN_FILE, {})
    return warnings_data

def save_warnings(data):
    save_json(WARN_FILE, data)

def load_timeouts():
    global timeouts_data
    timeouts_data = ensure_json(TIMEOUTS_FILE, {})
    return timeouts_data

def save_timeouts(data):
    save_json(TIMEOUTS_FILE, data)

async def check_auto_ban(guild: discord.Guild, member: discord.Member):
    warnings = load_warnings()
    uid = str(member.id)
    total = len(warnings.get(uid, []))
    if total >= 5:
        try:
            await member.ban(reason="Auto-ban: exceeded 5 warnings")
            log_action(guild, f"🚫 Auto-ban: {member} (warnings: {total})")
            try:
                await member.send(f"🚫 You were automatically banned from **{guild.name}** after receiving {total} warnings.")
            except:
                pass
            warnings.pop(uid, None)
            save_warnings(warnings)
        except Exception:
            log_action(guild, f"⚠️ Auto-ban failed for {member} (missing perms?)")

# ---------------------------
# Auto-moderation (anti-link, anti-spam, caps) and XP granting
# ---------------------------
user_message_times = {}

@bot.event
async def on_message(message: discord.Message):
    try:
        if message.author.bot:
            return

        # Always process commands first
        await bot.process_commands(message)

        if not message.guild:
            # allow XP in guild-only; skip DMs for auto-mod
            return

        gcfg = guild_config(message.guild.id)
        content = message.content or ""
        lower = content.lower()

        # anti-link
        if gcfg["filters"].get("anti_link", True):
            if re.search(r"https?:\/\/\S+", lower):
                try:
                    await message.delete()
                except:
                    pass
                log_action(message.guild, f"🚫 Link removed from {message.author}")
                try:
                    await message.author.send(f"⚠️ Links are not allowed in {message.guild.name}.")
                except:
                    pass
                return

        # caps filter
        if gcfg["filters"].get("caps_filter", True):
            if len(content) > 10 and content.isupper():
                try:
                    await message.delete()
                except:
                    pass
                log_action(message.guild, f"🧢 Caps message deleted from {message.author}")
                try:
                    await message.author.send("🧢 Please avoid excessive caps.")
                except:
                    pass
                return

        # anti-spam
        if gcfg["filters"].get("anti_spam", True):
            nowt = time.time()
            lst = user_message_times.get(message.author.id, [])
            lst = [t for t in lst if nowt - t < 5]
            lst.append(nowt)
            user_message_times[message.author.id] = lst
            if len(lst) > 5:
                try:
                    await message.delete()
                except:
                    pass
                log_action(message.guild, f"🚷 Spam: {message.author}")
                try:
                    await message.author.send("⛔ Slow down — you're sending messages too quickly.")
                except:
                    pass
                return

        # XP
        try:
            xp_add_message(message.author)
        except Exception:
            pass

    except Exception:
        print("on_message error:", traceback.format_exc())

# ---------------------------
# Slash & command implementations
# ---------------------------

# Setup commands
@bot.tree.command(name="setwelcome", description="Set welcome channel")
@app_commands.checks.has_permissions(administrator=True)
async def setwelcome(interaction: discord.Interaction, channel: discord.TextChannel):
    gcfg = guild_config(interaction.guild.id)
    gcfg["welcome_channel"] = str(channel.id)
    save_json(CONFIG_FILE, config)
    await interaction.response.send_message(f"✅ Welcome channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="setgoodbye", description="Set goodbye channel")
@app_commands.checks.has_permissions(administrator=True)
async def setgoodbye(interaction: discord.Interaction, channel: discord.TextChannel):
    gcfg = guild_config(interaction.guild.id)
    gcfg["goodbye_channel"] = str(channel.id)
    save_json(CONFIG_FILE, config)
    await interaction.response.send_message(f"✅ Goodbye channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="setlog", description="Set log channel")
@app_commands.checks.has_permissions(administrator=True)
async def setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    gcfg = guild_config(interaction.guild.id)
    gcfg["log_channel"] = str(channel.id)
    save_json(CONFIG_FILE, config)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="setwelcomedm", description="Set custom welcome DM (use {user} and {server})")
@app_commands.checks.has_permissions(administrator=True)
async def setwelcomedm(interaction: discord.Interaction, *, message: str):
    gcfg = guild_config(interaction.guild.id)
    gcfg["welcome_dm"] = message
    save_json(CONFIG_FILE, config)
    await interaction.response.send_message("✅ Welcome DM updated.", ephemeral=True)

# Moderation commands
@bot.tree.command(name="kick", description="Kick a member")
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.describe(member="Member to kick", reason="Reason")
async def slash_kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 {member.mention} kicked. Reason: {reason}")
        log_action(interaction.guild, f"👢 Kick: {member} by {interaction.user} — {reason}")
        try:
            await member.send(f"👢 You were kicked from {interaction.guild.name}. Reason: {reason}")
        except:
            pass
    except Exception:
        await interaction.response.send_message("❌ Failed to kick (permissions?).", ephemeral=True)

@bot.tree.command(name="ban", description="Ban a member")
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.describe(member="Member to ban", reason="Reason")
async def slash_ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    try:
        await member.ban(reason=reason)
        await interaction.response.send_message(f"⛔ {member.mention} banned. Reason: {reason}")
        log_action(interaction.guild, f"⛔ Ban: {member} by {interaction.user} — {reason}")
        try:
            await member.send(f"⛔ You were banned from {interaction.guild.name}. Reason: {reason}")
        except:
            pass
    except Exception:
        await interaction.response.send_message("❌ Failed to ban (permissions?).", ephemeral=True)

@bot.tree.command(name="unban", description="Unban a user by name#discriminator or ID")
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.describe(user="username#discriminator or user id")
async def slash_unban(interaction: discord.Interaction, user: str):
    try:
        if user.isdigit():
            try:
                await interaction.guild.unban(discord.Object(id=int(user)))
                await interaction.response.send_message(f"✅ Unbanned id {user}", ephemeral=True)
                return
            except:
                pass
        if "#" in user:
            name, discr = user.split("#", 1)
            bans = await interaction.guild.bans()
            for entry in bans:
                u = entry.user
                if u.name == name and u.discriminator == discr:
                    await interaction.guild.unban(u)
                    await interaction.response.send_message(f"✅ Unbanned {u}", ephemeral=True)
                    return
        await interaction.response.send_message("❌ User not found in bans.", ephemeral=True)
    except Exception:
        await interaction.response.send_message("❌ Failed to unban.", ephemeral=True)

# Timeout/parsing
def parse_duration(duration: str):
    units = {'s':1,'m':60,'h':3600,'d':86400}
    try:
        val = int(duration[:-1])
        unit = duration[-1]
        return val * units[unit]
    except Exception:
        return None

@bot.tree.command(name="timeout", description="Temporarily timeout a member (e.g., 10m, 1h)")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(member="Member", duration="10s,5m,1h,1d", reason="Reason")
async def slash_timeout(interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided"):
    sec = parse_duration(duration)
    if sec is None:
        await interaction.response.send_message("❌ Invalid duration format.", ephemeral=True)
        return
    try:
        until = discord.utils.utcnow() + datetime.timedelta(seconds=sec)
        await member.timeout(until, reason=reason)
        await interaction.response.send_message(f"⏳ {member.mention} timed out for {duration}. Reason: {reason}")
        log_action(interaction.guild, f"⏳ Timeout: {member} for {duration} by {interaction.user} — {reason}")
        # record timeout
        tdata = load_timeouts()
        uid = str(member.id)
        rec = {"moderator": str(interaction.user), "duration": duration, "reason": reason, "timestamp": now_iso()}
        tdata.setdefault(uid, []).append(rec)
        save_timeouts(tdata)
        # auto-warn after 3 timeouts
        if len(tdata.get(uid, [])) >= 3:
            wdata = load_warnings()
            wdata.setdefault(uid, []).append({"moderator":"Auto-Mod","reason":"3+ timeouts","time":now_iso()})
            save_warnings(wdata)
            log_action(interaction.guild, f"⚠️ Auto-warn: {member} after 3 timeouts.")
            try:
                await member.send("⚠️ You received an automatic warning for repeated timeouts.")
            except:
                pass
            await check_auto_ban(interaction.guild, member)
    except discord.Forbidden:
        await interaction.response.send_message("⚠️ Missing permission to timeout that member.", ephemeral=True)
    except Exception:
        await interaction.response.send_message("❌ Timeout failed.", ephemeral=True)

@bot.tree.command(name="untimeout", description="Remove a member's timeout")
@app_commands.checks.has_permissions(moderate_members=True)
async def slash_untimeout(interaction: discord.Interaction, member: discord.Member):
    try:
        await member.timeout(None)
        await interaction.response.send_message(f"✅ Timeout removed for {member.mention}")
        log_action(interaction.guild, f"✅ Timeout removed: {member} by {interaction.user}")
    except Exception:
        await interaction.response.send_message("❌ Failed to remove timeout.", ephemeral=True)

@bot.tree.command(name="timeouts", description="Show timeout history for a user")
@app_commands.checks.has_permissions(moderate_members=True)
async def slash_timeouts(interaction: discord.Interaction, member: discord.Member):
    tdata = load_timeouts()
    uid = str(member.id)
    items = tdata.get(uid, [])
    if not items:
        await interaction.response.send_message(f"No timeouts for {member.mention}", ephemeral=True)
        return
    embed = discord.Embed(title=f"⏳ Timeouts for {member}", color=discord.Color.orange())
    for i, rec in enumerate(items[-10:], start=1):
        embed.add_field(name=f"#{i}", value=f"{rec['duration']} — {rec['reason']} by {rec['moderator']}\n{rec['timestamp']}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Warning system
@bot.tree.command(name="warn", description="Warn a member")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(member="Member to warn", reason="Reason")
async def slash_warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    w = load_warnings()
    uid = str(member.id)
    rec = {"moderator": str(interaction.user), "reason": reason, "time": now_iso()}
    w.setdefault(uid, []).append(rec)
    save_warnings(w)
    await interaction.response.send_message(f"⚠️ Warned {member.mention}. Reason: {reason}")
    log_action(interaction.guild, f"⚠️ Warn: {member} by {interaction.user} — {reason}")
    try:
        await member.send(f"⚠️ You were warned in {interaction.guild.name}. Reason: {reason}")
    except:
        pass
    await check_auto_ban(interaction.guild, member)

@bot.tree.command(name="warnings", description="Show warnings for a user")
@app_commands.checks.has_permissions(manage_messages=True)
async def slash_warnings(interaction: discord.Interaction, member: discord.Member):
    w = load_warnings()
    uid = str(member.id)
    items = w.get(uid, [])
    if not items:
        await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)
        return
    embed = discord.Embed(title=f"⚠️ Warnings for {member}", color=discord.Color.orange())
    for i, rec in enumerate(items[-10:], start=1):
        embed.add_field(name=f"#{i}", value=f"{rec['reason']} — {rec['moderator']}\n{rec['time']}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clearwarns", description="Clear all warnings for a user")
@app_commands.checks.has_permissions(manage_messages=True)
async def slash_clearwarns(interaction: discord.Interaction, member: discord.Member):
    w = load_warnings()
    uid = str(member.id)
    if uid in w:
        del w[uid]
        save_warnings(w)
        await interaction.response.send_message(f"✅ Cleared warnings for {member.mention}", ephemeral=True)
        log_action(interaction.guild, f"🧹 Cleared warnings for {member} by {interaction.user}")
    else:
        await interaction.response.send_message("That user has no warnings.", ephemeral=True)

# Infractions
@bot.tree.command(name="infractions", description="View all warnings and timeouts for a user")
@app_commands.checks.has_permissions(moderate_members=True)
async def slash_infractions(interaction: discord.Interaction, member: discord.Member):
    w = load_warnings(); t = load_timeouts()
    uid = str(member.id)
    warns = w.get(uid, []); topts = t.get(uid, [])
    total = len(warns) + len(topts)
    if total == 0:
        await interaction.response.send_message(f"{member.mention} has a clean record!", ephemeral=True)
        return
    embed = discord.Embed(title=f"📜 Infractions for {member}", color=discord.Color.red() if total>3 else discord.Color.orange())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Warnings", value=str(len(warns)), inline=True)
    embed.add_field(name="Timeouts", value=str(len(topts)), inline=True)
    if warns:
        text = ""
        for i, rec in enumerate(warns[-5:], start=1):
            text += f"#{i} {rec['reason']} — {rec['moderator']} ({rec['time']})\n"
        embed.add_field(name="Recent Warnings", value=text[:1024], inline=False)
    if topts:
        text = ""
        for i, rec in enumerate(topts[-5:], start=1):
            text += f"#{i} {rec['duration']} {rec['reason']} — {rec['moderator']} ({rec['timestamp']})\n"
        embed.add_field(name="Recent Timeouts", value=text[:1024], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Tickets & reaction panels (admin)
@bot.tree.command(name="ticket_panel", description="Create a ticket panel (button) in the current channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_panel(interaction: discord.Interaction):
    view = TicketCreateView()
    await interaction.response.send_message("🎫 Click to create a ticket.", view=view)
    log_action(interaction.guild, f"Ticket panel created by {interaction.user}")

@bot.tree.command(name="ticket_category", description="Set ticket category channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    gcfg = guild_config(interaction.guild.id)
    gcfg["ticket_category"] = str(category.id)
    save_json(CONFIG_FILE, config)
    await interaction.response.send_message(f"✅ Ticket category set to {category.name}", ephemeral=True)

@bot.tree.command(name="reaction_panel", description="Create a reaction/ button role panel")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(panel_type="button or reaction")
async def reaction_panel(interaction: discord.Interaction, panel_type: str = "button", *, text: str = "React to get roles"):
    embed = discord.Embed(title="Reaction Roles", description=text, color=discord.Color.blurple())
    msg = await interaction.channel.send(embed=embed)
    reaction_panels[str(msg.id)] = {"guild": str(interaction.guild.id), "type": panel_type, "roles": {}}
    save_reaction_panels()
    if panel_type == "button":
        try:
            await msg.edit(view=ReactionRoleView(msg.id))
        except:
            pass
    await interaction.response.send_message(f"✅ Panel created (ID: {msg.id})", ephemeral=True)

@bot.tree.command(name="add_reaction_role", description="Link an emoji to a role for a panel")
@app_commands.checks.has_permissions(manage_roles=True)
async def add_reaction_role(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
    panel = reaction_panels.get(str(message_id))
    if not panel:
        await interaction.response.send_message("Panel not found.", ephemeral=True); return
    panel["roles"][emoji] = str(role.id)
    save_reaction_panels()
    if panel.get("type") == "reaction":
        try:
            # attempt to find message
            for ch in interaction.guild.text_channels:
                try:
                    m = await ch.fetch_message(int(message_id))
                    await m.add_reaction(emoji)
                    break
                except:
                    continue
        except:
            pass
    else:
        try:
            for ch in interaction.guild.text_channels:
                try:
                    m = await ch.fetch_message(int(message_id))
                    await m.edit(view=ReactionRoleView(message_id))
                    break
                except:
                    continue
        except:
            pass
    await interaction.response.send_message(f"✅ Added {emoji} -> {role.name} to panel {message_id}", ephemeral=True)

# Premium toggle + example
@bot.tree.command(name="premium", description="Toggle premium utilities for server (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def premium_toggle(interaction: discord.Interaction, enable: bool):
    gcfg = guild_config(interaction.guild.id)
    gcfg["premium"] = bool(enable)
    save_json(CONFIG_FILE, config)
    await interaction.response.send_message(f"Premium utilities {'enabled' if enable else 'disabled'} for this server.", ephemeral=True)

@bot.tree.command(name="premium_info", description="(Premium) Show upgraded utilities - example")
async def premium_info(interaction: discord.Interaction):
    gcfg = guild_config(interaction.guild.id)
    if not gcfg.get("premium"):
        await interaction.response.send_message("This server does not have premium utilities enabled. Ask an admin to run `/premium true`", ephemeral=True)
        return
    await interaction.response.send_message("✨ Premium utilities active: advanced logs, priority ticket handling, extra automod rules.", ephemeral=True)

# ---------------------------
# Help view (button-based)
# ---------------------------
class HelpView(View):
    def __init__(self):
        super().__init__(timeout=120)
    async def update_embed(self, interaction: discord.Interaction, title, desc, color):
        embed = discord.Embed(title=title, description=desc, color=color)
        embed.set_footer(text="Use the buttons • Expires in 2 minutes")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Moderation ⚔️", style=discord.ButtonStyle.blurple)
    async def mod_btn(self, interaction: discord.Interaction, button: Button):
        desc = (
            "• `/kick`, `/ban`, `/unban`\n"
            "• `/warn`, `/warnings`, `/clearwarns`\n"
            "• `/timeout`, `/untimeout`, `/timeouts`\n"
            "• `/infractions` — full punishment summary\n"
            "• Auto-warn after 3 timeouts; Auto-ban after 5 warnings"
        )
        await self.update_embed(interaction, "⚔️ Moderation", desc, discord.Color.red())

    @discord.ui.button(label="Tickets & Roles 🎫", style=discord.ButtonStyle.green)
    async def ticket_btn(self, interaction: discord.Interaction, button: Button):
        desc = (
            "• `/ticket_panel` — Create ticket creation button\n"
            "• `/ticket_category` — Set a category for tickets\n"
            "• `/reaction_panel` — Create reaction/button role panel\n"
            "• `/add_reaction_role` — Link emoji -> role for a panel"
        )
        await self.update_embed(interaction, "🎫 Tickets & Roles", desc, discord.Color.green())

    @discord.ui.button(label="XP & Premium ✨", style=discord.ButtonStyle.gray)
    async def xp_btn(self, interaction: discord.Interaction, button: Button):
        desc = (
            "• Active XP system: chat messages grant XP and levels\n"
            "• Admins can configure role rewards in config.json\n"
            "• `/premium true` to enable premium utilities"
        )
        await self.update_embed(interaction, "✨ XP & Premium", desc, discord.Color.blurple())

    @discord.ui.button(label="Close ❌", style=discord.ButtonStyle.red)
    async def close_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.message.delete()

@bot.tree.command(name="help", description="Show the help menu")
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(title="🛠️ Bot Help", description="Press buttons to view categories.", color=discord.Color.blurple())
    view = HelpView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# ---------------------------
# Rotating status (auto)
# ---------------------------
async def cycle_status():
    statuses = [
        ("watching", "over the server 👀"),
        ("listening", "to your commands 🎧"),
        ("playing", "with moderation tools 🛠️"),
        ("competing", "for best bot award 🏆"),
        ("watching", "for rule breakers ⚔️"),
        ("listening", "to feedback 💬")
    ]
    await bot.wait_until_ready()
    while not bot.is_closed():
        status_type, status_text = random.choice(statuses)
        try:
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
        except Exception:
            print("Failed to change presence:", traceback.format_exc())
        await asyncio.sleep(60)  # rotate every 60 seconds

# ---------------------------
# Startup helpers
# ---------------------------
async def rebuild_views_on_startup():
    await bot.wait_until_ready()
    # Reattach views for reaction-role button panels (best-effort)
    for mid, panel in list(reaction_panels.items()):
        try:
            gid = int(panel["guild"])
            g = bot.get_guild(gid)
            if not g: continue
            for ch in g.text_channels:
                try:
                    m = await ch.fetch_message(int(mid))
                    if panel.get("type") == "button":
                        await m.edit(view=ReactionRoleView(mid))
                    break
                except:
                    continue
        except:
            continue

# ---------------------------
# Events: welcome/goodbye & ready
# ---------------------------
@bot.event
async def on_member_join(member: discord.Member):
    gcfg = guild_config(member.guild.id)
    if gcfg.get("welcome_channel"):
        ch = member.guild.get_channel(int(gcfg["welcome_channel"]))
        if ch:
            await ch.send(f"🎉 Welcome {member.mention} to **{member.guild.name}**!")
    try:
        dm_template = gcfg.get("welcome_dm", "👋 Welcome {user} to {server}!")
        message = dm_template.replace("{user}", member.name).replace("{server}", member.guild.name)
        await member.send(message)
    except:
        pass
    log_action(member.guild, f"✅ Member joined: {member}")

@bot.event
async def on_member_remove(member: discord.Member):
    gcfg = guild_config(member.guild.id)
    if gcfg.get("goodbye_channel"):
        ch = member.guild.get_channel(int(gcfg["goodbye_channel"]))
        if ch:
            await ch.send(f"👋 {member.name} has left the server.")
    log_action(member.guild, f"❌ Member left: {member}")

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await bot.tree.sync()
    except Exception:
        pass
    # start background tasks
    bot.loop.create_task(cycle_status())
    bot.loop.create_task(rebuild_views_on_startup())
    load_xp()
    print("Background tasks started.")

# ---------------------------
# Run the bot
# ---------------------------
if __name__ == "__main__":
    load_xp()
    bot.run(TOKEN)
