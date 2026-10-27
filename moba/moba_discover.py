"""
Разведка moba.ru: вход в аккаунт, поиск, карточка товара, корзина (только просмотр).
Ничего не заказывает и в корзину не кладёт.

Запуск:
    python3 moba_discover.py "дисплей Samsung S26"

Логин/пароль берутся из файла .env рядом со скриптом:
    MOBA_LOGIN=...
    MOBA_PASSWORD=...

Результат: папка ~/moba-discovery/ со скриншотами, HTML и логом сетевых запросов.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "https://moba.ru"
OUT = Path.home() / "moba-discovery"
OUT.mkdir(exist_ok=True)


def load_env():
    env_path = Path(__file__).with_name(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
LOGIN = os.environ.get("MOBA_LOGIN", "")
PASSWORD = os.environ.get("MOBA_PASSWORD", "")
QUERY = sys.argv[1] if len(sys.argv) > 1 else "дисплей Samsung S26"

SKIP = ("mc.yandex", "google", "vk.com", "top-fwz", "jivo", ".woff", ".ttf", ".png", ".jpg", ".webp", ".svg", ".gif", ".css")
net_log = open(OUT / "network.jsonl", "w", encoding="utf-8")


def mask(text):
    if text and PASSWORD:
        text = text.replace(PASSWORD, "***")
    return text


async def on_response(resp):
    req = resp.request
    url = req.url
    if any(s in url for s in SKIP) or req.resource_type not in ("xhr", "fetch", "document"):
        return
    entry = {
        "method": req.method,
        "url": url,
        "type": req.resource_type,
        "status": resp.status,
        "content_type": resp.headers.get("content-type", ""),
        "post_data": mask((req.post_data or "")[:2000]),
    }
    if "json" in entry["content_type"] or req.resource_type in ("xhr", "fetch"):
        try:
            body = await resp.text()
            entry["body_head"] = mask(body[:3000])
        except Exception:
            pass
    net_log.write(json.dumps(entry, ensure_ascii=False) + "\n")
    net_log.flush()


async def snap(page, name):
    await page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    (OUT / f"{name}.html").write_text(mask(await page.content()), encoding="utf-8")
    print(f"  сохранено: {name}")


async def settle(page, ms=3000):
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(ms)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 1000},
            locale="ru-RU",
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        )
        page = await ctx.new_page()
        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

        print("1. Страница входа")
        await page.goto(f"{BASE}/personal/")
        await settle(page)
        await snap(page, "01_login_page")

        if LOGIN and PASSWORD:
            print("2. Вход")
            pwd = page.locator("input[type=password]:visible").first
            form = pwd.locator("xpath=ancestor::form[1]")
            login_input = form.locator(
                "input[type=email]:visible, input[type=text]:visible, input[type=tel]:visible"
            ).first
            await login_input.fill(LOGIN)
            await pwd.fill(PASSWORD)
            await pwd.press("Enter")
            await settle(page, 4000)
            await snap(page, "02_after_login")
            logged_in = await page.locator("input[type=password]:visible").count() == 0
            print(f"   вход {'похоже, успешен' if logged_in else 'НЕ удался — смотри 02_after_login.png'}")
        else:
            print("2. Пропускаю вход: нет MOBA_LOGIN/MOBA_PASSWORD в .env")

        print(f"3. Поиск: {QUERY}")
        await page.goto(f"{BASE}/catalog/?q={QUERY}")
        await settle(page, 4000)
        await snap(page, "03_search")

        links = await page.eval_on_selector_all(
            "a[href*='/catalog/']", "els => els.map(e => e.href)"
        )
        product_links = [
            u for u in dict.fromkeys(links)
            if re.match(r"^https://moba\.ru/catalog/[^/?]+/[^/?]+/$", u)
        ]
        (OUT / "search_links.txt").write_text("\n".join(product_links), encoding="utf-8")
        print(f"   найдено ссылок на товары: {len(product_links)}")

        if product_links:
            print("4. Карточка товара")
            await page.goto(product_links[0])
            await settle(page, 4000)
            await snap(page, "04_product")

            for text in ("Самовывоз", "магазин", "Наличие", "Где купить"):
                el = page.get_by_text(text, exact=False).first
                try:
                    if await el.count() and await el.is_visible():
                        await el.click(timeout=3000)
                        await settle(page, 2500)
                        await snap(page, "05_product_stores")
                        break
                except Exception:
                    continue

        print("5. Корзина (только просмотр)")
        await page.goto(f"{BASE}/cart/")
        await settle(page)
        await snap(page, "06_cart")

        await browser.close()
    net_log.close()
    print(f"\nГотово. Всё в папке {OUT}")


asyncio.run(main())
