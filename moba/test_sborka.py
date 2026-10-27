# test_sborka.py
"""
ТЕСТОВАЯ СБОРКА: прогон реальных заказов WB через всю цепочку, без оформления.

Магазины: Анри, Лёша, Борода (Гор не трогаем).
Берёт ВСЕ позиции:
  - новые заказы (/api/v3/orders/new);
  - заказы «на сборке» (supplierStatus=confirm) за последние --days дней.
Для каждой позиции: название WB → категория → связка или автоподбор → карточка moba
(цена, наличие в Митино, остаток) → разница с ценой WB → список проблем.
В конце собирает ОДНУ общую корзину на moba (Митино, все позиции с количеством),
открывает страницу оформления, сверяет итог, делает скриншот и очищает корзину.
Кнопку «Оформить заказ» НЕ нажимает никогда (от LIVE_ORDERS не зависит).

Ничего не меняет: ни в WB, ни в связках, ни в обучении, ни в задачах бота.
Всё пишет в data/test_runs/<время>/ и упаковывает в data/test_runs/<время>.zip.

Запуск:  venv/bin/python test_sborka.py [--days 2] [--no-cart] [--no-tg]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import shutil
import sys
import time
import traceback
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

from learning import Learned
from matcher import find_candidates, parse_wb_title
from moba_client import MobaClient
from moba_order import (
    BASE_URL, CITY_ID, MITINO_STORE_ID, MITINO_STORE_LABEL, MobaOrderer,
)
from storage import Storage
from training import card_category, CATEGORIES
from wb_content import WbContent
from suppliers_ext import MasterMobileClient, Poisk123Client, find_elsewhere, format_elsewhere

DB_PATH = "data/autozakaz.db"
MSK = ZoneInfo("Europe/Moscow")
WB = "https://marketplace-api.wildberries.ru"
TEST_SHOPS = [
    {"key": "shop2", "title": "Анри", "env": "WB_SHOP2"},
    {"key": "shop3", "title": "Лёша", "env": "WB_SHOP3"},
    {"key": "shop4", "title": "Борода", "env": "WB_SHOP4"},
]
CAT_TITLES = {c["key"]: c["title"] for c in CATEGORIES}
CANCEL_WB = {"canceled", "canceled_by_client", "declined_by_client", "defect"}

log = logging.getLogger("test_sborka")


# ---------------------------------------------------------------------- #
# WB (только чтение)
# ---------------------------------------------------------------------- #
async def wb_call(client: httpx.AsyncClient, method: str, url: str, token: str,
                  **kw) -> Any:
    for attempt in range(6):
        try:
            r = await client.request(method, url, headers={"Authorization": token}, **kw)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            log.warning("WB сеть %s, повтор", exc)
            await asyncio.sleep(3 + attempt * 3)
            continue
        if r.status_code == 429:
            wait = float(r.headers.get("X-Ratelimit-Retry") or r.headers.get("Retry-After") or 20)
            log.warning("WB 429, ждём %.0f c", wait)
            await asyncio.sleep(min(max(wait, 2), 70))
            continue
        if r.status_code >= 500:
            await asyncio.sleep(3 + attempt * 3)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"WB {url} → HTTP {r.status_code}: {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"WB {url}: не удалось после повторов")


def rub(raw: dict) -> Optional[float]:
    v = raw.get("convertedPrice")
    if raw.get("convertedCurrencyCode") not in (None, 643) or v is None:
        v = raw.get("price") if raw.get("currencyCode") in (None, 643) else None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return round(float(v) / 100.0, 2)
    return None


async def collect_orders(client: httpx.AsyncClient, days: int) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    date_from = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    for shop in TEST_SHOPS:
        token = os.getenv(shop["env"], "").strip()
        if not token:
            errors.append(f"{shop['title']}: нет токена {shop['env']}")
            continue
        seen: set[int] = set()
        # 1) новые
        try:
            data = await wb_call(client, "GET", f"{WB}/api/v3/orders/new", token)
            for o in data.get("orders") or []:
                seen.add(int(o["id"]))
                rows.append({"shop": shop, "stage": "новый", "raw": o,
                             "supplierStatus": "new", "wbStatus": None})
            log.info("%s: новых %d", shop["title"], len(data.get("orders") or []))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{shop['title']}: /orders/new — {exc}")
        # 2) за N дней → статусы → только «на сборке»
        try:
            hist: list[dict] = []
            nxt = 0
            for _ in range(20):
                d = await wb_call(client, "GET", f"{WB}/api/v3/orders", token,
                                  params={"limit": 1000, "next": nxt, "dateFrom": date_from})
                hist.extend(d.get("orders") or [])
                nxt = d.get("next") or 0
                if not d.get("orders") or len(d.get("orders")) < 1000:
                    break
            ids = [int(o["id"]) for o in hist if int(o["id"]) not in seen]
            statuses: dict[int, dict] = {}
            for i in range(0, len(ids), 1000):
                st = await wb_call(client, "POST", f"{WB}/api/v3/orders/status", token,
                                   json={"orders": ids[i:i + 1000]})
                for s in st.get("orders") or []:
                    statuses[int(s["id"])] = s
            n = 0
            for o in hist:
                oid = int(o["id"])
                if oid in seen:
                    continue
                s = statuses.get(oid) or {}
                if s.get("supplierStatus") != "confirm":
                    continue
                n += 1
                rows.append({"shop": shop, "stage": "на сборке", "raw": o,
                             "supplierStatus": s.get("supplierStatus"),
                             "wbStatus": s.get("wbStatus")})
            log.info("%s: за %d дн. всего %d, на сборке %d", shop["title"], days, len(hist), n)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{shop['title']}: история/статусы — {exc}")
    return rows, errors


# ---------------------------------------------------------------------- #
# moba: подбор по одной позиции
# ---------------------------------------------------------------------- #
async def product_info(moba: MobaClient, url: str, cache: dict) -> dict:
    if url in cache:
        return cache[url]
    info: dict = {"url": url}
    try:
        p = await moba.get_product(url)
        mitino = "unknown"
        if getattr(p, "_stock_block_found", False):
            mitino = "yes" if p.stock_by_store.get("Митино") else "no"
        info.update({
            "ok": True, "product_id": p.product_id, "name": p.name, "article": p.article,
            "price": p.price, "max_qty": p.max_qty, "mitino": mitino,
            "stores_in_stock": sorted(k for k, v in p.stock_by_store.items() if v),
        })
    except Exception as exc:  # noqa: BLE001
        info.update({"ok": False, "error": str(exc)})
    cache[url] = info
    return info


async def resolve(storage: Storage, moba: MobaClient, learned: Learned, shop_key: str,
                  nm_id: Optional[int], title: str, pcache: dict) -> dict:
    out: dict = {"source": None, "problems": []}
    link = await storage.get_link(shop_key, nm_id)
    out["link"] = link
    cand_res: Optional[dict] = None

    async def candidates() -> dict:
        nonlocal cand_res
        if cand_res is None:
            t0 = time.monotonic()
            try:
                cand_res = await find_candidates(moba, title, learned=learned)
            except Exception as exc:  # noqa: BLE001
                cand_res = {"items": [], "hidden": 0, "queries": [], "error": str(exc)}
            cand_res["_sec"] = round(time.monotonic() - t0, 1)
        return cand_res

    chosen_url: Optional[str] = None
    if link and link.get("supplier_url"):
        out["source"] = "связка"
        chosen_url = link["supplier_url"]
    else:
        res = await candidates()
        items = res.get("items") or []
        if items and items[0].get("recommended"):
            out["source"] = "автоподбор ⭐"
            chosen_url = items[0]["url"]
        elif items:
            out["source"] = "нет уверенного"
            out["problems"].append("нет связки, бот не уверен — нужен выбор человека")
        elif res.get("skip_reason"):
            out["source"] = "не moba"
            out["problems"].append(res["skip_reason"])
        else:
            out["source"] = "не найдено"
            out["problems"].append("на moba ничего не нашлось" +
                                   (f" (ошибка: {res['error']})" if res.get("error") else ""))

    if chosen_url:
        info = await product_info(moba, chosen_url, pcache)
        out["moba"] = info
        if not info.get("ok"):
            out["problems"].append(f"карточка moba не открылась: {info.get('error')}")
        elif info["mitino"] == "no":
            out["problems"].append("в Митино нет")
        elif info["mitino"] == "unknown":
            out["problems"].append("наличие в Митино не определилось")
        # если связанного нет в Митино — что предложил бы бот вместо него
        if out["source"] == "связка" and (not info.get("ok") or info.get("mitino") != "yes"):
            res = await candidates()
            alt = [i for i in res.get("items") or [] if i.get("mitino") == "yes"]
            if alt:
                out["alternative"] = alt[0]
    if cand_res is not None:
        out["candidates"] = {
            "queries": cand_res.get("queries"), "hidden": cand_res.get("hidden"),
            "sec": cand_res.get("_sec"), "error": cand_res.get("error"),
            "items": [{k: i.get(k) for k in ("name", "price", "mitino", "quality",
                                              "quality_label", "frame", "recommended", "url")}
                      for i in (cand_res.get("items") or [])],
        }
    return out


# ---------------------------------------------------------------------- #
# moba: общая корзина (без оформления)
# ---------------------------------------------------------------------- #
async def cart_dry_run(login: str, password: str, wanted: "OrderedDict[int, dict]",
                       run_dir: Path, keep_cart: bool = False) -> dict:
    """wanted: product_id -> {qty, price, name}. Кнопку оформления не трогаем."""
    res: dict = {"ok": False, "steps": []}
    orderer = MobaOrderer(login, password, "anri", dry_run=True)
    added = False
    try:
        await orderer.start()
        cart = await orderer._get_cart()
        foreign = cart.get("cartProducts") or []
        if foreign:
            res["error"] = ("в корзине moba уже есть товары: " +
                            ", ".join(str(p.get("title") or p.get("productId")) for p in foreign) +
                            " — корзину не трогал")
            return res
        await orderer._api("POST", "/api/choose-store", {"store_id": MITINO_STORE_ID, "city_id": CITY_ID})
        for pid, w in wanted.items():
            step = {"product_id": pid, "name": w["name"], "qty": w["qty"]}
            r = await orderer._api("POST", "/api/cart/items", {"productId": int(pid)})
            added = True
            step["add_http"] = r.get("status")
            if r.get("status") != 200 or not isinstance(r.get("data"), dict):
                step["error"] = (r.get("text") or "")[:300]
                res["steps"].append(step)
                continue
            if w["qty"] > 1:
                bid = None
                for cp in r["data"].get("cartProducts") or []:
                    if int(cp.get("productId") or -1) == int(pid):
                        bid = cp.get("basketItemId")
                q = await orderer._api("POST", "/api/cart/items/quantity", {
                    "productId": int(pid), "basketItemId": int(bid or 0), "quantity": int(w["qty"])})
                step["qty_http"] = q.get("status")
                if q.get("status") != 200:
                    step["error"] = "количество: " + (q.get("text") or "")[:300]
            res["steps"].append(step)

        cart = await orderer._get_cart()
        (run_dir / "cart.json").write_text(json.dumps(cart, ensure_ascii=False, indent=2), "utf-8")
        res["store_id"] = (cart.get("store") or {}).get("id")
        cart_by_pid = {int(p.get("productId") or -1): p for p in cart.get("cartProducts") or []}
        diffs = []
        for pid, w in wanted.items():
            p = cart_by_pid.get(int(pid))
            if p is None:
                diffs.append(f"{w['name']}: нет в корзине")
                continue
            if int(p.get("quantity") or 0) != w["qty"]:
                diffs.append(f"{w['name']}: в корзине {p.get('quantity')} шт., нужно {w['qty']}")
            if str(p.get("availabilityStatus")) != "in_stock":
                diffs.append(f"{w['name']}: статус {p.get('availabilityStatus')}")
            if int(p.get("price") or 0) > int(w["price"] or 0):
                diffs.append(f"{w['name']}: цена в корзине {p.get('price')} > карточки {w['price']}")
        res["cart_diffs"] = diffs
        res["cart_items"] = {str(pid): {"qty": int(p.get("quantity") or 0),
                                        "status": str(p.get("availabilityStatus")),
                                        "price": p.get("price")}
                             for pid, p in cart_by_pid.items()}
        res["prices"] = [{"product_id": pid, "name": w["name"], "card": w["price"],
                          "cart": (cart_by_pid.get(int(pid)) or {}).get("price")}
                         for pid, w in wanted.items()]

        page = orderer._page
        await page.goto(f"{BASE_URL}/order/", wait_until="domcontentloaded")
        try:
            await page.get_by_role("button", name="Оформить заказ").wait_for(state="visible", timeout=20000)
        except Exception as exc:  # noqa: BLE001
            res["steps"].append({"order_page": f"кнопка не появилась: {exc}"})
        html = await page.content()
        oj = orderer._extract_order_json(html)
        if oj:
            (run_dir / "order_page.json").write_text(json.dumps(oj, ensure_ascii=False, indent=2), "utf-8")
            ob = oj.get("order") or {}
            summ = ob.get("summary") or {}
            res.update({
                "canOrder": ob.get("canOrder"), "errors": ob.get("errors"),
                "total": summ.get("total"), "deliveryPrice": summ.get("deliveryPrice"),
                "groups": [{"store": g.get("store"), "items": len(g.get("items") or []),
                            "unavailable": [i.get("title") or i.get("productId")
                                            for i in g.get("items") or [] if i.get("available") is not True]}
                           for g in ob.get("groups") or []],
            })
            res["dom_check"] = await orderer._check_dom_state()
        else:
            res["error"] = "не удалось извлечь JSON со страницы /order/"
        try:
            await page.screenshot(path=str(run_dir / "order_page.png"), full_page=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("скриншот: %s", exc)
        res["ok"] = True
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{exc}"
        res["trace"] = traceback.format_exc()
    finally:
        if added and keep_cart:
            res["cart_cleared"] = False
            res["kept"] = True
        elif added:
            await orderer._clear_cart()
            try:
                left = (await orderer._get_cart()).get("cartProducts") or []
                res["cart_cleared"] = not left
            except Exception:  # noqa: BLE001
                res["cart_cleared"] = None
        await orderer.close()
    return res


# ---------------------------------------------------------------------- #
# Отчёт
# ---------------------------------------------------------------------- #
def fmt_money(v: Any) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


async def tg_send(text: str, files: list[Path], bot_env: str = "BOT_MAIN", chat_env: str = "TG_ANRI_ID",
                  parse_mode: Optional[str] = None) -> None:
    token = os.getenv(bot_env, "").strip()
    chat = os.getenv(chat_env, "").strip()
    if not token or not chat.isdigit():
        return
    from aiogram import Bot
    from aiogram.types import FSInputFile
    proxy = os.getenv("TG_PROXY", "").strip()
    if proxy:
        from aiogram.client.session.aiohttp import AiohttpSession
        bot = Bot(token=token, session=AiohttpSession(proxy=proxy))
    else:
        bot = Bot(token=token)
    try:
        if parse_mode and len(text) > 4000:   # не резать HTML посреди тега
            text = text[:3900].rsplit("\n", 1)[0] + "\n…"
        await bot.send_message(int(chat), text[:4000], parse_mode=parse_mode,
                               disable_web_page_preview=True)
        for f in files:
            if f.exists():
                await bot.send_document(int(chat), FSInputFile(str(f)))
    finally:
        await bot.session.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2, help="за сколько дней брать заказы «на сборке»")
    ap.add_argument("--no-cart", action="store_true", help="не собирать корзину на moba")
    ap.add_argument("--no-tg", action="store_true", help="не присылать итог в бот")
    ap.add_argument("--keep-cart", action="store_true",
                    help="оставить товары в корзине moba (оформить вручную на сайте); кнопку скрипт всё равно не жмёт")
    args = ap.parse_args()

    load_dotenv()
    stamp = datetime.now(MSK).strftime("%Y%m%d_%H%M")
    run_dir = Path("data/test_runs") / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        handlers=[logging.FileHandler(run_dir / "run.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    log.info("ТЕСТОВАЯ СБОРКА %s — заказ НЕ оформляется", stamp)
    t_start = time.monotonic()
    wall_start = time.time()
    summary = ""

    storage = Storage(DB_PATH)
    await storage.init()
    learned = Learned(await storage.feedback_rows(owner="anri"))
    moba = MobaClient(os.getenv("MOBA_LOGIN", "").strip(), os.getenv("MOBA_PASSWORD", "").strip(), "anri")
    wbc = WbContent(storage)
    http = httpx.AsyncClient(timeout=25.0)
    mm_client = MasterMobileClient()
    p123_client = Poisk123Client()
    ecache: dict[tuple, dict] = {}

    try:
        orders, wb_errors = await collect_orders(http, args.days)
        (run_dir / "wb_orders_raw.json").write_text(json.dumps(
            [{"shop": o["shop"]["title"], "stage": o["stage"], "wbStatus": o["wbStatus"],
              "raw": o["raw"]} for o in orders], ensure_ascii=False, indent=2), "utf-8")
        log.info("Всего позиций: %d", len(orders))

        lines: list[dict] = []
        rcache: dict[tuple, dict] = {}
        pcache: dict[str, dict] = {}
        for n, o in enumerate(orders, 1):
            shop, raw = o["shop"], o["raw"]
            nm_id = raw.get("nmId")
            token = os.getenv(shop["env"], "").strip()
            title = None
            try:
                title = await wbc.get_title(shop["key"], nm_id, token)
            except Exception as exc:  # noqa: BLE001
                log.warning("название %s: %s", nm_id, exc)
            title = title or raw.get("article") or f"nmId {nm_id}"
            cat = card_category({"title": title})
            parsed = parse_wb_title(title)
            line: dict = {
                "n": n, "shop": shop["title"], "shop_key": shop["key"], "stage": o["stage"],
                "wbStatus": o["wbStatus"], "wb_order_id": raw.get("id"), "nm_id": nm_id,
                "article": raw.get("article"), "created_at": raw.get("createdAt"),
                "wb_price": rub(raw), "title": title, "category": cat,
                "category_title": CAT_TITLES.get(cat, "не запчасть / не распознано"),
                "parsed": {k: parsed.get(k) for k in ("type", "brand", "models", "quality")},
                "problems": [],
            }
            if o["wbStatus"] in CANCEL_WB:
                line["problems"].append(f"WB статус {o['wbStatus']} — заказ отменён, не закупаем")
            key = (shop["key"], nm_id)
            t0 = time.monotonic()
            if key not in rcache:
                rcache[key] = await resolve(storage, moba, learned, shop["key"], nm_id, title, pcache)
            r = rcache[key]
            line["sec"] = round(time.monotonic() - t0, 1)
            line.update({k: v for k, v in r.items() if k != "problems"})
            line["problems"] += list(r["problems"])
            if cat is None:
                line["problems"].append("категория не распознана (не дисплей/крышка/шлейф/АКБ?)")
            elif cat != "disp":
                line["problems"].append(f"этап «{CAT_TITLES[cat]}» ещё не обучен — подбор не проверен")
            if not parsed.get("models"):
                line["problems"].append("модель из названия WB не вытащилась")
            m = line.get("moba") or {}
            if m.get("ok") and line["wb_price"]:
                line["diff"] = round(line["wb_price"] - m["price"], 2)
                if m["price"] >= line["wb_price"]:
                    line["problems"].append(
                        f"закупка {m['price']} ₽ ≥ цены продажи {line['wb_price']:.0f} ₽")
            line["to_buy"] = bool(m.get("ok") and m.get("mitino") == "yes"
                                  and o["wbStatus"] not in CANCEL_WB)
            # не берём на moba — ищем в Мастер Мобайле и на poisk123 (Митино)
            if not line["to_buy"] and o["wbStatus"] not in CANCEL_WB:
                if key not in ecache:
                    try:
                        ecache[key] = await find_elsewhere(title, mm_client, p123_client)
                    except Exception as exc:  # noqa: BLE001
                        ecache[key] = {"error": str(exc), "mitino": [], "mitino_other_quality": [],
                                       "mm_not_mitino": [], "errors": [str(exc)], "wb_quality": None}
                ext = ecache[key]
                line["elsewhere"] = ext
                line["elsewhere_text"] = format_elsewhere(ext)
                if ext["mitino"]:
                    b = ext["mitino"][0]
                    line["problems"].append(
                        f"есть в Митино: {b.get('shop') or b['supplier']} "
                        f"{b.get('price_club') or b.get('opt') or b.get('price')} ₽")
                else:
                    line["problems"].append("НЕТ НИГДЕ В МИТИНО (moba, Мастер Мобайл, poisk123)")
            lines.append(line)
            log.info("[%d/%d] %s №%s · %s → %s · %s · %s", n, len(orders), shop["title"],
                     raw.get("id"), title[:60], line["source"], m.get("price"),
                     "; ".join(line["problems"]) or "ок")
            with (run_dir / "lines.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")

        # ---- общая корзина ----
        wanted: "OrderedDict[int, dict]" = OrderedDict()
        for ln in lines:
            if not ln["to_buy"]:
                continue
            m = ln["moba"]
            w = wanted.setdefault(m["product_id"], {"qty": 0, "price": m["price"], "name": m["name"],
                                                     "max_qty": m.get("max_qty"), "orders": []})
            w["qty"] += 1
            w["orders"].append(f"{ln['shop']} №{ln['wb_order_id']}")
        for pid, w in wanted.items():
            if w.get("max_qty") and w["qty"] > w["max_qty"]:
                for ln in lines:
                    if ln.get("moba", {}).get("product_id") == pid:
                        ln["problems"].append(f"нужно {w['qty']} шт., на moba можно взять максимум {w['max_qty']}")
        cart_res: dict = {"skipped": True}
        if wanted and not args.no_cart:
            log.info("Собираю общую корзину на moba: %d товаров, %d шт.",
                     len(wanted), sum(w["qty"] for w in wanted.values()))
            cart_res = await cart_dry_run(os.getenv("MOBA_LOGIN", "").strip(),
                                          os.getenv("MOBA_PASSWORD", "").strip(), wanted, run_dir,
                                          keep_cart=args.keep_cart)
            log.info("Корзина: %s", {k: v for k, v in cart_res.items() if k not in ("trace", "steps")})

        # ---- не хватило на moba (кол-во меньше / «ожидается поступление») → остаток ищем дальше ----
        ci = cart_res.get("cart_items") or {}
        if ci:
            for pid, w in wanted.items():
                c = ci.get(str(pid)) or {}
                got = c.get("qty", 0) if c.get("status") == "in_stock" else 0
                short = w["qty"] - got
                if short <= 0:
                    continue
                victims = [ln for ln in lines if ln["to_buy"] and (ln.get("moba") or {}).get("product_id") == pid][-short:]
                for ln in victims:
                    ln["to_buy"] = False
                    ln["problems"].append(
                        f"на moba не хватает: в корзине {got} из {w['qty']} ({c.get('status') or 'нет'}) → ищем дальше")
                    key = (ln["shop_key"], ln["nm_id"], "short")
                    if key not in ecache:
                        try:
                            ecache[key] = await find_elsewhere(ln["title"], mm_client, p123_client)
                        except Exception as exc:  # noqa: BLE001
                            ecache[key] = {"mitino": [], "mitino_other_quality": [], "mm_not_mitino": [],
                                           "errors": [str(exc)], "wb_quality": None}
                    ln["elsewhere"] = ecache[key]
                    ln["elsewhere_text"] = format_elsewhere(ecache[key])
                w["qty"] = got
            wanted = OrderedDict((k, v) for k, v in wanted.items() if v["qty"] > 0)

        # ---- файлы ----
        with (run_dir / "report.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["№", "Магазин", "Этап WB", "Заказ WB", "nmId", "Артикул", "Название WB",
                        "Категория", "Качество WB", "Источник", "Товар moba", "Цена moba",
                        "Митино", "Цена WB", "Разница", "Закупаем", "Проблемы", "Ссылка moba", "Сек", "Где ещё (Митино)"])
            for ln in lines:
                m = ln.get("moba") or {}
                w.writerow([ln["n"], ln["shop"], ln["stage"], ln["wb_order_id"], ln["nm_id"], ln["article"],
                            ln["title"], ln["category_title"], ln["parsed"].get("quality"), ln["source"],
                            m.get("name", ""), m.get("price", ""), m.get("mitino", ""), ln["wb_price"],
                            ln.get("diff", ""), "да" if ln["to_buy"] else "нет",
                            " | ".join(ln["problems"]), m.get("url", ""), ln.get("sec"),
                            ln.get("elsewhere_text", "")])
        with (run_dir / "cart.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["productId", "Товар moba", "Кол-во", "Цена", "Сумма", "Заказы WB"])
            for pid, it in wanted.items():
                w.writerow([pid, it["name"], it["qty"], it["price"], it["qty"] * it["price"],
                            ", ".join(it["orders"])])
        (run_dir / "cart_result.json").write_text(json.dumps(cart_res, ensure_ascii=False, indent=2,
                                                             default=str), "utf-8")

        # ---- сводка ----
        total = len(lines)
        by_src = Counter(ln["source"] for ln in lines)
        by_cat = Counter(ln["category_title"] for ln in lines)
        by_shop = Counter(ln["shop"] for ln in lines)
        buy = [ln for ln in lines if ln["to_buy"]]
        buy_sum = sum(w["qty"] * w["price"] for w in wanted.values())
        sell_sum = sum(ln["wb_price"] or 0 for ln in buy)
        prob = Counter()
        for ln in lines:
            for p in ln["problems"]:
                prob[p.split(" — ")[0].split(":")[0][:70]] += 1
        sm = [f"🧪 Тестовая сборка {stamp} (заказ НЕ оформлен)",
              f"Позиций: {total} · " + ", ".join(f"{k} {v}" for k, v in by_shop.items()),
              f"Этапы: новые {sum(1 for l in lines if l['stage']=='новый')}, "
              f"на сборке {sum(1 for l in lines if l['stage']=='на сборке')}",
              "Категории: " + ", ".join(f"{k} {v}" for k, v in by_cat.most_common()),
              "Подбор: " + ", ".join(f"{k} {v}" for k, v in by_src.most_common()),
              f"К закупке в Митино: {len(buy)} шт. ({len(wanted)} товаров) на {fmt_money(buy_sum)} ₽, "
              f"продано на WB за {fmt_money(sell_sum)} ₽",
              f"Не закупается автоматически: {total - len(buy)}"]
        if not cart_res.get("skipped"):
            if cart_res.get("ok"):
                sm.append(f"Корзина moba: итог {fmt_money(cart_res.get('total'))} ₽, "
                          f"canOrder={cart_res.get('canOrder')}, доставка {cart_res.get('deliveryPrice')}, "
                          f"проверка формы: {cart_res.get('dom_check') or 'ок'}, "
                          + ("корзина ОСТАВЛЕНА на moba — оформи вручную на сайте или очисти"
                             if cart_res.get("kept") else
                             f"очищена: {'да' if cart_res.get('cart_cleared') else 'НЕТ ⚠️'}"))
                if cart_res.get("cart_diffs"):
                    sm.append("Расхождения корзины: " + "; ".join(cart_res["cart_diffs"][:10]))
                if cart_res.get("total") is not None and int(cart_res["total"]) != int(buy_sum):
                    sm.append(f"⚠️ Итог корзины {cart_res['total']} ≠ сумме по карточкам {buy_sum}")
                    sm += [f"   {p['card']} → {p['cart']} ₽ · {p['name'][:50]}"
                           for p in cart_res.get("prices") or [] if p.get("cart") != p.get("card")]
            else:
                sm.append(f"Корзина moba: ОШИБКА — {cart_res.get('error')}")
        if wb_errors:
            sm.append("Ошибки WB: " + "; ".join(wb_errors))
        ext_lines = [ln for ln in lines if ln.get("elsewhere_text")]
        if ext_lines:
            sm.append("Не на moba:")
            for ln in ext_lines:
                sm.append(f"— {ln['shop']} №{ln['wb_order_id']} · {ln['title'][:60]}")
                sm += ["   " + x for x in ln["elsewhere_text"].split("\n")[:4]]
        if prob:
            sm.append("Проблемы:")
            sm += [f"  • {k} — {v}" for k, v in prob.most_common(15)]
        sm.append(f"Время: {round((time.monotonic() - t_start) / 60, 1)} мин")
        summary = "\n".join(sm)
        (run_dir / "summary.txt").write_text(summary + "\n", "utf-8")
        log.info("\n%s", summary)
    finally:
        await moba.close()
        await wbc.close()
        await http.aclose()
        await mm_client.close()
        await p123_client.close()

    # debug-файлы подбора за время прогона — тоже в архив
    try:
        dbg = run_dir / "match_debug"
        dbg.mkdir(exist_ok=True)
        for p in Path("debug").glob("match_*.json"):
            if p.stat().st_mtime >= wall_start - 5:
                shutil.copy2(p, dbg / p.name)
    except Exception as exc:  # noqa: BLE001
        log.warning("debug: %s", exc)

    zip_path = shutil.make_archive(str(run_dir), "zip", root_dir=run_dir.parent, base_dir=run_dir.name)
    log.info("Архив: %s", zip_path)

    if not args.no_tg:
        try:
            await tg_send(summary, [run_dir / "report.csv", run_dir / "cart.csv"])
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram: %s", exc)
    print(f"\nГОТОВО: {zip_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
