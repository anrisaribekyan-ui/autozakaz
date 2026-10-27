# test_moba_client.py
"""
Тест модуля MobaClient: логин, поиск, наличие в Митино.
Корзина не затрагивается.
"""

import asyncio
import logging
import os
import re

from dotenv import load_dotenv

from moba_client import MobaClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def _print_user_id(client: MobaClient) -> None:
    c = await client._get_client()
    resp = await c.get("/personal/", follow_redirects=False)
    m = re.search(r'const\s+userId\s*=\s*"(\d*)"', resp.text)
    if m:
        print(f"userId после входа: {m.group(1)!r}")
    else:
        print("userId после входа: не найден")


async def main() -> None:
    load_dotenv()

    login = os.getenv("MOBA_LOGIN")
    password = os.getenv("MOBA_PASSWORD")
    if not login or not password:
        raise SystemExit("Не заданы MOBA_LOGIN / MOBA_PASSWORD в .env")

    client = MobaClient(login=login, password=password, account_name="test")
    try:
        await client.login()
        await client.ensure_logged_in()
        await _print_user_id(client)

        results = await client.search("дисплей Samsung S26", limit=10)
        print(f"Найдено результатов: {len(results)}")

        for i, item in enumerate(results[:3], start=1):
            url = item["url"]
            print(f"\n--- {i}. {url} ---")
            try:
                product = await client.get_product(url)
            except Exception as exc:  # noqa: BLE001
                print(f"  Ошибка карточки: {exc}")
                continue

            mitino = await client.mitino_status(url)
            print(f"  Артикул: {product.article}")
            print(f"  Название: {product.name}")
            print(f"  Цена: {product.price} руб.")
            print(f"  Наличие в Митино (mitino_status): {mitino}")
            print(f"  max_qty: {product.max_qty}")
            print(f"  Все магазины: {product.stock_by_store}")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
