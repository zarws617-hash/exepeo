"""Anime Slayer (animeslayer.to) scraper for the Discord bot.

Public API
----------
search_anime_slayer(query)            -> list[AnimeSlayerResult]
get_episodes_slayer(slug)             -> list[AnimeSlayerEpisode]
find_episode_slayer(title, ep_num)    -> AnimeSlayerEpisode | None
get_stream_url_slayer(watch_url)      -> dict[str, str] | None
    Returns quality-labelled direct URLs e.g. {"1080p": "https://…", "720p": "…"}
get_episode_meta_slayer(watch_url)    -> tuple[str, str]
    Returns (title, thumbnail_url) scraped from the episode page OG tags.
"""

from __future__ import annotations

import base64
import logging
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import cloudscraper
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL   = "https://animeslayer.to"
SEARCH_API = f"{BASE_URL}/api/search"

_HREF_XOR_KEY   = "asxwqa147"
_STREAM_XOR_KEY = "AQWXZSCED@@POIUYTRR159"
_FLARE_URL      = "https://patrimoines-en-mouvement.org/lib/flare/v3.php"


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_session() -> cloudscraper.CloudScraper:
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    s.headers.update({"Accept-Language": "ar,en;q=0.9", "Referer": BASE_URL})
    return s


def _href_xor(encoded: str, key: str = _HREF_XOR_KEY) -> str:
    """Decode an obfuscated episode href (base64 + XOR)."""
    try:
        decoded = base64.b64decode(encoded).decode("latin-1")
        return "".join(
            chr(ord(ch) ^ ord(key[i % len(key)]))
            for i, ch in enumerate(decoded)
        )
    except Exception:
        return ""


def _stream_xor(data: str, key: str = _STREAM_XOR_KEY) -> str:
    """Decrypt a stream payload (base64 + XOR, same algo as hrefXor but different key)."""
    try:
        padded  = data + "=" * ((4 - len(data) % 4) % 4)
        decoded = base64.b64decode(padded).decode("latin-1")
        return "".join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(decoded))
    except Exception as exc:
        log.debug("[animeslayer] _stream_xor failed: %s", exc)
        return ""


# ── data classes ─────────────────────────────────────────────────────────────

@dataclass
class AnimeSlayerResult:
    title:    str
    slug:     str          # e.g. "naruto-shippuuden-movie-1-cae"
    url:      str          # full title page URL
    image:    str
    kind:     str          # مسلسل / فيلم / أونا …
    status:   str
    episodes: Optional[int]


@dataclass
class AnimeSlayerEpisode:
    number:    int
    title:     str
    watch_url: str          # https://animeslayer.to/e/<slug>#<hash>
    thumb:     str
    is_batch:  bool = field(default=False)   # True when this entry covers multiple episodes


# ── title / episode helpers ───────────────────────────────────────────────────

def _normalize_title(t: str) -> str:
    """Lower-case, strip diacritics and non-alphanumeric chars for comparison."""
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if c.isalnum() or c.isspace())
    return " ".join(t.split())


def _title_similarity(a: str, b: str) -> float:
    """
    Very simple word-overlap similarity in [0, 1].
    Returns 1.0 for identical titles, 0.0 for no shared words.
    We don't need full fuzzy-matching — just enough to avoid obvious mismatches
    (e.g. matching 'naruto' when searching for 'one piece').
    """
    words_a = set(_normalize_title(a).split())
    words_b = set(_normalize_title(b).split())
    if not words_a or not words_b:
        return 0.0
    shared = words_a & words_b
    return len(shared) / max(len(words_a), len(words_b))


# Regex to detect batch/range episode titles like "الحلقات 1-13" or "Episodes 01 to 26".
# Uses an alternation group for the separator so "to" is matched as a token,
# not as individual characters inside a character class.
_BATCH_TITLE_RE = re.compile(
    r"(\d+)\s*(?:-|–|—|to|إلى)\s*(\d+)",
    re.IGNORECASE,
)


def _is_batch_episode(title: str, ep_num: int) -> bool:  # noqa: ARG001
    """
    Return True when the episode entry represents a range of episodes
    (e.g. title "الحلقات 1-13" or "Episodes 01 to 26") rather than a single episode.
    """
    m = _BATCH_TITLE_RE.search(title)
    if not m:
        return False
    lo, hi = int(m.group(1)), int(m.group(2))
    return hi > lo  # any detected ascending range → batch


def _batch_contains(title: str, ep_num: int) -> bool:
    """
    Return True when *title* contains a range A-B and ep_num falls within [A, B].
    Used to surface batch entries as a last resort when the exact episode is missing.
    """
    m = _BATCH_TITLE_RE.search(title)
    if not m:
        return False
    lo, hi = int(m.group(1)), int(m.group(2))
    return hi > lo and lo <= ep_num <= hi


# ── stream extraction helpers ────────────────────────────────────────────────

