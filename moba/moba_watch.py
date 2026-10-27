# moba_watch.py
"""
Сторож заказов moba: moba иногда отменяет заказ (или часть) и не сообщает.
Раз в час (systemd timer) открывает личный кабинет moba, читает список заказов и их статусы,
сравнивает с прошлым разом и пишет Анри в Telegram, если статус изменился на «отменён»
(и вообще при любой смене статуса — коротко).

Только чтение. Первый запуск сохраняет страницы в debug/moba_watch/ для проверки разбора.

Запуск:  venv/bin/python moba_watch.py [--no-tg] [--all]   (--all — прислать текущие статусы всех заказов)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from moba_order import BASE_URL, MobaOrderer

DB_PATH = "data/autozakaz.db"
DEBUG = Path("debug/moba_watch")
MSK = ZoneInfo("Europe/Moscow")
# Текущие заказы + история (moba: «Посмотреть историю заказов» = ?filter_history=Y)
CANDIDATE_PAGES = ["/personal/orders/", "/personal/orders/?filter_history=Y"]
STATUS_WORDS = ["отмен", "аннулир", "готов к выдаче", "можно забирать", "собран", "в сборке", "принят",
                "новый", "выдан", "получен", "выполнен", "ожидает", "в пути", "передан", "частично"]
log = logging.getLogger("moba_watch")


def norm(t: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", (t or "").lower()).strip()


async def order_text(o: "MobaOrderer", num: str, list_html: str) -> str:
    """Текст страницы заказа №num: ищем ссылку на него в списке заказов, иначе пробуем типовые адреса."""
    urls = []
    if list_html:
        for m in re.finditer(r'href="([^"]*' + re.escape(num) + r'[^"]*)"', list_html):
            urls.append(m.group(1))
    urls += [f"/personal/orders/{num}/", f"/personal/order/{num}/", f"/personal/orders/?ID={num}",
             f"/personal/order/detail/{num}/"]
    for u in urls:
        try:
            await o._page.goto(u if u.startswith("http") else f"{BASE_URL}{u}", wait_until="domcontentloaded")
            body = await o._page.inner_text("body")
            if num in body and ("шт" in body or "₽" in body or "руб" in body):
                await o._page.screenshot(path=str(DEBUG / f"order_{num}.png"), full_page=True)
                return body
        except Exception:  # noqa: BLE001
            continue
    return ""


def parse_orders(html: str) -> dict[str, dict]:
    """Список заказов moba (разведано 18.10): блоки «Заказ №N от …, K товаров на сумму S ₽»,
    дальше шаги статуса; пройденный шаг = за ним дата. Текущий статус — первый шаг с датой."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    lines = [x.strip() for x in soup.get_text("\n").split("\n") if x.strip()]
    text = "\n".join(lines)
    out: dict[str, dict] = {}
    parts = re.split(r"\n(?=Заказ\s*\n?\s*№\s*\d{5,})", "\n" + text)
    for part in parts:
        m = re.match(r"\s*Заказ\s*№\s*(\d{5,})", part)
        if not m:
            continue
        num = m.group(1)
        cnt = re.search(r"(\d+)\s+товар\w*\s+на\s+сумм\w*\s+([\d\s\u00a0]+)", part)
        steps = part.split("\n")
        status = "?"
        for i, ln in enumerate(steps):
            low = ln.lower()
            if any(w in low for w in ("выполнен", "готов к выдаче", "принят", "отмен", "собира", "в обработке",
                                       "передан", "в пути")) and len(ln) < 40:
                nxt = steps[i + 1] if i + 1 < len(steps) else ""
                if re.match(r"\d{2}\.\d{2}\.\d{4}$", nxt):
                    status = low
                    break
        out[num] = {"status": status,
                    "count": int(cnt.group(1)) if cnt else None,
                    "total": re.sub(r"\D", "", cnt.group(2)) if cnt else None,
                    "text": " | ".join(steps[:6])[:300]}
    return out


