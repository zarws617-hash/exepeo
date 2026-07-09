"""Entry point — runs the main bot and the Go Live selfbot concurrently."""

from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from mangastarz.bot import run as run_bot

    user_token = os.environ.get("USER_TOKEN", "").strip()

    if user_token:
        from mangastarz import selfbot
        log.info("[main] USER_TOKEN found — starting selfbot alongside main bot")
        await asyncio.gather(
            run_bot(),
            selfbot.run(user_token),
            return_exceptions=False,
        )
    else:
        log.warning(
            "[main] USER_TOKEN not set — Go Live streaming (/golive) will be unavailable. "
            "Add USER_TOKEN to Replit Secrets to enable it."
        )
        await run_bot()


if __name__ == "__main__":
    asyncio.run(main())
