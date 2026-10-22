
import os
import re
import json
import time
import random
import asyncio
import threading
import shutil
from dataclasses import dataclass, asdict, field
from datetime import timedelta
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import chess
import chess.engine
import chess.pgn

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import uvicorn
from dotenv import load_dotenv

from economy_casino import setup_economy


# =========================================================
# CONFIG
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "").strip()
PANEL_SECRET = os.getenv("PANEL_SECRET", "change-this-secret").strip()
PANEL_HOST = os.getenv("PANEL_HOST", "127.0.0.1").strip()
PANEL_PORT = int(os.getenv("PANEL_PORT", "8000"))

OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
MOD_LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID", "0") or 0)

FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg").strip()

# Opsiyonel: Stockfish yolu. Boşsa dahili hafif satranç botu kullanılır.
STOCKFISH_PATH = os.getenv("STOCKFISH_PATH", "").strip()

AUTOMOD_ENABLED = os.getenv("AUTOMOD_ENABLED", "false").lower() == "true"
BLOCK_INVITES = os.getenv("BLOCK_INVITES", "false").lower() == "true"
MAX_MENTIONS = int(os.getenv("MAX_MENTIONS", "5"))

BAD_WORDS = [
    x.strip().lower()
    for x in os.getenv("BAD_WORDS", "").split(",")
    if x.strip()
]

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN .env dosyasında yok.")

if not PANEL_PASSWORD:
    raise RuntimeError("PANEL_PASSWORD .env dosyasında yok.")


# =========================================================
# DISCORD
# =========================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)

BOT_LOOP: Optional[asyncio.AbstractEventLoop] = None


# =========================================================
# YT-DLP
# =========================================================

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": False,
    "noplaylist": False,
    "default_search": "ytsearch1",
    "extract_flat": False,
}

FFMPEG_BASE_BEFORE = (
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_delay_max 5"
)

FFMPEG_OPTIONS = {
    "options": "-vn"
}


def fmt_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "?"

    try:
        seconds = int(seconds)
    except Exception:
        return "?"

    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)

    if h:
        return f"{h}:{m:02d}:{s:02d}"

    return f"{m}:{s:02d}"


@dataclass
class Song:
    title: str
    webpage_url: str
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    requested_by: str = "Web Panel"

    def public(self):
        d = asdict(self)
        d["duration_text"] = fmt_duration(self.duration)
        return d


async def ytdlp_extract(query: str):
    loop = asyncio.get_running_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            return ydl.extract_info(query, download=False)

    return await loop.run_in_executor(None, _extract)


async def make_song_list(
    query: str,
    requested_by: str,
    max_playlist: int = 50
):
    raw_query = query.strip()

    if not raw_query:
        raise RuntimeError("Şarkı adı veya URL gir.")

    if not re.match(r"^https?://", raw_query):
        raw_query = f"ytsearch1:{raw_query}"

    info = await ytdlp_extract(raw_query)

    if not info:
        raise RuntimeError("Sonuç bulunamadı.")

    entries = info.get("entries")

    if entries:
        songs = []

        for entry in entries[:max_playlist]:
            if not entry:
                continue

            url = entry.get("webpage_url") or entry.get("url")

            if not url:
                continue

            if not str(url).startswith("http"):
                url = f"https://www.youtube.com/watch?v={url}"

            songs.append(
                Song(
                    title=entry.get("title") or "Bilinmeyen şarkı",
                    webpage_url=url,
                    duration=entry.get("duration"),
                    thumbnail=entry.get("thumbnail"),
                    requested_by=requested_by,
                )
            )

        if songs:
            return songs

    url = (
        info.get("webpage_url")
        or info.get("original_url")
        or info.get("url")
    )

    if not url:
        raise RuntimeError("Medya URL'si bulunamadı.")

    return [
        Song(
            title=info.get("title") or "Bilinmeyen şarkı",
            webpage_url=url,
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail"),
            requested_by=requested_by,
        )
    ]


async def resolve_stream_url(webpage_url: str):
    info = await ytdlp_extract(webpage_url)

    if info.get("entries"):
        info = next(
            (x for x in info["entries"] if x),
            None
        )

    if not info:
        raise RuntimeError("Stream bilgisi alınamadı.")

    url = info.get("url")

    if not url:
        formats = info.get("formats") or []

        audio_formats = [
            f for f in formats
            if f.get("url")
            and f.get("acodec") != "none"
        ]

        if audio_formats:
            url = audio_formats[-1]["url"]

    if not url:
        raise RuntimeError("Ses stream URL'si bulunamadı.")

    return url


# =========================================================
# MUSIC MANAGER
# =========================================================

class MusicManager:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: list[Song] = []
        self.current: Optional[Song] = None

        self.volume = 0.60
        self.loop_mode = "off"

        self.text_channel_id: Optional[int] = None

        self.skip_requested = False
        self.restart_requested: Optional[int] = None

        self.current_offset = 0
        self.current_started_at: Optional[float] = None

        self.idle_task: Optional[asyncio.Task] = None

        self.last_error: Optional[str] = None

    @property
    def guild(self):
        return bot.get_guild(self.guild_id)

    @property
    def voice(self):
        guild = self.guild
        return guild.voice_client if guild else None

    async def ensure_voice(self, voice_channel_id: int):
        guild = self.guild

        if not guild:
            raise RuntimeError("Sunucu bulunamadı.")

        voice_channel_id = int(voice_channel_id)

        channel = guild.get_channel(voice_channel_id)

        if channel is None:
            try:
                channel = await guild.fetch_channel(voice_channel_id)
            except Exception as e:
                raise RuntimeError(
                    f"Ses kanalı bulunamadı. "
                    f"ID: {voice_channel_id} | {e}"
                )

        print(
            "VOICE DEBUG:",
            "ID =", voice_channel_id,
            "NAME =", getattr(channel, "name", None),
            "TYPE =", type(channel).__name__,
        )

        if not isinstance(
            channel,
            (discord.VoiceChannel, discord.StageChannel)
        ):
            raise RuntimeError(
                f"Geçerli bir ses kanalı seç. "
                f"Seçilen: {getattr(channel, 'name', 'Bilinmiyor')} "
                f"| Tip: {type(channel).__name__} "
                f"| ID: {voice_channel_id}"
            )

        if guild.voice_client:
            if guild.voice_client.channel.id != channel.id:
                await guild.voice_client.move_to(channel)
        else:
            await channel.connect()

        return guild.voice_client

    async def enqueue(
        self,
        query: str,
        voice_channel_id: int,
        requested_by: str,
        text_channel_id: Optional[int] = None,
    ):
        await self.ensure_voice(voice_channel_id)

        if text_channel_id:
            self.text_channel_id = int(text_channel_id)

        songs = await make_song_list(
            query,
            requested_by,
        )

        self.queue.extend(songs)

        voice = self.voice

        if (
            voice
            and not voice.is_playing()
            and not voice.is_paused()
            and self.current is None
        ):
            await self.play_next()

        return songs

    async def _announce(
        self,
        text: str = "",
        embed: Optional[discord.Embed] = None
    ):
        if not self.text_channel_id:
            return

        channel = bot.get_channel(self.text_channel_id)

        if channel and hasattr(channel, "send"):
            try:
                await channel.send(
                    content=text or None,
                    embed=embed
                )
            except Exception:
                pass

    async def _start_song(
        self,
        song: Song,
        offset: int = 0
    ):
        voice = self.voice

        if not voice:
            raise RuntimeError("Bot ses kanalında değil.")

        stream_url = await resolve_stream_url(
            song.webpage_url
        )

        before = FFMPEG_BASE_BEFORE

        if offset > 0:
            before = f"-ss {int(offset)} {before}"

        source = discord.FFmpegPCMAudio(
            stream_url,
            executable=FFMPEG_PATH,
            before_options=before,
            **FFMPEG_OPTIONS,
        )

        source = discord.PCMVolumeTransformer(
            source,
            volume=self.volume
        )

        self.current = song
        self.current_offset = int(offset)
        self.current_started_at = time.monotonic()
        self.last_error = None

        def after_play(error):
            if BOT_LOOP is None:
                return

            asyncio.run_coroutine_threadsafe(
                self._after_song(error),
                BOT_LOOP
            )

        voice.play(
            source,
            after=after_play
        )

        embed = discord.Embed(
            title="🎵 Şimdi Çalıyor",
            description=f"**{song.title}**",
            url=song.webpage_url,
        )

        embed.add_field(
            name="Süre",
            value=fmt_duration(song.duration),
            inline=True
        )

        embed.add_field(
            name="Ekleyen",
            value=song.requested_by,
            inline=True
        )

        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)

        await self._announce(embed=embed)

    async def play_next(self):
        voice = self.voice

        if not voice:
            self.current = None
            return

        if not self.queue:
            self.current = None
            self.current_started_at = None
            self._schedule_idle_disconnect()
            return

        song = self.queue.pop(0)

        try:
            await self._start_song(song)

        except Exception as e:
            self.last_error = str(e)

            await self._announce(
                f"❌ Oynatma hatası: `{e}`"
            )

            self.current = None

            await self.play_next()

    async def _after_song(self, error):
        if error:
            self.last_error = str(error)

        if (
            self.restart_requested is not None
            and self.current is not None
        ):
            offset = self.restart_requested
            self.restart_requested = None

            song = self.current

            try:
                await self._start_song(
                    song,
                    offset=offset
                )

            except Exception as e:
                self.last_error = str(e)
                self.current = None
                await self.play_next()

            return

        old = self.current

        self.current = None
        self.current_started_at = None

        if old and not self.skip_requested:
            if self.loop_mode == "track":
                self.queue.insert(0, old)

            elif self.loop_mode == "queue":
                self.queue.append(old)

        self.skip_requested = False

        await self.play_next()

    def _schedule_idle_disconnect(self):
        if self.idle_task and not self.idle_task.done():
            return

        async def idle():
            await asyncio.sleep(300)

            voice = self.voice

            if (
                voice
                and not voice.is_playing()
                and not voice.is_paused()
                and not self.queue
            ):
                await voice.disconnect()

        self.idle_task = asyncio.create_task(idle())

    async def pause(self):
        if self.voice and self.voice.is_playing():
            self.voice.pause()
            return True

        return False

    async def resume(self):
        if self.voice and self.voice.is_paused():
            self.voice.resume()
            return True

        return False

    async def skip(self):
        if (
            self.voice
            and (
                self.voice.is_playing()
                or self.voice.is_paused()
            )
        ):
            self.skip_requested = True
            self.voice.stop()
            return True

        return False

    async def stop(self):
        self.queue.clear()
        self.skip_requested = True
        self.restart_requested = None

        if (
            self.voice
            and (
                self.voice.is_playing()
                or self.voice.is_paused()
            )
        ):
            self.voice.stop()

        else:
            self.current = None

        return True

    async def leave(self):
        await self.stop()

        if self.voice:
            await self.voice.disconnect()

        self.current = None

    async def set_volume(self, percent: int):
        percent = max(
            0,
            min(200, int(percent))
        )

        self.volume = percent / 100.0

        if (
            self.voice
            and isinstance(
                self.voice.source,
                discord.PCMVolumeTransformer
            )
        ):
            self.voice.source.volume = self.volume

        return percent

    async def seek(self, seconds: int):
        if not self.current or not self.voice:
            raise RuntimeError("Çalan şarkı yok.")

        seconds = max(0, int(seconds))

        if self.current.duration:
            seconds = min(
                seconds,
                max(0, self.current.duration - 1)
            )

        self.restart_requested = seconds
        self.voice.stop()

        return seconds

    async def replay(self):
        return await self.seek(0)

    def shuffle(self):
        random.shuffle(self.queue)

    def clear(self):
        self.queue.clear()

    def remove(self, index: int):
        idx = index - 1

        if idx < 0 or idx >= len(self.queue):
            raise RuntimeError("Geçersiz sıra numarası.")

        return self.queue.pop(idx)

    def move(
        self,
        old_index: int,
        new_index: int
    ):
        old = old_index - 1
        new = new_index - 1

        if (
            not (0 <= old < len(self.queue))
            or not (0 <= new < len(self.queue))
        ):
            raise RuntimeError("Geçersiz sıra numarası.")

        song = self.queue.pop(old)
        self.queue.insert(new, song)

    def state(self):
        voice = self.voice

        elapsed = None

        if (
            self.current
            and self.current_started_at
            and voice
            and not voice.is_paused()
        ):
            elapsed = (
                int(time.monotonic() - self.current_started_at)
                + self.current_offset
            )

        return {
            "connected": bool(
                voice and voice.is_connected()
            ),
            "voice_channel": (
                voice.channel.name
                if voice and voice.channel
                else None
            ),
            "playing": bool(
                voice and voice.is_playing()
            ),
            "paused": bool(
                voice and voice.is_paused()
            ),
            "volume": round(self.volume * 100),
            "loop_mode": self.loop_mode,
            "current": (
                self.current.public()
                if self.current
                else None
            ),
            "elapsed": elapsed,
            "elapsed_text": fmt_duration(elapsed),
            "queue": [
                s.public()
                for s in self.queue
            ],
            "last_error": self.last_error,
        }


