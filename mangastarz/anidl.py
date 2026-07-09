"""Anime search (AniList GraphQL) + torrent lookup (nyaa.si) for the Discord bot."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "MangaStarzBot/1.0"})

ANILIST_GQL  = "https://graphql.anilist.co"
NYAA_RSS     = "https://nyaa.si/?page=rss"
NYAA_TORRENT = "https://nyaa.si/download/{id}.torrent"

# synonyms field added — AniList stores Arabic titles there for many anime
_SEARCH_QUERY = """
query ($search: String, $page: Int, $per: Int) {
  Page(page: $page, perPage: $per) {
    media(search: $search, type: ANIME, sort: POPULARITY_DESC) {
      id
      title { romaji english native }
      synonyms
      episodes
      coverImage { large }
      siteUrl
      description(asHtml: false)
      format
      status
    }
  }
}
"""

# ── Arabic ↔ English lookup ───────────────────────────────────────────────────
# Covers colloquial Arabic names that AniList synonyms may not index.
# Keys: normalised Arabic (no diacritics, lowercase).  Values: best English search term.
_AR_EN: dict[str, str] = {
    # ──── شونين / أكشن ─────────────────────────────────────────────────────
    "ناروتو": "Naruto",
    "ناروتو شيبودن": "Naruto Shippuden",
    "ناروتو شيبودين": "Naruto Shippuden",
    "بوروتو": "Boruto",
    "البورتو": "Boruto",
    "ون بيس": "One Piece",
    "ون بيز": "One Piece",
    "بليتش": "Bleach",
    "دراغون بول": "Dragon Ball",
    "دراغون بول زد": "Dragon Ball Z",
    "دراغون بول سوبر": "Dragon Ball Super",
    "هجوم العمالقة": "Attack on Titan",
    "هجوم العمالقه": "Attack on Titan",
    "شينغيكي نو كيوجين": "Attack on Titan",
    "قاتل الشياطين": "Demon Slayer",
    "ديمون سلاير": "Demon Slayer",
    "كيمتسو نو يايبا": "Demon Slayer Kimetsu no Yaiba",
    "كيمتسو": "Demon Slayer",
    "جوجوتسو كايسن": "Jujutsu Kaisen",
    "بلو لوك": "Blue Lock",
    "بلاك كلوفر": "Black Clover",
    "البرسيم الاسود": "Black Clover",
    "اكاديمية بطلتي": "My Hero Academia",
    "مدرسة الابطال": "My Hero Academia",
    "ميلي اكاديمي": "My Hero Academia",
    "تشينساو مان": "Chainsaw Man",
    "رجل المنشار": "Chainsaw Man",
    "سورد ارت اونلاين": "Sword Art Online",
    "السيف الاسطوري": "Sword Art Online",
    "هنتر هنتر": "Hunter x Hunter",
    "هانتر هانتر": "Hunter x Hunter",
    "فوليمتال": "Fullmetal Alchemist",
    "فولميتال": "Fullmetal Alchemist Brotherhood",
    "محارب الحديد": "Fullmetal Alchemist Brotherhood",
    "طوكيو غول": "Tokyo Ghoul",
    "طوكيو غوول": "Tokyo Ghoul",
    "كود غياس": "Code Geass",
    "كود جياس": "Code Geass",
    "ليلوش": "Code Geass",
    "اوفرلورد": "Overlord",
    "اوورلورد": "Overlord",
    "ري زيرو": "Re:Zero",
    "ريزيرو": "Re:Zero",
    "الخطايا السبع المميتة": "Seven Deadly Sins",
    "خطايا السبع المميتة": "Seven Deadly Sins",
    "الخطايا السبع": "Seven Deadly Sins",
    "فينلاند ساغا": "Vinland Saga",
    "فينلاند سيغا": "Vinland Saga",
    "دندادان": "Dandadan",
    "كايجو": "Kaiju No. 8",
    "كايجو نمبر": "Kaiju No. 8",
    "موب سايكو": "Mob Psycho 100",
    "موبو سايكو": "Mob Psycho 100",
    "اكامي غا كيل": "Akame ga Kill",
    "اكامي": "Akame ga Kill",
    "دكتور ستون": "Dr. Stone",
    "عالم الحجر": "Dr. Stone",
    "الفحم الحجري": "Dr. Stone",
    "باكي": "Baki",
    "بيرسيرك": "Berserk",
    "برسيرك": "Berserk",
    "جنة الجحيم": "Hell's Paradise Jigokuraku",
    "ظل القوة": "The Eminence in Shadow",
    "شادو غاردن": "The Eminence in Shadow",
    "غورن لاقان": "Gurren Lagann",
    "تينغن توبا": "Gurren Lagann",
    "نوراغامي": "Noragami",
    "ستينز غيت": "Steins;Gate",
    "سبي اكس فاميلي": "Spy x Family",
    "الجاسوس والاسرة": "Spy x Family",
    "الجاسوس x الاسرة": "Spy x Family",
    "وايند بريكر": "Wind Breaker",
    # ──── رياضي ────────────────────────────────────────────────────────────
    "هايكيو": "Haikyuu",
    "الكرة الطائرة": "Haikyuu",
    "كوروكو نو باسكيت": "Kuroko no Basket",
    "كوروكو": "Kuroko no Basket",
    "الكابتن ماجد": "Captain Tsubasa",
    "كابتن ماجد": "Captain Tsubasa",
    "ماجد": "Captain Tsubasa",
    "سلام دنك": "Slam Dunk",
    # ──── فانتازيا / isekai ─────────────────────────────────────────────────
    "دانماشي": "DanMachi",
    "ماجي": "Magi The Labyrinth of Magic",
    "متاهة السحر": "Magi The Labyrinth of Magic",
    "التناسخ": "That Time I Got Reincarnated as a Slime",
    "الوقت الذي تحولت فيه الى وحش طيني": "That Time I Got Reincarnated as a Slime",
    "صنع في الهاوية": "Made in Abyss",
    "ميد ان ابيس": "Made in Abyss",
    "انجل بيتس": "Angel Beats",
    "كلاناد": "Clannad",
    # ──── ميكا / كلاسيك ────────────────────────────────────────────────────
    "مادوكا ماجيكا": "Puella Magi Madoka Magica",
    "مادوكا": "Puella Magi Madoka Magica",
    "سيلور مون": "Sailor Moon",
    "غاندام": "Gundam",
    "جاندام": "Gundam",
    "ايفانغيليون": "Neon Genesis Evangelion",
    "ايفا": "Neon Genesis Evangelion",
    "اكيرا": "Akira",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

_AR_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def _is_arabic(text: str) -> bool:
    """Return True if the text contains Arabic characters."""
    return bool(_AR_RE.search(text))


def _normalize_ar(text: str) -> str:
    """Strip diacritics/tatweel and lowercase for dictionary lookup."""
    # Remove Arabic diacritics (harakat) and tatweel
    text = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", text)
    # Normalise alef variants → bare alef
    text = re.sub(r"[أإآٱ]", "ا", text)
    # Normalise teh marbuta → ha
    text = text.replace("ة", "ه")
    # Normalise ya variants
    text = text.replace("ى", "ي")
    return text.strip().lower()


def _ar_to_en(query: str) -> str | None:
    """Look up an Arabic query in the translation table. Returns English term or None."""
    key = _normalize_ar(query)
    if key in _AR_EN:
        return _AR_EN[key]
    # Partial match: if the normalised key starts with or contains a known entry
    for ar, en in _AR_EN.items():
        if ar in key or key in ar:
            return en
    return None


def _extract_arabic_synonyms(synonyms: list[str]) -> str:
    """Return the first Arabic synonym from AniList's synonyms list, or ''."""
    for s in synonyms:
        if s and _is_arabic(s):
            return s
    return ""


