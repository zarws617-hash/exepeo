"""AniList airing schedule fetcher for anime episode notifications."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

ANILIST_URL = "https://graphql.anilist.co"

_SCHEDULE_QUERY = """
query ($from: Int, $to: Int, $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    airingSchedules(
      airingAt_greater: $from
      airingAt_lesser:  $to
      sort: TIME
    ) {
      episode
      airingAt
      media {
        id
        title { romaji english }
        coverImage { medium }
        siteUrl
      }
    }
  }
}
"""


@dataclass
class AiringEpisode:
    media_id:   int
    title:      str
    episode:    int
    airing_at:  int
    cover_url:  str
    site_url:   str

    @property
    def airing_dt(self) -> datetime:
        return datetime.fromtimestamp(self.airing_at, tz=timezone.utc)


def fetch_airing_today() -> list[AiringEpisode]:
    """Return episodes airing in the next 24 hours (UTC)."""
    now  = int(time.time())
    end  = now + 86400
    return _fetch_range(now, end)


def fetch_airing_week() -> list[AiringEpisode]:
    """Return episodes airing in the next 7 days (UTC)."""
    now = int(time.time())
    end = now + 7 * 86400
    return _fetch_range(now, end)


def _fetch_range(from_ts: int, to_ts: int) -> list[AiringEpisode]:
    import time as _time
    episodes: list[AiringEpisode] = []
    page = 1
    MAX_PAGES = 10  # safety cap to avoid infinite loops
    while page <= MAX_PAGES:
        try:
            resp = requests.post(
                ANILIST_URL,
                json={
                    "query": _SCHEDULE_QUERY,
                    "variables": {"from": from_ts, "to": to_ts, "page": page},
                },
                timeout=15,
            )
            # Respect AniList rate limit (90 req/min)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                log.warning("[anime] rate-limited, sleeping %ds", retry_after)
                _time.sleep(min(retry_after, 60))
                continue
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.error("[anime] AniList fetch failed (page %d): %s", page, exc)
            break

        page_data = (data.get("data") or {}).get("Page") or {}
        for item in page_data.get("airingSchedules") or []:
            media = item.get("media") or {}
            title_obj = media.get("title") or {}
            title = title_obj.get("english") or title_obj.get("romaji") or "?"
            episodes.append(AiringEpisode(
                media_id  = media.get("id", 0),
                title     = title,
                episode   = item.get("episode", 0),
                airing_at = item.get("airingAt", 0),
                cover_url = (media.get("coverImage") or {}).get("medium", ""),
                site_url  = media.get("siteUrl", ""),
            ))

        if not page_data.get("pageInfo", {}).get("hasNextPage"):
            break
        page += 1
        _time.sleep(0.5)  # polite delay between pages

    log.info("[anime] %d episodes fetched (%d→%d)", len(episodes), from_ts, to_ts)
    return episodes
