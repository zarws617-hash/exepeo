"""SQLite storage for subscriptions, seen chapters, and series type cache."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiosqlite

DB_PATH   = "mangastarz_bot.db"
JSON_PATH = "data_backup.json"

log = logging.getLogger(__name__)


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen_chapters (
                chapter_url TEXT PRIMARY KEY,
                manga_title TEXT NOT NULL,
                chapter_num TEXT NOT NULL,
                seen_at     TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_seen_chapters_manga_title
                ON seen_chapters(manga_title);

            CREATE TABLE IF NOT EXISTS guild_channels (
                guild_id   INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                manga_url   TEXT NOT NULL,
                manga_title TEXT NOT NULL,
                UNIQUE(guild_id, manga_url)
            );

            CREATE TABLE IF NOT EXISTS series_type_cache (
                manga_url   TEXT PRIMARY KEY,
                series_type TEXT NOT NULL,
                cached_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS user_dm_subscriptions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                manga_url   TEXT NOT NULL,
                manga_title TEXT NOT NULL,
                UNIQUE(user_id, manga_url)
            );

            CREATE INDEX IF NOT EXISTS idx_user_dm_subscriptions_manga_url
                ON user_dm_subscriptions(manga_url);

            CREATE TABLE IF NOT EXISTS news_channels (
                guild_id   INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS seen_tweets (
                tweet_id TEXT PRIMARY KEY,
                seen_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS anime_notify_channels (
                guild_id   INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS seen_episodes (
                media_id   INTEGER NOT NULL,
                episode    INTEGER NOT NULL,
                seen_at    TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (media_id, episode)
            );

            CREATE TABLE IF NOT EXISTS developer_ids (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        await db.commit()


async def is_chapter_seen(chapter_url: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM seen_chapters WHERE chapter_url = ?", (chapter_url,)
        ) as cur:
            return await cur.fetchone() is not None


async def mark_chapter_seen(chapter_url: str, manga_title: str, chapter_num: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_chapters (chapter_url, manga_title, chapter_num) VALUES (?,?,?)",
            (chapter_url, manga_title, chapter_num),
        )
        await db.commit()


async def set_guild_channel(guild_id: int, channel_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO guild_channels (guild_id, channel_id) VALUES (?,?)",
            (guild_id, channel_id),
        )
        await db.commit()


async def get_guild_channel(guild_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT channel_id FROM guild_channels WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_all_guild_channels() -> list[tuple[int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT guild_id, channel_id FROM guild_channels") as cur:
            return await cur.fetchall()


async def add_subscription(guild_id: int, manga_url: str, manga_title: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO subscriptions (guild_id, manga_url, manga_title) VALUES (?,?,?)",
                (guild_id, manga_url, manga_title),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_subscription(guild_id: int, manga_url: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM subscriptions WHERE guild_id=? AND manga_url=?",
            (guild_id, manga_url),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_subscriptions(guild_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT manga_url, manga_title FROM subscriptions WHERE guild_id=?",
            (guild_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [{"url": r[0], "title": r[1]} for r in rows]


async def get_guilds_subscribed_to(manga_url: str) -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT guild_id FROM subscriptions WHERE manga_url=?", (manga_url,)
        ) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


async def get_cached_series_type(manga_url: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT series_type FROM series_type_cache WHERE manga_url = ?", (manga_url,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def cache_series_type(manga_url: str, series_type: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO series_type_cache (manga_url, series_type) VALUES (?,?)",
            (manga_url, series_type),
        )
        await db.commit()


async def add_dm_subscription(user_id: int, manga_url: str, manga_title: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO user_dm_subscriptions (user_id, manga_url, manga_title) VALUES (?,?,?)",
                (user_id, manga_url, manga_title),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_dm_subscription(user_id: int, manga_url: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM user_dm_subscriptions WHERE user_id=? AND manga_url=?",
            (user_id, manga_url),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_dm_subscriptions(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT manga_url, manga_title FROM user_dm_subscriptions WHERE user_id=?",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [{"url": r[0], "title": r[1]} for r in rows]


async def get_users_subscribed_to_dm(manga_url: str) -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM user_dm_subscriptions WHERE manga_url=?", (manga_url,)
        ) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


async def get_seen_chapters_for_manga(manga_title: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT chapter_url, chapter_num FROM seen_chapters
               WHERE manga_title = ?
               ORDER BY seen_at DESC""",
            (manga_title,),
        ) as cur:
            rows = await cur.fetchall()
            return [{"url": r[0], "num": r[1]} for r in rows]


async def get_dm_user_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM user_dm_subscriptions"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_all_dm_subscriptions() -> list[tuple[int, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT user_id, manga_url FROM user_dm_subscriptions"
        ) as cur:
            return await cur.fetchall()


# ── News (anime X/Twitter feed) ───────────────────────────────────────────────