def _anilist_search(query: str, limit: int) -> list[dict]:
    """Raw AniList search — returns the media list or [] on error."""
    try:
        r = _SESSION.post(
            ANILIST_GQL,
            json={
                "query": _SEARCH_QUERY,
                "variables": {"search": query, "page": 1, "per": limit},
            },
            timeout=12,
        )
        r.raise_for_status()
        return r.json().get("data", {}).get("Page", {}).get("media", []) or []
    except Exception as exc:
        log.warning("[anidl] AniList search failed for %r: %s", query[:60], exc)
        return []


def _parse_media(item: dict, arabic_query: str = "") -> "AnimeResult":
    """Convert one AniList media dict into an AnimeResult."""
    titles    = item.get("title") or {}
    synonyms  = item.get("synonyms") or []
    eng       = titles.get("english") or titles.get("romaji") or "?"
    romaji    = titles.get("romaji") or ""
    desc_raw  = item.get("description") or ""
    desc      = re.sub(r"<[^>]+>", "", desc_raw)[:280]
    ar_syn    = _extract_arabic_synonyms(synonyms)

    # title_ar: prefer an Arabic synonym from AniList, fall back to romaji
    title_ar = ar_syn or romaji

    return AnimeResult(
        mal_id    = item["id"],
        title     = eng,
        title_ar  = title_ar,
        episodes  = item.get("episodes"),
        cover_url = (item.get("coverImage") or {}).get("large", ""),
        url       = item.get("siteUrl", ""),
        synopsis  = desc,
        format    = (item.get("format") or "").replace("_", " "),
    )


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class AnimeResult:
    mal_id:    int           # AniList ID here
    title:     str
    title_ar:  str           # Arabic synonym (if found) or romaji
    episodes:  Optional[int]
    cover_url: str
    url:       str
    synopsis:  str
    format:    str           # TV / MOVIE / OVA …


@dataclass
class TorrentResult:
    title:    str
    link:     str
    magnet:   str
    size:     str
    seeders:  int
    leechers: int


# ── AniList search ────────────────────────────────────────────────────────────

