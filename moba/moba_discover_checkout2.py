"""
Разведка №3: страница оформления при магазине МИТИНО.
Заказ НЕ создаётся: финальная кнопка не нажимается, на странице оформления блокируются
все POST/PUT/PATCH/DELETE (кроме выбора способа получения — они тоже блокируются и логируются).
В конце корзина очищается. Магазин в аккаунте остаётся «Митино».

Запуск:  cd ~/moba && venv/bin/python moba_discover_checkout2.py
Результат: ~/moba-discovery3/
"""
import asyncio, json, os, re
from pathlib import Path
from playwright.async_api import async_playwright

BASE = "https://moba.ru"
OUT = Path.home() / "moba-discovery3"; OUT.mkdir(exist_ok=True)
MITINO = 6372
CHEAP_URL = "https://moba.ru/catalog/konteynery-sim/derzhatel-sim-dlya-huawei-honor-10-lite-hry-lx1-siniy/"

for line in Path(__file__).with_name(".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
LOGIN, PASSWORD = os.environ["MOBA_LOGIN"], os.environ["MOBA_PASSWORD"]

SKIP = ("yandex", "google", "vk.com", ".woff", ".ttf", ".png", ".jpg", ".webp", ".svg", ".gif", ".css", ".js")
net = open(OUT / "network.jsonl", "w", encoding="utf-8")
state = {"phase": "start", "block": False}
mask = lambda t: t.replace(PASSWORD, "***") if t else t


async def on_response(resp):
    r = resp.request
    if any(s in r.url for s in SKIP): return
    e = {"phase": state["phase"], "method": r.method, "url": r.url, "status": resp.status, "post": mask((r.post_data or "")[:4000])}
    if r.resource_type in ("xhr", "fetch"):
        try: e["body"] = mask((await resp.text())[:6000])
        except Exception: pass
    net.write(json.dumps(e, ensure_ascii=False) + "\n"); net.flush()


async def guard(route):
    r = route.request
    if state["block"] and r.method in ("POST", "PUT", "PATCH", "DELETE"):
        net.write(json.dumps({"phase": state["phase"], "BLOCKED": True, "method": r.method, "url": r.url,
                              "post": (r.post_data or "")[:6000]}, ensure_ascii=False) + "\n"); net.flush()
        print(f"   [защита] заблокирован {r.method} {r.url}")
        await route.abort()
    else:
        await route.continue_()


async def snap(page, name):
    await page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    (OUT / f"{name}.html").write_text(mask(await page.content()), encoding="utf-8")
    print(f"  сохранено: {name}")


async def settle(page, ms=2500):
    try: await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception: pass
    await page.wait_for_timeout(ms)


async def api(page, method, path, body=None):
    return await page.evaluate(
        """async ([m, p, b]) => { const r = await fetch(p, {method: m, headers: {'Content-Type':'application/json','Accept':'application/json'},
           body: b ? JSON.stringify(b) : undefined}); return {status: r.status, text: (await r.text()).slice(0, 3000)}; }""",
        [method, path, body])


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 1000}, locale="ru-RU")
        page = await ctx.new_page()
        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await ctx.route("**/*", guard)

        state["phase"] = "login"; print("1. Вход")
        await page.goto(f"{BASE}/personal/"); await settle(page)
        pwd = page.locator("input[type=password]:visible").first
        form = pwd.locator("xpath=ancestor::form[1]")
        await form.locator("input[type=email]:visible, input[type=text]:visible, input[type=tel]:visible").first.fill(LOGIN)
        await pwd.fill(PASSWORD); await pwd.press("Enter"); await settle(page, 3000)

        state["phase"] = "prepare"; print("2. Очистка корзины и выбор Митино через API")
        print("   cart/clear:", await api(page, "POST", "/api/cart/clear"))
        print("   choose-store:", await api(page, "POST", "/api/choose-store", {"store_id": MITINO, "city_id": 711}))
        await page.goto(f"{BASE}/"); await settle(page)
        sid = re.search(r'const storeId\s*=\s*(\d+)', await page.content())
        print(f"   storeId сейчас: {sid.group(1) if sid else '?'} (нужно {MITINO})")
        if not sid or int(sid.group(1)) != MITINO:
            print("   Митино не выбралось — стоп"); return

        state["phase"] = "add"; print("3. Товар в корзину")
        await page.goto(CHEAP_URL); await settle(page)
        pid = re.search(r"product:\s*\{id:\s*(\d+)", await page.content())
        res = await api(page, "POST", "/api/cart/items", {"productId": int(pid.group(1))})
        print("   cart/items:", res["status"], res["text"][:300])

        state["phase"] = "cart"; print("4. Корзина")
        await page.goto(f"{BASE}/cart/"); await settle(page, 3000); await snap(page, "01_cart")

        state["phase"] = "checkout"; state["block"] = True
        print("5. Оформление (отправка заблокирована)")
        await page.goto(f"{BASE}/order/"); await settle(page, 4000)
        await snap(page, "02_order")
        html = await page.content()
        m = re.search(r"FB_MOBA\.modules\.order\((\{.*?\})\);\s*\n", html, re.S)
        if m:
            (OUT / "order_schema.json").write_text(m.group(1), encoding="utf-8"); print("   схема формы сохранена")
        # кнопки и поля формы — только перечислить
        buttons = await page.eval_on_selector_all("button", "els => els.filter(e=>e.offsetParent).map(e=>e.innerText.trim()).filter(Boolean)")
        inputs = await page.eval_on_selector_all("input,select,textarea", "els => els.map(e=>({name:e.name,type:e.type,value:e.value,checked:e.checked,disabled:e.disabled}))")
        (OUT / "order_controls.json").write_text(json.dumps({"buttons": buttons, "inputs": inputs}, ensure_ascii=False, indent=1), encoding="utf-8")
        print("   кнопки:", buttons[-15:])

        state["phase"] = "cleanup"; state["block"] = False; print("6. Очистка корзины")
        print("   cart/clear:", await api(page, "POST", "/api/cart/clear"))
        print("   items/count:", await api(page, "GET", "/api/user/items/count"))
        await browser.close()
    net.close(); print(f"\nГотово. Всё в {OUT}")

asyncio.run(main())