async def set_news_channel(guild_id: int, channel_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO news_channels (guild_id, channel_id) VALUES (?,?)",
            (guild_id, channel_id),
        )
        await db.commit()


async def get_all_news_channels() -> list[tuple[int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT guild_id, channel_id FROM news_channels") as cur:
            return await cur.fetchall()


async def is_tweet_seen(tweet_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM seen_tweets WHERE tweet_id = ?", (tweet_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def mark_tweet_seen(tweet_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_tweets (tweet_id) VALUES (?)", (tweet_id,)
        )
        await db.commit()


# ── Anime notifications ───────────────────────────────────────────────────────

async def set_anime_notify_channel(guild_id: int, channel_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO anime_notify_channels (guild_id, channel_id) VALUES (?,?)",
            (guild_id, channel_id),
        )
        await db.commit()


async def get_all_anime_notify_channels() -> list[tuple[int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT guild_id, channel_id FROM anime_notify_channels") as cur:
            return await cur.fetchall()


async def is_episode_seen(media_id: int, episode: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM seen_episodes WHERE media_id=? AND episode=?",
            (media_id, episode),
        ) as cur:
            return await cur.fetchone() is not None


async def mark_episode_seen(media_id: int, episode: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_episodes (media_id, episode) VALUES (?,?)",
            (media_id, episode),
        )
        await db.commit()


# ── Developer IDs (persistent) ────────────────────────────────────────────────

async def get_db_developer_ids() -> set[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM developer_ids") as cur:
            rows = await cur.fetchall()
            return {r[0] for r in rows}


async def add_db_developer_id(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO developer_ids (user_id) VALUES (?)", (user_id,)
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_db_developer_id(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM developer_ids WHERE user_id = ?", (user_id,)
        )
        await db.commit()
        return cur.rowcount > 0


# ── Maintenance ───────────────────────────────────────────────────────────────

async def purge_old_seen_chapters(keep_days: int = 90) -> int:
    """Delete seen_chapters entries older than keep_days. Returns number deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM seen_chapters WHERE seen_at < datetime('now', ?)",
            (f"-{keep_days} days",),
        )
        await db.commit()
        deleted = cur.rowcount
    if deleted:
        log.info("[db] purged %d old seen_chapters (>%d days)", deleted, keep_days)
    return deleted


async def purge_old_seen_tweets(keep_days: int = 30) -> int:
    """Delete seen_tweets entries older than keep_days. Returns number deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM seen_tweets WHERE seen_at < datetime('now', ?)",
            (f"-{keep_days} days",),
        )
        await db.commit()
        deleted = cur.rowcount
    if deleted:
        log.info("[db] purged %d old seen_tweets (>%d days)", deleted, keep_days)
    return deleted


async def purge_old_seen_episodes(keep_days: int = 30) -> int:
    """Delete seen_episodes entries older than keep_days. Returns number deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM seen_episodes WHERE seen_at < datetime('now', ?)",
            (f"-{keep_days} days",),
        )
        await db.commit()
        deleted = cur.rowcount
    if deleted:
        log.info("[db] purged %d old seen_episodes (>%d days)", deleted, keep_days)
    return deleted


# ── JSON backup ───────────────────────────────────────────────────────────────

async def export_to_json() -> str:
    """Export all bot data to JSON_PATH and return the path."""
    data: dict = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "guild_channels": [],
        "news_channels": [],
        "subscriptions": [],
        "user_dm_subscriptions": [],
        "seen_chapters": [],
        "seen_tweets": [],
        "series_type_cache": [],
    }

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT guild_id, channel_id FROM guild_channels") as cur:
            data["guild_channels"] = [dict(r) for r in await cur.fetchall()]

        async with db.execute("SELECT guild_id, channel_id FROM news_channels") as cur:
            data["news_channels"] = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            "SELECT guild_id, manga_url, manga_title FROM subscriptions"
        ) as cur:
            data["subscriptions"] = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            "SELECT user_id, manga_url, manga_title FROM user_dm_subscriptions"
        ) as cur:
            data["user_dm_subscriptions"] = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            "SELECT chapter_url, manga_title, chapter_num, seen_at FROM seen_chapters ORDER BY seen_at DESC"
        ) as cur:
            data["seen_chapters"] = [dict(r) for r in await cur.fetchall()]

        async with db.execute("SELECT tweet_id, seen_at FROM seen_tweets") as cur:
            data["seen_tweets"] = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            "SELECT manga_url, series_type, cached_at FROM series_type_cache"
        ) as cur:
            data["series_type_cache"] = [dict(r) for r in await cur.fetchall()]

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    log.info("[db] JSON backup saved → %s (%d subs, %d chapters seen)",
             JSON_PATH,
             len(data["user_dm_subscriptions"]),
             len(data["seen_chapters"]))
    return JSON_PATH