def search_anime(query: str, limit: int = 8) -> list[AnimeResult]:
    """
    Multi-pass AniList search — handles Arabic, English, and Romaji.

    Passes (Arabic queries only):
      1. Original Arabic query  → AniList searches synonyms natively
      2. Lookup table → well-known Arabic names mapped to English
      3. Same as pass 2 but after stripping diacritics/tatweel

    Results from multiple passes are merged (deduped by AniList ID) and
    re-ranked: entries whose synonyms contain an Arabic title matching the
    user's query float to the top.
    """
    arabic = _is_arabic(query)
    seen_ids: set[int] = set()
    merged: list[dict] = []

    def _add(items: list[dict]) -> None:
        for item in items:
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                merged.append(item)

    # Pass 1 — original query (works for English, Romaji, and AniList-indexed Arabic)
    _add(_anilist_search(query, limit))

    # Passes 2 & 3 — only when query is Arabic and pass 1 gave few results
    if arabic and len(merged) < 4:
        en_term = _ar_to_en(query)
        if en_term:
            log.info("[anidl] Arabic lookup: %r → %r", query, en_term)
            _add(_anilist_search(en_term, limit))

        # Pass 3: normalised Arabic (diacritics stripped) if still sparse
        if len(merged) < 4:
            norm = _normalize_ar(query)
            if norm != query:
                _add(_anilist_search(norm, limit))

    if not merged:
        return []

    # Re-rank: boost entries that have a matching Arabic synonym
    norm_q = _normalize_ar(query) if arabic else ""

    def _score(item: dict) -> int:
        if not norm_q:
            return 0
        synonyms = item.get("synonyms") or []
        for s in synonyms:
            if s and _is_arabic(s) and norm_q in _normalize_ar(s):
                return 1        # match found — higher is better
        return 0

    merged.sort(key=_score, reverse=True)

    return [_parse_media(item, query) for item in merged[:limit]]


# ── nyaa.si torrent lookup ────────────────────────────────────────────────────

def search_torrents(
    anime_title: str,
    episode: int,
    quality: str = "1080",
) -> list[TorrentResult]:
    """
    Search nyaa.si for an anime episode torrent.
    Returns up to 5 best results sorted by seeders.
    """
    query = f"{anime_title} {episode:02d} {quality}p"
    params = {"page": "rss", "q": query, "c": "1_2", "f": "0"}
    try:
        r = _SESSION.get(NYAA_RSS, params=params, timeout=12)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as exc:
        log.warning("[anidl] nyaa RSS failed: %s", exc)
        return []

    ns    = {"nyaa": "https://nyaa.si/xmlns/nyaa"}
    items : list[TorrentResult] = []

    for item in root.iter("item"):
        title    = (item.findtext("title") or "").strip()
        link     = (item.findtext("link") or "").strip()
        size     = (item.findtext("nyaa:size", namespaces=ns) or "?").strip()
        seeders  = int(item.findtext("nyaa:seeders",  namespaces=ns) or 0)
        leechers = int(item.findtext("nyaa:leechers", namespaces=ns) or 0)

        info_hash_match = re.search(r"/([0-9a-fA-F]{40})", link)
        if info_hash_match:
            ih     = info_hash_match.group(1)
            magnet = (
                f"magnet:?xt=urn:btih:{ih}"
                f"&dn={requests.utils.quote(title)}"
                f"&tr=http://nyaa.tracker.wf:7777/announce"
            )
        else:
            magnet = ""

        items.append(TorrentResult(
            title=title, link=link, magnet=magnet,
            size=size, seeders=seeders, leechers=leechers,
        ))

    items.sort(key=lambda x: x.seeders, reverse=True)
    return items[:5]


def search_episode_youtube(
    anime_title: str,
    ep_num: int,
    max_results: int = 3,
) -> list[dict]:
    """Search YouTube for an anime episode using yt-dlp.

    Returns a list of dicts with keys: url, duration (seconds), channel, is_full.
    An episode is considered "full" if its duration is >= 20 minutes.
    """
    import yt_dlp  # already a project dependency

    query = f"{anime_title} episode {ep_num}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "default_search": f"ytsearch{max_results + 2}",
        "noplaylist": True,
        "socket_timeout": 15,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results + 2}:{query}", download=False)
            entries = (info or {}).get("entries") or []
    except Exception as exc:
        log.warning("[anidl] YouTube search failed: %s", exc)
        return []

    results: list[dict] = []
    for entry in entries:
        if not entry:
            continue
        vid_id = entry.get("id") or entry.get("webpage_url_basename")
        if not vid_id:
            continue
        try:
            duration = int(entry.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        results.append(
            {
                "url": f"https://www.youtube.com/watch?v={vid_id}",
                "duration": duration,
                "channel": (entry.get("channel") or entry.get("uploader") or "?")[:60],
                "is_full": duration >= 20 * 60,
            }
        )
        if len(results) >= max_results:
            break

    return results


def download_torrent_bytes(torrent_link: str) -> Optional[bytes]:
    """
    Download the .torrent file from nyaa.si and return raw bytes.
    The link is like https://nyaa.si/download/1234567.torrent
    Returns None on failure.
    """
    try:
        r = _SESSION.get(torrent_link, timeout=15)
        r.raise_for_status()
        if r.content[:1] == b"d":   # valid bencoded torrent starts with 'd'
            return r.content
        return None
    except Exception as exc:
        log.warning("[anidl] torrent download failed: %s", exc)
        return None