music_managers: dict[int, MusicManager] = {}


def get_music(guild_id: int):
    if guild_id not in music_managers:
        music_managers[guild_id] = MusicManager(guild_id)

    return music_managers[guild_id]


# =========================================================
# MODERATION + WARNINGS + LOG
# =========================================================

WARNINGS_FILE = DATA_DIR / "warnings.json"
MODLOG_FILE = DATA_DIR / "modlog.jsonl"


def load_warnings():
    if not WARNINGS_FILE.exists():
        return {}

    try:
        return json.loads(
            WARNINGS_FILE.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return {}


warnings_db = load_warnings()


def save_warnings():
    WARNINGS_FILE.write_text(
        json.dumps(
            warnings_db,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8",
    )


async def send_modlog(
    guild: discord.Guild,
    action: str,
    target: str,
    reason: str,
    actor: str = "Web Panel",
):
    record = {
        "ts": int(time.time()),
        "guild_id": guild.id,
        "action": action,
        "target": target,
        "reason": reason,
        "actor": actor,
    }

    with MODLOG_FILE.open(
        "a",
        encoding="utf-8"
    ) as f:
        f.write(
            json.dumps(
                record,
                ensure_ascii=False
            )
            + "\n"
        )

    if MOD_LOG_CHANNEL_ID:
        channel = guild.get_channel(
            MOD_LOG_CHANNEL_ID
        )

        if channel and hasattr(channel, "send"):
            embed = discord.Embed(
                title=f"🛡 {action}",
                description=reason or "Sebep yok"
            )

            embed.add_field(
                name="Hedef",
                value=target,
                inline=True
            )

            embed.add_field(
                name="İşlemi yapan",
                value=actor,
                inline=True
            )

            try:
                await channel.send(embed=embed)
            except Exception:
                pass


def get_modlog(limit=50):
    if not MODLOG_FILE.exists():
        return []

    lines = (
        MODLOG_FILE
        .read_text(encoding="utf-8")
        .splitlines()[-limit:]
    )

    out = []

    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except Exception:
            pass

    return out


def assert_manageable(
    guild: discord.Guild,
    member: discord.Member
):
    me = guild.me

    if member.id == guild.owner_id:
        raise RuntimeError(
            "Sunucu sahibine bu işlem uygulanamaz."
        )

    if member.id == me.id:
        raise RuntimeError(
            "Bot kendine işlem uygulayamaz."
        )

    if me.top_role <= member.top_role:
        raise RuntimeError(
            "Bot rolü hedef kullanıcının rolünden yukarıda olmalı."
        )


# =========================================================
# DISCORD EVENTS
# =========================================================

@bot.event
async def setup_hook():
    global BOT_LOOP

    BOT_LOOP = asyncio.get_running_loop()

    # Virtual credit economy, coupon system and casino commands.
    await setup_economy(
        bot,
        DATA_DIR,
        OWNER_ID,
        {
            "STARTING_CREDITS": os.getenv("STARTING_CREDITS", "10000"),
            "DAILY_CREDITS": os.getenv("DAILY_CREDITS", "500"),
            "MIN_CASINO_BET": os.getenv("MIN_CASINO_BET", "10"),
            "MAX_CASINO_BET": os.getenv("MAX_CASINO_BET", "1000000"),
            "MIN_COUPON_STAKE": os.getenv("MIN_COUPON_STAKE", "50"),
        },
    )

    if GUILD_ID:
        guild_obj = discord.Object(
            id=GUILD_ID
        )

        bot.tree.copy_global_to(
            guild=guild_obj
        )

        await bot.tree.sync(
            guild=guild_obj
        )

    else:
        await bot.tree.sync()


@bot.event
async def on_ready():
    print("---------------------------")
    print(f"Bot giriş yaptı: {bot.user}")
    print(f"Bot ID: {bot.user.id}")
    print(
        f"Web panel: "
        f"http://{PANEL_HOST}:{PANEL_PORT}"
    )
    print("---------------------------")


INVITE_RE = re.compile(
    r"(discord\.gg/|discord(?:app)?\.com/invite/)",
    re.I
)

spam_cache: dict[int, list[float]] = {}


@bot.event
async def on_message(
    message: discord.Message
):
    if message.author.bot:
        return

    if (
        AUTOMOD_ENABLED
        and isinstance(
            message.author,
            discord.Member
        )
    ):
        if not (
            message.author
            .guild_permissions
            .manage_messages
        ):
            content_lower = (
                message.content.lower()
            )

            reason = None

            if (
                BAD_WORDS
                and any(
                    word in content_lower
                    for word in BAD_WORDS
                )
            ):
                reason = "Yasaklı kelime"

            elif (
                BLOCK_INVITES
                and INVITE_RE.search(
                    message.content
                )
            ):
                reason = "Discord davet linki"

            elif (
                len(message.mentions)
                > MAX_MENTIONS
            ):
                reason = "Aşırı mention"

            else:
                now = time.time()

                arr = spam_cache.setdefault(
                    message.author.id,
                    []
                )

                arr.append(now)

                spam_cache[
                    message.author.id
                ] = [
                    t
                    for t in arr
                    if now - t <= 6
                ]

                if (
                    len(
                        spam_cache[
                            message.author.id
                        ]
                    )
                    >= 7
                ):
                    reason = "Spam"

            if reason:
                try:
                    await message.delete()

                    await message.channel.send(
                        f"⚠️ {message.author.mention}: {reason}.",
                        delete_after=5,
                    )

                except Exception:
                    pass

                return

    await bot.process_commands(message)


# =========================================================
# PREFIX MUSIC COMMANDS
# =========================================================

@bot.command()
async def help(ctx):
    await ctx.send(
        "**Müzik:** "
        "`!play`, `!pause`, `!resume`, "
        "`!skip`, `!stop`, `!queue`, "
        "`!now`, `!shuffle`, `!loop`, "
        "`!loopqueue`, `!volume`, "
        "`!remove`, `!move`, `!clear`, "
        "`!seek`, `!replay`, "
        "`!join`, `!leave`\n"
        "**Web panel:** `/panel`\n"
        "**Satranç:** `/chess`, `/move`\n"
        "**Ekonomi:** `/bakiye`, `/gunluk`, `/liderlik`\n"
        "**Casino:** `/blackjack`, `/roulette`, `/slots`, `/coinflip`\n"
        "**Kupon:** `/maclar`, `/kupon`, `/kuponlarim`"
    )


@bot.command()
async def join(ctx):
    if not ctx.author.voice:
        await ctx.send(
            "❌ Önce bir ses kanalına gir."
        )
        return

    await get_music(
        ctx.guild.id
    ).ensure_voice(
        ctx.author.voice.channel.id
    )

    await ctx.send("🔊 Kanala girdim.")


@bot.command()
async def play(
    ctx,
    *,
    query: str
):
    if not ctx.author.voice:
        await ctx.send(
            "❌ Önce bir ses kanalına gir."
        )
        return

    m = get_music(ctx.guild.id)

    msg = await ctx.send(
        f"🔎 Aranıyor: **{query}**"
    )

    try:
        songs = await m.enqueue(
            query,
            ctx.author.voice.channel.id,
            requested_by=str(ctx.author),
            text_channel_id=ctx.channel.id,
        )

        await msg.edit(
            content=(
                f"✅ **{len(songs)}** parça eklendi: "
                f"**{songs[0].title}**"
            )
        )

    except Exception as e:
        await msg.edit(
            content=f"❌ `{e}`"
        )


@bot.command()
async def pause(ctx):
    ok = await get_music(
        ctx.guild.id
    ).pause()

    await ctx.send(
        "⏸️ Duraklatıldı."
        if ok
        else "❌ Çalan şarkı yok."
    )


@bot.command()
async def resume(ctx):
    ok = await get_music(
        ctx.guild.id
    ).resume()

    await ctx.send(
        "▶️ Devam."
        if ok
        else "❌ Duraklatılmış şarkı yok."
    )


@bot.command()
async def skip(ctx):
    ok = await get_music(
        ctx.guild.id
    ).skip()

    await ctx.send(
        "⏭️ Geçildi."
        if ok
        else "❌ Çalan şarkı yok."
    )


@bot.command()
async def stop(ctx):
    await get_music(
        ctx.guild.id
    ).stop()

    await ctx.send(
        "⏹️ Durduruldu, sıra temizlendi."
    )


@bot.command()
async def leave(ctx):
    await get_music(
        ctx.guild.id
    ).leave()

    await ctx.send("👋 Çıktım.")


@bot.command(name="queue")
async def queue_cmd(ctx):
    q = get_music(
        ctx.guild.id
    ).queue

    if not q:
        await ctx.send("📭 Sıra boş.")
        return

    text = "\n".join(
        f"{i+1}. {s.title}"
        for i, s in enumerate(q[:20])
    )

    await ctx.send(
        f"📜 **Sıra**\n{text}"
    )


@bot.command()
async def now(ctx):
    m = get_music(ctx.guild.id)

    if not m.current:
        await ctx.send(
            "❌ Şu an çalan parça yok."
        )
        return

    await ctx.send(
        f"🎵 **{m.current.title}** "
        f"— {fmt_duration(m.current.duration)}"
    )


@bot.command()
async def shuffle(ctx):
    get_music(
        ctx.guild.id
    ).shuffle()

    await ctx.send(
        "🔀 Sıra karıştırıldı."
    )


@bot.command()
async def loop(ctx):
    m = get_music(ctx.guild.id)

    m.loop_mode = (
        "off"
        if m.loop_mode == "track"
        else "track"
    )

    await ctx.send(
        f"🔁 Track loop: **{m.loop_mode}**"
    )


@bot.command()
async def loopqueue(ctx):
    m = get_music(ctx.guild.id)

    m.loop_mode = (
        "off"
        if m.loop_mode == "queue"
        else "queue"
    )

    await ctx.send(
        f"🔁 Queue loop: **{m.loop_mode}**"
    )


@bot.command()
async def volume(
    ctx,
    percent: int
):
    val = await get_music(
        ctx.guild.id
    ).set_volume(percent)

    await ctx.send(
        f"🔊 Ses: **%{val}**"
    )


@bot.command()
async def remove(
    ctx,
    index: int
):
    try:
        song = get_music(
            ctx.guild.id
        ).remove(index)

        await ctx.send(
            f"🗑️ Çıkarıldı: **{song.title}**"
        )

    except Exception as e:
        await ctx.send(
            f"❌ {e}"
        )


@bot.command()
async def move(
    ctx,
    old_index: int,
    new_index: int
):
    try:
        get_music(
            ctx.guild.id
        ).move(
            old_index,
            new_index
        )

        await ctx.send(
            "↕️ Sıra güncellendi."
        )

    except Exception as e:
        await ctx.send(
            f"❌ {e}"
        )


@bot.command()
async def clear(ctx):
    get_music(
        ctx.guild.id
    ).clear()

    await ctx.send(
        "🧹 Sıra temizlendi."
    )


def parse_time(value: str):
    value = value.strip()

    if ":" in value:
        parts = [
            int(x)
            for x in value.split(":")
        ]

        if len(parts) == 2:
            return (
                parts[0] * 60
                + parts[1]
            )

        if len(parts) == 3:
            return (
                parts[0] * 3600
                + parts[1] * 60
                + parts[2]
            )

    return int(value)


@bot.command()
async def seek(
    ctx,
    timecode: str
):
    try:
        sec = parse_time(timecode)

        sec = await get_music(
            ctx.guild.id
        ).seek(sec)

        await ctx.send(
            f"⏩ {fmt_duration(sec)} konumuna gidildi."
        )

    except Exception as e:
        await ctx.send(
            f"❌ {e}"
        )


@bot.command()
async def replay(ctx):
    try:
        await get_music(
            ctx.guild.id
        ).replay()

        await ctx.send(
            "⏮️ Baştan başlatıldı."
        )

    except Exception as e:
        await ctx.send(
            f"❌ {e}"
        )



# =========================================================
# DISCORD CHAT CHESS
# =========================================================

CHESS_PIECES = {
    "K": "♔",
    "Q": "♕",
    "R": "♖",
    "B": "♗",
    "N": "♘",
    "P": "♙",
    "k": "♚",
    "q": "♛",
    "r": "♜",
    "b": "♝",
    "n": "♞",
    "p": "♟",
}

CHESS_VALUE = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20000,
}


@dataclass
class ChessGame:
    guild_id: int
    channel_id: int
    white_id: int
    black_id: int

    board: chess.Board = field(default_factory=chess.Board)

    message_id: Optional[int] = None

    vs_bot: bool = False
    bot_id: Optional[int] = None
    difficulty: int = 10

    san_history: list[str] = field(default_factory=list)
    last_move_uci: Optional[str] = None
    last_san: Optional[str] = None

    draw_offer_from: Optional[int] = None
    undo_request_from: Optional[int] = None

    finished: bool = False
    result_text: Optional[str] = None

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def player_for_turn(self) -> int:
        return self.white_id if self.board.turn == chess.WHITE else self.black_id

    def opponent_of(self, user_id: int) -> Optional[int]:
        if user_id == self.white_id:
            return self.black_id

        if user_id == self.black_id:
            return self.white_id

        return None

    def is_player(self, user_id: int) -> bool:
        return user_id in {
            self.white_id,
            self.black_id,
        }


@dataclass
class ChessChallenge:
    guild_id: int
    channel_id: int
    challenger_id: int
    opponent_id: int
    challenger_color: str
    difficulty: int = 10
    message_id: Optional[int] = None


active_chess_games: dict[int, ChessGame] = {}
pending_chess_challenges: dict[int, ChessChallenge] = {}


def chess_name(
    guild: discord.Guild,
    user_id: int
) -> str:
    if bot.user and user_id == bot.user.id:
        return bot.user.display_name

    member = guild.get_member(user_id)

    if member:
        return member.display_name

    return f"User {user_id}"


def chess_board_text(
    board: chess.Board
) -> str:
    """
    White altta olacak şekilde Discord chat için Unicode tahta.
    """
    lines = []

    for rank in range(7, -1, -1):
        row = []

        for file_index in range(8):
            square = chess.square(
                file_index,
                rank
            )

            piece = board.piece_at(
                square
            )

            if piece:
                row.append(
                    CHESS_PIECES[
                        piece.symbol()
                    ]
                )
            else:
                # Tahta daha okunaklı olsun.
                row.append(
                    "·"
                    if (
                        file_index + rank
                    ) % 2 == 0
                    else "•"
                )

        lines.append(
            f"{rank + 1}  "
            + " ".join(row)
        )

    lines.append(
        "   a b c d e f g h"
    )

    return "\n".join(lines)


def chess_move_history_text(
    game: ChessGame,
    max_full_moves: int = 8
) -> str:
    if not game.san_history:
        return "Henüz hamle yok."

    sans = game.san_history
    start_pair = max(
        0,
        (len(sans) // 2) - max_full_moves
    )

    start_index = start_pair * 2

    chunks = []

    i = start_index

    while i < len(sans):
        move_no = i // 2 + 1

        white = sans[i]

        black = (
            sans[i + 1]
            if i + 1 < len(sans)
            else ""
        )

        if black:
            chunks.append(
                f"{move_no}. {white} {black}"
            )
        else:
            chunks.append(
                f"{move_no}. {white}"
            )

        i += 2

    return "  ".join(chunks)


def chess_result_from_board(
    board: chess.Board
) -> Optional[str]:
    outcome = board.outcome(
        claim_draw=True
    )

    if not outcome:
        return None

    if outcome.winner is chess.WHITE:
        winner = "Beyaz"

    elif outcome.winner is chess.BLACK:
        winner = "Siyah"

    else:
        winner = None

    termination_map = {
        chess.Termination.CHECKMATE: "Şah mat",
        chess.Termination.STALEMATE: "Pat",
        chess.Termination.INSUFFICIENT_MATERIAL: "Yetersiz materyal",
        chess.Termination.SEVENTYFIVE_MOVES: "75 hamle kuralı",
        chess.Termination.FIVEFOLD_REPETITION: "Beş kez tekrar",
        chess.Termination.FIFTY_MOVES: "50 hamle kuralı",
        chess.Termination.THREEFOLD_REPETITION: "Üç kez tekrar",
    }

    reason = termination_map.get(
        outcome.termination,
        str(outcome.termination)
    )

    if winner:
        return (
            f"🏁 {reason} — "
            f"**{winner} kazandı.**"
        )

    return (
        f"🤝 {reason} — "
        f"**Berabere.**"
    )


def chess_embed(
    game: ChessGame
) -> discord.Embed:
    guild = bot.get_guild(
        game.guild_id
    )

    if not guild:
        raise RuntimeError(
            "Satranç sunucusu bulunamadı."
        )

    white_name = chess_name(
        guild,
        game.white_id
    )

    black_name = chess_name(
        guild,
        game.black_id
    )

    embed = discord.Embed(
        title=(
            f"♟️ {white_name} vs "
            f"{black_name}"
        ),
        description=(
            "```text\n"
            + chess_board_text(game.board)
            + "\n```"
        ),
    )

    embed.add_field(
        name="⚪ Beyaz",
        value=white_name,
        inline=True,
    )

    embed.add_field(
        name="⚫ Siyah",
        value=black_name,
        inline=True,
    )

    if game.finished:
        embed.add_field(
            name="Sonuç",
            value=(
                game.result_text
                or "Oyun bitti."
            ),
            inline=False,
        )

    else:
        turn_id = game.player_for_turn()

        turn_name = chess_name(
            guild,
            turn_id
        )

        turn_icon = (
            "⚪"
            if game.board.turn
            == chess.WHITE
            else "⚫"
        )

        status = (
            f"{turn_icon} **{turn_name}**"
        )

        if game.board.is_check():
            status += " — ♚ **Şah!**"

        embed.add_field(
            name="Sıra",
            value=status,
            inline=False,
        )

    if game.last_san:
        embed.add_field(
            name="Son hamle",
            value=(
                f"`{game.last_san}` "
                f"(`{game.last_move_uci}`)"
            ),
            inline=True,
        )

    if game.vs_bot:
        embed.add_field(
            name="Bot seviyesi",
            value=(
                f"{game.difficulty}/20"
                + (
                    " · Stockfish"
                    if stockfish_available()
                    else " · Dahili bot"
                )
            ),
            inline=True,
        )

    embed.add_field(
        name="Hamleler",
        value=chess_move_history_text(
            game
        )[:1024],
        inline=False,
    )

    if (
        game.draw_offer_from
        and not game.finished
    ):
        offer_name = chess_name(
            guild,
            game.draw_offer_from
        )

        embed.add_field(
            name="🤝 Beraberlik teklifi",
            value=(
                f"{offer_name} beraberlik "
                f"teklif etti. Rakip "
                f"**Beraberlik** butonuna "
                f"basarsa kabul edilir."
            ),
            inline=False,
        )

    if (
        game.undo_request_from
        and not game.finished
        and not game.vs_bot
    ):
        requester = chess_name(
            guild,
            game.undo_request_from
        )

        embed.add_field(
            name="↩️ Geri alma isteği",
            value=(
                f"{requester} son hamleyi "
                f"geri almak istiyor. "
                f"Rakip **Geri Al** "
                f"butonuna basarsa kabul edilir."
            ),
            inline=False,
        )

    embed.set_footer(
        text=(
            "Hamle: /move e4, /move Nf3 "
            "veya /move e2e4"
        )
    )

    return embed


async def chess_message(
    game: ChessGame
) -> Optional[discord.Message]:
    channel = bot.get_channel(
        game.channel_id
    )

    if not channel:
        try:
            channel = await bot.fetch_channel(
                game.channel_id
            )
        except Exception:
            return None

    if not game.message_id:
        return None

    try:
        return await channel.fetch_message(
            game.message_id
        )
    except Exception:
        return None


async def update_chess_message(
    game: ChessGame
):
    msg = await chess_message(game)

    if not msg:
        return

    view = (
        None
        if game.finished
        else ChessGameView(
            game.channel_id
        )
    )

    await msg.edit(
        embed=chess_embed(game),
        view=view,
    )


def finish_chess_game(
    game: ChessGame,
    result_text: str
):
    game.finished = True
    game.result_text = result_text
    game.draw_offer_from = None
    game.undo_request_from = None


def check_chess_finished(
    game: ChessGame
) -> bool:
    result = chess_result_from_board(
        game.board
    )

    if result:
        finish_chess_game(
            game,
            result
        )

        return True

    return False


def parse_chat_chess_move(
    board: chess.Board,
    raw_move: str
) -> tuple[chess.Move, str]:
    raw = raw_move.strip()

    if not raw:
        raise ValueError(
            "Hamle boş olamaz."
        )

    # Önce UCI: e2e4 / e7e8q
    if re.fullmatch(
        r"[a-hA-H][1-8][a-hA-H][1-8][qQrRbBnN]?",
        raw
    ):
        move = chess.Move.from_uci(
            raw.lower()
        )

        if move not in board.legal_moves:
            raise ValueError(
                "Bu UCI hamlesi legal değil."
            )

        san = board.san(move)

        return move, san

    # Sonra SAN: e4 / Nf3 / O-O / Qxf7+
    try:
        move = board.parse_san(raw)

    except ValueError:
        raise ValueError(
            "Hamleyi anlayamadım. "
            "Örnek: `e4`, `Nf3`, "
            "`O-O` veya `e2e4`."
        )

    san = board.san(move)

    return move, san


def stockfish_available() -> bool:
    if not STOCKFISH_PATH:
        return False

    if Path(
        STOCKFISH_PATH
    ).exists():
        return True

    return bool(
        shutil.which(
            STOCKFISH_PATH
        )
    )


def fallback_chess_bot_move(
    board: chess.Board,
    difficulty: int
) -> chess.Move:
    """
    Stockfish yoksa tamamen random yerine:
    mat > terfi > değerli taş alma > şah > merkez
    öncelikli hafif bot.
    """
    legal_moves = list(
        board.legal_moves
    )

    if not legal_moves:
        raise RuntimeError(
            "Botun legal hamlesi yok."
        )

    scored = []

    for move in legal_moves:
        score = random.uniform(
            -25,
            25
        )

        if board.is_capture(move):
            victim = board.piece_at(
                move.to_square
            )

            attacker = board.piece_at(
                move.from_square
            )

            if victim:
                score += (
                    CHESS_VALUE[
                        victim.piece_type
                    ]
                    * 1.2
                )

            if attacker:
                score -= (
                    CHESS_VALUE[
                        attacker.piece_type
                    ]
                    * 0.08
                )

        if move.promotion:
            score += (
                CHESS_VALUE.get(
                    move.promotion,
                    0
                )
                * 1.1
            )

        center = {
            chess.D4,
            chess.E4,
            chess.D5,
            chess.E5,
        }

        if move.to_square in center:
            score += 35

        board.push(move)

        if board.is_checkmate():
            score += 1_000_000

        elif board.is_check():
            score += 80

        # Basit materyal farkı
        material = 0

        for piece_type, value in CHESS_VALUE.items():
            material += (
                len(
                    board.pieces(
                        piece_type,
                        chess.WHITE
                    )
                )
                * value
            )

            material -= (
                len(
                    board.pieces(
                        piece_type,
                        chess.BLACK
                    )
                )
                * value
            )

        # Hamleyi yapan tarafın bakış açısı.
        mover_was_white = not board.turn

        score += (
            material * 0.04
            if mover_was_white
            else -material * 0.04
        )

        board.pop()

        scored.append(
            (
                score,
                move
            )
        )

    scored.sort(
        key=lambda x: x[0],
        reverse=True
    )

    difficulty = max(
        1,
        min(20, difficulty)
    )

    # Kolay seviyede daha geniş aday havuzundan,
    # zor seviyede en üstlerden seçim.
    candidate_count = max(
        1,
        int(
            8
            - (
                difficulty - 1
            )
            * 7 / 19
        )
    )

    candidates = scored[
        :min(
            candidate_count,
            len(scored)
        )
    ]

    return random.choice(
        candidates
    )[1]


async def choose_chess_bot_move(
    game: ChessGame
) -> chess.Move:
    board_copy = game.board.copy(
        stack=True
    )

    difficulty = max(
        1,
        min(20, game.difficulty)
    )

    if stockfish_available():
        loop = asyncio.get_running_loop()

        def engine_work():
            engine = (
                chess.engine
                .SimpleEngine
                .popen_uci(
                    STOCKFISH_PATH
                )
            )

            try:
                try:
                    engine.configure(
                        {
                            "Skill Level":
                            difficulty
                        }
                    )
                except Exception:
                    pass

                think_time = (
                    0.05
                    + difficulty
                    * 0.025
                )

                result = engine.play(
                    board_copy,
                    chess.engine.Limit(
                        time=think_time
                    )
                )

                return result.move

            finally:
                try:
                    engine.quit()
                except Exception:
                    pass

        return await loop.run_in_executor(
            None,
            engine_work
        )

    return fallback_chess_bot_move(
        board_copy,
        difficulty
    )


async def chess_bot_turn(
    game: ChessGame
):
    if (
        game.finished
        or not game.vs_bot
        or not game.bot_id
        or game.player_for_turn()
        != game.bot_id
    ):
        return

    async with game.lock:
        if (
            game.finished
            or game.player_for_turn()
            != game.bot_id
        ):
            return

        move = await choose_chess_bot_move(
            game
        )

        san = game.board.san(move)

        game.board.push(move)

        game.last_move_uci = move.uci()
        game.last_san = san
        game.san_history.append(san)

        game.draw_offer_from = None
        game.undo_request_from = None

        check_chess_finished(game)

        await update_chess_message(game)


def chess_color_assignment(
    challenger_id: int,
    opponent_id: int,
    challenger_color: str
) -> tuple[int, int]:
    color = (
        challenger_color
        or "random"
    ).lower()

    if color not in {
        "white",
        "black",
        "random"
    }:
        color = "random"

    if color == "random":
        color = random.choice(
            [
                "white",
                "black",
            ]
        )

    if color == "white":
        return (
            challenger_id,
            opponent_id
        )

    return (
        opponent_id,
        challenger_id
    )


async def start_chess_game_message(
    *,
    guild: discord.Guild,
    channel: discord.abc.Messageable,
    challenger_id: int,
    opponent_id: int,
    challenger_color: str,
    difficulty: int,
    existing_message: Optional[
        discord.Message
    ] = None,
) -> ChessGame:
    white_id, black_id = (
        chess_color_assignment(
            challenger_id,
            opponent_id,
            challenger_color
        )
    )

    game = ChessGame(
        guild_id=guild.id,
        channel_id=channel.id,
        white_id=white_id,
        black_id=black_id,
        vs_bot=(
            bot.user is not None
            and opponent_id
            == bot.user.id
        ),
        bot_id=(
            bot.user.id
            if (
                bot.user is not None
                and opponent_id
                == bot.user.id
            )
            else None
        ),
        difficulty=max(
            1,
            min(20, difficulty)
        ),
    )

    active_chess_games[
        channel.id
    ] = game

    pending_chess_challenges.pop(
        channel.id,
        None
    )

    view = ChessGameView(
        channel.id
    )

    if existing_message:
        await existing_message.edit(
            content=None,
            embed=chess_embed(game),
            view=view,
        )

        game.message_id = (
            existing_message.id
        )

    else:
        msg = await channel.send(
            embed=chess_embed(game),
            view=view,
        )

        game.message_id = msg.id

    if (
        game.vs_bot
        and game.player_for_turn()
        == game.bot_id
    ):
        await chess_bot_turn(game)

    return game


class ChessChallengeView(
    discord.ui.View
):
    def __init__(
        self,
        channel_id: int
    ):
        super().__init__(
            timeout=180
        )

        self.channel_id = channel_id

    async def on_timeout(self):
        pending_chess_challenges.pop(
            self.channel_id,
            None
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ) -> bool:
        challenge = (
            pending_chess_challenges
            .get(self.channel_id)
        )

        if not challenge:
            await interaction.response.send_message(
                "Bu satranç daveti artık aktif değil.",
                ephemeral=True,
            )
            return False

        if (
            interaction.user.id
            != challenge.opponent_id
        ):
            await interaction.response.send_message(
                "Bu davet sana gönderilmedi.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(
        label="Kabul Et",
        emoji="✅",
        style=discord.ButtonStyle.success
    )
    async def accept(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        challenge = (
            pending_chess_challenges
            .get(self.channel_id)
        )

        if not challenge:
            await interaction.response.send_message(
                "Davet artık aktif değil.",
                ephemeral=True,
            )
            return

        if (
            self.channel_id
            in active_chess_games
        ):
            await interaction.response.send_message(
                "Bu kanalda zaten aktif bir maç var.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        await start_chess_game_message(
            guild=interaction.guild,
            channel=interaction.channel,
            challenger_id=(
                challenge.challenger_id
            ),
            opponent_id=(
                challenge.opponent_id
            ),
            challenger_color=(
                challenge.challenger_color
            ),
            difficulty=(
                challenge.difficulty
            ),
            existing_message=(
                interaction.message
            ),
        )

    @discord.ui.button(
        label="Reddet",
        emoji="❌",
        style=discord.ButtonStyle.danger
    )
    async def decline(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        challenge = (
            pending_chess_challenges
            .pop(
                self.channel_id,
                None
            )
        )

        await interaction.response.edit_message(
            content=(
                "❌ Satranç daveti reddedildi."
                if challenge
                else "Davet artık aktif değil."
            ),
            embed=None,
            view=None,
        )


class ChessGameView(
    discord.ui.View
):
    def __init__(
        self,
        channel_id: int
    ):
        super().__init__(
            timeout=None
        )

        self.channel_id = channel_id

    async def game_and_check(
        self,
        interaction: discord.Interaction
    ) -> Optional[ChessGame]:
        game = active_chess_games.get(
            self.channel_id
        )

        if not game or game.finished:
            await interaction.response.send_message(
                "Bu maç artık aktif değil.",
                ephemeral=True,
            )
            return None

        if not game.is_player(
            interaction.user.id
        ):
            await interaction.response.send_message(
                "Bu maçın oyuncusu değilsin.",
                ephemeral=True,
            )
            return None

        return game

    @discord.ui.button(
        label="Pes Et",
        emoji="🏳️",
        style=discord.ButtonStyle.danger
    )
    async def resign(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        game = await self.game_and_check(
            interaction
        )

        if not game:
            return

        opponent_id = game.opponent_of(
            interaction.user.id
        )

        guild = interaction.guild

        winner_name = chess_name(
            guild,
            opponent_id
        )

        finish_chess_game(
            game,
            (
                f"🏳️ "
                f"{interaction.user.display_name} "
                f"pes etti — "
                f"**{winner_name} kazandı.**"
            ),
        )

        await interaction.response.edit_message(
            embed=chess_embed(game),
            view=None,
        )

    @discord.ui.button(
        label="Beraberlik",
        emoji="🤝",
        style=discord.ButtonStyle.secondary
    )
    async def draw_offer(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        game = await self.game_and_check(
            interaction
        )

        if not game:
            return

        if game.vs_bot:
            if (
                game.board.can_claim_draw()
                or game.board.is_insufficient_material()
            ):
                finish_chess_game(
                    game,
                    "🤝 **Berabere.**",
                )

                await interaction.response.edit_message(
                    embed=chess_embed(game),
                    view=None,
                )
            else:
                await interaction.response.send_message(
                    "🤖 Maqnobot beraberlik teklifini reddetti.",
                    ephemeral=True,
                )

            return

        if (
            game.draw_offer_from
            and game.draw_offer_from
            != interaction.user.id
        ):
            finish_chess_game(
                game,
                "🤝 Oyuncular anlaştı — **Berabere.**",
            )

            await interaction.response.edit_message(
                embed=chess_embed(game),
                view=None,
            )

            return

        game.draw_offer_from = (
            interaction.user.id
        )

        await interaction.response.edit_message(
            embed=chess_embed(game),
            view=self,
        )

    @discord.ui.button(
        label="Geri Al",
        emoji="↩️",
        style=discord.ButtonStyle.primary
    )
    async def undo(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        game = await self.game_and_check(
            interaction
        )

        if not game:
            return

        if not game.board.move_stack:
            await interaction.response.send_message(
                "Geri alınacak hamle yok.",
                ephemeral=True,
            )
            return

        if game.vs_bot:
            # Bot maçı: mümkünse bot + kullanıcı hamlesini beraber geri al.
            undo_count = min(
                2,
                len(
                    game.board.move_stack
                )
            )

            for _ in range(
                undo_count
            ):
                game.board.pop()

                if game.san_history:
                    game.san_history.pop()

            game.last_move_uci = None
            game.last_san = (
                game.san_history[-1]
                if game.san_history
                else None
            )

            game.draw_offer_from = None
            game.undo_request_from = None

            await interaction.response.edit_message(
                embed=chess_embed(game),
                view=self,
            )

            return

        if (
            game.undo_request_from
            and game.undo_request_from
            != interaction.user.id
        ):
            game.board.pop()

            if game.san_history:
                game.san_history.pop()

            game.last_move_uci = None
            game.last_san = (
                game.san_history[-1]
                if game.san_history
                else None
            )

            game.undo_request_from = None
            game.draw_offer_from = None

            await interaction.response.edit_message(
                embed=chess_embed(game),
                view=self,
            )

            return

        game.undo_request_from = (
            interaction.user.id
        )

        await interaction.response.edit_message(
            embed=chess_embed(game),
            view=self,
        )


@bot.tree.command(
    name="chess",
    description=(
        "Discord chat içinde satranç maçı başlat."
    )
)
@app_commands.describe(
    opponent=(
        "Rakip. Boş bırakırsan Maqnobot'a karşı oynarsın."
    ),
    color=(
        "Senin rengin: white, black veya random"
    ),
    difficulty=(
        "Bot seviyesi 1-20. İnsan maçında yok sayılır."
    ),
)
@app_commands.choices(
    color=[
        app_commands.Choice(
            name="Rastgele",
            value="random"
        ),
        app_commands.Choice(
            name="Beyaz",
            value="white"
        ),
        app_commands.Choice(
            name="Siyah",
            value="black"
        ),
    ]
)
async def chess_command(
    interaction: discord.Interaction,
    opponent: Optional[
        discord.Member
    ] = None,
    color: Optional[
        app_commands.Choice[str]
    ] = None,
    difficulty: int = 10,
):
    if not interaction.guild:
        await interaction.response.send_message(
            "Satranç sadece sunucuda kullanılabilir.",
            ephemeral=True,
        )
        return

    channel_id = interaction.channel_id

    if channel_id in active_chess_games:
        game = active_chess_games[
            channel_id
        ]

        if not game.finished:
            await interaction.response.send_message(
                "Bu kanalda zaten aktif bir satranç maçı var. "
                "Mevcut maçın mesajını kullan veya pes et.",
                ephemeral=True,
            )
            return

        active_chess_games.pop(
            channel_id,
            None
        )

    if (
        channel_id
        in pending_chess_challenges
    ):
        await interaction.response.send_message(
            "Bu kanalda zaten bekleyen bir satranç daveti var.",
            ephemeral=True,
        )
        return

    chosen_color = (
        color.value
        if color
        else "random"
    )

    difficulty = max(
        1,
        min(20, int(difficulty))
    )

    # Rakip boşsa Maqnobot.
    if opponent is None:
        if not bot.user:
            await interaction.response.send_message(
                "Bot henüz hazır değil.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        game = await start_chess_game_message(
            guild=interaction.guild,
            channel=interaction.channel,
            challenger_id=interaction.user.id,
            opponent_id=bot.user.id,
            challenger_color=chosen_color,
            difficulty=difficulty,
        )

        await interaction.followup.send(
            (
                f"♟️ Maç başladı. "
                f"Bot seviyesi: **{game.difficulty}/20**. "
                f"Hamle için `/move` kullan."
            ),
            ephemeral=True,
        )

        return

    if opponent.id == interaction.user.id:
        await interaction.response.send_message(
            "Kendine karşı maç açamazsın 😄",
            ephemeral=True,
        )
        return

    if opponent.bot:
        if (
            bot.user
            and opponent.id == bot.user.id
        ):
            await interaction.response.defer()

            game = await start_chess_game_message(
                guild=interaction.guild,
                channel=interaction.channel,
                challenger_id=interaction.user.id,
                opponent_id=bot.user.id,
                challenger_color=chosen_color,
                difficulty=difficulty,
            )

            await interaction.followup.send(
                (
                    f"♟️ Maç başladı. "
                    f"Bot seviyesi: **{game.difficulty}/20**."
                ),
                ephemeral=True,
            )

            return

        await interaction.response.send_message(
            "Başka Discord botlarına karşı maç açamam.",
            ephemeral=True,
        )
        return

    challenge = ChessChallenge(
        guild_id=interaction.guild.id,
        channel_id=channel_id,
        challenger_id=interaction.user.id,
        opponent_id=opponent.id,
        challenger_color=chosen_color,
        difficulty=difficulty,
    )

    pending_chess_challenges[
        channel_id
    ] = challenge

    embed = discord.Embed(
        title="♟️ Satranç Daveti",
        description=(
            f"{opponent.mention}, "
            f"{interaction.user.mention} "
            f"seni satranç maçına davet ediyor."
        ),
    )

    embed.add_field(
        name="Daveti gönderenin rengi",
        value=(
            {
                "white": "⚪ Beyaz",
                "black": "⚫ Siyah",
                "random": "🎲 Rastgele",
            }[
                chosen_color
            ]
        ),
        inline=True,
    )

    await interaction.response.send_message(
        embed=embed,
        view=ChessChallengeView(
            channel_id
        ),
    )

    msg = await interaction.original_response()

    challenge.message_id = msg.id


@bot.tree.command(
    name="move",
    description="Aktif satranç maçında hamle yap."
)
@app_commands.describe(
    move=(
        "Örnek: e4, Nf3, O-O veya e2e4"
    )
)
async def chess_move_command(
    interaction: discord.Interaction,
    move: str
):
    game = active_chess_games.get(
        interaction.channel_id
    )

    if not game or game.finished:
        await interaction.response.send_message(
            "Bu kanalda aktif bir satranç maçı yok.",
            ephemeral=True,
        )
        return

    if not game.is_player(
        interaction.user.id
    ):
        await interaction.response.send_message(
            "Bu maçın oyuncusu değilsin.",
            ephemeral=True,
        )
        return

    if (
        game.player_for_turn()
        != interaction.user.id
    ):
        await interaction.response.send_message(
            "Sıra sende değil.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(
        ephemeral=True
    )

    async with game.lock:
        try:
            chess_move, san = (
                parse_chat_chess_move(
                    game.board,
                    move
                )
            )

        except ValueError as e:
            await interaction.followup.send(
                f"❌ {e}",
                ephemeral=True,
            )
            return

        game.board.push(
            chess_move
        )

        game.last_move_uci = (
            chess_move.uci()
        )

        game.last_san = san
        game.san_history.append(san)

        game.draw_offer_from = None
        game.undo_request_from = None

        finished = check_chess_finished(
            game
        )

        await update_chess_message(
            game
        )

    await interaction.followup.send(
        f"✅ Hamle: **{san}**",
        ephemeral=True,
    )

    if (
        not finished
        and game.vs_bot
    ):
        await chess_bot_turn(game)


@bot.tree.command(
    name="chessboard",
    description="Aktif maçın satranç tahtasını yeniden gönder."
)
async def chessboard_command(
    interaction: discord.Interaction
):
    game = active_chess_games.get(
        interaction.channel_id
    )

    if not game:
        await interaction.response.send_message(
            "Bu kanalda aktif bir satranç maçı yok.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=chess_embed(game),
        view=(
            None
            if game.finished
            else ChessGameView(
                interaction.channel_id
            )
        ),
    )

    msg = await interaction.original_response()

    game.message_id = msg.id


@bot.tree.command(
    name="chesspgn",
    description="Aktif satranç maçının PGN kaydını göster."
)
async def chesspgn_command(
    interaction: discord.Interaction
):
    game = active_chess_games.get(
        interaction.channel_id
    )

    if not game:
        await interaction.response.send_message(
            "Bu kanalda satranç maçı yok.",
            ephemeral=True,
        )
        return

    pgn_game = chess.pgn.Game.from_board(
        game.board
    )

    guild = interaction.guild

    if guild:
        pgn_game.headers[
            "White"
        ] = chess_name(
            guild,
            game.white_id
        )

        pgn_game.headers[
            "Black"
        ] = chess_name(
            guild,
            game.black_id
        )

    pgn_text = str(pgn_game)

    if len(pgn_text) <= 1800:
        await interaction.response.send_message(
            f"```pgn\n{pgn_text}\n```",
            ephemeral=True,
        )

    else:
        data = pgn_text.encode(
            "utf-8"
        )

        file = discord.File(
            fp=__import__(
                "io"
            ).BytesIO(data),
            filename="maqnobot_game.pgn",
        )

        await interaction.response.send_message(
            "♟️ PGN dosyası:",
            file=file,
            ephemeral=True,
        )




# =========================================================
# SLASH COMMANDS
# =========================================================

@bot.tree.command(
    name="panel",
    description="Maqnobot web panel adresini gösterir."
)
async def slash_panel(
    interaction: discord.Interaction
):
    if (
        OWNER_ID
        and interaction.user.id != OWNER_ID
    ):
        await interaction.response.send_message(
            "Bu komut sadece bot sahibine açık.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"🖥️ Panel: "
        f"`http://{PANEL_HOST}:{PANEL_PORT}`\n"
        f"Panel şifresi `.env` içindeki "
        f"`PANEL_PASSWORD` değeridir.",
        ephemeral=True,
    )


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI(
    title="Maqnobot Control Panel"
)

app.add_middleware(
    SessionMiddleware,
    secret_key=PANEL_SECRET,
    same_site="lax",
    https_only=False,
)

templates = Jinja2Templates(
    directory=str(
        BASE_DIR / "templates"
    )
)


def require_login(request: Request):
    if not request.session.get(
        "logged_in"
    ):
        raise HTTPException(
            status_code=401,
            detail="Giriş gerekli."
        )


def selected_guild():
    if GUILD_ID:
        guild = bot.get_guild(
            GUILD_ID
        )

        if guild:
            return guild

    if bot.guilds:
        return bot.guilds[0]

    raise RuntimeError(
        "Bot henüz sunucuya bağlı değil."
    )


def run_on_bot(
    coro,
    timeout=25
):
    if BOT_LOOP is None:
        raise RuntimeError(
            "Bot event loop henüz hazır değil."
        )

    fut = asyncio.run_coroutine_threadsafe(
        coro,
        BOT_LOOP
    )

    return fut.result(
        timeout=timeout
    )


# =========================================================
# WEB ROUTES
# STARLETTE 1.0 UYUMLU
# =========================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
async def home(
    request: Request
):
    if not request.session.get(
        "logged_in"
    ):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={}
        )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={}
    )


@app.post("/login")
async def login(
    request: Request,
    password: str = Form(...)
):
    if password != PANEL_PASSWORD:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Şifre yanlış."
            },
            status_code=401,
        )

    request.session[
        "logged_in"
    ] = True

    return RedirectResponse(
        "/",
        status_code=303
    )


@app.post("/logout")
async def logout(
    request: Request
):
    request.session.clear()

    return RedirectResponse(
        "/",
        status_code=303
    )


async def build_state():
    # Discord snowflake IDlerini browsera STRING gonder; JavaScript Number bunlari yuvarlar.
    guild = selected_guild()
    m = get_music(guild.id)

    members = [
        {
            "id": str(mem.id),
            "name": mem.display_name,
            "username": str(mem),
            "bot": mem.bot,
        }
        for mem in guild.members
    ]

    roles = [
        {
            "id": str(r.id),
            "name": r.name
        }
        for r in guild.roles
        if (
            not r.is_default()
            and not r.managed
        )
    ]

    text_channels = [
        {
            "id": str(c.id),
            "name": c.name
        }
        for c in guild.text_channels
    ]

    voice_channels = [
        {
            "id": str(c.id),
            "name": c.name
        }
        for c in guild.channels
        if isinstance(
            c,
            (discord.VoiceChannel, discord.StageChannel)
        )
    ]

    return {
        "guild": {
            "id": str(guild.id),
            "name": guild.name,
            "member_count": guild.member_count,
        },
        "music": m.state(),
        "members": members,
        "roles": roles,
        "text_channels": text_channels,
        "voice_channels": voice_channels,
        "warnings": warnings_db,
        "modlog": get_modlog(50),
    }


@app.get("/api/state")
async def api_state(
    request: Request
):
    require_login(request)

    try:
        return run_on_bot(
            build_state()
        )

    except Exception as e:
        return JSONResponse(
            {
                "error": str(e)
            },
            status_code=500
        )


# =========================================================
# MUSIC API
# =========================================================

@app.post("/api/music/play")
async def api_music_play(
    request: Request,
    query: str = Form(...),
    voice_channel_id: int = Form(...),
    text_channel_id: Optional[int] = Form(None),
):
    require_login(request)

    async def action():
        guild = selected_guild()

        songs = await get_music(
            guild.id
        ).enqueue(
            query,
            voice_channel_id,
            requested_by="Web Panel",
            text_channel_id=text_channel_id,
        )

        return {
            "ok": True,
            "added": len(songs),
            "first": songs[0].title,
        }

    try:
        return run_on_bot(
            action(),
            timeout=60
        )

    except Exception as e:
        return JSONResponse(
            {
                "error": str(e)
            },
            status_code=400
        )


@app.post(
    "/api/music/{action}"
)
async def api_music_action(
    request: Request,
    action: str,
    value: Optional[str] = Form(None),
):
    require_login(request)

    async def do():
        guild = selected_guild()
        m = get_music(guild.id)

        if action == "pause":
            return {
                "ok": await m.pause()
            }

        if action == "resume":
            return {
                "ok": await m.resume()
            }

        if action == "skip":
            return {
                "ok": await m.skip()
            }

        if action == "stop":
            await m.stop()
            return {"ok": True}

        if action == "leave":
            await m.leave()
            return {"ok": True}

        if action == "shuffle":
            m.shuffle()
            return {"ok": True}

        if action == "clear":
            m.clear()
            return {"ok": True}

        if action == "replay":
            await m.replay()
            return {"ok": True}

        if action == "volume":
            volume_value = await m.set_volume(
                int(value or 60)
            )

            return {
                "ok": True,
                "volume": volume_value,
            }

        if action == "loop":
            if value not in {
                "off",
                "track",
                "queue"
            }:
                raise RuntimeError(
                    "Loop değeri off/track/queue olmalı."
                )

            m.loop_mode = value

            return {
                "ok": True,
                "loop_mode": value,
            }

        if action == "seek":
            sec = parse_time(
                value or "0"
            )

            await m.seek(sec)

            return {
                "ok": True,
                "seek": sec,
            }

        if action == "remove":
            song = m.remove(
                int(value or "0")
            )

            return {
                "ok": True,
                "removed": song.title,
            }

        raise RuntimeError(
            "Bilinmeyen müzik işlemi."
        )

    try:
        return run_on_bot(
            do()
        )

    except Exception as e:
        return JSONResponse(
            {
                "error": str(e)
            },
            status_code=400
        )


@app.post(
    "/api/music/move-item"
)
async def api_music_move(
    request: Request,
    old_index: int = Form(...),
    new_index: int = Form(...),
):
    require_login(request)

    async def do():
        guild = selected_guild()

        get_music(
            guild.id
        ).move(
            old_index,
            new_index
        )

        return {"ok": True}

    try:
        return run_on_bot(do())

    except Exception as e:
        return JSONResponse(
            {
                "error": str(e)
            },
            status_code=400
        )


# =========================================================
# MODERATION API
# =========================================================

@app.post("/api/mod/timeout")
async def api_timeout(
    request: Request,
    member_id: int = Form(...),
    minutes: int = Form(...),
    reason: str = Form("Web panel"),
):
    require_login(request)

    async def do():
        guild = selected_guild()

        member = guild.get_member(
            member_id
        )

        if not member:
            raise RuntimeError(
                "Üye bulunamadı."
            )

        assert_manageable(
            guild,
            member
        )

        await member.timeout(
            timedelta(
                minutes=max(1, minutes)
            ),
            reason=reason,
        )

        await send_modlog(
            guild,
            "TIMEOUT",
            str(member),
            reason,
        )

        return {"ok": True}

    try:
        return run_on_bot(do())

    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=400
        )


@app.post("/api/mod/untimeout")
async def api_untimeout(
    request: Request,
    member_id: int = Form(...),
    reason: str = Form("Web panel"),
):
    require_login(request)

    async def do():
        guild = selected_guild()

        member = guild.get_member(
            member_id
        )

        if not member:
            raise RuntimeError(
                "Üye bulunamadı."
            )

        assert_manageable(
            guild,
            member
        )

        await member.timeout(
            None,
            reason=reason
        )

        await send_modlog(
            guild,
            "UNTIMEOUT",
            str(member),
            reason,
        )

        return {"ok": True}

    try:
        return run_on_bot(do())

    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=400
        )


@app.post("/api/mod/kick")
async def api_kick(
    request: Request,
    member_id: int = Form(...),
    reason: str = Form("Web panel"),
):
    require_login(request)

    async def do():
        guild = selected_guild()

        member = guild.get_member(
            member_id
        )

        if not member:
            raise RuntimeError(
                "Üye bulunamadı."
            )

        assert_manageable(
            guild,
            member
        )

        target = str(member)

        await member.kick(
            reason=reason
        )

        await send_modlog(
            guild,
            "KICK",
            target,
            reason,
        )

        return {"ok": True}

    try:
        return run_on_bot(do())

    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=400
        )


