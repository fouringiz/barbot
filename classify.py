"""Классификация текста: тип заявки, категория расхода, сумма, слова-отмены."""
import re

from utils import esc, norm, short

# ---------------------------------------------------------------- needs classification
BREAK_KW = (
    "слома", "не работает", "не работают", "течет", "течь", "протек", "нет воды", "нет горячей",
    "нет света", "не включ", "сгорел", "глючит", "не морозит", "не холодит", "засор", "выбива",
    "искрит", "не греет", "трещин", "отвалил", "не закрыва", "не открыва",
)
EQUIP_KW = (
    "холодильник", "морозил", "льдогенератор", "блендер", "кофемашин", "посудомо", "техник",
    "шейкер", "джиггер", "стрейнер", "мадлер", "весы", "лампа", "светильник", "кран", "смесител",
    "бойлер", "водонагрев", "вытяжк", "кондиционер", "колонк", "принтер", "термина", "стул", "стол",
)
BAR_KW = (
    "кампари", "campari", "апероль", "aperol", "джин", "gin", "виски", "whisk", "бурбон", "ром ", "ром,", "rum",
    "текил", "tequila", "мескал", "водк", "vodka", "коньяк", "cognac", "бренди", "чача", "вермут", "vermouth",
    "ликер", "ликёр", "биттер", "ангостур", "пейшо", "абсент", "амаро", "фернет", "мараскино", "куантро",
    "трипл", "шартрез", "сироп", "сок", "пюре", "лайм", "лимон", "апельсин", "грейпфрут", "мят", "базилик",
    "тархун", "лед", "лёд", "тоник", "содов", "просекко", "шампанск", "вино", "пиво", "кола", "имбирн",
    "кофе", "эспрессо", "сливки", "белок", "яйц", "сахар", "мед ", "мёд", "соль", "гарнир", "вишн", "олив",
)
TYPE_ORDER = ("поломка", "оборудование", "бар", "расходник")
TYPE_ICON = {"поломка": "🔧", "оборудование": "📦", "бар": "🍾", "расходник": "🧻"}


def classify(text: str) -> str:
    t = norm(text)
    if any(k in t for k in BREAK_KW):
        return "поломка"
    if any(k in t for k in EQUIP_KW):
        return "оборудование"
    if any(k in (t + " ") for k in BAR_KW):
        return "бар"
    return "расходник"


def need_line(r: dict, closed: bool = False) -> str:
    t = r.get("type", "")
    icon = TYPE_ICON.get(t, "•")
    if closed:
        return (
            f"✅ <b>Заявка #{r['id']}</b> закрыта\n"
            f"<s>{esc(r['text'])}</s>\n"
            f"👤 {esc(r['closed_by'])} · {short(r['closed_at'])}"
        )
    label = f"<b>{esc(t).upper()}</b>" if t == "поломка" else esc(t)
    return (
        f"{icon} <b>Заявка #{r['id']}</b> · {label}\n"
        f"{esc(r['text'])}\n"
        f"👤 {esc(r['who'])} · {short(r['created'])}"
    )


# --- expenses («Расходы»)
QTY_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(кг|г|гр|шт|бут|л|мл|уп|пач|пак|кор|ящ|x|х)\b", re.I)
CUR_RE = re.compile(r"(\d+(?:[.,]\d{1,2})?)\s*(₼|azn|man|манат\w*|м\b)", re.I)
NUM_RE = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d{1,2})?)(?![\d.,])")
NEED_RE = re.compile(r"#(\d+)")
HOUSE_KW = (
    "бумаг", "мыло", "салфет", "пакет", "губк", "моющ", "перчат", "швабр", "тряпк", "фольг", "пленк", "плёнк",
    "трубоч", "одноразов", "жидкост", "чистящ", "порош", "мешк", "зубочист", "свеч", "спичк", "зажигал",
)
OTHER_KW = ("такси", "доставк", "курьер", "бензин", "парков", "связь", "интернет", "реклам", "печать", "чаев")
CAT_ICON = {"бар": "🍾", "расходники": "🧻", "ремонт": "🔧", "оборудование": "📦", "прочее": "•", "без описания": "❔"}


def parse_amount(text: str) -> float | None:
    t = NEED_RE.sub(" ", text)
    t = QTY_RE.sub(" ", t)
    m = CUR_RE.search(t)
    if m:
        return float(m.group(1).replace(",", "."))
    nums = NUM_RE.findall(t)
    return float(nums[-1].replace(",", ".")) if nums else None


def exp_category(text: str) -> str:
    t = norm(text)
    if not re.search(r"[a-zа-я]", t):
        return "без описания"
    if any(k in t for k in BREAK_KW):
        return "ремонт"
    if any(k in t for k in EQUIP_KW):
        return "оборудование"
    if any(k in (t + " ") for k in BAR_KW):
        return "бар"
    if any(k in t for k in HOUSE_KW):
        return "расходники"
    if any(k in t for k in OTHER_KW):
        return "прочее"
    return "прочее"


DISMISS_WORDS = ("❌", "x", "х", "-", "удали", "удоли", "удалить", "проигнорь", "проигнорируй", "игнор", "пропусти", "пропустить",
                 "не чек", "не расход", "не надо", "не нужно", "забей", "отмена", "отмени", "skip", "ignore", "мимо")


def is_dismiss(text: str) -> bool:
    t = norm(text).strip(".!, ")
    return bool(t) and (t in DISMISS_WORDS or any(t.startswith(w) for w in DISMISS_WORDS if len(w) > 2))
