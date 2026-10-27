# matcher.py
"""
Умный подбор товара на moba по названию WB.
Чистые функции + find_candidates (использует MobaClient).
"""

from __future__ import annotations

import json
import logging
import re
import types
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from moba_client import MobaClient

logger = logging.getLogger(__name__)

DEBUG_DIR = Path("debug")

# ---------------------------------------------------------------------- #
# Служебные слова и бренды
# ---------------------------------------------------------------------- #
STOP_WORDS = {
    "для", "копия", "copy",
    "оригинал", "original",
    "or", "oled", "amoled", "incell", "in-cell",
    "оптима", "optima", "standard", "стандарт",
    "в", "сборе", "с", "тачскрином",
    "модуль", "рамкой", "рамке", "рамка",
    "черный", "чёрный", "белый", "синий", "золотой",
    "красный", "зеленый", "зелёный", "фиолетовый",
    "серый", "голубой",
    "premium", "премиум", "lcd", "tft", "ips", "оригинальный", "service", "pack",
    "черная", "белая", "синяя", "золотая", "красная", "зеленая", "фиолетовая", "серая", "голубая",
}

# canon == None — слово выкидывается без смены бренда (galaxy)
BRAND_ALIASES: dict[str, Optional[str]] = {
    "apple": "apple",
    "iphone": "apple",
    "samsung": "samsung",
    "galaxy": None,
    "xiaomi": "xiaomi",
    "redmi": "xiaomi",
    "poco": "xiaomi",
    "mi": "xiaomi",
    "huawei": "huawei",
    "honor": "honor",
    "realme": "realme",
    "tecno": "tecno",
    "infinix": "infinix",
    "oppo": "oppo",
    "vivo": "vivo",
    "oneplus": "oneplus",
}

_XIAOMI_SUBBRANDS = ("redmi", "poco", "mi")

# Бренды, которые moba пишет вместе («Huawei Honor X9»)
BRAND_GROUPS = {"huawei": {"huawei", "honor"}, "honor": {"huawei", "honor"}}

# Цвета: основа слова → цвет
COLOR_STEMS = {"черн": "black", "бел": "white", "син": "blue", "голуб": "lightblue", "золот": "gold",
               "красн": "red", "зелен": "green", "зелён": "green", "фиолет": "purple", "сер": "gray",
               "розов": "pink", "желт": "yellow", "жёлт": "yellow"}


_OR100_RE = re.compile(r"(?<![\wА-Яа-яЁё])or\s?100(?![\wА-Яа-яЁё])", re.I)


def is_or100(title: str) -> bool:
    return bool(_OR100_RE.search(title or ""))


_CODE_RE = re.compile(r"\b([A-Z]{2,4}-?[A-Z]{0,3}\d{2,5}[A-Z0-9]{0,4})\b")


def model_codes(title: str) -> set[str]:
    """Коды моделей вида RMX2185, CPH2631, MAO-LX9N (без OR100 и коротких A40/X9)."""
    return {c for c in _CODE_RE.findall((title or "").upper())
            if not re.fullmatch(r"OR100|[A-Z]\d{1,3}[A-Z]?", c)}


def codes_conflict(wb_title: str, moba_name: str) -> bool:
    a, b = model_codes(wb_title), model_codes(moba_name)
    return bool(a and b and not (a & b))


def wb_wants_frame(title: str) -> bool:
    return bool(re.search(r"рамк|в рамке|с рам\.", (title or "").lower()))


def detect_color(title: str) -> Optional[str]:
    low = (title or "").lower()
    for w in re.findall(r"[а-яё]+", low):
        if w.startswith("серебр"):
            return "silver"
        for stem, col in COLOR_STEMS.items():
            if w.startswith(stem) and not w.startswith("сенсор") and w not in ("сервисный", "серия"):
                return col
    return None

