# wb_supply.py
"""
Перенос заказа WB в сборку (поставку FBS).
Только по действию человека (кнопка / команда).
В dry_run не вызывается.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Union
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

WB_BASE = "https://marketplace-api.wildberries.ru"
DEBUG_DIR = Path("debug")
RETRY_DELAYS = [2, 5, 10]
MAX_429_RETRIES = 3

MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


class WbSupplyError(Exception):
    pass


def supply_name_for_today() -> str:
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    return f"Отправка {now.day} {MONTHS_GENITIVE[now.month - 1]}"


class WbSupply:
    def __init__(self, timeout: float = 20.0) -> None:
        self._client = httpx.AsyncClient(timeout=timeout)
        self._locks: dict[str, asyncio.Lock] = {}
        self._samples_saved: set[str] = set()

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Lock per token (чтобы два заказа не создали две поставки)
    # ------------------------------------------------------------------ #
    def _lock_for(self, token: str) -> asyncio.Lock:
        lock = self._locks.get(token)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[token] = lock
        return lock

    # ------------------------------------------------------------------ #
    # debug-образцы
    # ------------------------------------------------------------------ #
    def _save_sample(
        self,
        key: str,
        method: str,
        url: str,
        status: int,
        body: str,
    ) -> None:
        if key in self._samples_saved:
            return
        self._samples_saved.add(key)
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = DEBUG_DIR / f"wb_supply_{key}.json"
            with path.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "method": method,
                        "url": url,
                        "status": status,
                        "body": body[:10000],
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            logger.info("Сохранён образец WB supply: %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось сохранить образец %s: %s", key, exc)

    # ------------------------------------------------------------------ #
    # HTTP с повторами
    # ------------------------------------------------------------------ #
    async def _request(
        self,
        method: str,
        url: str,
        token: str,
        *,
        json_body: Optional[dict] = None,
        sample_key: Optional[str] = None,
    ) -> httpx.Response:
        headers = {"Authorization": token}
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        retries_5xx = 0
        retries_429 = 0

        while True:
            try:
                resp = await self._client.request(
                    method, url, headers=headers, json=json_body
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                # POST/PATCH повторяем только если запрос точно не ушёл
                # (ошибка соединения), иначе можно создать вторую поставку.
                if method != "GET" and not isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout)
                ):
                    raise WbSupplyError(
                        f"сетевая ошибка, результат неизвестен: {exc}. "
                        f"Проверь сборку в ЛК WB перед повтором"
                    ) from exc
                if retries_5xx >= len(RETRY_DELAYS):
                    raise WbSupplyError(f"сетевая ошибка: {exc}") from exc
                wait = RETRY_DELAYS[retries_5xx]
                retries_5xx += 1
                logger.warning("Сеть %s, повтор через %s c", exc, wait)
                await asyncio.sleep(wait)
                continue

            if resp.status_code == 429:
                if retries_429 >= MAX_429_RETRIES:
                    raise WbSupplyError("429 не отпускает")
                ra = resp.headers.get("Retry-After")
                wait = 60.0
                if ra:
                    try:
                        wait = float(ra)
                    except ValueError:
                        pass
                retries_429 += 1
                logger.warning(
                    "429, ждём %.1f c (попытка %d/%d)",
                    wait, retries_429, MAX_429_RETRIES,
                )
                await asyncio.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                if retries_5xx >= len(RETRY_DELAYS):
                    raise WbSupplyError(
                        f"5xx ({resp.status_code}): {resp.text[:300]}"
                    )
                wait = RETRY_DELAYS[retries_5xx]
                retries_5xx += 1
                logger.warning(
                    "5xx (%d), повтор через %s c", resp.status_code, wait
                )
                await asyncio.sleep(wait)
                continue

            if sample_key:
                self._save_sample(
                    sample_key, method, url, resp.status_code, resp.text
                )

            if 400 <= resp.status_code < 500:
                # Не повторяем. Полное тело в лог.
                logger.error(
                    "WB supply %s %s → %d: %s",
                    method, url, resp.status_code, resp.text[:500],
                )
                raise WbSupplyError(
                    f"HTTP {resp.status_code}: {resp.text[:500]}"
                )

            return resp

    # ------------------------------------------------------------------ #
    # Поставки
    # ------------------------------------------------------------------ #
    async def find_open_supply(
        self, token: str, name: str
    ) -> Optional[str]:
        next_val = 0
        seen_nexts: set[int] = set()
        while True:
            url = f"{WB_BASE}/api/v3/supplies?limit=1000&next={next_val}"
            resp = await self._request(
                "GET", url, token, sample_key="supplies"
            )
            try:
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                raise WbSupplyError(
                    f"не JSON в supplies: {resp.text[:200]}"
                ) from exc
            supplies = data.get("supplies") or []
            for s in supplies:
                if not isinstance(s, dict):
                    continue
                if s.get("name") == name and not s.get("done"):
                    return str(s.get("id"))
            if not supplies:
                return None
            nxt = data.get("next")
            if not isinstance(nxt, int) or nxt in seen_nexts:
                return None
            seen_nexts.add(nxt)
            next_val = nxt

    async def create_supply(self, token: str, name: str) -> str:
        url = f"{WB_BASE}/api/v3/supplies"
        resp = await self._request(
            "POST", url, token,
            json_body={"name": name},
            sample_key="create",
        )
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise WbSupplyError(
                f"не JSON в create_supply: {resp.text[:200]}"
            ) from exc
        sid = data.get("id")
        if not sid:
            raise WbSupplyError(
                f"create_supply: нет id в ответе: {data!r}"
            )
        return str(sid)

    async def add_order(
        self, token: str, supply_id: str, wb_order_id: int
    ) -> None:
        url = (
            f"{WB_BASE}/api/marketplace/v3/supplies/"
            f"{supply_id}/orders"
        )
        resp = await self._request(
            "PATCH", url, token,
            json_body={"orders": [int(wb_order_id)]},
            sample_key="add_order",
        )
        if resp.status_code not in (200, 204):
            raise WbSupplyError(
                f"add_order: HTTP {resp.status_code}: {resp.text[:300]}"
            )

    async def order_in_supply(
        self, token: str, supply_id: str, wb_order_id: int
    ) -> bool:
        url = (
            f"{WB_BASE}/api/marketplace/v3/supplies/"
            f"{supply_id}/order-ids"
        )
        resp = await self._request(
            "GET", url, token, sample_key="order_ids"
        )
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise WbSupplyError(
                f"не JSON в order-ids: {resp.text[:200]}"
            ) from exc
        ids: list = []
        if isinstance(data, list):
            ids = data
        elif isinstance(data, dict):
            for k in ("orders", "orderIds", "ids"):
                v = data.get(k)
                if isinstance(v, list):
                    ids = v
                    break
        target = int(wb_order_id)
        for x in ids:
            try:
                if int(x) == target:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    # ------------------------------------------------------------------ #
    # Основное действие
    # ------------------------------------------------------------------ #
    async def move_to_today(
        self, token: str, wb_order_id: int
    ) -> tuple[str, str]:
        async with self._lock_for(token):
            name = supply_name_for_today()
            sid = await self.find_open_supply(token, name)
            if sid is None:
                sid = await self.create_supply(token, name)
                logger.info("Создана поставка %s «%s»", sid, name)
            elif await self.order_in_supply(token, sid, wb_order_id):
                # повтор после сбоя: заказ уже там
                return sid, name
            await self.add_order(token, sid, wb_order_id)
            ok = await self.order_in_supply(token, sid, wb_order_id)
            if not ok:
                raise WbSupplyError(
                    f"заказ {wb_order_id} не найден в поставке {sid} "
                    f"после добавления"
                )
            return sid, name
