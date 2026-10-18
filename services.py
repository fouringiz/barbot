"""Общая логика для бота и API: посты в топики, стоп-лист, меню для гостей."""
import logging

from aiogram import Bot
from aiogram.types import Message

import sheets
from config import GROUP_ID
from sheets import run
from specs import spec_names, tok_match
from utils import esc, short

log = logging.getLogger("barbot.services")

bot: Bot | None = None   # выставляет main.py после создания Bot


def set_bot(b: Bot) -> None:
    global bot
    bot = b


async def post(topic: int | None, text: str) -> Message | None:
    if not GROUP_ID or bot is None:
        log.warning("GROUP_ID/бот не настроены — пост пропущен: %s", text[:60])
        return None
    return await bot.send_message(GROUP_ID, text, message_thread_id=topic)


async def stop_items() -> list[str]:
    return [r["item"] for r in await run(sheets.get().stoplist)]


async def stop_text() -> str:
    rows = await run(sheets.get().stoplist)
    if not rows:
        return "✅ Стоп-лист пуст"
    return "⛔ <b>Стоп-лист</b>\n" + "\n".join(
        f"• {esc(r['item'])} <i>— с {short(r['since'])}, {esc(r['who'])}</i>" for r in rows
    )


def spec_available(spec: dict, stop: list[str]) -> bool:
    return not any(tok_match(st, n) for st in stop for n in spec_names(spec))


async def menu_for_guests() -> list[dict]:
    """Авторская линия (тег «меню») с учётом стоп-листа — то, что увидит гость."""
    specs = await run(sheets.get().specs)
    stop = await stop_items()
    out = []
    for s in specs:
        tags = [t.strip() for t in str(s.get("tags", "")).split(";") if t.strip()]
        if "меню" not in [t.lower() for t in tags]:
            continue
        out.append({
            "name": s["name"],
            "pitch": s.get("pitch", ""),
            "method": s.get("method", ""),
            "glass": s.get("glass", ""),
            "garnish": s.get("garnish", ""),
            "tags": [t for t in tags if t.lower() != "меню"],
            "available": spec_available(s, stop),
        })
    return out