# Тип товара: основа слова → как называет moba (первое слово её названий)
KNOWN_TYPES = ["дисплей", "аккумулятор", "шлейф", "крышка", "корпус", "плата", "разъем", "разъём",
               "камера", "стекло", "динамик", "микрофон", "тачскрин"]

_LEADING_STOP = {
    alias for alias, canon in BRAND_ALIASES.items()
    if canon is not None and alias not in _XIAOMI_SUBBRANDS
} | {"galaxy"}


# ---------------------------------------------------------------------- #
# Правила качества moba
# ---------------------------------------------------------------------- #
QUALITY_RULES: dict[str, list[str]] = {
    "original": ["or", "(sp)", "service pack", "оригинал", "100%"],
    "oled": ["oled", "amoled"],
    "copy": [
        "optima", "оптима", "standard", "стандарт",
        "incell", "in-cell", "tft", "ips", "lcd",
        "копия", "premium", "премиум", "hq", "amp", "mecanico",
    ],
}


# ---------------------------------------------------------------------- #
# 1. parse_wb_title
# ---------------------------------------------------------------------- #
def _detect_wb_quality(low: str) -> str:
    if "oled" in low or "amoled" in low:
        return "oled"
    # «(OR)», «- OR», «OR в рамке», «Origin…» — оригинал
    if ("оригинал" in low or "original" in low or "origin" in low or re.search(r"(?<![a-zа-яё])orig(?![a-zа-яё])", low)
            or re.search(r"(?<![a-zа-яё])or(?![a-zа-яё])", low)):
        return "original"
    return "copy"


def _find_brand(low: str) -> tuple[Optional[str], int]:
    """Возвращает (canon, end_position) первого бренда в строке."""
    best: Optional[tuple[str, int, int]] = None  # canon, start, end
    for alias, canon in BRAND_ALIASES.items():
        if canon is None:
            continue
        pattern = r"(?<![\wА-Яа-яЁё])" + re.escape(alias) + r"(?![\wА-Яа-яЁё])"
        m = re.search(pattern, low)
        if not m:
            continue
        if best is None or m.start() < best[1]:
            # подбренд Xiaomi (redmi/poco/mi) не «съедаем» — он станет префиксом модели
            end = m.start() if alias in _XIAOMI_SUBBRANDS else m.end()
            best = (canon, m.start(), end)
    if best is None:
        return None, 0
    return best[0], best[2]


def parse_wb_title(title: str) -> dict:
    raw = title
    low = title.lower()

    m = re.match(r"\s*([\wА-Яа-яЁё]+)", low)
    type_word = m.group(1) if m else ""
    # «LCD дисплей для…», «Оригинальный дисплей…» — тип берём по известному слову, а не первому
    m_known = re.search(r"(?<![\wА-Яа-яЁё])(" + "|".join(KNOWN_TYPES) + r")", low)
    if m_known:
        type_word = m_known.group(1)

    brand, brand_end = _find_brand(low)
    if brand is None:
        return {
            "type": type_word,
            "brand": "",
            "models": [],
            "quality": _detect_wb_quality(low),
            "raw": raw,
        }

    rest = low[brand_end:].strip()

    # Xiaomi-подбренд сохраняем как префикс модели
    prefix = ""
    if brand == "xiaomi":
        m2 = re.match(r"\s*(redmi|poco|mi)\b", rest)
        if m2:
            prefix = m2.group(1) + " "
            rest = rest[m2.end():].strip()

    # Выкидываем ведущие бренд-алиасы (iphone после apple, galaxy после samsung)
    while True:
        m3 = re.match(r"([\wА-Яа-яЁё]+)\b", rest)
        if not m3:
            break
        w = m3.group(1)
        if w in _LEADING_STOP:
            rest = rest[m3.end():].strip()
            continue
        break

    # Обрезаем до "(" или " - "
    cut = len(rest)
    for marker in ("(", " - "):
        idx = rest.find(marker)
        if 0 <= idx < cut:
            cut = idx
    head = rest[:cut].strip()

    # Убираем стоп-слова
    parts = re.split(r"\s+", head)
    cleaned: list[str] = []
    for p in parts:
        w = re.sub(r"[^\wА-Яа-яЁё]", "", p)
        if w in STOP_WORDS:
            continue
        cleaned.append(p)
    head = " ".join(cleaned).strip()

    # Мультимодели через "/" или ","
    models: list[str] = []
    for piece in re.split(r"[/,]", head):
        piece = piece.strip()
        if not piece:
            continue
        models.append((prefix + piece).strip().lower())

    seen: set[str] = set()
    uniq: list[str] = []
    for m_ in models:
        if m_ and m_ not in seen:
            seen.add(m_)
            uniq.append(m_)

    return {
        "type": type_word,
        "brand": brand,
        "models": uniq,
        "quality": _detect_wb_quality(low),
        "raw": raw,
    }


