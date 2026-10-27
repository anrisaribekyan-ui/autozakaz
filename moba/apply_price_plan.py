"""Записать в WB новые цены из таблицы price_plan_<дата>.xlsx (решение Анри 22.10: сразу на 18%, как в таблице).

  * берёт строки «поднять» / «снизить» с колонкой «Станет (до скидки)»; скидку продавца не меняет;
  * пропускает явные ошибки подбора: на WB мелочь (коннектор, наклейка, скотч, винт, сетка, проклейка…),
    а у поставщика — целая запчасть; закупка дороже нашей цены в 1,5+ раза;
  * перед записью сохраняет старые цены в data/price_changes_<время>.json → откат: --rollback <файл>;
  * пробно по умолчанию; --apply пишет только при PRICE_APPLY_LIVE=1 в .env.

Запуск:  venv/bin/python apply_price_plan.py data/price_plan_2026-10-22.xlsx [--apply]
         venv/bin/python apply_price_plan.py --rollback data/price_changes_20261022_0710.json [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx
import openpyxl
from dotenv import load_dotenv

from price_plan import SHOPS, UPLOAD_URL, _retry_after
from unit_econ import _prices_token

SMALL = re.compile(r"коннектор|наклейк|скотч|проклейк|винт|сетк|клей|изоляц|заглушк|пыльник|уплотнит|резинк|"
                   r"стекло камеры|линз|держател|толкател|кнопк|шлейф|антенн|сим[- ]?лоток|лоток", re.I)


def suspicious(wb_title: str, sup_name: str, cost: float, ours: float) -> str | None:
    wb_small = set(m.group(0).lower() for m in SMALL.finditer(wb_title or ""))
    sup_small = set(m.group(0).lower() for m in SMALL.finditer(sup_name or ""))
    if wb_small - sup_small:
        return "на WB мелочь (" + ", ".join(sorted(wb_small - sup_small)) + "), у поставщика другое"
    if ours and cost > ours * 1.5:
        return f"закупка {cost:.0f} ₽ больше нашей цены {ours:.0f} ₽ в 1,5+ раза — похоже на ошибку подбора"
    return None


async def upload(c: httpx.AsyncClient, token: str, items: list[dict]) -> list[str]:
    out = []
    for i in range(0, len(items), 1000):
        part = items[i:i + 1000]
        while True:
            r = await c.post(UPLOAD_URL, json={"data": part}, headers={"Authorization": token})
            if r.status_code == 429:
                w = _retry_after(r)
                print(f"   WB 429 — жду {w} с", flush=True)
                await asyncio.sleep(w)
                continue
            break
        if r.status_code == 200:
            out.append(f"пачка {i // 1000 + 1}: ок (задача {((r.json() or {}).get('data') or {}).get('id')})")
        else:
            out.append(f"пачка {i // 1000 + 1}: ошибка {r.status_code} {r.text[:200]}")
        print("  ", out[-1], flush=True)
        await asyncio.sleep(7)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx", nargs="?")
    ap.add_argument("--rollback")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-tg", action="store_true")
    args = ap.parse_args()
    load_dotenv(".env")
    live = args.apply and os.getenv("PRICE_APPLY_LIVE", "0") == "1"
    by_title = {s["title"]: s for s in SHOPS}
    plan: dict[str, list[dict]] = {}
    old: dict[str, list[dict]] = {}
    skipped: list[str] = []
    if args.rollback:
        data = json.loads(Path(args.rollback).read_text("utf-8"))
        for title, items in data.items():
            plan[title] = [{"nmID": x["nmID"], "price": x["price"], "discount": x["discount"]} for x in items]
    else:
        wb = openpyxl.load_workbook(args.xlsx, read_only=True)
        for ws in wb.worksheets:
            if ws.title not in by_title:
                continue
            rows = list(ws.iter_rows(values_only=True))
            h = {name: i for i, name in enumerate(rows[0])}
            for r in rows[1:]:
                new_base = r[h["Станет (до скидки)"]]
                what = str(r[h["Что делать"]] or "")
                if not new_base or not what.startswith(("поднять", "снизить")):
                    continue
                why = suspicious(r[h["Название"]], r[h["Товар у поставщика"]], r[h["Закупка опт"]] or 0,
                                 r[h["Наша цена сейчас"]] or 0)
                if why:
                    skipped.append(f"{ws.title} {r[h['nmID']]} · {str(r[h['Название']])[:50]} — {why}")
                    continue
                disc = int(r[h["Скидка %"]] or 0)
                plan.setdefault(ws.title, []).append({"nmID": int(r[h["nmID"]]), "price": int(new_base), "discount": disc})
                old.setdefault(ws.title, []).append({"nmID": int(r[h["nmID"]]), "price": int(round(r[h["Цена до скидки"]])),
                                                     "discount": disc})
    lines = [f"💰 <b>Цены WB {'— записываю' if live else '(пробно)'}</b> · {datetime.now():%d.%m %H:%M}"]
    for t, items in plan.items():
        lines.append(f"• {t}: {len(items)} цен")
    lines.append(f"Пропущено как ошибки подбора: {len(skipped)}")
    print("\n".join(lines).replace("<b>", "").replace("</b>", ""))
    for s in skipped[:30]:
        print("   пропуск:", s)
    if not live:
        print("\nпробно — в WB ничего не записано (для записи: --apply и PRICE_APPLY_LIVE=1)")
        return 0
    if old:
        bk = Path("data") / f"price_changes_{datetime.now():%Y%m%d_%H%M}.json"
        bk.write_text(json.dumps(old, ensure_ascii=False), "utf-8")
        lines.append(f"Старые цены сохранены: {bk} (откат: --rollback {bk} --apply)")
    async with httpx.AsyncClient(timeout=60) as c:
        for t, items in plan.items():
            tok = _prices_token(os.getenv(by_title[t]["env"], "").strip())
            print(t, "→ WB", flush=True)
            res = await upload(c, tok, items)
            lines.append(f"{t}: " + "; ".join(res))
    text = "\n".join(lines)
    print(text.replace("<b>", "").replace("</b>", ""))
    if not args.no_tg:
        from test_sborka import tg_send
        await tg_send(text, [], "BOT_MAIN", "TG_ANRI_ID", parse_mode="HTML")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
