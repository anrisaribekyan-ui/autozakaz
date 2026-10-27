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
    "в", "сборе", "с", "тачскрином", "bc",
    "узкое", "узкий", "узким", "широкое", "широкий", "широким", "отверстие", "отверстием",
    "премиум", "premium",
    "модуль", "рамкой", "рамке", "рамка", "раме", "рамой", "рама",
    "черный", "чёрный", "белый", "синий", "золотой",
    "красный", "зеленый", "зелёный", "фиолетовый",
    "серый", "голубой",
    "premium", "премиум", "lcd", "tft", "ips", "jk", "mnk", "zy", "rj", "hex", "оригинальный", "service", "pack",
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
    "nokia": "nokia", "tcl": "tcl", "wiko": "wiko", "motorola": "motorola", "moto": "motorola",
    "google": "google", "zte": "zte", "nubia": "zte", "asus": "asus", "sony": "sony", "lg": "lg",
    "meizu": "meizu", "oukitel": "oukitel", "blackview": "blackview", "doogee": "doogee", "itel": "itel",
    "lenovo": "lenovo", "nothing": "nothing", "ulefone": "ulefone", "alcatel": "alcatel", "bq": "bq",
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
            if not re.fullmatch(r"OR100|ORIG\d*|OR\d+|[A-Z]\d{1,3}[A-Z]?", c)}


def codes_conflict(wb_title: str, moba_name: str) -> bool:
    a, b = model_codes(wb_title), model_codes(moba_name)
    return bool(a and b and not (a & b))


def wb_wants_frame(title: str) -> bool:
    return bool(re.search(r"рамк|в рамке|с рам\.|(?<![а-яё])(?:в\s+)?рам[еыо]й?(?![а-яё])", (title or "").lower()))


_LATIN_COLORS = {"gold": "gold", "black": "black", "white": "white", "blue": "blue", "silver": "silver",
                 "graphite": "gray", "gray": "gray", "grey": "gray", "green": "green", "purple": "purple",
                 "red": "red", "pink": "pink", "yellow": "yellow"}


def colors_compatible(a: Optional[str], b: Optional[str]) -> bool:
    """Цвета совместимы: не указан, совпадает, или «Серебро» ↔ белый/серебристый."""
    if not a or not b or a == b:
        return True
    pair = {a, b}
    return "silver_or_white" in pair and bool(pair & {"white", "silver"})


def detect_color(title: str) -> Optional[str]:
    low = (title or "").lower()
    for w in re.findall(r"[a-zа-яё]+", low):
        if w in _LATIN_COLORS:
            return _LATIN_COLORS[w]
        if w.startswith("серебрист"):
            return "silver"
        if w.startswith("серебр"):      # «Серебро» подходит и к белому, и к серебристому (Анри, 18.10)
            return "silver_or_white"
        if w.startswith("графит"):      # «Графит» = серый
            return "gray"
        for stem, col in COLOR_STEMS.items():
            if w.startswith(stem) and not w.startswith("сенсор") and w not in ("сервисный", "серия"):
                return col
    return None

# Тип товара: основа слова → как называет moba (первое слово её названий)
KNOWN_TYPES = ["дисплей", "аккумулятор", "шлейф", "крышка", "корпус", "плата", "разъем", "разъём",
               "камера", "стекло", "динамик", "микрофон", "тачскрин", "спикер", "бузер", "звонок"]

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
    # «Спикер» (верхний) и «Бузер» (нижний) у Анри — на moba это «Динамик …»
    # moba: нижний = «Звонок (buzzer) …», верхний = «Динамик (speaker) …» (прайс moba, 18.10)
    if type_word in ("спикер", "бузер", "звонок", "динамик"):
        bottom = re.search(r"бузер|buzzer|звонок|полифон|нижн", low)
        type_word = "звонок" if bottom else "динамик"
    # «АКБ», «Батарея» — на moba это «Аккумулятор»
    if type_word in ("акб", "батарея", "батарейка") or re.match(r"\s*(акб|батаре)", low):
        type_word = "аккумулятор"

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

    # Обрезаем до "(" или " - " (для шлейфов/плат — ещё и до назначения: «на кнопки громкости»)
    cut = len(rest)
    if type_word.startswith(("шлейф", "плат", "разъ", "коннект", "камер", "динамик", "звонок")):
        mcut = _PART_CUT_RE.search(" " + rest)
        if mcut and mcut.start() > 0:
            cut = mcut.start() - 1
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
        if cleaned and detect_color(w):  # «13 Pro Max Золото» — цвет не часть модели
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
    # « + тач» (с пробелами) — это «в сборе с тачскрином», а не Plus
    s = re.sub(r"\s\+\s", " и ", s.lower()).replace("+", " plus ")
    # «/» и «(» оставляем отдельным токеном: «iPhone 8/SE 2020» — после 8 идёт «/», а не «se»
    return " ".join(re.findall(r"[\wА-Яа-яЁё]+|[/(]", s))


