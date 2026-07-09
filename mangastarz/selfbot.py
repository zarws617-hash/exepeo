"""Selfbot manager — spawns selfbot_worker.py as a subprocess and proxies commands.

The worker process uses discord.py-self (isolated in .selfbot_libs/) while this
module (in the main bot process) uses discord-py. They communicate via JSON lines
over stdin/stdout.

Public API (called from bot.py commands):
  is_ready()          → bool
  request_stream(...)  → dict {"ok": bool, ...}
  request_stop(guild_id) → None
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Optional

log = logging.getLogger(__name__)

# ── State ─────────────────────────────────────────────────────────────────────

_proc: Optional[asyncio.subprocess.Process] = None
_ready = False
_pending: dict[str, asyncio.Future] = {}
_cmd_counter = 0
_reader_task: Optional[asyncio.Task] = None


def is_ready() -> bool:
    return _ready and _proc is not None and _proc.returncode is None


# ── Subprocess management ─────────────────────────────────────────────────────

async def _read_stdout(proc: asyncio.subprocess.Process) -> None:
    """Background task: read JSON lines from the worker's stdout."""
    assert proc.stdout is not None
    while True:
        try:
            line = await proc.stdout.readline()
            if not line:
                log.warning("[selfbot] worker stdout closed")
                break
            obj = json.loads(line.decode().strip())
            _id = obj.get("_id")
            if _id and _id in _pending:
                fut = _pending.pop(_id)
                if not fut.done():
                    fut.set_result(obj)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("[selfbot] stdout read error: %s", exc)


async def run(token: str) -> None:
    """Start the selfbot worker subprocess. Called from run_bot.py."""
    global _proc, _ready, _reader_task

    worker = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "selfbot_worker.py")
    env = {**os.environ, "USER_TOKEN": token}

    log.info("[selfbot] launching worker: %s", worker)
    _proc = await asyncio.create_subprocess_exec(
        sys.executable, worker,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,   # worker logs to its own stderr (visible in console)
        env=env,
    )

    _reader_task = asyncio.ensure_future(_read_stdout(_proc))

    # Ping loop — wait until the worker is ready (responds to ping)
    for attempt in range(30):
        await asyncio.sleep(1)
        try:
            result = await _send({"cmd": "ping"}, timeout=3.0)
            if result.get("pong"):
                _ready = True
                log.info("[selfbot] worker is ready (attempt %d)", attempt + 1)
                break
        except Exception:
            pass
    else:
        log.warning("[selfbot] worker did not become ready in time — streaming may fail")

    # Wait for the worker to exit
    await _proc.wait()
    _ready = False
    log.info("[selfbot] worker exited with code %s", _proc.returncode)


# ── IPC helpers ───────────────────────────────────────────────────────────────

def _next_id() -> str:
    global _cmd_counter
    _cmd_counter += 1
    return str(_cmd_counter)


async def _send(cmd: dict, timeout: float = 45.0) -> dict:
    """Write *cmd* to the worker's stdin and await the matching response."""
    if _proc is None or _proc.stdin is None or _proc.returncode is not None:
        return {"ok": False, "error": "worker غير متصل"}

    _id = _next_id()
    cmd["_id"] = _id
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _pending[_id] = fut

    line = (json.dumps(cmd) + "\n").encode()
    try:
        _proc.stdin.write(line)
        await _proc.stdin.drain()
    except Exception as exc:
        _pending.pop(_id, None)
        return {"ok": False, "error": f"write error: {exc}"}

    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _pending.pop(_id, None)
        return {"ok": False, "error": "انتهت مهلة انتظار استجابة الـ worker (timeout)"}


# ── Public API ────────────────────────────────────────────────────────────────

async def request_stream(
    guild_id: int,
    channel_id: int,
    video_url: str,
    referer: str = "",
    timeout: float = 45.0,
) -> dict:
    """Ask the selfbot worker to join a voice channel and stream *video_url*."""
    if not is_ready():
        return {
            "ok": False,
            "error": (
                "حساب البث (selfbot) غير متصل.\n"
                "تأكد أن **USER_TOKEN** مضبوط في Secrets ثم أعد تشغيل البوت."
            ),
        }
    return await _send(
        {
            "cmd": "stream",
            "guild_id": guild_id,
            "channel_id": channel_id,
            "video_url": video_url,
            "referer": referer,
        },
        timeout=timeout,
    )


async def request_stop(guild_id: int) -> None:
    """Stop any active stream in *guild_id*."""
    if is_ready():
        await _send({"cmd": "stop", "guild_id": guild_id}, timeout=10.0)
