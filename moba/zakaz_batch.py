# zakaz_batch.py
"""
ЗАКУПКА ПАЧКОЙ (решение Анри, 18.10): в 10:00 и 15:00 по Москве.

1. Новые заказы WB магазинов Анри, Лёша, Борода (Гор — отдельно, не трогаем).
2. Подбор: связка или уверенный автоподбор (⭐) на moba, в Митино → в общую корзину.
   Всё остальное (не уверен / нет в Митино / АКБ без BC) — не покупаем, ищем у Мастер Мобайла и на poisk123
   и присылаем Анри списком (Smg777 → отдельный список «для Доски»).
3. Одна корзина moba в Митино. Не хватает количества / «ожидается поступление» → берём сколько есть,
   остаток — в список «купить в другом месте».
4. Проверки страницы оформления (Митино, самовывоз, наличные, итог = сумме, все товары доступны).
5. БОЕВОЙ режим (только если И в .env BATCH_LIVE=1, И запуск с --live): нажать «Оформить заказ»,
   затем перенести оплаченные этим заказом заказы WB в поставку «Отправка <дата>».
   Иначе — всё то же, но кнопку не нажимает и корзину очищает (пробный режим).
6. Отчёт в Telegram Анри + журнал в БД (таблица batch_lines) — повторно один и тот же заказ WB не покупается.

Запуск:  venv/bin/python zakaz_batch.py            (пробно)
         venv/bin/python zakaz_batch.py --live     (боевой, нужен BATCH_LIVE=1 в .env)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import logging
import os
import sys
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import httpx
from dotenv import load_dotenv

from learning import Learned
from matcher import parse_wb_title
from moba_client import MobaClient
from moba_order import BASE_URL, CITY_ID, MITINO_STORE_ID, MobaOrderer, OrderRequest
from storage import Storage
from suppliers_ext import MasterMobileClient, Poisk123Client, find_elsewhere, format_elsewhere
import test_sborka
from test_sborka import CANCEL_WB, MSK, collect_orders, fmt_money, resolve, rub, tg_send
from training import card_category
from unit_econ import profit_line, seller_price
from wb_content import WbContent
from wb_supply import WbSupply, supply_name_for_today

DB_PATH = "data/autozakaz.db"
OWNERS = {
    # Анри: магазины Анри, Лёша, Борода; аккаунт moba Анри; отчёт в бот Анри
    "anri": {"shops": [{"key": "shop2", "title": "Анри", "env": "WB_SHOP2"},
                       {"key": "shop3", "title": "Лёша", "env": "WB_SHOP3"},
                       {"key": "shop4", "title": "Борода", "env": "WB_SHOP4"}],
             "moba": ("MOBA_LOGIN", "MOBA_PASSWORD"), "account": "anri", "bot": ("BOT_MAIN", "TG_ANRI_ID")},
    # Гор (решение 18.10): те же правила, свой аккаунт moba, отчёт в его бот
    "gor": {"shops": [{"key": "gor", "title": "Гор", "env": "WB_GOR"}],
            "moba": ("MOBA_GOR_LOGIN", "MOBA_GOR_PASSWORD"), "account": "gor", "bot": ("BOT_GOR", "TG_GOR_ID")},
}
def match_title(title: str, owner: str) -> str:
    """Особые правила владельца для подбора. Гор (18.10): крышки ВСЕГДА премиум — других он не продаёт."""
    if owner == "gor":
        low = (title or "").lower()
        if re.search(r"крышк|корпус", low) and not re.search(r"премиум|premium", low):
            return f"{title} Премиум"
    return title


CONFIDENT = ("связка", "автоподбор ⭐")
log = logging.getLogger("zakaz_batch")


# ---------------------------------------------------------------------- #
# Журнал: какие заказы WB уже куплены пачкой
# ---------------------------------------------------------------------- #
async def journal_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS batch_lines (
            shop_key TEXT NOT NULL, wb_order_id INTEGER NOT NULL, status TEXT NOT NULL,
            moba_order TEXT, product_id INTEGER, price INTEGER, supply TEXT, run TEXT, ts TEXT,
            PRIMARY KEY (shop_key, wb_order_id))""")
        cur = await db.execute("PRAGMA table_info(batch_lines)")
        cols = {r[1] for r in await cur.fetchall()}
        for c, t in (("title", "TEXT"), ("nm_id", "INTEGER"), ("product_name", "TEXT"), ("note", "TEXT")):
            if c not in cols:
                await db.execute(f"ALTER TABLE batch_lines ADD COLUMN {c} {t}")
        await db.commit()