@app.post("/api/mod/ban")
async def api_ban(
    request: Request,
    member_id: int = Form(...),
    reason: str = Form("Web panel"),
):
    require_login(request)

    async def do():
        guild = selected_guild()

        member = guild.get_member(
            member_id
        )

        if not member:
            raise RuntimeError(
                "Üye bulunamadı."
            )

        assert_manageable(
            guild,
            member
        )

        target = str(member)

        await guild.ban(
            member,
            reason=reason,
            delete_message_seconds=0,
        )

        await send_modlog(
            guild,
            "BAN",
            target,
            reason,
        )

        return {"ok": True}

    try:
        return run_on_bot(do())

    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=400
        )


@app.post("/api/mod/unban")
async def api_unban(
    request: Request,
    user_id: int = Form(...),
    reason: str = Form("Web panel"),
):
    require_login(request)

    async def do():
        guild = selected_guild()

        user = await bot.fetch_user(
            user_id
        )

        await guild.unban(
            user,
            reason=reason
        )

        await send_modlog(
            guild,
            "UNBAN",
            str(user),
            reason,
        )

        return {"ok": True}

    try:
        return run_on_bot(do())

    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=400
        )


@app.post("/api/mod/purge")
async def api_purge(
    request: Request,
    channel_id: int = Form(...),
    amount: int = Form(...),
):
    require_login(request)

    async def do():
        guild = selected_guild()

        channel = guild.get_channel(
            channel_id
        )

        if not isinstance(
            channel,
            discord.TextChannel
        ):
            raise RuntimeError(
                "Metin kanalı bulunamadı."
            )

        amount2 = max(
            1,
            min(200, amount)
        )

        deleted = await channel.purge(
            limit=amount2
        )

        await send_modlog(
            guild,
            "PURGE",
            f"#{channel.name}",
            f"{len(deleted)} mesaj",
        )

        return {
            "ok": True,
            "deleted": len(deleted),
        }

    try:
        return run_on_bot(do())

    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=400
        )


