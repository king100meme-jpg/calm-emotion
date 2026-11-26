
import asyncio
import logging
import os
import shutil
import shlex
import time

from collections import deque
from urllib.parse import urlparse

import discord
import yt_dlp

from discord.ext import commands, tasks


VERSION = "Music V3.2"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("music")


# ==========================================
# Music Extraction
# ==========================================

def resolve(query):

    options = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "socket_timeout": 30,
        "retries": 2,
        "js_runtimes": {"deno": {}}
    }

    with yt_dlp.YoutubeDL(options) as ydl:

        data = ydl.extract_info(
            query,
            download=False
        )

        if data and "entries" in data:
            data = next(
                (entry for entry in data["entries"] if entry),
                None
            )

        if not data or not data.get("url"):
            raise RuntimeError("No playable audio found")

        return data


def query_for(text, soundcloud=False):

    text = text.strip()

    if not text or len(text) > 500:
        raise ValueError("اكتب اسم المقطع أو الرابط.")

    if "://" in text:

        parsed = urlparse(text)
        host = parsed.hostname or ""

        domains = (
            "youtube.com",
            "youtu.be",
            "soundcloud.com"
        )

        if parsed.scheme not in ("http", "https") or not any(
            host == domain or host.endswith("." + domain)
            for domain in domains
        ):
            raise ValueError(
                "يدعم البوت YouTube وSoundCloud فقط."
            )

        return text

    return (
        "scsearch1:" if soundcloud else "ytsearch1:"
    ) + text


# ==========================================
# Audio Diagnostics
# ==========================================

class CheckedAudio(discord.AudioSource):

    def __init__(self, source, label):

        self.source = source
        self.label = label
        self.frames = 0

    def read(self):

        data = self.source.read()

        if data:
            self.frames += 1

            if self.frames == 1:
                log.info(
                    "%s: first audio frame received",
                    self.label
                )

        return data

    def is_opus(self):
        return self.source.is_opus()

    def cleanup(self):

        log.info(
            "%s: audio frames=%s",
            self.label,
            self.frames
        )

        self.source.cleanup()


# ==========================================
# Bot Configuration
# ==========================================

