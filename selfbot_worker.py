"""Selfbot worker — runs as a subprocess, uses discord.py-self for Go Live streaming.

Reads JSON commands from stdin, writes JSON responses to stdout.
Each line is a complete JSON object.

Commands:
  {"cmd": "stream", "guild_id": 123, "channel_id": 456, "video_url": "...", "referer": "..."}
  {"cmd": "stop",   "guild_id": 123}
  {"cmd": "ping"}

Responses:
  {"ok": true,  "go_live": true}
  {"ok": false, "error": "..."}
  {"ok": true,  "pong": true}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

# ── Use discord.py-self from isolated directory ───────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_SELFBOT_LIBS = os.path.join(_HERE, ".selfbot_libs")
sys.path.insert(0, _SELFBOT_LIBS)

# Remove cached discord module if discord-py was already imported in this process
for _key in list(sys.modules.keys()):
    if _key == "discord" or _key.startswith("discord."):
        del sys.modules[_key]

import discord  # noqa: E402  (now gets discord.py-self)

import govideo  # noqa: E402,F401  (kept for reference — see do_stream() note on DAVE/4017)

# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [selfbot] %(levelname)s: %(message)s",
    stream=sys.stderr,   # keep stderr for logs; stdout is reserved for JSON responses
)
log = logging.getLogger(__name__)

# Load libopus explicitly — ctypes.util.find_library returns None on NixOS
# because libraries live in /nix/store, not the standard ldconfig paths.
# This worker is a separate process from the main bot and must load opus itself.
for _opus_path in [
    "/nix/store/0py9xncsn0s6vqxhvqblvhs2cqbb30s8-libopus-1.5.2/lib/libopus.so",
    "libopus.so.0",
    "libopus",
]:
    try:
        discord.opus.load_opus(_opus_path)
        log.info("loaded libopus from %s", _opus_path)
        break
    except Exception:
        pass
if not discord.opus.is_loaded():
    log.warning("libopus could not be loaded — voice playback will fail")

_FFMPEG_BASE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


class StreamBot(discord.Client):
    def __init__(self) -> None:
        super().__init__()
        self._active: dict[int, discord.VoiceClient] = {}
        self._video: dict[int, govideo.GoLiveVideoSession] = {}
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._read_task: asyncio.Task | None = None

    async def on_ready(self) -> None:
        log.info("ready — logged in as %s", self.user)
        # Guard against on_ready firing again on reconnect — only spawn the
        # stdin reader once, and keep a strong reference so the task isn't
        # garbage-collected mid-flight (asyncio only holds a weak reference
        # to fire-and-forget tasks created via ensure_future/create_task).
        if self._read_task is None or self._read_task.done():
            self._read_task = asyncio.ensure_future(self._read_commands())

    async def stop_stream(self, guild_id: int) -> None:
        # Go Live path: the GoLiveVideoSession owns the voice connection
        # itself (via discord-video-stream-py's VoiceStreamClient), so
        # stopping it also leaves the voice channel.
        video = self._video.pop(guild_id, None)
        if video:
            try:
                await video.stop()
            except Exception:
                pass

        # Audio-only fallback path: a plain discord.VoiceClient.
        vc = self._active.pop(guild_id, None)
        if not vc:
            return
        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
        except Exception:
            pass
        try:
            if vc.is_connected():
                await vc.disconnect(force=True)
        except Exception:
            pass
        log.info("stopped stream for guild %d", guild_id)

    async def do_stream(
        self, guild_id: int, channel_id: int, video_url: str, referer: str
    ) -> dict:
        try:
            guild = self.get_guild(guild_id)
            if not guild:
                return {"ok": False, "error": "السيرفر غير موجود — تأكد أن حساب USER_TOKEN عضو في السيرفر"}

            channel = guild.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.VoiceChannel):
                return {"ok": False, "error": "الروم الصوتي غير موجود"}

            await self.stop_stream(guild_id)

            # ── Attempt 1: DAVE-enabled Go Live video stream ──────────────
            # Uses DAVEVoiceStreamClient which implements the full MLS key
            # exchange required by Discord (error 4017 if missing).
            go_live_ok = False
            try:
                session = govideo.GoLiveVideoSession(self, guild_id, channel_id)
                await session.start()
                await session.send_video(video_url, referer)
                self._video[guild_id] = session
                go_live_ok = True
                log.info("Go Live video stream started (DAVE) for guild %d", guild_id)
                return {"ok": True, "go_live": go_live_ok}
            except Exception as video_exc:
                log.warning(
                    "Go Live video failed (guild %d): %s — falling back to audio-only",
                    guild_id, video_exc,
                )
                # Clean up any partial video session
                partial = self._video.pop(guild_id, None)
                if partial:
                    try:
                        await partial.stop()
                    except Exception:
                        pass

            # ── Fallback: audio-only via plain discord.VoiceClient ────────
            try:
                vc: discord.VoiceClient = await channel.connect()
            except discord.ClientException:
                existing = guild.voice_client
                if existing is not None:
                    try:
                        await existing.disconnect(force=True)
                    except Exception:
                        pass
                vc = await channel.connect()

            self._active[guild_id] = vc

            before_opts = _FFMPEG_BASE
            if referer:
                before_opts += f' -headers "Referer: {referer}\\r\\n"'

            source = discord.FFmpegPCMAudio(
                video_url,
                before_options=before_opts,
                options="-vn",
            )

            gid = guild_id
            bot = self

            def _after(error: Exception | None) -> None:
                if error:
                    log.warning("playback error guild %d: %s", gid, error)
                bot._active.pop(gid, None)
                asyncio.run_coroutine_threadsafe(_cleanup(vc), bot.loop)

            vc.play(source, after=_after)
            return {"ok": True, "go_live": go_live_ok}

        except Exception as exc:
            import traceback as _tb
            log.error(
                "do_stream error type=%s msg=%r\n%s",
                type(exc).__name__, str(exc), _tb.format_exc(),
            )
            self._active.pop(guild_id, None)
            err_msg = str(exc) or f"{type(exc).__name__} (no message)"
            return {"ok": False, "error": err_msg}

    async def _read_commands(self) -> None:
        """Read JSON commands from stdin in a background thread, dispatch them."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            try:
                line = await reader.readline()
                if not line:
                    log.info("stdin closed — shutting down")
                    await self.close()
                    break
                cmd = json.loads(line.decode().strip())
                asyncio.ensure_future(self._handle(cmd))
            except Exception as exc:
                log.error("command read error: %s", exc)

    async def _handle(self, cmd: dict) -> None:
        action = cmd.get("cmd")
        try:
            if action == "ping":
                _write({"ok": True, "pong": True, "_id": cmd.get("_id")})

            elif action == "stream":
                gid = int(cmd["guild_id"])
                lock = self._guild_locks.setdefault(gid, asyncio.Lock())
                async with lock:
                    result = await self.do_stream(
                        guild_id=gid,
                        channel_id=int(cmd["channel_id"]),
                        video_url=cmd["video_url"],
                        referer=cmd.get("referer", ""),
                    )
                result["_id"] = cmd.get("_id")
                _write(result)

            elif action == "stop":
                gid = int(cmd["guild_id"])
                lock = self._guild_locks.setdefault(gid, asyncio.Lock())
                async with lock:
                    await self.stop_stream(gid)
                _write({"ok": True, "_id": cmd.get("_id")})

            else:
                _write({"ok": False, "error": f"unknown command: {action}", "_id": cmd.get("_id")})
        except Exception as exc:
            log.error("_handle error for %s: %s", action, exc)
            _write({"ok": False, "error": str(exc), "_id": cmd.get("_id")})


async def _cleanup(vc: discord.VoiceClient) -> None:
    try:
        if vc.is_connected():
            await vc.disconnect(force=True)
    except Exception:
        pass


async def main() -> None:
    token = os.environ.get("USER_TOKEN", "").strip()
    if not token:
        log.error("USER_TOKEN not set — selfbot cannot start")
        sys.exit(1)

    bot = StreamBot()
    log.info("starting selfbot with USER_TOKEN …")
    await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