@app.post("/api/mod/channel")
async def api_channel_action(
    request: Request,
    channel_id: int = Form(...),
    action: str = Form(...),
    seconds: int = Form(0),
):
    require_login(request)

    async def do():
        guild = selected_guild()

        channel = guild.get_channel(
            channel_id
        )

        if not isinstance(
            channel,
            discord.TextChannel
        ):
            raise RuntimeError(
                "Metin kanalı bulunamadı."
            )

        if action in {
            "lock",
            "unlock"
        }:
            overwrite = (
                channel.overwrites_for(
                    guild.default_role
                )
            )

            overwrite.send_messages = (
                False
                if action == "lock"
                else None
            )

            await channel.set_permissions(
                guild.default_role,
                overwrite=overwrite,
            )

            await send_modlog(
                guild,
                action.upper(),
                f"#{channel.name}",
                "Web panel",
            )

        elif action == "slowmode":
            delay = max(
                0,
                min(
                    21600,
                    int(seconds)
                )
            )

            await channel.edit(
                slowmode_delay=delay
            )

            await send_modlog(
                guild,
                "SLOWMODE",
                f"#{channel.name}",
                f"{delay} saniye",
            )

        else:
            raise RuntimeError(
                "Bilinmeyen kanal işlemi."
            )

        return {"ok": True}

    try:
        return run_on_bot(do())

    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=400
        )