# ---------------------------------------------------------------------- #
# 2. classify_moba
# ---------------------------------------------------------------------- #
def _tail_of(label: str) -> str:
    idx = label.rfind(" - ")
    if idx < 0:
        return ""
    return label[idx + 3:].strip()


def _match_quality_tail(tail_low: str) -> Optional[str]:
    for q, words in QUALITY_RULES.items():
        for w in words:
            if w.startswith("("):  # (sp)
                if w in tail_low:
                    return q
                continue
            if w == "or":
                if re.search(r"(?<![\wА-Яа-яЁё])or(?:\s?100)?(?![\wА-Яа-яЁё])", tail_low):
                    return q
                continue
            if w in tail_low:
                return q
    return None


def classify_moba(label: str) -> dict:
    low = label.lower()
    tail = _tail_of(low)
    tail_low = tail.lower()

    quality = "unknown"
    if tail_low:
        # original приоритетнее oled (OR может встретиться с OLED одновременно)
        q = _match_quality_tail(tail_low)
        # original проверяем первым отдельно, чтобы не потерять из-за порядка словаря
        if re.search(r"(?<![\wА-Яа-яЁё])or(?:\s?100)?(?![\wА-Яа-яЁё])", tail_low) \
                or "(sp)" in tail_low \
                or "service pack" in tail_low \
                or "оригинал" in tail_low \
                or "100%" in tail_low:
            quality = "original"
        elif "oled" in tail_low or "amoled" in tail_low:
            quality = "oled"
        elif q is not None:
            quality = q

    orig_tail = _tail_of(label)
    quality_label = orig_tail[:40] if orig_tail else ""

    frame: Optional[bool]
    if "с рамкой" in low or "в рамке" in low:
        frame = True
    elif "в сборе с тачскрином" in low:
        frame = False
    else:
        frame = None

    return {
        "quality": quality,
        "quality_label": quality_label,
        "frame": frame,
    }


# ---------------------------------------------------------------------- #
# 3. model_matches
# ---------------------------------------------------------------------- #
def _normalize(s: str) -> str:
    # «S26+» -> «s26 plus», чтобы S26 не совпадал с S26+
    s = s.lower().replace("+", " plus ")
    # «/» и «(» оставляем отдельным токеном: «iPhone 8/SE 2020» — после 8 идёт «/», а не «se»
    return " ".join(re.findall(r"[\wА-Яа-яЁё]+|[/(]", s))


# Слова, которые делают модель ДРУГОЙ: «iPhone 11» ≠ «iPhone 11 Pro», «S26» ≠ «S26 Ultra»
MODEL_SUFFIXES = {
    "pro", "max", "plus", "ultra", "lite", "mini", "fe", "prime", "neo",
    "edge", "note", "power", "play", "turbo", "se", "x", "t", "s", "c", "e",
    "5g", "4g", "hd",
}


