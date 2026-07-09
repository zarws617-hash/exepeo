"""Go Live (video + audio) session — DAVE-aware stream gateway.

Strategy
--------
1. `channel.connect()` — discord.py-self VoiceClient joins the voice channel
   and handles the VOICE_STATE_UPDATE / VOICE_SERVER_UPDATE exchange.  This
   always works and gives us the session credentials (token, session_id,
   endpoint).

2. DAVEVoiceGateway — opens a SECOND voice-gateway WebSocket to the same
   voice server with those same credentials but requests Go Live (OP 18).
   Discord treats this as the "stream" session (separate SSRC/UDP).  This
   gateway handles the full DAVE MLS handshake so Discord doesn't close with
   4017.

3. DAVEMediaUdp — sends FFmpeg-encoded H.264 video packets (and audio) over
   the stream UDP socket with DAVE pre-encryption.

The audio VoiceClient (from step 1) stays connected in parallel, keeping the
user "in the voice channel" on Discord's side.

Public API (called from selfbot_worker.py)
------------------------------------------
    session = GoLiveVideoSession(bot, guild_id, channel_id)
    await session.start()
    await session.send_video(url, referer)
    await session.stop()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord

from mangastarz.dave_gateway import DAVEVoiceGateway, DAVEMediaUdp
from discord_video_stream.enums import Codec, StreamType

log = logging.getLogger(__name__)


class GoLiveNotSupported(Exception):
    """Raised when the stream session cannot be established."""


class GoLiveVideoSession:
    """Owns one Go Live session for a single guild."""

    def __init__(self, bot: discord.Client, guild_id: int, channel_id: int) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id

        # discord.py-self VoiceClient (audio — keeps us "in" the channel)
        self._voice_client: Optional[discord.VoiceClient] = None
        # DAVE stream gateway + UDP (video)
        self._dave_gateway: Optional[DAVEVoiceGateway] = None
        self._udp: Optional[DAVEMediaUdp] = None
        # Video player task
        self._play_task: Optional[asyncio.Task] = None
        # Gateway monitor task — watches closed_event and triggers _cleanup
        self._monitor_task: Optional[asyncio.Task] = None
        self._closed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Join the voice channel and establish the DAVE video stream."""
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            raise GoLiveNotSupported("السيرفر غير موجود لحساب البث")

        channel = guild.get_channel(self.channel_id)
        if channel is None:
            raise GoLiveNotSupported("الروم الصوتي غير موجود")

        # ── Step 1: Join via discord.py-self VoiceClient ─────────────
        # This uses discord.py-self's proven callback mechanism to receive
        # VOICE_STATE_UPDATE + VOICE_SERVER_UPDATE and populate credentials.
        try:
            self._voice_client = await asyncio.wait_for(
                channel.connect(), timeout=20.0
            )
        except asyncio.TimeoutError as exc:
            raise GoLiveNotSupported("انتهت مهلة الانضمام للروم الصوتي") from exc
        except Exception as exc:
            raise GoLiveNotSupported(f"فشل الانضمام للروم الصوتي: {exc}") from exc

        # ── Step 2: Extract voice credentials ────────────────────────
        conn = self._voice_client._connection
        token = conn.token
        session_id = conn.session_id
        endpoint = conn.endpoint  # discord.py-self already strips wss://

        if not token or not session_id or not endpoint:
            await self._voice_client.disconnect(force=True)
            raise GoLiveNotSupported("لم يتم استلام بيانات الاتصال الصوتي")

        # Strip trailing :80 if present (same as discord-video-stream-py)
        endpoint = endpoint.rstrip(":80")
        user_id = int(self.bot.user.id)

        log.info(
            "Voice credentials OK — session=%s endpoint=%s",
            session_id[:8] + "…", endpoint,
        )

        # ── Steps 3-4: Gateway + UDP (cleanup VoiceClient on any failure) ──
        # If anything from here onward raises, we must disconnect the already-
        # connected VoiceClient and close any partially-created gateway state.
        try:
            self._dave_gateway = DAVEVoiceGateway(
                endpoint=f"wss://{endpoint}?v=8",
                guild_id=self.guild_id,
                user_id=user_id,
                session_id=session_id,
                token=token,
                channel_id=self.channel_id,
            )

            try:
                udp_ip, udp_port, ssrc, secret_key, enc_mode = await asyncio.wait_for(
                    self._dave_gateway.connect(
                        width=1280, height=720, fps=30,
                        codec=Codec.H264,
                        stream_type=StreamType.GO_LIVE,
                    ),
                    timeout=30.0,
                )
            except asyncio.TimeoutError as exc:
                raise GoLiveNotSupported("انتهت مهلة إعداد stream gateway") from exc
            except Exception as exc:
                raise GoLiveNotSupported(f"فشل إعداد stream gateway: {exc}") from exc

            self._udp = DAVEMediaUdp(
                ip=udp_ip,
                port=udp_port,
                ssrc=ssrc,
                secret_key=secret_key,
                encryption_mode=enc_mode,
                dave_gateway=self._dave_gateway,
                codec=Codec.H264,
                fps=30,
                width=1280,
                height=720,
            )
            try:
                await self._udp.start()
            except Exception as exc:
                raise GoLiveNotSupported(f"فشل تشغيل UDP sender: {exc}") from exc

        except GoLiveNotSupported:
            # Teardown whatever was partially initialised, then re-raise
            await self._cleanup("failed during start()")
            raise

        # ── Step 5: Start gateway monitor ────────────────────────────
        # Watches the gateway's closed_event and calls _cleanup() if the
        # stream session dies unexpectedly (e.g. non-resumable 4006 with all
        # reconnect attempts exhausted).
        self._monitor_task = asyncio.create_task(
            self._monitor_gateway(), name="golive-gateway-monitor"
        )

        log.info(
            "Go Live stream ready (DAVE) — guild %d / channel %d / ssrc %d",
            self.guild_id, self.channel_id, ssrc,
        )

    async def send_video(self, video_url: str, referer: str = "") -> None:
        """Start streaming *video_url* over the established DAVE stream."""
        if self._udp is None:
            raise GoLiveNotSupported("اتصال البث المرئي غير جاهز")

        from discord_video_stream import VideoPlayer

        headers = f"Referer: {referer}\r\n" if referer else ""
        player = VideoPlayer(
            video_url,
            self._udp,
            codec=Codec.H264,
            resolution="720p",
            fps=30,
            headers=headers,
        )

        @player.on("error")
        async def _on_error(exc: Exception) -> None:
            log.warning("Go Live video playback error: %s", exc)

        self._play_task = asyncio.ensure_future(player.play())

    async def stop(self) -> None:
        """Stop playback, close stream gateway, and leave the voice channel."""
        await self._cleanup("stopped")

    # ------------------------------------------------------------------
    # Internal: single shared cleanup path
    # ------------------------------------------------------------------

    async def _cleanup(self, reason: str = "stopped") -> None:
        """
        Tear down the full session — play task, gateway, UDP, voice client.

        This is the single authoritative cleanup path.  Both stop() and
        _monitor_gateway() call it so there is no code-path divergence that
        could leave resources open or double-free.

        Safe to call from inside the monitor task (avoids awaiting itself).
        """
        if self._closed:
            return
        self._closed = True

        current_task = asyncio.current_task()

        # Cancel the monitor task (unless we ARE the monitor task)
        if (
            self._monitor_task
            and not self._monitor_task.done()
            and self._monitor_task is not current_task
        ):
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass

        # Stop the video player
        if self._play_task and not self._play_task.done():
            self._play_task.cancel()
            try:
                await self._play_task
            except (asyncio.CancelledError, Exception):
                pass

        # Stop the UDP sender (releases socket + internal sender tasks)
        if self._udp:
            try:
                stop_fn = getattr(self._udp, "stop", None) or getattr(self._udp, "close", None)
                if stop_fn is not None:
                    result = stop_fn()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:
                pass
            self._udp = None

        # Close the DAVE stream gateway
        if self._dave_gateway:
            try:
                await self._dave_gateway.close()
            except Exception:
                pass
            self._dave_gateway = None

        # Leave the voice channel
        if self._voice_client:
            try:
                await self._voice_client.disconnect(force=True)
            except Exception:
                pass
            self._voice_client = None

        log.info(
            "Go Live session %s — guild %d / channel %d",
            reason, self.guild_id, self.channel_id,
        )

    # ------------------------------------------------------------------
    # Internal: gateway health monitor
    # ------------------------------------------------------------------

    async def _monitor_gateway(self) -> None:
        """Wait for the gateway's closed_event then call _cleanup().

        Runs as a background task alongside the play task.  If the DAVE
        gateway closes non-resumably (e.g. Discord sends code 4006 after
        all reconnect attempts are exhausted), _cleanup() is called so the
        play task stops sending into a dead socket and the voice channel is
        left cleanly.
        """
        if self._dave_gateway is None:
            return
        try:
            await self._dave_gateway.closed_event.wait()
        except asyncio.CancelledError:
            return

        if self._closed:
            return  # _cleanup() already running or done — nothing to do

        log.warning(
            "Go Live gateway closed unexpectedly (guild %d) — tearing down session",
            self.guild_id,
        )
        await self._cleanup("stopped after gateway failure")