# Слова, которые делают модель ДРУГОЙ: «iPhone 11» ≠ «iPhone 11 Pro», «S26» ≠ «S26 Ultra»
MODEL_SUFFIXES = {
    "pro", "max", "plus", "ultra", "lite", "mini", "fe", "prime", "neo",
    "edge", "note", "power", "play", "turbo", "se", "x", "t", "s", "c", "e",
    "5g", "4g", "hd",
}


_SERIES_WORDS = {"pad", "tab", "ipad", "mate", "nova", "enjoy", "magic", "matepad", "mediapad", "view", "play",
                 "smart", "watch", "band"}
_TYPE_LEAD_ADJ = {"задняя", "передняя", "основная", "нижняя", "верхняя", "lcd", "оригинальный"}


def model_matches(label: str, wb: dict) -> bool:
    type_ = (wb.get("type") or "").lower().strip()
    models = wb.get("models") or []
    if not type_ or not models:
        return False

    normalized = _normalize(label)

    # Тип — в начале строки
    # тип должен стоять в начале названия moba («Дисплей для…», «Задняя крышка для…»)
    # Первое значимое слово moba = тип WB. «Рамка дисплея», «Стекло для переклейки дисплея»,
    # «Шлейф дисплея» — другие запчасти, хоть и содержат слово «дисплей».
    stem = type_[:6]
    words = [w for w in normalized.split() if w not in _TYPE_LEAD_ADJ]
    # «Плата зарядки» Анри = на moba «Шлейф … плата системный разъём/микрофон»
    alt = ("шлейф",) if stem.startswith("плат") else ()
    # moba называет аккумуляторы «АКБ для …»
    if stem.startswith("аккуму") and words and words[0].startswith("акб"):
        words = ["аккумулятор"] + words[1:]
    if not words or not (words[0].startswith(stem) or (alt and words[0].startswith(alt) and "плат" in normalized)):
        return False

    # бренд WB должен встречаться в названии moba (Honor 9S ≠ Redmi Note 9S, OPPO A60 ≠ Itel A60)
    brand = (wb.get("brand") or "").lower()
    if brand:
        group = BRAND_GROUPS.get(brand, {brand})
        aliases = {a for a, c in BRAND_ALIASES.items() if c in group} | group
        if not any(re.search(r"(?<![\wА-Яа-яЁё])" + re.escape(a) + r"(?![\wА-Яа-яЁё])", normalized) for a in aliases):
            return False

    tokens = normalized.split()
    wb_raw_tokens = set(_normalize(wb.get("raw") or "").split())
    for model in models:
        m_tokens = _normalize(model).split()
        if not m_tokens:
            continue
        head_m, last_m = m_tokens[:-1], m_tokens[-1]
        k = len(head_m)
        for i in range(len(tokens) - k):
            if tokens[i:i + k] != head_m:
                continue
            # «Honor 8» ≠ «Honor Pad 8», «Honor 20 Lite» ≠ «Mate 20 Lite»: серия перед моделью должна быть и на WB
            if i > 0 and tokens[i - 1] in _SERIES_WORDS and tokens[i - 1] not in wb_raw_tokens:
                continue
            # перебираем варианты через «/»: «redmi 9a / 9c / 10a» содержит redmi 9c
            j = i + k
            while j < len(tokens):
                if tokens[j] == last_m:
                    nxt = tokens[j + 1] if j + 1 < len(tokens) else ""
                    # «Honor 7» ≠ «MediaPad X1 7.0»: голая цифра-модель — перед ней бренд/слово с WB, после — не «.0»
                    if k == 0 and last_m.isdigit():
                        prev = tokens[j - 1] if j > 0 else ""
                        if (prev not in wb_raw_tokens and prev != "/") or nxt.isdigit():
                            break
                    if nxt not in MODEL_SUFFIXES and not (nxt.isdigit() and len(nxt) <= 2 and last_m.isalpha()) and not re.fullmatch(r"20\d\d", nxt):  # «11 pro», «C11 2021» — другая модель
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

        if type_.startswith("плат"):
            add(f"шлейф {use_brand} {model_low} плата".replace("  ", " "))
            add(f"шлейф {model_low} плата")
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