def _get_flare_urls(s: cloudscraper.CloudScraper) -> tuple[str, str]:
    """Fetch apiFirst and apiSec from the flare endpoint."""
    r = s.get(_FLARE_URL, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data["first"], data["sec"]


def _extract_san_mwsem(s: cloudscraper.CloudScraper, watch_url: str) -> tuple[str, str]:
    """Fetch the episode watch page and extract san + mwsem JS variables."""
    r = s.get(watch_url, timeout=15)
    r.raise_for_status()
    san_m   = re.search(r'const san\s*=\s*"([^"]+)"', r.text)
    mwsem_m = re.search(r'const mwsem\s*=\s*"([^"]+)"', r.text)
    san   = san_m.group(1)   if san_m   else ""
    mwsem = mwsem_m.group(1) if mwsem_m else ""
    return san, mwsem


def _parse_direct_urls(player_page: str) -> dict[str, str]:
    """
    Extract quality-keyed direct video URLs from the player page HTML.
    Returns e.g. {"360p": "https://…", "1080p": "https://…"}.

    The video.js-based player pages embed sources as a JS array of objects,
    e.g. ``{ src: '...mp4', type: 'video/mp4', label: '1080p', res: '1080' }``
    — but the field order is not guaranteed (some templates put ``label``
    before ``src``, others after). Scanning each ``{...}`` object as its own
    unit and searching for ``src``/``label`` independently inside it handles
    both orderings, unlike a single sequential regex which only matched one
    direction and silently returned nothing for the other.

    Some CDN hosts (e.g. bkvideo.online) serve video via ``/download?s=…``
    URLs without a file extension. These also appear inside the same video.js
    source array with a ``type: 'video/mp4'`` field — so we accept any
    ``src:`` URL when the surrounding object declares a video MIME type.
    """
    urls: dict[str, str] = {}
    for block in re.findall(r"\{[^{}]*\}", player_page, re.DOTALL):
        # Primary: URL with .mp4/.m3u8 extension
        src_m = re.search(
            r"""src\s*:\s*['"](https?://[^'"]+\.(?:mp4|m3u8)[^'"]*)['"]""",
            block,
            re.IGNORECASE,
        )
        # Secondary: URL without extension but accompanied by a video MIME type
        if not src_m:
            has_video_type = bool(re.search(
                r"""type\s*:\s*['"]video/""", block, re.IGNORECASE
            ))
            if has_video_type:
                src_m = re.search(
                    r"""src\s*:\s*['"](https?://[^'"]{20,})['"]""",
                    block,
                    re.IGNORECASE,
                )

        if not src_m:
            continue
        label_m = re.search(r"""label\s*:\s*['"](\d{3,4})p?['"]""", block, re.IGNORECASE)
        quality = f"{label_m.group(1)}p" if label_m else "default"
        urls.setdefault(quality, src_m.group(1).rstrip("',"))

    if urls:
        return urls

    # Fallback for older/simpler pages: quality label then URL on one line.
    for m in re.finditer(
        r'(\d{3,4}p)[^<>]*?["\']?(https?://[^\s"\'<>]+\.(?:mp4|m3u8)[^\s"\'<>]*)',
        player_page,
        re.IGNORECASE,
    ):
        quality, url = m.group(1), m.group(2).rstrip("',")
        if quality not in urls:
            urls[quality] = url

    # Last resort: any src: '…mp4/m3u8' without quality label
    if not urls:
        for m in re.finditer(
            r"src\s*:\s*['\"]?(https?://[^\s\"'<>]+\.(?:mp4|m3u8)[^\s\"'<>]*)",
            player_page,
            re.IGNORECASE,
        ):
            urls.setdefault("default", m.group(1).rstrip("',"))
            break

    return urls


def _parse_server_iframes(player_page: str, base_url: str = BASE_URL) -> list[str]:
    """
    Extract alternative *server* iframe URLs from a player page.

    Only matches URLs that are clearly video-server player pages
    (p_wit.php, p_rift.php, p_sl.php, …) on the same CDN host as the
    original player URL.  This deliberately excludes episode-navigation
    links (which contain "watch" / "stream" but point to episode pages on
    animeslayer.to) to avoid fetching content from the wrong episode.

    Relative URLs are resolved against *base_url*.
    """
    # Derive the CDN host from base_url so we only follow same-origin iframes
    parsed_base = urllib.parse.urlparse(base_url)
    cdn_host = parsed_base.netloc  # e.g. "www.patrimoines-en-mouvement.org"

    found: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'(?:src|data-src|data-url)\s*=\s*["\']([^"\']+)["\']',
        player_page,
        re.IGNORECASE,
    ):
        raw = m.group(1).strip()
        if not raw:
            continue
        url = urllib.parse.urljoin(base_url, raw)
        parsed = urllib.parse.urlparse(url)

        # Must be on the same CDN host (not animeslayer.to episode pages)
        if cdn_host and parsed.netloc != cdn_host:
            continue
        # Skip static JS/CSS bundle assets (video.js library chunks etc.) —
        # these also live under /lib/player/ and were previously matched as
        # if they were alternative video servers, wasting the only real
        # extraction attempt on files that can never contain a stream URL.
        if parsed.path.lower().endswith((".js", ".mjs", ".css", ".map", ".json")):
            continue
        # Must look like a server-specific player script, not a general page
        if not re.search(r'p_wit|p_rift|p_sl|p_blk|/player/', parsed.path, re.IGNORECASE):
            continue
        if "vfail" in url.lower():
            continue
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found


