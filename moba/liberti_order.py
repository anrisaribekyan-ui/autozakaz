"""Заказ у Liberti (Митино) по последней пачке закупки: то, что пачка отнесла в «🟩 Liberti / SMG».

  * берёт data/batch_runs/<последний>_<owner>/lines.json, строки с выбором Liberti (артикул 0L-…);
  * по каждому артикулу смотрит на сайте остаток «Либерти - Митино» и оптовую цену;
  * пробно (по умолчанию) — только отчёт; --live и LIBERTI_LIVE=1 в .env — кладёт в корзину и оформляет
    самовывоз, наличные; затем переносит заказы WB в сегодняшнюю поставку и пишет журнал
    (повторный запуск их уже не закажет).
  * чужие товары в корзине Liberti → не оформляет (сначала очистить корзину на сайте).
  * отчёт — в бот владельца; для Гора копия в бот Анри.

Запуск:  venv/bin/python liberti_order.py --owner gor [--live] [--run data/batch_runs/20261021_2318_gor]
Нужно в .env: LIBERTI_LOGIN, LIBERTI_PASSWORD, LIBERTI_PHONE (телефон для заказа).
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from liberti_client import LibertiClient
from zakaz_batch import MSK, OWNERS, journal_done, journal_init, journal_set


def latest_run(owner: str) -> Path | None:
    runs = sorted(Path("data/batch_runs").glob(f"*_{owner}/lines.json"))
    return runs[-1].parent if runs else None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", choices=list(OWNERS), default="gor")
    ap.add_argument("--run", help="папка пачки (по умолчанию последняя)")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--no-tg", action="store_true")
    args = ap.parse_args()
    load_dotenv(".env")
    live = args.live and os.getenv("LIBERTI_LIVE", "0") == "1"
    own = OWNERS[args.owner]
    run = Path(args.run) if args.run else latest_run(args.owner)
    if not run or not (run / "lines.json").exists():
        print("нет пачки для", args.owner)
        return 2
    lines = json.loads((run / "lines.json").read_text("utf-8"))
    await journal_init()
    done = await journal_done()
    shop_by_title = {s["title"]: s for s in own["shops"]}
    want = []
    for ln in lines:
        pick = ln.get("lib_pick") or {}
        shop = shop_by_title.get(ln.get("shop"))
        if not shop or ln.get("bought") or ln.get("bought_mm") or not str(pick.get("src", "")).startswith("Liberti"):
            continue
        if not pick.get("art") or (shop["key"], int(ln["wb_order_id"])) in done:
            continue
        want.append((ln, shop, pick))
    head = f"🟩 <b>Liberti · {own['shops'][0]['title'] if args.owner == 'gor' else 'Анри · Лёша · Борода'}</b>" \
           f" · {datetime.now(MSK):%d.%m %H:%M}" + ("" if live else " (пробно)")
    if not want:
        print("нечего заказывать у Liberti в", run)
        return 0
    lc = LibertiClient(os.getenv("LIBERTI_LOGIN", "").strip(), os.getenv("LIBERTI_PASSWORD", "").strip())
    out = [head]
    try:
        items: dict[str, object] = {}
        for _ln, _sh, pick in want:
            code = pick["art"]
            if code in items:
                continue
            it = await lc.search_code(code)
            if it:
                await lc.mitino(it)
            items[code] = it
        need: dict[str, int] = {}
        ok_lines, miss = [], []
        for ln, shop, pick in want:
            it = items.get(pick["art"])
            n = need.get(pick["art"], 0) + 1
            if it and it.mitino is not None and it.mitino >= n:
                need[pick["art"]] = n
                ok_lines.append((ln, shop, it))
            else:
                why = "не нашёл на сайте" if not it else ("Митино не понял" if it.mitino is None
                                                          else f"в Митино {it.mitino} шт.")
                miss.append((ln, shop, pick, why))
        total = sum((it.opt or 0) for _l, _s, it in ok_lines)
        res = None
        if ok_lines and live:
            phone = os.getenv("LIBERTI_PHONE", "").strip()
            if not phone:
                out.append("❌ нет LIBERTI_PHONE в .env — не оформляю")
            elif await lc.basket_ids():
                out.append("❌ в корзине Liberti уже есть товары — очисти корзину на сайте и запусти снова")
            else:
                expected: dict[int, int] = {}
                for _l, _s, it in ok_lines:
                    expected[it.id] = expected.get(it.id, 0) + 1
                for pid, q in expected.items():
                    await lc.add(pid, q)
                orders = ", ".join(f"№{ln['wb_order_id']}" for ln, _s, _i in ok_lines)
                res = await lc.checkout(expected, phone, f"WB {args.owner}: {orders}"[:250], True, total)
        elif ok_lines:
            out.append(f"🧪 можно заказать {len(ok_lines)} шт. на ≈{total} ₽ (пробно, ничего не оформлено)")
        if res is not None:
            if res.status == "placed":
                out.append(f"✅ Заказ Liberti №{res.order_number} на {res.total:.0f} ₽ — забрать в Митино, пав. 440")
            elif res.status == "unknown":
                out.append("⚠️ заказ МОГ уйти — проверь «Историю заказов» на liberti.ru! " + "; ".join(res.problems))
            else:
                out.append("❌ не оформлено: " + "; ".join(res.problems))
        for ln, _s, it in ok_lines:
            out.append(f"• {html.escape(str(ln['shop']))} №{ln['wb_order_id']} · <b>{it.opt} ₽</b> · Митино {it.mitino} шт.\n"
                       f"   {html.escape(it.name[:80])} ({it.code})")
        if miss:
            out.append(f"\n📍 Нет в Митино у Liberti ({len(miss)}) — купить в другом месте:")
            for ln, _s, pick, why in miss:
                out.append(f"• {html.escape(str(ln['shop']))} №{ln['wb_order_id']} · {html.escape(pick['name'][:70])} — {why}")
        # журнал + поставка
        if res is not None and res.status in ("placed", "unknown"):
            from wb_supply import WbSupply
            sup = WbSupply()
            try:
                for ln, shop, it in ok_lines:
                    if res.status == "unknown":
                        await journal_set(shop["key"], ln["wb_order_id"], status="unknown", run=run.name)
                        continue
                    await journal_set(shop["key"], ln["wb_order_id"], status="placed",
                                      moba_order=f"LIB-{res.order_number}", product_id=it.id, price=it.opt,
                                      run=run.name, title=ln.get("title"), nm_id=ln.get("nm_id"),
                                      product_name=it.name)
                    try:
                        sid, _name = await sup.move_to_today(os.getenv(shop["env"], "").strip(), ln["wb_order_id"])
                        await journal_set(shop["key"], ln["wb_order_id"], status="supply_ok", supply=sid)
                    except Exception as exc:  # noqa: BLE001
                        out.append(f"⚠️ №{ln['wb_order_id']} не перенёсся в сборку: {exc}")
                        await journal_set(shop["key"], ln["wb_order_id"], status="supply_fail")
            finally:
                await sup.close()
    finally:
        await lc.close()
    text = "\n".join(out)
    print(text.replace("<b>", "").replace("</b>", ""))
    if not args.no_tg:
        from test_sborka import tg_send
        bots = [own["bot"]] + ([("BOT_MAIN", "TG_ANRI_ID")] if args.owner == "gor" else [])
        for b in bots:
            try:
                await tg_send(text, [], *b, parse_mode="HTML")
            except Exception as exc:  # noqa: BLE001
                print("Telegram:", exc)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
