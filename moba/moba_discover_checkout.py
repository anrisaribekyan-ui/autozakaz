"""
Разведка №2 moba.ru: выбор магазина Митино, корзина, страница оформления.
ЗАКАЗ НЕ СОЗДАЁТСЯ:
  - финальную кнопку скрипт не нажимает;
  - на странице оформления все POST/PUT/PATCH-запросы, кроме чтения, блокируются на уровне браузера.
В конце товар убирается из корзины.

Запуск (на сервере, в ~/moba):
    venv/bin/python moba_discover_checkout.py
Результат: ~/moba-discovery2/
"""
import asyncio
import json
import os
import re
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "https://moba.ru"
OUT = Path.home() / "moba-discovery2"
OUT.mkdir(exist_ok=True)
CHEAP_QUERY = "держатель sim"   # дешёвый товар для пробы

for line in Path(__file__).with_name(".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
LOGIN, PASSWORD = os.environ["MOBA_LOGIN"], os.environ["MOBA_PASSWORD"]

SKIP = ("mc.yandex", "google", "vk.com", "jivo", ".woff", ".ttf", ".png", ".jpg", ".webp", ".svg", ".gif", ".css", ".js")
net = open(OUT / "network.jsonl", "w", encoding="utf-8")
state = {"phase": "start", "block_writes": False}


def mask(t):
    return t.replace(PASSWORD, "***") if t else t


async def on_response(resp):
    req = resp.request
    if any(s in req.url for s in SKIP):
        return
    e = {"phase": state["phase"], "method": req.method, "url": req.url, "status": resp.status,
         "type": req.resource_type, "post": mask((req.post_data or "")[:3000])}
    if req.resource_type in ("xhr", "fetch"):
        try:
            e["body"] = mask((await resp.text())[:4000])
        except Exception:
            pass
    net.write(json.dumps(e, ensure_ascii=False) + "\n")
    net.flush()


async def guard(route):
    req = route.request
    if state["block_writes"] and req.method in ("POST", "PUT", "PATCH", "DELETE"):
        net.write(json.dumps({"phase": state["phase"], "BLOCKED": True, "method": req.method,
                              "url": req.url, "post": (req.post_data or "")[:3000]}, ensure_ascii=False) + "\n")
        net.flush()
        print(f"   [защита] заблокирован {req.method} {req.url}")
        await route.abort()
    else:
        await route.continue_()


async def snap(page, name):
    await page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    (OUT / f"{name}.html").write_text(mask(await page.content()), encoding="utf-8")
    print(f"  сохранено: {name}")


async def settle(page, ms=2500):
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(ms)


async def click_text(page, text, exact=False, timeout=4000):
    loc = page.get_by_text(text, exact=exact)
    for i in range(await loc.count()):
        el = loc.nth(i)
        try:
            if await el.is_visible():
                await el.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 1000}, locale="ru-RU")
        page = await ctx.new_page()
        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await ctx.route("**/*", guard)

        # 1. Вход
        state["phase"] = "login"
        print("1. Вход")
        await page.goto(f"{BASE}/personal/")
        await settle(page)
        pwd = page.locator("input[type=password]:visible").first
        form = pwd.locator("xpath=ancestor::form[1]")
        await form.locator("input[type=email]:visible, input[type=text]:visible, input[type=tel]:visible").first.fill(LOGIN)
        await pwd.fill(PASSWORD)
        await pwd.press("Enter")
        await settle(page, 3000)

        # 2. Выбор магазина Митино
        state["phase"] = "store_select"
        print("2. Выбор магазина Митино")
        await page.goto(f"{BASE}/")
        await settle(page)
        opened = await click_text(page, "Савёлово") or await click_text(page, "Москва")
        await settle(page, 2000)
        await snap(page, "01_store_modal")
        if opened and await click_text(page, "Митино"):
            await settle(page, 2000)
            await snap(page, "02_store_after_click")
            # возможна кнопка подтверждения выбора
            for t in ("Выбрать", "Подтвердить", "Сохранить", "Применить"):
                if await click_text(page, t, exact=True):
                    await settle(page, 2000)
                    break
        await page.goto(f"{BASE}/")
        await settle(page)
        await snap(page, "03_home_after_store")
        sid = re.search(r'const storeId\s*=\s*(\d+)', await page.content())
        print(f"   текущий storeId: {sid.group(1) if sid else '?'} (Митино = 6372)")

        # 3. Дешёвый товар в корзину
        state["phase"] = "add_to_cart"
        print("3. Кладу дешёвый товар в корзину")
        await page.goto(f"{BASE}/catalog/?q={CHEAP_QUERY}")
        await settle(page, 3500)
        links = await page.eval_on_selector_all("a[href*='/catalog/']", "els => els.map(e => e.href)")
        prods = [u for u in dict.fromkeys(links) if re.match(r"^https://moba\.ru/catalog/[^/?]+/[^/?]+/$", u)]
        added = None
        for url in prods[:8]:
            await page.goto(url)
            await settle(page, 2000)
            html = await page.content()
            if "Митино" in html and "В наличии" in html:
                btn = page.get_by_role("button", name=re.compile("В корзину"))
                if await btn.count():
                    await btn.first.click()
                    await settle(page, 2500)
                    added = url
                    await snap(page, "04_product_added")
                    break
        print(f"   добавлен: {added}")
        if not added:
            print("   не нашёл подходящий товар — стоп")
            return

        # 4. Корзина
        state["phase"] = "cart"
        print("4. Корзина")
        await page.goto(f"{BASE}/cart/")
        await settle(page, 3000)
        await snap(page, "05_cart")
        # отметить «включить в заказ», если не отмечено
        boxes = page.locator("input[type=checkbox]:visible")
        for i in range(await boxes.count()):
            try:
                if not await boxes.nth(i).is_checked():
                    await boxes.nth(i).check(timeout=2000)
            except Exception:
                pass
        await settle(page, 2000)
        await snap(page, "06_cart_checked")

        # 5. Страница оформления — с этого момента любые записи блокируются
        state["phase"] = "checkout"
        state["block_writes"] = True
        print("5. Оформление (запись заблокирована)")
        if await click_text(page, "Перейти к оформлению"):
            await settle(page, 4000)
        print(f"   URL оформления: {page.url}")
        await snap(page, "07_checkout")
        # раскрыть варианты получения/оплаты, ничего не отправляя
        for t in ("Самовывоз", "Митино", "Наличными"):
            if await click_text(page, t):
                await settle(page, 1500)
        await snap(page, "08_checkout_options")
        (OUT / "checkout_url.txt").write_text(page.url, encoding="utf-8")

        # 6. Убрать товар из корзины
        state["block_writes"] = False
        state["phase"] = "cleanup"
        print("6. Убираю товар из корзины")
        await page.goto(f"{BASE}/cart/")
        await settle(page, 3000)
        if not await click_text(page, "Очистить"):
            print("   кнопка «Очистить» не найдена — удали товар из корзины вручную")
        await settle(page, 2500)
        for t in ("Да", "Удалить", "Очистить"):
            await click_text(page, t, exact=True)
        await settle(page, 2500)
        await snap(page, "09_cart_after_cleanup")
        cnt = await page.evaluate("fetch('/api/user/items/count').then(r=>r.json())")
        print(f"   в корзине сейчас: {cnt}")

        await browser.close()
    net.close()
    print(f"\nГотово. Всё в {OUT}")


asyncio.run(main())
