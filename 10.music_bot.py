
import asyncio
import os
import shutil
import discord
import yt_dlp
from discord.ext import commands
from urllib.parse import urlparse

# ==========================================
# Bot Configuration
# ==========================================

COMMAND_PREFIX = "#"
VOICE_CHANNEL_ID = 1559179128807227523
TOKEN = os.getenv("TOKEN_BOT_1")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=COMMAND_PREFIX,
    intents=intents
)

music_lock = asyncio.Lock()

# ==========================================
# Audio Configuration
# ==========================================

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": False,
    "no_warnings": False,
    "socket_timeout": 30,
    "retries": 3,
    "js_runtimes": {"deno": {}}
}

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_delay_max 5"
    ),
    "options": "-vn -af volume=0.8"
}

# ==========================================
# Room Protection
# ==========================================

@bot.check
async def check_bot_room(ctx):
    if ctx.author.bot or ctx.guild is None:
        return False

    if not ctx.author.voice:
        return False

    return ctx.author.voice.channel.id == VOICE_CHANNEL_ID


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        return

    if isinstance(error, commands.CommandNotFound):
        return

    print(f"Command Error: {error}", flush=True)

# ==========================================
# Music Extraction
# ==========================================

def extract_music(query):
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ytdl:
        data = ytdl.extract_info(
            query,
            download=False
        )

        if not data:
            return None

        if "entries" in data:
            return next(
                (entry for entry in data["entries"] if entry),
                None
            )

        return data


def playback_finished(error):
    if error:
        print(f"Playback Error: {error}", flush=True)
    else:
        print("Playback ended.", flush=True)

# ==========================================
# Automatic Voice Connection
# ==========================================

async def connect_voice():
    channel = bot.get_channel(VOICE_CHANNEL_ID)

    if not isinstance(channel, discord.VoiceChannel):
        raise RuntimeError("Voice channel not found")

    vc = channel.guild.voice_client

    if vc and vc.is_connected():
        if vc.channel.id != VOICE_CHANNEL_ID:
            await vc.move_to(channel)

        return vc

    if vc:
        await vc.disconnect(force=True)

    return await channel.connect(
        timeout=30,
        reconnect=True,
        self_deaf=True
    )


@bot.event
async def on_ready():
    print(f"Bot Online: {bot.user}", flush=True)
    print(f"FFmpeg: {shutil.which('ffmpeg')}", flush=True)
    print(f"Deno: {shutil.which('deno')}", flush=True)
    print(f"Discord.py: {discord.__version__}", flush=True)

    await bot.change_presence(
        activity=discord.Game(name="#ش | #sc")
    )

    try:
        async with music_lock:
            vc = await connect_voice()

        print(
            f"Connected: {vc.channel.name}",
            flush=True
        )

    except Exception as error:
        print(f"Voice Error: {error}", flush=True)

# ==========================================
# Play Music
# ==========================================

@bot.command(
    name="ش",
    aliases=["play", "yt", "sc"]
)
async def play_music(ctx, *, query: str = None):

    if not query or not query.strip():
        return await ctx.reply(
            "❌ اكتب اسم الأغنية أو الرابط!"
        )

    if not shutil.which("ffmpeg"):
        return await ctx.reply(
            "❌ FFmpeg غير مثبت على الاستضافة."
        )

    query = query.strip()

    if query.startswith(("https://", "http://")):

        host = urlparse(query).hostname or ""

        allowed_domains = (
            "youtube.com",
            "youtu.be",
            "soundcloud.com"
        )

        if not any(
            host == domain or host.endswith("." + domain)
            for domain in allowed_domains
        ):
            return await ctx.reply(
                "❌ يدعم البوت YouTube وSoundCloud فقط."
            )

        search_query = query

    elif ctx.invoked_with == "sc":
        search_query = f"scsearch1:{query}"

    else:
        search_query = f"ytsearch1:{query}"

    async with music_lock:
        try:
            async with ctx.typing():

                song = await asyncio.to_thread(
                    extract_music,
                    search_query
                )

                if not song or not song.get("url"):
                    return await ctx.reply(
                        "❌ لم يتم العثور على المقطع."
                    )

                if (
                    not ctx.author.voice
                    or ctx.author.voice.channel.id
                    != VOICE_CHANNEL_ID
                ):
                    return

                vc = await connect_voice()

                if vc.is_playing() or vc.is_paused():
                    vc.stop()

                song_url = song["url"]
                title = song.get("title", "Unknown")

                audio = discord.FFmpegOpusAudio(
                    song_url,
                    codec="libopus",
                    bitrate=128,
                    **FFMPEG_OPTIONS
                )

                try:
                    vc.play(
                        audio,
                        after=playback_finished
                    )
                except Exception:
                    audio.cleanup()
                    raise

                print(
                    f"Playing: {title}",
                    flush=True
                )

                await ctx.reply(
                    f"🎵 جاري التشغيل: **{title}**",
                    mention_author=False
                )

        except Exception as error:
            print(
                f"Music Error: {error}",
                flush=True
            )

            await ctx.reply(
                "❌ تعذر تشغيل المقطع. راجع Deploy Logs."
            )

# ==========================================
# Join Voice
# ==========================================

@bot.command(name="دخول", aliases=["join"])
async def join_voice(ctx):
    async with music_lock:
        try:
            await connect_voice()
            await ctx.reply("✅ تم الاتصال بالروم.")
        except Exception as error:
            print(f"Join Error: {error}", flush=True)
            await ctx.reply("❌ فشل الاتصال.")

# ==========================================
# Stop Music
# ==========================================

@bot.command(name="وقف", aliases=["stop"])
async def stop_music(ctx):
    async with music_lock:
        vc = ctx.voice_client

        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await ctx.reply("⏹️ تم إيقاف الأغنية.")
        else:
            await ctx.reply("❌ لا توجد أغنية تعمل.")

# ==========================================
# Pause Music
# ==========================================

@bot.command(name="مؤقت", aliases=["pause"])
async def pause_music(ctx):
    vc = ctx.voice_client

    if vc and vc.is_playing():
        vc.pause()
        await ctx.reply("⏸️ تم الإيقاف المؤقت.")
    else:
        await ctx.reply("❌ لا توجد أغنية تعمل.")

# ==========================================
# Resume Music
# ==========================================

@bot.command(name="كمل", aliases=["resume"])
async def resume_music(ctx):
    vc = ctx.voice_client

    if vc and vc.is_paused():
        vc.resume()
        await ctx.reply("▶️ تم استكمال الأغنية.")
    else:
        await ctx.reply("❌ لا توجد أغنية متوقفة.")

# ==========================================
# Leave Voice
# ==========================================

@bot.command(name="طلوع", aliases=["leave"])
async def leave_voice(ctx):
    async with music_lock:
        vc = ctx.voice_client

        if vc:
            await vc.disconnect(force=True)
            await ctx.reply("👋 تم مغادرة الروم.")
        else:
            await ctx.reply("❌ البوت غير متصل.")

# ==========================================
# Start Bot
# ==========================================

if not TOKEN:
    raise RuntimeError("TOKEN_BOT_1 not found!")

bot.run(TOKEN)
