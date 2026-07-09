# manga-starz Discord Bot

A Discord notification bot for the Arabic manga website manga-starz.net. It scrapes the site for new chapters and notifies Discord users/channels when updates are available.

## Setup

### Required Secrets
- `DISCORD_BOT_TOKEN` — Your Discord bot token (from Discord Developer Portal)
- `DEVELOPER_IDS` — Comma-separated Discord user IDs that have admin access to bot commands (e.g. `123456789,987654321`)
- `USER_TOKEN` — *(اختياري لكن مطلوب لـ Go Live)* توكن حساب Discord شخصي. يتيح للبوت بث الأنمي بالفيديو (Go Live) داخل الروم الصوتي عبر أمر `/golive`.

### Running
The bot is configured to run via the "Start application" workflow using:
```
python run_bot.py
```

## Architecture
- `run_bot.py` — Entry point
- `mangastarz/bot.py` — Discord client, slash commands, polling loop
- `mangastarz/scraper.py` — Web scraping logic (cloudscraper + BeautifulSoup4)
- `mangastarz/database.py` — Async SQLite storage (subscriptions, seen chapters, cache)

## Slash Commands

### Go Live / بث الأنمي
- `/golive <name> <episode>` — حساب شخصي (USER_TOKEN) يدخل الروم الصوتي ويبث الحلقة كـ Go Live بالفيديو والصوت
- `/leave_stream` — إيقاف بث Go Live ومغادرة الروم الصوتي

**Go Live architecture (current):**

The bot attempts true Discord Go Live (video stream) using a full DAVE (MLS E2EE) implementation:

1. `discord.py-self` VoiceClient joins the voice channel (audio path).
2. `DAVEVoiceGateway` opens a **second** WebSocket to the same voice server and performs the full DAVE MLS handshake (OP 26 key package, OP 28 commit/welcome, OP 22 execute-transition) so Discord doesn't reject with 4017.
3. `DAVEMediaUdp` sends FFmpeg-encoded H.264 frames with DAVE pre-encryption over the stream UDP socket.

**4006 "Session no longer valid" handling (fixed):**

Discord occasionally closes the stream gateway with close code 4006. The previous code called OP 7 RESUME, which Discord rejects with 4006 again, creating an infinite noisy log loop. The fix:

- Close codes are now classified: **fatal** (4004/4011-4014/4016) → session terminated; **non-resumable** (4001-4003/4005/4006/4009/4017) → full IDENTIFY reconnect; **resumable** (4015/1000 etc.) → OP 7 RESUME.
- On non-resumable close, `_reconnect_identify()` opens a new WebSocket and repeats the full IDENTIFY → READY → SELECT\_PROTOCOL → SESSION\_DESCRIPTION handshake, propagating the new `secret_key`, `encryption_mode`, and SSRC to the UDP sender atomically before the next packet is sent.
- A `closed_event` asyncio.Event fires when the session cannot be recovered; `GoLiveVideoSession` watches it via a monitor task and tears down cleanly (play task, UDP, gateway, voice client) through a single idempotent `_cleanup()` path.

If video fails (on initial connect or after all reconnect attempts), the bot falls back to **audio-only** playback automatically and reports `go_live: false` to the user (see `selfbot_worker.py::do_stream`).

### الفصول والمانجا
- `/latest` — Show last 10 chapters from manga-starz.net
- `/search <name>` — Search for a manga/manhwa
- `/watch <name>` — Subscribe to DM notifications for new chapters
- `/unwatch <name>` — Unsubscribe from DM notifications
- `/list` — List your DM subscriptions
- `/status` — Bot status and your subscription count
- `/setchannel` — [Dev] Set notification channel
- `/watchall` — [Dev] Enable notifications for all new titles
- `/check` — [Dev] Manually trigger a chapter check
- `/adddev` / `/removedev` / `/listdevs` — [Dev] Manage developer access

## Environment Setup (Replit)

Dependencies installed via pip:
- `discord.py`, `aiohttp`, `aiosqlite`, `cloudscraper`, `curl-cffi`, `beautifulsoup4`
- `feedparser`, `pynacl`, `requests`, `tweety-ns`, `yt-dlp`, `davey`

System packages installed via Nix: `ffmpeg`, `libopus`

Workflow: **Start application** → `python run_bot.py`

The bot will not start until `DISCORD_BOT_TOKEN` is added to Replit Secrets.
Add secrets in the **Secrets** tab (🔒) in the sidebar.

## User Preferences