@app.post("/api/mod/nickname")
async def api_nickname(
    request: Request,
    member_id: int = Form(...),
    nickname: str = Form(""),
):
    require_login(request)

    async def do():
        guild = selected_guild()

        member = guild.get_member(
            member_id
        )

        if not member:
            raise RuntimeError(
                "Üye bulunamadı."
            )

        assert_manageable(
            guild,
            member
        )

        await member.edit(
            nick=nickname or None,
            reason="Web panel",
        )

        await send_modlog(
            guild,
            "NICKNAME",
            str(member),
            nickname or "(sıfırlandı)",
        )

        return {"ok": True}

    try:
        return run_on_bot(do())

    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=400
        )


@app.post("/api/mod/role")
async def api_role(
    request: Request,
    member_id: int = Form(...),
    role_id: int = Form(...),
    action: str = Form(...),
):
    require_login(request)

    async def do():
        guild = selected_guild()

        member = guild.get_member(
            member_id
        )

        role = guild.get_role(
            role_id
        )

        if not member or not role:
            raise RuntimeError(
                "Üye veya rol bulunamadı."
            )

        assert_manageable(
            guild,
            member
        )

        if role >= guild.me.top_role:
            raise RuntimeError(
                "Bot bu rolü yönetemiyor."
            )

        if action == "add":
            await member.add_roles(
                role,
                reason="Web panel",
            )

            act = "ROLE_ADD"

        elif action == "remove":
            await member.remove_roles(
                role,
                reason="Web panel",
            )

            act = "ROLE_REMOVE"

        else:
            raise RuntimeError(
                "Bilinmeyen rol işlemi."
            )

        await send_modlog(
            guild,
            act,
            str(member),
            role.name,
        )

        return {"ok": True}

    try:
        return run_on_bot(do())

    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=400
        )


