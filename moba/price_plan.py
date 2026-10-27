"""План цен на WB «в плюс» по всем товарам магазина (только расчёт, в WB ничего не меняет).

Для каждого товара: наша цена сейчас (после скидки продавца), себес (закупка опт по прайсам moba / Мастер Мобайл
/ SMG / Liberti + 100 ₽ упаковка), прибыль сейчас, цена «в ноль» и цена под целевую маржу.
Доли расходов WB / рекламы / налога — по магазину (unit_econ.rates, из WB Dashboard).

Запуск:  venv/bin/python price_plan.py                 — все магазины, маржа 15%
         venv/bin/python price_plan.py --margin 18 --shops shop2,shop4
         ... --apply  — записать новые цены в WB (только при PRICE_APPLY_LIVE=1 в .env)
Результат: data/price_plan_<дата>.xlsx (лист на магазин + «Итог») и в бот Анри.

Правила записи (Анри 21.10: «везде ±18% прибыли»):
  * меняем только если маржа сейчас вне коридора цель ± --band п.п. (по умолчанию 3);
  * скидку продавца оставляем как есть, меняем цену до скидки;
  * наборы «2+1» и товары без себеса не трогаем;
  * за один раз цена не падает больше чем на 30% (карантин цен WB) и не растёт больше чем на 60%
    (такие помечаются «проверь себес» — часто это ошибка подбора).
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
import openpyxl
from dotenv import load_dotenv
from openpyxl.styles import Font, PatternFill

import gor_costs as G
from unit_econ import _prices_token, rates

SHOPS = [
    {"key": "shop2", "title": "Анри", "env": "WB_SHOP2"},
    {"key": "shop3", "title": "Лёша", "env": "WB_SHOP3"},
    {"key": "shop4", "title": "Борода", "env": "WB_SHOP4"},
    {"key": "gor", "title": "Гор", "env": "WB_GOR"},
]
PRICES_URL = "https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter"
UPLOAD_URL = "https://discounts-prices-api.wildberries.ru/api/v2/upload/task"
MAX_DOWN, MAX_UP = 0.30, 0.60


async def upload(c: httpx.AsyncClient, token: str, items: list[dict]) -> list[str]:
    """Новые цены в WB пачками по 1000. Возвращает id задач / ошибки."""
    out = []
    for i in range(0, len(items), 1000):
        part = items[i:i + 1000]
        for _ in range(5):
            r = await c.post(UPLOAD_URL, json={"data": part}, headers={"Authorization": token})
            if r.status_code == 429:
                await asyncio.sleep(10)
                continue
            break
        if r.status_code == 200:
            out.append(f"ok task {((r.json() or {}).get('data') or {}).get('id')}")
        else:
            out.append(f"ошибка {r.status_code}: {r.text[:200]}")
        await asyncio.sleep(1)
    return out
CARDS_URL = "https://content-api.wildberries.ru/content/v2/get/cards/list"


def _retry_after(r: httpx.Response) -> int:
    """WB: X-Ratelimit-Retry — сколько секунд ждать. Повтор раньше срока продлевает блокировку."""
    for h in ("x-ratelimit-retry", "x-ratelimit-reset", "retry-after"):
        try:
            v = int(float(r.headers.get(h, "")))
            if v > 0:
                return v + 2
        except ValueError:
            pass
    return 30


async def goods(c: httpx.AsyncClient, token: str) -> list[dict]:
    out, offset = [], 0
    while True:
        r = await c.get(PRICES_URL, params={"limit": 1000, "offset": offset}, headers={"Authorization": token})
        if r.status_code == 429:
            wait = _retry_after(r)
            print(f"   цены WB: 429, жду {wait} с (как просит WB)", flush=True)
            await asyncio.sleep(wait)
            continue
        r.raise_for_status()
        part = ((r.json().get("data") or {}).get("listGoods") or [])
        out += part
        print(f"   цены WB: {len(out)}", flush=True)
        if len(part) < 1000:
            return out
        offset += 1000
        await asyncio.sleep(6.5)


async def titles(c: httpx.AsyncClient, token: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    cursor: dict = {"limit": 100}
    while True:
        body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
        r = await c.post(CARDS_URL, json=body, headers={"Authorization": token})
        if r.status_code == 429:
            wait = _retry_after(r)
            print(f"   карточки WB: 429, жду {wait} с", flush=True)
            await asyncio.sleep(wait)
            continue
        r.raise_for_status()
        d = r.json()
        for card in d.get("cards") or []:
            out[int(card["nmID"])] = {"title": card.get("title") or "", "vendor": card.get("vendorCode") or ""}
        cur = d.get("cursor") or {}
        if len(out) % 2000 < 100:
            print(f"   карточки WB: {len(out)}", flush=True)
        if not d.get("cards"):
            return out
        if (cur.get("total") or 0) < 100:
            return out
        cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
        await asyncio.sleep(0.7)


def purchase(title: str, owner: str) -> dict | None:
    t = G.gor_title(title) if owner == "gor" else title
    wb = G.parse_wb_title(t)
    moba = None if (G.is_battery(wb) and not G.battery_tag(t)) else G.moba_any(t)
    ext = {"mm": G.mm_offline_best(t), "p123": None, "prio": None}
    return G.choose(t, moba, ext)


def need_price(cost: float, r: dict, margin_pct: float) -> float | None:
    k = 1 - (r["wb"] + r["ads"] + r["tax"] + margin_pct) / 100
    return None if k <= 0 else (cost + r["pack"]) / k


def round_price(p: float) -> int:
    """Красивая цена: вверх до …90 (до 1000 — до …9)."""
    if p < 1000:
        return int(math.ceil(p / 10) * 10 - 1)
    return int(math.ceil(p / 100) * 100 - 10)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--margin", type=float, default=15.0, help="целевая маржа, % от нашей цены")
    ap.add_argument("--shops", default=",".join(s["key"] for s in SHOPS))
    ap.add_argument("--band", type=float, default=3.0, help="коридор вокруг цели, п.п.")
    ap.add_argument("--apply", action="store_true", help="записать цены в WB")
    ap.add_argument("--no-tg", action="store_true")
    args = ap.parse_args()
    load_dotenv(".env")
    live = args.apply and os.getenv("PRICE_APPLY_LIVE", "0") == "1"
    tg_lines = [f"💰 <b>Цены под {args.margin:g}% {'(записано в WB)' if live else '(пробно)'}</b>"]
    want = set(args.shops.split(","))
    wbk = openpyxl.Workbook()
    summary = wbk.active
    summary.title = "Итог"
    summary.append(["Магазин", "Товаров", "Себес найден", "В минус сейчас", "Ниже целевой маржи",
                    "Доли: WB / реклама / налог", f"Целевая маржа {args.margin:g}%"])
    red, yellow, green = (PatternFill("solid", fgColor=c) for c in ("F8D7DA", "FFF2CC", "E2F0D9"))
    async with httpx.AsyncClient(timeout=40) as c:
        for shop in SHOPS:
            if shop["key"] not in want:
                continue
            main_tok = os.getenv(shop["env"], "").strip()
            price_tok = _prices_token(main_tok)
            if not main_tok:
                print(shop["title"], ": нет токена", shop["env"])
                continue
            t0 = time.time()
            try:
                gl = await goods(c, price_tok)
            except httpx.HTTPStatusError as exc:
                print(shop["title"], ": цены недоступны —", exc.response.status_code, "(нужен токен «Цены и скидки»,",
                      shop["env"] + "_PRICES)")
                continue
            try:
                tl = await titles(c, main_tok)
            except httpx.HTTPStatusError as exc:
                print(shop["title"], ": названия недоступны —", exc.response.status_code)
                tl = {}
            r = rates(shop["key"])
            ws = wbk.create_sheet(shop["title"])
            ws.append(["nmID", "Артикул", "Название", "Цена до скидки", "Скидка %", "Наша цена сейчас",
                       "Закупка опт", "Где", "Товар у поставщика", "Прибыль сейчас ₽", "Маржа сейчас %",
                       "Цена в ноль", f"Цена под {args.margin:g}%", "Рекомендуемая цена (после скидки)",
                       "Цена до скидки при той же скидке", "Что делать", "Станет (до скидки)",
                       "Станет (после скидки)", "Маржа станет %"])
            n = found = minus = low = 0
            upd: list[dict] = []
            n_up = n_down = n_cap = 0
            for g in gl:
                nm = int(g["nmID"])
                info = tl.get(nm, {})
                title = info.get("title") or ""
                sz = (g.get("sizes") or [{}])[0]
                base = sz.get("price") or g.get("price")
                disc = g.get("discount") or 0
                ours = sz.get("discountedPrice") or (base * (100 - disc) / 100 if base else None)
                n += 1
                if n % 1000 == 0:
                    print(f"   {shop['title']}: посчитано {n} из {len(gl)}", flush=True)
                best = purchase(title, shop["key"]) if title else None
                row = [nm, info.get("vendor") or g.get("vendorCode"), title, base, disc, ours]
                if title and re.search(r"(?<!\d)2\s*\+\s*1(?!\d)", title):
                    ws.append(row + [None] * 9 + ["набор 2+1 — не трогаю"])
                    continue
                if not best or not ours:
                    ws.append(row + [None, None, None, None, None, None, None, None, None,
                                     "нет себеса — не нашёл у поставщиков" if title else "нет названия"])
                    continue
                found += 1
                cost = best["price"]
                prof = ours * (1 - (r["wb"] + r["ads"] + r["tax"]) / 100) - cost - r["pack"]
                mpct = prof / ours * 100
                p0 = need_price(cost, r, 0)
                pt = need_price(cost, r, args.margin)
                rec = round_price(pt) if pt else None
                base_new = round(rec / (1 - disc / 100)) if rec and disc < 100 else rec
                new_base = new_ours = new_m = None
                if prof < 0:
                    minus += 1
                if abs(mpct - args.margin) <= args.band or not rec or not base:
                    what, fill = "ок — в коридоре", green
                else:
                    tgt = rec
                    capped = False
                    if tgt < ours * (1 - MAX_DOWN):
                        tgt, capped = ours * (1 - MAX_DOWN), True
                    if tgt > ours * (1 + MAX_UP):
                        tgt, capped = ours * (1 + MAX_UP), True
                    tgt = round_price(tgt)
                    new_base = int(round(tgt / (1 - disc / 100))) if disc < 100 else tgt
                    new_ours = round(new_base * (100 - disc) / 100)
                    new_m = round((new_ours * (1 - (r["wb"] + r["ads"] + r["tax"]) / 100) - cost - r["pack"])
                                  / new_ours * 100, 1)
                    if new_base == base:
                        what, fill = "ок", green
                        new_base = None
                    else:
                        upd.append({"nmID": nm, "price": new_base, "discount": int(disc)})
                        if new_ours > ours:
                            n_up += 1
                            low += 1
                            what, fill = "поднять", (red if prof < 0 else yellow)
                        else:
                            n_down += 1
                            what, fill = "снизить", green
                        if capped:
                            n_cap += 1
                            what += " · упёрлось в лимит — проверь себес"
                ws.append(row + [cost, best["src"], best["name"], round(prof), round(mpct, 1),
                                 round(p0) if p0 else None, round(pt) if pt else None, rec, base_new, what,
                                 new_base, new_ours, new_m])
                ws.cell(ws.max_row, 16).fill = fill
            res = ""
            if live and upd:
                res = "; ".join(await upload(c, price_tok, upd))
            tg_lines.append(f"• <b>{shop['title']}</b>: товаров {n} · себес есть {found} · в минус сейчас {minus} · "
                            f"поднять {n_up} · снизить {n_down} · проверить себес {n_cap}"
                            + (f"\n   WB: {res}" if res else ""))
            for col, w in zip("ABCDEFGHIJKLMNOPQRS", (11, 14, 55, 9, 7, 9, 9, 16, 55, 9, 8, 9, 9, 11, 11, 28, 10, 10, 8)):
                ws.column_dimensions[col].width = w
            ws.freeze_panes = "D2"
            ws.auto_filter.ref = ws.dimensions
            for cell in ws[1]:
                cell.font = Font(bold=True)
            summary.append([shop["title"], n, found, minus, low,
                            f'{r["wb"]:g}% / {r["ads"]:g}% / {r["tax"]:g}%', ""])
            print(f'{shop["title"]}: товаров {n}, себес найден {found}, в минус {minus}, ниже цели {low} '
                  f'({time.time() - t0:.0f} с)')
    for cell in summary[1]:
        cell.font = Font(bold=True)
    for col, w in zip("ABCDEFG", (12, 9, 12, 14, 16, 22, 18)):
        summary.column_dimensions[col].width = w
    out = Path("data") / f"price_plan_{datetime.now():%Y-%m-%d}.xlsx"
    wbk.save(out)
    print("готово:", out)
    text = "\n".join(tg_lines)
    print(text.replace("<b>", "").replace("</b>", ""))
    if not args.no_tg:
        from test_sborka import tg_send
        await tg_send(text, [out], "BOT_MAIN", "TG_ANRI_ID", parse_mode="HTML")


if __name__ == "__main__":
    asyncio.run(main())
