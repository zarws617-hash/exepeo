"""Discord bot for مانجا ستارز (manga-starz.net) chapter notifications."""

from __future__ import annotations

import asyncio
import glob
import io
import logging
import os
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks


def _load_opus_library() -> None:
    """Ensure libopus is loaded for voice (Opus encoding/decoding).

    On NixOS, `libopus` installed via the Nix store isn't found by ctypes'
    default `find_library()` search (it only checks standard system paths),
    so discord.py's automatic opus loading silently fails and voice playback
    raises `discord.opus.OpusNotLoaded` at the first `vc.play(...)` call —
    only surfacing once someone actually tries to use a voice command.

    Nix exposes the resolved library directories via the
    `REPLIT_LD_LIBRARY_PATH` / `LD_LIBRARY_PATH` env vars, so we scan those
    (fast — a handful of directories) instead of globbing the entire
    `/nix/store` (which holds tens of thousands of entries and can take
    30+ seconds to search).
    """
    if discord.opus.is_loaded():
        return

    lib_dirs: list[str] = []
    for env_var in ("REPLIT_LD_LIBRARY_PATH", "LD_LIBRARY_PATH"):
        value = os.environ.get(env_var, "")
        if value:
            lib_dirs.extend(value.split(":"))

    candidates: list[str] = []
    for lib_dir in lib_dirs:
        if "libopus" in lib_dir:
            candidates.extend(sorted(glob.glob(os.path.join(lib_dir, "libopus.so*"))))

    for path in candidates:
        try:
            discord.opus.load_opus(path)
            if discord.opus.is_loaded():
                log.info("[opus] loaded libopus from %s", path)
                return
        except OSError:
            continue

    log.warning(
        "[opus] could not locate/load libopus — voice commands (e.g. /voicewatch, "
        "/watchparty) will fail until the 'libopus' system dependency is installed."
    )

from . import database as db
from .anime import AiringEpisode, fetch_airing_today, fetch_airing_week
from .news import Tweet, fetch_latest_tweets, get_cached_tweets
from .scraper import Chapter, fetch_latest_chapters, fetch_manga_chapters, fetch_series_type, search_manga
from .anidl import AnimeResult, search_anime
from .animeslayer import BASE_URL as SLAYER_BASE_URL, AnimeSlayerEpisode, find_episode_slayer, get_stream_url_slayer, get_episode_meta_slayer

# ffmpeg options for streaming a remote HTTP(S)/HLS URL into a Discord voice
# channel. `-reconnect*` flags let ffmpeg recover from transient network drops
# instead of dying mid-episode. The CDN behind Anime Slayer's video URLs
# requires a Referer header (same as the download-link extraction path in
# animeslayer.py) or it responds with an error instead of the stream.
_FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    f'-headers "Referer: {SLAYER_BASE_URL}/\r\n"'
)
_FFMPEG_OPTIONS = "-vn"

# Preferred quality order when picking a stream URL for voice playback.
_VOICE_QUALITY_ORDER = ["720p", "1080p", "480p", "360p", "default"]

log = logging.getLogger(__name__)

POLL_INTERVAL_MINUTES      = 5
NEWS_POLL_INTERVAL_MINUTES = 5
ANIME_POLL_INTERVAL_MINUTES = 5
ANIME_COLOR = 0xE8410A
SITE_NAME  = "The sky"
SITE_URL   = "https://manga-starz.net"
EMBED_COLOR = 0x1B6CA8

# Guild IDs to sync slash commands to immediately on startup (in addition to
# the global sync, which can take up to an hour to propagate). Comma-separated
# override available via the INSTANT_SYNC_GUILD_IDS env var.
_INSTANT_SYNC_GUILD_IDS = [
    g.strip()
    for g in os.environ.get("INSTANT_SYNC_GUILD_IDS", "1481718193608462497").split(",")
    if g.strip()
]

COLOR_SUCCESS = 0x57F287
COLOR_ERROR   = 0xED4245
COLOR_WARN    = 0xFEE75C
COLOR_INFO    = 0x3B82F6


def _fmt_ch_num(raw: str) -> str:
    """Return a clean label like 'الفصل 42' or 'الفصل 42.5' from any raw text."""
    import re
    m = re.search(r'(\d+(?:[.,]\d+)?)', raw.strip())
    if m:
        num = m.group(1).replace(",", ".")
        return f"الفصل {num}"
    return raw.strip()


def _embed_success(msg: str) -> discord.Embed:
    embed = discord.Embed(description=f"✅ {msg}" if not msg.startswith("✅") else msg, color=COLOR_SUCCESS)
    embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
    return embed


def _embed_error(msg: str) -> discord.Embed:
    embed = discord.Embed(description=f"❌ {msg}" if not msg.startswith("❌") else msg, color=COLOR_ERROR)
    embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
    return embed


def _embed_warn(msg: str) -> discord.Embed:
    embed = discord.Embed(description=f"⚠️ {msg}" if not msg.startswith("⚠️") else msg, color=COLOR_WARN)
    embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
    return embed


def _embed_info(msg: str) -> discord.Embed:
    embed = discord.Embed(description=msg, color=COLOR_INFO)
    embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
    return embed


def _download_cover_bytes(url: str) -> Optional[tuple[bytes, str]]:
    """Download cover bytes using cloudscraper. Returns (content, ext) or None."""
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(url, timeout=12, headers={"Referer": "https://manga-starz.net/"})
        if resp.status_code == 200 and resp.content:
            ct = resp.headers.get("Content-Type", "").lower()
            ext = "jpg" if ("jpeg" in ct or "jpg" in ct or url.lower().endswith(".jpg") or url.lower().endswith(".jpeg")) else "png"
            log.info("Cover downloaded OK: %d bytes (%s)", len(resp.content), url)
            return resp.content, ext
        else:
            log.warning("Cover HTTP %s for %s", resp.status_code, url)
    except Exception as exc:
        log.warning("Cover download failed for %s: %s", url, exc)
    return None


async def _get_cover_bytes(cover_url: str) -> Optional[tuple[bytes, str]]:
    """Download cover bytes asynchronously. Returns (bytes, ext) or None."""
    if not cover_url:
        return None
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_cover_bytes, cover_url)


def _make_cover_file(data: tuple[bytes, str]) -> discord.File:
    """Create a fresh discord.File from (bytes, ext). Must be called fresh per send."""
    content, ext = data
    return discord.File(io.BytesIO(content), filename=f"cover.{ext}")


async def _get_cover_file(cover_url: str) -> Optional[discord.File]:
    """Async wrapper — downloads cover bytes in thread, creates File in async context."""
    result = await _get_cover_bytes(cover_url)
    if result is None:
        return None
    return _make_cover_file(result)


# ── Developer access ──────────────────────────────────────────────────────────

def _load_env_dev_ids() -> set[int]:
    """Read DEVELOPER_IDS env var (comma-separated Discord user IDs)."""
    raw = os.environ.get("DEVELOPER_IDS", "").strip()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


async def _is_developer(user_id: int) -> bool:
    """Check env var AND persistent DB developer list."""
    if user_id in _load_env_dev_ids():
        return True
    db_ids = await db.get_db_developer_ids()
    return user_id in db_ids