def model_matches(label: str, wb: dict) -> bool:
    type_ = (wb.get("type") or "").lower().strip()
    models = wb.get("models") or []
    if not type_ or not models:
        return False

    normalized = _normalize(label)

    # Тип — в начале строки
    # тип должен стоять в начале названия moba («Дисплей для…», «Задняя крышка для…»)
    head = normalized.split()[:3]
    stem = type_[:6]
    if not any(w.startswith(stem) for w in head):
        return False

    # бренд WB должен встречаться в названии moba (Honor 9S ≠ Redmi Note 9S, OPPO A60 ≠ Itel A60)
    brand = (wb.get("brand") or "").lower()
    if brand:
        group = BRAND_GROUPS.get(brand, {brand})
        aliases = {a for a, c in BRAND_ALIASES.items() if c in group} | group
        if not any(re.search(r"(?<![\wА-Яа-яЁё])" + re.escape(a) + r"(?![\wА-Яа-яЁё])", normalized) for a in aliases):
            return False

    tokens = normalized.split()
    for model in models:
        m_tokens = _normalize(model).split()
        if not m_tokens:
            continue
        head_m, last_m = m_tokens[:-1], m_tokens[-1]
        k = len(head_m)
        for i in range(len(tokens) - k):
            if tokens[i:i + k] != head_m:
                continue
            # перебираем варианты через «/»: «redmi 9a / 9c / 10a» содержит redmi 9c
            j = i + k
            while j < len(tokens):
                if tokens[j] == last_m:
                    nxt = tokens[j + 1] if j + 1 < len(tokens) else ""
                    if nxt not in MODEL_SUFFIXES and not re.fullmatch(r"20\d\d", nxt):  # «11 pro», «C11 2021» — другая модель
                        return True
                    break
                # следующий вариант через «/» (пропускаем возможный суффикс текущего, например «8 plus / 9»)
                nj = j + 1
                while nj < len(tokens) and tokens[nj] in MODEL_SUFFIXES:
                    nj += 1
                if nj < len(tokens) and tokens[nj] == "/":
                    j = nj + 1
                    continue
                break
    return False


# ---------------------------------------------------------------------- #
# 4. allowed
# ---------------------------------------------------------------------- #
_ALLOWED_MAP = {
    "copy": {"copy", "unknown"},
    "oled": {"oled", "unknown"},
    "original": {"original", "unknown"},
}


def allowed(wb_quality: str, moba_quality: str) -> bool:
    return moba_quality in _ALLOWED_MAP.get(wb_quality, {"unknown"})


# ---------------------------------------------------------------------- #
# 5. build_queries
# ---------------------------------------------------------------------- #
def build_queries(wb: dict) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = " ".join(q.split()).strip().lower()
        if not q or q in seen:
            return
        seen.add(q)
        out.append(q)

    brand = (wb.get("brand") or "").lower()
    type_ = (wb.get("type") or "").lower()
    quality = wb.get("quality") or "copy"
    models = wb.get("models") or []

    for model in models:
        model_low = model.lower()
        use_brand = "iphone" if brand == "apple" else brand
        if brand == "xiaomi":
            first = model_low.split(" ", 1)[0]
            if first in _XIAOMI_SUBBRANDS:
                use_brand = ""

        # бренд в названии WB и на moba часто разный (Huawei Honor X9c ↔ Honor X9c):
        # для «говорящих» моделей (буквы+цифры: x9c, a40) ищем и без бренда
        distinctive = bool(re.search(r"[a-z]", model_low) and re.search(r"\d", model_low))
        if use_brand:
            add(f"{type_} {use_brand} {model_low}")
            if distinctive:
                add(f"{type_} {model_low}")
            add(f"{use_brand} {model_low}")
        else:
            add(f"{type_} {model_low}")
            add(model_low)

        if quality == "copy":
            if use_brand:
                add(f"{type_} {use_brand} {model_low} optima")
                add(f"{type_} {use_brand} {model_low} incell")
                add(f"{type_} {use_brand} {model_low} standard")
            else:
                add(f"{type_} {model_low} optima")
                add(f"{type_} {model_low} incell")
                add(f"{type_} {model_low} standard")

    return out[:8]


