"""Работа со спеками: разбор ингредиентов, нечёткий поиск, карточка."""
import re

from rapidfuzz import fuzz, process

from utils import esc, norm

# "сок лайма 25" -> "сок лайма"; "ангостура 2 дэш" -> "ангостура"
UNIT_RE = re.compile(
    r"\s*[\d.,/]+\s*(мл|ml|дэш\w*|dash\w*|шт|г|гр|бл|б\.?л\.?|ч\.?л\.?|капл\w*|top|топ)?\.?\s*$",
    re.I,
)


def ingr_name(part: str) -> str:
    return norm(UNIT_RE.sub("", part))


def spec_parts(spec: dict) -> list[str]:
    return [p.strip() for p in str(spec.get("ingredients", "")).split(";") if p.strip()]


def spec_names(spec: dict) -> list[str]:
    return [ingr_name(p) for p in spec_parts(spec)]


def tok_match(q: str, name: str) -> bool:
    """q — короткий запрос ('лайм'), name — имя ингредиента ('сок лайма')."""
    q, name = norm(q), norm(name)
    if not q:
        return False
    if re.search(r"(^|\s)" + re.escape(q), name):     # префикс по границе слова
        return True
    return any(fuzz.ratio(q, t) >= 85 for t in name.split())   # опечатки


def find_spec(specs: list[dict], query: str, cutoff: int = 70) -> dict | None:
    index: dict[str, dict] = {}
    for s in specs:
        keys = [s.get("name", "")] + str(s.get("aliases", "")).split(",")
        for k in keys:
            k = norm(k)
            if k:
                index[k] = s
    if not index:
        return None
    hit = process.extractOne(norm(query), list(index.keys()), scorer=fuzz.WRatio, score_cutoff=cutoff)
    return index[hit[0]] if hit else None


def card(spec: dict) -> str:
    lines = [f"🍸 <b>{esc(spec['name'])}</b>"]
    lines += [esc(p) for p in spec_parts(spec)]
    meta = " · ".join(esc(spec[k]) for k in ("method", "glass", "garnish") if spec.get(k))
    if meta:
        lines.append(f"<i>{meta}</i>")
    if spec.get("pitch"):
        lines.append(f"💬 {esc(spec['pitch'])}")
    return "\n".join(lines)


def affected(specs: list[dict], item: str) -> list[str]:
    return [s["name"] for s in specs if any(tok_match(item, n) for n in spec_names(s))]
