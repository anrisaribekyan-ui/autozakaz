# unit_econ.py
"""
Цена, по которой МЫ продали (с нашей скидкой, БЕЗ СПП), и примерная прибыль с заказа.

Цена продажи (по порядку):
1. WB «Цены и скидки» API: discountedPrice по nmId — наша цена после нашей скидки, СПП WB туда не входит.
   Нужен токен с категорией «Цены и скидки». Если у токена магазина её нет — шаг пропускается.
2. Поле заказа salePrice (если WB его прислал) / convertedPrice — запасной вариант, помечается «≈ из заказа».

Прибыль ≈ продали − расходы WB − реклама − налог (доли по магазину из WB Dashboard) − закупка − 100 ₽ упаковка.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx

log = logging.getLogger(__name__)
PRICES_URL = "https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter"
_cache: dict[tuple[str, int], tuple[float, Optional[float]]] = {}
_no_scope: set[str] = set()


def _f(env: str) -> Optional[float]:
    v = os.getenv(env, "").strip().replace(",", ".")
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _prices_token(token: str) -> str:
    """Токен «Цены и скидки» для магазина: WB_<X>_PRICES, если задан, иначе сам токен."""
    if not token:
        return token
    for k, v in os.environ.items():
        if k.endswith("_PRICES") and v.strip() and os.environ.get(k[:-7], "").strip() == token.strip():
            return v.strip()
    return token


async def seller_price(token: str, nm_id: Optional[int], raw: Optional[dict] = None,
                       client: Optional[httpx.AsyncClient] = None) -> tuple[Optional[float], str]:
    """(цена продажи без СПП, откуда взята).

    Нужен токен с категорией «Цены и скидки». Если основной токен магазина (WB_SHOP4 и т.п.) её не имеет,
    можно положить отдельный в .env с суффиксом _PRICES (WB_SHOP4_PRICES=…) — возьмётся он."""
    token = _prices_token(token)
    if token and nm_id and token not in _no_scope:
        key = (token[-12:], int(nm_id))
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < 3 * 3600 and hit[1]:
            return hit[1], "наша цена"
        own = client is None
        c = client or httpx.AsyncClient(timeout=20)
        try:
            r = await c.get(PRICES_URL, params={"limit": 1, "filterNmID": int(nm_id)},
                            headers={"Authorization": token})
            if r.status_code in (401, 403):
                _no_scope.add(token)
                log.warning("Цены и скидки API: у токена нет доступа (HTTP %d) — беру цену из заказа", r.status_code)
            elif r.status_code == 200:
                goods = ((r.json().get("data") or {}).get("listGoods") or [])
                if goods:
                    sizes = goods[0].get("sizes") or []
                    p = sizes[0].get("discountedPrice") if sizes else None
                    if p is None and goods[0].get("price") is not None:
                        p = goods[0]["price"] * (100 - (goods[0].get("discount") or 0)) / 100
                    if p:
                        _cache[key] = (time.time(), float(p))
                        return float(p), "наша цена"
        except Exception as exc:  # noqa: BLE001
            log.warning("Цены и скидки API: %s", exc)
        finally:
            if own:
                await c.aclose()
    raw = raw or {}
    for k in ("salePrice", "convertedPrice", "price"):
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v:
            return round(v / 100.0, 2), ("≈ из заказа" if k == "salePrice" else "⚠️ цена покупателя со скидкой WB — неточно")
    return None, "нет"


# Доли от выручки по магазинам — из WB Dashboard (19.10.2026, «Финансы за период»):
#   Гор:  расходы WB 48,6% · реклама 10,5% · налог УСН 4,1% выручки
#   Анри: расходы WB 53,4% · реклама 8,7%  · налог УСН 4,2% выручки
# Лёша/Борода — пока как у Анри. Переопределить: .env WB_EXPENSES_PCT_<KEY>, ADS_PCT_<KEY>, TAX_PCT_<KEY>
# (KEY = SHOP2 / SHOP3 / SHOP4 / GOR) или общие WB_EXPENSES_PCT / ADS_PCT / TAX_PCT.
SHOP_RATES = {
    "gor":   {"wb": 48.6, "ads": 10.5, "tax": 4.1},
    "shop2": {"wb": 53.4, "ads": 8.7, "tax": 4.2},
    "shop3": {"wb": 53.4, "ads": 8.7, "tax": 4.2},
    "shop4": {"wb": 53.4, "ads": 8.7, "tax": 4.2},
}
_DEFAULT = {"wb": 53.4, "ads": 8.7, "tax": 4.2}


def rates(shop: Optional[str] = None) -> dict:
    base = dict(SHOP_RATES.get((shop or "").lower(), _DEFAULT))
    sk = (shop or "").upper()
    for k, env in (("wb", "WB_EXPENSES_PCT"), ("ads", "ADS_PCT"), ("tax", "TAX_PCT")):
        v = _f(f"{env}_{sk}") if sk else None
        if v is None:
            v = _f(env)
        if v is not None:
            base[k] = v
    pack = _f("PACK_RUB")
    base["pack"] = 100.0 if pack is None else pack
    return base


def profit(sale: Optional[float], cost: Optional[float], shop: Optional[str] = None) -> dict:
    """Продали − расходы WB % − реклама % − налог % − закупка − упаковка (доли — по магазину, из WB Dashboard)."""
    out: dict = {"sale": sale, "cost": cost, "parts": [], "missing": []}
    if not sale or cost is None:
        out["profit"] = None
        return out
    r = rates(shop)
    wb = round(sale * r["wb"] / 100, 0)
    ads = round(sale * r["ads"] / 100, 0)
    tax = round(sale * r["tax"] / 100, 0)
    total = sale - wb - ads - tax - cost - r["pack"]
    out["parts"] = [(f"WB {r['wb']:g}%", -wb), (f"реклама {r['ads']:g}%", -ads), (f"налог {r['tax']:g}%", -tax),
                    ("закупка", -cost), ("упаковка", -r["pack"])]
    out["profit"] = round(total, 0)
    out["margin_pct"] = round(total / sale * 100)
    return out


def fmt_rub(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:,.0f}".replace(",", " ")


def profit_line(sale: Optional[float], src: str, cost: Optional[float], shop: Optional[str] = None) -> str:
    """«Продали 2 353 ₽ · закупка 690 · … → ≈ +1 120 ₽ (48%)»."""
    p = profit(sale, cost, shop)
    if p["profit"] is None:
        return f"Продали {fmt_rub(sale)} ₽ ({src})"
    parts = " · ".join(f"{n} {fmt_rub(-v)}" for n, v in p["parts"])
    sign = "+" if p["profit"] >= 0 else "−"
    warn = " ⚠️ в минус" if p["profit"] < 0 else ""
    miss = f" (без: {', '.join(p['missing'])})" if p["missing"] else ""
    return (f"Продали {fmt_rub(sale)} ₽ ({src}) · {parts} → ≈ {sign}{fmt_rub(abs(p['profit']))} ₽ "
            f"({p['margin_pct']}%){warn}{miss}")