# ---------------------------------------------------------------------- #
# 6. find_candidates
# ---------------------------------------------------------------------- #
def _dump_match_debug(wb: dict, result: dict, rejected: list[dict]) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        models = wb.get("models") or ["unknown"]
        part = models[0].replace(" ", "_") or "unknown"
        path = DEBUG_DIR / f"match_{part}.json"
        payload = {
            "wb": wb,
            "queries": result.get("queries", []),
            "items": result.get("items", []),
            "hidden": result.get("hidden", 0),
            "rejected": rejected,
        }
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось сохранить debug/match_*.json: %s", exc)


_BC_RE = re.compile(r"(?<![A-Za-zА-Яа-я])BC(?![A-Za-zА-Яа-я])")


def is_battery(wb: dict) -> bool:
    t = (wb.get("type") or "").lower()
    return t.startswith("аккумулятор") or t in ("акб", "батарея")


def has_bc(name: str) -> bool:
    return bool(_BC_RE.search(name or ""))


def _norm_low(s: str) -> str:
    return " ".join(s.split()).strip().lower()


async def find_candidates(
    moba_client: "MobaClient",
    wb_title: str,
    show_all: bool = False,
    max_items: int = 8,
    learned: Optional["Learned"] = None,
    check_stock: bool = True,
    max_queries: int = 8,
) -> dict:
    """check_stock=False — режим тренировки: без карточки и наличия, цена из поиска (быстро)."""
    from learning import norm_label  # локальный импорт, чтобы избежать цикла

    wb = parse_wb_title(wb_title)

    # ------- queries: сначала из обученных, потом базовые ------- #
    queries: list[str] = []
    seen_q: set[str] = set()

    if learned is not None:
        for model in wb.get("models", []):
            try:
                for q in learned.model_queries(wb.get("brand", ""), model):
                    qq = _norm_low(q)
                    if qq and qq not in seen_q:
                        seen_q.add(qq)
                        queries.append(qq)
            except Exception as exc:  # noqa: BLE001
                logger.warning("model_queries упал: %s", exc)

    for q in build_queries(wb):
        qq = _norm_low(q)
        if qq and qq not in seen_q:
            seen_q.add(qq)
            queries.append(qq)

    # ------- поиск ------- #
    raw_results: list[dict] = []
    seen_urls: set[str] = set()
    for q in queries[:max_queries]:
        try:
            res = await moba_client.search(q, 20)
        except Exception as exc:  # noqa: BLE001
            logger.warning("search('%s') упал: %s", q, exc)
            continue
        for r in res:
            url = r.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            raw_results.append({"url": url, "name": r.get("name") or "", "price": r.get("price")})

    # ------- фильтр ------- #
    matched: list[dict] = []
    rejected: list[dict] = []
    hidden_quality = 0

    for r in raw_results:
        if not model_matches(r["name"], wb):
            rejected.append({
                "url": r["url"],
                "name": r["name"],
                "reason": "model",
            })
            continue
        cls = classify_moba(r["name"])
        if is_battery(wb) and not has_bc(r["name"]):
            # аккумуляторы на moba берём ТОЛЬКО с пометкой BC — остальные не показываем даже в «Показать все»
            rejected.append({"url": r["url"], "name": r["name"], "reason": "battery_not_bc"})
            continue
        if wb_wants_frame(wb_title) and cls.get("frame") is not True:
            # на WB «с рамкой» — значит и на moba только с рамкой
            if not show_all:
                hidden_quality += 1
                rejected.append({"url": r["url"], "name": r["name"], "reason": "frame"})
                continue
        wb_color = detect_color(wb_title)
        moba_color = detect_color(r["name"])
        if wb_color and moba_color and wb_color != moba_color:
            # другой цвет — только в «Показать все»
            if not show_all:
                hidden_quality += 1
                rejected.append({"url": r["url"], "name": r["name"], "reason": "color"})
                continue
        if not show_all:
            hide = False
            if not allowed(wb["quality"], cls["quality"]):
                hide = True
            if learned is not None and learned.is_blocked(
                wb["quality"], cls["quality_label"]
            ):
                hide = True
            if hide:
                hidden_quality += 1
                rejected.append({
                    "url": r["url"],
                    "name": r["name"],
                    "reason": "quality",
                    "quality": cls["quality"],
                })
                continue
        r["_cls"] = cls
        matched.append(r)

    # ------- OR100: на WB «OR100» — строго OR100; обычный OR только если OR100 на moba нет ------- #
    if is_or100(wb_title) and not show_all and any(is_or100(r["name"]) for r in matched):
        keep = []
        for r in matched:
            if is_or100(r["name"]):
                keep.append(r)
            else:
                hidden_quality += 1
                rejected.append({"url": r["url"], "name": r["name"], "reason": "not_or100"})
        matched = keep

    # ------- обогащение ------- #
    enriched: list[dict] = []
    for r in matched[:max_items]:
        if not check_stock:
            product = types.SimpleNamespace(
                price=r.get("price"), article=None, product_id=None
            )
            status = "unknown"
        else:
            try:
                product = await moba_client.get_product(r["url"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("get_product(%s) упал: %s", r["url"], exc)
                continue
            try:
                status = await moba_client.mitino_status(r["url"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("mitino_status(%s) упал: %s", r["url"], exc)
                status = "unknown"

        cls = r["_cls"]
        score = 0.0
        if learned is not None:
            try:
                score = learned.label_score(
                    wb["quality"], cls["quality_label"]
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("label_score упал: %s", exc)
                score = 0.0

        enriched.append({
            "url": r["url"],
            "name": r["name"],
            "price": product.price,
            "article": product.article,
            "product_id": product.product_id,
            "mitino": status,
            "quality": cls["quality"],
            "quality_label": cls["quality_label"],
            "frame": cls["frame"],
            "_score": score,
            "_norm_label": norm_label(cls["quality_label"]),
        })

    # ------- сортировка ------- #
    _wf = wb_wants_frame(wb_title)
    if wb["quality"] == "copy":
        # Правило Анри: копия (или ничего не написано) — нужна САМАЯ ДЕШЁВАЯ копия.
        # Нелюбимые пометки (премиум и т.п., баллы < −2) уходят вниз.
        enriched.sort(key=lambda x: (
            0 if x["mitino"] == "yes" else 1,
            0 if bool(x.get("frame")) == _wf else 1,
            1 if float(x["_score"] or 0.0) < -2 else 0,
            int(x["price"] or 10**9),
        ))
    else:
      enriched.sort(key=lambda x: (
        0 if x["mitino"] == "yes" else 1,
        0 if bool(x.get("frame")) == _wf else 1,  # без рамки на WB — сначала варианты без рамки
        -float(x["_score"] or 0.0),
        int(x["price"] or 0),
    ))

    # ------- recommended ------- #
    for it in enriched:
        it["recommended"] = False
    if enriched:
        first = enriched[0]
        if wb["quality"] == "copy":
            if first["mitino"] == "yes" and first["quality"] == "copy" and float(first["_score"] or 0.0) >= -2:
                first["recommended"] = True  # самая дешёвая подходящая копия
        elif first["mitino"] == "yes" and float(first["_score"] or 0.0) >= 3.0:
            first["recommended"] = True

    # ------- убрать служебные поля ------- #
    for it in enriched:
        it.pop("_score", None)
        it.pop("_norm_label", None)

    result = {
        "wb": wb,
        "items": enriched,
        "hidden": hidden_quality,
        "queries": queries,
    }
    _dump_match_debug(wb, result, rejected)
    return result
