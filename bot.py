import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import asyncio
import re
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ── Data helpers ──────────────────────────────────────────────────────────────
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

def load_json(filename):
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)

def save_json(filename, data):
    path = os.path.join(DATA_DIR, filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── Utility: parse message link ───────────────────────────────────────────────
def parse_message_link(link: str):
    """Returns (guild_id, channel_id, message_id) or None."""
    pattern = r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
    match = re.match(pattern, link.strip())
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    return None

async def fetch_message_from_link(bot, link: str):
    """Fetch discord.Message from a message link."""
    parsed = parse_message_link(link)
    if not parsed:
        return None, "❌ Format link salah. Contoh: `https://discord.com/channels/123/456/789`"
    guild_id, channel_id, message_id = parsed
    channel = bot.get_channel(channel_id)
    if not channel:
        return None, "❌ Channel tidak ditemukan. Pastikan bot ada di server tersebut."
    try:
        msg = await channel.fetch_message(message_id)
        return msg, None
    except discord.NotFound:
        return None, "❌ Pesan tidak ditemukan."
    except discord.Forbidden:
        return None, "❌ Bot tidak punya akses ke channel tersebut."

def parse_duration(duration_str: str):
    """Parse '10m', '1h', '2d' → seconds. Returns None if invalid."""
    if not duration_str:
        return None
    pattern = r"^(\d+)(s|m|h|d)$"
    match = re.match(pattern, duration_str.strip().lower())
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]

# ── Logging helper ────────────────────────────────────────────────────────────
async def send_log(guild: discord.Guild, embed: discord.Embed):
    config = load_json("config.json")
    guild_id = str(guild.id)
    if guild_id not in config or "log_channel" not in config[guild_id]:
        return
    channel = guild.get_channel(int(config[guild_id]["log_channel"]))
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

# ── Track sent DMs ────────────────────────────────────────────────────────────
def track_dm(guild_id: int, user_id: int, dm_message: discord.Message, content: str):
    dm_log = load_json("dm_log.json")
    key = str(guild_id)
    if key not in dm_log:
        dm_log[key] = {}
    user_key = str(user_id)
    if user_key not in dm_log[key]:
        dm_log[key][user_key] = []
    dm_log[key][user_key].append({
        "dm_channel_id": dm_message.channel.id,
        "dm_message_id": dm_message.id,
        "content_preview": content[:100],
        "sent_at": datetime.utcnow().isoformat()
    })
    save_json("dm_log.json", dm_log)

# ═════════════════════════════════════════════════════════════════════════════
# GROUP: /dm
# ═════════════════════════════════════════════════════════════════════════════
dm_group = app_commands.Group(name="dm", description="Kirim DM ke member server")

