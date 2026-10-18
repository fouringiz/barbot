"""Текстовые и временные помощники без побочных эффектов."""
import html
import re
from datetime import datetime

from config import DATE_FMT, TZ

def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s).replace("ё", "е").strip().lower())


def esc(s) -> str:
    return html.escape(str(s))


def now() -> datetime:
    return datetime.now(TZ)


def stamp(dt: datetime | None = None) -> str:
    return (dt or now()).strftime(DATE_FMT)


def parse_stamp(s: str) -> datetime | None:
    try:
        return datetime.strptime(str(s).strip(), DATE_FMT).replace(tzinfo=TZ)
    except ValueError:
        return None


def short(d: str) -> str:
    """02.10.2026 15:31 -> 02.10 15:31"""
    return re.sub(r"\.\d{4} ", " ", str(d))


RU_DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
RU_MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def ru_day(dt: datetime) -> str:
    return f"{RU_DAYS[dt.weekday()]} {dt.strftime('%d.%m')}"


def fmt_money(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".00", "") + " ₼"
