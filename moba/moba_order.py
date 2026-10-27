# moba_order.py
"""
Модуль оформления заказа на moba.ru через Playwright.
По умолчанию dry_run=True: всё, кроме клика «Оформить заказ».

Главный принцип: при любом сомнении — НЕ оформлять, вернуть rejected/unknown.
Корзину чистим ТОЛЬКО если этот вызов сам добавил товар (added_by_us).
Главный признак успеха — URL /order/success/?ids=<N>.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://moba.ru"
CITY_ID = 711
MITINO_STORE_ID = 6372
MITINO_STORE_LABEL = "Митино"
DELIVERY_SERVICE_VALUE = "3673"
PAYMENT_METHOD_VALUE = "26"

DATA_DIR = Path("data")
DEBUG_DIR = Path("debug")

_USER_ID_RE = re.compile(r'const\s+userId\s*=\s*"?(\d*)"?')
_STORE_ID_RE = re.compile(r'const\s+storeId\s*=\s*(\d+)\s*;?')
_ORDER_NUM_RES = (
    re.compile(r"№\s*(\d{4,})"),
    re.compile(r"[Зз]аказ\D{0,20}(\d{4,})"),
)

# Главный признак успеха — URL /order/success/?ids=<N>
_SUCCESS_URL_RE = re.compile(r"/order/success/\?ids=\d+")
_SUCCESS_URL_IDS_RE = re.compile(r"ids=(\d+)")

_ORDER_JSON_MARKER = "FB_MOBA.modules.order("

_SUCCESS_PHRASES = ("спасибо", "заказ принят", "заказ оформлен", "успешно")

_COMMENT_SELECTORS = (
    'textarea',  # на moba у поля комментария нет name; на странице одна textarea
    'textarea[name="comment"]',
    'textarea[name="COMMENT"]',
    'textarea[name="orderComment"]',
    'textarea#comment',
    'textarea[name="ORDER_COMMENT"]',
)


class MobaOrderError(Exception):
    pass


@dataclass
class OrderRequest:
    request_id: str
    product_id: int
    quantity: int
    expected_price: int
    title_hint: str = ""
    comment: str = ""


@dataclass
class OrderResult:
    status: str  # "placed" | "dry_run_ok" | "rejected" | "unknown"
    order_number: Optional[str]
    total: Optional[int]
    message: str
    screenshot_path: Optional[str] = None


class MobaOrderer:
    def __init__(
        self,
        login: str,
        password: str,
        account_name: str,
        dry_run: bool = True,
    ) -> None:
        self._login = login
        self._password = password
        self._account_name = account_name
        self._dry_run = dry_run

        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

        self._lock = asyncio.Lock()
        self._state_path = DATA_DIR / f"state_{account_name}.json"
        self._journal_path = DATA_DIR / f"orders_{account_name}.jsonl"
        self._cart_sample_saved = False

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)

        if self._state_path.exists():
            self._context = await self._browser.new_context(
                storage_state=str(self._state_path)
            )
        else:
            self._context = await self._browser.new_context()

        self._page = await self._context.new_page()

        if not await self._is_logged_in():
            await self._do_login()

        await self._ensure_logged_in()

    async def close(self) -> None:
        try:
            if self._context is not None:
                await self._context.storage_state(path=str(self._state_path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось сохранить storage_state: %s", exc)
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None

    # ------------------------------------------------------------------ #
    # Авторизация
    # ------------------------------------------------------------------ #
    async def _page_html(self, url: str) -> str:
        assert self._page is not None
        await self._page.goto(url, wait_until="domcontentloaded")
        return await self._page.content()

    async def _is_logged_in(self) -> bool:
        try:
            html = await self._page_html(f"{BASE_URL}/personal/")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось открыть /personal/: %s", exc)
            return False
        m = _USER_ID_RE.search(html)
        return bool(m and m.group(1))

    async def _do_login(self) -> None:
        assert self._page is not None
        await self._page.goto(f"{BASE_URL}/personal/", wait_until="domcontentloaded")

        forms = self._page.locator("form")
        count = await forms.count()
        target_form = None
        for i in range(count):
            f = forms.nth(i)
            if await f.locator('input[type="password"]').count() > 0:
                target_form = f
                break
        if target_form is None:
            raise MobaOrderError("Форма входа не найдена на /personal/")

        login_input = target_form.locator(
            'input[type="text"], input[type="email"]'
        ).first
        password_input = target_form.locator('input[type="password"]').first

        await login_input.fill(self._login)
        await password_input.fill(self._password)
        await password_input.press("Enter")
        try:
            await self._page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:  # noqa: BLE001
            pass

        if not await self._is_logged_in():
            raise MobaOrderError("Вход не удался: userId пустой после отправки формы")

        try:
            await self._context.storage_state(path=str(self._state_path))  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось сохранить state после входа: %s", exc)

    async def _ensure_logged_in(self) -> None:
        if await self._is_logged_in():
            return
        await self._do_login()

    # ------------------------------------------------------------------ #
    # API через fetch внутри страницы
    # ------------------------------------------------------------------ #
    async def _api(
        self, method: str, path: str, body: Optional[dict] = None
    ) -> dict:
        assert self._page is not None
        script = """
        async ([method, path, body]) => {
            const opts = {
                method: method,
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json'
                },
                credentials: 'include'
            };
            if (body !== null && body !== undefined) {
                opts.body = JSON.stringify(body);
            }
            try {
                const r = await fetch(path, opts);
                const text = await r.text();
                let data = null;
                try { data = JSON.parse(text); } catch (e) { data = null; }
                return { ok: true, status: r.status, data: data, text: text };
            } catch (e) {
                return { ok: false, status: 0, data: null, text: String(e) };
            }
        }
        """
        res = await self._page.evaluate(script, [method, path, body])
        if not isinstance(res, dict) or not res.get("ok"):
            raise MobaOrderError(
                f"fetch {method} {path} не выполнен: {res!r}"
            )
        return res

    async def _get_cart(self) -> dict:
        res = await self._api("GET", "/api/cart")
        data = res.get("data")
        if not isinstance(data, dict):
            raise MobaOrderError(
                f"/api/cart вернул не-JSON: {res.get('text', '')[:300]!r}"
            )
        return data

    async def _clear_cart(self) -> None:
        try:
            await self._api("POST", "/api/cart/clear", {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось очистить корзину: %s", exc)

    # ------------------------------------------------------------------ #
    # Журнал
    # ------------------------------------------------------------------ #
    def _journal_read_all(self) -> list[dict]:
        if not self._journal_path.exists():
            return []
        out: list[dict] = []
        try:
            with self._journal_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось прочитать журнал: %s", exc)
        return out

    def _journal_has_active(self, request_id: str) -> bool:
        for entry in self._journal_read_all():
            if entry.get("request_id") != request_id:
                continue
            if entry.get("status") in ("submitting", "placed", "unknown"):
                return True
        return False

    def _journal_write(self, entry: dict) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with self._journal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    # ------------------------------------------------------------------ #
    # Разбор страницы /order/
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_order_json(html: str) -> Optional[dict]:
        idx = html.find(_ORDER_JSON_MARKER)
        if idx < 0:
            return None
        pos = idx + len(_ORDER_JSON_MARKER)
        try:
            obj, _ = json.JSONDecoder().raw_decode(html, pos)
        except Exception:  # noqa: BLE001
            return None
        return obj if isinstance(obj, dict) else None

    @staticmethod
    def _find_field(fields: list[dict], name: str) -> Optional[dict]:
        for f in fields:
            if f.get("name") == name:
                return f
        return None

    @staticmethod
    def _option_label(field: dict, value: str) -> Optional[str]:
        for opt in field.get("options") or []:
            if str(opt.get("value")) == str(value):
                return str(opt.get("label") or "")
        return None

    # ------------------------------------------------------------------ #
    # Основной метод
    # ------------------------------------------------------------------ #
    async def place_order(self, req: OrderRequest) -> OrderResult:
        async with self._lock:
            return await self._place_order_locked(req)

    async def _place_order_locked(self, req: OrderRequest) -> OrderResult:
        # --- 1. Защита от дублей ---
        if self._journal_has_active(req.request_id):
            return OrderResult(
                status="rejected",
                order_number=None,
                total=None,
                message=(
                    f"заказ по этому request_id уже оформлялся: {req.request_id}"
                ),
            )

        # Корзину чистим ТОЛЬКО если этот вызов сам добавил товар.
        added_by_us = False
        cleaned_up = False

        async def _cleanup_if_added() -> None:
            nonlocal cleaned_up
            if added_by_us and not cleaned_up:
                await self._clear_cart()
                cleaned_up = True

        try:
            # --- 2. Проверка входа ---
            await self._ensure_logged_in()

            # --- 3. Корзина должна быть пуста ---
            cart = await self._get_cart()
            cart_products = cart.get("cartProducts") or []
            if cart_products:
                titles = ", ".join(
                    str(p.get("title") or p.get("productId") or "?")
                    for p in cart_products
                )
                # НЕ чистим: это могли быть товары, положенные человеком вручную.
                return OrderResult(
                    status="rejected",
                    order_number=None,
                    total=None,
                    message=(
                        f"в корзине moba есть посторонние товары: {titles}, "
                        f"оформление остановлено"
                    ),
                )

            # --- 4. Магазин Митино ---
            await self._api(
                "POST",
                "/api/choose-store",
                {"store_id": MITINO_STORE_ID, "city_id": CITY_ID},
            )
            home_html = await self._page_html(f"{BASE_URL}/")
            store_m = _STORE_ID_RE.search(home_html)
            if not store_m or int(store_m.group(1)) != MITINO_STORE_ID:
                return OrderResult(
                    status="rejected",
                    order_number=None,
                    total=None,
                    message=(
                        f"не удалось выбрать магазин Митино "
                        f"(storeId={store_m.group(1) if store_m else '?'})"
                    ),
                )

            # --- 5. Добавить товар ---
            add_res = await self._api(
                "POST", "/api/cart/items", {"productId": int(req.product_id)}
            )
            if add_res.get("status") != 200 or not isinstance(add_res.get("data"), dict):
                return OrderResult(
                    status="rejected",
                    order_number=None,
                    total=None,
                    message=(
                        f"добавление товара не удалось: "
                        f"{add_res.get('text', '')[:200]}"
                    ),
                )
            # Только теперь наш товар в корзине — ставим флаг.
            added_by_us = True

            if req.quantity > 1:
                added = add_res["data"]
                add_products = added.get("cartProducts") or []
                if len(add_products) != 1:
                    await _cleanup_if_added()
                    return OrderResult(
                        status="rejected",
                        order_number=None,
                        total=None,
                        message="после добавления в корзине не одна позиция",
                    )
                basket_item_id = add_products[0].get("basketItemId")
                q_res = await self._api(
                    "POST",
                    "/api/cart/items/quantity",
                    {
                        "productId": int(req.product_id),
                        "basketItemId": int(basket_item_id),
                        "quantity": int(req.quantity),
                    },
                )
                if q_res.get("status") != 200:
                    await _cleanup_if_added()
                    return OrderResult(
                        status="rejected",
                        order_number=None,
                        total=None,
                        message=(
                            f"не удалось установить количество: "
                            f"{q_res.get('text', '')[:200]}"
                        ),
                    )

            # --- 6. Проверка корзины ---
            cart = await self._get_cart()
            reason = self._check_cart(cart, req)
            if reason:
                await _cleanup_if_added()
                return OrderResult(
                    status="rejected",
                    order_number=None,
                    total=None,
                    message=reason,
                )

            # --- 7. Страница /order/ ---
            assert self._page is not None
            await self._page.goto(
                f"{BASE_URL}/order/", wait_until="domcontentloaded"
            )
            try:
                await self._page.wait_for_selector(
                    'input[name="delivery-service"]', timeout=20000
                )
                await self._page.get_by_role(
                    "button", name="Оформить заказ"
                ).wait_for(state="visible", timeout=20000)
            except Exception as exc:  # noqa: BLE001
                await _cleanup_if_added()
                return OrderResult(
                    status="rejected",
                    order_number=None,
                    total=None,
                    message=f"не дождались отрисовки /order/: {exc}",
                )

            html = await self._page.content()
            order_json = self._extract_order_json(html)
            if order_json is None:
                await _cleanup_if_added()
                return OrderResult(
                    status="rejected",
                    order_number=None,
                    total=None,
                    message="не удалось извлечь JSON со страницы /order/",
                )

            reason = self._check_order_json(order_json, req)
            if reason:
                await _cleanup_if_added()
                return OrderResult(
                    status="rejected",
                    order_number=None,
                    total=None,
                    message=reason,
                )

            dom_reason = await self._check_dom_state()
            if dom_reason:
                await _cleanup_if_added()
                return OrderResult(
                    status="rejected",
                    order_number=None,
                    total=None,
                    message=dom_reason,
                )

            # --- 8. dry_run ---
            order_block = order_json.get("order") or {}
            summary = order_block.get("summary") or {}
            total = int(summary.get("total") or 0)

            if self._dry_run:
                # Если в dry_run передан comment — заполним поле, но НЕ отправляем.
                if req.comment:
                    await self._fill_comment(req.comment)

                screenshot_path = None
                try:
                    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                    screenshot_path = str(
                        DEBUG_DIR / f"dryrun_{req.request_id}.png"
                    )
                    await self._page.screenshot(
                        path=screenshot_path, full_page=True
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Не удалось сохранить скриншот dry_run: %s", exc)
                    screenshot_path = None

                await _cleanup_if_added()
                return OrderResult(
                    status="dry_run_ok",
                    order_number=None,
                    total=total,
                    message=(
                        f"dry_run: все проверки пройдены, кнопка не нажата, "
                        f"итог {total} руб., корзина очищена"
                    ),
                    screenshot_path=screenshot_path,
                )

            # --- 9. Боевой режим ---
            return await self._submit(req, order_json, added_by_us)

        except MobaOrderError as exc:
            await _cleanup_if_added()
            return OrderResult(
                status="rejected",
                order_number=None,
                total=None,
                message=f"остановлено: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Неожиданная ошибка в place_order")
            await _cleanup_if_added()
            return OrderResult(
                status="rejected",
                order_number=None,
                total=None,
                message=f"неожиданная ошибка: {exc}",
            )

    # ------------------------------------------------------------------ #
    # Поле комментария
    # ------------------------------------------------------------------ #
    async def _fill_comment(self, text: str) -> None:
        assert self._page is not None
        for sel in _COMMENT_SELECTORS:
            try:
                loc = self._page.locator(sel)
                if await loc.count() == 1:
                    await loc.first.fill(text)
                    logger.info("Поле комментария заполнено (%s)", sel)
                    return
            except Exception:  # noqa: BLE001
                continue
        logger.warning(
            "Поле комментария не найдено на /order/, comment не записан"
        )

    # ------------------------------------------------------------------ #
    # Проверки
    # ------------------------------------------------------------------ #
    def _save_cart_sample(self, cart: dict) -> None:
        if self._cart_sample_saved:
            return
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            with (DEBUG_DIR / "cart_sample.json").open("w", encoding="utf-8") as f:
                json.dump(cart, f, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось сохранить cart_sample.json: %s", exc)
        self._cart_sample_saved = True

    def _check_cart(self, cart: dict, req: OrderRequest) -> Optional[str]:
        self._save_cart_sample(cart)

        store = cart.get("store") or {}
        if int(store.get("id") or -1) != MITINO_STORE_ID:
            return f"магазин в корзине не Митино (id={store.get('id')})"

        items = cart.get("cartProducts") or []
        if len(items) != 1:
            return f"в корзине {len(items)} позиций, ожидалась 1"

        it = items[0]
        if int(it.get("productId") or -1) != int(req.product_id):
            return (
                f"в корзине другой productId: "
                f"{it.get('productId')} вместо {req.product_id}"
            )

        qty_raw = it.get("quantity")
        price_raw = it.get("price")
        if (
            not isinstance(qty_raw, (int, float))
            or isinstance(qty_raw, bool)
            or not isinstance(price_raw, (int, float))
            or isinstance(price_raw, bool)
        ):
            return "неизвестная структура корзины: нет поля price/quantity"
        qty = int(qty_raw)
        price = int(price_raw)

        if qty != int(req.quantity):
            return f"количество в корзине {qty}, ожидалось {req.quantity}"
        if str(it.get("availabilityStatus")) != "in_stock":
            return f"товар не in_stock: {it.get('availabilityStatus')!r}"
        if price > int(req.expected_price):
            return f"цена выросла с {req.expected_price} до {price}"
        return None

    @staticmethod
    def _check_order_json(order_json: dict, req: OrderRequest) -> Optional[str]:
        order_block = order_json.get("order")
        if not isinstance(order_block, dict):
            return "в JSON /order/ нет блока order"
        if order_block.get("canOrder") is not True:
            return "canOrder != true"
        errors = order_block.get("errors") or []
        if errors:
            return f"ошибки в order.errors: {errors}"

        groups = order_block.get("groups") or []
        if len(groups) != 1:
            return f"в заказе {len(groups)} групп, ожидалась 1"
        g = groups[0]
        if str(g.get("store")) != MITINO_STORE_LABEL:
            return f"группа не Митино: {g.get('store')!r}"
        items = g.get("items") or []
        if len(items) != 1:
            return f"в группе {len(items)} товаров, ожидался 1"
        it = items[0]
        if int(it.get("productId") or -1) != int(req.product_id):
            return (
                f"в заказе другой productId: "
                f"{it.get('productId')} вместо {req.product_id}"
            )
        if int(it.get("quantity") or 0) != int(req.quantity):
            return (
                f"количество в заказе {it.get('quantity')}, "
                f"ожидалось {req.quantity}"
            )
        if it.get("available") is not True:
            return "товар в заказе недоступен (available != true)"

        schema = order_block.get("schema") or []
        all_fields: list[dict] = []
        for section in schema:
            all_fields.extend(section.get("fields") or [])

        ds = MobaOrderer._find_field(all_fields, "deliveryService")
        if ds is None:
            return "в schema нет поля deliveryService"
        if str(ds.get("defaultValue")) != DELIVERY_SERVICE_VALUE:
            return (
                f"deliveryService.defaultValue={ds.get('defaultValue')!r}, "
                f"ожидалось {DELIVERY_SERVICE_VALUE}"
            )
        ds_label = MobaOrderer._option_label(ds, DELIVERY_SERVICE_VALUE) or ""
        if "Самовывоз" not in ds_label or "Митин" not in ds_label:
            return f"label службы доставки не тот: {ds_label!r}"

        pm = MobaOrderer._find_field(all_fields, "paymentMethod")
        if pm is None:
            return "в schema нет поля paymentMethod"
        if str(pm.get("defaultValue")) != PAYMENT_METHOD_VALUE:
            return (
                f"paymentMethod.defaultValue={pm.get('defaultValue')!r}, "
                f"ожидалось {PAYMENT_METHOD_VALUE}"
            )
        pm_label = MobaOrderer._option_label(pm, PAYMENT_METHOD_VALUE) or ""
        if "Наличными" not in pm_label:
            return f"label платежа не «Наличными»: {pm_label!r}"

        summary = order_block.get("summary") or {}
        if int(summary.get("deliveryPrice") or 0) != 0:
            return (
                f"deliveryPrice={summary.get('deliveryPrice')}, ожидался 0"
            )
        total = int(summary.get("total") or 0)
        expected_total = int(req.expected_price) * int(req.quantity)
        price_per_unit = int(it.get("price") or 0)
        if price_per_unit * int(req.quantity) != total:
            return (
                f"итог {total} != цена*кол-во "
                f"({price_per_unit}*{req.quantity})"
            )
        if total > expected_total:
            return (
                f"итог {total} превышает ожидание "
                f"{expected_total} (цена {price_per_unit})"
            )
        return None

    async def _check_dom_state(self) -> Optional[str]:
        assert self._page is not None
        ds = self._page.locator(
            f'input[name="delivery-service"][value="{DELIVERY_SERVICE_VALUE}"]'
        )
        if await ds.count() != 1:
            return (
                f"радио delivery-service={DELIVERY_SERVICE_VALUE} "
                f"найдено {await ds.count()} раз"
            )
        if not await ds.is_checked():
            return "радио delivery-service не отмечено"

        pm = self._page.locator(
            f'input[name="paymentMethod"][value="{PAYMENT_METHOD_VALUE}"]'
        )
        if await pm.count() != 1:
            return (
                f"радио paymentMethod={PAYMENT_METHOD_VALUE} "
                f"найдено {await pm.count()} раз"
            )
        if not await pm.is_checked():
            return "радио paymentMethod не отмечено"

        policy = self._page.locator('input[name="policy"]')
        if await policy.count() < 1:
            return "чекбокс policy не найден"
        if not await policy.first.is_checked():
            return "чекбокс policy не отмечен"
        return None

    # ------------------------------------------------------------------ #
    # Запасные признаки успеха (если URL не получен)
    # ------------------------------------------------------------------ #
    async def _wait_until_outcome(
        self, url_before: str, timeout: float = 5.0
    ) -> None:
        assert self._page is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self._page.url != url_before:
                    return
            except Exception:  # noqa: BLE001
                pass

            text = ""
            try:
                text = await self._page.inner_text("body")
            except Exception:  # noqa: BLE001
                text = ""
            low = text.lower()
            for phrase in _SUCCESS_PHRASES:
                if phrase in low:
                    return

            try:
                dialogs = self._page.get_by_role("dialog")
                n = await dialogs.count()
                for i in range(n):
                    d = dialogs.nth(i)
                    try:
                        if await d.is_visible():
                            dt = await d.inner_text()
                            if "заказ" in dt.lower():
                                return
                    except Exception:  # noqa: BLE001
                        continue
            except Exception:  # noqa: BLE001
                pass

            await asyncio.sleep(0.5)

    async def _find_new_order_number(
        self, nums_before: set[str], post_bodies: list[str]
    ) -> Optional[str]:
        assert self._page is not None
        try:
            text = await self._page.inner_text("body")
        except Exception:  # noqa: BLE001
            text = ""
        for rx in _ORDER_NUM_RES:
            for m in rx.finditer(text):
                num = m.group(1)
                if num not in nums_before:
                    return num
        for body in post_bodies:
            for rx in _ORDER_NUM_RES:
                for m in rx.finditer(body):
                    num = m.group(1)
                    if num not in nums_before:
                        return num
        return None

    # ------------------------------------------------------------------ #
    # Боевое оформление
    # ------------------------------------------------------------------ #
    async def _submit(
        self,
        req: OrderRequest,
        order_json: dict,
        added_by_us: bool,
    ) -> OrderResult:
        assert self._page is not None

        async def _cleanup() -> None:
            if added_by_us:
                await self._clear_cart()

        # --- Проверка кнопки ДО записи submitting ---
        buttons = self._page.get_by_role("button", name="Оформить заказ")
        cnt = await buttons.count()
        if cnt != 1:
            await _cleanup()
            return OrderResult(
                status="rejected",
                order_number=None,
                total=None,
                message=(
                    f"кнопок «Оформить заказ» найдено {cnt}, "
                    f"ожидалась 1 — клик отменён"
                ),
            )
        btn = buttons.first
        if not await btn.is_visible():
            await _cleanup()
            return OrderResult(
                status="rejected",
                order_number=None,
                total=None,
                message="кнопка «Оформить заказ» невидима — клик отменён",
            )

        # --- Комментарий (если есть) ---
        if req.comment:
            await self._fill_comment(req.comment)

        # --- Снимок состояния ДО клика ---
        url_before = self._page.url
        try:
            text_before = await self._page.inner_text("body")
        except Exception:  # noqa: BLE001
            text_before = ""
        nums_before: set[str] = set()
        for rx in _ORDER_NUM_RES:
            for m in rx.finditer(text_before):
                nums_before.add(m.group(1))

        # --- Запись submitting непосредственно перед кликом ---
        self._journal_write(
            {
                "request_id": req.request_id,
                "status": "submitting",
                "time": time.time(),
                "product_id": req.product_id,
                "quantity": req.quantity,
                "expected_price": req.expected_price,
                "title_hint": req.title_hint,
            }
        )

        # --- Логгер сети ---
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        net_path = DEBUG_DIR / f"submit_{req.request_id}.jsonl"
        net_file = net_path.open("w", encoding="utf-8")
        net_file_lock = asyncio.Lock()
        write_tasks: list[asyncio.Task] = []
        post_bodies: list[str] = []

        async def _handle_response(response) -> None:
            body_text = ""
            is_post = False
            try:
                request = response.request
                try:
                    raw = await response.body()
                    body_text = raw.decode("utf-8", errors="replace")[:10000]
                except Exception:  # noqa: BLE001
                    body_text = ""
                entry = {
                    "method": request.method,
                    "url": request.url,
                    "status": response.status,
                    "request_body": (request.post_data or "")[:10000],
                    "response_body": body_text,
                }
                is_post = (request.method or "").upper() == "POST"
            except Exception as exc:  # noqa: BLE001
                entry = {"error": str(exc)}
            async with net_file_lock:
                try:
                    net_file.write(
                        json.dumps(entry, ensure_ascii=False) + "\n"
                    )
                    net_file.flush()
                except Exception:  # noqa: BLE001
                    pass
                if is_post and body_text:
                    post_bodies.append(body_text)

        def _on_response(response) -> None:
            task = asyncio.create_task(_handle_response(response))
            write_tasks.append(task)

        self._page.on("response", _on_response)

        outcome_written = False
        order_number: Optional[str] = None
        screenshot_path: Optional[str] = None
        html_path: Optional[str] = None
        url_success = False

        try:
            # --- Клик РОВНО ОДИН РАЗ ---
            await btn.click()

            # --- Главное ожидание: URL успеха ---
            try:
                await self._page.wait_for_url(
                    _SUCCESS_URL_RE, timeout=30000
                )
                url_success = True
                m = _SUCCESS_URL_IDS_RE.search(self._page.url)
                if m:
                    order_number = m.group(1)
            except Exception:  # noqa: BLE001
                logger.info(
                    "URL /order/success/ не получен, пробуем запасные признаки"
                )
                try:
                    await self._wait_until_outcome(url_before, timeout=5.0)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(2.0)
                if write_tasks:
                    try:
                        await asyncio.wait_for(asyncio.gather(
                            *write_tasks, return_exceptions=True
                        ), timeout=10)
                    except Exception:  # noqa: BLE001
                        pass
                order_number = await self._find_new_order_number(
                    nums_before, post_bodies
                )

            # --- Дать логгеру шанс дописать ---
            if write_tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*write_tasks, return_exceptions=True), timeout=10)
                except Exception:  # noqa: BLE001
                    pass

            # --- Скриншот и HTML ---
            try:
                screenshot_path = str(
                    DEBUG_DIR / f"submit_{req.request_id}.png"
                )
                await self._page.screenshot(
                    path=screenshot_path, full_page=True
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Скриншот submit не сохранён: %s", exc)
                screenshot_path = None

            try:
                html_path = str(
                    DEBUG_DIR / f"submit_{req.request_id}.html"
                )
                content = await self._page.content()
                Path(html_path).write_text(content, encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                logger.warning("HTML submit не сохранён: %s", exc)
                html_path = None

            # --- Главный путь: URL успеха + номер => placed ---
            if url_success and order_number:
                self._journal_write(
                    {
                        "request_id": req.request_id,
                        "status": "placed",
                        "time": time.time(),
                        "order_number": order_number,
                        "via": "url",
                    }
                )
                outcome_written = True
                summary = (order_json.get("order") or {}).get("summary") or {}
                return OrderResult(
                    status="placed",
                    order_number=order_number,
                    total=int(summary.get("total") or 0),
                    message=f"заказ оформлен, номер {order_number}",
                    screenshot_path=screenshot_path,
                )

            # --- Запасной путь: номер + пустая корзина ---
            cart_empty = False
            try:
                cart = await self._get_cart()
                cart_empty = not (cart.get("cartProducts") or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Не удалось проверить корзину после клика: %s", exc
                )

            if order_number and cart_empty:
                self._journal_write(
                    {
                        "request_id": req.request_id,
                        "status": "placed",
                        "time": time.time(),
                        "order_number": order_number,
                        "via": "fallback",
                    }
                )
                outcome_written = True
                summary = (order_json.get("order") or {}).get("summary") or {}
                return OrderResult(
                    status="placed",
                    order_number=order_number,
                    total=int(summary.get("total") or 0),
                    message=f"заказ оформлен, номер {order_number}",
                    screenshot_path=screenshot_path,
                )

            self._journal_write(
                {
                    "request_id": req.request_id,
                    "status": "unknown",
                    "time": time.time(),
                    "order_number": order_number,
                    "url_success": url_success,
                    "cart_empty": cart_empty,
                }
            )
            outcome_written = True
            return OrderResult(
                status="unknown",
                order_number=order_number,
                total=None,
                message=(
                    "Не уверен, оформился ли заказ — проверь личный кабинет "
                    "moba вручную. Повтор НЕ выполняется."
                ),
                screenshot_path=screenshot_path,
            )
        except Exception as exc:  # noqa: BLE001
            # Кнопка уже нажата: исход неизвестен. НЕ rejected, НЕ чистим корзину, НЕ повторяем.
            logger.exception("Ошибка после клика «Оформить заказ»")
            return OrderResult(
                status="unknown",
                order_number=order_number,
                total=None,
                message=(
                    f"Ошибка после нажатия «Оформить заказ» ({exc}). Заказ МОГ оформиться — "
                    "проверь личный кабинет moba вручную. Повтор НЕ выполняется."
                ),
                screenshot_path=screenshot_path,
            )
        finally:
            # Если исход не записан (исключение/прерывание) — пишем unknown.
            if not outcome_written:
                try:
                    self._journal_write(
                        {
                            "request_id": req.request_id,
                            "status": "unknown",
                            "time": time.time(),
                            "order_number": order_number,
                            "url_success": url_success,
                            "interrupted": True,
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass
            try:
                self._page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass
            if write_tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*write_tasks, return_exceptions=True), timeout=10)
                except Exception:  # noqa: BLE001
                    pass
            try:
                net_file.close()
            except Exception:  # noqa: BLE001
                pass
