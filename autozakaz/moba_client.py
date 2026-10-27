# moba_client.py
"""
Модуль для работы с moba.ru: вход, поиск, наличие, цена.
Без оформления заказов и работы с корзиной.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://moba.ru"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
CITY_ID = 711
REQUEST_TIMEOUT = 20.0
RETRY_DELAYS = [2, 5, 10]
RATE_LIMIT_INTERVAL = 1.0
COOKIES_DIR = Path("data")
DEBUG_DIR = Path("debug")

_CATALOG_URL_RE = re.compile(
    r"^(?:https?://moba\.ru)?/catalog/[^/]+/[^/]+/?$"
)
_ARTICLE_RE = re.compile(r"Артикул\s*:\s*([A-Za-z0-9\-_./]+)")
_USER_ID_RE = re.compile(r'const\s+userId\s*=\s*"(\d*)"')


class MobaError(Exception):
    """Базовая ошибка модуля."""


class MobaAuthError(MobaError):
    """Ошибка авторизации."""


@dataclass
class MobaProduct:
    product_id: int
    article: str
    name: str
    url: str
    price: int
    stock_by_store: dict[str, bool] = field(default_factory=dict)
    max_qty: int = 0


class MobaClient:
    def __init__(self, login: str, password: str, account_name: str) -> None:
        self._login = login
        self._password = password
        self._account_name = account_name
        self._client: Optional[httpx.AsyncClient] = None
        self._rate_lock = asyncio.Lock()
        self._last_request_ts = 0.0
        self._stores_cache: dict[str, int] = {}
        self._stores_loaded = False
        self._cookies_path = COOKIES_DIR / f"cookies_{account_name}.json"

    # ------------------------------------------------------------------ #
    # Инфраструктура
    # ------------------------------------------------------------------ #
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=REQUEST_TIMEOUT,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                },
                follow_redirects=False,
            )
            self._load_cookies()
        return self._client

    def _load_cookies(self) -> None:
        if not self._cookies_path.exists():
            return
        try:
            with self._cookies_path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось прочитать cookies %s: %s", self._cookies_path, exc)
            return
        assert self._client is not None
        for item in raw:
            try:
                self._client.cookies.set(
                    name=item["name"],
                    value=item["value"],
                    domain=item.get("domain"),
                    path=item.get("path", "/"),
                )
            except Exception:  # noqa: BLE001
                continue
        logger.info("Загружены cookies для аккаунта %s", self._account_name)

    def _save_cookies(self) -> None:
        if self._client is None:
            return
        COOKIES_DIR.mkdir(parents=True, exist_ok=True)
        data = []
        for c in self._client.cookies.jar:
            data.append(
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path,
                }
            )
        try:
            with self._cookies_path.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось сохранить cookies: %s", exc)

    async def _rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            delta = now - self._last_request_ts
            if delta < RATE_LIMIT_INTERVAL:
                await asyncio.sleep(RATE_LIMIT_INTERVAL - delta)
            self._last_request_ts = time.monotonic()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        allow_redirects: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        client = await self._get_client()
        last_exc: Optional[Exception] = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            await self._rate_limit()
            try:
                resp = await client.request(
                    method,
                    url,
                    follow_redirects=allow_redirects,
                    **kwargs,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                logger.warning(
                    "Сетевая ошибка (%s) на %s %s, попытка %d",
                    exc, method, url, attempt + 1,
                )
            else:
                if 500 <= resp.status_code < 600:
                    logger.warning(
                        "5xx (%d) на %s %s, попытка %d",
                        resp.status_code, method, url, attempt + 1,
                    )
                else:
                    return resp
            if attempt < len(RETRY_DELAYS):
                await asyncio.sleep(RETRY_DELAYS[attempt])
        if last_exc is not None:
            raise MobaError(f"Сетевой сбой на {method} {url}: {last_exc}") from last_exc
        raise MobaError(f"Сервер вернул 5xx на {method} {url}")

    # ------------------------------------------------------------------ #
    # Авторизация
    # ------------------------------------------------------------------ #
    async def login(self) -> None:
        await self._get_client()
        await self._request("GET", "/personal/")

        payload = {
            "AUTH_FORM": "Y",
            "TYPE": "AUTH",
            "backurl": "/personal/",
            "USER_LOGIN": self._login,
            "USER_PASSWORD": self._password,
            "Login": "",
        }
        resp = await self._request(
            "POST",
            "/personal/?login=yes",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if resp.status_code not in (301, 302, 303):
            raise MobaAuthError(
                f"Ожидался редирект после логина, получен {resp.status_code}"
            )

        try:
            await self._check_session()
        except MobaAuthError as exc:
            raise MobaAuthError(f"неверный логин/пароль: {exc}") from exc

        self._save_cookies()
        logger.info("Успешный вход, аккаунт %s", self._account_name)

    async def _check_session(self) -> None:
        client = await self._get_client()
        await self._rate_limit()
        try:
            resp = await client.get("/personal/", follow_redirects=False)
        except httpx.HTTPError as exc:
            raise MobaAuthError(f"Ошибка проверки сессии: {exc}") from exc

        if resp.status_code in (301, 302, 303, 307, 308):
            raise MobaAuthError("Не залогинен (редирект с /personal/)")
        if resp.status_code != 200:
            raise MobaAuthError(
                f"Неожиданный код /personal/: {resp.status_code}"
            )
        m = _USER_ID_RE.search(resp.text)
        if not m or not m.group(1):
            raise MobaAuthError("userId пустой — не залогинен")

    async def ensure_logged_in(self) -> None:
        try:
            await self._check_session()
            return
        except MobaAuthError as exc:
            logger.info("Сессия неактивна (%s), выполняю повторный вход", exc)
        if self._client is not None:
            self._client.cookies.clear()
        self._stores_cache.clear()
        self._stores_loaded = False
        await self.login()

    # ------------------------------------------------------------------ #
    # Магазины
    # ------------------------------------------------------------------ #
    async def _load_stores(self) -> None:
        if self._stores_loaded:
            return
        resp = await self._request("GET", f"/api/stores?city_id={CITY_ID}")
        if resp.status_code != 200:
            raise MobaError(f"/api/stores вернул {resp.status_code}")
        data = resp.json()
        stores = data if isinstance(data, list) else data.get("stores") or data.get("items") or []
        cache: dict[str, int] = {}
        for st in stores:
            if not isinstance(st, dict):
                continue
            label = st.get("label") or st.get("name")
            sid = st.get("id")
            if label and sid is not None:
                cache[str(label).strip()] = int(sid)
        self._stores_cache = cache
        self._stores_loaded = True
        logger.info("Загружено магазинов: %d", len(cache))

    async def _store_id(self, store: str) -> Optional[int]:
        await self._load_stores()
        if store in self._stores_cache:
            return self._stores_cache[store]
        for label, sid in self._stores_cache.items():
            if store.lower() in label.lower():
                return sid
        return None

    # ------------------------------------------------------------------ #
    # Поиск
    # ------------------------------------------------------------------ #
    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Поиск: сначала быстрый JSON API, если он сломан (бывает 500) — HTML-каталог.
        Если API упал, 10 минут сразу идём в HTML, не тратя время на повторы."""
        items: list[dict] = []
        now = asyncio.get_event_loop().time()
        if now >= getattr(self, "_api_down_until", 0.0):
            try:
                client = await self._get_client()
                await self._rate_limit()
                resp = await client.get("/api/products/search", params={"query": query})
                if resp.status_code == 200:
                    data = resp.json()
                    self._dump_debug_search(data)
                    items = self._extract_search_items(data)
                else:
                    logger.warning("moba search API %d — переключаюсь на HTML на 10 мин", resp.status_code)
                    self._api_down_until = now + 600
            except Exception as exc:  # noqa: BLE001
                logger.warning("moba search API: %s — переключаюсь на HTML на 10 мин", exc)
                self._api_down_until = now + 600

        if not items:
            items = await self._search_html(query)

        return items[:limit]

    def _dump_debug_search(self, data: Any) -> None:
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = DEBUG_DIR / "search_sample.json"
            if not path.exists():
                with path.open("w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Не удалось сохранить debug/search_sample.json: %s", exc)

    @staticmethod
    def _extract_search_items(data: Any) -> list[dict]:
        candidates_keys = (
            "products", "products_result", "items", "results",
            "hits", "goods", "product_list",
        )
        raw_items: list[Any] = []

        def pick(obj: Any) -> None:
            if isinstance(obj, dict):
                for key in candidates_keys:
                    val = obj.get(key)
                    if isinstance(val, list) and val:
                        raw_items.extend(val)
                        return
                for v in obj.values():
                    pick(v)
            elif isinstance(obj, list):
                for v in obj:
                    pick(v)

        pick(data)

        result: list[dict] = []
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            url = it.get("url") or it.get("link") or it.get("href")
            name = it.get("name") or it.get("title") or it.get("label") or ""
            name = re.sub(r"<[^>]+>", "", str(name)).strip()  # в API бывает <em>
            article = it.get("article") or it.get("artikul") or it.get("sku")
            if not url:
                continue
            if url.startswith("/"):
                url = BASE_URL + url
            if not _CATALOG_URL_RE.match(url):
                continue
            entry: dict = {"url": url, "name": name}
            if isinstance(it.get("price"), (int, float)):
                entry["price"] = it["price"]
            if article:
                entry["article"] = article
            result.append(entry)
        return result

    @staticmethod
    def _parse_catalog_cards(html_text: str) -> list[dict]:
        """Карточки товаров со страницы каталога/поиска: url, name, price."""
        out: list[dict] = []
        seen: set[str] = set()
        for block in html_text.split('product-grid"')[1:]:
            m_href = re.search(r'href="(/catalog/[^"?#]+/)"', block)
            m_name = re.search(r'product-grid__name".*?<span[^>]*>(.*?)</span>', block, re.S)
            if not m_href or not m_name:
                continue
            url = BASE_URL + m_href.group(1)
            if url in seen:
                continue
            seen.add(url)
            name = html_lib.unescape(re.sub(r"<[^>]+>", "", m_name.group(1))).strip()
            entry: dict = {"url": url, "name": name}
            m_price = re.search(r'product-grid__price[^>]*>\s*([\d\s]+)\s*₽', block)
            if m_price:
                try:
                    entry["price"] = int(re.sub(r"\s", "", m_price.group(1)))
                except ValueError:
                    pass
            out.append(entry)
        return out

    async def _search_html(self, query: str) -> list[dict]:
        resp = await self._request("GET", "/catalog/", params={"q": query})
        if resp.status_code != 200:
            return []
        cards = self._parse_catalog_cards(resp.text)
        if cards:
            return cards
        soup = BeautifulSoup(resp.text, "lxml")
        seen: set[str] = set()
        items: list[dict] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].split("?")[0].split("#")[0]
            if not href:
                continue
            if href.startswith("http://") or href.startswith("https://"):
                if not href.startswith(BASE_URL):
                    continue
                full = href
                path = full[len(BASE_URL):]
            else:
                if not href.startswith("/"):
                    continue
                path = href
                full = BASE_URL + href
            if not _CATALOG_URL_RE.match(path):
                continue
            if full in seen:
                continue
            seen.add(full)
            name = a.get_text(" ", strip=True)
            if not name:
                continue
            items.append({"url": full, "name": name})
        return items

    # ------------------------------------------------------------------ #
    # Карточка товара
    # ------------------------------------------------------------------ #
    async def get_product(self, url: str) -> MobaProduct:
        if url.startswith("/"):
            url = BASE_URL + url
        resp = await self._request("GET", url)
        if resp.status_code != 200:
            raise MobaError(f"Карточка {url} вернула {resp.status_code}")
        return self._parse_product(resp.text, url)

    def _parse_product(self, html: str, url: str) -> MobaProduct:
        soup = BeautifulSoup(html, "lxml")

        h1 = soup.find("h1")
        name = h1.get_text(" ", strip=True) if h1 else ""

        article = ""
        m = _ARTICLE_RE.search(html)
        if m:
            article = m.group(1)

        article_pos = -1
        for i, elem in enumerate(soup.descendants):
            if isinstance(elem, str) and "Артикул:" in elem:
                article_pos = i
                break

        product_data: Optional[str] = None
        for i, elem in enumerate(soup.descendants):
            if article_pos >= 0 and i <= article_pos:
                continue
            if not hasattr(elem, "get"):
                continue
            xd = elem.get("x-data")
            if xd and "product" in xd:
                product_data = xd
                break

        if not product_data:
            raise MobaError("не удалось разобрать карточку")

        product_id = self._extract_int(product_data, r"id\s*:\s*(\d+)")
        price = self._extract_int(product_data, r"price\s*:\s*(\d+)")
        max_qty = self._extract_int(product_data, r"max\s*:\s*(\d+)")

        if not product_id or not price:
            raise MobaError("не удалось разобрать карточку")

        stock = self._parse_stock(soup)
        stock_block_found = stock is not None
        stock_dict = stock if stock is not None else {}

        product = MobaProduct(
            product_id=product_id,
            article=article,
            name=name,
            url=url,
            price=price,
            stock_by_store=stock_dict,
            max_qty=max_qty,
        )
        # Внутренний признак: был ли вообще блок «Наличие в магазинах»
        product._stock_block_found = stock_block_found  # type: ignore[attr-defined]
        return product

    @staticmethod
    def _extract_int(text: str, pattern: str) -> int:
        m = re.search(pattern, text)
        return int(m.group(1)) if m else 0

    @staticmethod
    def _parse_stock(soup: BeautifulSoup) -> Optional[dict[str, bool]]:
        """
        Возвращает:
          None — блок «Наличие в магазинах» не найден на странице;
          dict — блок найден (возможно пустой).
        """
        text = soup.get_text("\n", strip=True)
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

        start_idx: Optional[int] = None
        for i, line in enumerate(lines):
            if "Наличие в магазинах" in line:
                start_idx = i + 1
                break
        if start_idx is None:
            return None

        end_idx = len(lines)
        for i in range(start_idx, len(lines)):
            if "Показать весь список" in lines[i] or "Оплата" in lines[i]:
                end_idx = i
                break

        section = lines[start_idx:end_idx]
        stock: dict[str, bool] = {}
        i = 0
        while i + 1 < len(section):
            head = section[i]
            status = section[i + 1]
            if "," in head and not head.startswith("В наличии"):
                store = head.split(",", 1)[0].strip()
                if store:
                    stock[store] = (status.strip() == "В наличии")
                    i += 2
                    continue
            i += 1
        return stock

    async def mitino_status(self, url: str) -> str:
        product = await self.get_product(url)
        if not getattr(product, "_stock_block_found", False):
            return "unknown"
        if "Митино" not in product.stock_by_store:
            return "no"
        return "yes" if product.stock_by_store["Митино"] else "no"

    async def available_in(self, url: str, store: str = "Митино") -> Optional[MobaProduct]:
        product = await self.get_product(url)
        if product.stock_by_store.get(store) is True:
            return product
        return None

    # ------------------------------------------------------------------ #
    # Завершение
    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        if self._client is not None:
            self._save_cookies()
            await self._client.aclose()
            self._client = None