# ── public functions ──────────────────────────────────────────────────────────

# bool values to try for the apiSec call in order.
# "no" = default/first server; "yes" = alternative server.
# Numeric strings were removed — the backend may interpret them as episode
# numbers rather than server selectors, which would return the wrong episode.
_BOOL_CANDIDATES = ["no", "yes"]


def _is_playable_video_url(s: cloudscraper.CloudScraper, url: str) -> bool:
    """
    Verify *url* is not a dead-link landing page masquerading as a video.

    Some file hosts (e.g. MediaFire) respond with a 200 OK HTML page when a
    file has been removed, instead of a proper error status.  We detect that
    specific case — HTTP 200 with a text/html Content-Type — and reject it.

    Any other outcome (video bytes, CDN headers, 206 partial, 403, 5xx, or
    a connection error) is treated as *probably valid* and returned as True.
    This is intentionally lenient: CDNs that block Range requests or require
    specific Referer headers would produce false negatives under a stricter
    check, causing valid URLs to be silently discarded.
    """
    try:
        r = s.get(
            url,
            headers={"Referer": BASE_URL, "Range": "bytes=0-2048"},
            timeout=10,
            stream=True,
        )
        status = r.status_code
        content_type = r.headers.get("Content-Type", "")
        r.close()
        # Only reject a confirmed "200 OK + HTML" response — that is the
        # MediaFire-style dead-link pattern.  Everything else passes.
        if status == 200 and ("html" in content_type.lower() or "text/" in content_type.lower()):
            log.info("[animeslayer] dead HTML landing page at %s", url[:80])
            return False
        return True
    except Exception as exc:
        # Network / proxy errors are treated as "might be valid" — let ffmpeg
        # find out; we do not want to suppress a working URL due to a transient
        # probe failure.
        log.debug("[animeslayer] video URL probe error (treating as valid) %s: %s", url[:80], exc)
        return True


class _PlayerFetchResult:
    """Structured result from ``_fetch_player_urls``.

    Attributes
    ----------
    urls:
        Quality-keyed dict of *playable* stream URLs (empty on failure).
    transport_error:
        True when the player page itself could not be fetched (network / proxy
        error).  These player URLs should NOT be marked exhausted.
    had_raw_urls:
        True when the page was fetched and raw URLs were extracted, but all of
        them were rejected by ``_is_playable_video_url``.  These player URLs
        should also NOT be marked exhausted — the player URL is genuinely valid
        (it returned something), only the specific CDN link was unplayable.
    """

    __slots__ = ("urls", "transport_error", "had_raw_urls")

    def __init__(
        self,
        urls: "dict[str, str]",
        transport_error: bool = False,
        had_raw_urls: bool = False,
    ) -> None:
        self.urls = urls
        self.transport_error = transport_error
        self.had_raw_urls = had_raw_urls

    def __bool__(self) -> bool:  # truthy when usable URLs were found
        return bool(self.urls)


def _fetch_player_urls(
    s: cloudscraper.CloudScraper,
    player_url: str,
) -> _PlayerFetchResult:
    """
    Fetch *player_url* and extract direct stream URLs.

    Returns a :class:`_PlayerFetchResult` with:
    - ``urls`` — quality-keyed dict of playable stream URLs (empty on failure)
    - ``transport_error`` — True if the page fetch itself failed (network /
      proxy error), False if the fetch succeeded but no stream URLs were found.

    Callers use ``transport_error`` to decide whether to mark the URL as
    *exhausted* (deterministic empty → mark) or leave it eligible for retry
    (transport failure → do not mark).
    """
    if "vfail" in player_url.lower():
        log.info("[animeslayer] skipping vfail player URL: %s", player_url[:120])
        return _PlayerFetchResult({})

    try:
        r = s.get(player_url, headers={"Referer": BASE_URL}, timeout=12)
        r.raise_for_status()
    except Exception as exc:
        log.warning("[animeslayer] player fetch failed (%s): %s", player_url[:80], exc)
        return _PlayerFetchResult({}, transport_error=True)

    urls = _parse_direct_urls(r.text)
    if urls:
        log.info("[animeslayer] stream URLs from %s: %s", player_url[:80], list(urls.keys()))
    else:
        # No direct URLs — look for nested server iframes and try each one
        alt_iframes = _parse_server_iframes(r.text, base_url=player_url)
        if alt_iframes:
            log.info(
                "[animeslayer] no direct URLs in player page; trying %d nested iframe(s)",
                len(alt_iframes),
            )
        for iframe_url in alt_iframes:
            try:
                ri = s.get(iframe_url, headers={"Referer": player_url}, timeout=12)
                ri.raise_for_status()
                urls = _parse_direct_urls(ri.text)
                if urls:
                    log.info(
                        "[animeslayer] stream URLs from nested iframe %s: %s",
                        iframe_url[:80], list(urls.keys()),
                    )
                    break
            except Exception as exc:
                log.warning("[animeslayer] iframe fetch failed (%s): %s", iframe_url[:80], exc)

    if not urls:
        return _PlayerFetchResult({})

    valid_urls = {q: u for q, u in urls.items() if _is_playable_video_url(s, u)}
    dead = urls.keys() - valid_urls.keys()
    if dead:
        log.warning(
            "[animeslayer] discarded %d dead/expired link(s) from %s: %s",
            len(dead), player_url[:80], list(dead),
        )
    # had_raw_urls=True signals that the player page DID return URLs — it is a
    # valid page — but every CDN link was filtered out.  The caller must not
    # mark this player URL as exhausted.
    return _PlayerFetchResult(valid_urls, had_raw_urls=bool(dead and not valid_urls))