def dev_only():
    """Interaction check: only developer IDs may use this command."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if await _is_developer(interaction.user.id):
            return True
        await interaction.response.send_message(
            embed=_embed_error("❌ هذا الأمر متاح للمطورين فقط."), ephemeral=True
        )
        return False
    return app_commands.check(predicate)


# ── Bot ───────────────────────────────────────────────────────────────────────

_DEFAULT_ACTIVITY = discord.Activity(
    type=discord.ActivityType.watching,
    name="manga-starz.net",
)


class MangaBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(
            intents=discord.Intents.default(),
            activity=_DEFAULT_ACTIVITY,
            status=discord.Status.idle,
        )
        self.tree = app_commands.CommandTree(self)
        # guild_id -> asyncio.TimerHandle scheduled to auto-disconnect an idle
        # voice session (e.g. episode finished playing, nobody restarted it).
        self._voice_idle_handles: dict[int, asyncio.TimerHandle] = {}

    def _cancel_voice_idle_timer(self, guild_id: int) -> None:
        handle = self._voice_idle_handles.pop(guild_id, None)
        if handle is not None:
            handle.cancel()

    def _schedule_voice_idle_disconnect(self, guild_id: int, delay: float = 120) -> None:
        """Auto-leave the voice channel if nothing is played within *delay* seconds."""
        self._cancel_voice_idle_timer(guild_id)

        async def _disconnect() -> None:
            self._voice_idle_handles.pop(guild_id, None)
            guild = self.get_guild(guild_id)
            vc = guild.voice_client if guild else None
            if vc and not vc.is_playing():
                await vc.disconnect(force=True)
                log.info("[voice] auto-disconnected from guild %d (idle)", guild_id)

        loop = asyncio.get_running_loop()
        handle = loop.call_later(delay, lambda: asyncio.ensure_future(_disconnect()))
        self._voice_idle_handles[guild_id] = handle

    async def setup_hook(self) -> None:
        await db.init_db()
        await db.export_to_json()
        _register_commands(self.tree, self)
        await self.tree.sync()

        # Also copy + sync commands to specific guild(s) instantly — global
        # sync can take up to an hour to propagate to Discord clients, which
        # made newly-added commands (e.g. /voicewatch) appear "broken" right
        # after deploy. A guild-scoped sync shows up immediately.
        for guild_id_str in _INSTANT_SYNC_GUILD_IDS:
            guild_id_str = guild_id_str.strip()
            if not guild_id_str:
                continue
            try:
                guild_obj = discord.Object(id=int(guild_id_str))
            except ValueError:
                log.warning("[sync] invalid guild id in INSTANT_SYNC_GUILD_IDS: %r", guild_id_str)
                continue
            self.tree.copy_global_to(guild=guild_obj)
            synced = await self.tree.sync(guild=guild_obj)
            log.info("[sync] instantly synced %d command(s) to guild %s", len(synced), guild_id_str)
        self.poll_loop.start()
        self.news_loop.start()
        self.anime_loop.start()
        self.maintenance_loop.start()
        log.info("Bot ready. Polling every %d minutes.", POLL_INTERVAL_MINUTES)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        log.error("App command error in '%s': %s", interaction.command, error, exc_info=error)
        is_dev = await _is_developer(interaction.user.id)
        if is_dev:
            cmd = interaction.command.name if interaction.command else "unknown"
            _err = _embed_error(
                f"❌ خطأ في الأمر `/{cmd}`\n```\n{type(error).__name__}: {error}\n```"
            )
        else:
            _err = _embed_warn("⚠️ صار شي ما، حاول مجدداً بعد لحظات.")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=_err, ephemeral=True)
            else:
                await interaction.response.send_message(embed=_err, ephemeral=True)
        except Exception:
            pass

    async def _get_text_channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        """Resolve a text channel, falling back to an API fetch if not cached.

        Closes a reliability gap where notifications were silently dropped
        whenever a channel wasn't in the internal cache (e.g. right after a
        restart or reconnect).
        """
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Could not fetch channel %d: %s", channel_id, exc)
                return None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _update_presence(self) -> None:
        sub_count = await db.get_dm_user_count()
        try:
            await self.change_presence(
                status=discord.Status.idle,
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"manga-starz.net • {sub_count} subscriber{'s' if sub_count != 1 else ''}",
                ),
            )
            log.info("Presence updated — idle | %d مشترك", sub_count)
        except Exception as exc:
            log.error("Presence update failed: %s", exc)

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_loop(self) -> None:
        await self._check_for_new_chapters()

    @poll_loop.before_loop
    async def before_poll(self) -> None:
        await self.wait_until_ready()
        await asyncio.sleep(5)
        await self._update_presence()

    @tasks.loop(minutes=NEWS_POLL_INTERVAL_MINUTES)
    async def news_loop(self) -> None:
        await self._check_for_new_tweets()

    @news_loop.before_loop
    async def before_news_loop(self) -> None:
        await self.wait_until_ready()
        await asyncio.sleep(15)

    @tasks.loop(minutes=ANIME_POLL_INTERVAL_MINUTES)
    async def anime_loop(self) -> None:
        await self._check_for_new_episodes()

    @anime_loop.before_loop
    async def before_anime_loop(self) -> None:
        await self.wait_until_ready()
        await asyncio.sleep(20)

    @tasks.loop(hours=24)
    async def maintenance_loop(self) -> None:
        """Daily cleanup: purge old seen records and export backup."""
        try:
            await db.purge_old_seen_chapters(keep_days=90)
            await db.purge_old_seen_tweets(keep_days=30)
            await db.purge_old_seen_episodes(keep_days=30)
            await db.export_to_json()
            log.info("[maintenance] Daily cleanup done.")
        except Exception as exc:
            log.error("[maintenance] Error during daily cleanup: %s", exc)

    @maintenance_loop.before_loop
    async def before_maintenance_loop(self) -> None:
        await self.wait_until_ready()

    async def _check_for_new_episodes(self) -> None:
        channels = await db.get_all_anime_notify_channels()
        if not channels:
            return
        log.info("Checking for new anime episodes…")
        loop = asyncio.get_running_loop()
        try:
            episodes: list[AiringEpisode] = await loop.run_in_executor(None, fetch_airing_today)
        except Exception as exc:
            log.error("Anime fetch error: %s", exc)
            return

        new_count = 0
        for ep in episodes:
            if await db.is_episode_seen(ep.media_id, ep.episode):
                continue
            await db.mark_episode_seen(ep.media_id, ep.episode)
            new_count += 1
            embed = _build_episode_embed(ep)
            for guild_id, channel_id in channels:
                channel = await self._get_text_channel(channel_id)
                if channel:
                    try:
                        await channel.send(embed=embed)
                    except discord.Forbidden:
                        log.warning("No permission in anime channel %d", channel_id)
                    except Exception as exc:
                        log.error("Anime send failed: %s", exc)
        log.info("Anime check done. %d new episode(s).", new_count)

    async def _check_for_new_tweets(self) -> None:
        log.info("Checking for new tweets from @CrunchyrollMENA…")
        news_channels = await db.get_all_news_channels()
        if not news_channels:
            return
        loop = asyncio.get_running_loop()
        try:
            tweets: list[Tweet] = await loop.run_in_executor(None, fetch_latest_tweets, 20)
        except Exception as e:
            log.error("News fetch error: %s", e)
            return

        new_count = 0
        for tweet in reversed(tweets):
            if await db.is_tweet_seen(tweet.tweet_id):
                continue
            await db.mark_tweet_seen(tweet.tweet_id)
            new_count += 1
            embeds = _build_tweet_embeds(tweet)
            for guild_id, channel_id in news_channels:
                channel = await self._get_text_channel(channel_id)
                if channel:
                    try:
                        await channel.send(embeds=embeds)
                    except discord.Forbidden:
                        log.warning("No permission in news channel %d", channel_id)
                    except Exception as e:
                        log.error("News send failed: %s", e)
        log.info("News check done. %d new tweet(s).", new_count)

    async def _get_series_type(self, manga_title: str, manga_url: str) -> str:
        cache_key = manga_url or manga_title
        cached = await db.get_cached_series_type(cache_key)
        if cached is not None:
            return cached
        loop = asyncio.get_running_loop()
        series_type = await loop.run_in_executor(None, fetch_series_type, manga_title)
        await db.cache_series_type(cache_key, series_type)
        return series_type

    async def _check_for_new_chapters(self) -> None:
        log.info("Checking for new chapters…")
        loop = asyncio.get_running_loop()
        try:
            chapters: list[Chapter] = await loop.run_in_executor(None, fetch_latest_chapters)
        except Exception as e:
            log.error("Scraper error: %s", e)
            return

        guild_channels = await db.get_all_guild_channels()

        # Preload all guild subscriptions once — avoids N×M DB hits inside the loop
        guild_subs: dict[int, set[str]] = {}
        for guild_id, _ in guild_channels:
            subs = await db.get_subscriptions(guild_id)
            guild_subs[guild_id] = {s["url"] for s in subs}

        new_count = 0
        for ch in chapters:
            if not ch.chapter_url:
                continue
            if await db.is_chapter_seen(ch.chapter_url):
                continue

            await db.mark_chapter_seen(ch.chapter_url, ch.manga_title, ch.chapter_num)
            new_count += 1

            series_type = await self._get_series_type(ch.manga_title, ch.manga_url)
            embed = _build_chapter_embed(ch, series_type)

            for guild_id, channel_id in guild_channels:
                subscribed_urls = guild_subs.get(guild_id, set())
                # If the guild has specific subscriptions, filter by them
                if subscribed_urls:
                    if not ch.manga_url or ch.manga_url not in subscribed_urls:
                        continue

                channel = await self._get_text_channel(channel_id)
                if channel:
                    try:
                        # Fresh view instance per message — each message tracks
                        # its own timeout independently and avoids shared state.
                        notif_view = (
                            ChapterNotificationView(ch.manga_title, ch.manga_url, self)
                            if ch.manga_url else None
                        )
                        await channel.send(embed=embed, view=notif_view)
                    except discord.Forbidden:
                        log.warning("No permission in channel %d", channel_id)
                    except Exception as e:
                        log.error("Send failed: %s", e)

            # Only send DMs if the manga has a valid URL
            if ch.manga_url:
                dm_user_ids = await db.get_users_subscribed_to_dm(ch.manga_url)
                for user_id in dm_user_ids:
                    try:
                        user = await self.fetch_user(user_id)
                        await user.send(embed=embed)
                        log.info("DM sent to user %d for '%s'", user_id, ch.manga_title)
                    except discord.Forbidden:
                        log.warning("Cannot DM user %d (DMs closed)", user_id)
                    except Exception as e:
                        log.error("DM failed for user %d: %s", user_id, e)

        log.info("Done. %d new chapter(s) found.", new_count)


NEWS_COLOR = 0x1DA1F2  # Twitter/X blue


SITE_ICON = "https://manga-starz.net/favicon.ico"
_FOOTER_ICON = SITE_ICON


def _build_episode_embed(ep: AiringEpisode) -> discord.Embed:
    embed = discord.Embed(
        title=ep.title,
        url=ep.site_url,
        color=ANIME_COLOR,
        timestamp=ep.airing_dt,
    )
    embed.set_author(name="🎌 حلقة جديدة!", icon_url=SITE_ICON)
    embed.add_field(name="الحلقة", value=f"**{ep.episode}**", inline=True)
    embed.add_field(name="شاهد على", value=f"[AniList]({ep.site_url})", inline=True)
    if ep.cover_url:
        embed.set_thumbnail(url=ep.cover_url)
    embed.set_footer(text="AniList • جدول الأنمي", icon_url=_FOOTER_ICON)
    return embed


def _build_schedule_embed(episodes: list[AiringEpisode], title: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        color=ANIME_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name="📅 جدول الأنمي", icon_url=SITE_ICON)
    for ep in episodes[:20]:
        ts = f"<t:{ep.airing_at}:R>"
        embed.add_field(
            name=f"🎬 {ep.title[:50]}",
            value=f"ح. **{ep.episode}** — {ts}",
            inline=True,
        )
    total = len(episodes)
    shown = min(total, 20)
    embed.set_footer(text=f"AniList • {shown} من {total} حلقة", icon_url=_FOOTER_ICON)
    return embed


def _build_tweet_embeds(tweet: Tweet) -> list[discord.Embed]:
    """Build up to 4 embeds for a tweet (text + images). Discord allows max 10 embeds per message."""
    main = discord.Embed(
        description=tweet.text or "",
        url=tweet.url,
        color=NEWS_COLOR,
    )
    main.set_author(
        name="Crunchyroll MENA — أخبار الأنمي",
        url="https://x.com/CrunchyrollMENA",
        icon_url="https://pbs.twimg.com/profile_images/1589121816956817408/iMkllRLJ_400x400.jpg",
    )
    main.set_footer(
        text="X (Twitter) • @CrunchyrollMENA",
        icon_url="https://abs.twimg.com/favicons/twitter.3.ico",
    )
    if tweet.timestamp:
        try:
            from email.utils import parsedate_to_datetime
            main.timestamp = parsedate_to_datetime(tweet.timestamp)
        except Exception:
            pass

    embeds: list[discord.Embed] = [main]

    if tweet.images:
        main.set_image(url=tweet.images[0])
        for img_url in tweet.images[1:4]:
            extra = discord.Embed(url=tweet.url, color=NEWS_COLOR)
            extra.set_image(url=img_url)
            embeds.append(extra)

    return embeds


def _build_chapter_embed(ch: Chapter, series_type: str = "") -> discord.Embed:
    embed = discord.Embed(
        title=ch.manga_title or "The sky",
        url=ch.manga_url or ch.chapter_url or SITE_URL,
        color=EMBED_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    if series_type:
        embed.set_author(name=f"📖 فصل جديد  •  {series_type}", icon_url=SITE_ICON)
    else:
        embed.set_author(name="📖 فصل جديد", icon_url=SITE_ICON)
    embed.add_field(name="📌 الفصل", value=f"**{_fmt_ch_num(ch.chapter_num)}**", inline=True)
    embed.add_field(name="🔗 اقرأ الآن", value=f"[اضغط هنا ◀]({ch.chapter_url})", inline=True)
    if ch.cover_url:
        embed.set_thumbnail(url=ch.cover_url)
    embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
    return embed


# ── Notification interactive views ────────────────────────────────────────────

class ChapterPickerModal(discord.ui.Modal, title="اختر فصلاً"):
    """Modal: user types a chapter number → bot DMs that chapter link."""

    chapter_input = discord.ui.TextInput(
        label="رقم الفصل",
        placeholder="مثال: 8",
        required=True,
        max_length=10,
    )

    def __init__(self, manga_title: str, manga_url: str) -> None:
        super().__init__()
        self.manga_title = manga_title
        self.manga_url   = manga_url

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        chapters: list[dict] = await loop.run_in_executor(
            None, fetch_manga_chapters, self.manga_title, self.manga_url
        )
        if not chapters:
            await interaction.followup.send(
                embed=_embed_error("❌ تعذّر تحميل الفصول — جرّب مجدداً."),
                ephemeral=True,
            )
            return

        query = self.chapter_input.value.strip()
        query_nums = re.findall(r"\d+(?:\.\d+)?", query)
        query_num  = query_nums[0] if query_nums else None

        def _matches(ch_label: str) -> bool:
            if not query_num:
                return ch_label.strip() == query
            nums = re.findall(r"\d+(?:\.\d+)?", ch_label)
            return any(n == query_num for n in nums)

        match = next((ch for ch in chapters if _matches(ch["num"])), None)
        if match is None:
            await interaction.followup.send(
                embed=_embed_error(
                    f"❌ الفصل **{query}** غير موجود في **{self.manga_title}**.\n"
                    f"الفصول المتاحة: {len(chapters)} فصل."
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=self.manga_title,
            url=self.manga_url,
            color=EMBED_COLOR,
        )
        embed.set_author(name="📖 الفصل المطلوب", icon_url=SITE_ICON)
        embed.add_field(name="📌 الفصل",    value=f"**{_fmt_ch_num(match['num'])}**",      inline=True)
        embed.add_field(name="🔗 اقرأ الآن", value=f"[اضغط هنا ◀]({match['url']})", inline=True)
        embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)

        try:
            await interaction.user.send(embed=embed)
            await interaction.followup.send(
                embed=_embed_success("✅ تم إرسال الفصل إلى خاصك!"),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=_embed_error("❌ لا يمكن الإرسال — تأكد أن رسائلك الخاصة مفتوحة."),
                ephemeral=True,
            )


class ChapterPickerView(discord.ui.View):
    """
    Ephemeral view shown after clicking 'الفصول' on a notification.
    Two buttons: pick a specific chapter (modal) or receive all chapters by DM.
    """

    _DM_CHUNK = 30  # chapters per DM embed

    def __init__(self, manga_title: str, manga_url: str) -> None:
        super().__init__(timeout=120)
        self.manga_title = manga_title
        self.manga_url   = manga_url

    @discord.ui.button(label="فصل محدد", style=discord.ButtonStyle.primary, emoji="🔢")
    async def specific_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            ChapterPickerModal(self.manga_title, self.manga_url)
        )

    @discord.ui.button(label="جميع الفصول", style=discord.ButtonStyle.secondary, emoji="📤")
    async def all_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        chapters: list[dict] = await loop.run_in_executor(
            None, fetch_manga_chapters, self.manga_title, self.manga_url
        )
        if not chapters:
            await interaction.followup.send(
                embed=_embed_error("❌ تعذّر تحميل الفصول — جرّب مجدداً."),
                ephemeral=True,
            )
            return

        # Send chapters in DM embeds, CHUNK per embed (oldest → newest per embed)
        ordered = list(reversed(chapters))   # oldest first
        total_pages = max(1, (len(ordered) + self._DM_CHUNK - 1) // self._DM_CHUNK)
        try:
            for page_idx in range(total_pages):
                start  = page_idx * self._DM_CHUNK
                chunk  = ordered[start : start + self._DM_CHUNK]
                desc   = "\n".join(
                    f"[{_fmt_ch_num(ch['num'])}]({ch['url']})" for ch in chunk
                )
                embed = discord.Embed(
                    title=self.manga_title,
                    url=self.manga_url,
                    description=desc,
                    color=EMBED_COLOR,
                )
                embed.set_author(
                    name=f"📚 قائمة الفصول  •  {page_idx + 1}/{total_pages}",
                    icon_url=SITE_ICON,
                )
                embed.set_footer(
                    text=f"{len(chapters)} فصل  •  {SITE_NAME}", icon_url=_FOOTER_ICON
                )
                await interaction.user.send(embed=embed)

            await interaction.followup.send(
                embed=_embed_success(
                    f"✅ تم إرسال **{len(chapters)} فصل** إلى خاصك!"
                ),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=_embed_error("❌ لا يمكن الإرسال — تأكد أن رسائلك الخاصة مفتوحة."),
                ephemeral=True,
            )


class ChapterNotificationView(discord.ui.View):
    """
    Buttons attached to chapter notification embeds posted in channels.
    • 🔔 متابعة  — subscribe user to DM notifications for this manga
    • 📚 الفصول — open chapter picker (specific chapter or all chapters via DM)

    Uses a 24-hour timeout (not persistent custom_ids) — buttons remain active
    for 24 h which covers normal notification interaction windows.
    """

    def __init__(self, manga_title: str, manga_url: str, bot: "MangaBot") -> None:
        super().__init__(timeout=86_400)   # 24 hours
        self.manga_title = manga_title
        self.manga_url   = manga_url
        self.bot         = bot

    @discord.ui.button(label="متابعة", style=discord.ButtonStyle.success, emoji="🔔")
    async def follow_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        added = await db.add_dm_subscription(
            interaction.user.id, self.manga_url, self.manga_title
        )
        if added:
            await interaction.followup.send(
                embed=_embed_success(
                    f"✅ سيصلك إشعار بالخاص عند صدور فصل جديد من **{self.manga_title}**."
                ),
                ephemeral=True,
            )
            await db.export_to_json()
            await self.bot._update_presence()
        else:
            await interaction.followup.send(
                embed=_embed_warn(
                    f"⚠️ أنت مشترك بالفعل في **{self.manga_title}**."
                ),
                ephemeral=True,
            )

    @discord.ui.button(label="الفصول", style=discord.ButtonStyle.secondary, emoji="📚")
    async def chapters_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        embed = discord.Embed(
            title=self.manga_title,
            url=self.manga_url,
            description=(
                "اختر ما تريد:\n"
                "• **فصل محدد** — أدخل رقم الفصل وسيُرسل إلى خاصك\n"
                "• **جميع الفصول** — سيُرسل إليك كامل قائمة الفصول بالخاص"
            ),
            color=EMBED_COLOR,
        )
        embed.set_author(name="📚 الفصول", icon_url=SITE_ICON)
        embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
        await interaction.response.send_message(
            embed=embed,
            view=ChapterPickerView(self.manga_title, self.manga_url),
            ephemeral=True,
        )


# ── UI Views ──────────────────────────────────────────────────────────────────

CHAPTERS_PER_PAGE = 60


class GoToChapterModal(discord.ui.Modal, title="اذهب لفصل محدد"):
    chapter_input = discord.ui.TextInput(
        label="رقم الفصل",
        placeholder="مثال: 50",
        required=True,
        max_length=10,
    )

    def __init__(self, paginator: "ChapterPaginatorView") -> None:
        super().__init__()
        self.paginator = paginator

    async def on_submit(self, interaction: discord.Interaction) -> None:
        import re
        query = self.chapter_input.value.strip()
        chapters = self.paginator.chapters

        # Reject pure-text input — must contain at least one digit
        if not re.search(r'\d', query):
            await interaction.response.send_message(
                embed=_embed_error("❌ أدخل رقم الفصل فقط.\nمثال: **50** أو **10.5**"),
                ephemeral=True,
            )
            return

        # Extract the numeric part from the query for exact comparison
        query_nums = re.findall(r'\d+(?:\.\d+)?', query)
        query_num = query_nums[0] if query_nums else None

        def _chapter_num_matches(ch_label: str) -> bool:
            if not query_num:
                return ch_label.strip() == query
            nums = re.findall(r'\d+(?:\.\d+)?', ch_label)
            return any(n == query_num for n in nums)

        match = next((ch for ch in chapters if _chapter_num_matches(ch["num"])), None)

        if match is None:
            await interaction.response.send_message(
                embed=_embed_error(
                    f"❌ الفصل **{query}** غير موجود في **{self.paginator.title}**.\n"
                    f"الفصول المتاحة: {len(chapters)} فصل."
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=self.paginator.title,
            url=self.paginator.manga_url,
            color=EMBED_COLOR,
        )
        embed.set_author(name="📖 الفصل المطلوب", icon_url=SITE_ICON)
        embed.add_field(name="📌 الفصل", value=f"**{_fmt_ch_num(match['num'])}**", inline=True)
        embed.add_field(name="🔗 اقرأ الآن", value=f"[اضغط هنا ◀]({match['url']})", inline=True)
        embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ChapterPaginatorView(discord.ui.View):
    def __init__(
        self,
        title: str,
        manga_url: str,
        chapters: list[dict],
        bot: Optional["MangaBot"] = None,
        result: Optional[dict] = None,
    ) -> None:
        super().__init__(timeout=180)
        self.title = title
        self.manga_url = manga_url
        self.chapters = list(reversed(chapters))
        self.bot = bot
        self.result = result
        self.cover_url: str = (result or {}).get("cover", "")
        self.page = 0
        self.total_pages = max(1, (len(chapters) + CHAPTERS_PER_PAGE - 1) // CHAPTERS_PER_PAGE)
        self._refresh_buttons()
        if bot is None:
            self.watch_btn.disabled = True

    def _refresh_buttons(self) -> None:
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        start = self.page * CHAPTERS_PER_PAGE
        end = start + CHAPTERS_PER_PAGE
        slice_ = list(reversed(self.chapters[start:end]))

        first_ch = self.chapters[0]
        latest_ch = self.chapters[-1]

        # Build description line-by-line, never exceeding Discord's 4096-char limit
        LIMIT = 4096
        built: list[str] = []
        total_len = 0
        hidden = 0
        for ch in slice_:
            line = f"[{_fmt_ch_num(ch['num'])}]({ch['url']})"
            # +1 for the newline separator
            needed = len(line) + (1 if built else 0)
            if total_len + needed > LIMIT - 30:  # keep 30 chars for overflow note
                hidden = len(slice_) - len(built)
                break
            built.append(line)
            total_len += needed

        if not built:
            description = "لا توجد فصول."
        elif hidden:
            description = "\n".join(built) + f"\n… و{hidden} فصل آخر"
        else:
            description = "\n".join(built)

        embed = discord.Embed(
            title=self.title,
            url=self.manga_url or SITE_URL,
            description=description,
            color=EMBED_COLOR,
        )
        embed.set_author(name="📚 قائمة الفصول", icon_url=SITE_ICON)
        if self.cover_url:
            embed.set_thumbnail(url=self.cover_url)
        embed.add_field(
            name="🔰 أول فصل",
            value=f"[{_fmt_ch_num(first_ch['num'])}]({first_ch['url']})",
            inline=True,
        )
        embed.add_field(
            name="🆕 آخر فصل",
            value=f"[{_fmt_ch_num(latest_ch['num'])}]({latest_ch['url']})",
            inline=True,
        )
        embed.set_footer(
            text=f"صفحة {self.page + 1} / {self.total_pages}  •  {len(self.chapters)} فصل  •  {SITE_NAME}",
            icon_url=_FOOTER_ICON,
        )
        return embed

    @discord.ui.button(label="◀ السابق", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page -= 1
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self, attachments=[])

    @discord.ui.button(label="التالي ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page += 1
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self, attachments=[])

    @discord.ui.button(label="🔢 اذهب لفصل", style=discord.ButtonStyle.primary, row=0)
    async def goto_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(GoToChapterModal(self))

    @discord.ui.button(label="🔔 متابعة", style=discord.ButtonStyle.success, row=1)
    async def watch_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.bot or not self.result:
            await interaction.response.send_message(embed=_embed_error("❌ غير متاح."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        added = await db.add_dm_subscription(
            interaction.user.id, self.result["url"], self.result["title"]
        )
        if added:
            await interaction.followup.send(
                embed=_embed_success(f"✅ سيصلك إشعار بالخاص عند صدور فصل جديد من **{self.result['title']}**."),
                ephemeral=True,
            )
            await db.export_to_json()
            await self.bot._update_presence()
        else:
            await interaction.followup.send(
                embed=_embed_warn(f"⚠️ أنت مشترك بالفعل في **{self.result['title']}**."),
                ephemeral=True,
            )


async def _load_and_show_chapters(
    interaction: discord.Interaction,
    result: dict,
    bot: Optional["MangaBot"] = None,
) -> None:
    """Fetch chapters and display paginator. Edits the original response."""
    loop = asyncio.get_running_loop()
    chapters = await loop.run_in_executor(
        None, fetch_manga_chapters, result["title"], result.get("url", "")
    )
    if not chapters:
        await interaction.edit_original_response(
            embed=_embed_error(
                f"❌ لم يتم العثور على فصول لـ **{result['title']}**.\n"
                f"جرب مجدداً بعد لحظات أو استخدم `/watch` لمتابعة الفصول الجديدة."
            ),
            content=None,
        )
        return
    view = ChapterPaginatorView(result["title"], result.get("url", ""), chapters, bot=bot, result=result)
    await interaction.edit_original_response(content=None, embed=view.build_embed(), view=view, attachments=[])


async def _do_watch_subscription(
    interaction: discord.Interaction,
    result: dict,
    bot: "MangaBot",
) -> None:
    """Subscribe the user to DM notifications for a manga."""
    added = await db.add_dm_subscription(interaction.user.id, result["url"], result["title"])
    if added:
        await interaction.edit_original_response(
            embed=_embed_success(f"✅ سيصلك إشعار بالخاص عند صدور فصل جديد من **{result['title']}**."),
            content=None,
            view=None,
        )
        await db.export_to_json()
        await bot._update_presence()
    else:
        await interaction.edit_original_response(
            embed=_embed_warn(f"⚠️ أنت مشترك بالفعل في **{result['title']}**."),
            content=None,
            view=None,
        )


class WatchConfirmView(discord.ui.View):
    def __init__(self, result: dict, bot: "MangaBot", user_id: Optional[int] = None) -> None:
        super().__init__(timeout=60)
        self.result = result
        self.bot = bot
        self.user_id = user_id

    async def _check(self, interaction: discord.Interaction) -> bool:
        if self.user_id and interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=_embed_error("❌ هذه الرسالة ليست لك."), ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🔔 اشتراك", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check(interaction): return
        await interaction.response.defer()
        await _do_watch_subscription(interaction, self.result, self.bot)

    @discord.ui.button(label="❌ إلغاء", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check(interaction): return
        try:
            await interaction.message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            await interaction.response.edit_message(content=None, embed=None, view=None, attachments=[])


class WatchResultSelect(discord.ui.Select):
    def __init__(self, results: list[dict], bot: "MangaBot") -> None:
        options = [
            discord.SelectOption(
                label=r["title"][:100],
                value=str(i),
                description=r["url"][:100] if r.get("url") else None,
            )
            for i, r in enumerate(results[:25])
        ]
        super().__init__(
            placeholder="اختر العنوان الصحيح للاشتراك…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.results = results
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.view._check(interaction):
            return
        idx = int(self.values[0])
        result = self.results[idx]
        await interaction.response.defer()
        await _do_watch_subscription(interaction, result, self.bot)


class WatchResultsView(discord.ui.View):
    def __init__(self, results: list[dict], bot: "MangaBot", user_id: Optional[int] = None) -> None:
        super().__init__(timeout=60)
        self.user_id = user_id
        self.add_item(WatchResultSelect(results, bot))

    async def _check(self, interaction: discord.Interaction) -> bool:
        if self.user_id and interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=_embed_error("❌ هذه الرسالة ليست لك."), ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="❌ إلغاء", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check(interaction): return
        try:
            await interaction.message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            await interaction.response.edit_message(content=None, embed=None, view=None, attachments=[])


class SearchConfirmView(discord.ui.View):
    def __init__(self, result: dict, bot: "MangaBot", user_id: Optional[int] = None) -> None:
        super().__init__(timeout=60)
        self.result = result
        self.bot = bot
        self.user_id = user_id

    async def _check(self, interaction: discord.Interaction) -> bool:
        if self.user_id and interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=_embed_error("❌ هذه الرسالة ليست لك."), ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="📋 كل الفصول", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check(interaction): return
        await interaction.response.edit_message(
            embed=_embed_info("⏳ جاري جلب الفصول…"), content=None, view=None, attachments=[]
        )
        await _load_and_show_chapters(interaction, self.result, bot=self.bot)

    @discord.ui.button(label="🔔 متابعة", style=discord.ButtonStyle.primary)
    async def watch_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check(interaction): return
        await interaction.response.defer()
        await _do_watch_subscription(interaction, self.result, self.bot)

    @discord.ui.button(label="❌ إلغاء", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check(interaction): return
        try:
            await interaction.message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            await interaction.response.edit_message(content=None, embed=None, view=None, attachments=[])


def _slug_label(url: str) -> str:
    """Turn a manga URL into a short readable slug for dropdown descriptions."""
    slug = url.rstrip("/").split("/")[-1]
    return slug[:50] if slug else ""


class SearchResultSelect(discord.ui.Select):
    def __init__(self, results: list[dict], bot: Optional["MangaBot"] = None) -> None:
        options = [
            discord.SelectOption(
                label=r["title"][:100],
                value=str(i),
                description=_slug_label(r.get("url", "")) or None,
            )
            for i, r in enumerate(results[:25])
        ]
        super().__init__(
            placeholder="اختر العنوان الصحيح…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.results = results
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.view._check(interaction):
            return
        idx = int(self.values[0])
        result = self.results[idx]
        await interaction.response.edit_message(
            embed=_embed_info("⏳ جاري جلب الفصول…"), content=None, view=None, attachments=[]
        )
        await _load_and_show_chapters(interaction, result, bot=self.bot)


class SearchResultsView(discord.ui.View):
    def __init__(self, results: list[dict], bot: Optional["MangaBot"] = None, user_id: Optional[int] = None) -> None:
        super().__init__(timeout=60)
        self.user_id = user_id
        self.add_item(SearchResultSelect(results, bot=bot))

    async def _check(self, interaction: discord.Interaction) -> bool:
        if self.user_id and interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=_embed_error("❌ هذه الرسالة ليست لك."), ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="❌ إلغاء", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check(interaction): return
        try:
            await interaction.message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            await interaction.response.edit_message(content=None, embed=None, view=None, attachments=[])


# ── Commands ──────────────────────────────────────────────────────────────────

def _register_commands(tree: app_commands.CommandTree, bot: MangaBot) -> None:

    # ── Developer-only ────────────────────────────────────────────────────────

    @tree.command(name="setchannel", description="[مطور] تعيين قناة الإشعارات")
    @dev_only()
    async def setchannel(
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message(embed=_embed_error("❌ يعمل في السيرفر فقط."), ephemeral=True)
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(embed=_embed_error("❌ الرجاء تحديد قناة نصية."), ephemeral=True)
            return
        await db.set_guild_channel(interaction.guild_id, target.id)
        await interaction.response.send_message(
            embed=_embed_success(f"✅ الإشعارات ستُرسل إلى {target.mention}"), ephemeral=True
        )

    @tree.command(name="watchall", description="[مطور] إشعارات لجميع العناوين الجديدة")
    @dev_only()
    async def watchall(interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message(embed=_embed_error("❌ يعمل في السيرفر فقط."), ephemeral=True)
            return
        subs = await db.get_subscriptions(interaction.guild_id)
        for s in subs:
            await db.remove_subscription(interaction.guild_id, s["url"])
        await interaction.response.send_message(
            embed=_embed_success("✅ سيتم إشعارك بجميع الفصول الجديدة من **مانجا ستارز**."), ephemeral=True
        )

    @tree.command(name="check", description="[مطور] فحص الفصول الجديدة فوراً")
    @dev_only()
    async def check_now(interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message(embed=_embed_error("❌ يعمل في السيرفر فقط."), ephemeral=True)
            return
        await interaction.response.send_message(embed=_embed_info("🔍 جاري الفحص…"), ephemeral=True)
        await bot._check_for_new_chapters()
        await interaction.edit_original_response(
            embed=_embed_success("✅ تم الفحص! الفصول الجديدة ستظهر في القناة المحددة.")
        )

    @tree.command(name="adddev", description="[مطور] إضافة مطور جديد بواسطة User ID")
    @dev_only()
    async def adddev(interaction: discord.Interaction, user_id: str) -> None:
        if not user_id.isdigit():
            await interaction.response.send_message(embed=_embed_error("❌ الـ ID يجب أن يكون رقماً."), ephemeral=True)
            return
        uid = int(user_id)
        env_ids = _load_env_dev_ids()
        if uid in env_ids:
            await interaction.response.send_message(
                embed=_embed_warn("⚠️ هذا الـ ID مضاف مسبقاً في المتغيرات البيئية."), ephemeral=True
            )
            return
        added = await db.add_db_developer_id(uid)
        if not added:
            await interaction.response.send_message(embed=_embed_warn("⚠️ هذا الـ ID مضاف مسبقاً."), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=_embed_success(f"✅ تمت إضافة `{user_id}` للمطورين (يُحفظ في قاعدة البيانات)."), ephemeral=True
        )

    @tree.command(name="removedev", description="[مطور] إزالة مطور بواسطة User ID")
    @dev_only()
    async def removedev(interaction: discord.Interaction, user_id: str) -> None:
        if not user_id.isdigit():
            await interaction.response.send_message(embed=_embed_error("❌ الـ ID يجب أن يكون رقماً."), ephemeral=True)
            return
        uid = int(user_id)
        env_ids = _load_env_dev_ids()
        if uid in env_ids:
            await interaction.response.send_message(
                embed=_embed_error(
                    "❌ هذا الـ ID محدد في المتغيرات البيئية `DEVELOPER_IDS` ولا يمكن حذفه من هنا."
                ),
                ephemeral=True,
            )
            return
        removed = await db.remove_db_developer_id(uid)
        if not removed:
            await interaction.response.send_message(
                embed=_embed_error("❌ هذا الـ ID غير موجود في قائمة المطورين."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=_embed_success(f"✅ تمت إزالة `{user_id}` من المطورين."), ephemeral=True
        )

    @tree.command(name="listdevs", description="[مطور] عرض قائمة المطورين")
    @dev_only()
    async def listdevs(interaction: discord.Interaction) -> None:
        env_ids = _load_env_dev_ids()
        db_ids = await db.get_db_developer_ids()
        all_ids = env_ids | db_ids
        if not all_ids:
            await interaction.response.send_message(embed=_embed_info("📋 لا يوجد مطورون مضافون."), ephemeral=True)
            return
        lines = []
        for uid in sorted(all_ids):
            source = "🔒 env" if uid in env_ids else "💾 db"
            lines.append(f"• `{uid}` ({source})")
        embed = discord.Embed(
            title="المطورون",
            description="\n".join(lines),
            color=EMBED_COLOR,
        )
        embed.set_author(name="👨‍💻 قائمة المطورين", icon_url=SITE_ICON)
        embed.set_footer(text=f"{len(all_ids)} مطور  •  {SITE_NAME}", icon_url=_FOOTER_ICON)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── عام (الكل يستخدمها) ──────────────────────────────────────────────────

    @tree.command(name="search", description="ابحث عن مانغا/مانهوا/مانها وشوف فصولها")
    async def search(interaction: discord.Interaction, الاسم: str) -> None:
        await interaction.response.defer()
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, search_manga, الاسم)
        if not results:
            await interaction.followup.send(
                embed=_embed_error(f"❌ لم أجد **{الاسم}** في مانجا ستارز."), ephemeral=True
            )
            return
        user_id = interaction.user.id
        if len(results) == 1:
            best = results[0]
            cover_url = best.get("cover", "")
            cover_file = await _get_cover_file(cover_url)
            log.info("[search] cover_file=%s cover_url=%s", cover_file is not None, bool(cover_url))
            embed = discord.Embed(
                title=best["title"],
                url=best.get("url", SITE_URL),
                color=EMBED_COLOR,
            )
            embed.set_author(name="🔍 هل تقصد هذا؟", icon_url=SITE_ICON)
            if cover_file:
                embed.set_image(url=f"attachment://{cover_file.filename}")
            embed.set_footer(text=f"{SITE_NAME}  •  اضغط زر الفصول أو المتابعة أدناه", icon_url=_FOOTER_ICON)
            view = SearchConfirmView(best, bot, user_id=user_id)
            if cover_file:
                await interaction.edit_original_response(embed=embed, view=view, attachments=[cover_file])
            else:
                await interaction.edit_original_response(embed=embed, view=view)
        else:
            shown = min(len(results), 25)
            best = results[0]
            cover_url = best.get("cover", "")
            cover_file = await _get_cover_file(cover_url)
            log.info("[search-multi] cover_file=%s cover_url=%s", cover_file is not None, bool(cover_url))
            list_lines = [
                f"`{i+1}.` [{r['title']}]({r['url']})" if r.get("url") else f"`{i+1}.` {r['title']}"
                for i, r in enumerate(results[:shown])
            ]
            embed = discord.Embed(
                title=f"نتائج البحث عن: {الاسم}",
                description="\n".join(list_lines),
                color=EMBED_COLOR,
            )
            embed.set_author(name="🔍 بحث", icon_url=SITE_ICON)
            if cover_file:
                embed.set_thumbnail(url=f"attachment://{cover_file.filename}")
            embed.set_footer(
                text=f"{SITE_NAME}  •  {shown} نتيجة — اختر من القائمة أدناه",
                icon_url=_FOOTER_ICON,
            )
            view = SearchResultsView(results, bot=bot, user_id=user_id)
            if cover_file:
                await interaction.edit_original_response(embed=embed, view=view, attachments=[cover_file])
            else:
                await interaction.edit_original_response(embed=embed, view=view)

    @tree.command(name="watch", description="تلقي إشعارات بالخاص عند صدور فصل جديد")
    async def watch(interaction: discord.Interaction, الاسم: str) -> None:
        await interaction.response.defer()
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, search_manga, الاسم)
        if not results:
            await interaction.followup.send(
                embed=_embed_error(f"❌ لم أجد **{الاسم}** في مانجا ستارز."), ephemeral=True
            )
            return

        user_id = interaction.user.id
        if len(results) == 1:
            best = results[0]
            cover_url = best.get("cover", "")
            cover_file = await _get_cover_file(cover_url)
            log.info("[watch] cover_file=%s cover_url=%s", cover_file is not None, bool(cover_url))
            embed = discord.Embed(
                title=best["title"],
                url=best.get("url", SITE_URL),
                color=EMBED_COLOR,
            )
            embed.set_author(name="🔔 هل تريد تلقي إشعارات لهذا العنوان؟", icon_url=SITE_ICON)
            if cover_file:
                embed.set_image(url=f"attachment://{cover_file.filename}")
            embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
            view = WatchConfirmView(best, bot, user_id=user_id)
            if cover_file:
                await interaction.edit_original_response(embed=embed, view=view, attachments=[cover_file])
            else:
                await interaction.edit_original_response(embed=embed, view=view)
        else:
            shown = min(len(results), 25)
            best = results[0]
            cover_url = best.get("cover", "")
            cover_file = await _get_cover_file(cover_url)
            log.info("[watch-multi] cover_file=%s cover_url=%s", cover_file is not None, bool(cover_url))
            embed = discord.Embed(
                title=f"اشتراك في: {الاسم}",
                description=f"وُجد **{shown}** نتيجة — اختر العنوان الصحيح لمتابعته:",
                color=EMBED_COLOR,
            )
            embed.set_author(name="🔔 متابعة عنوان", icon_url=SITE_ICON)
            if cover_file:
                embed.set_thumbnail(url=f"attachment://{cover_file.filename}")
            embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
            view = WatchResultsView(results, bot, user_id=user_id)
            if cover_file:
                await interaction.edit_original_response(embed=embed, view=view, attachments=[cover_file])
            else:
                await interaction.edit_original_response(embed=embed, view=view)

    @tree.command(name="unwatch", description="إيقاف إشعارات الخاص لمانغا/مانهوا معينة")
    async def unwatch(interaction: discord.Interaction, الاسم: str) -> None:
        subs = await db.get_dm_subscriptions(interaction.user.id)
        query = الاسم.lower()
        # exact match first, then partial — avoids removing wrong title
        match = next((s for s in subs if s["title"].lower() == query), None)
        if not match:
            match = next((s for s in subs if query in s["title"].lower()), None)
        if not match:
            await interaction.response.send_message(
                embed=_embed_error(f"❌ لم أجد **{الاسم}** في قائمة اشتراكاتك."), ephemeral=True
            )
            return
        await db.remove_dm_subscription(interaction.user.id, match["url"])
        await interaction.response.send_message(
            embed=_embed_success(f"✅ تم إيقاف إشعارات الخاص لـ **{match['title']}**."), ephemeral=True
        )
        await bot._update_presence()

    @tree.command(name="list", description="عرض العناوين التي تتلقى إشعاراتها بالخاص")
    async def list_subs(interaction: discord.Interaction) -> None:
        subs = await db.get_dm_subscriptions(interaction.user.id)
        if not subs:
            await interaction.response.send_message(
                embed=_embed_info("📋 لا توجد اشتراكات. استخدم `/watch` لإضافة عنوان."), ephemeral=True
            )
            return
        lines = [f"`{i+1}.` [{s['title']}]({s['url']})" for i, s in enumerate(subs)]
        embed = discord.Embed(
            title="اشتراكاتك بالخاص",
            description="\n".join(lines),
            color=EMBED_COLOR,
        )
        embed.set_author(name="📬 قائمة المتابعة", icon_url=SITE_ICON)
        embed.set_footer(
            text=f"{SITE_NAME}  •  {len(subs)} عنوان — استخدم /unwatch للإلغاء",
            icon_url=_FOOTER_ICON,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="latest", description="آخر 10 فصول من مانجا ستارز — خاص وسيرفر")
    async def latest(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        try:
            chapters = await loop.run_in_executor(None, fetch_latest_chapters)
        except Exception:
            await interaction.followup.send(embed=_embed_error("❌ فشل جلب البيانات، حاول مجدداً."), ephemeral=True)
            return
        if not chapters:
            await interaction.followup.send(embed=_embed_error("❌ لم يتم العثور على فصول."), ephemeral=True)
            return

        seen: set[str] = set()
        unique: list[Chapter] = []
        for ch in chapters:
            if ch.chapter_url not in seen:
                seen.add(ch.chapter_url)
                unique.append(ch)
            if len(unique) == 10:
                break

        lines = [
            f"`{i+1}.` **[{ch.manga_title}]({ch.manga_url or ch.chapter_url})** — [ف. {ch.chapter_num}]({ch.chapter_url})"
            for i, ch in enumerate(unique)
        ]
        embed = discord.Embed(
            title="آخر الفصول الصادرة",
            url=SITE_URL,
            description="\n".join(lines),
            color=EMBED_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="📋 آخر الفصول", icon_url=SITE_ICON)
        cover_file = None
        if unique and unique[0].cover_url:
            cover_file = await _get_cover_file(unique[0].cover_url)
            if cover_file:
                embed.set_thumbnail(url=f"attachment://{cover_file.filename}")
        embed.set_footer(text=f"{SITE_NAME}  •  آخر {len(unique)} فصل", icon_url=_FOOTER_ICON)
        if cover_file:
            await interaction.followup.send(embed=embed, file=cover_file, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(name="setnewschannel", description="[مطور] تعيين قناة أخبار الأنمي من @CrunchyrollMENA")
    @dev_only()
    async def setnewschannel(
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message(embed=_embed_error("❌ يعمل في السيرفر فقط."), ephemeral=True)
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(embed=_embed_error("❌ الرجاء تحديد قناة نصية."), ephemeral=True)
            return
        await db.set_news_channel(interaction.guild_id, target.id)
        await interaction.response.send_message(
            embed=_embed_success(
                f"✅ أخبار الأنمي ستُرسل إلى {target.mention} (كل {NEWS_POLL_INTERVAL_MINUTES} دقيقة)"
            ),
            ephemeral=True,
        )

    @tree.command(name="checknews", description="[مطور] فحص أخبار الأنمي فوراً")
    @dev_only()
    async def checknews(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=_embed_info("🔍 جاري فحص أخبار الأنمي…"), ephemeral=True)
        await bot._check_for_new_tweets()
        await interaction.edit_original_response(
            embed=_embed_success("✅ تم الفحص! الأخبار الجديدة ستظهر في القناة المحددة.")
        )

    @tree.command(name="latestnews", description="آخر 5 أخبار أنمي من @CrunchyrollMENA")
    async def latestnews(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        tweets = get_cached_tweets(5)
        if not tweets:
            loop = asyncio.get_running_loop()
            try:
                tweets = await loop.run_in_executor(None, fetch_latest_tweets, 5)
            except Exception:
                pass
        if not tweets:
            await interaction.followup.send(
                embed=_embed_info("⏳ لم تُجلب الأخبار بعد — حاول بعد قليل أو استخدم `/checknews`."),
                ephemeral=True,
            )
            return
        for tweet in tweets[:5]:
            embeds = _build_tweet_embeds(tweet)
            await interaction.followup.send(embeds=embeds)

    @tree.command(name="setanimechannel", description="[مطور] تعيين قناة إشعارات حلقات الأنمي")
    @dev_only()
    async def setanimechannel(
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message(embed=_embed_error("❌ يعمل في السيرفر فقط."), ephemeral=True)
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(embed=_embed_error("❌ الرجاء تحديد قناة نصية."), ephemeral=True)
            return
        await db.set_anime_notify_channel(interaction.guild_id, target.id)
        await interaction.response.send_message(
            embed=_embed_success(
                f"✅ إشعارات حلقات الأنمي ستُرسل إلى {target.mention} (كل {ANIME_POLL_INTERVAL_MINUTES} دقيقة)"
            ),
            ephemeral=True,
        )

    @tree.command(name="checkanime", description="[مطور] فحص حلقات الأنمي فوراً")
    @dev_only()
    async def checkanime(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=_embed_info("🔍 جاري فحص حلقات الأنمي…"), ephemeral=True)
        await bot._check_for_new_episodes()
        await interaction.edit_original_response(
            embed=_embed_success("✅ تم الفحص! الحلقات الجديدة ستظهر في القناة المحددة.")
        )

    @tree.command(name="schedule", description="مواعيد حلقات الأنمي اليوم أو هذا الأسبوع")
    async def schedule(
        interaction: discord.Interaction,
        نطاق: Optional[str] = "today",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        try:
            if نطاق and "week" in نطاق.lower():
                episodes = await loop.run_in_executor(None, fetch_airing_week)
                title = "📅 حلقات الأنمي — هذا الأسبوع"
            else:
                episodes = await loop.run_in_executor(None, fetch_airing_today)
                title = "📅 حلقات الأنمي — اليوم"
        except Exception:
            await interaction.followup.send(embed=_embed_error("❌ فشل جلب البيانات، حاول مجدداً."), ephemeral=True)
            return
        if not episodes:
            await interaction.followup.send(embed=_embed_info("📭 لا توجد حلقات في هذا النطاق الزمني."), ephemeral=True)
            return
        embed = _build_schedule_embed(episodes, title)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(name="status", description="حالة البوت واشتراكاتك")
    async def status(interaction: discord.Interaction) -> None:
        total = await db.get_dm_user_count()
        subs = await db.get_dm_subscriptions(interaction.user.id)
        my_count = len(subs)

        embed = discord.Embed(
            title="حالة البوت",
            url=SITE_URL,
            color=EMBED_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="📊 مانجا ستارز Bot", icon_url=SITE_ICON)
        embed.add_field(name="📡 المصدر", value=f"[{SITE_NAME}]({SITE_URL})", inline=True)
        embed.add_field(name="⏱ فحص الفصول", value=f"كل {POLL_INTERVAL_MINUTES} د.", inline=True)
        embed.add_field(name="📰 فحص الأخبار", value=f"كل {NEWS_POLL_INTERVAL_MINUTES} د.", inline=True)
        embed.add_field(name="✨ إجمالي المشتركين", value=f"**{total}** مستخدم", inline=True)
        embed.add_field(
            name="📬 اشتراكاتك",
            value=f"**{my_count}** عنوان" if my_count else "لا توجد — جرّب `/watch`",
            inline=True,
        )
        embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
        await interaction.response.send_message(embed=embed, ephemeral=True)


    # ── Anime search + torrent commands ───────────────────────────────────────

    class SlayerEpisodeView(discord.ui.View):
        """View shown with an Anime Slayer episode embed — download + share buttons."""

        def __init__(self, watch_url: str, title: str = "", ep_num: int = 0) -> None:
            super().__init__(timeout=300)
            self.watch_url = watch_url
            self.share_title = title
            self.share_ep_num = ep_num

        @discord.ui.button(label="⬇️ روابط التحميل", style=discord.ButtonStyle.primary)
        async def download_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            loop = asyncio.get_running_loop()

            urls: dict[str, str] = await loop.run_in_executor(
                None, get_stream_url_slayer, self.watch_url
            )

            if not urls:
                await interaction.followup.send(
                    embed=_embed_error(
                        "❌ تعذّر استخراج روابط التحميل.\n"
                        "💡 جرّب مباشرةً من موقع [Anime Slayer]"
                        f"({self.watch_url})"
                    ),
                    ephemeral=True,
                )
                return

            # Build quality-sorted embed
            quality_order = ["1080p", "720p", "480p", "360p", "default"]
            embed = discord.Embed(
                title="روابط التحميل المباشر",
                color=ANIME_COLOR,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_author(name="⬇️ Anime Slayer — دبلجة عربية", icon_url=SITE_ICON)

            added = 0
            for q in quality_order:
                if q in urls:
                    label = q if q != "default" else "مباشر"
                    embed.add_field(
                        name=f"📥 {label}",
                        value=f"[تحميل]({urls[q]})",
                        inline=True,
                    )
                    added += 1

            # Ignore any keys outside the known quality labels — unexpected keys
            # (e.g. from a multi-episode playlist) must not appear as fields.

            embed.set_footer(text="الروابط مؤقتة — حمّل فوراً", icon_url=_FOOTER_ICON)
            await interaction.followup.send(embed=embed, ephemeral=True)

        @discord.ui.button(label="📤 شارك الحلقة", style=discord.ButtonStyle.secondary)
        async def share_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            """Send the episode watch link publicly to the channel."""
            if interaction.channel is None:
                await interaction.response.send_message(
                    embed=_embed_error("❌ تعذّر إرسال الرابط — الشانل غير متاح."),
                    ephemeral=True,
                )
                return

            # Build a compact share embed
            ep_label = f" — حلقة **{self.share_ep_num}**" if self.share_ep_num else ""
            title_line = f"**{self.share_title}**{ep_label}" if self.share_title else "حلقة أنمي"
            share_embed = discord.Embed(
                description=(
                    f"🎌 {title_line}\n\n"
                    f"▶️ [شاهد الآن على Anime Slayer]({self.watch_url})"
                ),
                color=ANIME_COLOR,
                url=self.watch_url,
            )
            share_embed.set_author(
                name=f"شارك {interaction.user.display_name} هذه الحلقة",
                icon_url=interaction.user.display_avatar.url,
            )
            share_embed.set_footer(text="animeslayer.to", icon_url=_FOOTER_ICON)

            await interaction.response.send_message(embed=share_embed)

    class EpisodeModal(discord.ui.Modal, title="اختر رقم الحلقة"):
        episode_input = discord.ui.TextInput(
            label="رقم الحلقة",
            placeholder="مثال: 1",
            required=True,
            max_length=5,
        )

        def __init__(self, anime: AnimeResult) -> None:
            super().__init__()
            self.anime = anime

        async def on_submit(self, interaction: discord.Interaction) -> None:
            raw = self.episode_input.value.strip()
            if not raw.isdigit():
                await interaction.response.send_message(
                    embed=_embed_error("❌ أدخل رقم صحيح فقط."), ephemeral=True
                )
                return
            ep_num = int(raw)
            anime  = self.anime
            if anime.episodes and ep_num > anime.episodes:
                await interaction.response.send_message(
                    embed=_embed_error(
                        f"❌ العدد الكلي للحلقات هو **{anime.episodes}**."
                    ),
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)
            loop = asyncio.get_running_loop()

            # 1️⃣ Try Anime Slayer first (Arabic dubbed source)
            slayer_ep: AnimeSlayerEpisode | None = await loop.run_in_executor(
                None, find_episode_slayer, anime.title, ep_num
            )

            if slayer_ep:
                if slayer_ep.is_batch:
                    title_line = f"{anime.title} — حلقات (باك)"
                    batch_note = (
                        f"⚠️ لم يُعثر على الحلقة **{ep_num}** منفردةً — "
                        f"هذا الإدخال يحتوي على **مجموعة حلقات**"
                        + (f": {slayer_ep.title}" if slayer_ep.title else "")
                        + "\nيمكنك استخدام `/episode` مع رابط الحلقة المحددة مباشرةً."
                    )
                else:
                    title_line = f"{anime.title} — حلقة {ep_num}"
                    batch_note = ""

                embed = discord.Embed(
                    title=title_line,
                    url=slayer_ep.watch_url,
                    description=(
                        (f"**{slayer_ep.title}**\n\n" if slayer_ep.title and not slayer_ep.is_batch else "")
                        + batch_note
                    ) or None,
                    color=ANIME_COLOR,
                    timestamp=discord.utils.utcnow(),
                )
                embed.set_author(name="🎌 Anime Slayer — دبلجة عربية", icon_url=SITE_ICON)
                if slayer_ep.thumb:
                    embed.set_thumbnail(url=slayer_ep.thumb)
                embed.add_field(name="شاهد الآن", value=f"[اضغط هنا ◀]({slayer_ep.watch_url})", inline=False)
                embed.set_footer(text="animeslayer.to", icon_url=_FOOTER_ICON)
                view = SlayerEpisodeView(slayer_ep.watch_url, title=anime.title, ep_num=ep_num)
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)
                return

            await interaction.followup.send(
                embed=_embed_error(
                    f"❌ لم يُعثر على الحلقة **{ep_num}** من **{anime.title}** على Anime Slayer.\n"
                    "💡 جرّب `/episode` مع رابط الحلقة مباشرةً."
                ),
                ephemeral=True,
            )

    class AnimeSelectMenu(discord.ui.Select):
        def __init__(self, results: list[AnimeResult], user_id: int) -> None:
            from .anidl import _is_arabic

            options = []
            for i, a in enumerate(results[:25]):
                # Label: prefer Arabic synonym when available, else English title
                label = (
                    a.title_ar if (a.title_ar and _is_arabic(a.title_ar)) else a.title
                )[:100]

                # Description: format + episodes + the other-language title
                other = a.title if (a.title_ar and _is_arabic(a.title_ar)) else a.title_ar
                desc_parts = []
                if a.format:
                    desc_parts.append(a.format)
                desc_parts.append(f"{a.episodes or '?'} حلقة")
                if other and other != a.title and other != a.title_ar:
                    desc_parts.append(other[:40])
                desc = " • ".join(desc_parts)[:100]

                options.append(discord.SelectOption(label=label, value=str(i), description=desc))

            super().__init__(
                placeholder="اختر الأنمي…",
                min_values=1, max_values=1,
                options=options,
            )
            self.results  = results
            self.user_id  = user_id

        async def callback(self, interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    embed=_embed_error("❌ هذه القائمة ليست لك."), ephemeral=True
                )
                return
            anime = self.results[int(self.values[0])]
            await interaction.response.send_modal(EpisodeModal(anime))

    class AnimeSearchView(discord.ui.View):
        def __init__(self, results: list[AnimeResult], user_id: int) -> None:
            super().__init__(timeout=60)
            self.add_item(AnimeSelectMenu(results, user_id))

    @tree.command(name="anime", description="ابحث عن أنمي واحصل على رابط تحميل الحلقة")
    @app_commands.describe(الاسم="اسم الأنمي بالعربي أو الإنجليزي")
    async def anime_search(interaction: discord.Interaction, الاسم: str) -> None:
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()

        results: list[AnimeResult] = await loop.run_in_executor(
            None, search_anime, الاسم
        )

        if not results:
            await interaction.followup.send(
                embed=_embed_error(
                    f"❌ لا توجد نتائج لـ **{الاسم}**.\n"
                    "💡 جرّب الكتابة **بالإنجليزي** مثل: `naruto` أو `one piece`"
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"نتائج البحث: {الاسم}",
            description=f"وُجد **{len(results)}** نتيجة — اختر الأنمي ثم أدخل رقم الحلقة:",
            color=ANIME_COLOR,
        )
        embed.set_author(name="🔍 بحث أنمي", icon_url=SITE_ICON)
        if results[0].cover_url:
            embed.set_thumbnail(url=results[0].cover_url)
        embed.set_footer(text="The sky  •  AniList", icon_url=_FOOTER_ICON)

        view = AnimeSearchView(results, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── Episode download view ────────────────────────────────────────────────
    class EpisodeDownloadView(discord.ui.View):
        """Persistent button that reveals quality-keyed download links on demand."""

        def __init__(self, links: list[tuple[str, str]], source_label: str) -> None:
            super().__init__(timeout=600)
            self._links = links          # [(label, url), ...]
            self._source = source_label

        @discord.ui.button(label="تحميل", style=discord.ButtonStyle.primary, emoji="📥")
        async def download_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            if not self._links:
                await interaction.response.send_message(
                    embed=_embed_error("❌ لا توجد روابط تحميل متاحة."), ephemeral=True
                )
                return
            embed = discord.Embed(
                title="روابط التحميل المباشر",
                color=ANIME_COLOR,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_author(name=f"⬇️ {self._source}", icon_url=SITE_ICON)
            for label, url in self._links:
                embed.add_field(
                    name=f"📥 {label}",
                    value=f"[اضغط للتحميل]({url})" if len(url) <= 512 else "الرابط طويل جداً",
                    inline=True,
                )
            embed.set_footer(text="الروابط مؤقتة — حمّل فوراً", icon_url=_FOOTER_ICON)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="episode", description="استخرج رابط تحميل حلقة من animeslayer.to أو anime3rb.com")
    @app_commands.describe(
        الرابط="رابط صفحة الحلقة من animeslayer.to أو anime3rb.com",
    )
    async def episode_dl(
        interaction: discord.Interaction, الرابط: str
    ) -> None:
        is_slayer   = "animeslayer.to" in الرابط
        is_anime3rb = "anime3rb.com"   in الرابط

        if not is_slayer and not is_anime3rb:
            await interaction.response.send_message(
                embed=_embed_error(
                    "❌ الرابط يجب أن يكون من:\n"
                    "• **animeslayer.to** — مثال: `https://animeslayer.to/e/حلقة-123#hash`\n"
                    "• **anime3rb.com**"
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        loop = asyncio.get_running_loop()

        # ── Anime Slayer path ────────────────────────────────────────────────
        if is_slayer:
            # Fetch metadata and stream URLs in parallel
            (title, thumb), urls = await asyncio.gather(
                loop.run_in_executor(None, get_episode_meta_slayer, الرابط),
                loop.run_in_executor(None, get_stream_url_slayer, الرابط),
            )

            if not urls:
                await interaction.followup.send(
                    embed=_embed_error(
                        "❌ تعذّر استخراج روابط التحميل من Anime Slayer.\n"
                        "💡 جرّب فتح الرابط مباشرةً من المتصفح."
                    ),
                )
                return

            quality_order = ["1080p", "720p", "480p", "360p", "default"]
            links = [
                (q if q != "default" else "مباشر", urls[q])
                for q in quality_order if q in urls
            ]

            embed = discord.Embed(
                title=title or الرابط.rsplit("/", 1)[-1] or "حلقة",
                url=الرابط,
                color=ANIME_COLOR,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_author(name="🎬 Anime Slayer — دبلجة عربية", icon_url=SITE_ICON)
            if thumb:
                embed.set_image(url=thumb)
            embed.set_footer(text="The sky", icon_url=_FOOTER_ICON)

            view = EpisodeDownloadView(links, source_label="Anime Slayer")
            await interaction.followup.send(embed=embed, view=view)
            return

        # ── anime3rb path (yt-dlp) ───────────────────────────────────────────
        def _extract() -> dict:
            """
            Extract info for a single anime3rb episode.

            Root-cause guard against the "all episodes" bug:
            anime3rb page JS exposes the full series playlist to yt-dlp, so
            extract_info() can return a playlist wrapper (type='playlist' /
            'multi_video') with every episode as an entry.  We unwrap the
            wrapper and take ONLY the first entry — which corresponds to the
            episode URL the user supplied — rather than letting the caller
            see hundreds of entries.

            Extra safeguards:
            • playlist_items='1'  → yt-dlp itself fetches at most 1 entry
            • noplaylist=True     → prefer single-video interpretation
            • We never fall back to webpage_url (that's the series page, not a video)
            """
            import yt_dlp

            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "playlist_items": "1",
                "http_headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36"
                    ),
                    "Referer": "https://anime3rb.com/",
                },
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                raw = ydl.extract_info(الرابط, download=False) or {}

            for _ in range(5):
                if raw.get("_type") not in ("playlist", "multi_video"):
                    break
                entries = [e for e in (raw.get("entries") or []) if e]
                if not entries:
                    break
                raw = entries[0]

            return raw

        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(None, _extract),
                timeout=40,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                embed=_embed_error("⏰ انتهى الوقت — الموقع بطيء أو محجوب.")
            )
            return
        except Exception as exc:
            await interaction.followup.send(
                embed=_embed_error(f"❌ تعذّر استخراج الرابط:\n```{str(exc)[:300]}```")
            )
            return

        title = info.get("title") or "حلقة"
        thumb = info.get("thumbnail") or ""

        # Collect direct video stream formats (mp4 / m3u8 / ts)
        formats = info.get("formats") or []
        video_formats = [
            f for f in formats
            if f.get("vcodec") not in (None, "none")
            and f.get("url")
            and (f.get("ext") or "").lower() in ("mp4", "m3u8", "ts", "")
        ]
        video_formats.sort(key=lambda f: (f.get("height") or 0), reverse=True)

        if not video_formats:
            direct = info.get("url") or ""
            if not direct or "anime3rb.com" in direct:
                await interaction.followup.send(
                    embed=_embed_error(
                        "❌ لم يُعثر على رابط فيديو مباشر.\n"
                        "💡 تأكد أن الرابط هو صفحة **حلقة** وليس صفحة **الأنمي** كاملاً.\n"
                        "مثال صحيح: `https://anime3rb.com/episodes/chainsaw-man-1`"
                    ),
                )
                return
            video_formats = [{"url": direct, "height": 0, "ext": "mp4"}]

        # Deduplicate by height, keep top 4
        seen_heights: set = set()
        links = []
        for fmt in video_formats:
            height = fmt.get("height") or 0
            if height in seen_heights and height != 0:
                continue
            seen_heights.add(height)
            ext   = (fmt.get("ext") or "mp4").upper()
            label = f"{height}p ({ext})" if height else f"Auto ({ext})"
            links.append((label, fmt["url"]))
            if len(links) >= 4:
                break

        if not links:
            await interaction.followup.send(
                embed=_embed_error("❌ لم يُعثر على جودات قابلة للتشغيل.")
            )
            return

        embed = discord.Embed(
            title=title[:256],
            url=الرابط,
            color=ANIME_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="🎬 anime3rb", icon_url=SITE_ICON)
        if thumb:
            embed.set_image(url=thumb)
        embed.set_footer(text="The sky", icon_url=_FOOTER_ICON)

        view = EpisodeDownloadView(links, source_label="anime3rb")
        await interaction.followup.send(embed=embed, view=view)

    # ── Voice playback ───────────────────────────────────────────────────────
    def _pick_voice_url(urls: dict[str, str]) -> Optional[str]:
        for q in _VOICE_QUALITY_ORDER:
            if q in urls:
                return urls[q]
        return next(iter(urls.values()), None)

    class VoicePlaybackView(discord.ui.View):
        """Attached to the 'now playing' message — lets anyone stop playback."""

        def __init__(self, guild_id: int) -> None:
            super().__init__(timeout=None)
            self.guild_id = guild_id

        @discord.ui.button(label="⏹️ إيقاف ومغادرة", style=discord.ButtonStyle.danger)
        async def stop_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            guild = interaction.guild
            vc = guild.voice_client if guild else None
            if not vc:
                await interaction.response.send_message(
                    embed=_embed_warn("⚠️ البوت مو داخل روم صوتي حالياً."), ephemeral=True
                )
                return
            bot._cancel_voice_idle_timer(self.guild_id)
            vc.stop()
            await vc.disconnect(force=True)
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                embed=_embed_success("✅ تم الإيقاف ومغادرة الروم الصوتي."), ephemeral=True
            )

    @tree.command(name="voicewatch", description="يدخل البوت رومك الصوتي ويشغّل صوت الحلقة مباشرة")
    @app_commands.describe(الاسم="اسم الأنمي", الحلقة="رقم الحلقة")
    async def voice_watch(interaction: discord.Interaction, الاسم: str, الحلقة: int) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=_embed_error("❌ هذا الأمر يعمل في السيرفر فقط."), ephemeral=True
            )
            return

        member = interaction.user
        voice_state = getattr(member, "voice", None)
        if voice_state is None or voice_state.channel is None:
            await interaction.response.send_message(
                embed=_embed_error("❌ لازم تكون داخل روم صوتي أول."), ephemeral=True
            )
            return
        target_channel = voice_state.channel

        perms = target_channel.permissions_for(interaction.guild.me)
        if not (perms.connect and perms.speak):
            await interaction.response.send_message(
                embed=_embed_error("❌ ما عندي صلاحية الدخول/التحدث في هذا الروم الصوتي."),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        loop = asyncio.get_running_loop()

        slayer_ep: AnimeSlayerEpisode | None = await loop.run_in_executor(
            None, find_episode_slayer, الاسم, الحلقة
        )
        if not slayer_ep:
            await interaction.followup.send(
                embed=_embed_error(
                    f"❌ لم يُعثر على الحلقة **{الحلقة}** من **{الاسم}** على Anime Slayer."
                ),
            )
            return

        urls: dict[str, str] = await loop.run_in_executor(
            None, get_stream_url_slayer, slayer_ep.watch_url
        )
        stream_url = _pick_voice_url(urls)
        if not stream_url:
            await interaction.followup.send(
                embed=_embed_error("❌ تعذّر استخراج رابط الصوت/الفيديو لهذه الحلقة."),
            )
            return

        try:
            vc = interaction.guild.voice_client
            if vc is None:
                vc = await target_channel.connect()
            elif vc.channel.id != target_channel.id:
                await vc.move_to(target_channel)
        except discord.ClientException as exc:
            await interaction.followup.send(
                embed=_embed_error(f"❌ تعذّر الاتصال بالروم الصوتي:\n```{exc}```"),
            )
            return

        if vc.is_playing() or vc.is_paused():
            vc.stop()

        guild_id = interaction.guild.id
        bot._cancel_voice_idle_timer(guild_id)

        def _after_playback(error: Optional[Exception]) -> None:
            if error:
                log.warning("[voice] playback error in guild %d: %s", guild_id, error)
            bot.loop.call_soon_threadsafe(bot._schedule_voice_idle_disconnect, guild_id)

        source = discord.FFmpegPCMAudio(
            stream_url,
            before_options=_FFMPEG_BEFORE_OPTIONS,
            options=_FFMPEG_OPTIONS,
        )
        vc.play(source, after=_after_playback)

        embed = discord.Embed(
            title=f"🔊 جاري التشغيل — {الاسم} • حلقة {الحلقة}",
            description=f"يتم البث الصوتي الآن في **{target_channel.name}**",
            color=ANIME_COLOR,
        )
        embed.set_author(name="🎧 Voice Watch", icon_url=SITE_ICON)
        if slayer_ep.thumb:
            embed.set_thumbnail(url=slayer_ep.thumb)
        embed.set_footer(text="صوت فقط — البوتات لا تقدر تشارك فيديو داخل ديسكورد", icon_url=_FOOTER_ICON)

        await interaction.followup.send(embed=embed, view=VoicePlaybackView(guild_id))

    # ── Watch Together ───────────────────────────────────────────────────────
    class WatchTogetherView(discord.ui.View):
        """Posted with the episode embed — a shared countdown button so every
        participant clicks the video link at the same moment, a direct link
        button to open the episode, and a self-reported 'opened it' tracker.

        Note: Discord link-style buttons never fire an interaction event (they
        just open the URL client-side), so the bot has no way to detect a real
        click. The '✅ فتحتها' button lets participants confirm manually so the
        group can see who's ready.
        """

        def __init__(
            self,
            title: str,
            episode_num: int,
            video_url: str,
            voice_channel: Optional[discord.VoiceChannel] = None,
        ) -> None:
            super().__init__(timeout=None)
            self.title = title
            self.episode_num = episode_num
            self.voice_channel = voice_channel
            self._counting = False
            self._ready: set[int] = set()
            self.add_item(
                discord.ui.Button(
                    label="▶️ فتح الحلقة",
                    style=discord.ButtonStyle.link,
                    url=video_url,
                )
            )

        def _ready_field_value(self) -> str:
            if not self._ready:
                return "لا أحد بعد"
            return "، ".join(f"<@{uid}>" for uid in self._ready)

        def _sync_ready_field(self, embed: discord.Embed) -> None:
            for i, field in enumerate(embed.fields):
                if field.name == "✅ جاهزون":
                    embed.set_field_at(
                        i, name="✅ جاهزون", value=self._ready_field_value(), inline=False
                    )
                    return
            embed.add_field(name="✅ جاهزون", value=self._ready_field_value(), inline=False)

        @discord.ui.button(label="✅ فتحتها", style=discord.ButtonStyle.secondary)
        async def mark_ready_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            self._ready.add(interaction.user.id)
            embed = interaction.message.embeds[0].copy()
            self._sync_ready_field(embed)
            await interaction.response.edit_message(embed=embed, view=self)

        @discord.ui.button(label="🚀 ابدأ العد التنازلي", style=discord.ButtonStyle.success)
        async def start_countdown(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            if self._counting:
                await interaction.response.send_message(
                    embed=_embed_warn("⚠️ فيه عدّ تنازلي شغّال حالياً."), ephemeral=True
                )
                return
            self._counting = True
            button.disabled = True
            await interaction.response.edit_message(view=self)

            base_embed = interaction.message.embeds[0]
            try:
                for n in (3, 2, 1):
                    embed = base_embed.copy()
                    embed.description = f"## ⏳ {n}...\nاستعدوا! جهزوا الحلقة وما تضغطون إلا لما يوصل العدّ للصفر."
                    await interaction.edit_original_response(embed=embed, view=self)
                    await asyncio.sleep(1)

                go_embed = base_embed.copy()
                go_embed.description = (
                    f"## 🎬 دزّوها الحين!\nكل الأعضاء يضغطون **▶️ فتح الحلقة** بنفس اللحظة "
                    f"عشان تتفرجون على **{self.title} — حلقة {self.episode_num}** مع بعض!"
                )
                await interaction.edit_original_response(embed=go_embed, view=self)
            finally:
                self._counting = False
                button.disabled = False
                await interaction.edit_original_response(view=self)

    @tree.command(
        name="watchtogether",
        description="ينشر رسالة 'مشاهدة معًا' فيها رابط الحلقة وعدّ تنازلي عشان تتفرجون كلكم بنفس الوقت",
    )
    @app_commands.describe(الاسم="اسم الأنمي", الحلقة="رقم الحلقة")
    async def watch_together(interaction: discord.Interaction, الاسم: str, الحلقة: int) -> None:
        await interaction.response.defer()
        loop = asyncio.get_running_loop()

        slayer_ep: AnimeSlayerEpisode | None = await loop.run_in_executor(
            None, find_episode_slayer, الاسم, الحلقة
        )
        if not slayer_ep:
            await interaction.followup.send(
                embed=_embed_error(
                    f"❌ لم يُعثر على الحلقة **{الحلقة}** من **{الاسم}** على Anime Slayer."
                ),
            )
            return

        urls: dict[str, str] = await loop.run_in_executor(
            None, get_stream_url_slayer, slayer_ep.watch_url
        )
        video_url = _pick_voice_url(urls)
        # Fallback: use the animeslayer watch page when direct CDN links are dead/unavailable
        link_url = video_url or slayer_ep.watch_url
        quality = (
            next((q for q, u in urls.items() if u == video_url), "غير معروف")
            if video_url
            else "صفحة المشاهدة"
        )

        embed = discord.Embed(
            title=f"🎉 مشاهدة معًا — {الاسم} • حلقة {الحلقة}",
            description=(
                "اضغط **🚀 ابدأ العد التنازلي** وانتظروا كلكم، بعدها اضغطوا "
                "**▶️ فتح الحلقة** بنفس اللحظة عشان تتفرجون مع بعض!"
            ),
            color=ANIME_COLOR,
        )
        embed.set_author(name="🍿 Watch Together", icon_url=SITE_ICON)
        if slayer_ep.thumb:
            embed.set_image(url=slayer_ep.thumb)
        embed.add_field(name="الجودة", value=quality, inline=True)
        embed.add_field(name="المصدر", value="Anime Slayer", inline=True)
        embed.set_footer(
            text="كل عضو يفتح الرابط بجهازه — البوت ما يقدر يشارك فيديو داخل ديسكورد",
            icon_url=_FOOTER_ICON,
        )

        await interaction.followup.send(
            embed=embed, view=WatchTogetherView(الاسم, الحلقة, link_url)
        )

    @tree.command(
        name="watchparty",
        description="يجمع بين الصوت المباشر بالروم الصوتي ورسالة 'مشاهدة معًا' بنفس الوقت",
    )
    @app_commands.describe(الاسم="اسم الأنمي", الحلقة="رقم الحلقة")
    async def watch_party(interaction: discord.Interaction, الاسم: str, الحلقة: int) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=_embed_error("❌ هذا الأمر يعمل في السيرفر فقط."), ephemeral=True
            )
            return

        await interaction.response.defer()
        loop = asyncio.get_running_loop()

        slayer_ep: AnimeSlayerEpisode | None = await loop.run_in_executor(
            None, find_episode_slayer, الاسم, الحلقة
        )
        if not slayer_ep:
            await interaction.followup.send(
                embed=_embed_error(
                    f"❌ لم يُعثر على الحلقة **{الحلقة}** من **{الاسم}** على Anime Slayer."
                ),
            )
            return

        urls: dict[str, str] = await loop.run_in_executor(
            None, get_stream_url_slayer, slayer_ep.watch_url
        )
        stream_url = _pick_voice_url(urls)
        # Fallback: use the animeslayer watch page when direct CDN links are dead/unavailable
        link_url = stream_url or slayer_ep.watch_url
        quality = (
            next((q for q, u in urls.items() if u == stream_url), "غير معروف")
            if stream_url
            else "صفحة المشاهدة"
        )

        # Try to join + play audio if the caller is in a voice channel AND we
        # have a direct streamable URL. This is best-effort — the watch-together
        # link/countdown still works even when voice playback is unavailable.
        voice_joined = False
        member = interaction.user
        voice_state = getattr(member, "voice", None)
        target_channel = voice_state.channel if voice_state else None

        if stream_url and target_channel is not None:
            perms = target_channel.permissions_for(interaction.guild.me)
            if perms.connect and perms.speak:
                try:
                    vc = interaction.guild.voice_client
                    if vc is None:
                        vc = await target_channel.connect()
                    elif vc.channel.id != target_channel.id:
                        await vc.move_to(target_channel)

                    if vc.is_playing() or vc.is_paused():
                        vc.stop()

                    guild_id = interaction.guild.id
                    bot._cancel_voice_idle_timer(guild_id)

                    def _after_playback(error: Optional[Exception]) -> None:
                        if error:
                            log.warning("[voice] playback error in guild %d: %s", guild_id, error)
                        bot.loop.call_soon_threadsafe(bot._schedule_voice_idle_disconnect, guild_id)

                    source = discord.FFmpegPCMAudio(
                        stream_url,
                        before_options=_FFMPEG_BEFORE_OPTIONS,
                        options=_FFMPEG_OPTIONS,
                    )
                    vc.play(source, after=_after_playback)
                    voice_joined = True
                except discord.ClientException as exc:
                    log.warning("[watchparty] voice connect failed: %s", exc)

        embed = discord.Embed(
            title=f"🎉 مشاهدة معًا — {الاسم} • حلقة {الحلقة}",
            description=(
                "اضغط **🚀 ابدأ العد التنازلي** وانتظروا كلكم، بعدها اضغطوا "
                "**▶️ فتح الحلقة** بنفس اللحظة عشان تتفرجون مع بعض!"
                + (
                    f"\n\n🔊 البوت داخل **{target_channel.name}** يبث صوت الحلقة مباشرة أيضاً!"
                    if voice_joined
                    else ""
                )
            ),
            color=ANIME_COLOR,
        )
        embed.set_author(name="🍿 Watch Party", icon_url=SITE_ICON)
        if slayer_ep.thumb:
            embed.set_image(url=slayer_ep.thumb)
        embed.add_field(name="الجودة", value=quality, inline=True)
        embed.add_field(name="المصدر", value="Anime Slayer", inline=True)
        embed.add_field(
            name="🔊 الصوت المباشر",
            value=f"شغّال في **{target_channel.name}**" if voice_joined else "لم يتم التفعيل — كن داخل روم صوتي عشانه يشتغل",
            inline=True,
        )
        embed.set_footer(
            text="كل عضو يفتح الرابط بجهازه — البوت ما يقدر يشارك فيديو داخل ديسكورد",
            icon_url=_FOOTER_ICON,
        )

        view = WatchTogetherView(الاسم, الحلقة, link_url, voice_channel=target_channel)
        await interaction.followup.send(embed=embed, view=view)

    # ── Go Live streaming (via personal user-account selfbot) ────────────────

    @tree.command(
        name="golive",
        description="يشغّل حلقة الأنمي كـ Go Live في الروم الصوتي (يتطلب USER_TOKEN)",
    )
    @app_commands.describe(الاسم="اسم الأنمي", الحلقة="رقم الحلقة")
    async def go_live(interaction: discord.Interaction, الاسم: str, الحلقة: int) -> None:
        from . import selfbot as _selfbot_mod

        if interaction.guild is None:
            await interaction.response.send_message(
                embed=_embed_error("❌ هذا الأمر يعمل في السيرفر فقط."), ephemeral=True
            )
            return

        member = interaction.user
        voice_state = getattr(member, "voice", None)
        if voice_state is None or voice_state.channel is None:
            await interaction.response.send_message(
                embed=_embed_error("❌ لازم تكون داخل روم صوتي أول."), ephemeral=True
            )
            return
        target_channel = voice_state.channel

        if not _selfbot_mod.is_ready():
            await interaction.response.send_message(
                embed=_embed_error(
                    "❌ حساب البث (selfbot) غير متصل.\n"
                    "تأكد أن **USER_TOKEN** مضبوط في Secrets ثم أعد تشغيل البوت."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        loop = asyncio.get_running_loop()

        slayer_ep: AnimeSlayerEpisode | None = await loop.run_in_executor(
            None, find_episode_slayer, الاسم, الحلقة
        )
        if not slayer_ep:
            await interaction.followup.send(
                embed=_embed_error(
                    f"❌ لم يُعثر على الحلقة **{الحلقة}** من **{الاسم}** على Anime Slayer."
                )
            )
            return

        urls: dict[str, str] = await loop.run_in_executor(
            None, get_stream_url_slayer, slayer_ep.watch_url
        )
        video_url = _pick_voice_url(urls)
        if not video_url:
            await interaction.followup.send(
                embed=_embed_error("❌ تعذّر استخراج رابط الفيديو لهذه الحلقة.")
            )
            return

        result = await _selfbot_mod.request_stream(
            guild_id=interaction.guild.id,
            channel_id=target_channel.id,
            video_url=video_url,
            referer=SLAYER_BASE_URL + "/",
        )

        if not result.get("ok"):
            err = result.get("error", "خطأ غير معروف")
            await interaction.followup.send(embed=_embed_error(f"❌ فشل البث:\n{err}"))
            return

        go_live_active = result.get("go_live", False)
        video_active   = result.get("video", False)
        quality = next((q for q, u in urls.items() if u == video_url), "")

        if video_active:
            status_line = "📹 **فيديو + صوت** — البث يتضمن الصورة والصوت!"
        elif go_live_active:
            status_line = "✅ **Go Live** مفعّل — الحلقة تبث بالفيديو والصوت!"
        else:
            status_line = "🔊 صوت فقط — الفيديو يتطلب DAVE/E2EE متوافق"

        embed = discord.Embed(
            title=f"📺 بث الأنمي — {الاسم} • حلقة {الحلقة}",
            description=(
                f"{status_line}\n"
                f"البث جاري في **{target_channel.name}**"
            ),
            color=ANIME_COLOR,
        )
        embed.set_author(name="📡 Go Live Stream", icon_url=SITE_ICON)
        if slayer_ep.thumb:
            embed.set_thumbnail(url=slayer_ep.thumb)
        if quality:
            embed.add_field(name="الجودة", value=quality, inline=True)
        embed.add_field(name="المصدر", value="Anime Slayer", inline=True)
        embed.set_footer(
            text="استخدم /leave_stream لإيقاف البث", icon_url=_FOOTER_ICON
        )
        await interaction.followup.send(embed=embed)

    @tree.command(name="leave_stream", description="إيقاف بث Go Live ومغادرة الروم الصوتي")
    async def leave_stream(interaction: discord.Interaction) -> None:
        from . import selfbot as _selfbot_mod

        if interaction.guild is None:
            await interaction.response.send_message(
                embed=_embed_error("❌ هذا الأمر يعمل في السيرفر فقط."), ephemeral=True
            )
            return
        await _selfbot_mod.request_stop(interaction.guild.id)
        await interaction.response.send_message(
            embed=_embed_success("✅ تم إيقاف بث Go Live ومغادرة الروم الصوتي."),
            ephemeral=True,
        )

    @tree.command(name="leave", description="إخراج البوت من الروم الصوتي")
    async def leave_voice(interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.voice_client is None:
            await interaction.response.send_message(
                embed=_embed_warn("⚠️ البوت مو داخل روم صوتي حالياً."), ephemeral=True
            )
            return
        bot._cancel_voice_idle_timer(interaction.guild.id)
        await interaction.guild.voice_client.disconnect(force=True)
        await interaction.response.send_message(
            embed=_embed_success("✅ تم مغادرة الروم الصوتي."), ephemeral=True
        )


# ── Entry point ───────────────────────────────────────────────────────────────

async def run() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN غير موجود في المتغيرات البيئية")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    _load_opus_library()

    bot = MangaBot()
    async with bot:
        await bot.start(token)
