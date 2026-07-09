"""
scraper_v2.py — Manga-starz.net scraper (rewrite)

Fixes:
  - Multi-strategy post-ID lookup: exact slug → strip trailing version
    number → title → any-slug-contains.
  - Single AJAX call returns ALL chapters (no false pagination).
  - Cloudflare bypass via cloudscraper (GET homepage first for cookies).
"""

from __future__ import annotations

import logging
import re
import requests
from dataclasses import dataclass

import cloudscraper
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL  = "https://manga-starz.net"
AJAX_URL  = f"{BASE_URL}/wp-admin/admin-ajax.php"
AJAX_HDR  = {"X-Requested-With": "XMLHttpRequest", "Referer": BASE_URL}

_COUNTRY_TYPE = {
    "JP": "📚 مانغا",
    "KR": "🇰🇷 مانهوا",
    "CN": "🇨🇳 مانها",
    "TW": "🇨🇳 مانها",
}


# ── dataclass ────────────────────────────────────────────────────────────────

@dataclass
class Chapter:
    manga_title: str
    manga_url:   str
    chapter_num: str
    chapter_url: str
    cover_url:   str


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_scraper() -> cloudscraper.CloudScraper:
    sc = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    sc.headers.update({"Accept-Language": "ar,en;q=0.9", "Referer": BASE_URL})
    return sc


def _slug_from_url(url: str) -> str:
    """Extract slug from manga URL.  e.g. .../manga/chainsaw-man-2/ → chainsaw-man-2"""
    return url.rstrip("/").split("/")[-1]


def _strip_trailing_version(slug: str) -> str:
    """chainsaw-man-digital-colored-2 → chainsaw-man-digital-colored"""
    return re.sub(r"-\d+$", "", slug)