def _ytdlp_extract_qualities(info: "dict") -> "dict[str, str]":
    """
    Given a yt-dlp info dict (possibly a playlist/entries wrapper), return a
    quality-keyed URL dict of all usable video formats found.

    Handles three shapes of extractor output:
    - Single video: info has a ``formats`` list directly.
    - Playlist / entries wrapper: info has an ``entries`` list whose first item
      is the real video dict (common for embed-page extractors).
    - Bare URL: info has a direct ``url`` field but no ``formats``.
    """
    # Unwrap playlist/entries if present
    if not info:
        return {}
    if "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return {}
        info = entries[0]

    fmts: list[dict] = info.get("formats") or []

    # Bare-URL extractor (no formats list)
    if not fmts and info.get("url"):
        height = info.get("height") or 0
        quality = f"{height}p" if height else "default"
        return {quality: info["url"]}

    # Build quality → URL mapping; prefer video-bearing formats, highest res first.
    result: dict[str, str] = {}
    video_fmts = [f for f in fmts if f.get("url") and f.get("vcodec") not in (None, "none")]
    if not video_fmts:
        # Fall back to any format with a URL (audio-only / unknown)
        video_fmts = [f for f in fmts if f.get("url")]

    for fmt in video_fmts:
        url = fmt.get("url", "")
        if not url:
            continue
        height = fmt.get("height") or 0
        quality = f"{height}p" if height else fmt.get("format_id", "default")
        result.setdefault(quality, url)  # keep first/highest seen per quality label

    return result


def _get_stream_urls_ytdlp(
    s: cloudscraper.CloudScraper,
    watch_url: str,
    base_params: dict,
    api_sec: str,
) -> dict[str, str]:
    """
    Last-resort fallback after all API-based attempts are exhausted.

    Strategy (in order):
    1. Make a few more fresh apiSec calls — gather player pages not yet seen
       and first try our normal regex parser on them (fast path).
    2. For any bkvideo.online / cdn.bkvideo.online URLs found in player pages,
       run yt-dlp to resolve them to stable CDN links with quality labels.
    3. Try yt-dlp directly on the episode watch URL itself.

    Returns a quality-keyed URL dict on success, empty dict on failure.
    Budget: capped to avoid stalling Discord interactions.
    """
    try:
        import yt_dlp  # noqa: PLC0415
    except ImportError:
        log.warning("[animeslayer] yt-dlp not installed — fallback unavailable")
        return {}

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 12,
        "http_headers": {"Referer": BASE_URL},
    }

    seen: set[str] = set()
    _FALLBACK_BUDGET = 45.0  # hard cap for the entire fallback phase
    fallback_deadline = time.monotonic() + _FALLBACK_BUDGET

    # ── 1 & 2: Fresh API calls → player page → parser / yt-dlp ──────────────
    for bool_val in ["no", "yes"]:
        for _ in range(3):
            if time.monotonic() >= fallback_deadline:
                break
            try:
                params = urllib.parse.urlencode({**base_params, "bool": bool_val})
                r3 = s.post(
                    api_sec,
                    data=params,
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "Referer": BASE_URL},
                    timeout=10,
                )
                r3.raise_for_status()
                j3 = r3.json()

                player_url = _stream_xor(j3.get("data", ""))
                if not player_url or not player_url.startswith("http"):
                    continue
                if "mega.nz" in player_url or "mega.co.nz" in player_url:
                    continue
                if "vfail" in player_url.lower():
                    continue
                if player_url in seen:
                    continue
                seen.add(player_url)

                try:
                    rp = s.get(player_url, headers={"Referer": BASE_URL}, timeout=12)
                    rp.raise_for_status()
                except Exception:
                    continue

                # Fast path: regex parser (no extra network calls)
                direct = _parse_direct_urls(rp.text)
                if direct:
                    valid = {q: u for q, u in direct.items() if _is_playable_video_url(s, u)}
                    if valid:
                        log.info("[animeslayer] fallback: parser succeeded on retry: %s", list(valid.keys()))
                        return valid

                # Slow path: find CDN URLs in the page and run yt-dlp on each
                cdn_candidates = re.findall(
                    r'https?://(?:[^/\s"\'<>]*bkvideo\.online|cdn\.bkvideo\.online)'
                    r'/[^\s"\'<>]{10,}',
                    rp.text,
                    re.IGNORECASE,
                )
                for cdn_url in cdn_candidates[:3]:
                    if time.monotonic() >= fallback_deadline:
                        break
                    cdn_url = cdn_url.rstrip("',;")
                    try:
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(cdn_url, download=False)
                        result = _ytdlp_extract_qualities(info)
                        if result:
                            log.info("[animeslayer] yt-dlp resolved CDN URL, qualities: %s", list(result.keys()))
                            return result
                    except Exception as exc:
                        log.debug("[animeslayer] yt-dlp on CDN URL %s failed: %s", cdn_url[:60], exc)

            except Exception as exc:
                log.debug("[animeslayer] yt-dlp fallback API call failed: %s", exc)
            time.sleep(0.5)

    # ── 3: yt-dlp directly on the watch URL ──────────────────────────────────
    if time.monotonic() < fallback_deadline:
        try:
            log.info("[animeslayer] trying yt-dlp directly on watch URL: %s", watch_url[:100])
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(watch_url, download=False)
            result = _ytdlp_extract_qualities(info)
            if result:
                log.info("[animeslayer] yt-dlp watch URL extraction succeeded: %s", list(result.keys()))
                return result
        except Exception as exc:
            log.info("[animeslayer] yt-dlp on watch URL failed: %s", exc)

    return {}