_CATALOG: Optional[list[dict]] = None
CATALOG_PATH = "data/moba_price.json"


def catalog_articles(wb: dict, limit: int = 4) -> list[str]:
    """Артикулы из прайса moba для модели WB (прайс бывает старым — цены не берём, только артикулы)."""
    global _CATALOG
    if _CATALOG is None:
        try:
            import json as _json
            with open(CATALOG_PATH, encoding="utf-8") as f:
                _CATALOG = _json.load(f)
        except Exception:  # noqa: BLE001
            _CATALOG = []
    hits = [r for r in _CATALOG if r.get("art") and model_matches(r["name"], wb)]
    if not hits and is_battery(wb):
        # АКБ у moba названы по коду: «АКБ для Samsung EB-BA530ABE (A530F A8 2018)».
        # Ищем по кодам из WB (BN47, EB-BG990ABY, A530 → A530F)
        def _nc(x: str) -> str:
            return re.sub(r"[^A-Z0-9]", "", x.upper())
        codes = {_nc(m) for m in re.findall(r"\b[A-Za-z]{1,4}-?[A-Za-z]{0,4}\d[A-Za-z0-9-]{1,10}\b", wb.get("raw") or "")}
        codes = {c for c in codes if len(c) >= 4 and re.search(r"[A-Z]", c)}
        if codes:
            for r in _CATALOG:
                if not r.get("art") or not re.match(r"\s*(акб|аккумулятор)", r["name"].lower()):
                    continue
                toks = {_nc(t) for t in re.findall(r"[A-Za-z0-9-]+", r["name"])}
                if codes & toks or any(t.startswith(c) and len(t) - len(c) <= 2 for c in codes for t in toks):
                    hits.append(r)
    hits.sort(key=lambda r: r.get("price") or 10**9)
    return [r["art"] for r in hits[:limit]]


def is_battery(wb: dict) -> bool:
    t = (wb.get("type") or "").lower()
    return t.startswith("аккумулятор") or t in ("акб", "батарея")


def has_bc(name: str) -> bool:
    return bool(_BC_RE.search(name or "")) or "battery collection" in (name or "").lower()


def is_premium(name: str) -> bool:
    """Премиум: «Премиум/Premium», у Мастер Мобайла — «со стеклом камеры» / «в сборе со стеклом камеры»."""
    low = (name or "").lower()
    return bool(re.search(r"премиум|premium|со стеклом камеры", low))


PART_TYPES = ("шлейф", "плат", "разъ", "коннект", "камер", "динамик", "звонок")


def is_part(wb_type: str) -> bool:
    return (wb_type or "").startswith(PART_TYPES)


def is_premium_part(name: str) -> bool:
    """Шлейфы/платы/разъёмы (Анри, 18.10): премиум = «с компонентами» / «премиум»; оригинал тоже уровень премиум.
    Дешёвые — без компонентов."""
    low = (name or "").lower()
    if re.search(r"без\s+комп", low):
        return False
    return bool(re.search(r"с\s+комп|премиум|premium|ориг|orig|прем\b|(?<![a-zа-яё])or(?:100)?(?![a-zа-яё])|\(sp\)", low))


