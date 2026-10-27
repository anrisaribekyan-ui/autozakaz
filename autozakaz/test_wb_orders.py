# test_wb_orders.py
"""
Тест модуля WbPoller.
На пустой базе: первый poll_once делает baseline по каждому магазину
и заводит строку в shop_state. Второй — 0 новых. pending() — 0.
Ограничение 60 с отключено (rate_limit_sec=0).
"""

import asyncio
import logging
import os
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv

from wb_orders import SHOPS, WbPoller


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


TEST_DB = "data/test_wb.db"


async def _print_shop_state(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT shop_key, baselined_at FROM shop_state ORDER BY shop_key"
        )
        rows = await cursor.fetchall()
        print("  shop_state:")
        if not rows:
            print("    (пусто)")
        for r in rows:
            print(f"    {r[0]} | baselined_at={r[1]}")


async def _print_db_summary(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT shop_key, status, COUNT(*) FROM wb_orders "
            "GROUP BY shop_key, status ORDER BY shop_key, status"
        )
        rows = await cursor.fetchall()
        print("  wb_orders (shop_key | status | count):")
        if not rows:
            print("    (пусто)")
        for r in rows:
            print(f"    {r[0]} | {r[1]} | {r[2]}")

        cursor = await db.execute(
            "SELECT shop_key, wb_order_id, article, nm_id, price_kop "
            "FROM wb_orders ORDER BY shop_key, wb_order_id LIMIT 10"
        )
        rows = await cursor.fetchall()
        if rows:
            print("  Примеры (первые 10):")
            for sk, oid, art, nm, pk in rows:
                price_rub = (
                    (pk / 100.0) if isinstance(pk, (int, float)) else None
                )
                print(
                    f"    {sk} | id={oid} | article={art} | "
                    f"nmId={nm} | price={price_rub} руб."
                )


async def _print_pending(poller: WbPoller) -> None:
    items = await poller.pending()
    print(f"  pending(): {len(items)} шт.")
    for o in items[:10]:
        print(
            f"    {o.shop_title} | id={o.wb_order_id} | "
            f"article={o.article} | nmId={o.nm_id} | price={o.price_rub}"
        )


async def main() -> None:
    load_dotenv()

    p = Path(TEST_DB)
    if p.exists():
        p.unlink()

    poller = WbPoller(db_path=TEST_DB, rate_limit_sec=0.0)
    try:
        await poller.init()

        print("\n=== Первый poll_once (baseline по каждому магазину) ===")
        first = await poller.poll_once()
        print(f"Первый poll_once вернул новых: {len(first)} (ожидается 0)")

        print("\nПо магазинам (первый опрос):")
        for shop in SHOPS:
            token = os.getenv(shop["env"], "").strip()
            if not token:
                print(
                    f"  {shop['title']}: токен {shop['env']} пуст — пропущен"
                )
                continue
            api_n = poller.last_api_count(shop["key"])
            ins_n = poller.last_insert_count(shop["key"])
            print(
                f"  {shop['title']}: API отдал {api_n}, записано {ins_n}"
            )

        print("\nshop_state после первого опроса:")
        await _print_shop_state(TEST_DB)

        print("\nwb_orders после первого опроса:")
        await _print_db_summary(TEST_DB)

        print("\npending() после первого опроса:")
        await _print_pending(poller)

        print("\nПауза 5 с...")
        await asyncio.sleep(5)

        print("\n=== Второй poll_once ===")
        second = await poller.poll_once()
        print(f"Второй poll_once вернул новых: {len(second)} (ожидается 0)")

        print("\nПо магазинам (второй опрос):")
        for shop in SHOPS:
            token = os.getenv(shop["env"], "").strip()
            if not token:
                continue
            api_n = poller.last_api_count(shop["key"])
            ins_n = poller.last_insert_count(shop["key"])
            print(
                f"  {shop['title']}: API отдал {api_n}, записано {ins_n}"
            )

        print("\nshop_state после второго опроса:")
        await _print_shop_state(TEST_DB)

        print("\npending() после второго опроса:")
        await _print_pending(poller)

    finally:
        await poller.close()


if __name__ == "__main__":
    asyncio.run(main())