def get_stream_url_slayer(watch_url: str) -> dict[str, str]:
    """
    Given an Anime Slayer episode watch URL (https://animeslayer.to/e/<slug>#<hash>),
    resolve the direct video stream URLs.

    Tries every known server via the ``bool`` parameter of the apiSec call.
    Falls back to nested server iframes when the primary player page yields no URLs
    (e.g. when the server returns a vfail.php page for missing episodes).

    Returns a dict keyed by quality label e.g. {"1080p": "https://…mp4", "720p": "…"}.
    Returns empty dict on failure.
    """
    # Parse slug and frag from watch_url
    m = re.search(r"/e/([^#?]+)(?:#(.+))?", watch_url)
    if not m:
        log.warning("[animeslayer] cannot parse watch_url: %r", watch_url)
        return {}
    slug = m.group(1).rstrip("/")
    frag = m.group(2) or ""

    # ep = last token of slug (after last '-')
    ep = slug.rsplit("-", 1)[-1]

    s = _make_session()

    try:
        # 1. Get apiFirst / apiSec
        api_first, api_sec = _get_flare_urls(s)

        # 2. Get san / mwsem from episode page (needed for apiSec call)
        san, mwsem = _extract_san_mwsem(s, watch_url)

        # 3. POST to apiFirst → encrypted a/b/c/d
        r2 = s.post(
            api_first,
            data=f"pe={urllib.parse.quote(ep)}&hash={urllib.parse.quote(frag)}",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Referer": BASE_URL},
            timeout=15,
        )
        r2.raise_for_status()
        j2 = r2.json()
        if j2.get("status") != "ok":
            log.warning("[animeslayer] apiFirst error: %s", j2)
            return {}

    except Exception as exc:
        log.warning("[animeslayer] get_stream_url_slayer setup failed: %s", exc)
        return {}

    # 4–6. Try each bool candidate until we get real stream URLs
    required_keys = {"a", "b", "c", "d"}
    if not required_keys.issubset(j2):
        log.warning("[animeslayer] apiFirst response missing keys: %s", j2)
        return {}
    base_params = {
        "keyn": j2["d"], "name": san,  "pe": j2["c"],
        "id":   j2["a"], "info": j2["b"], "san": san, "mwsem": mwsem,
    }

    # The site returns a *different, randomly-picked* player template on
    # each apiSec call for the same bool value.  Known templates:
    #   p_blk.php — video.js page with bkvideo.online download URLs (quality-labelled)
    #   p_wit.php — video.js page with MediaFire CDN download URLs
    #   mega.nz   — JS-only embed; no extractable URL without a browser runtime
    #   vfail.php — dead/unavailable server (skipped explicitly)
    # Re-POSTing with the same bool value re-rolls which template comes back,
    # so retrying enough times before giving up recovers from an unlucky streak
    # (e.g. repeated mega.nz draws) instead of failing outright.
    _ATTEMPTS_PER_BOOL = 8  # 16 total attempts across both bool values
    # Short backoff between attempts lets the upstream proxy recover from
    # transient 403/5xx errors without wasting too much wall time.
    _RETRY_DELAY_SECONDS = 1.0
    # Short delay when we fast-skip (mega.nz / duplicate URL) — just enough
    # to let the server register a new random pick on the next POST.
    _SKIP_DELAY_SECONDS = 0.4
    # Hard wall-clock budget for the main API loop.  Prevents a flood of slow
    # requests from stalling a Discord interaction for several minutes.
    _MAX_ELAPSED_SECONDS = 90.0

    # Track player URLs whose pages were *successfully fetched* and yielded no
    # usable stream URLs (deterministic empty result).  We skip these on later
    # attempts to avoid re-fetching the same dead page.
    # We do NOT add URLs that failed with a transport/network exception — those
    # may succeed on a retry when the proxy recovers.
    exhausted_player_urls: set[str] = set()

    deadline = time.monotonic() + _MAX_ELAPSED_SECONDS

    for bool_val in _BOOL_CANDIDATES:
        for attempt in range(1, _ATTEMPTS_PER_BOOL + 1):
            if time.monotonic() >= deadline:
                log.warning(
                    "[animeslayer] time budget %.0fs reached after %d/%d/%d attempts — stopping",
                    _MAX_ELAPSED_SECONDS, attempt, _ATTEMPTS_PER_BOOL, len(_BOOL_CANDIDATES),
                )
                break

            try:
                params = urllib.parse.urlencode({**base_params, "bool": bool_val})
                r3 = s.post(
                    api_sec,
                    data=params,
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "Referer": BASE_URL},
                    timeout=10,
                )
                r3.raise_for_status()
                j3 = r3.json()

                player_url = _stream_xor(j3.get("data", ""))
                if not player_url or not player_url.startswith("http"):
                    log.info("[animeslayer] bool=%r attempt %d/%d: could not decrypt player URL", bool_val, attempt, _ATTEMPTS_PER_BOOL)
                    time.sleep(_RETRY_DELAY_SECONDS)
                    continue

                # Fast-skip Mega.nz embeds — they are JS-only players with no
                # extractable direct URL without a full browser runtime.
                if "mega.nz" in player_url or "mega.co.nz" in player_url:
                    log.info(
                        "[animeslayer] bool=%r attempt %d/%d: mega.nz embed — re-rolling",
                        bool_val, attempt, _ATTEMPTS_PER_BOOL,
                    )
                    time.sleep(_SKIP_DELAY_SECONDS)
                    continue

                # Skip player URLs already confirmed as dead/empty (successful
                # fetch that yielded no usable stream URLs).  Do NOT skip URLs
                # that failed with a transport error — those may work on retry.
                if player_url in exhausted_player_urls:
                    log.info(
                        "[animeslayer] bool=%r attempt %d/%d: known-empty player URL — re-rolling",
                        bool_val, attempt, _ATTEMPTS_PER_BOOL,
                    )
                    time.sleep(_SKIP_DELAY_SECONDS)
                    continue

                log.info(
                    "[animeslayer] bool=%r attempt %d/%d → player URL: %s",
                    bool_val, attempt, _ATTEMPTS_PER_BOOL, player_url[:120],
                )

                fetch_result = _fetch_player_urls(s, player_url)
                if fetch_result.urls:
                    return fetch_result.urls

                # Only mark as exhausted when the fetch returned nothing at all
                # (no raw URLs found, no transport error).  Two cases must NOT
                # be marked exhausted:
                # • transport_error — transient network failure; retry is fine.
                # • had_raw_urls   — player page is valid but CDN link was dead;
                #   the server may return a fresh CDN link on the next API call.
                if not fetch_result.transport_error and not fetch_result.had_raw_urls:
                    exhausted_player_urls.add(player_url)
                log.info(
                    "[animeslayer] bool=%r attempt %d/%d yielded no stream URLs, retrying",
                    bool_val, attempt, _ATTEMPTS_PER_BOOL,
                )
                time.sleep(_RETRY_DELAY_SECONDS)

            except Exception as exc:
                # Unexpected exception — do NOT mark the URL as exhausted.
                log.warning("[animeslayer] bool=%r attempt %d/%d failed: %s", bool_val, attempt, _ATTEMPTS_PER_BOOL, exc)
                time.sleep(_RETRY_DELAY_SECONDS)
        else:
            continue
        break  # time budget hit — exit both loops

    log.warning("[animeslayer] all API servers exhausted for %s — trying yt-dlp fallback", watch_url)

    # ── yt-dlp fallback ───────────────────────────────────────────────────────
    # As a last resort, try yt-dlp on the player pages we *did* successfully
    # fetch but couldn't parse (e.g. bkvideo URLs with non-standard containers).
    # Also try it on any bkvideo.online URLs extracted but not yet resolved.
    ytdlp_result = _get_stream_urls_ytdlp(s, watch_url, base_params, api_sec)
    if ytdlp_result:
        log.info("[animeslayer] yt-dlp fallback succeeded: %s", list(ytdlp_result.keys()))
        return ytdlp_result

    log.warning("[animeslayer] all extraction methods exhausted for %s", watch_url)
    return {}


