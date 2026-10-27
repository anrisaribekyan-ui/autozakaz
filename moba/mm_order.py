# mm_order.py
"""
Оформление заказа в Мастер Мобайле (master-mobile.ru) через Playwright.
РЕЖИМ ТОЛЬКО ПРОВЕРКИ: всё, кроме кнопки «Оформить заказ». Отправки заказа в этом модуле НЕТ.

Схема сайта (разведано 16.10.2026):
- Вход: форма /auth/?login=yes (AUTH_FORM=Y, TYPE=AUTH, USER_LOGIN, USER_PASSWORD).
- Корзина (Bitrix, модуль sp:tools): BX.ajax.runAction("sp:tools.BasketController.add",
  {data:{id, quantity, delay:"N", properties:[]}}), .refresh (HTML корзины: .card-tiny[data-id]), .clear.
- Оформление: /order/ = bitrix sale.order.ajax (#bx-soa-order-form).
  Доставка 44 «Самовывоз» (магазины ['2','1','10','6','20','19','24']), BUYER_STORE=10 «Митинский радиорынок пав. 535»
  (по умолчанию стоит 2 — Савеловский, его надо сменить!). Оплата PAY_SYSTEM 14 «Наличными при получении».

Корзину чистим ТОЛЬКО если она была пуста до нас (там могли лежать товары, положенные человеком).

Запуск проверки:  venv/bin/python mm_order.py 67454      (id товара MM, можно несколько: 67454:1 53781:2)
Результат: debug/mm_order/<время>/ (скриншоты, HTML, JSON заказа) + вывод проверок.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

logger = logging.getLogger(__name__)

BASE = "https://master-mobile.ru"
MITINO_STORE_ID = "10"
PICKUP_DELIVERY_ID = "44"
CASH_PAY_SYSTEM_ID = "14"
DATA_DIR = Path("data")
DEBUG_ROOT = Path("debug/mm_order")


@dataclass
class MMCheckResult:
    ok: bool
    total: Optional[float] = None
    problems: list[str] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    debug_dir: str = ""
    cart_cleared: Optional[bool] = None
    status: str = "checked"          # checked | placed | unknown | rejected
    order_number: Optional[str] = None


class MasterMobileOrderer:
    def __init__(self, login: str, password: str) -> None:
        self._login = login
        self._password = password
        self._state = DATA_DIR / "state_mm.json"
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._ctx: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._dbg = DEBUG_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")

    # ---------------- жизненный цикл ---------------- #
    async def start(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._dbg.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        kw: dict[str, Any] = {"locale": "ru-RU", "viewport": {"width": 1400, "height": 1000}}
        if self._state.exists():
            kw["storage_state"] = str(self._state)
        self._ctx = await self._browser.new_context(**kw)
        self.page = await self._ctx.new_page()

    async def close(self) -> None:
        try:
            if self._ctx is not None:
                await self._ctx.storage_state(path=str(self._state))
        except Exception:  # noqa: BLE001
            pass
        for obj in (self._ctx, self._browser):
            try:
                if obj is not None:
                    await obj.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw is not None:
            await self._pw.stop()

    async def _snap(self, name: str) -> None:
        assert self.page is not None
        try:
            await self.page.screenshot(path=str(self._dbg / f"{name}.png"), full_page=True)
            (self._dbg / f"{name}.html").write_text(await self.page.content(), "utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("snap %s: %s", name, exc)

    # ---------------- вход ---------------- #
    async def is_logged_in(self) -> bool:
        assert self.page is not None
        await self.page.goto(f"{BASE}/personal/", wait_until="domcontentloaded")
        return "/auth/" not in self.page.url

    async def ensure_login(self) -> None:
        if await self.is_logged_in():
            return
        assert self.page is not None
        await self.page.goto(f"{BASE}/auth/", wait_until="domcontentloaded")
        form = self.page.locator('form[name="form_auth"]')
        await form.locator('input[name="USER_LOGIN"]').fill(self._login)
        await form.locator('input[name="USER_PASSWORD"]').fill(self._password)
        await form.locator('input[name="Login"]').click()
        try:
            await self.page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:  # noqa: BLE001
            pass
        if not await self.is_logged_in():
            await self._snap("login_failed")
            raise RuntimeError("Мастер Мобайл: вход не удался (см. debug/mm_order/*/login_failed.png)")
        await self._ctx.storage_state(path=str(self._state))  # type: ignore[union-attr]

    # ---------------- корзина ---------------- #
    async def _run(self, action: str, data: dict) -> dict:
        assert self.page is not None
        return await self.page.evaluate(
            """async ([a, d]) => { try { return await BX.ajax.runAction(a, {data: d}); }
                                 catch (e) { return {status: 'error', errors: e && e.errors ? e.errors : String(e)}; } }""",
            [action, data])

    async def basket(self) -> list[dict]:
        """[{basket_id, product_id, name, qty, price}] из HTML корзины."""
        res = await self._run("sp:tools.BasketController.refresh", {})
        html = res.get("data") if isinstance(res, dict) else ""
        (self._dbg / "basket_refresh.html").write_text(str(html), "utf-8")
        items: list[dict] = []
        for m in re.finditer(r'<div class="card-tiny[^"]*"\s+id="(\d+)"\s+data-id="(\d+)"', str(html)):
            chunk = str(html)[m.end(): m.end() + 6000]
            name = re.search(r'card-tiny__name[^>]*>\s*([^<]+)<', chunk)
            qty = re.search(r'value="(\d+)"[^>]*(?:quantity|mquantity)|(?:quantity|mquantity)[^>]*value="(\d+)"', chunk)
            price = re.search(r'([\d\s ]+)\s*₽', chunk)
            items.append({
                "basket_id": m.group(1), "product_id": m.group(2),
                "name": name.group(1).strip() if name else "",
                "qty": int((qty.group(1) or qty.group(2))) if qty else None,
                "price": int(re.sub(r"\D", "", price.group(1))) if price else None,
            })
        return items

    # ---------------- проверка заказа ---------------- #
    async def check_order(self, wanted: dict[str, int], submit: bool = False,
                          max_total: Optional[float] = None, request_id: str = "") -> MMCheckResult:
        """wanted: {product_id: qty}. Кладёт в корзину, проверяет /order/.
        submit=True — после всех проверок ставит согласие и жмёт «Подтвердить заказ» ОДИН раз."""
        r = MMCheckResult(ok=False, debug_dir=str(self._dbg))
        assert self.page is not None
        await self.ensure_login()
        await self.page.goto(f"{BASE}/", wait_until="domcontentloaded")

        before = await self.basket()
        if before:
            r.problems.append("в корзине Мастер Мобайла уже есть товары: "
                              + ", ".join(f"{b['name'] or b['product_id']}×{b['qty']}" for b in before)
                              + " — корзину не трогаю")
            return r

        added = False
        try:
            for pid, qty in wanted.items():
                res = await self._run("sp:tools.BasketController.add",
                                      {"id": int(pid), "quantity": int(qty), "delay": "N", "properties": []})
                added = True
                if res.get("status") != "success":
                    r.problems.append(f"не добавился {pid}: {res.get('errors')}")
            got = await self.basket()
            r.items = got
            by_pid = {g["product_id"]: g for g in got}
            for pid, qty in wanted.items():
                g = by_pid.get(str(pid))
                if not g:
                    r.problems.append(f"{pid}: нет в корзине")
                elif g["qty"] is not None and g["qty"] != int(qty):
                    r.problems.append(f"{g['name'] or pid}: в корзине {g['qty']} шт., нужно {qty}")
            if len(got) != len(wanted):
                r.problems.append(f"в корзине {len(got)} позиций, ожидалось {len(wanted)}")

            # --- оформление: самовывоз, Митино, наличные ---
            await self.page.goto(f"{BASE}/order/", wait_until="domcontentloaded")
            await self.page.wait_for_selector("#bx-soa-order-form", timeout=30000)
            await self._snap("order_initial")
            state = await self._set_pickup_mitino_cash()
            (self._dbg / "order_result.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
            await self._snap("order_ready")
            r.problems += state.get("problems", [])
            r.total = state.get("total")
            if r.total is not None and max_total is not None and float(r.total) > float(max_total):
                r.problems.append(f"итог {r.total} ₽ больше лимита {max_total:.0f} ₽")
            r.ok = not r.problems
            if r.ok and submit:
                await self._submit(r, request_id)
                if r.status in ("placed", "unknown"):
                    added = False   # корзину не трогаем: заказ ушёл или мог уйти
        except Exception as exc:  # noqa: BLE001
            r.problems.append(f"ошибка: {exc}")
            await self._snap("error")
        finally:
            if added:
                await self.page.goto(f"{BASE}/", wait_until="domcontentloaded")
                await self._run("sp:tools.BasketController.clear", {})
                r.cart_cleared = not await self.basket()
        return r

    async def _submit(self, r: MMCheckResult, request_id: str) -> None:
        """Согласие на обработку данных → «Подтвердить заказ» ровно один раз → номер заказа."""
        assert self.page is not None
        page = self.page
        # согласие (ORDER_PROP_25) — чекбокс скрыт, кликаем по его подписи
        # 19.10: на странице ДВА согласия — «на передачу личных данных» (ORDER_PROP_25) и
        # «с условиями обработки персональных данных и Политикой» у кнопки. Отмечаем все чекбоксы,
        # у которых в подписи «соглас» (галочки «Добавить следующие товары» не трогаем).
        ok = await page.evaluate("""() => {
            const boxes = [...document.querySelectorAll('input[type=checkbox]')].filter(cb => {
                const l = cb.closest('label') || document.querySelector('label[for="' + cb.id + '"]');
                const t = ((l && l.innerText) || (cb.parentElement && cb.parentElement.innerText) || '').toLowerCase();
                return cb.name === 'ORDER_PROP_25' || t.includes('соглас');
            });
            if (!boxes.length) return 'нет чекбокса согласия';
            for (const cb of boxes) {
                if (!cb.checked) {
                    const l = cb.closest('label') || document.querySelector('label[for="' + cb.id + '"]');
                    (l || cb).click();
                    if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles: true})); }
                }
            }
            const bad = boxes.filter(cb => !cb.checked).length;
            return bad ? 'не отметилось согласие (' + bad + ' из ' + boxes.length + ')' : 'ok';
        }""")
        if ok != "ok":
            r.problems.append(ok)
            r.status = "rejected"
            r.ok = False
            return
        await page.wait_for_timeout(800)
        btn = page.locator("button", has_text="Подтвердить заказ")
        if await btn.count() != 1 or not await btn.first.is_enabled():
            r.problems.append(f"кнопка «Подтвердить заказ»: найдено {await btn.count()}, активна — нет")
            r.status = "rejected"
            r.ok = False
            await self._snap("submit_blocked")
            return
        journal = DATA_DIR / "orders_mm.jsonl"
        with journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"request_id": request_id, "status": "submitting", "total": r.total,
                                "ts": datetime.now().isoformat()}, ensure_ascii=False) + "\n")
        url_before = page.url
        await btn.first.click()
        num = None
        try:
            await page.wait_for_url(re.compile(r"ORDER_ID=\d+"), timeout=30000)
            m = re.search(r"ORDER_ID=(\d+)", page.url)
            num = m.group(1) if m else None
        except Exception:  # noqa: BLE001
            await page.wait_for_timeout(3000)
            try:
                body = await page.inner_text("body")
                m = re.search(r"[Зз]аказ\D{0,10}(\d{4,})", body)
                num = m.group(1) if m and page.url != url_before else None
            except Exception:  # noqa: BLE001
                pass
        await self._snap("after_submit")
        if not num:
            try:
                errs = await page.evaluate("""() => [...document.querySelectorAll(
                    '.alert, .errortext, .bx-soa-section-error, [class*=error]')]
                    .map(e => e.innerText.trim()).filter(t => t && t.length < 300).slice(0, 5)""")
                if errs:
                    r.problems.append("сайт пишет: " + " | ".join(errs))
            except Exception:  # noqa: BLE001
                pass
        r.order_number = num
        r.status = "placed" if num else "unknown"
        if not num:
            r.problems.append("не увидел номер заказа после нажатия — заказ МОГ уйти, проверь в кабинете Мастер Мобайла")
        with journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"request_id": request_id, "status": r.status, "order": num, "total": r.total,
                                "ts": datetime.now().isoformat()}, ensure_ascii=False) + "\n")

    async def _set_pickup_mitino_cash(self) -> dict:
        """Выбирает доставку 44 + магазин 10 + оплату 14 через компонент sale.order.ajax и читает результат."""
        assert self.page is not None
        js = """
        async ([deliv, store, pay]) => {
          const C = BX.Sale && BX.Sale.OrderAjaxComponent;
          if (!C) return {problems: ['нет BX.Sale.OrderAjaxComponent на /order/']};
          const form = document.getElementById('bx-soa-order-form');
          const setVal = (name, val) => {
            let els = form.querySelectorAll('[name="' + name + '"]');
            if (!els.length) { const i = document.createElement('input'); i.type = 'hidden'; i.name = name; form.appendChild(i); els = [i]; }
            els.forEach(e => { if (e.type === 'radio' || e.type === 'checkbox') { e.checked = (e.value == val); } else { e.value = val; } });
          };
          setVal('DELIVERY_ID', deliv);
          setVal('BUYER_STORE', store);
          setVal('PAY_SYSTEM_ID', pay);
          await new Promise(res => {
            const done = () => { BX.removeCustomEvent('onAjaxSuccess', done); setTimeout(res, 800); };
            BX.addCustomEvent('onAjaxSuccess', done);
            C.sendRequest();
            setTimeout(res, 15000);
          });
          const R = C.result || {};
          const pick = (o) => Object.values(o || {}).find(x => x && x.CHECKED === 'Y');
          const d = pick(R.DELIVERY), p = pick(R.PAY_SYSTEM);
          const total = R.TOTAL ? (R.TOTAL.ORDER_TOTAL_PRICE ?? R.TOTAL.ORDER_PRICE) : null;
          const problems = [];
          if (!d || String(d.ID) !== deliv) problems.push('доставка не «Самовывоз» (' + (d && d.NAME) + ')');
          if (String(R.BUYER_STORE) !== store) problems.push('магазин не Митино (BUYER_STORE=' + R.BUYER_STORE + ')');
          if (!p || String(p.ID || p.PAY_SYSTEM_ID) !== pay) problems.push('оплата не «Наличными при получении» (' + (p && p.NAME) + ')');
          if (d && Number(d.PRICE || 0) !== 0) problems.push('доставка платная: ' + d.PRICE);
          const errs = R.ERROR ? JSON.stringify(R.ERROR) : '';
          if (errs && errs !== '{}' && errs !== '[]') problems.push('ошибки формы: ' + errs.slice(0, 300));
          const props = (R.ORDER_PROP && R.ORDER_PROP.properties || []).filter(x => x.REQUIRED === 'Y')
              .map(x => ({name: x.NAME, filled: !!(x.VALUE && x.VALUE.length && String(x.VALUE[0]).trim())}));
          props.filter(x => !x.filled).forEach(x => problems.push('не заполнено обязательное поле: ' + x.name));
          const rows = (R.GRID && R.GRID.ROWS) ? Object.values(R.GRID.ROWS).map(x => ({
              id: x.data && x.data.PRODUCT_ID, name: x.data && x.data.NAME, qty: x.data && x.data.QUANTITY,
              price: x.data && x.data.PRICE })) : [];
          const btn = [...document.querySelectorAll('a,button')].find(b => /Оформить заказ/i.test(b.textContent || ''));
          return {problems, total, delivery: d && {id: d.ID, name: d.NAME, price: d.PRICE},
                  store: R.BUYER_STORE, pay: p && {id: p.ID || p.PAY_SYSTEM_ID, name: p.NAME},
                  rows, required_props: props, submit_button_found: !!btn};
        }"""
        return await self.page.evaluate(js, [PICKUP_DELIVERY_ID, MITINO_STORE_ID, CASH_PAY_SYSTEM_ID])


async def _main(argv: list[str]) -> int:
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    login, password = os.getenv("MM_LOGIN", "").strip(), os.getenv("MM_PASSWORD", "").strip()
    if not login or not password:
        print("Нет MM_LOGIN / MM_PASSWORD в .env")
        return 2
    wanted: dict[str, int] = {}
    for a in argv or ["67454"]:
        pid, _, q = a.partition(":")
        wanted[pid] = int(q or 1)
    o = MasterMobileOrderer(login, password)
    await o.start()
    try:
        res = await o.check_order(wanted)
    finally:
        await o.close()
    print("\n=== Мастер Мобайл: проверка заказа (НЕ оформлен) ===")
    print("товары:", wanted)
    print("корзина:", res.items)
    print("итог:", res.total, "· корзина очищена:", res.cart_cleared)
    print("ПРОБЛЕМЫ:" if res.problems else "Проблем нет ✅", *res.problems, sep="\n  ")
    import shutil
    z = shutil.make_archive(res.debug_dir, "zip", root_dir=str(Path(res.debug_dir).parent),
                            base_dir=Path(res.debug_dir).name)
    print("Архив:", z)
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
