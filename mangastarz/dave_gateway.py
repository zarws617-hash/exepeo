"""DAVE-aware voice stream gateway for Discord Go Live video streaming.

Discord enforces DAVE (Audio & Video End-to-End Encryption) on all stream/voice
gateway connections (close code 4017 if missing).  The `discord-video-stream-py`
library's built-in VoiceGateway does NOT implement DAVE.  This module subclasses
it to add full DAVE support using the `davey` library (already a project dep).

Design
------
* DAVEVoiceGateway   – subclasses VoiceGateway, adds DAVE handshake
* DAVEVoiceStreamClient – subclasses VoiceStreamClient, injects the gateway
* DAVEMediaUdp          – subclasses MediaUdp, applies DAVE before RTP encrypt

Usage (same interface as govideo.py's GoLiveVideoSession):
    session = GoLiveVideoSession(bot, guild_id, channel_id)
    await session.start()
    await session.send_video(video_url, referer)
    await session.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from typing import Callable, Optional

import websockets

from discord_video_stream.voice.gateway import (
    VoiceGateway,
    OP_HEARTBEAT, OP_HEARTBEAT_ACK, OP_RESUMED, OP_HELLO,
    OP_IDENTIFY, OP_READY, OP_SELECT_PROTOCOL, OP_SESSION_DESCRIPTION,
    OP_SPEAKING, OP_VIDEO,
    SUPPORTED_ENCRYPTION_MODES,
    MAX_RECONNECT_ATTEMPTS, RECONNECT_BACKOFF_BASE,
)
from discord_video_stream.voice.client import VoiceStreamClient
from discord_video_stream.voice.udp import MediaUdp, AUDIO_SAMPLES_PER_FRAME, VIDEO_CLOCK_RATE
from discord_video_stream.voice.rtp import build_audio_rtp_header, build_video_rtp_header, H264_PAYLOAD_TYPE, VP8_PAYLOAD_TYPE
from discord_video_stream.voice.encryption import encrypt_packet
from discord_video_stream.enums import Codec, StreamType, Resolution

log = logging.getLogger(__name__)

# ── Discord voice gateway close codes ────────────────────────────────────────
# Non-resumable: OP 7 RESUME will always be rejected; must re-IDENTIFY instead.
_VOICE_NON_RESUMABLE = frozenset({
    4001,  # Unknown opcode
    4002,  # Failed to decode payload
    4003,  # Not authenticated (sent before IDENTIFY)
    4005,  # Already authenticated
    4006,  # Session is no longer valid  ← the primary culprit
    4009,  # Session timed out
    4017,  # DAVE protocol required / MLS processing failed
})
# Fatal: reconnection of any kind won't help.
_VOICE_FATAL = frozenset({
    4004,  # Authentication failed (bad token)
    4011,  # No server found
    4012,  # Unknown protocol
    4013,  # Unknown encryption mode
    4014,  # Disconnected (kicked from channel)
    4016,  # Unknown encryption mode (duplicate)
})

# DAVE / MLS OP codes (text messages — discord.py-self naming)
OP_DAVE_PREPARE_TRANSITION        = 21
OP_DAVE_EXECUTE_TRANSITION        = 22
OP_DAVE_TRANSITION_READY          = 23
OP_DAVE_PREPARE_EPOCH             = 24

# DAVE / MLS OP codes (binary messages)
OP_MLS_EXTERNAL_SENDER            = 25
OP_MLS_KEY_PACKAGE                = 26
OP_MLS_PROPOSALS                  = 27
OP_MLS_COMMIT_WELCOME             = 28
OP_MLS_ANNOUNCE_COMMIT_TRANSITION = 29
OP_MLS_WELCOME                    = 30
OP_MLS_INVALID_COMMIT_WELCOME     = 31


class DAVEVoiceGateway(VoiceGateway):
    """
    Extends VoiceGateway with full DAVE (MLS E2EE) support.

    Changes vs. base class
    ----------------------
    * IDENTIFY includes ``max_dave_protocol_version: 1``
    * Binary WebSocket messages (MLS key exchange) are handled in the
      receive loop instead of crashing json.loads()
    * DAVE OPs 21-31 are handled in both the handshake path and the
      background receive loop
    * ``dave_session`` property exposes the live davey.DaveSession so
      DAVEMediaUdp can encrypt frames with it
    * Non-resumable close codes (e.g. 4006) trigger a full IDENTIFY reconnect
      instead of a futile OP 7 RESUME attempt that always returns 4006 again
    * ``closed_event`` is set whenever the gateway closes non-resumably so
      callers (GoLiveVideoSession) can react immediately
    """

    def __init__(
        self,
        endpoint: str,
        guild_id: int,
        user_id: int,
        session_id: str,
        token: str,
        channel_id: int = 0,
    ) -> None:
        super().__init__(endpoint, guild_id, user_id, session_id, token)
        self._channel_id = channel_id
        self._dave_session = None
        self._dave_protocol_version: int = 0
        self._dave_pending_transitions: dict[int, int] = {}

        # Stream parameters — saved so _reconnect_identify can resend OP 18
        self._stream_width: int = 0
        self._stream_height: int = 0
        self._stream_fps: int = 30
        self._stream_codec: Optional[Codec] = None
        self._stream_type: Optional[StreamType] = None

        # Fired whenever the gateway closes in a way the session cannot survive
        self.closed_event: asyncio.Event = asyncio.Event()

        # Callbacks registered by DAVEMediaUdp to react to SSRC changes
        self._ssrc_change_callbacks: list[Callable[[int], None]] = []
        # Callbacks registered by DAVEMediaUdp to receive updated crypto on reconnect
        # Signature: cb(secret_key: list[int], encryption_mode: str)
        self._session_update_callbacks: list[Callable[[list, str], None]] = []

    # ------------------------------------------------------------------
    # Public property
    # ------------------------------------------------------------------

    @property
    def dave_session(self):
        """Live davey.DaveSession (None if DAVE not active)."""
        return self._dave_session

    @property
    def dave_can_encrypt(self) -> bool:
        """True once the DAVE session is fully ready to encrypt media."""
        return (
            self._dave_protocol_version != 0
            and self._dave_session is not None
            and self._dave_session.ready
        )

    def register_ssrc_callback(self, cb: Callable[[int], None]) -> None:
        """Let DAVEMediaUdp register a callback to receive new SSRC values."""
        self._ssrc_change_callbacks.append(cb)

    def register_session_callback(self, cb: Callable[[list, str], None]) -> None:
        """Let DAVEMediaUdp register a callback to receive new crypto after reconnect.

        cb(secret_key: list[int], encryption_mode: str) — called atomically
        after a successful full re-IDENTIFY so the UDP sender can swap crypto
        before the next outbound packet.
        """
        self._session_update_callbacks.append(cb)

    # ------------------------------------------------------------------
    # Override: full connect() with DAVE
    # ------------------------------------------------------------------

    async def connect(
        self,
        *,
        width: int,
        height: int,
        fps: int,
        codec: Codec,
        stream_type: StreamType,
    ) -> tuple[str, int, int, list[int], str]:
        # Save stream parameters for use in _reconnect_identify
        self._stream_width = width
        self._stream_height = height
        self._stream_fps = fps
        self._stream_codec = codec
        self._stream_type = stream_type

        log.info("DAVEVoiceGateway: connecting to %s", self._endpoint)
        self._ws = await websockets.connect(
            self._endpoint,
            max_size=None,
            ping_interval=None,
        )

        # OP 8 Hello
        hello = await self._recv_op_skip_binary(OP_HELLO)
        self._heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000.0

        # OP 0 Identify — WITH max_dave_protocol_version
        await self._send(OP_IDENTIFY, {
            "server_id": str(self._guild_id),
            "user_id": str(self._user_id),
            "session_id": self._session_id,
            "token": self._token,
            "max_dave_protocol_version": 1,
        })

        # OP 2 Ready
        ready = await self._recv_op_skip_binary(OP_READY)
        self._ssrc = ready["d"]["ssrc"]
        udp_ip_raw = ready["d"]["ip"]
        udp_port = ready["d"]["port"]
        modes: list[str] = ready["d"]["modes"]

        # UDP hole-punch (base class method)
        discovered_ip, discovered_port = await self._udp_hole_punch(udp_ip_raw, udp_port)

        self._encryption_mode = next(
            (m for m in SUPPORTED_ENCRYPTION_MODES if m in modes),
            modes[0],
        )

        # OP 1 Select Protocol
        await self._send(OP_SELECT_PROTOCOL, {
            "protocol": "udp",
            "data": {
                "address": discovered_ip,
                "port": discovered_port,
                "mode": self._encryption_mode,
            },
        })

        # OP 4 Session Description — may have dave_protocol_version
        session_desc = await self._recv_op_skip_binary(OP_SESSION_DESCRIPTION)
        self._secret_key = session_desc["d"]["secret_key"]
        self._encryption_mode = session_desc["d"]["mode"]
        dave_pv = session_desc["d"].get("dave_protocol_version", 0) or 0
        log.info("SESSION_DESCRIPTION received, dave_protocol_version=%d", dave_pv)

        # Init DAVE session if required
        if dave_pv > 0:
            await self._init_dave_session(dave_pv)

        # OP 18 Video (Go Live signalling)
        if stream_type == StreamType.GO_LIVE:
            await self._send_video_op(width, height, fps, codec)

        # OP 5 Speaking
        await self.set_speaking(True)

        # Start background heartbeat + receive loop
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="dave-voice-heartbeat"
        )
        self._recv_task = asyncio.create_task(
            self._receive_loop(), name="dave-voice-recv-loop"
        )

        self._reconnect_attempts = 0
        return udp_ip_raw, udp_port, self._ssrc, self._secret_key, self._encryption_mode

    # ------------------------------------------------------------------
    # DAVE initialisation
    # ------------------------------------------------------------------

    async def _init_dave_session(self, dave_protocol_version: int) -> None:
        """Create a DaveSession and send the MLS key package to Discord."""
        try:
            import davey
            self._dave_protocol_version = dave_protocol_version
            if self._dave_session is not None:
                self._dave_session.reinit(dave_protocol_version, self._user_id, self._channel_id)
            else:
                self._dave_session = davey.DaveSession(
                    dave_protocol_version, self._user_id, self._channel_id
                )
            key_package = self._dave_session.get_serialized_key_package()
            await self._send_binary_msg(OP_MLS_KEY_PACKAGE, key_package)
            log.info("DAVE session created, MLS_KEY_PACKAGE sent (protocol v%d)", dave_protocol_version)
        except Exception as exc:
            log.warning("DAVE init failed: %s — proceeding without DAVE encryption", exc)

    # ------------------------------------------------------------------
    # Binary message helpers
    # ------------------------------------------------------------------

    async def _send_binary_msg(self, op: int, data: bytes) -> None:
        """Send a binary gateway message: op (1 byte) + data."""
        if self._ws:
            await self._ws.send(bytes([op]) + data)

    async def _handle_binary(self, raw: bytes) -> None:
        """Dispatch an incoming binary gateway message."""
        if len(raw) < 3:
            return
        # Format: seq_ack (2 bytes) + op (1 byte) + data
        op = raw[2]
        data = raw[3:]

        if self._dave_session is None:
            log.debug("Binary OP %d received but no DAVE session yet, ignoring", op)
            return

        try:
            await self._dispatch_binary_dave(op, data)
        except Exception as exc:
            log.warning("DAVE binary OP %d handling error: %s", op, exc)

    async def _dispatch_binary_dave(self, op: int, data: bytes) -> None:
        import davey

        if op == OP_MLS_EXTERNAL_SENDER:
            self._dave_session.set_external_sender(data)
            log.debug("DAVE: MLS_EXTERNAL_SENDER set")

        elif op == OP_MLS_PROPOSALS:
            if len(data) < 1:
                return
            optype = data[0]
            result = self._dave_session.process_proposals(
                davey.ProposalsOperationType.append if optype == 0
                else davey.ProposalsOperationType.revoke,
                data[1:],
            )
            if isinstance(result, davey.CommitWelcome):
                commit_data = (
                    result.commit + result.welcome if result.welcome
                    else result.commit
                )
                await self._send_binary_msg(OP_MLS_COMMIT_WELCOME, commit_data)
                log.debug("DAVE: proposals processed, MLS_COMMIT_WELCOME sent")

        elif op == OP_MLS_ANNOUNCE_COMMIT_TRANSITION:
            if len(data) < 2:
                return
            transition_id = struct.unpack_from(">H", data, 0)[0]
            commit_data = data[2:]
            try:
                self._dave_session.process_commit(commit_data)
                if transition_id != 0:
                    self._dave_pending_transitions[transition_id] = self._dave_protocol_version
                    await self._send(OP_DAVE_TRANSITION_READY, {"transition_id": transition_id})
                log.info(
                    "DAVE: commit processed (transition %d), session ready=%s",
                    transition_id, self._dave_session.ready,
                )
            except Exception as exc:
                log.warning("DAVE: commit processing failed: %s — recovering", exc)
                await self._recover_from_invalid_commit(transition_id)

        elif op == OP_MLS_WELCOME:
            if len(data) < 2:
                return
            transition_id = struct.unpack_from(">H", data, 0)[0]
            welcome_data = data[2:]
            try:
                self._dave_session.process_welcome(welcome_data)
                if transition_id != 0:
                    self._dave_pending_transitions[transition_id] = self._dave_protocol_version
                    await self._send(OP_DAVE_TRANSITION_READY, {"transition_id": transition_id})
                log.info(
                    "DAVE: welcome processed (transition %d), session ready=%s",
                    transition_id, self._dave_session.ready,
                )
            except Exception as exc:
                log.warning("DAVE: welcome processing failed: %s — recovering", exc)
                await self._recover_from_invalid_commit(transition_id)

        else:
            log.debug("DAVE: unhandled binary OP %d", op)

    # ------------------------------------------------------------------
    # Text DAVE OP handling
    # ------------------------------------------------------------------

    async def _handle_text_dave(self, op: int, data: dict) -> None:
        """Handle a text (JSON) DAVE opcode."""
        if op == OP_DAVE_PREPARE_TRANSITION:
            transition_id = data.get("transition_id", 0)
            protocol_version = data.get("protocol_version", 0)
            self._dave_pending_transitions[transition_id] = protocol_version
            if transition_id == 0:
                await self._execute_dave_transition(transition_id)
            else:
                if protocol_version == 0 and self._dave_session:
                    self._dave_session.set_passthrough_mode(True, 120)
                await self._send(OP_DAVE_TRANSITION_READY, {"transition_id": transition_id})
            log.debug("DAVE: PREPARE_TRANSITION %d (v%d)", transition_id, protocol_version)

        elif op == OP_DAVE_EXECUTE_TRANSITION:
            transition_id = data.get("transition_id", 0)
            await self._execute_dave_transition(transition_id)
            log.debug("DAVE: EXECUTE_TRANSITION %d", transition_id)

        elif op == OP_DAVE_PREPARE_EPOCH:
            epoch = data.get("epoch", 0)
            protocol_version = data.get("protocol_version", self._dave_protocol_version)
            log.debug("DAVE: PREPARE_EPOCH %d (v%d)", epoch, protocol_version)
            if epoch == 1 and self._dave_session:
                try:
                    self._dave_session.reinit(protocol_version, self._user_id, self._channel_id)
                    key_package = self._dave_session.get_serialized_key_package()
                    await self._send_binary_msg(OP_MLS_KEY_PACKAGE, key_package)
                except Exception as exc:
                    log.warning("DAVE: epoch reinit failed: %s", exc)

    async def _execute_dave_transition(self, transition_id: int) -> None:
        """Apply a pending DAVE protocol-version transition."""
        new_version = self._dave_pending_transitions.pop(transition_id, None)
        if new_version is None:
            log.debug("DAVE: execute_transition %d — no pending entry, skipping", transition_id)
            return

        old_version = self._dave_protocol_version
        self._dave_protocol_version = new_version
        log.debug(
            "DAVE: transition %d executed — protocol version %d → %d",
            transition_id, old_version, new_version,
        )

        if new_version == 0:
            # Downgrade to plaintext: put session in passthrough mode
            if self._dave_session:
                try:
                    self._dave_session.set_passthrough_mode(True, 10)
                except Exception:
                    pass
            log.info("DAVE: downgraded to passthrough (no E2EE)")
        elif self._dave_session:
            log.info("DAVE: transition complete, session ready=%s", self._dave_session.ready)

    async def _recover_from_invalid_commit(self, transition_id: int) -> None:
        """Send MLS_INVALID_COMMIT_WELCOME and reinitialise the key package."""
        try:
            # Notify Discord that the commit/welcome was invalid
            payload = struct.pack(">H", transition_id) if transition_id != 0 else b""
            await self._send_binary_msg(OP_MLS_INVALID_COMMIT_WELCOME, payload)
            # Re-create the session so we can participate again
            if self._dave_session and self._dave_protocol_version > 0:
                self._dave_session.reinit(
                    self._dave_protocol_version, self._user_id, self._channel_id
                )
                key_package = self._dave_session.get_serialized_key_package()
                await self._send_binary_msg(OP_MLS_KEY_PACKAGE, key_package)
                log.info("DAVE: invalid commit recovered, new key package sent")
        except Exception as exc:
            log.warning("DAVE: recovery from invalid commit failed: %s", exc)

    # ------------------------------------------------------------------
    # Override _recv_op to skip binary during handshake
    # ------------------------------------------------------------------

    async def _recv_op_skip_binary(self, expected_op: int, *, timeout: float = 30.0) -> dict:
        """Like _recv_op but silently skips binary frames."""
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for OP {expected_op}")
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError(f"Timed out waiting for OP {expected_op}")
            if isinstance(raw, bytes):
                log.debug("Skipping binary frame during handshake (expecting OP %d)", expected_op)
                continue
            msg = json.loads(raw)
            op = msg.get("op")
            if op == expected_op:
                return msg
            log.debug("Skipping OP %s while waiting for OP %d", op, expected_op)

    # ------------------------------------------------------------------
    # Full IDENTIFY reconnect (for non-resumable close codes like 4006)
    # ------------------------------------------------------------------

    async def _reconnect_identify(self) -> bool:
        """
        Attempt a full re-IDENTIFY after a non-resumable disconnect.

        Unlike OP 7 RESUME (which Discord rejects with 4006 again), this
        opens a brand-new WebSocket and sends OP 0 IDENTIFY from scratch.
        Returns True if the reconnect succeeded, False if all attempts failed.
        """
        for attempt in range(MAX_RECONNECT_ATTEMPTS):
            delay = min(RECONNECT_BACKOFF_BASE * (2 ** attempt), 30.0)
            log.info(
                "Voice gateway full reconnect attempt %d/%d in %.1fs…",
                attempt + 1, MAX_RECONNECT_ATTEMPTS, delay,
            )

            # Bail out early if stop() was called while we were sleeping/retrying
            if self._closed:
                log.info("Voice gateway reconnect aborted — session was closed externally")
                return False

            await asyncio.sleep(delay)

            # Check again after the sleep (stop() may have arrived during the wait)
            if self._closed:
                log.info("Voice gateway reconnect aborted — session was closed externally")
                return False

            try:
                # ── 1. Stop heartbeat BEFORE touching self._ws ────────
                # The heartbeat loop holds a reference to self._ws; swapping
                # the socket while heartbeat is mid-send can send OP 3 on an
                # unauthenticated new connection, causing 4003/4001 failures.
                if self._heartbeat_task and not self._heartbeat_task.done():
                    self._heartbeat_task.cancel()
                    try:
                        await asyncio.wait_for(self._heartbeat_task, timeout=2.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                        pass
                    self._heartbeat_task = None

                # ── 2. Close old socket cleanly ───────────────────────
                try:
                    await self._ws.close()
                except Exception:
                    pass

                # ── 3. New WebSocket ──────────────────────────────────
                self._ws = await websockets.connect(
                    self._endpoint,
                    max_size=None,
                    ping_interval=None,
                )

                # ── Handshake (mirrors connect()) ─────────────────────
                hello = await self._recv_op_skip_binary(OP_HELLO, timeout=15.0)
                self._heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000.0

                await self._send(OP_IDENTIFY, {
                    "server_id": str(self._guild_id),
                    "user_id": str(self._user_id),
                    "session_id": self._session_id,
                    "token": self._token,
                    "max_dave_protocol_version": 1,
                })

                ready = await self._recv_op_skip_binary(OP_READY, timeout=15.0)
                new_ssrc = ready["d"]["ssrc"]
                modes: list[str] = ready["d"]["modes"]
                udp_ip_raw = ready["d"]["ip"]
                udp_port = ready["d"]["port"]

                # Hole-punch for the new session
                discovered_ip, discovered_port = await self._udp_hole_punch(udp_ip_raw, udp_port)

                self._encryption_mode = next(
                    (m for m in SUPPORTED_ENCRYPTION_MODES if m in modes),
                    modes[0],
                )

                await self._send(OP_SELECT_PROTOCOL, {
                    "protocol": "udp",
                    "data": {
                        "address": discovered_ip,
                        "port": discovered_port,
                        "mode": self._encryption_mode,
                    },
                })

                session_desc = await self._recv_op_skip_binary(OP_SESSION_DESCRIPTION, timeout=15.0)
                self._secret_key = session_desc["d"]["secret_key"]
                self._encryption_mode = session_desc["d"]["mode"]
                dave_pv = session_desc["d"].get("dave_protocol_version", 0) or 0

                # ── Notify DAVEMediaUdp of new session credentials ───
                # Always fire both callbacks atomically so UDP sends the
                # next packet with the correct key, mode, AND SSRC.
                if new_ssrc != self._ssrc:
                    log.info(
                        "Voice gateway reconnect: SSRC changed %d → %d",
                        self._ssrc, new_ssrc,
                    )
                    self._ssrc = new_ssrc
                    for cb in self._ssrc_change_callbacks:
                        try:
                            cb(new_ssrc)
                        except Exception:
                            pass

                # Fire crypto update regardless of SSRC change — Discord
                # always issues a fresh secret_key on each IDENTIFY exchange.
                for cb in self._session_update_callbacks:
                    try:
                        cb(self._secret_key, self._encryption_mode)
                    except Exception:
                        pass

                # Re-init DAVE
                if dave_pv > 0:
                    await self._init_dave_session(dave_pv)

                # Re-send OP 18 for Go Live
                if self._stream_type == StreamType.GO_LIVE and self._stream_codec is not None:
                    await self._send_video_op(
                        self._stream_width, self._stream_height,
                        self._stream_fps, self._stream_codec,
                    )

                # OP 5 Speaking
                await self.set_speaking(True)

                # Restart heartbeat (was cancelled at the top of this attempt)
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(), name="dave-voice-heartbeat"
                )

                self._reconnect_attempts = 0
                log.info(
                    "Voice gateway full reconnect succeeded (attempt %d/%d)",
                    attempt + 1, MAX_RECONNECT_ATTEMPTS,
                )
                return True

            except Exception as exc:
                log.warning(
                    "Voice gateway full reconnect attempt %d/%d failed: %s",
                    attempt + 1, MAX_RECONNECT_ATTEMPTS, exc,
                )
                self._reconnect_attempts += 1

        log.error(
            "Voice gateway full reconnect failed after %d attempts — stream session terminated",
            MAX_RECONNECT_ATTEMPTS,
        )
        return False

    # ------------------------------------------------------------------
    # Override receive loop to handle binary + DAVE text OPs
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        """Process incoming gateway messages (acks, DAVE exchange, resumes)."""
        try:
            while not self._closed:
                try:
                    raw = await self._ws.recv()
                except websockets.exceptions.ConnectionClosed as exc:
                    if self._closed:
                        return

                    rcvd_code = exc.rcvd.code if exc.rcvd else None

                    if rcvd_code in _VOICE_FATAL:
                        log.error(
                            "Voice gateway closed with fatal code %s — stream session cannot continue",
                            exc,
                        )
                        self._closed = True
                        self.closed_event.set()
                        return

                    if rcvd_code in _VOICE_NON_RESUMABLE:
                        log.warning(
                            "Voice gateway closed with non-resumable code %s "
                            "(skipping RESUME, attempting full IDENTIFY reconnect)",
                            exc,
                        )
                        reconnected = await self._reconnect_identify()
                        if reconnected:
                            # Back in business — continue the receive loop on the new WS
                            continue
                        # All reconnect attempts exhausted
                        self._closed = True
                        self.closed_event.set()
                        return

                    # Resumable disconnect (e.g. 4015 server crash, 1000/1001 clean close)
                    log.warning("Voice gateway closed (%s), attempting resume", exc)
                    await self._resume()
                    return

                if isinstance(raw, bytes):
                    await self._handle_binary(raw)
                    continue

                msg = json.loads(raw)
                op = msg.get("op")
                data = msg.get("d", {})

                if op == OP_HEARTBEAT_ACK:
                    self._reconnect_attempts = 0
                elif op == OP_RESUMED:
                    log.info("Voice gateway session resumed")
                    self._reconnect_attempts = 0
                elif op == OP_HELLO:
                    self._heartbeat_interval = data.get("heartbeat_interval", 41250) / 1000.0
                    log.debug("OP_HELLO during receive loop — heartbeat interval updated")
                elif op in (
                    OP_DAVE_PREPARE_TRANSITION, OP_DAVE_EXECUTE_TRANSITION,
                    OP_DAVE_TRANSITION_READY, OP_DAVE_PREPARE_EPOCH,
                ):
                    await self._handle_text_dave(op, data)
                else:
                    log.debug("Unhandled OP %s in receive loop", op)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self._closed:
                log.error("Receive loop error: %s", exc, exc_info=True)
                self.closed_event.set()

    # ------------------------------------------------------------------
    # Override heartbeat loop (unchanged but needed for self._closed)
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closed:
                nonce = int(time.time() * 1000)
                await self._send(OP_HEARTBEAT, nonce)
                log.debug("Heartbeat sent nonce=%d", nonce)
                await asyncio.sleep(self._heartbeat_interval)
        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed:
            log.warning("Connection closed during heartbeat.")

    # ------------------------------------------------------------------
    # Override close() to also fire closed_event
    # ------------------------------------------------------------------

    async def close(self) -> None:
        self._closed = True
        self.closed_event.set()
        try:
            await super().close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# DAVEVoiceStreamClient — injects DAVEVoiceGateway into create_stream()
# ---------------------------------------------------------------------------

class DAVEVoiceStreamClient(VoiceStreamClient):
    """Extends VoiceStreamClient so create_stream() uses DAVEVoiceGateway."""

    async def create_stream(
        self,
        *,
        resolution: str | Resolution = Resolution.R720P,
        fps: int = 30,
        codec: str | Codec = Codec.H264,
        stream_type: str | StreamType = StreamType.GO_LIVE,
    ) -> "DAVEMediaUdp":
        if self._voice_state is None or self._voice_server is None:
            raise RuntimeError("Voice connection not fully established.")

        res = Resolution(resolution) if isinstance(resolution, str) else resolution
        codec = Codec(codec) if isinstance(codec, str) else codec
        stream_type = StreamType(stream_type) if isinstance(stream_type, str) else stream_type

        width, height = res.dimensions()
        endpoint = self._voice_server["endpoint"].rstrip(":80")

        guild_id = int(self._voice_state["guild_id"])
        user_id = int(self._voice_state["user_id"])
        session_id = self._voice_state["session_id"]
        token = self._voice_server["token"]
        channel_id = int(self._voice_state.get("channel_id") or 0)

        dave_gateway = DAVEVoiceGateway(
            endpoint=f"wss://{endpoint}?v=8",
            guild_id=guild_id,
            user_id=user_id,
            session_id=session_id,
            token=token,
            channel_id=channel_id,
        )
        self._gateway = dave_gateway

        udp_ip, udp_port, ssrc, secret_key, enc_mode = await dave_gateway.connect(
            width=width, height=height, fps=fps,
            codec=codec, stream_type=stream_type,
        )

        self._udp = DAVEMediaUdp(
            ip=udp_ip,
            port=udp_port,
            ssrc=ssrc,
            secret_key=secret_key,
            encryption_mode=enc_mode,
            dave_gateway=dave_gateway,
            codec=codec,
            fps=fps,
            width=width,
            height=height,
        )
        await self._udp.start()
        return self._udp


# ---------------------------------------------------------------------------
# DAVEMediaUdp — applies DAVE encryption before standard RTP encryption
# ---------------------------------------------------------------------------

class DAVEMediaUdp(MediaUdp):
    """
    Extends MediaUdp to apply DAVE (E2EE) encryption when the session is ready.

    Audio frames: dave_session.encrypt_opus(opus_bytes) → then RTP encrypt
    Video frames: dave_session.encrypt(codec, h264_bytes) → then RTP encrypt
    """

    def __init__(
        self,
        *args,
        dave_gateway: Optional[DAVEVoiceGateway] = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._dave_gateway = dave_gateway
        # Import davey codec mapping once
        try:
            import davey as _davey
            self._davey_codec_map = {
                Codec.H264: _davey.Codec.h264,
                Codec.VP8: _davey.Codec.vp8,
            }
            self._davey = _davey
        except ImportError:
            self._davey_codec_map = {}
            self._davey = None

        # Preserve the delta between video SSRC and audio SSRC so we can
        # replicate it when the gateway reconnects with a new SSRC value.
        # (The base MediaUdp class sets _video_ssrc = _ssrc + <some constant>.)
        self._video_ssrc_delta: int = (
            getattr(self, "_video_ssrc", self._ssrc) - self._ssrc
        )

        # Register callbacks so reconnect propagates both SSRC and crypto to us
        if dave_gateway is not None:
            dave_gateway.register_ssrc_callback(self._on_ssrc_change)
            dave_gateway.register_session_callback(self._on_session_update)

    def _on_ssrc_change(self, new_ssrc: int) -> None:
        """Called by DAVEVoiceGateway when the SSRC changes after reconnect."""
        log.info(
            "DAVEMediaUdp: SSRC updated %d → %d (video %d → %d)",
            self._ssrc, new_ssrc,
            getattr(self, "_video_ssrc", self._ssrc),
            new_ssrc + self._video_ssrc_delta,
        )
        self._ssrc = new_ssrc
        # Keep video SSRC in sync using the original delta from the base class
        if hasattr(self, "_video_ssrc"):
            self._video_ssrc = new_ssrc + self._video_ssrc_delta

    def _on_session_update(self, secret_key: list, encryption_mode: str) -> None:
        """Called by DAVEVoiceGateway after a successful full reconnect.

        Atomically swaps the crypto material so the next outbound RTP packet
        uses the correct session key and encryption mode issued by Discord for
        the new IDENTIFY session.  Called with the key already validated by the
        SESSION_DESCRIPTION handshake, so it is safe to apply immediately.
        """
        log.info(
            "DAVEMediaUdp: crypto updated (mode %s → %s, key length %d)",
            getattr(self, "_encryption_mode", "?"), encryption_mode, len(secret_key),
        )
        self._secret_key = secret_key
        self._encryption_mode = encryption_mode

    def _dave_encrypt_audio(self, opus_frame: bytes) -> bytes:
        """Apply DAVE encryption to an Opus frame if DAVE is ready."""
        if (
            self._dave_gateway
            and self._dave_gateway.dave_can_encrypt
            and self._davey
        ):
            try:
                return self._dave_gateway.dave_session.encrypt_opus(opus_frame)
            except Exception as exc:
                log.debug("DAVE audio encrypt failed: %s", exc)
        return opus_frame

    def _dave_encrypt_video(self, payload: bytes) -> bytes:
        """Apply DAVE encryption to a video payload if DAVE is ready.

        davey API: DaveSession.encrypt(media_type, codec, packet)
        """
        if (
            self._dave_gateway
            and self._dave_gateway.dave_can_encrypt
            and self._davey
        ):
            davey_codec = self._davey_codec_map.get(self._codec)
            if davey_codec is not None:
                try:
                    return self._dave_gateway.dave_session.encrypt(
                        self._davey.MediaType.video, davey_codec, payload
                    )
                except Exception as exc:
                    log.debug("DAVE video encrypt failed: %s", exc)
        return payload

    # ------------------------------------------------------------------
    # Override send methods
    # ------------------------------------------------------------------

    async def send_audio_frame(self, opus_frame: bytes) -> None:
        """Send one 20 ms Opus frame with optional DAVE pre-encryption."""
        payload = self._dave_encrypt_audio(opus_frame)

        header = build_audio_rtp_header(
            sequence=self._audio_seq,
            timestamp=self._audio_ts,
            ssrc=self._ssrc,
        )
        packet = encrypt_packet(
            header, payload, self._secret_key,
            self._encryption_mode, nonce_counter=self._nonce_counter,
        )
        await self._send_raw(packet)
        self._nonce_counter = (self._nonce_counter + 1) & 0xFFFFFFFF
        self._audio_seq = (self._audio_seq + 1) & 0xFFFF
        self._audio_ts = (self._audio_ts + AUDIO_SAMPLES_PER_FRAME) & 0xFFFFFFFF

    async def send_video_packets(
        self,
        rtp_payloads: list[bytes],
        *,
        is_keyframe: bool = False,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        """Send video frame packets with optional DAVE pre-encryption."""
        if not rtp_payloads:
            return

        frame_width = width or self._width
        frame_height = height or self._height
        n = len(rtp_payloads)

        for i, raw_payload in enumerate(rtp_payloads):
            is_last = (i == n - 1)
            include_ext = is_keyframe and (i == 0)

            # Apply DAVE encryption to the raw NAL/VP8 payload
            payload = self._dave_encrypt_video(raw_payload)

            header = build_video_rtp_header(
                sequence=self._video_seq,
                timestamp=self._video_ts,
                ssrc=self._video_ssrc,
                payload_type=self._video_pt,
                marker=is_last,
                width=frame_width if include_ext else 0,
                height=frame_height if include_ext else 0,
            )
            packet = encrypt_packet(
                header, payload, self._secret_key,
                self._encryption_mode, nonce_counter=self._nonce_counter,
            )
            await self._send_raw(packet)
            self._nonce_counter = (self._nonce_counter + 1) & 0xFFFFFFFF
            self._video_seq = (self._video_seq + 1) & 0xFFFF

        self._video_ts = (self._video_ts + self._video_ts_increment) & 0xFFFFFFFF