def get_episode_meta_slayer(watch_url: str) -> tuple[str, str]:
    """
    Fetch the episode watch page and return (title, thumbnail_url) from its OG tags.
    Returns ('', '') on any failure — callers should degrade gracefully.
    """
    s = _make_session()
    try:
        r = s.get(watch_url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        og_title = soup.find("meta", property="og:title")
        title = (og_title.get("content") or "") if og_title else ""
        if not title:
            tag = soup.find("title")
            title = tag.get_text().split("|")[0].strip() if tag else ""

        og_image = soup.find("meta", property="og:image")
        thumb = (og_image.get("content") or "") if og_image else ""

        return title.strip(), thumb.strip()
    except Exception as exc:
        log.warning("[animeslayer] get_episode_meta_slayer failed for %s: %s", watch_url[:80], exc)
        return "", ""


# Special dash variants (en-dash, em-dash, horizontal bar, minus sign) that the
# site's search endpoint silently chokes on — a query containing one of these
# returns zero results even when a plain-ASCII-hyphen equivalent would match.
_SPECIAL_DASHES_RE = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")


def _sanitize_query(query: str) -> str:
    """Normalize characters that break the Anime Slayer search endpoint."""
    q = _SPECIAL_DASHES_RE.sub(" ", query)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def search_anime_slayer(query: str, limit: int = 8) -> list[AnimeSlayerResult]:
    """Search Anime Slayer by title. Returns up to *limit* results."""
    query = _sanitize_query(query)
    s = _make_session()
    try:
        r = s.get(SEARCH_API, params={"q": query}, timeout=12)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("[animeslayer] search failed for %r: %s", query, exc)
        return []

    results: list[AnimeSlayerResult] = []
    for item in data[:limit]:
        href = item.get("href", "")                         # e.g. /title/naruto-…-cae
        # removeprefix strips the exact string "/title/" (not individual chars)
        slug = href.removeprefix("/title/").strip("/")
        results.append(AnimeSlayerResult(
            title    = item.get("title", ""),
            slug     = slug,
            url      = f"{BASE_URL}{href}",
            image    = item.get("image", ""),
            kind     = item.get("type", ""),
            status   = item.get("status", ""),
            episodes = item.get("episodes"),
        ))
    return results


def get_episodes_slayer(slug: str) -> list[AnimeSlayerEpisode]:
    """Fetch all episodes for a given anime slug from its title page."""
    s = _make_session()
    url = f"{BASE_URL}/title/{slug}"
    try:
        r = s.get(url, timeout=15)
        r.raise_for_status()
    except Exception as exc:
        log.warning("[animeslayer] title page failed for %r: %s", slug, exc)
        return []

    # Extract the `const episodes = [ … ];` block from the page JS
    m = re.search(r"const episodes\s*=\s*\[([^\]]*)\];", r.text, re.DOTALL)
    if not m:
        log.warning("[animeslayer] no episodes block found for slug %r", slug)
        return []

    block = "[" + m.group(1) + "]"

    # Field-level patterns
    _n   = re.compile(r"\bn\s*:\s*(\d+)")
    _t   = re.compile(r'title\s*:\s*"([^"]*)"')
    _h   = re.compile(r'href\s*:\s*"([^"]*)"')
    _th  = re.compile(r'thumb\s*:\s*"([^"]*)"')

    episodes: list[AnimeSlayerEpisode] = []
    # Each episode object is delimited by { … }
    for obj_m in re.finditer(r"\{(.*?)\}", block, re.DOTALL):
        body = obj_m.group(1)
        nm = _n.search(body)
        hm = _h.search(body)
        if not nm or not hm:
            continue
        tm  = _t.search(body)
        thm = _th.search(body)
        decoded_path = _href_xor(hm.group(1))
        watch_url = (
            f"{BASE_URL}{decoded_path}"
            if decoded_path.startswith("/")
            else decoded_path
        )
        title_str = tm.group(1) if tm else ""
        episodes.append(AnimeSlayerEpisode(
            number    = int(nm.group(1)),
            title     = title_str,
            watch_url = watch_url,
            thumb     = thm.group(1) if thm else "",
            is_batch  = _is_batch_episode(title_str, int(nm.group(1))),
        ))

    log.info("[animeslayer] %d episodes found for slug %r", len(episodes), slug)
    return episodes


# Words that flag a candidate as a spin-off / recap / special — we deprioritise
# these so the main series is preferred when both appear in search results.
_SPINOFF_TOKENS = frozenset([
    "recap", "recaps", "ملخص", "ملخصات",
    "special", "specials", "خاص",
    "movie", "film", "فيلم",
    "ova", "ona",
    "compilation", "تجميعة",
])


def _is_spinoff(title: str) -> bool:
    """Return True if the candidate title looks like a recap / special / movie."""
    words = set(_normalize_title(title).split())
    return bool(words & _SPINOFF_TOKENS)


def _search_anime_slayer_with_fallback(anime_title: str, limit: int = 8) -> list[AnimeSlayerResult]:
    """
    Search Anime Slayer, retrying with progressively simplified queries when the
    exact title yields nothing.

    The site's search endpoint is a strict/fragile matcher — long titles with
    subtitles (e.g. "Chainsaw Man Movie: Reze Arc") or extra qualifier words can
    return zero results even though a shorter version of the same title matches
    fine. We try, in order:
      1. The sanitized title as-is.
      2. Only the part before a ':' or '-' separator (drops subtitles).
      3. Progressively shorter word-prefixes of the title (drops trailing words
         one at a time), down to a 2-word minimum so we don't end up searching
         for an overly generic single word.
    Stops at the first query that returns any results.
    """
    query = _sanitize_query(anime_title)
    results = search_anime_slayer(query, limit=limit)
    if results:
        return results

    # Drop everything after a ':' or standalone '-' separator (subtitle/arc name).
    head = re.split(r"\s*[:\-]\s*", query, maxsplit=1)[0].strip()
    if head and head != query:
        results = search_anime_slayer(head, limit=limit)
        if results:
            log.info("[animeslayer] fallback search matched on head %r", head)
            return results

    # Progressively drop trailing words.
    words = query.split()
    for n in range(len(words) - 1, 1, -1):
        prefix = " ".join(words[:n])
        if prefix == head:
            continue  # already tried above
        results = search_anime_slayer(prefix, limit=limit)
        if results:
            log.info("[animeslayer] fallback search matched on prefix %r", prefix)
            return results

    return []


def find_episode_slayer(
    anime_title: str,
    ep_num: int,
    min_similarity: float = 0.25,
) -> Optional[AnimeSlayerEpisode]:
    """
    Search for an anime by title and return the episode matching *ep_num*.

    Strategy:
    1. Search Anime Slayer with the given title (up to 8 candidates), falling
       back to simplified queries if the exact title returns nothing (see
       `_search_anime_slayer_with_fallback`).
    2. Skip candidates whose title has < *min_similarity* word overlap with the
       query — this avoids returning episodes from completely unrelated anime.
    3. Among matching candidates, try non-spinoff entries first (exact series),
       then fall back to spinoffs (recap / special / movie) if nothing else matches.
    4. Within each tier, prefer single (non-batch) episodes over batch/range entries.
    Returns None if not found.
    """
    results = _search_anime_slayer_with_fallback(anime_title, limit=8)
    if not results:
        log.warning("[animeslayer] no search results for %r", anime_title)
        return None

    # Separate candidates into two tiers: main series vs. spin-offs
    main_candidates:    list[AnimeSlayerResult] = []
    spinoff_candidates: list[AnimeSlayerResult] = []

    for candidate in results:
        sim = _title_similarity(anime_title, candidate.title)
        log.info(
            "[animeslayer] candidate %r (slug=%r, sim=%.2f, spinoff=%s)",
            candidate.title, candidate.slug, sim, _is_spinoff(candidate.title),
        )
        if sim < min_similarity:
            log.info(
                "[animeslayer] skipping %r — similarity %.2f below %.2f",
                candidate.title, sim, min_similarity,
            )
            continue
        if _is_spinoff(candidate.title):
            spinoff_candidates.append(candidate)
        else:
            main_candidates.append(candidate)

    def _search_in(candidates: list[AnimeSlayerResult]) -> tuple[
        Optional[AnimeSlayerEpisode], Optional[AnimeSlayerEpisode]
    ]:
        """Return (single_ep, batch_ep) found in these candidates."""
        single: Optional[AnimeSlayerEpisode] = None
        batch:  Optional[AnimeSlayerEpisode] = None
        for candidate in candidates:
            episodes = get_episodes_slayer(candidate.slug)
            if not episodes:
                continue
            for ep in episodes:
                if ep.number == ep_num and not ep.is_batch:
                    log.info(
                        "[animeslayer] ✓ single episode %d in %r",
                        ep_num, candidate.title,
                    )
                    return ep, None   # best possible match — stop immediately
                if ep.is_batch and batch is None:
                    covers = (ep.number == ep_num) or _batch_contains(ep.title, ep_num)
                    if covers:
                        batch = ep
                        log.info(
                            "[animeslayer] batch covers ep %d in %r — keeping as fallback",
                            ep_num, candidate.title,
                        )
            log.info(
                "[animeslayer] episode %d not in %d eps for %r",
                ep_num, len(episodes), candidate.title,
            )
        return single, batch

    # Tier 1: main series candidates
    single, batch = _search_in(main_candidates)
    if single:
        return single          # exact single match in main series — best result

    # Main-tier batch takes precedence over ANY spinoff result
    if batch:
        log.warning("[animeslayer] returning main-series batch ep %d as fallback", ep_num)
        return batch

    # Tier 2: spin-off candidates (recap, special, …) — only if main tier empty
    so_single, so_batch = _search_in(spinoff_candidates)
    if so_single:
        log.warning("[animeslayer] returning spinoff single ep %d as last resort", ep_num)
        return so_single
    if so_batch:
        log.warning("[animeslayer] returning spinoff batch ep %d as last resort", ep_num)
        return so_batch

    log.warning("[animeslayer] episode %d not found for %r", ep_num, anime_title)
    return None