def _parse_chapters(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    chapters = []
    for li in soup.select("li.wp-manga-chapter"):
        a = li.select_one("a")
        if not a:
            continue
        url = a.get("href", "").strip()
        num = a.get_text(strip=True)
        if url:
            chapters.append({"num": num, "url": url})
    return chapters


# ── post-ID lookup (multi-strategy) ─────────────────────────────────────────

def _madara_search(query: str, scraper: cloudscraper.CloudScraper) -> BeautifulSoup:
    """Run madara_load_more search and return parsed soup (empty soup on error)."""
    try:
        resp = scraper.post(
            AJAX_URL,
            data={
                "action": "madara_load_more",
                "page": "0",
                "template": "madara-core/content/content-archive",
                "vars[paged]": "1",
                "vars[orderby]": "meta_value_num",
                "vars[template]": "archive",
                "vars[sidebar]": "right",
                "vars[post_type]": "wp-manga",
                "vars[post_status]": "publish",
                "vars[s]": query,
                "vars[manga_archives_item_layout]": "default",
            },
            headers=AJAX_HDR,
            timeout=30,
        )
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.warning("[starz-v2] madara search error for '%s': %s", query, exc)
        return BeautifulSoup("", "html.parser")


def _find_pid_in_soup(
    soup: BeautifulSoup,
    target_slug: str,
) -> str | None:
    """
    From search results soup, return the post-ID whose href best matches
    target_slug.  Tries exact match first, then contains match, then first.
    """
    items = soup.select("[data-post-id]")
    if not items:
        return None

    # Pass 1 – exact slug at end of href
    for el in items:
        link = el.select_one("a[href]")
        href = link.get("href", "").rstrip("/") if link else ""
        if href.endswith(target_slug):
            pid = el.get("data-post-id")
            log.info("[starz-v2] exact slug match pid=%s slug=%s", pid, target_slug)
            return pid

    # Pass 2 – slug appears anywhere in href
    for el in items:
        link = el.select_one("a[href]")
        href = link.get("href", "") if link else ""
        if target_slug in href:
            pid = el.get("data-post-id")
            log.info("[starz-v2] contains slug match pid=%s slug=%s", pid, target_slug)
            return pid

    return None


def _get_post_id(
    title: str,
    scraper: cloudscraper.CloudScraper,
    manga_url: str = "",
) -> str | None:
    """
    Multi-strategy post-ID lookup:
      1. slug (exact) as search term
      2. slug with trailing version number stripped
      3. manga title
      4. fallback: return first result from title search

    Within each search, prefer exact-slug href match, then any-contains match.
    """
    target_slug = _slug_from_url(manga_url) if manga_url else ""
    stripped_slug = _strip_trailing_version(target_slug) if target_slug else ""

    queries_with_match: list[tuple[str, str]] = []

    if target_slug:
        queries_with_match.append((target_slug.replace("-", " "), target_slug))

    if stripped_slug and stripped_slug != target_slug:
        queries_with_match.append((stripped_slug.replace("-", " "), target_slug))

    if title:
        queries_with_match.append((title, target_slug))

    for query, slug_to_match in queries_with_match:
        soup = _madara_search(query, scraper)
        if soup.find():
            pid = _find_pid_in_soup(soup, slug_to_match) if slug_to_match else None
            if pid:
                return pid

    # Last resort: search by title, pick first result
    if title:
        soup = _madara_search(title, scraper)
        first = soup.select_one("[data-post-id]")
        if first:
            pid = first.get("data-post-id")
            log.warning("[starz-v2] fallback first-result pid=%s for '%s'", pid, title)
            return pid

    log.error("[starz-v2] could not find post ID for '%s' (url=%s)", title, manga_url)
    return None


# ── public API ───────────────────────────────────────────────────────────────

def fetch_latest_chapters() -> list[Chapter]:
    """Scrape homepage for the latest chapters."""
    scraper = _make_scraper()
    try:
        resp = scraper.get(f"{BASE_URL}/", timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.error("[starz-v2] homepage fetch failed: %s", exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    chapters: list[Chapter] = []

    for card in soup.select(".page-item-detail"):
        title_el = card.select_one(".post-title h3 a, .post-title h4 a")
        if not title_el:
            continue
        manga_title = title_el.get_text(strip=True)
        manga_url   = title_el.get("href", "")

        cover_el  = card.select_one(".item-thumb img")
        cover_url = ""
        if cover_el:
            cover_url = (
                cover_el.get("src")
                or cover_el.get("data-src")
                or cover_el.get("data-lazy-src")
                or ""
            )

        for ch_item in card.select(".chapter-item"):
            ch_link = ch_item.select_one("span.chapter a, a.btn-link")
            if not ch_link:
                continue
            ch_url = ch_link.get("href", "").strip()
            ch_num = ch_link.get_text(strip=True)
            if not ch_url:
                continue
            chapters.append(Chapter(
                manga_title=manga_title,
                manga_url=manga_url,
                chapter_num=ch_num,
                chapter_url=ch_url,
                cover_url=cover_url,
            ))

    log.info("[starz-v2] %d latest chapters fetched", len(chapters))
    return chapters


_ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: MANGA) {
    countryOfOrigin
  }
}
"""


def fetch_series_type(title: str) -> str:
    if not title:
        return ""
    try:
        resp = requests.post(
            "https://graphql.anilist.co",
            json={"query": _ANILIST_QUERY, "variables": {"search": title}},
            timeout=10,
        )
        if resp.status_code == 404:
            return ""
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            return ""
        media = (data.get("data") or {}).get("Media") or {}
        country = media.get("countryOfOrigin", "")
        return _COUNTRY_TYPE.get(country, "")
    except Exception as exc:
        log.debug("[anilist] lookup failed for '%s': %s", title, exc)
        return ""


def fetch_manga_chapters(manga_title: str, manga_url: str = "") -> list[dict]:
    """
    Fetch ALL chapters for a manga in one AJAX call.

    manga-starz.net returns every chapter in a single manga_get_chapters
    response — no real pagination exists.  Pass manga_url (from search_manga)
    for accurate post-ID lookup via slug matching.
    """
    scraper = _make_scraper()
    post_id = _get_post_id(manga_title, scraper, manga_url=manga_url)
    if not post_id:
        return []

    try:
        resp = scraper.post(
            AJAX_URL,
            data={"action": "manga_get_chapters", "manga": post_id},
            headers=AJAX_HDR,
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.error("[starz-v2] manga_get_chapters failed for '%s': %s", manga_title, exc)
        return []

    chapters = _parse_chapters(resp.text)
    log.info("[starz-v2] %d chapters for '%s' (pid=%s)", len(chapters), manga_title, post_id)
    return chapters


def _query_to_slug(query: str) -> str:
    """Convert a search query to a URL slug. e.g. 'One Piece' → 'one-piece'"""
    return re.sub(r"[^a-z0-9]+", "-", query.lower().strip()).strip("-")


def _try_direct_url(slug: str, scraper: cloudscraper.CloudScraper) -> dict | None:
    """
    Try fetching BASE_URL/manga/{slug}/ directly.
    If it returns a valid manga page, extract the title and return {title, url}.
    """
    url = f"{BASE_URL}/manga/{slug}/"
    try:
        resp = scraper.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        title_el = (
            soup.select_one(".post-title h1")
            or soup.select_one(".post-title h3")
            or soup.select_one("h1.entry-title")
        )
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        if title:
            log.info("[starz-v2] direct URL hit: '%s' → %s", title, url)
            return {"title": title, "url": url}
    except Exception as exc:
        log.debug("[starz-v2] direct URL check failed for slug '%s': %s", slug, exc)
    return None


def _full_size_image(src: str) -> str:
    """Strip WordPress thumbnail suffix (e.g. -110x150) to get the original image."""
    return re.sub(r"-\d+x\d+(\.\w+)$", r"\1", src)


def _fetch_cover_from_page(url: str, scraper: cloudscraper.CloudScraper) -> str:
    """
    Scrape the manga page to extract the cover image URL.
    Tries OG image meta, then the summary-image thumbnail.
    """
    try:
        resp = scraper.get(url, timeout=15)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        # Try Open Graph image first (most reliable)
        og = soup.select_one('meta[property="og:image"]')
        if og and og.get("content"):
            return og["content"].strip()
        # Try the summary thumbnail used by Madara themes
        img = (
            soup.select_one(".summary_image img")
            or soup.select_one(".tab-summary img")
            or soup.select_one(".manga-thumbnail img")
        )
        if img:
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
            return _full_size_image(src.strip()) if src else ""
    except Exception as exc:
        log.debug("[starz-v2] cover-from-page failed for %s: %s", url, exc)
    return ""


def _covers_from_madara_soup(soup: BeautifulSoup) -> dict[str, str]:
    """Extract {url → cover_url} from madara_load_more HTML soup."""
    covers: dict[str, str] = {}
    for item in soup.select("[data-post-id]"):
        link = item.select_one("a[href]")
        img  = item.select_one("img")
        if not link or not img:
            continue
        url = link.get("href", "").strip().rstrip("/") + "/"
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or ""
        )
        if url and src:
            covers[url] = _full_size_image(src)
    return covers


def search_manga(query: str) -> list[dict]:
    """Search manga-starz.net by title. Returns [{title, url, cover}, ...]."""
    scraper = _make_scraper()
    try:
        scraper.get(f"{BASE_URL}/", timeout=20)
    except Exception:
        pass

    results: list[dict] = []

    try:
        resp = scraper.post(
            AJAX_URL,
            data={"action": "wp-manga-search-manga", "title": query},
            headers=AJAX_HDR,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") and data.get("data"):
            results = [
                {"title": item["title"], "url": item["url"], "cover": ""}
                for item in data["data"]
                if item.get("title") and item.get("url")
            ]
            log.info("[starz-v2] search '%s' → %d results", query, len(results))
    except Exception as exc:
        log.warning("[starz-v2] search AJAX failed: %s — fallback to homepage", exc)

    # Fetch covers via madara_load_more using the original query
    madara_soup = _madara_search(query, scraper)
    covers = _covers_from_madara_soup(madara_soup)
    for r in results:
        key = r["url"].rstrip("/") + "/"
        if not r["cover"] and key in covers:
            r["cover"] = covers[key]

    # For results still missing covers: re-search madara using the URL slug as query
    # (handles cases like "chainsawman" → slug "chainsaw-man" → query "chainsaw man")
    for r in results:
        if not r.get("cover") and r.get("url"):
            url_slug = _slug_from_url(r["url"])
            slug_query = url_slug.replace("-", " ")
            if slug_query.lower() != query.lower():
                extra_soup = _madara_search(slug_query, scraper)
                extra_covers = _covers_from_madara_soup(extra_soup)
                covers.update(extra_covers)
                key = r["url"].rstrip("/") + "/"
                if key in covers:
                    r["cover"] = covers[key]
                    log.info("[starz-v2] cover via slug-query for '%s': %s", r["title"], r["cover"])

    # Try direct slug URL — catches exact-name titles the API misses (e.g. "one-piece")
    slug = _query_to_slug(query)
    direct_url = f"{BASE_URL}/manga/{slug}/"
    already_in_results = any(
        r["url"].rstrip("/") == direct_url.rstrip("/") for r in results
    )
    if not already_in_results:
        direct = _try_direct_url(slug, scraper)
        if direct:
            direct["cover"] = covers.get(direct["url"].rstrip("/") + "/", "")
            results.insert(0, direct)

    if results:
        return results[:25]

    # Last fallback: scan homepage chapters
    chapters = fetch_latest_chapters()
    q = query.lower()
    seen: set[str] = set()
    fallback = []
    for ch in chapters:
        if q in ch.manga_title.lower() and ch.manga_url not in seen:
            seen.add(ch.manga_url)
            fallback.append({"title": ch.manga_title, "url": ch.manga_url, "cover": ch.cover_url})
    return fallback[:25]