# Назначение шлейфа/платы: должно совпадать (кнопки громкости ≠ кнопки включения ≠ межплатный …)
PART_FUNCS = [
    ("беспровод", "wireless"), ("вспышк", "flash"),
    ("громк", "volume"), ("включ", "power"), ("power", "power"), ("межплат", "interboard"),
    ("отпечат", "fingerprint"), ("сканер", "fingerprint"), ("системн", "charge"), ("зарядк", "charge"),
    ("type-c", "charge"), ("micro usb", "charge"), ("microusb", "charge"), ("дисплея", "lcd"), ("на дисплей", "lcd"),
    ("lcd", "lcd"),
    ("sim", "sim"), ("сим", "sim"), ("камер", "camera"), ("динамик", "speaker"), ("микрофон", "mic"),
    ("nfc", "nfc"), ("антенн", "antenna"), ("вибро", "vibro"),
]


CAMERA_FUNCS = [("фронт", "front"), ("передн", "front"), ("селфи", "front"), ("широк", "wide"),
                ("ultra", "wide"), ("макро", "macro"), ("глубин", "depth"), ("основн", "main"),
                ("задн", "main"), ("тылов", "main")]
SPEAKER_FUNCS = [("бузер", "bottom"), ("buzzer", "bottom"), ("полифон", "bottom"), ("нижн", "bottom"),
                 ("звонок", "bottom"), ("спикер", "top"), ("speaker", "top"), ("слухов", "top"),
                 ("разговор", "top"), ("верхн", "top")]


def part_func(text: str, wb_type: str = "") -> Optional[str]:
    low = (text or "").lower()
    t = (wb_type or "").lower()
    if t.startswith("камер") or (not t and low.lstrip().startswith("камер")):
        return next((k for st, k in CAMERA_FUNCS if st in low), None)
    if t.startswith(("динамик", "звонок")) or low.lstrip().startswith(("динамик", "спикер", "бузер", "звонок")):
        return next((k for st, k in SPEAKER_FUNCS if st in low), None)
    # разъём: Type-C ≠ MicroUSB ≠ Lightning
    if t.startswith("разъ"):
        if "type-c" in low or "type c" in low or "usb-c" in low:
            return "type-c"
        if "micro" in low:
            return "micro-usb"
        if "lightning" in low:
            return "lightning"
    for stem, key in PART_FUNCS:
        if stem in low:
            return key
    if "нижн" in low and "плат" in low:   # «Нижняя плата» = плата зарядки
        return "charge"
    return None


def part_funcs(text: str, wb_type: str = "") -> set[str]:
    """Все назначения в названии (у moba бывают комбо: «на кнопки громкости/включения»)."""
    low = (text or "").lower()
    t = (wb_type or "").lower()
    if t.startswith("камер"):
        return {k for st, k in CAMERA_FUNCS if st in low}
    if t.startswith(("динамик", "звонок")):
        return {k for st, k in SPEAKER_FUNCS if st in low}
    one = part_func(text, wb_type)
    out = {k for st, k in PART_FUNCS if st in low}
    if "беспровод" in low and "системн" not in low and not re.search(r"type-?c|micro\s?usb|разъ[её]м", low):
        out.discard("charge")          # «на беспроводную зарядку» ≠ разъём зарядки
        if one == "charge":
            one = "wireless"
    if one:
        out.add(one)
    return out


def part_func_ok(wb_title: str, name: str, wb_type: str) -> bool:
    """Назначение WB должно быть среди назначений товара поставщика."""
    want = part_func(wb_title, wb_type)
    if not want:
        return True
    return want in part_funcs(name, wb_type)


def premium_rule_applies(wb_type: str) -> bool:
    """Премиум (с компонентами) — только для шлейфов/плат/разъёмов. У камер iPhone на moba всё «Премиум»."""
    return (wb_type or "").startswith(("шлейф", "плат", "разъ", "коннект"))