@app.post("/api/mod/voice")
async def api_voice(
    request: Request,
    member_id: int = Form(...),
    action: str = Form(...),
    channel_id: Optional[int] = Form(None),
):
    require_login(request)

    async def do():
        guild = selected_guild()

        member = guild.get_member(
            member_id
        )

        if not member:
            raise RuntimeError(
                "Üye bulunamadı."
            )

        assert_manageable(
            guild,
            member
        )

        if action == "mute":
            await member.edit(
                mute=True,
                reason="Web panel",
            )

        elif action == "unmute":
            await member.edit(
                mute=False,
                reason="Web panel",
            )

        elif action == "deafen":
            await member.edit(
                deafen=True,
                reason="Web panel",
            )

        elif action == "undeafen":
            await member.edit(
                deafen=False,
                reason="Web panel",
            )

        elif action == "disconnect":
            await member.move_to(
                None,
                reason="Web panel",
            )

        elif action == "move":
            ch = guild.get_channel(
                channel_id or 0
            )

            if not isinstance(
                ch,
                discord.VoiceChannel
            ):
                raise RuntimeError(
                    "Ses kanalı bulunamadı."
                )

            await member.move_to(
                ch,
                reason="Web panel",
            )

        else:
            raise RuntimeError(
                "Bilinmeyen voice işlemi."
            )

        await send_modlog(
            guild,
            f"VOICE_{action.upper()}",
            str(member),
            "Web panel",
        )

        return {"ok": True}

    try:
        return run_on_bot(do())

    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=400
        )


