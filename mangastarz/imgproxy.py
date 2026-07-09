"""Lightweight HTTP image proxy that bypasses Cloudflare using cloudscraper."""

from __future__ import annotations

import logging
import urllib.parse
from aiohttp import web
import asyncio
import concurrent.futures

import cloudscraper

log = logging.getLogger(__name__)

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


def _fetch_image_sync(url: str) -> tuple[bytes, str]:
    """Fetch image synchronously using cloudscraper (blocks Cloudflare bypass)."""
    scraper = cloudscraper.create_scraper()
    resp = scraper.get(url, timeout=15, headers={"Referer": "https://manga-starz.net/"})
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "image/jpeg")
    return resp.content, content_type


async def handle_img(request: web.Request) -> web.Response:
    url = request.rel_url.query.get("url", "")
    if not url:
        return web.Response(status=400, text="missing url param")
    try:
        url = urllib.parse.unquote(url)
        loop = asyncio.get_running_loop()
        data, ctype = await loop.run_in_executor(_executor, _fetch_image_sync, url)
        return web.Response(body=data, content_type=ctype.split(";")[0].strip())
    except Exception as exc:
        log.warning("imgproxy fetch failed for %s: %s", url, exc)
        return web.Response(status=502, text=f"upstream error: {exc}")


async def start_proxy(port: int = 5000) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/img", handle_img)
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Image proxy started on port %d", port)
    return runner