_PART_CUT_RE = re.compile(r"\s(?:на|с|без|под|для\s+кнопк)\s|\s(?:кнопк|громк|включ|межплат|отпечат|сканер|"
                          r"системн|зарядк|разъ[её]м|type-c|micro\s?usb|дисплея|sim|сим|камер|динамик|микрофон|"
                          r"nfc|антенн|вибро|компонент|основн|фронт|передн|задн|тылов|широк|макро|селфи|"
                          r"спикер|бузер|полифон|слухов|нижн|верхн|звонок)")


def is_narrow(name: str) -> bool:
    return "узк" in (name or "").lower()


_OR_RE = re.compile(r"(?<![a-zа-яё])or(?:100)?(?![a-zа-яё])|ориг|orig", re.I)


def is_or(name: str) -> bool:
    """Оригинал (OR / OR100 / «Оригинал»)."""
    return bool(_OR_RE.search(name or ""))


def cover_premium_ok(wb_title: str, name: str) -> bool:
    """Крышки: без «премиум» на WB премиум не берём (переплата). На WB премиум → премиум
    или OR (оригинал лучше премиума, Анри 21.10; выгодность проверяет тот, кто выбирает)."""
    if is_premium(wb_title):
        return is_premium(name) or is_or(name)
    return not is_premium(name)


def wb_full_size(title: str) -> bool:
    return bool(re.search(r"full\s*size|фулл?\s*сайз", (title or "").lower()))


BATTERY_TAGS = {"bc": "battery collection", "zevo": "zevo", "jcid": "jcid"}


def battery_tag(title: str) -> Optional[str]:
    """Фирма АКБ из названия WB: BC (Battery Collection), Zevo, JCID — только их берём на moba."""
    low = (title or "").lower()
    if has_bc(title) or "battery collection" in low:
        return "battery collection"
    for k in ("zevo", "jcid"):
        if k in low:
            return k
    return None


_MAKER_RE = re.compile(r"(?<![a-z0-9])(jk|zy|mnk|rj|hex|gx|sl)(?![a-z0-9])", re.I)


def display_maker(title: str) -> Optional[str]:
    """Производитель дисплея из названия WB (JK, ZY, MNK…): берём только у него (Анри: JK — Мастер Мобайл)."""
    if "диспл" not in (title or "").lower():
        return None
    m = _MAKER_RE.search(title or "")
    return m.group(1).lower() if m else None


def maker_ok(wb_title: str, name: str) -> bool:
    mk = display_maker(wb_title)
    return not mk or (display_maker(name) == mk)