@app.post("/api/mod/warn")
async def api_warn(
    request: Request,
    member_id: int = Form(...),
    reason: str = Form(...),
):
    require_login(request)

    async def do():
        guild = selected_guild()

        member = guild.get_member(
            member_id
        )

        if not member:
            raise RuntimeError(
                "Üye bulunamadı."
            )

        key = str(member_id)

        warnings_db.setdefault(
            key,
            []
        ).append(
            {
                "reason": reason,
                "ts": int(
                    time.time()
                ),
            }
        )

        save_warnings()

        await send_modlog(
            guild,
            "WARN",
            str(member),
            reason,
        )

        return {
            "ok": True,
            "count": len(
                warnings_db[key]
            ),
        }

    try:
        return run_on_bot(do())

    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=400
        )


@app.post(
    "/api/mod/clear-warnings"
)
async def api_clear_warnings(
    request: Request,
    member_id: int = Form(...),
):
    require_login(request)

    warnings_db[
        str(member_id)
    ] = []

    save_warnings()

    return {"ok": True}


# =========================================================
# RUN
# =========================================================

def run_web():
    uvicorn.run(
        app,
        host=PANEL_HOST,
        port=PANEL_PORT,
        log_level="warning",
    )


if __name__ == "__main__":
    web_thread = threading.Thread(
        target=run_web,
        daemon=True,
    )

    web_thread.start()

    bot.run(DISCORD_TOKEN)