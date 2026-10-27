# wb_orders.py
"""
Модуль чтения новых FBS-заказов Wildberries.
Только чтение — ничего в WB не меняет.

Baseline per shop: первый УСПЕШНЫЙ опрос магазина пишет его заказы
со статусом 'baseline' и создаёт запись в shop_state.
run_forever опирается на pending() (status='new'), чтобы не терять
заказы при падениях on_new.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aiosqlite
import httpx

logger = logging.getLogger(__name__)

NEW_ORDERS_URL = "https://marketplace-api.wildberries.ru/api/v3/orders/new"
REQUEST_TIMEOUT = 20.0
RETRY_DELAYS = [2, 5, 10]
DEFAULT_RATE_LIMIT_SEC = 60.0
MAX_429_RETRIES = 3
DEBUG_DIR = Path("debug")

SHOPS = [
    {"key": "gor",   "title": "Гор",       "env": "WB_GOR",   "owner": "gor"},
    {"key": "shop2", "title": "Анри", "env": "WB_SHOP2", "owner": "anri"},
    {"key": "shop3", "title": "Лёша", "env": "WB_SHOP3", "owner": "anri"},
    {"key": "shop4", "title": "Борода", "env": "WB_SHOP4", "owner": "anri"},
]

_SHOPS_BY_KEY = {s["key"]: s for s in SHOPS}


@dataclass
class WbOrder:
    shop_key: str
    shop_title: str
    owner: str
    wb_order_id: int
    nm_id: Optional[int]
    article: Optional[str]
    chrt_id: Optional[int]
    sku: Optional[str]
    price_rub: Optional[float]
    created_at: Optional[str]


class WbPoller:
    def __init__(
        self,
        db_path: str = "data/autozakaz.db",
        rate_limit_sec: float = DEFAULT_RATE_LIMIT_SEC,
    ) -> None:
        self._db_path = db_path
        self._rate_limit_sec = float(rate_limit_sec)
        self._last_poll: dict[str, float] = {}
        self._client: Optional[httpx.AsyncClient] = None
        self._last_api_counts: dict[str, int] = {}
        self._last_insert_counts: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # Инфраструктура
    # ------------------------------------------------------------------ #
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def init(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS wb_orders (
                    shop_key      TEXT NOT NULL,
                    wb_order_id   INTEGER NOT NULL,
                    nm_id         INTEGER,
                    article       TEXT,
                    chrt_id       INTEGER,
                    sku           TEXT,
                    price_kop     INTEGER,
                    created_at    TEXT,
                    raw_json      TEXT,
                    first_seen_at TEXT NOT NULL,
                    status        TEXT NOT NULL,
                    PRIMARY KEY (shop_key, wb_order_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS shop_state (
                    shop_key      TEXT PRIMARY KEY,
                    baselined_at  TEXT NOT NULL
                )
                """
            )
            await db.commit()

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    async def _fetch_new_orders(
        self, shop_key: str, shop_title: str, token: str, token_env: str
    ) -> dict:
        client = await self._get_client()
        headers = {"Authorization": token}

        retries_5xx = 0
        retries_429 = 0

        while True:
            try:
                resp = await client.get(NEW_ORDERS_URL, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if retries_5xx >= len(RETRY_DELAYS):
                    raise RuntimeError(
                        f"Сетевая ошибка для {shop_title}: {exc}"
                    ) from exc
                wait = RETRY_DELAYS[retries_5xx]
                retries_5xx += 1
                logger.warning(
                    "Сеть %s на %s, повтор через %s c", exc, shop_title, wait
                )
                await asyncio.sleep(wait)
                continue

            if resp.status_code in (401, 403):
                # Токен не пишем, только имя переменной.
                logger.error(
                    "токен %s недействителен (HTTP %d) для магазина %s",
                    token_env, resp.status_code, shop_title,
                )
                raise RuntimeError(
                    f"токен {token_env} недействителен (HTTP {resp.status_code})"
                )

            if resp.status_code == 429:
                if retries_429 >= MAX_429_RETRIES:
                    raise RuntimeError(f"429 не отпускает {shop_title}")
                ra = resp.headers.get("Retry-After")
                wait = 60.0
                if ra:
                    try:
                        wait = float(ra)
                    except ValueError:
                        pass
                retries_429 += 1
                logger.warning(
                    "429 на %s, ждём %.1f c (попытка %d/%d)",
                    shop_title, wait, retries_429, MAX_429_RETRIES,
                )
                await asyncio.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                if retries_5xx >= len(RETRY_DELAYS):
                    raise RuntimeError(
                        f"5xx ({resp.status_code}) для {shop_title}"
                    )
                wait = RETRY_DELAYS[retries_5xx]
                retries_5xx += 1
                logger.warning(
                    "5xx (%d) на %s, повтор через %s c",
                    resp.status_code, shop_title, wait,
                )
                await asyncio.sleep(wait)
                continue

            if resp.status_code != 200:
                raise RuntimeError(
                    f"Неожиданный код {resp.status_code} для {shop_title}"
                )

            try:
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"Ответ {shop_title} не JSON: {exc}"
                ) from exc

    # ------------------------------------------------------------------ #
    # Разбор и запись
    # ------------------------------------------------------------------ #
    def _parse_order(self, shop: dict, raw: dict) -> Optional[WbOrder]:
        try:
            wb_id = int(raw["id"])
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "Пропущен заказ без id в %s: %r", shop["key"], raw
            )
            return None

        skus = raw.get("skus") or []
        sku = None
        if isinstance(skus, list) and skus:
            sku = str(skus[0])

        # Цена в рублях: convertedPrice (код 643). Поле price — в валюте покупателя
        # (933 = белорусский рубль и т.п.), его брать нельзя.
        price_kop = raw.get("convertedPrice")
        if raw.get("convertedCurrencyCode") not in (None, 643) or price_kop is None:
            price_kop = raw.get("price") if raw.get("currencyCode") in (None, 643) else None
        price_rub: Optional[float] = None
        if isinstance(price_kop, (int, float)) and not isinstance(price_kop, bool):
            price_rub = float(price_kop) / 100.0

        nm_id = raw.get("nmId")
        if nm_id is not None:
            try:
                nm_id = int(nm_id)
            except (TypeError, ValueError):
                nm_id = None

        chrt_id = raw.get("chrtId")
        if chrt_id is not None:
            try:
                chrt_id = int(chrt_id)
            except (TypeError, ValueError):
                chrt_id = None

        article = raw.get("article")
        if article is not None:
            article = str(article)

        created_at = raw.get("createdAt")
        if created_at is not None:
            created_at = str(created_at)

        return WbOrder(
            shop_key=shop["key"],
            shop_title=shop["title"],
            owner=shop["owner"],
            wb_order_id=wb_id,
            nm_id=nm_id,
            article=article,
            chrt_id=chrt_id,
            sku=sku,
            price_rub=price_rub,
            created_at=created_at,
        )

    async def _insert_order(
        self,
        db: aiosqlite.Connection,
        order: WbOrder,
        raw: dict,
        first_seen_at: str,
        status: str,
    ) -> bool:
        # Цена в рублях: convertedPrice (код 643). Поле price — в валюте покупателя
        # (933 = белорусский рубль и т.п.), его брать нельзя.
        price_kop = raw.get("convertedPrice")
        if raw.get("convertedCurrencyCode") not in (None, 643) or price_kop is None:
            price_kop = raw.get("price") if raw.get("currencyCode") in (None, 643) else None
        if not isinstance(price_kop, (int, float)) or isinstance(price_kop, bool):
            price_kop = None

        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO wb_orders
              (shop_key, wb_order_id, nm_id, article, chrt_id, sku,
               price_kop, created_at, raw_json, first_seen_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.shop_key,
                order.wb_order_id,
                order.nm_id,
                order.article,
                order.chrt_id,
                order.sku,
                price_kop,
                order.created_at,
                json.dumps(raw, ensure_ascii=False),
                first_seen_at,
                status,
            ),
        )
        return cursor.rowcount > 0

    def _save_sample(self, shop_key: str, data: dict) -> None:
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = DEBUG_DIR / f"wb_new_sample_{shop_key}.json"
            with path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("Сохранён образец ответа: %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось сохранить образец %s: %s", shop_key, exc)

    # ------------------------------------------------------------------ #
    # Методы состояния
    # ------------------------------------------------------------------ #
    async def set_status(
        self, shop_key: str, wb_order_id: int, status: str
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE wb_orders SET status = ? "
                "WHERE shop_key = ? AND wb_order_id = ?",
                (status, shop_key, int(wb_order_id)),
            )
            await db.commit()

    async def pending(self) -> list[WbOrder]:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                SELECT shop_key, wb_order_id, nm_id, article, chrt_id,
                       sku, price_kop, created_at
                FROM wb_orders
                WHERE status = 'new'
                ORDER BY first_seen_at ASC, wb_order_id ASC
                """
            )
            rows = await cursor.fetchall()

        out: list[WbOrder] = []
        for (
            shop_key, wb_order_id, nm_id, article, chrt_id,
            sku, price_kop, created_at,
        ) in rows:
            shop = _SHOPS_BY_KEY.get(shop_key)
            if shop is None:
                logger.warning(
                    "В БД заказ неизвестного shop_key=%s, пропуск", shop_key
                )
                continue
            price_rub: Optional[float] = None
            if isinstance(price_kop, (int, float)) and not isinstance(price_kop, bool):
                price_rub = float(price_kop) / 100.0
            out.append(
                WbOrder(
                    shop_key=shop_key,
                    shop_title=shop["title"],
                    owner=shop["owner"],
                    wb_order_id=int(wb_order_id),
                    nm_id=int(nm_id) if nm_id is not None else None,
                    article=article,
                    chrt_id=int(chrt_id) if chrt_id is not None else None,
                    sku=sku,
                    price_rub=price_rub,
                    created_at=created_at,
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # Опрос
    # ------------------------------------------------------------------ #
    async def _shop_has_baseline(self, db: aiosqlite.Connection, shop_key: str) -> bool:
        cursor = await db.execute(
            "SELECT 1 FROM shop_state WHERE shop_key = ? LIMIT 1",
            (shop_key,),
        )
        return (await cursor.fetchone()) is not None

    async def _mark_baselined(
        self, db: aiosqlite.Connection, shop_key: str, ts: str
    ) -> None:
        await db.execute(
            "INSERT OR IGNORE INTO shop_state (shop_key, baselined_at) "
            "VALUES (?, ?)",
            (shop_key, ts),
        )

    async def poll_once(self) -> list[WbOrder]:
        await self.init()
        self._last_api_counts = {}
        self._last_insert_counts = {}

        new_orders: list[WbOrder] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            for shop in SHOPS:
                token = os.getenv(shop["env"], "").strip()
                if not token:
                    logger.warning(
                        "Токен %s не задан — магазин %s пропущен",
                        shop["env"], shop["title"],
                    )
                    continue

                last = self._last_poll.get(shop["key"], 0.0)
                elapsed = time.monotonic() - last
                if elapsed < self._rate_limit_sec:
                    wait = self._rate_limit_sec - elapsed
                    logger.info(
                        "Магазин %s: ждём %.1f c до следующего опроса",
                        shop["title"], wait,
                    )
                    await asyncio.sleep(wait)

                try:
                    data = await self._fetch_new_orders(
                        shop["key"], shop["title"], token, shop["env"]
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("Ошибка опроса %s: %s", shop["title"], exc)
                    self._last_poll[shop["key"]] = time.monotonic()
                    self._last_api_counts[shop["key"]] = 0
                    self._last_insert_counts[shop["key"]] = 0
                    # shop_state не трогаем.
                    continue

                self._last_poll[shop["key"]] = time.monotonic()

                is_first_success = not await self._shop_has_baseline(
                    db, shop["key"]
                )

                orders = (data or {}).get("orders") or []
                if not isinstance(orders, list):
                    orders = []
                self._last_api_counts[shop["key"]] = len(orders)

                logger.info(
                    "Магазин %s: получено заказов %d%s",
                    shop["title"], len(orders),
                    " (первый успешный опрос)" if is_first_success else "",
                )

                sample_path = DEBUG_DIR / f"wb_new_sample_{shop['key']}.json"
                if orders and not sample_path.exists():
                    self._save_sample(shop["key"], data)

                inserted_here = 0
                status = "baseline" if is_first_success else "new"
                for raw in orders:
                    if not isinstance(raw, dict):
                        continue
                    wb_order = self._parse_order(shop, raw)
                    if wb_order is None:
                        continue
                    inserted = await self._insert_order(
                        db, wb_order, raw, now_iso, status
                    )
                    if inserted:
                        inserted_here += 1
                        if is_first_success:
                            logger.info(
                                "baseline: записан заказ %s/%s",
                                shop["key"], wb_order.wb_order_id,
                            )
                        else:
                            new_orders.append(wb_order)
                self._last_insert_counts[shop["key"]] = inserted_here

                # Отмечаем baseline только при УСПЕШНОМ опросе
                # (в т.ч. при 0 заказов).
                if is_first_success:
                    await self._mark_baselined(db, shop["key"], now_iso)

            await db.commit()

        return new_orders

    async def run_forever(
        self, on_new: Callable[[WbOrder], Awaitable[None]]
    ) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception("poll_once упал: %s", exc)

            # Опрашиваем pending() — так не теряем заказы при падении on_new.
            try:
                pending_orders = await self.pending()
            except Exception as exc:  # noqa: BLE001
                logger.exception("pending() упал: %s", exc)
                pending_orders = []

            for order in pending_orders:
                # Помечаем dispatched ДО вызова — защита от повторного вызова.
                try:
                    await self.set_status(
                        order.shop_key, order.wb_order_id, "dispatched"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Не удалось пометить dispatched %s/%s: %s",
                        order.shop_key, order.wb_order_id, exc,
                    )
                    continue

                try:
                    await on_new(order)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "on_new упал для %s/%s: %s",
                        order.shop_key, order.wb_order_id, exc,
                    )
                    try:
                        await self.set_status(
                            order.shop_key,
                            order.wb_order_id,
                            "dispatch_error",
                        )
                    except Exception as exc2:  # noqa: BLE001
                        logger.exception(
                            "Не удалось пометить dispatch_error %s/%s: %s",
                            order.shop_key, order.wb_order_id, exc2,
                        )

            await asyncio.sleep(self._rate_limit_sec)

    # ------------------------------------------------------------------ #
    # Диагностика (для теста)
    # ------------------------------------------------------------------ #
    def last_api_count(self, shop_key: str) -> int:
        return int(self._last_api_counts.get(shop_key, 0))

    def last_insert_count(self, shop_key: str) -> int:
        return int(self._last_insert_counts.get(shop_key, 0))


__all__ = ["WbPoller", "WbOrder", "SHOPS"]
