"""Liberti (liberti.ru, оптовый кабинет Анри): поиск по артикулу, остаток «Либерти - Митино», корзина, оформление.

Разведка 21.10 (discover_liberti*.py):
  * вход — стандартный Битрикс: POST /?login=yes (AUTH_FORM/TYPE/USER_LOGIN/USER_PASSWORD);
  * поиск: /search/?q=<артикул> → карточки product-list-item data-id=…, «Артикул: 0L-…», оптовая цена;
  * карточка: «Наличие на складе "Либерти - Митино": N шт.»;
  * в корзину: POST /local/public/basket.php {method: add, id, quantity, sessid};
  * оформление: компонент sale.order.ajax (самовывоз ID 1, наличные ID 1), ajax.php action=saveOrderAjax.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger(__name__)
BASE = "https://liberti.ru"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
AJAX = "/local/components/bitrix/sale.order.ajax/ajax.php"
MIN_INTERVAL = 1.0


def _plain(t: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t)


def _num(s: str) -> Optional[int]:
    s = re.sub(r"[^\d]", "", s or "")
    return int(s) if s else None


@dataclass
class LibItem:
    id: int
    code: str
    name: str
    url: str
    opt: Optional[int] = None
    mitino: Optional[int] = None      # шт. на складе «Либерти - Митино» (None — не понял)


@dataclass
class LibOrderResult:
    status: str                        # placed / dry_run_ok / failed / unknown
    order_number: Optional[str] = None
    total: Optional[float] = None
    problems: list[str] = field(default_factory=list)


class LibertiClient:
    def __init__(self, login: str, password: str) -> None:
        self.login_, self.password = login, password
        self.c = httpx.AsyncClient(base_url=BASE, follow_redirects=True, timeout=30,
                                   headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})
        self.sessid: Optional[str] = None
        self.logged = False
        self._last = 0.0

    async def _get(self, url: str, **kw) -> httpx.Response:
        await self._pace()
        return await self.c.get(url, **kw)

    async def _post(self, url: str, **kw) -> httpx.Response:
        await self._pace()
        return await self.c.post(url, **kw)

    async def _pace(self) -> None:
        loop = asyncio.get_event_loop()
        d = loop.time() - self._last
        if d < MIN_INTERVAL:
            await asyncio.sleep(MIN_INTERVAL - d)
        self._last = loop.time()

    def _grab_sessid(self, text: str) -> None:
        m = re.search(r"bitrix_sessid['\"]?\s*:\s*['\"]([0-9a-f]{32})", text) or \
            re.search(r'sessid"\s*:\s*"([0-9a-f]{32})', text) or \
            re.search(r'name="sessid"[^>]*value="([0-9a-f]{32})"', text)
        if m:
            self.sessid = m.group(1)

    async def login(self) -> None:
        if self.logged:
            return
        await self._get("/")
        r = await self._post("/?login=yes", data={"AUTH_FORM": "Y", "TYPE": "AUTH", "backurl": "/",
                                                  "USER_LOGIN": self.login_, "USER_PASSWORD": self.password,
                                                  "USER_REMEMBER": "Y", "Login": "Войти"})
        if not re.search(r"logout=yes|Выйти", r.text):
            raise RuntimeError("Liberti: не удалось войти (логин/пароль?)")
        self._grab_sessid(r.text)
        self.logged = True

    # ---------- каталог ----------
    async def search_code(self, code: str) -> Optional[LibItem]:
        """Товар по артикулу (0L-…) из поиска."""
        await self.login()
        r = await self._get("/search/", params={"q": code})
        self._grab_sessid(r.text)
        for blk in re.split(r'class="product-list-item product-list-item--table"\s+data-id="', r.text)[1:]:
            pid = _num(blk[:15])
            art = re.search(r"Артикул:\s*([0-9A-Za-zА-Яа-я-]+)", blk)
            if not pid or not art or art.group(1).strip().upper() != code.strip().upper():
                continue
            href = re.search(r'product-list-item__name-link"\s*href="([^"]+)"', blk)
            name = re.search(r'product-list-item__name-link"[^>]*>\s*<span[^>]*>(.*?)</span>', blk, re.S)
            opt = re.search(r'price--main">\s*<div class="product-list-item__price-value">\s*([\d\s]+)', blk)
            return LibItem(id=pid, code=code, name=_html.unescape(name.group(1)).strip() if name else code,
                           url=href.group(1) if href else "", opt=_num(opt.group(1)) if opt else None)
        return None

    async def mitino(self, item: LibItem) -> LibItem:
        """Остаток на складе «Либерти - Митино» и оптовая цена с карточки."""
        if not item.url:
            return item
        r = await self._get(item.url)
        self._grab_sessid(r.text)
        t = _plain(r.text)
        m = re.search(r'Наличие на складе "?Либерти\s*-\s*Митино"?\s*:\s*(\d+|нет|много)', t, re.I)
        if m:
            v = m.group(1).lower()
            item.mitino = 0 if v == "нет" else (50 if v == "много" else int(v))
        else:
            item.mitino = 0 if "Либерти - Митино" not in t else None
        m = re.search(r"([\d\s]+)\s*₽\s*Оптовая цена", t)
        if m:
            item.opt = _num(m.group(1)) or item.opt
        return item

    # ---------- корзина ----------
    async def basket_ids(self) -> list[str]:
        r = await self._get("/personal/cart/")
        self._grab_sessid(r.text)
        m = re.search(r'"basketProductIds"\s*:\s*\[([^\]]*)\]', r.text)
        return re.findall(r'"?(\d+)"?', m.group(1)) if m else []

    async def add(self, pid: int, qty: int) -> dict:
        await self.login()
        if not self.sessid:
            await self.basket_ids()
        r = await self._post("/local/public/basket.php",
                             data={"method": "add", "id": pid, "quantity": qty, "sessid": self.sessid},
                             headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/personal/cart/"})
        try:
            d = r.json()
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"Liberti корзина: ответ {r.status_code} {r.text[:150]}")
        if d.get("status") == "error":
            raise RuntimeError(f"Liberti корзина: {d.get('error')}")
        return d

    # ---------- оформление ----------
    async def _order_page(self) -> dict:
        r = await self._get("/personal/order/make/")
        t = r.text
        self._grab_sessid(t)
        sp = re.search(r"signedParamsString:\s*'([^']+)'", t)
        rows = re.findall(r"'PRODUCT_ID':'(\d+)','QUANTITY':'([\d.]+)'.*?'PRICE':'([\d.]+)'", t)
        tot = re.search(r"'ORDER_TOTAL_PRICE':'([\d.]+)'", t)
        props = {}
        for pid, val in re.findall(r"\{'ID':'(\d+)','PERSON_TYPE_ID':'1'.*?'VALUE':\['([^']*)'\]\}", t):
            props[pid] = val
        return {"signed": sp.group(1) if sp else None, "rows": rows,
                "total": float(tot.group(1)) if tot else None, "props": props}

    async def checkout(self, expected: dict[int, int], phone: str, comment: str, submit: bool,
                       max_total: float) -> LibOrderResult:
        """expected: {product_id: qty}. Корзина должна совпадать с ним (чужое в корзине — не оформляем)."""
        res = LibOrderResult(status="failed")
        page = await self._order_page()
        got: dict[int, int] = {}
        for pid, q, _p in page["rows"]:
            got[int(pid)] = got.get(int(pid), 0) + int(float(q))
        if got != expected:
            extra = {k: v for k, v in got.items() if k not in expected}
            res.problems.append(f"корзина не совпадает с заказом (ждал {expected}, в корзине {got})"
                                + (" — в корзине есть чужие товары, очисти её на сайте" if extra else ""))
            return res
        res.total = page["total"]
        if res.total is None or res.total > max_total * 1.05 + 1:
            res.problems.append(f"сумма {res.total} ₽ больше ожидаемой {max_total:.0f} ₽")
            return res
        if not page["signed"] or not self.sessid:
            res.problems.append("не нашёл служебные поля формы оформления")
            return res
        if not submit:
            res.status = "dry_run_ok"
            return res
        p = page["props"]
        data = {
            "via_ajax": "Y", "action": "saveOrderAjax", "soa-action": "saveOrderAjax", "SITE_ID": "s1",
            "sessid": self.sessid, "signedParamsString": page["signed"], "location_type": "code",
            "BUYER_STORE": "0", "PERSON_TYPE": "1", "PERSON_TYPE_OLD": "1", "DELIVERY_ID": "1", "PAY_SYSTEM_ID": "1",
            "ORDER_PROP_1": p.get("1") or "anri sar", "ORDER_PROP_15": p.get("15") or "0000073738",
            "ORDER_PROP_3": phone, "ORDER_PROP_2": p.get("2") or "", "ORDER_PROP_24": comment,
            "ORDER_DESCRIPTION": comment, "guaranteeCheckbox": "on", "save": "Y",
        }
        try:
            r = await self._post(AJAX, data=data, headers={"X-Requested-With": "XMLHttpRequest",
                                                           "Referer": BASE + "/personal/order/make/"})
            d = r.json()
        except Exception as exc:  # noqa: BLE001
            res.status = "unknown"
            res.problems.append(f"ответ на оформление не разобрал: {exc} — проверь «Историю заказов» на сайте")
            return res
        order = d.get("order") or {}
        url = order.get("REDIRECT_URL") or ""
        num = order.get("ID") or (re.search(r"ORDER_ID=([\w-]+)", url) or [None, None])[1]
        if num:
            res.status, res.order_number = "placed", str(num)
            return res
        err = order.get("ERROR") or d.get("error") or d
        res.problems.append(f"сайт не принял заказ: {str(err)[:300]}")
        return res

    # ---------- остаток Митино всем каталогом (22.10: список = склад «Либерти - Митино», сверено) ----------
    async def _list_page(self, url: str, page: int) -> tuple[list[dict], str]:
        r = await self._get(url, params={"PAGEN_1": page} if page > 1 else None)
        out = []
        for blk in re.split(r'class="product-list-item[^"]*"\s+data-id="', r.text)[1:]:
            pid = re.match(r"(\d+)", blk)
            art = re.search(r"Артикул:\s*([0-9A-Za-z-]+)", blk)
            if not pid or not art:
                continue
            q = re.search(r"В наличии\s*(\d+)\s*шт", blk)
            href = re.search(r'href="(/[^"]+\.html)"', blk)
            opt = re.search(r'price--main">\s*<div class="product-list-item__price-value">\s*([\d\s]+)', blk)
            out.append({"id": int(pid.group(1)), "code": art.group(1), "mitino": int(q.group(1)) if q else 0,
                        "url": href.group(1) if href else "", "opt": _num(opt.group(1)) if opt else None})
        return out, r.text

    async def mitino_snapshot(self, roots: tuple[str, ...] = ("/catalog/zapchasti/zapchasti-dlya-telefonov/",
                                                              "/catalog/zapchasti/zapchasti-dlya-apple/"),
                              max_pages: int = 60) -> dict[str, dict]:
        """{артикул: {id, mitino, url, opt}} по всем подразделам. ~1 запрос в секунду."""
        await self.login()
        snap: dict[str, dict] = {}
        for root in roots:
            r = await self._get(root)
            subs = sorted(set(re.findall(r'href="(' + re.escape(root) + r'[^"#?]+/)"', r.text))) or [root]
            for sub in subs:
                seen_first = None
                for page in range(1, max_pages + 1):
                    items, _t = await self._list_page(sub, page)
                    if not items or items[0]["code"] == seen_first:
                        break            # страницы кончились (Битрикс отдаёт последнюю повторно)
                    if page == 1:
                        seen_first = items[0]["code"]
                    new = 0
                    for it in items:
                        if it["code"] not in snap:
                            new += 1
                        snap[it["code"]] = it
                    if new == 0 or len(items) < 20:
                        break
                log.info("Liberti %s: всего %d", sub, len(snap))
        return snap

    async def close(self) -> None:
        await self.c.aclose()
