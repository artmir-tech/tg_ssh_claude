"""Entry point: `python -m claude_control` (run by systemd: claude-control.service)."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from . import __version__, render
from .bot import Bot
from .config import load_config
from .db import Registry
from .manager import Manager
from .telegram import TelegramAPI


class RedactingFormatter(logging.Formatter):
    """Last line of defence: the bot token never reaches the journal."""

    def __init__(self, secret: str):
        super().__init__("%(levelname)s %(name)s: %(message)s")
        self.secret = secret

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(self.secret, "<token>")


async def amain() -> None:
    cfg = load_config()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(cfg.bot_token))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    log = logging.getLogger("cc.main")
    log.info("Claude Control v%s starting (max concurrent runs: %d)", __version__, cfg.max_concurrent)

    render.set_timezone(cfg.timezone)
    db = Registry(cfg.db_path)
    tg = TelegramAPI(cfg.bot_token, cfg.tg_api_base)
    manager = Manager(cfg, db)
    bot = Bot(cfg, db, tg, manager)

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, main_task.cancel)
    try:
        await bot.run()
    except asyncio.CancelledError:
        log.info("shutdown requested")
    finally:
        await bot.stop()
        await manager.shutdown()
        await tg.close()
        db.close()
        log.info("Claude Control stopped")


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
