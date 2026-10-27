# wb_content.py
"""
Получение названия товара WB по nmId через Content API.
Кэш — таблица wb_titles (через Storage).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from storage import Storage

logger = logging.getLogger(__name__)

CARDS_URL = "https://content-api.wildberries.ru/content/v2/get/cards/list"
DEBUG_DIR = Path("debug")
RETRY_DELAYS = [2, 5, 10]


class WbContent:
    def __init__(self, storage: "Storage", timeout: float = 20.0) -> None:
        self._storage = storage
        self._client = httpx.AsyncClient(timeout=timeout)
        self._sample_saved = False

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def get_title(
        self, shop_key: str, nm_id: Optional[int], token: str
    ) -> Optional[str]:
        if not nm_id or not token:
            return None
        nm_id = int(nm_id)
        cached = await self._storage.get_wb_title(shop_key, nm_id)
        if cached:
            return cached
        title = await self._fetch_title(token, nm_id)
        if title:
            try:
                await self._storage.save_wb_title(shop_key, nm_id, title)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не удалось сохранить wb_title: %s", exc)
        return title

    async def _fetch_title(self, token: str, nm_id: int) -> Optional[str]:
        body = {
            "settings": {
                "cursor": {"limit": 1},
                "filter": {"textSearch": str(nm_id), "withPhoto": -1},
            }
        }
        headers = {
            "Authorization": token,
            "Content-Type": "application/json",
        }
        retries = 0
        while True:
            try:
                resp = await self._client.post(
                    CARDS_URL, headers=headers, json=body
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if retries >= len(RETRY_DELAYS):
                    logger.warning("Сеть WB content API: %s", exc)
                    return None
                await asyncio.sleep(RETRY_DELAYS[retries])
                retries += 1
                continue

            if 500 <= resp.status_code < 600:
                if retries >= len(RETRY_DELAYS):
                    logger.warning("5xx WB content API: %d", resp.status_code)
                    return None
                await asyncio.sleep(RETRY_DELAYS[retries])
                retries += 1
                continue

            if resp.status_code in (401, 403):
                # Токен не логируем.
                logger.error(
                    "WB content API: недействительный токен (HTTP %d)",
                    resp.status_code,
                )
                return None

            if resp.status_code != 200:
                logger.warning(
                    "WB content API вернул %d", resp.status_code
                )
                return None

            try:
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("WB content API не JSON: %s", exc)
                return None

            self._save_sample(data)
            cards = data.get("cards") or []
            if not cards or not isinstance(cards, list):
                return None
            first = cards[0]
            if not isinstance(first, dict):
                return None
            title = first.get("title")
            if not title:
                return None
            return str(title)

    def _save_sample(self, data: dict) -> None:
        if self._sample_saved:
            return
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = DEBUG_DIR / "wb_card_sample.json"
            with path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("Сохранён образец ответа WB Content API: %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Не удалось сохранить wb_card_sample.json: %s", exc
            )
        self._sample_saved = True