async def journal_rebuy(shop_keys: list[str]) -> list[dict]:
    """Заказы WB, которые надо закупить заново (moba убрала позицию из заказа и т.п.)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        q = ",".join("?" * len(shop_keys))
        cur = await db.execute(f"SELECT * FROM batch_lines WHERE status='rebuy' AND shop_key IN ({q})", shop_keys)
        return [dict(r) for r in await cur.fetchall()]


async def journal_done() -> set[tuple[str, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT shop_key, wb_order_id FROM batch_lines "
                               "WHERE status IN ('placed','supply_ok','supply_fail','unknown')")
        return {(r[0], int(r[1])) for r in await cur.fetchall()}


async def journal_set(shop_key: str, wb_order_id: int, **f: Any) -> None:
    f.setdefault("ts", datetime.now(MSK).isoformat())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO batch_lines (shop_key, wb_order_id, status, ts) VALUES (?,?,?,?)",
                         (shop_key, int(wb_order_id), f.get("status", "new"), f["ts"]))
        sets = ", ".join(f"{k}=?" for k in f)
        await db.execute(f"UPDATE batch_lines SET {sets} WHERE shop_key=? AND wb_order_id=?",
                         (*f.values(), shop_key, int(wb_order_id)))
        await db.commit()


# ---------------------------------------------------------------------- #
# Корзина moba: собрать → проверить → (оформить)
# ---------------------------------------------------------------------- #
async def build_cart(o: MobaOrderer, wanted: "OrderedDict[int, int]") -> dict:
    await o._api("POST", "/api/choose-store", {"store_id": MITINO_STORE_ID, "city_id": CITY_ID})
    for pid, qty in wanted.items():
        r = await o._api("POST", "/api/cart/items", {"productId": int(pid)})
        if r.get("status") != 200 or not isinstance(r.get("data"), dict):
            continue
        if qty > 1:
            bid = next((cp.get("basketItemId") for cp in r["data"].get("cartProducts") or []
                        if int(cp.get("productId") or -1) == int(pid)), None)
            if bid:
                await o._api("POST", "/api/cart/items/quantity",
                             {"productId": int(pid), "basketItemId": int(bid), "quantity": int(qty)})
    cart = await o._get_cart()
    return {str(p.get("productId")): {"qty": int(p.get("quantity") or 0), "price": int(p.get("price") or 0),
                                      "status": str(p.get("availabilityStatus"))}
            for p in cart.get("cartProducts") or []}


async def moba_batch(login: str, password: str, wanted: "OrderedDict[int, int]", live: bool,
                     run_dir: Path, request_id: str, account: str = "anri") -> dict:
    """wanted: product_id -> qty. Возвращает {status, got{pid:qty}, total, order_number, problems}."""
    res: dict = {"status": "rejected", "got": {}, "problems": []}
    o = MobaOrderer(login, password, account, dry_run=not live)
    added = False
    try:
        await o.start()
        if (await o._get_cart()).get("cartProducts"):
            res["problems"].append("в корзине moba уже есть товары — пачку не собираю, корзину не трогаю")
            return res
        cart = await build_cart(o, wanted)
        added = True
        # что реально можно взять: только in_stock, не больше, чем есть
        got = OrderedDict()
        for pid, qty in wanted.items():
            c = cart.get(str(pid)) or {}
            q = min(qty, c.get("qty", 0)) if c.get("status") == "in_stock" else 0
            if q > 0:
                got[pid] = q
        if got != wanted:  # пересобрать корзину ровно под то, что есть
            await o._clear_cart()
            cart = await build_cart(o, got) if got else {}
        res["got"] = {str(k): v for k, v in got.items()}
        res["cart"] = cart
        if not got:
            res["problems"].append("на moba ничего из пачки нет в наличии")
            return res
        expected = sum(cart.get(str(pid), {}).get("price", 0) * q for pid, q in got.items())
        bad = [pid for pid, q in got.items()
               if (cart.get(str(pid)) or {}).get("qty") != q or (cart.get(str(pid)) or {}).get("status") != "in_stock"]
        if bad:
            res["problems"].append(f"корзина не совпала по товарам {bad}")
            return res

        page = o._page
        await page.goto(f"{BASE_URL}/order/", wait_until="domcontentloaded")
        await page.get_by_role("button", name="Оформить заказ").wait_for(state="visible", timeout=20000)
        oj = o._extract_order_json(await page.content())
        if not oj:
            res["problems"].append("не удалось прочитать страницу оформления")
            return res
        (run_dir / "order_page.json").write_text(json.dumps(oj, ensure_ascii=False, indent=2), "utf-8")
        ob = oj.get("order") or {}
        summ = ob.get("summary") or {}
        groups = ob.get("groups") or []
        total = int(summ.get("total") or 0)
        res["total"] = total
        if ob.get("canOrder") is not True:
            res["problems"].append("moba: canOrder != true")
        if ob.get("errors"):
            res["problems"].append(f"moba: ошибки {ob.get('errors')}")
        if len(groups) != 1 or str(groups[0].get("store")) != "Митино":
            res["problems"].append(f"moba: группы {[g.get('store') for g in groups]}, нужна одна «Митино»")
        if int(summ.get("deliveryPrice") or 0) != 0:
            res["problems"].append(f"moba: доставка {summ.get('deliveryPrice')}")
        if total != expected:
            res["problems"].append(f"moba: итог {total} ≠ ожидаемому {expected}")
        dom = await o._check_dom_state()
        if dom:
            res["problems"].append(f"moba: {dom}")
        cap = int(os.getenv("BATCH_MAX_RUB", "20000") or 20000)
        if total > cap:
            res["problems"].append(f"сумма {total} ₽ больше лимита {cap} ₽ — не оформляю, проверь и закажи сам "
                                   f"(или подними BATCH_MAX_RUB в .env)")
        await page.screenshot(path=str(run_dir / "order_page.png"), full_page=True)
        if res["problems"]:
            return res
        if not live:
            res["status"] = "dry_run_ok"
            return res
        # --- БОЕВОЙ: один клик «Оформить заказ» ---
        req = OrderRequest(request_id=request_id, product_id=0, quantity=sum(got.values()),
                           expected_price=total, title_hint=f"пачка {len(got)} товаров",
                           comment=f"WB пачка {request_id}")
        r = await o._submit(req, oj, True)
        added = r.status != "placed" and r.status != "unknown"  # после успеха корзина уже пуста
        res.update({"status": r.status, "order_number": r.order_number, "message": r.message})
        if r.status != "placed":
            res["problems"].append(f"оформление: {r.status} — {r.message}")
        return res
    except Exception as exc:  # noqa: BLE001
        res["problems"].append(f"ошибка: {exc}")
        return res
    finally:
        if added and res.get("status") not in ("placed", "unknown"):
            await o._clear_cart()
        await o.close()


# ---------------------------------------------------------------------- #
def _batt_cheaper_elsewhere(ln: dict, mm_item: dict, owner: str) -> bool:
    """АКБ (Анри 20.10): сначала Liberti/SMG (самый дешёвый опт), Мастер Мобайл — последним."""
    try:
        import gor_costs as G
        t = match_title(ln["title"], owner)
        if not G.is_battery(G.parse_wb_title(t)) or G.battery_tag(t):
            return False
        mm_price = float(mm_item.get("price_club") or mm_item.get("price") or 10**9)
        alt = [x["price"] for x in (G.liberti_best(t), G.smg_best(t)) if x]
        return bool(alt) and min(alt) <= mm_price
    except Exception:  # noqa: BLE001
        return False


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="боевой режим (нужен ещё BATCH_LIVE=1 в .env)")
    ap.add_argument("--no-tg", action="store_true")
    ap.add_argument("--owner", choices=list(OWNERS), default="anri", help="чьи магазины: anri (Анри/Лёша/Борода) или gor")
    args = ap.parse_args()
    own = OWNERS[args.owner]
    test_sborka.TEST_SHOPS = own["shops"]
    load_dotenv()
    live = args.live and os.getenv("BATCH_LIVE", "0") == "1"
    stamp = datetime.now(MSK).strftime("%Y%m%d_%H%M")
    run_dir = Path("data/batch_runs") / f"{stamp}_{args.owner}"
    run_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        handlers=[logging.FileHandler(run_dir / "run.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    log.info("ПАЧКА %s — режим %s", stamp, "БОЕВОЙ" if live else "пробный (кнопку не жму)")

    await journal_init()
    done = await journal_done()
    storage = Storage(DB_PATH)
    await storage.init()
    learned = Learned(await storage.feedback_rows(owner=args.owner))
    login, password = os.getenv(own["moba"][0], "").strip(), os.getenv(own["moba"][1], "").strip()
    if not login or not password:
        print(f"Нет {own['moba'][0]} / {own['moba'][1]} в .env")
        return 2
    moba = MobaClient(login, password, own["account"])
    wbc = WbContent(storage)
    http = httpx.AsyncClient(timeout=25.0)
    mm, p123 = MasterMobileClient(), Poisk123Client()
    lines: list[dict] = []
    try:
        orders, wb_errors = await collect_orders(http, 0)
        rcache: dict = {}
        pcache: dict = {}
        ecache: dict = {}
        for o in orders:
            shop, raw = o["shop"], o["raw"]
            if o["stage"] != "новый" or (shop["key"], int(raw["id"])) in done:
                continue
            nm_id = raw.get("nmId")
            token = os.getenv(shop["env"], "").strip()
            title = None
            try:
                title = await wbc.get_title(shop["key"], nm_id, token)
            except Exception:  # noqa: BLE001
                pass
            title = title or raw.get("article") or f"nmId {nm_id}"
            key = (shop["key"], nm_id)
            if key not in rcache:
                rcache[key] = await resolve(storage, moba, learned, shop["key"], nm_id,
                                            match_title(title, args.owner), pcache)
            r = rcache[key]
            m = r.get("moba") or {}
            sale, sale_src = await seller_price(token, nm_id, raw, http)
            ln = {"shop": shop, "wb_order_id": int(raw["id"]), "nm_id": nm_id, "title": title,
                  "wb_price": rub(raw), "sale": sale, "sale_src": sale_src,
                  "source": r["source"], "moba": m, "problems": list(r["problems"]),
                  "category": card_category({"title": title})}
            ln["to_buy"] = (r["source"] in CONFIDENT and m.get("ok") and m.get("mitino") == "yes"
                            and o["wbStatus"] not in CANCEL_WB)
            lines.append(ln)
            log.info("%s №%s · %s → %s %s", shop["title"], raw["id"], title[:60], r["source"],
                     m.get("price") if ln["to_buy"] else "")

        # --- закупить заново: позиции, которые moba убрала из прошлых заказов ---
        for rb in await journal_rebuy([sh["key"] for sh in own["shops"]]):
            shop = next(sh for sh in own["shops"] if sh["key"] == rb["shop_key"])
            title = rb.get("title") or ""
            key = (shop["key"], rb.get("nm_id"))
            if key not in rcache:
                rcache[key] = await resolve(storage, moba, learned, shop["key"], rb.get("nm_id"),
                                            match_title(title, args.owner), pcache)
            r = rcache[key]
            m = r.get("moba") or {}
            sale, sale_src = await seller_price(os.getenv(shop["env"], "").strip(), rb.get("nm_id"), None, http)
            ln = {"shop": shop, "wb_order_id": int(rb["wb_order_id"]), "nm_id": rb.get("nm_id"), "title": title,
                  "wb_price": None, "sale": sale, "sale_src": sale_src,
                  "source": r["source"], "moba": m, "rebuy": True,
                  "problems": ["🔁 закупка заново: moba убрала позицию из заказа №" + str(rb.get("moba_order"))]
                  + list(r["problems"]), "category": card_category({"title": title})}
            ln["to_buy"] = r["source"] in CONFIDENT and m.get("ok") and m.get("mitino") == "yes"
            lines.append(ln)

        # --- moba или Мастер Мобайл: где дешевле (Анри 18.10: они рядом; крышки при равной цене — MM, там возврат) ---
        for ln in lines:
            if not ln["to_buy"]:
                continue
            k = (ln["shop"]["key"], ln["nm_id"])
            if k not in ecache:
                try:
                    ecache[k] = await find_elsewhere(match_title(ln["title"], args.owner), mm, p123)
                except Exception as exc:  # noqa: BLE001
                    ecache[k] = {"mitino": [], "mitino_other_quality": [], "mm_not_mitino": [],
                                 "errors": [str(exc)], "wb_quality": None}
            mm_best = next((x for x in ecache[k]["mitino"] if x.get("supplier") == "Мастер Мобайл"), None)
            if not mm_best:
                continue
            mm_price = mm_best.get("price_club") or mm_best.get("price") or 10**9
            moba_price = ln["moba"]["price"]
            is_back = (ln.get("category") == "back")
            if mm_price < moba_price or (is_back and mm_price <= moba_price):
                ln["to_buy"] = False
                ln["mm_pick"] = mm_best
                ln["problems"].append(f"у Мастер Мобайла дешевле: {mm_price} ₽ против moba {moba_price} ₽"
                                      + (" (крышка — у MM возврат)" if is_back else ""))

        # --- moba пачкой ---
        wanted: "OrderedDict[int, int]" = OrderedDict()
        for ln in lines:
            if ln["to_buy"]:
                pid = ln["moba"]["product_id"]
                wanted[pid] = wanted.get(pid, 0) + 1
        mres: dict = {"status": "empty", "got": {}, "problems": []}
        if wanted:
            mres = await moba_batch(login, password, wanted, live, run_dir, f"batch-{stamp}-{args.owner}",
                                    own["account"])
        # распределить купленное по заказам WB (у кого не хватило — в «другое место»)
        left = {int(k): v for k, v in mres.get("got", {}).items()} if mres["status"] in ("placed", "dry_run_ok") else {}
        for ln in lines:
            if not ln["to_buy"]:
                continue
            pid = ln["moba"]["product_id"]
            if left.get(pid, 0) > 0:
                left[pid] -= 1
                ln["bought"] = True
            else:
                ln["to_buy"] = False
                ln["problems"].append("не вошло в заказ moba (нет нужного количества или ошибка корзины)")

        # --- журнал + поставка (только боевой и только после успешного заказа) ---
        supply = WbSupply()
        try:
            for ln in lines:
                if not ln.get("bought"):
                    continue
                if mres["status"] == "placed":
                    await journal_set(ln["shop"]["key"], ln["wb_order_id"], status="placed",
                                      moba_order=mres.get("order_number"), product_id=ln["moba"]["product_id"],
                                      price=ln["moba"]["price"], run=stamp, title=ln["title"], nm_id=ln["nm_id"],
                                      product_name=ln["moba"].get("name"))
                    if ln.get("rebuy"):   # уже в поставке — переносить не нужно
                        ln["supply"] = "уже в поставке"
                        await journal_set(ln["shop"]["key"], ln["wb_order_id"], status="supply_ok")
                        continue
                    try:
                        sid, name = await supply.move_to_today(os.getenv(ln["shop"]["env"], "").strip(),
                                                               ln["wb_order_id"])
                        ln["supply"] = name
                        await journal_set(ln["shop"]["key"], ln["wb_order_id"], status="supply_ok", supply=sid)
                    except Exception as exc:  # noqa: BLE001
                        ln["problems"].append(f"не перенёсся в сборку: {exc}")
                        await journal_set(ln["shop"]["key"], ln["wb_order_id"], status="supply_fail")
                elif mres["status"] == "unknown":
                    await journal_set(ln["shop"]["key"], ln["wb_order_id"], status="unknown", run=stamp)
        finally:
            await supply.close()

        # --- всё, что не куплено на moba: где ещё есть ---
        for ln in lines:
            if ln.get("bought"):
                continue
            k = (ln["shop"]["key"], ln["nm_id"])
            if k not in ecache:
                try:
                    ecache[k] = await find_elsewhere(match_title(ln["title"], args.owner), mm, p123)
                except Exception as exc:  # noqa: BLE001
                    ecache[k] = {"mitino": [], "mitino_other_quality": [], "mm_not_mitino": [],
                                 "errors": [str(exc)], "wb_quality": None}
            ln["elsewhere"] = ecache[k]
            first = (ecache[k].get("mitino") or [None])[0]
            if not ln.get("mm_pick") and first and first.get("supplier") == "Мастер Мобайл" \
                    and first.get("product_id") and not _batt_cheaper_elsewhere(ln, first, args.owner):
                ln["mm_pick"] = first
                ln["problems"].append("на moba нет/не уверен — беру у Мастер Мобайла (Митино)")

        # --- Мастер Мобайл: одна корзина, самовывоз пав. 535, наличные (решение Анри 18.10) ---
        mm_res = None
        mm_lines = [ln for ln in lines if ln.get("mm_pick") and not ln.get("bought")]
        if mm_lines:
            wanted_mm: "OrderedDict[str, int]" = OrderedDict()
            price_mm: dict[str, float] = {}
            for ln in mm_lines:
                x = ln["mm_pick"]
                pid = str(x["product_id"])
                if wanted_mm.get(pid, 0) >= int(x.get("mitino_qty") or 0):
                    ln["problems"].append("у Мастер Мобайла в Митино не хватает количества")
                    ln["mm_pick"] = None
                    continue
                wanted_mm[pid] = wanted_mm.get(pid, 0) + 1
                price_mm[pid] = float(x.get("price_club") or x.get("price") or 0)
            mm_login, mm_pass = os.getenv("MM_LOGIN", "").strip(), os.getenv("MM_PASSWORD", "").strip()
            if wanted_mm and mm_login and mm_pass:
                from mm_order import MasterMobileOrderer
                expected = sum(price_mm[p] * q for p, q in wanted_mm.items())
                cap = float(os.getenv("BATCH_MAX_RUB", "20000") or 20000)
                mo = MasterMobileOrderer(mm_login, mm_pass)
                try:
                    await mo.start()
                    mm_res = await mo.check_order(dict(wanted_mm), submit=live,
                                                  max_total=min(expected, cap), request_id=f"mm-{stamp}-{args.owner}")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Мастер Мобайл: %s", exc)
                finally:
                    await mo.close()
            ok_mm = mm_res is not None and (mm_res.status == "placed" or (not live and mm_res.ok))
            supply = WbSupply()
            try:
                for ln in mm_lines:
                    if not ln.get("mm_pick"):
                        continue
                    if not ok_mm:
                        if mm_res is not None and mm_res.status == "unknown":
                            await journal_set(ln["shop"]["key"], ln["wb_order_id"], status="unknown", run=stamp)
                        continue
                    ln["bought_mm"] = True
                    if not live:
                        continue
                    x = ln["mm_pick"]
                    await journal_set(ln["shop"]["key"], ln["wb_order_id"], status="placed",
                                      moba_order=f"MM-{mm_res.order_number}", product_id=int(x["product_id"]),
                                      price=int(x.get("price_club") or x.get("price") or 0), run=stamp,
                                      title=ln["title"], nm_id=ln["nm_id"], product_name=x.get("name"))
                    if ln.get("rebuy"):
                        await journal_set(ln["shop"]["key"], ln["wb_order_id"], status="supply_ok")
                        continue
                    try:
                        sid, name = await supply.move_to_today(os.getenv(ln["shop"]["env"], "").strip(),
                                                               ln["wb_order_id"])
                        ln["supply"] = name
                        await journal_set(ln["shop"]["key"], ln["wb_order_id"], status="supply_ok", supply=sid)
                    except Exception as exc:  # noqa: BLE001
                        ln["problems"].append(f"не перенёсся в сборку: {exc}")
                        await journal_set(ln["shop"]["key"], ln["wb_order_id"], status="supply_fail")
            finally:
                await supply.close()

        # --- отчёт ---
        mode = "" if live else " (ПРОБНО — не оформлено)"
        rep = [f"🛒 Закупка {datetime.now(MSK).strftime('%d.%m %H:%M')}{mode}"]
        bought = [ln for ln in lines if ln.get("bought")]
        if mres["status"] == "placed":
            rep.append(f"✅ moba заказ №{mres.get('order_number')} на {fmt_money(mres.get('total'))} ₽ · "
                       f"{len(bought)} шт. Забрать в Митино.")
        elif mres["status"] == "dry_run_ok":
            rep.append(f"🧪 moba: корзина собрана и проверена, {len(bought)} шт. на {fmt_money(mres.get('total'))} ₽")
        elif mres["status"] == "unknown":
            rep.append("⚠️ moba: заказ МОГ уйти — проверь на сайте вручную! " + "; ".join(mres["problems"]))
        elif wanted:
            rep.append("❌ moba: не оформлено — " + "; ".join(mres["problems"]))
        tot_sale = tot_profit = 0.0
        for ln in bought:
            rep.append(f"  • {ln['shop']['title']} №{ln['wb_order_id']} · {ln['moba']['price']} ₽ · {ln['title'][:55]}"
                       + (f" → {ln['supply']}" if ln.get("supply") else ""))
            rep.append("     " + profit_line(ln.get("sale"), ln.get("sale_src", ""), ln["moba"]["price"], ln["shop"]["key"]))
            from unit_econ import profit as _pr
            pr = _pr(ln.get("sale"), ln["moba"]["price"], ln["shop"]["key"])
            if pr.get("profit") is not None:
                tot_sale += ln["sale"]
                tot_profit += pr["profit"]
        if bought and tot_sale:
            rep.append(f"  Итого по пачке: продали {fmt_money(tot_sale)} ₽ → ≈ прибыль {fmt_money(tot_profit)} ₽ "
                       f"({round(tot_profit / tot_sale * 100)}%)")
        mm_lines = [ln for ln in lines if ln.get("mm_pick") and not ln.get("bought")]
        if mm_lines:
            if mm_res is not None and mm_res.status == "placed":
                rep.append(f"\n✅ Мастер Мобайл заказ №{mm_res.order_number} на {fmt_money(mm_res.total)} ₽ — "
                           f"забрать в Митино, пав. 535:")
            elif mm_res is not None and mm_res.status == "unknown":
                rep.append("\n⚠️ Мастер Мобайл: заказ МОГ уйти — проверь в кабинете! " + "; ".join(mm_res.problems))
            elif mm_res is not None and mm_res.ok:
                rep.append(f"\n🧪 Мастер Мобайл: корзина собрана и проверена на {fmt_money(mm_res.total)} ₽ (пробно):")
            else:
                why = "; ".join(mm_res.problems) if mm_res is not None else "нет MM_LOGIN/MM_PASSWORD или ошибка"
                rep.append(f"\n🟦 Купить у Мастер Мобайла вручную, Митино пав. 535 — авто не вышло ({why}):")
            for ln in mm_lines:
                x = ln["mm_pick"]
                rep.append(f"  • {ln['shop']['title']} №{ln['wb_order_id']} · {x.get('price_club') or x.get('price')} ₽ · "
                           f"{x['name'][:70]}")
                rep.append("     " + profit_line(ln.get("sale"), ln.get("sale_src", ""), x.get("price_club") or x.get("price"), ln["shop"]["key"]))
        other = [ln for ln in lines if not ln.get("bought") and not ln.get("mm_pick")]
        # --- Liberti / SMG по оффлайн-прайсам (АКБ: Liberti или SMG, что дешевле — Анри 20.10) ---
        lib_text = ""
        try:
            import gor_costs as G
            import html as _h
            cart: list[str] = []
            for ln in other:
                t = match_title(ln["title"], args.owner)
                cands = [x for x in (G.liberti_best(t), G.smg_best(t)) if x]
                if not cands:
                    x = G.liberti_any(t)
                    cands = [x] if x else []
                if not cands:
                    continue
                pick = min(cands, key=lambda x: x["price"])
                ln["lib_pick"] = pick
                cart.append(f"• {ln['shop']['title']} №{ln['wb_order_id']} · <b>{pick['price']} ₽</b> · "
                            f"{pick['src'].split(' ')[0]}{(' арт. ' + str(pick['art'])) if pick.get('art') else ''}\n"
                            f"   {_h.escape(pick['name'][:80])}" + (f" · {pick['avail']}" if pick.get("avail") else "")
                            + "\n   " + _h.escape(profit_line(ln.get("sale"), ln.get("sale_src", ""), pick["price"],
                                                              ln["shop"]["key"])))
            if cart:
                tot = sum(ln["lib_pick"]["price"] for ln in other if ln.get("lib_pick"))
                lib_text = (f"🟩 <b>Liberti / SMG — заказать ({len(cart)} шт., ≈{fmt_money(tot)} ₽)</b>\n"
                            "Наличие в Митино у них по сайту ещё не проверяется — сверь при заказе.\n\n"
                            + "\n".join(cart))
                rep.append("\n" + re.sub(r"</?b>", "", lib_text))
        except Exception as exc:  # noqa: BLE001
            log.warning("Liberti/SMG: %s", exc)
        if other:
            rep.append(f"\n📍 Купить в другом месте / решить ({len(other)}):")
            smg: list[str] = []
            for ln in other:
                ext = ln.get("elsewhere") or {}
                rep.append(f"— {ln['shop']['title']} №{ln['wb_order_id']} · {ln['title'][:60]}"
                           + (f" · продали {fmt_money(ln['sale'])} ₽" if ln.get("sale") else ""))
                if ln["problems"]:
                    rep.append("   " + ln["problems"][-1])
                rep += ["   " + x for x in format_elsewhere(ext).split("\n")[:3]]
                for it in ext.get("mitino") or []:
                    if str(it.get("shop", "")).lower().startswith("smg"):
                        smg.append(f"{it['name']} · {it.get('price')} ₽ ({ln['shop']['title']} №{ln['wb_order_id']})")
                        break
            if smg:
                rep.append("\n📋 Для «Доски» (Smg777):")
                rep += [f"  • {x}" for x in smg]
        if not lines:
            rep.append("Новых заказов нет.")
        if wb_errors:
            rep.append("⚠️ WB: " + "; ".join(wb_errors))
        report = "\n".join(rep)
        (run_dir / "report.txt").write_text(report + "\n", "utf-8")
        (run_dir / "lines.json").write_text(json.dumps(
            [{**ln, "shop": ln["shop"]["title"]} for ln in lines], ensure_ascii=False, indent=2, default=str), "utf-8")
        (run_dir / "moba.json").write_text(json.dumps(mres, ensure_ascii=False, indent=2, default=str), "utf-8")
        print(report)
        # 19.10: новый вид — короткое сообщение «что сделать» + подробный отчёт-документ (HTML)
        from report_fmt import html_report, tg_summary
        now = datetime.now(MSK)
        owner_title = "Гор" if args.owner == "gor" else "Анри · Лёша · Борода"
        rep_file = run_dir / f"Закупка_{owner_title.split(' ')[0]}_{now:%d.%m_%H%M}.html"
        try:
            rep_file.write_text(html_report(owner_title, lines, mres, mm_res, live, wb_errors, now), "utf-8")
            summary = tg_summary(owner_title, lines, mres, mm_res, live, wb_errors, now)
        except Exception as exc:  # noqa: BLE001  — отчёт не должен ронять закупку
            log.warning("отчёт: %s", exc)
            summary, rep_file = None, None
        if not args.no_tg:
            try:
                if summary:
                    await tg_send(summary, [rep_file] if rep_file and lines else [], *own["bot"], parse_mode="HTML")
                else:
                    await tg_send(report, [], *own["bot"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram: %s", exc)
                try:
                    await tg_send(report, [], *own["bot"])
                except Exception:  # noqa: BLE001
                    pass
            # Гор (Анри 21.10): всё, что уходит Гору, дублируем в бот Анри
            dup = [own["bot"]] + ([("BOT_MAIN", "TG_ANRI_ID")] if args.owner == "gor" else [])
            for i, bot in enumerate(dup):
                try:
                    if i and summary:
                        await tg_send("📎 копия отчёта Гора\n" + summary, [rep_file] if rep_file and lines else [],
                                      *bot, parse_mode="HTML")
                    if lib_text:
                        await tg_send(lib_text, [], *bot, parse_mode="HTML")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Telegram %s: %s", bot, exc)
            # Лёше и Бороде — отчёт по их магазину (через бот Анри; им нужно один раз нажать /start)
            for shop_key, chat_env in (("shop3", "TG_LESHA_ID"), ("shop4", "TG_BORODA_ID")):
                mine = [ln for ln in lines if ln["shop"]["key"] == shop_key]
                if not mine or not os.getenv(chat_env, "").strip().isdigit():
                    continue
                sh_title = mine[0]["shop"]["title"]
                mres_sh = {**mres, "total": None}   # сумма заказа moba общая на всех — по магазину не показываем
                sh_file = run_dir / f"Закупка_{sh_title}_{now:%d.%m_%H%M}.html"
                try:
                    sh_file.write_text(html_report(sh_title, mine, mres_sh, mm_res, live, [], now), "utf-8")
                    await tg_send(tg_summary(sh_title, mine, mres_sh, mm_res, live, [], now), [sh_file],
                                  "BOT_MAIN", chat_env, parse_mode="HTML")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Telegram %s: %s", chat_env, exc)
    finally:
        await moba.close()
        await wbc.close()
        await http.aclose()
        await mm.close()
        await p123.close()
    # --- Liberti (Митино): дозаказать то, что пачка отнесла в «🟩 Liberti» (Анри 22.10) ---
    if live and os.getenv("LIBERTI_LIVE", "0") == "1":
        import subprocess
        try:
            subprocess.run([sys.executable, "liberti_order.py", "--owner", args.owner, "--live", "--run", str(run_dir)]
                           + (["--no-tg"] if args.no_tg else []), timeout=900, check=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("Liberti: %s", exc)
    # --- заказы, зависшие в «мог уйти» (unknown) дольше 2 часов — напоминание, а не тишина ---
    try:
        keys = [sh["key"] for sh in own["shops"]]
        async with aiosqlite.connect(DB_PATH) as db:
            q = ",".join("?" * len(keys))
            cur = await db.execute(f"SELECT shop_key, wb_order_id, ts, coalesce(title,'') FROM batch_lines "
                                   f"WHERE status='unknown' AND shop_key IN ({q})", keys)
            stuck = [r for r in await cur.fetchall()
                     if datetime.now(MSK) - datetime.fromisoformat(r[2]) > timedelta(hours=2)]
        if stuck and not args.no_tg:
            txt = ("⚠️ <b>Зависли заказы</b> (закупка «могла уйти», подтверждения нет) — автозакупка их пропускает:\n"
                   + "\n".join(f"• {r[0]} №{r[1]} с {r[2][5:16].replace('T', ' ')} {r[3][:50]}" for r in stuck)
                   + "\nПроверь у поставщика. Если не куплено — напиши мне номер, сниму отметку.")
            await tg_send(txt, [], *own["bot"], parse_mode="HTML")
            if args.owner == "gor":
                await tg_send(txt, [], "BOT_MAIN", "TG_ANRI_ID", parse_mode="HTML")
    except Exception as exc:  # noqa: BLE001
        log.warning("зависшие: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