@dm_group.command(name="send", description="Kirim DM ke user pakai message link")
@app_commands.describe(
    user="User yang mau di-DM",
    message_link="Link pesan yang mau dikirim sebagai isi DM",
    delete_after="Auto-hapus DM setelah waktu ini (contoh: 10m, 1h, 2d). Kosongi = pakai default server"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def dm_send(interaction: discord.Interaction, user: discord.Member, message_link: str, delete_after: str = ""):
    await interaction.response.defer(ephemeral=True)

    # Cek default duration dari config
    config = load_json("config.json")
    guild_key = str(interaction.guild.id)
    default_duration = config.get(guild_key, {}).get("default_delete_after", None)

    # Tentukan durasi
    duration_str = delete_after.strip() if delete_after.strip() else None
    seconds = None
    if duration_str:
        seconds = parse_duration(duration_str)
        if seconds is None:
            await interaction.followup.send("❌ Format waktu salah. Contoh: `10m`, `1h`, `2d`", ephemeral=True)
            return
    elif default_duration:
        seconds = parse_duration(default_duration)

    # Ambil pesan dari link
    msg, error = await fetch_message_from_link(bot, message_link)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    content = msg.content or ""
    embeds = msg.embeds

    # Kirim DM
    try:
        if embeds:
            dm_msg = await user.send(content=content if content else None, embeds=embeds)
        else:
            dm_msg = await user.send(content=content)
    except discord.Forbidden:
        await interaction.followup.send(f"❌ Tidak bisa DM {user.mention}. Mungkin DM-nya dimatiin.", ephemeral=True)
        return

    track_dm(interaction.guild.id, user.id, dm_msg, content)

    # Auto-delete jika ada durasi
    if seconds:
        asyncio.create_task(auto_delete_dm(dm_msg, seconds))

    duration_text = f" (auto-delete: `{duration_str or default_duration}`)" if seconds else ""
    await interaction.followup.send(f"✅ DM berhasil dikirim ke {user.mention}{duration_text}", ephemeral=True)

    # Log
    embed = discord.Embed(title="📨 DM Terkirim", color=discord.Color.green(), timestamp=datetime.utcnow())
    embed.add_field(name="Ke", value=f"{user} ({user.id})", inline=True)
    embed.add_field(name="Oleh", value=f"{interaction.user} ({interaction.user.id})", inline=True)
    embed.add_field(name="Isi (preview)", value=content[:200] or "*(embed)*", inline=False)
    if seconds:
        embed.add_field(name="Auto-delete", value=duration_str or default_duration, inline=True)
    await send_log(interaction.guild, embed)


@dm_group.command(name="template", description="Kirim DM ke user pakai template yang sudah disimpan")
@app_commands.describe(
    user="User yang mau di-DM",
    name="Nama template",
    delete_after="Auto-hapus DM setelah waktu ini (contoh: 10m, 1h, 2d)"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def dm_template(interaction: discord.Interaction, user: discord.Member, name: str, delete_after: str = ""):
    await interaction.response.defer(ephemeral=True)

    templates = load_json("templates.json")
    guild_key = str(interaction.guild.id)
    guild_templates = templates.get(guild_key, {})

    if name not in guild_templates:
        await interaction.followup.send(f"❌ Template `{name}` tidak ditemukan. Cek `/template list`", ephemeral=True)
        return

    message_link = guild_templates[name]
    msg, error = await fetch_message_from_link(bot, message_link)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    content = msg.content or ""
    embeds = msg.embeds

    # Durasi
    config = load_json("config.json")
    default_duration = config.get(guild_key, {}).get("default_delete_after", None)
    duration_str = delete_after.strip() if delete_after.strip() else None
    seconds = None
    if duration_str:
        seconds = parse_duration(duration_str)
        if seconds is None:
            await interaction.followup.send("❌ Format waktu salah. Contoh: `10m`, `1h`, `2d`", ephemeral=True)
            return
    elif default_duration:
        seconds = parse_duration(default_duration)

    try:
        if embeds:
            dm_msg = await user.send(content=content if content else None, embeds=embeds)
        else:
            dm_msg = await user.send(content=content)
    except discord.Forbidden:
        await interaction.followup.send(f"❌ Tidak bisa DM {user.mention}.", ephemeral=True)
        return

    track_dm(interaction.guild.id, user.id, dm_msg, content)

    if seconds:
        asyncio.create_task(auto_delete_dm(dm_msg, seconds))

    duration_text = f" (auto-delete: `{duration_str or default_duration}`)" if seconds else ""
    await interaction.followup.send(f"✅ DM template `{name}` berhasil dikirim ke {user.mention}{duration_text}", ephemeral=True)

    embed = discord.Embed(title="📨 DM Template Terkirim", color=discord.Color.blue(), timestamp=datetime.utcnow())
    embed.add_field(name="Template", value=name, inline=True)
    embed.add_field(name="Ke", value=f"{user} ({user.id})", inline=True)
    embed.add_field(name="Oleh", value=f"{interaction.user} ({interaction.user.id})", inline=True)
    if seconds:
        embed.add_field(name="Auto-delete", value=duration_str or default_duration, inline=True)
    await send_log(interaction.guild, embed)


async def auto_delete_dm(dm_msg: discord.Message, seconds: int):
    await asyncio.sleep(seconds)
    try:
        await dm_msg.delete()
    except Exception:
        pass

tree.add_command(dm_group)

# ═════════════════════════════════════════════════════════════════════════════
# GROUP: /deletedm
# ═════════════════════════════════════════════════════════════════════════════
deletedm_group = app_commands.Group(name="deletedm", description="Hapus DM yang sudah dikirim bot")

@deletedm_group.command(name="last", description="Hapus DM terakhir yang dikirim bot ke user ini")
@app_commands.describe(user="User target")
@app_commands.checks.has_permissions(manage_messages=True)
async def deletedm_last(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    dm_log = load_json("dm_log.json")
    guild_key = str(interaction.guild.id)
    user_key = str(user.id)
    entries = dm_log.get(guild_key, {}).get(user_key, [])

    if not entries:
        await interaction.followup.send(f"❌ Tidak ada DM tercatat untuk {user.mention}.", ephemeral=True)
        return

    last = entries[-1]
    try:
        dm_channel = await user.create_dm()
        msg = await dm_channel.fetch_message(last["dm_message_id"])
        await msg.delete()
        entries.pop()
        dm_log[guild_key][user_key] = entries
        save_json("dm_log.json", dm_log)
        await interaction.followup.send(f"✅ DM terakhir ke {user.mention} berhasil dihapus.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Gagal hapus DM: `{e}`", ephemeral=True)
        return

    embed = discord.Embed(title="🗑️ DM Dihapus (Terakhir)", color=discord.Color.red(), timestamp=datetime.utcnow())
    embed.add_field(name="Dari", value=f"{user} ({user.id})", inline=True)
    embed.add_field(name="Oleh", value=f"{interaction.user}", inline=True)
    await send_log(interaction.guild, embed)


@deletedm_group.command(name="all", description="Hapus semua DM yang pernah dikirim bot ke user ini")
@app_commands.describe(user="User target")
@app_commands.checks.has_permissions(manage_messages=True)
async def deletedm_all(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    dm_log = load_json("dm_log.json")
    guild_key = str(interaction.guild.id)
    user_key = str(user.id)
    entries = dm_log.get(guild_key, {}).get(user_key, [])

    if not entries:
        await interaction.followup.send(f"❌ Tidak ada DM tercatat untuk {user.mention}.", ephemeral=True)
        return

    dm_channel = await user.create_dm()
    deleted = 0
    failed = 0
    for entry in entries:
        try:
            msg = await dm_channel.fetch_message(entry["dm_message_id"])
            await msg.delete()
            deleted += 1
        except Exception:
            failed += 1

    dm_log[guild_key][user_key] = []
    save_json("dm_log.json", dm_log)

    await interaction.followup.send(
        f"✅ Selesai! {deleted} DM dihapus" + (f", {failed} gagal dihapus (mungkin sudah dihapus)." if failed else "."),
        ephemeral=True
    )

    embed = discord.Embed(title="🗑️ Semua DM Dihapus", color=discord.Color.red(), timestamp=datetime.utcnow())
    embed.add_field(name="User", value=f"{user} ({user.id})", inline=True)
    embed.add_field(name="Oleh", value=f"{interaction.user}", inline=True)
    embed.add_field(name="Hasil", value=f"{deleted} dihapus, {failed} gagal", inline=True)
    await send_log(interaction.guild, embed)

tree.add_command(deletedm_group)

# ═════════════════════════════════════════════════════════════════════════════
# GROUP: /template
# ═════════════════════════════════════════════════════════════════════════════
template_group = app_commands.Group(name="template", description="Kelola template DM")

@template_group.command(name="create", description="Buat template baru dari message link")
@app_commands.describe(name="Nama template", message_link="Link pesan sebagai isi template")
@app_commands.checks.has_permissions(manage_messages=True)
async def template_create(interaction: discord.Interaction, name: str, message_link: str):
    await interaction.response.defer(ephemeral=True)

    # Validasi link
    msg, error = await fetch_message_from_link(bot, message_link)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    templates = load_json("templates.json")
    guild_key = str(interaction.guild.id)
    if guild_key not in templates:
        templates[guild_key] = {}

    templates[guild_key][name] = message_link
    save_json("templates.json", templates)

    preview = (msg.content or "*(embed)*")[:100]
    await interaction.followup.send(f"✅ Template `{name}` disimpan!\nPreview: {preview}", ephemeral=True)


@template_group.command(name="list", description="Lihat semua template yang ada")
@app_commands.checks.has_permissions(manage_messages=True)
async def template_list(interaction: discord.Interaction):
    templates = load_json("templates.json")
    guild_key = str(interaction.guild.id)
    guild_templates = templates.get(guild_key, {})

    if not guild_templates:
        await interaction.response.send_message("📭 Belum ada template. Buat dulu pakai `/template create`", ephemeral=True)
        return

    embed = discord.Embed(title="📋 Daftar Template", color=discord.Color.blurple())
    for name, link in guild_templates.items():
        embed.add_field(name=f"`{name}`", value=link, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@template_group.command(name="delete", description="Hapus template")
@app_commands.describe(name="Nama template yang mau dihapus")
@app_commands.checks.has_permissions(manage_messages=True)
async def template_delete(interaction: discord.Interaction, name: str):
    templates = load_json("templates.json")
    guild_key = str(interaction.guild.id)

    if name not in templates.get(guild_key, {}):
        await interaction.response.send_message(f"❌ Template `{name}` tidak ditemukan.", ephemeral=True)
        return

    del templates[guild_key][name]
    save_json("templates.json", templates)
    await interaction.response.send_message(f"✅ Template `{name}` berhasil dihapus.", ephemeral=True)

tree.add_command(template_group)

# ═════════════════════════════════════════════════════════════════════════════
# GROUP: /setup
# ═════════════════════════════════════════════════════════════════════════════
setup_group = app_commands.Group(name="setup", description="Konfigurasi bot")

@setup_group.command(name="logging", description="Set channel untuk log aktivitas DM")
@app_commands.describe(channel="Channel logging")
@app_commands.checks.has_permissions(administrator=True)
async def setup_logging(interaction: discord.Interaction, channel: discord.TextChannel):
    config = load_json("config.json")
    guild_key = str(interaction.guild.id)
    if guild_key not in config:
        config[guild_key] = {}
    config[guild_key]["log_channel"] = str(channel.id)
    save_json("config.json", config)
    await interaction.response.send_message(f"✅ Log channel diset ke {channel.mention}", ephemeral=True)


@setup_group.command(name="autodelete", description="Set default waktu auto-delete DM (kosongi untuk nonaktifkan)")
@app_commands.describe(duration="Durasi default (contoh: 10m, 1h, 2d) — kosongkan untuk nonaktifkan")
@app_commands.checks.has_permissions(administrator=True)
async def setup_autodelete(interaction: discord.Interaction, duration: str = ""):
    config = load_json("config.json")
    guild_key = str(interaction.guild.id)
    if guild_key not in config:
        config[guild_key] = {}

    if duration.strip() == "":
        config[guild_key].pop("default_delete_after", None)
        save_json("config.json", config)
        await interaction.response.send_message("✅ Default auto-delete dinonaktifkan.", ephemeral=True)
        return

    seconds = parse_duration(duration)
    if seconds is None:
        await interaction.response.send_message("❌ Format salah. Contoh: `10m`, `1h`, `2d`", ephemeral=True)
        return

    config[guild_key]["default_delete_after"] = duration.strip()
    save_json("config.json", config)
    await interaction.response.send_message(f"✅ Default auto-delete diset ke `{duration}`", ephemeral=True)

tree.add_command(setup_group)

# ═════════════════════════════════════════════════════════════════════════════
# Error handler
# ═════════════════════════════════════════════════════════════════════════════
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ Kamu tidak punya permission untuk command ini.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Terjadi error: `{error}`", ephemeral=True)

# ═════════════════════════════════════════════════════════════════════════════
# on_ready
# ═════════════════════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Bot aktif sebagai {bot.user} | Slash commands synced!")

bot.run(TOKEN)