async def wb_cancellations() -> list[str]:
    """Покупатель отменил заказ WB, а запчасть уже куплена (или куплена недавно) — предупредить."""
    import httpx
    shops = {"shop2": "WB_SHOP2", "shop3": "WB_SHOP3", "shop4": "WB_SHOP4", "gor": "WB_GOR"}
    out: list[str] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            cur = await db.execute("SELECT * FROM batch_lines WHERE status IN ('placed','supply_ok','supply_fail') "
                                   "AND ts >= date('now','-10 day')")
            rows = [dict(r) for r in await cur.fetchall()]
        except Exception:  # noqa: BLE001
            return out
        async with httpx.AsyncClient(timeout=25) as c:
            for shop_key, env in shops.items():
                mine = [r for r in rows if r["shop_key"] == shop_key]
                token = os.getenv(env, "").strip()
                if not mine or not token:
                    continue
                try:
                    resp = await c.post("https://marketplace-api.wildberries.ru/api/v3/orders/status",
                                        headers={"Authorization": token},
                                        json={"orders": [int(r["wb_order_id"]) for r in mine]})
                    st = {int(x["id"]): x for x in resp.json().get("orders") or []}
                except Exception as exc:  # noqa: BLE001
                    log.warning("WB status %s: %s", shop_key, exc)
                    continue
                for r in mine:
                    w = (st.get(int(r["wb_order_id"])) or {}).get("wbStatus") or ""
                    if "cancel" in w or "declined" in w:
                        await db.execute("UPDATE batch_lines SET status='wb_canceled', note=? "
                                         "WHERE shop_key=? AND wb_order_id=?", (w, shop_key, r["wb_order_id"]))
                        out.append(f"↩️ Покупатель отменил WB {shop_key} №{r['wb_order_id']} ({w}). Куплено: "
                                   f"«{(r.get('product_name') or '')[:60]}» {r.get('price') or ''} ₽, moba №{r.get('moba_order')}"
                                   + (" — крышку можно вернуть" if "крышк" in (r.get('title') or '').lower() else ""))
        await db.commit()
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tg", action="store_true")
    ap.add_argument("--all", action="store_true", help="прислать статусы всех найденных заказов")
    args = ap.parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    DEBUG.mkdir(parents=True, exist_ok=True)

    o = MobaOrderer(os.getenv("MOBA_LOGIN", "").strip(), os.getenv("MOBA_PASSWORD", "").strip(), "anri", dry_run=True)
    found: dict[str, dict] = {}
    list_html = ""
    try:
        await o.start()
        for path in CANDIDATE_PAGES:
            await o._page.goto(f"{BASE_URL}{path}", wait_until="domcontentloaded")
            try:
                await o._page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            html = await o._page.content()
            name = re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_") or "root"
            (DEBUG / f"{name}.html").write_text(html, "utf-8")
            await o._page.screenshot(path=str(DEBUG / f"{name}.png"), full_page=True)
            got = parse_orders(html)
            log.info("%s: заказов найдено %d", path, len(got))
            for k, v in got.items():
                found.setdefault(k, v)
            list_html += html
        # состав наших заказов (moba убирает позиции молча)
        removed: list[dict] = []
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            try:
                cur = await db.execute("SELECT * FROM batch_lines WHERE status IN ('placed','supply_ok','supply_fail') "
                                       "AND moba_order IS NOT NULL AND ts >= date('now','-5 day')")
                ours = [dict(r) for r in await cur.fetchall()]
            except Exception:  # noqa: BLE001
                ours = []
        by_order: dict[str, list[dict]] = {}
        for r in ours:
            by_order.setdefault(str(r["moba_order"]), []).append(r)
        for num, rows in by_order.items():
            text = await order_text(o, num, list_html)
            (DEBUG / f"order_{num}.txt").write_text(text, "utf-8")
            if not text:
                continue
            low = norm(text)
            missing = [r for r in rows if r.get("product_name") and norm(r["product_name"])[:60] not in low]
            if len(missing) == len(rows):
                # ни одной нашей позиции не нашли — скорее всего, не разобрали страницу; не поднимаем ложную тревогу
                log.warning("заказ %s: не нашёл на странице ни одной позиции — проверь debug/moba_watch/order_%s.*", num, num)
                continue
            removed += missing
    finally:
        await o.close()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS moba_watch (
            order_no TEXT PRIMARY KEY, status TEXT, total TEXT, first_seen TEXT, changed_at TEXT)""")
        cur = await db.execute("PRAGMA table_info(moba_watch)")
        if "cnt" not in {r[1] for r in await cur.fetchall()}:
            await db.execute("ALTER TABLE moba_watch ADD COLUMN cnt INTEGER")
        cur = await db.execute("SELECT order_no, status, total, cnt FROM moba_watch")
        prev = {r[0]: {"status": r[1], "total": r[2], "cnt": r[3]} for r in await cur.fetchall()}
        now = datetime.now(MSK).isoformat()
        changes: list[str] = []
        for num, d in found.items():
            p = prev.get(num)
            if p is None:
                await db.execute("INSERT INTO moba_watch (order_no, status, total, first_seen, changed_at, cnt) "
                                 "VALUES (?,?,?,?,?,?)", (num, d["status"], d["total"], now, now, d.get("count")))
                if prev and "отмен" in d["status"]:
                    changes.append(f"🚨 moba №{num}: {d['status'].upper()} ({d['total']} ₽)")
                # наш заказ из пачки: сверить число товаров с тем, что мы заказали
                try:
                    c2 = await db.execute("SELECT COUNT(*) FROM batch_lines WHERE moba_order=?", (num,))
                    ours = (await c2.fetchone())[0]
                except Exception:  # noqa: BLE001
                    ours = 0
                if ours and d.get("count") is not None and d["count"] < ours:
                    changes.append(f"🚨 moba №{num}: в заказе {d['count']} шт., а мы заказали {ours} — moba убрала позиции")
                continue
            # moba молча убирает позиции: уменьшилось число товаров или сумма
            if (p["cnt"] and d.get("count") is not None and d["count"] < p["cnt"]) or \
                    (p["total"] and d.get("total") and int(d["total"]) < int(p["total"])):
                changes.append(f"🚨 moba №{num}: УБРАЛИ ПОЗИЦИИ — было {p['cnt']} шт. на {p['total']} ₽, "
                               f"стало {d.get('count')} шт. на {d.get('total')} ₽")
            if p["status"] != d["status"]:
                icon = "🚨" if "отмен" in d["status"] else "ℹ️"
                changes.append(f"{icon} moba №{num}: {p['status']} → {d['status'].upper()}")
            await db.execute("UPDATE moba_watch SET status=?, total=?, cnt=?, changed_at=? WHERE order_no=?",
                             (d["status"], d["total"], d.get("count"), now, num))
        await db.commit()

    # к каким заказам WB относится отменённый заказ moba
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            for i, c in enumerate(changes):
                if "🚨" not in c:
                    continue
                num = re.search(r"№(\d+)", c).group(1)
                cur = await db.execute("SELECT shop_key, wb_order_id FROM batch_lines WHERE moba_order=?", (num,))
                rows = await cur.fetchall()
                if rows:
                    changes[i] += "\n   заказы WB: " + ", ".join(f"{r[0]} №{r[1]}" for r in rows) + " — закупить заново!"
        except Exception:  # noqa: BLE001
            pass

    async with aiosqlite.connect(DB_PATH) as db:
        for r in removed:
            await db.execute("UPDATE batch_lines SET status='rebuy', note=? WHERE shop_key=? AND wb_order_id=?",
                             (f"убрана из moba №{r['moba_order']}", r["shop_key"], r["wb_order_id"]))
            changes.append(f"🚨 moba №{r['moba_order']}: убрали позицию «{(r.get('product_name') or '')[:60]}» "
                           f"(заказ WB {r['shop_key']} №{r['wb_order_id']}) → закуплю заново в ближайшей пачке")
        await db.commit()
    changes += await wb_cancellations()

    (DEBUG / "last.json").write_text(json.dumps(found, ensure_ascii=False, indent=2), "utf-8")
    report = "\n".join(changes)
    if args.all:
        report = "\n".join([report, "Заказы moba:"] + [f"• №{n}: {d['status']} · {d.get('count')} шт. · {d['total']} ₽"
                                                        for n, d in found.items()])
    print(report or f"Изменений нет. Заказов на странице: {len(found)}")
    if not found:
        print("⚠️ Не нашёл заказов на страницах кабинета — пришли debug/moba_watch/*.png, поправлю разбор.")
    if report.strip() and not args.no_tg:
        from test_sborka import tg_send
        try:
            await tg_send(report, [])
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
