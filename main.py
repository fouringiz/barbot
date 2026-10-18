"""Точка входа: командный бот (polling) + HTTP API в одном процессе."""
import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import services as svc
import sheets
from api import server
from bots import team
from config import BOT_TOKEN, TZ

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("barbot")


async def main() -> None:
    await asyncio.to_thread(sheets.get)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    svc.set_bot(bot)

    dp = Dispatcher()
    dp.include_router(team.router)

    sched = AsyncIOScheduler(timezone=TZ)
    team.register_jobs(sched)
    sched.start()

    await server.start()
    await bot.set_my_commands(team.COMMANDS)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("polling started")
    outbox = asyncio.create_task(svc.expense_outbox_worker())
    try:
        await dp.start_polling(bot)
    finally:
        outbox.cancel()
        with suppress(asyncio.CancelledError):
            await outbox


if __name__ == "__main__":
    asyncio.run(main())
