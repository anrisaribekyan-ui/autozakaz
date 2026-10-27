# suppliers_ext.py
"""
Поиск товара вне moba: Мастер Мобайл (master-mobile.ru) и poisk123.ru.
Только чтение — ничего не заказывает.

Главный вопрос: «есть ли это в Митино, и где именно».
- Мастер Мобайл: магазин id=10 «ТК Митинский радиорынок» (пав. 535).
- poisk123: фильтр района «Митинский р-к» (cookie search_filter_2097=2__),
  затем по каждой позиции — остатки по точкам (ajax show-rest-info), Митино = точка с «Мит» в названии.

Разведано 16.10.2026 в браузере:
- MM поиск: GET /catalog/?q=<запрос> → .catalog__type-block__item-container (data-id),
  a.preview-item__name, .payment-price__value--basic / --club, .preview-item__available-value,
  .preview-item__info-vendor («Арт.: 064899»).
- MM остатки: POST /local/components/sp/card.amount/templates/.default/ajax.php
  region=&action=getStores&productId=<id> → [{ID, SHOP, PRODUCT_AMOUNT, AVAILABILITY_STATE}], без входа.
- poisk123: GET /s/moskva/?s=<запрос>&per_page=100 → tr[itemtype=…/Product]; остатки:
  POST /s/ ajax=show-rest-info&id=<shop-item>&city=2097&hash= → {arr_items:[{name, rest}]}.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from matcher import (allowed, cover_premium_ok, is_or, codes_conflict, maker_ok, colors_compatible, detect_color, is_battery, is_narrow, is_part, is_premium,
                     is_premium_part, part_func, part_func_ok, premium_rule_applies,
                     model_matches, parse_wb_title, wb_wants_frame)

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MM_BASE = "https://master-mobile.ru"
MM_MITINO_STORE_ID = "10"
P123_BASE = "https://poisk123.ru"
P123_CITY = "2097"          # Москва
P123_MITINO = "2097-2"      # «Митинский р-к»


def _digits(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    d = re.sub(r"[^\d]", "", s)
    return int(d) if d else None


def classify_ext(name: str) -> dict:
    """Качество по названию у сторонних поставщиков (скобки, а не « - »)."""
    low = (name or "").lower()
    if "oled" in low or "amoled" in low:
        q = "oled"
    elif re.search(r"orig|оригинал|(?<![a-zа-яё])or(?:\s?100)?(?![a-zа-яё])|\(sp\)|service pack|сервисный|100\s?%", low):
        q = "original"
    elif re.search(r"in-?cell|incell|tft|ips|копия|premium|премиум|hq|оптима|optima|стандарт", low):
        q = "copy"
    else:
        q = "unknown"
    # moba «модуль» = в рамке; MM «+ рамка»
    frame = bool(re.search(r"в рамке|с рамкой|модуль|\+\s*рамк|в раме\b|с рамой", low))
    return {"quality": q, "frame": frame}


def _fits(name: str, wb: dict, wb_title: str) -> tuple[bool, str]:
    """(подходит по модели/цвету/рамке, качество). Качество проверяем отдельно."""
    norm = re.sub(r"\s\+\s", " и ", name or "")          # MM: «A54 5G + тачскрин» ≠ «A54 5G Plus»
    if (wb.get("type") or "").startswith("диспл") and re.match(r"\s*lcd\s", norm, re.I) \
            and "диспле" not in norm.lower():
        norm = "Дисплей " + norm.strip()[4:]                # «LCD Tecno camon 12 air Service Pack»
    if not model_matches(norm, wb):
        # АКБ часто названы по коду (BM52, EB-BA530ABE): совпал код — подходит
        if not (is_battery(wb) and battery_code_match(wb_title, name)):
            return False, ""
    if is_battery(wb) and codes_conflict(wb_title, name):
        return False, ""
    wc, nc = detect_color(wb_title), detect_color(name)
    if not colors_compatible(wc, nc):
        return False, ""
    cls = classify_ext(name)
    if wb_wants_frame(wb_title) and not cls["frame"] and not (wb.get("type") or "").startswith("рамк"):
        return False, ""
    if (wb.get("type") or "").startswith(("крышк", "корпус")):
        # премиум/узкое — только если так на WB (Анри, 18.10); у крышек пометки качества нет
        if not cover_premium_ok(wb_title, name) or is_narrow(name) != is_narrow(wb_title):
            return False, ""
        return True, "unknown"
    if is_part(wb.get("type") or ""):
        _t = wb.get("type") or ""
        if not part_func_ok(wb_title, name, _t):
            return False, ""
        if premium_rule_applies(_t) and is_premium_part(name) != is_premium_part(wb_title):
            return False, ""
        return True, "unknown"
    if is_battery(wb):
        return True, "unknown"   # у АКБ «100%» — это ёмкость, а не «оригинал»
    return True, cls["quality"]


MM_PREMIUM_RE = re.compile(r"premium|премиум", re.I)


def mm_battery_ok(name: str, wb: dict) -> bool:
    """АКБ в Мастер Мобайле — только «(Premium)»: High+, SP, без пометки — хуже (Анри, 18.10)."""
    return not is_battery(wb) or bool(MM_PREMIUM_RE.search(name or ""))


def battery_code_match(wb_title: str, name: str) -> bool:
    def nc(x: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", x.upper())
    codes = {nc(m) for m in re.findall(r"\b[A-Za-z]{1,4}-?[A-Za-z]{0,4}\d[A-Za-z0-9-]{1,10}\b", wb_title or "")}
    codes = {c for c in codes if len(c) >= 4 and re.search(r"[A-Z]", c)}
    if not codes:
        return False
    toks = {nc(t) for t in re.findall(r"[A-Za-z0-9-]+", name or "")}
    return bool(codes & toks) or any(t.startswith(c) and len(t) - len(c) <= 2 for c in codes for t in toks)


# ---------------------------------------------------------------------- #
# Мастер Мобайл

_LAST: dict[str, float] = {}
MIN_INTERVAL = {"mm": 0.5, "p123": 6}   # секунды между запросами к одному сайту (иначе 429)
_LOCKS: dict[str, asyncio.Lock] = {}
_PAUSE: dict[str, float] = {}           # сайт ответил 429 → все запросы к нему ждут до этого момента
_BACKOFF: dict[str, float] = {}


async def _req(c: httpx.AsyncClient, site: str, method: str, url: str, **kw) -> httpx.Response:
    """Запрос с паузой между обращениями; на 429 — общая пауза для сайта (30 с → до 5 мин) и повтор."""
    import time
    lock = _LOCKS.setdefault(site, asyncio.Lock())
    r = None
    for attempt in range(6):
        async with lock:
            wait = max(_LAST.get(site, 0) + MIN_INTERVAL.get(site, 0), _PAUSE.get(site, 0)) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            _LAST[site] = time.monotonic()
        try:
            r = await c.request(method, url, **kw)
        except httpx.TransportError:
            if attempt == 5:
                raise
            await asyncio.sleep(10)
            continue
        if r.status_code in (429, 502, 503) and attempt < 5:
            ra = r.headers.get("Retry-After")
            back = float(ra) if ra and ra.isdigit() else min(_BACKOFF.get(site, 15) * 2, 300)
            _BACKOFF[site] = back
            _PAUSE[site] = time.monotonic() + back
            logger.warning("%s: %s, пауза %.0f с", site, r.status_code, back)
            continue
        if r.status_code < 400:
            _BACKOFF[site] = 15
        return r
    return r
    return r

# ---------------------------------------------------------------------- #
class MasterMobileClient:
    def __init__(self, timeout: float = 25.0) -> None:
        self._c = httpx.AsyncClient(base_url=MM_BASE, timeout=timeout, follow_redirects=True,
                                    headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})

    async def close(self) -> None:
        await self._c.aclose()

    async def search(self, query: str) -> list[dict]:
        r = await _req(self._c, "mm", "GET", "/catalog/", params={"q": query})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        out: list[dict] = []
        for el in soup.select(".catalog__type-block__item-container"):
            a = el.select_one("a.preview-item__name")
            if not a:
                continue
            basic = el.select_one(".payment-price__value--basic")
            club = el.select_one(".payment-price__value--club")
            avail = el.select_one(".preview-item__available-value")
            vendor = el.select_one(".preview-item__info-vendor")
            out.append({
                "supplier": "Мастер Мобайл",
                "product_id": el.get("data-id"),
                "name": a.get_text(" ", strip=True),
                "url": MM_BASE + a.get("href", ""),
                "price": _digits(basic.get_text(" ", strip=True).split("/")[0]) if basic else None,
                "price_club": _digits(club.get_text(" ", strip=True)) if club else None,
                "avail_text": avail.get_text(" ", strip=True) if avail else "",
                "article": (vendor.get_text(" ", strip=True).replace("Арт.:", "").strip() if vendor else ""),
            })
        return out

    async def stores(self, product_id: str, page_url: Optional[str] = None) -> list[dict]:
        """Без Referer/Origin и сессии сайт отвечает {"error":"Access denied"} (16.10, с сервера)."""
        ref = page_url or f"{MM_BASE}/catalog/"
        if not self._c.cookies:
            try:
                await _req(self._c, "mm", "GET", ref)  # получить сессию (PHPSESSID)
            except Exception as exc:  # noqa: BLE001
                logger.debug("MM warmup: %s", exc)
        r = await _req(self._c, "mm", "POST",
            "/local/components/sp/card.amount/templates/.default/ajax.php",
            data={"region": "", "action": "getStores", "productId": str(product_id)},
            headers={"Referer": ref, "Origin": MM_BASE, "X-Requested-With": "XMLHttpRequest",
                     "Bx-ajax": "true", "Accept": "application/json, text/javascript, */*; q=0.01"},
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            raise RuntimeError(f"ответ MM: {data}")
        return [s for s in data if isinstance(s, dict) and s.get("ID")]

    async def mitino_amount(self, product_id: str, page_url: Optional[str] = None) -> Optional[int]:
        """Остаток в Митино; None — не удалось узнать (магазина Митино нет в ответе / ошибка)."""
        try:
            stores = await self.stores(product_id, page_url)
            for s in stores:
                if str(s.get("ID")) == MM_MITINO_STORE_ID or "митин" in str(s.get("SHOP", "")).lower():
                    return int(s.get("PRODUCT_AMOUNT") or 0)
            logger.warning("MM stores(%s): нет магазина Митино в ответе: %s", product_id,
                           [(s.get("ID"), s.get("PRODUCT_AMOUNT")) for s in stores][:12])
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("MM stores(%s): %s", product_id, exc)
            return None


# ---------------------------------------------------------------------- #
# poisk123
# ---------------------------------------------------------------------- #
class Poisk123Client:
    def __init__(self, timeout: float = 25.0, mitino_only: bool = True) -> None:
        cookies = {"city2": P123_CITY}
        if mitino_only:
            cookies[f"search_filter_{P123_CITY}"] = "2__"
        self._c = httpx.AsyncClient(base_url=P123_BASE, timeout=timeout, follow_redirects=True,
                                    headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"},
                                    cookies=cookies)
        self._hash = ""

    async def close(self) -> None:
        await self._c.aclose()

    async def search(self, query: str) -> list[dict]:
        """Позиции только тех магазинов, у которых есть точка в Митино (фильтр района)."""
        r = await _req(self._c, "p123", "GET", "/s/moskva/", params={"s": query, "per_page": 100})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        m = re.search(r"hash\s*=\s*'([0-9a-f]+)'", r.text)
        if m:
            self._hash = m.group(1)
        districts: dict[str, set[str]] = {}
        for d in soup.select(".l-shops"):
            inp = d.select_one("input")
            if inp is not None:
                districts[inp.get("value", "")] = set(filter(None, (d.get("data-district") or "").split(",")))
        out: list[dict] = []
        for tr in soup.select('tr[itemtype="http://schema.org/Product"]'):
            nm = tr.select_one(".namebox-rrtt")
            shop_td = tr.select_one('[data-item="Магазин"]')
            if not nm or not shop_td:
                continue
            name = nm.contents[0].strip() if nm.contents and isinstance(nm.contents[0], str) else nm.get_text(" ", strip=True)
            shop = re.sub(r"\s+", " ", shop_td.get_text(" ", strip=True).replace("Инфо", "")).strip()
            rest_td = tr.select_one('[data-item="Наличие"]')
            opt_td = tr.select_one('[data-item="Цена опт"]')
            price = tr.select_one("[itemprop=price]")
            info = tr.select_one("a.show-rest-info")
            buy = tr.select_one("a.buy-link")
            href = buy.get("href", "") if buy else ""
            mshop = re.match(r"/buy/(\d+)/", href)
            out.append({
                "supplier": "poisk123",
                "name": name,
                "shop": shop,
                "shop_id": mshop.group(1) if mshop else None,
                "rest": rest_td.get_text(" ", strip=True) if rest_td else "",
                "opt": _digits(opt_td.get_text(strip=True)) if opt_td else None,
                "price": _digits(price.get_text(strip=True)) if price else None,
                "rid": info.get("data-id") if info else None,
                "url": P123_BASE + href if href else "",
                "_districts": districts.get(mshop.group(1)) if mshop else None,
            })
        return out

    async def points(self, rid: str) -> list[dict]:
        r = await _req(self._c, "p123", "POST", "/s/", data={"ajax": "show-rest-info", "id": rid,
                                            "city": P123_CITY, "hash": self._hash})
        r.raise_for_status()
        j = r.json()
        if j.get("hash"):
            self._hash = j["hash"]
        return [{"name": it.get("name"), "rest": it.get("rest")} for it in j.get("arr_items") or []]

    async def mitino_rest(self, row: dict) -> tuple[Optional[str], str]:
        """(остаток в Митино или None, откуда известно)."""
        if row.get("rid"):
            try:
                for p in await self.points(row["rid"]):
                    if "мит" in str(p["name"]).lower():
                        return str(p["rest"]), p["name"]
                return None, "нет точки Митино в остатках"
            except Exception as exc:  # noqa: BLE001
                return None, f"ошибка остатков: {exc}"
        m = re.search(r"\(([^)]+)\)", row["shop"])
        if m:
            return (row["rest"], row["shop"]) if "мит" in m.group(1).lower() else (None, row["shop"])
        if row.get("_districts") == {P123_MITINO}:
            return row["rest"], f"{row['shop']} (только Митино)"
        return None, "точка не указана"


# ---------------------------------------------------------------------- #
# Общий поиск «где ещё есть»
# ---------------------------------------------------------------------- #
def _p123_query(wb: dict) -> str:
    models = wb.get("models") or []
    m = models[0] if models else ""
    brand = wb.get("brand") or ""
    if brand == "xiaomi" or m.split(" ")[0] in ("redmi", "poco", "mi"):
        return m
    return f"{brand} {m}".strip()


async def find_elsewhere(wb_title: str, mm: Optional[MasterMobileClient] = None,
                         p123: Optional[Poisk123Client] = None, check_points: bool = True) -> dict:
    wb = parse_wb_title(wb_title)
    own_mm, own_p = mm is None, p123 is None
    mm = mm or MasterMobileClient()
    p123 = p123 or Poisk123Client()
    res: dict = {"wb_quality": wb.get("quality"), "mitino": [], "mitino_other_quality": [],
                 "mm_not_mitino": [], "errors": [], "queries": {}}
    try:
        # --- Мастер Мобайл ---
        q_mm = f"{wb.get('type') or ''} {_p123_query(wb)}".strip()
        res["queries"]["mm"] = q_mm
        try:
            items = await mm.search(q_mm) if wb.get("models") else []
        except Exception as exc:  # noqa: BLE001
            items = []
            res["errors"].append(f"Мастер Мобайл: {exc}")
        for it in items:
            ok, q = _fits(it["name"], wb, wb_title)
            if not ok or not mm_battery_ok(it["name"], wb) or not maker_ok(wb_title, it["name"]):
                continue
            it["quality"] = q
            if not it.get("price") and "нет в наличии" in (it.get("avail_text") or "").lower():
                continue  # «Нет в наличии / Под заказ» — пропускаем
            it["mitino_qty"] = await mm.mitino_amount(it["product_id"], it.get("url"))
            good_q = allowed(wb["quality"], q)
            if it["mitino_qty"]:
                (res["mitino"] if good_q else res["mitino_other_quality"]).append(it)
            elif it["mitino_qty"] is None and good_q:
                res["errors"].append(f"Мастер Мобайл: остаток в Митино не определился ({it['name'][:60]})")
                res["mm_not_mitino"].append(it)
            elif good_q:
                res["mm_not_mitino"].append(it)

        # --- poisk123 (только магазины с точкой в Митино) ---
        q_p = _p123_query(wb)
        res["queries"]["p123"] = q_p
        try:
            rows = await p123.search(q_p) if q_p else []
        except Exception as exc:  # noqa: BLE001
            rows = []
            res["errors"].append(f"poisk123: {exc}")
        for row in rows:
            if row["shop"].lower().startswith(("moba", "master mobile")):
                continue  # их проверяем напрямую
            ok, q = _fits(row["name"], wb, wb_title)
            if not ok:
                continue
            if check_points:
                rest, where = await p123.mitino_rest(row)
            else:
                rest, where = row.get("rest") or "?", row["shop"]
            row.update({"quality": q, "mitino_rest": rest, "mitino_point": where})
            row.pop("_districts", None)
            if rest:
                (res["mitino"] if allowed(wb["quality"], q) else res["mitino_other_quality"]).append(row)
    finally:
        if own_mm:
            await mm.close()
        if own_p:
            await p123.close()
    # Порядок Анри: сначала Мастер Мобайл, потом остальные точки Митино; внутри — по цене
    key = lambda x: (0 if x.get("supplier") == "Мастер Мобайл" else 1,  # noqa: E731
                     x.get("price_club") or x.get("opt") or x.get("price") or 10**9)
    for k in ("mitino", "mitino_other_quality", "mm_not_mitino"):
        res[k].sort(key=key)
    return res


_Q_RU = {"copy": "копия", "original": "оригинал", "oled": "OLED", "unknown": "?"}


def format_elsewhere(res: dict, limit: int = 5) -> str:
    def line(it: dict) -> str:
        if it["supplier"] == "Мастер Мобайл":
            pr = f"{it.get('price_club') or it.get('price')} ₽" + (" (клуб.)" if it.get("price_club") else "")
            return f"• Мастер Мобайл, Митино {it.get('mitino_qty')} шт · {pr} · {it['name']}"
        pr = f"{it.get('opt')} опт / {it.get('price')} ₽" if it.get("opt") else f"{it.get('price')} ₽"
        smg = " (SMG → в «Доску»)" if it["shop"].lower().startswith("smg") else ""
        return f"• {it['shop']}{smg}, {it.get('mitino_point')}: {it.get('mitino_rest')} · {pr} · {it['name']}"

    parts: list[str] = []
    if res["mitino"]:
        parts.append("📍 Есть в Митино:")
        parts += [line(x) for x in res["mitino"][:limit]]
    else:
        parts.append(f"🚫 Нет нигде в Митино ({_Q_RU.get(res['wb_quality'], '')}): ни moba, ни Мастер Мобайл, ни poisk123")
    if res["mitino_other_quality"]:
        parts.append("В Митино есть только другое качество:")
        parts += [line(x) + f" [{_Q_RU.get(x.get('quality'), '?')}]" for x in res["mitino_other_quality"][:limit]]
    if not res["mitino"] and res["mm_not_mitino"]:
        x = res["mm_not_mitino"][0]
        pr = x.get('price_club') or x.get('price')
        parts.append(f"Мастер Мобайл не в Митино{f': {pr} ₽' if pr else ''} · {x['name']}")
    if res["errors"]:
        parts.append("⚠️ " + "; ".join(res["errors"]))
    return "\n".join(parts)


if __name__ == "__main__":  # ручная проверка: venv/bin/python suppliers_ext.py "Дисплей для ..."
    import sys
    logging.basicConfig(level=logging.INFO)
    for t in sys.argv[1:] or ["Дисплей для Tecno Camon 12 Air (Черный) In-Cell",
                              "Дисплей для Samsung Galaxy A54 5G OLED в раме",
                              "Дисплей для Huawei MediaPad T1 7 Копия"]:
        r = asyncio.run(find_elsewhere(t))
        print("\n==", t, r["queries"])
        print(format_elsewhere(r))