def moba_skip_reason(wb_title: str) -> Optional[str]:
    """Аккумуляторы на moba — только BC / Zevo / JCID (Анри, 18.10). Остальные — SMG, Liberti, Мастер Мобайл."""
    wb = parse_wb_title(wb_title)
    if is_battery(wb) and not battery_tag(wb_title):
        return "аккумулятор не BC/Zevo/JCID — берём не на moba (Smg777 / Liberti / Мастер Мобайл)"
    return None


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
    skip = moba_skip_reason(wb_title)
    if skip:
        return {"wb": wb, "items": [], "hidden": 0, "queries": [], "skip_reason": skip}

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

    # ------- прайс moba (data/moba_price.json): если поиск moba не нашёл модель — ищем по артикулам из прайса ---- #
    if not any(model_matches(r["name"], wb) for r in raw_results):
        for art in catalog_articles(wb, limit=4):
            try:
                res = await moba_client.search(art, 5)
            except Exception as exc:  # noqa: BLE001
                logger.warning("search(art %s) упал: %s", art, exc)
                continue
            for r in res:
                url = r.get("url")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    raw_results.append({"url": url, "name": r.get("name") or "", "price": r.get("price")})
            queries.append(art)

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
        # крышки (правило Анри 18.10): без «премиум» на WB премиум не берём ВООБЩЕ (переплата), и наоборот;
        # «узкое отверстие» — только если на WB «узк»
        if (wb.get("type") or "").startswith(("крышк", "корпус")):
            if not cover_premium_ok(wb_title, r["name"]) or is_narrow(r["name"]) != is_narrow(wb_title):
                rejected.append({"url": r["url"], "name": r["name"], "reason": "premium_or_narrow"})
                continue
        # шлейфы/платы/разъёмы: премиум (с компонентами / оригинал) только если на WB премиум
        _t = wb.get("type") or ""
        if is_part(_t) and not part_func_ok(wb_title, r["name"], _t):
            rejected.append({"url": r["url"], "name": r["name"], "reason": "part_func"})
            continue
        if premium_rule_applies(wb.get("type") or "") and is_premium_part(r["name"]) != is_premium_part(wb_title):
            rejected.append({"url": r["url"], "name": r["name"], "reason": "part_premium"})
            continue
        # Производитель дисплея на WB (JK, ZY…) → только он
        if not maker_ok(wb_title, r["name"]):
            rejected.append({"url": r["url"], "name": r["name"], "reason": "maker"})
            continue
        # Дисплей iPhone «площадка под IC» на WB → только с площадкой
        if "площадк" in wb_title.lower() and "площадк" not in r["name"].lower():
            rejected.append({"url": r["url"], "name": r["name"], "reason": "ic_pad"})
            continue
        # АКБ: коды на WB и у поставщика не должны конфликтовать (BLP925 ≠ BLP975)
        if is_battery(wb) and codes_conflict(wb_title, r["name"]):
            rejected.append({"url": r["url"], "name": r["name"], "reason": "battery_code"})
            continue
        # OLED: на WB «Full Size» — только Full Size
        if wb.get("quality") == "oled" and wb_full_size(wb_title) and "full size" not in r["name"].lower():
            rejected.append({"url": r["url"], "name": r["name"], "reason": "not_full_size"})
            continue
        if wb_wants_frame(wb_title) and cls.get("frame") is not True:
            # на WB «с рамкой» — значит и на moba только с рамкой
            if not show_all:
                hidden_quality += 1
                rejected.append({"url": r["url"], "name": r["name"], "reason": "frame"})
                continue
        wb_color = detect_color(wb_title)
        moba_color = detect_color(r["name"])
        if not colors_compatible(wb_color, moba_color):
            # другой цвет — только в «Показать все»
            if not show_all:
                hidden_quality += 1
                rejected.append({"url": r["url"], "name": r["name"], "reason": "color"})
                continue
        if not show_all and not is_battery(wb) and not is_part(wb.get("type") or ""):
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

    # ------- обогащение (сначала самые дешёвые по цене поиска) ------- #
    matched.sort(key=lambda r: int(r.get("price") or 10**9))
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

    # ------- сортировка (правило Анри 18.10): ВСЕГДА самый дешёвый подходящий ------- #
    # копия/оригинал/OLED/крышки/АКБ — одинаково: в Митино → рамка как на WB → не «нелюбимое» → цена.
    # Для АКБ при равной цене — Battery Collection.
    _wf = wb_wants_frame(wb_title)
    is_back = (wb.get("type") or "").startswith(("крышк", "корпус")) or is_part(wb.get("type") or "")
    batt = is_battery(wb)
    enriched.sort(key=lambda x: (
        0 if x["mitino"] == "yes" else 1,
        0 if (batt or is_back or bool(x.get("frame")) == _wf) else 1,
        1 if float(x["_score"] or 0.0) < -2 else 0,
        int(x["price"] or 10**9),
        0 if (batt and has_bc(x["name"])) else 1,
    ))

    # ------- recommended: первый, если он в Митино и не «нелюбимый» ------- #
    for it in enriched:
        it["recommended"] = False
    if enriched:
        first = enriched[0]
        ok_q = batt or is_back or first["quality"] in (wb["quality"],) or \
            (first["quality"] == "unknown" and wb["quality"] == "copy")
        if first["mitino"] == "yes" and float(first["_score"] or 0.0) >= -2 and ok_q:
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