def create_bot(room_id, token_name):

    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True

    bot = commands.Bot(
        command_prefix=("#", "!"),
        intents=intents,
        help_command=None,
        allowed_mentions=discord.AllowedMentions.none()
    )

    queue = deque()
    lock = asyncio.Lock()

    current = None
    player_task = None

    enabled = True
    repeat = False
    volume = 50

    audio_mode = "pcm"

    generation = 0
    position = 0.0
    seek_offset = 0.0

    started = None
    duration = None
    track_title = None


    # ==========================================
    # Voice Connection
    # ==========================================

    def room():
        return bot.get_channel(room_id)


    def vc_now():

        channel = room()

        return (
            channel.guild.voice_client
            if channel else None
        )


    async def connect():

        channel = room()

        if not isinstance(
            channel,
            discord.VoiceChannel
        ):
            raise RuntimeError("Voice room unavailable")

        vc = vc_now()

        if vc and vc.is_connected():

            if vc.channel.id != room_id:
                await vc.move_to(channel)

        else:

            if vc:
                await vc.disconnect(force=True)

            vc = await channel.connect(
                timeout=30,
                reconnect=True,
                self_deaf=True,
                self_mute=False
            )

        if not channel.permissions_for(
            channel.guild.me
        ).speak:
            raise RuntimeError("Missing Speak permission")

        return vc


    @bot.check
    async def own_room(ctx):

        voice = getattr(
            ctx.author,
            "voice",
            None
        )

        channel = getattr(
            voice,
            "channel",
            None
        )

        return bool(
            ctx.guild
            and not ctx.author.bot
            and channel
            and channel.id == room_id
        )


    async def say(ctx, message):

        try:
            await ctx.send(message)

        except discord.HTTPException:
            log.warning(
                "%s cannot send message",
                token_name
            )


    def elapsed_time():

        if started is None:
            return position

        return position + (
            time.monotonic() - started
        )


    # ==========================================
    # Audio Engine
    # ==========================================

    def create_audio(data, offset=0, test=False):

        if test:

            url = "sine=frequency=440:duration=5"
            before = "-f lavfi"

        else:

            url = data["url"]

            before = (
                "-reconnect 1 "
                "-reconnect_streamed 1 "
                "-reconnect_delay_max 5 "
                "-rw_timeout 30000000"
            )

            headers = data.get("http_headers") or {}

            header = "".join(
                f"{key}: {value}\r\n"
                for key, value in headers.items()
                if "\r" not in str(value)
                and "\n" not in str(value)
                and "\r" not in str(key)
                and "\n" not in str(key)
            )

            if header:
                before += (
                    " -headers " + shlex.quote(header)
                )

            if offset:
                before += f" -ss {offset:.3f}"

        options = "-vn -loglevel warning"

        if audio_mode == "opus":

            source = discord.FFmpegOpusAudio(
                url,
                before_options=before,
                options=(
                    options
                    + f" -af volume={volume / 100:.2f}"
                ),
                codec="libopus",
                bitrate=128,
                stderr=None
            )

        else:

            source = discord.FFmpegPCMAudio(
                url,
                before_options=before,
                options=options,
                stderr=None
            )

            source = discord.PCMVolumeTransformer(
                source,
                volume=volume / 100
            )

        return CheckedAudio(
            source,
            token_name
        )


    # ==========================================
    # Music Player
    # ==========================================

    async def player():

        nonlocal current, player_task
        nonlocal position, started, seek_offset
        nonlocal duration, track_title

        try:

            while True:

                async with lock:

                    if not queue or not enabled:
                        return

                    item = queue.popleft()
                    current = item
                    ticket = generation

                    offset = seek_offset
                    seek_offset = 0.0

                    position = offset
                    started = None
                    duration = None
                    track_title = None

                query, ctx = item

                source = None

                try:

                    is_test = query == "__test__"

                    log.info(
                        "%s preparing audio mode=%s",
                        token_name,
                        audio_mode
                    )

                    if is_test:

                        data = None

                    else:

                        data = await asyncio.to_thread(
                            resolve,
                            query
                        )

                    async with lock:

                        if ticket != generation or not enabled:
                            continue

                        vc = await connect()

                        if is_test:

                            title = "اختبار الصوت"
                            duration = 5

                        else:

                            title = str(
                                data.get("title")
                                or "مقطع صوتي"
                            )[:180]

                            duration = data.get("duration")

                        source = create_audio(
                            data,
                            offset=offset,
                            test=is_test
                        )

                        loop = asyncio.get_running_loop()
                        done = loop.create_future()

                        def after(
                            error,
                            future=done,
                            event_loop=loop,
                            audio=source
                        ):

                            def finish():

                                if not future.done():

                                    if (
                                        error is None
                                        and audio.frames == 0
                                    ):
                                        future.set_result(
                                            RuntimeError(
                                                "FFmpeg returned no audio frames"
                                            )
                                        )
                                    else:
                                        future.set_result(error)

                            event_loop.call_soon_threadsafe(
                                finish
                            )

                        vc.play(
                            source,
                            after=after
                        )

                        source = None

                        track_title = title
                        started = time.monotonic()

                        log.info(
                            "%s PLAYING | mode=%s | volume=%s",
                            token_name,
                            audio_mode,
                            volume
                        )

                    try:

                        if not offset:

                            await ctx.send(
                                embed=panel_embed(),
                                view=Controls()
                            )

                    except discord.HTTPException:

                        log.warning(
                            "Cannot send player panel"
                        )

                    error = await done

                    if error:
                        raise error

                    log.info(
                        "%s playback finished",
                        token_name
                    )

                    async with lock:

                        if (
                            repeat
                            and ticket == generation
                            and enabled
                            and not is_test
                        ):
                            queue.appendleft(item)

                except Exception as exc:

                    if source:
                        source.cleanup()

                    # Avoid printing signed media URLs.
                    message = str(exc)

                    if "http://" in message or "https://" in message:
                        message = "Media URL omitted"

                    log.error(
                        "%s PLAYBACK ERROR: %s: %s",
                        token_name,
                        type(exc).__name__,
                        message
                    )

                    if ticket == generation:

                        await say(
                            ctx,
                            "❌ تعذر التشغيل: "
                            + type(exc).__name__
                            + "\nراجع Railway Logs."
                        )

                finally:

                    current = None
                    started = None
                    track_title = None

        finally:

            player_task = None

            if queue and enabled:
                start_player()


    def start_player():

        nonlocal player_task

        if player_task is None or player_task.done():

            player_task = asyncio.create_task(
                player()
            )


    # ==========================================
    # Seek And Volume
    # ==========================================

    def restart_at(seconds):

        nonlocal generation, seek_offset

        vc = vc_now()

        if (
            not current
            or not track_title
            or current[0] == "__test__"
            or not duration
            or not vc
            or not (
                vc.is_playing()
                or vc.is_paused()
            )
        ):
            return False

        seek_offset = max(
            0,
            min(
                max(0, duration - 1),
                seconds
            )
        )

        generation += 1

        queue.appendleft(current)
        vc.stop()

        return True


    def change_volume(level):

        nonlocal volume

        volume = max(0, min(100, level))

        vc = vc_now()

        if vc and isinstance(
            vc.source,
            CheckedAudio
        ):

            inner = vc.source.source

            if isinstance(
                inner,
                discord.PCMVolumeTransformer
            ):

                inner.volume = volume / 100

                return True

            return restart_at(elapsed_time())

        return False


    # ==========================================
    # Auto Reconnect
    # ==========================================

    @tasks.loop(seconds=15)
    async def maintain():

        if not enabled or not bot.is_ready():
            return

        try:

            async with lock:
                await connect()

        except Exception as exc:

            log.error(
                "%s VOICE ERROR: %s",
                token_name,
                str(exc)
            )


    @bot.event
    async def on_ready():

        log.info(
            "%s | %s | FFmpeg=%s | Discord=%s",
            VERSION,
            token_name,
            shutil.which("ffmpeg"),
            discord.__version__
        )

        await bot.change_presence(
            activity=discord.Game(
                name="#ش | #sc | #تحكم"
            )
        )

        if not maintain.is_running():
            maintain.start()


    # ==========================================
    # Music Commands
    # ==========================================

    @bot.command(
        name="ش",
        aliases=["play", "yt", "sc"]
    )
    async def play(ctx, *, text=""):

        nonlocal enabled

        try:

            query = query_for(
                text,
                ctx.invoked_with == "sc"
            )

        except ValueError as exc:

            return await say(
                ctx,
                str(exc)
            )

        async with lock:

            if len(queue) >= 30:

                return await say(
                    ctx,
                    "❌ القائمة ممتلئة."
                )

            enabled = True

            queue.append((query, ctx))
            start_player()

        await say(
            ctx,
            "✅ تمت إضافة المقطع للقائمة."
        )


    @bot.command(
        name="اختبار",
        aliases=["test"]
    )
    async def test(ctx):

        nonlocal enabled

        async with lock:

            if current or queue:

                return await say(
                    ctx,
                    "استخدم #وقف أولاً."
                )

            enabled = True

            queue.append(
                ("__test__", ctx)
            )

            start_player()

        await say(
            ctx,
            f"🔊 اختبار الصوت لمدة 5 ثوان | {audio_mode}"
        )


    @bot.command(
        name="نظام",
        aliases=["codec"]
    )
    async def set_codec(ctx, mode=""):

        nonlocal audio_mode

        mode = mode.lower()

        if mode not in ("pcm", "opus"):

            return await say(
                ctx,
                "استخدم #نظام pcm أو #نظام opus"
            )

        async with lock:

            if current or queue:

                return await say(
                    ctx,
                    "استخدم #وقف أولاً."
                )

            audio_mode = mode

        await say(
            ctx,
            f"✅ نظام الصوت: {audio_mode}"
        )


    @bot.command(
        name="وقف",
        aliases=["stop"]
    )
    async def stop(ctx):

        nonlocal generation, repeat, seek_offset

        async with lock:

            generation += 1
            seek_offset = 0
            repeat = False

            queue.clear()

            vc = vc_now()

            if vc:
                vc.stop()

        await say(
            ctx,
            "⏹️ تم إيقاف التشغيل."
        )


    @bot.command(
        name="تخطي",
        aliases=["skip"]
    )
    async def skip(ctx):

        nonlocal generation, seek_offset

        async with lock:

            generation += 1
            seek_offset = 0

            vc = vc_now()

            if vc:
                vc.stop()

        await say(
            ctx,
            "⏭️ تم التخطي."
        )


    @bot.command(
        name="مؤقت",
        aliases=["pause"]
    )
    async def pause(ctx):

        nonlocal position, started

        async with lock:

            vc = vc_now()

            if vc and vc.is_playing():

                position = elapsed_time()
                started = None

                vc.pause()

                message = "⏸️ تم الإيقاف المؤقت."

            else:

                message = "لا يوجد مقطع يعمل."

        await say(ctx, message)


    @bot.command(
        name="كمل",
        aliases=["resume"]
    )
    async def resume(ctx):

        nonlocal started

        async with lock:

            vc = vc_now()

            if vc and vc.is_paused():

                vc.resume()
                started = time.monotonic()

                message = "▶️ تم الاستكمال."

            else:

                message = "لا يوجد مقطع متوقف."

        await say(ctx, message)


    @bot.command(
        name="صوت",
        aliases=["volume"]
    )
    async def set_volume(ctx, level: int = 50):

        async with lock:

            applied = change_volume(level)

        message = f"🔊 الصوت: {volume}%"

        if not applied:
            message += " | يطبق عند التشغيل القادم."

        await say(ctx, message)


    @bot.command(
        name="تكرار",
        aliases=["loop"]
    )
    async def toggle_repeat(ctx):

        nonlocal repeat

        repeat = not repeat

        await say(
            ctx,
            "🔁 التكرار: "
            + ("مفعل" if repeat else "متوقف")
        )


    @bot.command(
        name="قائمة",
        aliases=["queue"]
    )
    async def list_queue(ctx):

        lines = [
            f"{i}. {q[:100]}"
            for i, (q, _) in enumerate(queue, 1)
        ]

        await say(
            ctx,
            "\n".join(lines)[:1900]
            if lines else "القائمة فارغة."
        )


    @bot.command(
        name="دخول",
        aliases=["join"]
    )
    async def join(ctx):

        nonlocal enabled

        async with lock:

            enabled = True
            await connect()

        await say(
            ctx,
            "✅ متصل بالروم المخصص."
        )


    @bot.command(
        name="طلوع",
        aliases=["leave"]
    )
    async def leave(ctx):

        nonlocal enabled, generation
        nonlocal repeat, seek_offset

        async with lock:

            enabled = False
            generation += 1
            seek_offset = 0
            repeat = False

            queue.clear()

            vc = vc_now()

            if vc:

                vc.stop()

                await vc.disconnect(
                    force=True
                )

        await say(
            ctx,
            "👋 تم الخروج من الروم."
        )


    # ==========================================
    # Control Panel
    # ==========================================

    def panel_embed():

        elapsed = int(elapsed_time())
        vc = vc_now()

        if vc and vc.is_paused():

            status = "متوقف مؤقتاً"

        elif vc and vc.is_playing():

            status = "يعمل"

        else:

            status = "لا يوجد تشغيل"

        embed = discord.Embed(
            title="🎵 لوحة التحكم بالأغاني",
            description=discord.utils.escape_markdown(
                track_title or "لا يوجد مقطع"
            ),
            color=0x5865F2
        )

        embed.add_field(
            name="الحالة",
            value=status
        )

        embed.add_field(
            name="الصوت",
            value=f"{volume}%"
        )

        embed.add_field(
            name="الانتظار",
            value=str(len(queue))
        )

        embed.add_field(
            name="الوقت التقريبي",
            value=f"{elapsed // 60}:{elapsed % 60:02d}"
        )

        embed.add_field(
            name="التكرار",
            value="مفعل" if repeat else "متوقف"
        )

        embed.add_field(
            name="نظام الصوت",
            value=audio_mode.upper()
        )

        embed.set_footer(
            text=VERSION
        )

        return embed


    # ==========================================
    # Buttons
    # ==========================================

    class Controls(discord.ui.View):

        def __init__(self):

            super().__init__(
                timeout=3600
            )


        async def interaction_check(self, interaction):

            voice = getattr(
                interaction.user,
                "voice",
                None
            )

            channel = getattr(
                voice,
                "channel",
                None
            )

            if channel and channel.id == room_id:
                return True

            await interaction.response.send_message(
                "ادخل الروم المخصص للبوت.",
                ephemeral=True
            )

            return False


        async def action(self, interaction, action):

            nonlocal position, started
            nonlocal generation, seek_offset, repeat

            await interaction.response.defer()

            msg = None

            async with lock:

                vc = vc_now()

                if action == "toggle":

                    if vc and vc.is_paused():

                        vc.resume()
                        started = time.monotonic()

                    elif vc and vc.is_playing():

                        position = elapsed_time()
                        started = None
                        vc.pause()

                    else:

                        msg = "لا يوجد مقطع يعمل."

                elif action in ("back", "forward"):

                    seconds = elapsed_time() + (
                        10 if action == "forward" else -10
                    )

                    if restart_at(seconds):

                        msg = (
                            f"الانتقال إلى {int(seek_offset)} ثانية."
                        )

                    else:

                        msg = "التقديم والترجيع غير متاح."

                elif action in ("down", "up"):

                    applied = change_volume(
                        volume + (
                            10 if action == "up" else -10
                        )
                    )

                    msg = f"🔊 الصوت: {volume}%"

                    if not applied:
                        msg += " | يطبق عند التشغيل القادم."

                elif action in ("skip", "stop"):

                    generation += 1
                    seek_offset = 0

                    if action == "stop":

                        repeat = False
                        queue.clear()

                    if vc:
                        vc.stop()

                elif action == "repeat":

                    repeat = not repeat

                elif action == "queue":

                    msg = (
                        "\n".join(
                            f"{i}. {q[:100]}"
                            for i, (q, _) in enumerate(queue, 1)
                        )
                        or "القائمة فارغة."
                    )[:1900]

            await interaction.edit_original_response(
                embed=panel_embed(),
                view=self
            )

            if msg:

                await interaction.followup.send(
                    msg,
                    ephemeral=True
                )


        @discord.ui.button(
            label="⏪ 10 ث",
            style=discord.ButtonStyle.secondary,
            row=0
        )
        async def button_0(self, interaction, button):

            await self.action(
                interaction,
                "back"
            )


        @discord.ui.button(
            label="⏯ تشغيل / مؤقت",
            style=discord.ButtonStyle.secondary,
            row=0
        )
        async def button_1(self, interaction, button):

            await self.action(
                interaction,
                "toggle"
            )


        @discord.ui.button(
            label="⏩ 10 ث",
            style=discord.ButtonStyle.secondary,
            row=0
        )
        async def button_2(self, interaction, button):

            await self.action(
                interaction,
                "forward"
            )


        @discord.ui.button(
            label="⏭ التالي",
            style=discord.ButtonStyle.secondary,
            row=0
        )
        async def button_3(self, interaction, button):

            await self.action(
                interaction,
                "skip"
            )


        @discord.ui.button(
            label="⏹ إيقاف",
            style=discord.ButtonStyle.danger,
            row=0
        )
        async def button_4(self, interaction, button):

            await self.action(
                interaction,
                "stop"
            )


        @discord.ui.button(
            label="🔉 -10%",
            style=discord.ButtonStyle.secondary,
            row=1
        )
        async def button_5(self, interaction, button):

            await self.action(
                interaction,
                "down"
            )


        @discord.ui.button(
            label="🔊 +10%",
            style=discord.ButtonStyle.secondary,
            row=1
        )
        async def button_6(self, interaction, button):

            await self.action(
                interaction,
                "up"
            )


        @discord.ui.button(
            label="🔁 تكرار",
            style=discord.ButtonStyle.secondary,
            row=1
        )
        async def button_7(self, interaction, button):

            await self.action(
                interaction,
                "repeat"
            )


        @discord.ui.button(
            label="📋 القائمة",
            style=discord.ButtonStyle.secondary,
            row=1
        )
        async def button_8(self, interaction, button):

            await self.action(
                interaction,
                "queue"
            )


        @discord.ui.button(
            label="🔄 تحديث",
            style=discord.ButtonStyle.secondary,
            row=1
        )
        async def button_9(self, interaction, button):

            await self.action(
                interaction,
                "refresh"
            )


    @bot.command(
        name="تحكم",
        aliases=["panel"]
    )
    async def control_panel(ctx):

        await ctx.send(
            embed=panel_embed(),
            view=Controls()
        )


    @bot.command(
        name="مساعدة",
        aliases=["help"]
    )
    async def help_cmd(ctx):

        await say(
            ctx,
            "**🎵 Music Commands**\n\n"
            "#ش اسم الأغنية\n"
            "#sc اسم الأغنية\n"
            "#تحكم\n"
            "#اختبار\n"
            "#نظام pcm\n"
            "#نظام opus\n"
            "#وقف\n"
            "#تخطي\n"
            "#مؤقت\n"
            "#كمل\n"
            "#صوت 50\n"
            "#تكرار\n"
            "#قائمة\n"
            "#دخول\n"
            "#طلوع"
        )


    @bot.event
    async def on_command_error(ctx, error):

        if isinstance(
            error,
            (
                commands.CheckFailure,
                commands.CommandNotFound
            )
        ):
            return

        log.error(
            "%s COMMAND ERROR: %s",
            token_name,
            type(error).__name__
        )

        await say(
            ctx,
            "❌ تعذر تنفيذ الأمر: "
            + type(error).__name__
        )


    return bot


# ==========================================
# Run Bot
# ==========================================

def run(room_id, token_name):

    token = os.getenv(token_name)

    if not token:

        raise RuntimeError(
            f"Missing environment variable: {token_name}"
        )

    if not shutil.which("ffmpeg"):

        raise RuntimeError(
            "FFmpeg missing"
        )

    create_bot(
        room_id,
        token_name
    ).run(token)
