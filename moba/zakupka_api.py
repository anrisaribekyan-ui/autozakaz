# zakupka_api.py
"""
«Оформить у поставщиков» для раздела «Закупки» CRM (решение Анри, 27.10).

CRM присылает карточки из «Что купить» с выбранным магазином; сервис:
  * GET  /config — какие магазины и аккаунты настроены (по логинам в .env), боевой режим или пробный;
  * POST /quote  — по тексту карточки ищет товар у поставщика: варианты, цена, наличие в МИТИНО;
  * POST /order  — оформляет ОДИН заказ на выбранные позиции: самовывоз Митино, оплата при получении.

Оформляют готовые и обкатанные модули: zakaz_batch.moba_batch, mm_order.MasterMobileOrderer,
liberti_client.LibertiClient — со всеми их проверками (Митино, итог, чужое в корзине → не оформлять).

Безопасность:
  * каждый запрос — с заголовком X-Zakupka-Token = ZAKUPKA_TOKEN из .env (без токена сервис не стартует);
  * БОЕВОЙ режим только при ZAKUPKA_LIVE=1 в .env; иначе всё то же, но последняя кнопка не нажимается
    и корзина очищается (статус dry_run_ok);
  * request_id: повторный запрос с тем же id (двойное нажатие, обрыв сети) НЕ оформляет второй заказ —
    возвращает сохранённый результат (журнал data/zakupka_orders.jsonl);
  * один заказ на магазин+аккаунт за раз (замок), лимит суммы ZAKUPKA_MAX_RUB (по умолчанию 30 000 ₽).

Запуск:  venv/bin/python zakupka_api.py          (порт ZAKUPKA_PORT, по умолчанию 8095)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

from aiohttp import web

log = logging.getLogger("zakupka_api")

DATA_DIR = Path("data")
JOURNAL = DATA_DIR / "zakupka_orders.jsonl"
MAX_ITEMS = 40
MAX_CANDIDATES = 5
SUPPLIER_NAMES = {"moba": "Moba", "mm": "Master Mobile", "liberti": "Liberti"}
# Поставщик в доске закупок (id магазина на карточке) → наш код
DOSKA_SUPPLIER = {"moba": "moba", "master": "mm", "liberti": "liberti"}


# ---------------------------------------------------------------------- #
# Данные
# ---------------------------------------------------------------------- #
@dataclass
class Candidate:
    product_id: str
    name: str
    price: Optional[int]
    mitino: Optional[int]          # шт. в Митино; 1 — «есть» (moba не говорит сколько), 0 — нет, None — не понял
    url: str = ""
    article: str = ""
    recommended: bool = False


@dataclass
class OrderOutcome:
    status: str                    # placed | dry_run_ok | rejected | unknown
    order_number: Optional[str] = None
    total: Optional[float] = None
    got: dict[str, int] = field(default_factory=dict)   # что реально вошло в заказ: product_id → шт.
    problems: list[str] = field(default_factory=list)


class Supplier(Protocol):
    async def quote(self, text: str) -> list[Candidate]: ...
    async def order(self, wanted: "OrderedDict[str, int]", expected_total: int, request_id: str,
                    comment: str, live: bool, max_rub: int) -> OrderOutcome: ...
    async def close(self) -> None: ...


def accounts_from_env() -> dict[str, dict[str, tuple[str, str]]]:
    """{supplier: {account: (login, password)}} — только те, у кого в .env есть логин и пароль."""
    def pair(lk: str, pk: str) -> Optional[tuple[str, str]]:
        lo, pw = os.getenv(lk, "").strip(), os.getenv(pk, "").strip()
        return (lo, pw) if lo and pw else None

    table = {
        "moba": {"anri": ("MOBA_LOGIN", "MOBA_PASSWORD"), "gor": ("MOBA_GOR_LOGIN", "MOBA_GOR_PASSWORD")},
        "mm": {"anri": ("MM_LOGIN", "MM_PASSWORD"), "gor": ("MM_GOR_LOGIN", "MM_GOR_PASSWORD")},
        "liberti": {"anri": ("LIBERTI_LOGIN", "LIBERTI_PASSWORD"), "gor": ("LIBERTI_GOR_LOGIN", "LIBERTI_GOR_PASSWORD")},
    }
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for sup, accs in table.items():
        for acc, (lk, pk) in accs.items():
            p = pair(lk, pk)
            if p:
                out.setdefault(sup, {})[acc] = p
    return out


ACCOUNT_NAMES = {"anri": "Анри", "gor": "Гор"}


# ---------------------------------------------------------------------- #
# Боевые поставщики (импорт внутри — Playwright и прочее грузятся только при надобности)
# ---------------------------------------------------------------------- #
class MobaSupplier:
    def __init__(self, login: str, password: str, account: str) -> None:
        self.login, self.password, self.account = login, password, account
        self._client = None
        self._learned = None

    async def _ensure(self):
        if self._client is None:
            from learning import Learned
            from moba_client import MobaClient
            from storage import Storage
            storage = Storage("data/autozakaz.db")
            await storage.init()
            try:
                self._learned = Learned(await storage.feedback_rows(owner=self.account))
            except Exception as exc:  # noqa: BLE001
                log.warning("обучение не загрузилось: %s", exc)
                self._learned = None
            self._client = MobaClient(self.login, self.password, self.account)
        return self._client

    async def quote(self, text: str) -> list[Candidate]:
        from matcher import find_candidates
        client = await self._ensure()
        res = await find_candidates(client, text, learned=self._learned, max_items=MAX_CANDIDATES)
        out = []
        for it in res.get("items") or []:
            if not it.get("product_id"):
                continue
            m = it.get("mitino")
            out.append(Candidate(product_id=str(it["product_id"]), name=it.get("name") or "",
                                 price=int(it["price"]) if it.get("price") else None,
                                 mitino=1 if m == "yes" else (0 if m == "no" else None),
                                 url=it.get("url") or "", article=it.get("article") or "",
                                 recommended=bool(it.get("recommended"))))
        return out

    async def order(self, wanted, expected_total, request_id, comment, live, max_rub) -> OrderOutcome:
        from zakaz_batch import moba_batch
        run_dir = DATA_DIR / "zakupka_runs" / request_id
        run_dir.mkdir(parents=True, exist_ok=True)
        os.environ["BATCH_MAX_RUB"] = str(max_rub)   # лимит moba_batch читает из окружения
        res = await moba_batch(self.login, self.password, OrderedDict((int(k), v) for k, v in wanted.items()),
                               live, run_dir, request_id, account=self.account)
        total = res.get("total")
        problems = list(res.get("problems") or [])
        if total is not None and expected_total and total > expected_total * 1.05 + 1:
            problems.append(f"итог {total} ₽ больше ожидаемого {expected_total} ₽")
        return OrderOutcome(status=res.get("status") or "rejected", order_number=res.get("order_number"),
                            total=total, got={str(k): int(v) for k, v in (res.get("got") or {}).items()},
                            problems=problems)

    async def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass


class MasterMobileSupplier:
    def __init__(self, login: str, password: str, account: str) -> None:
        self.login, self.password, self.account = login, password, account
        self._client = None

    async def quote(self, text: str) -> list[Candidate]:
        from suppliers_ext import MasterMobileClient
        if self._client is None:
            self._client = MasterMobileClient()
        found = (await self._client.search(text))[:MAX_CANDIDATES]
        out = []
        for it in found:
            pid = str(it.get("product_id") or "")
            if not pid:
                continue
            amount = await self._client.mitino_amount(pid, it.get("url"))
            price = it.get("price_club") or it.get("price")
            out.append(Candidate(product_id=pid, name=it.get("name") or "", price=int(price) if price else None,
                                 mitino=amount, url=it.get("url") or "", article=it.get("article") or ""))
        out.sort(key=lambda c: (0 if (c.mitino or 0) > 0 else 1, c.price or 10**9))
        if out and (out[0].mitino or 0) > 0:
            out[0].recommended = True
        return out

    async def order(self, wanted, expected_total, request_id, comment, live, max_rub) -> OrderOutcome:
        from mm_order import MasterMobileOrderer
        o = MasterMobileOrderer(self.login, self.password)
        await o.start()
        try:
            limit = min(float(max_rub), expected_total * 1.05 + 1) if expected_total else float(max_rub)
            r = await o.check_order({str(k): int(v) for k, v in wanted.items()}, submit=live,
                                    max_total=limit, request_id=request_id)
        finally:
            await o.close()
        if r.status in ("placed", "unknown"):
            status = r.status
        elif r.ok and not live:
            status = "dry_run_ok"
        else:
            status = "rejected"
        return OrderOutcome(status=status, order_number=r.order_number, total=r.total,
                            got={str(k): int(v) for k, v in wanted.items()} if status != "rejected" else {},
                            problems=list(r.problems))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()


class LibertiSupplier:
    def __init__(self, login: str, password: str, account: str) -> None:
        self.login, self.password, self.account = login, password, account
        self._client = None

    def _lc(self):
        if self._client is None:
            from liberti_client import LibertiClient
            self._client = LibertiClient(self.login, self.password)
        return self._client

    async def quote(self, text: str) -> list[Candidate]:
        lc = self._lc()
        items = await lc.search(text, limit=MAX_CANDIDATES)
        out = []
        for it in items:
            await lc.mitino(it)
            out.append(Candidate(product_id=str(it.id), name=it.name, price=it.opt, mitino=it.mitino,
                                 url=it.url, article=it.code))
        out.sort(key=lambda c: (0 if (c.mitino or 0) > 0 else 1, c.price or 10**9))
        if out and (out[0].mitino or 0) > 0:
            out[0].recommended = True
        return out

    async def order(self, wanted, expected_total, request_id, comment, live, max_rub) -> OrderOutcome:
        lc = self._lc()
        phone = os.getenv("LIBERTI_PHONE", "").strip()
        if not phone:
            return OrderOutcome(status="rejected", problems=["нет LIBERTI_PHONE в .env — не оформляю"])
        if await lc.basket_ids():
            return OrderOutcome(status="rejected",
                                problems=["в корзине Liberti уже есть товары — очисти корзину на сайте"])
        expected = {int(k): int(v) for k, v in wanted.items()}
        if not live:
            # у Liberti нет очистки корзины через сайт-API: в пробном режиме в корзину не кладём вовсе
            return OrderOutcome(status="dry_run_ok", total=float(expected_total),
                                got={str(k): v for k, v in expected.items()},
                                problems=["пробно: вход и наличие проверены, в корзину Liberti не клал"])
        limit = min(float(max_rub), float(expected_total or max_rub))
        try:
            for pid, q in expected.items():
                await lc.add(pid, q)
            res = await lc.checkout(expected, phone, comment[:250], live, limit)
        except Exception as exc:  # noqa: BLE001
            res = None
            problems = [f"ошибка: {exc}"]
        if res is None or res.status not in ("placed", "unknown", "dry_run_ok"):
            (problems if res is None else res.problems).append(
                "товары остались в корзине Liberti — очисти её на сайте перед следующим заказом")
        if res is None:
            return OrderOutcome(status="rejected", problems=problems)
        status = res.status if res.status in ("placed", "unknown", "dry_run_ok") else "rejected"
        return OrderOutcome(status=status, order_number=res.order_number, total=res.total,
                            got={str(k): v for k, v in expected.items()} if status != "rejected" else {},
                            problems=list(res.problems))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()


def make_supplier(code: str, login: str, password: str, account: str) -> Supplier:
    return {"moba": MobaSupplier, "mm": MasterMobileSupplier, "liberti": LibertiSupplier}[code](login, password, account)


# ---------------------------------------------------------------------- #
# Журнал заказов (защита от двойного оформления)
# ---------------------------------------------------------------------- #
def journal_get(request_id: str) -> Optional[dict]:
    if not JOURNAL.exists():
        return None
    found = None
    for line in JOURNAL.read_text("utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("request_id") == request_id:
            found = row
    return found


def journal_put(row: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------- #
# HTTP
# ---------------------------------------------------------------------- #
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class App:
    def __init__(self, token: str, factory=make_supplier, accounts: Optional[dict] = None,
                 live: Optional[bool] = None, max_rub: Optional[int] = None) -> None:
        self.token = token
        self.factory = factory
        self.accounts = accounts if accounts is not None else accounts_from_env()
        self.live = live if live is not None else os.getenv("ZAKUPKA_LIVE", "0") == "1"
        self.max_rub = max_rub if max_rub is not None else int(os.getenv("ZAKUPKA_MAX_RUB", "30000") or 30000)
        self.locks: dict[str, asyncio.Lock] = {}

    def build(self) -> web.Application:
        app = web.Application(middlewares=[self._auth], client_max_size=256 * 1024)
        app.router.add_get("/health", self.health)
        app.router.add_get("/config", self.config)
        app.router.add_post("/quote", self.quote)
        app.router.add_post("/order", self.order)
        return app

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        if request.path != "/health" and request.headers.get("X-Zakupka-Token") != self.token:
            return web.json_response({"error": "нет доступа"}, status=401)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            log.exception("ошибка запроса %s", request.path)
            return web.json_response({"error": f"ошибка сервиса закупок: {exc}"}, status=502)

    def _supplier(self, body: dict) -> tuple[str, str, Supplier]:
        sup = DOSKA_SUPPLIER.get(str(body.get("supplier") or ""), str(body.get("supplier") or ""))
        acc = str(body.get("account") or "anri")
        creds = (self.accounts.get(sup) or {}).get(acc)
        if not creds:
            raise ValueError(f"нет аккаунта {ACCOUNT_NAMES.get(acc, acc)} для {SUPPLIER_NAMES.get(sup, sup)} в .env")
        return sup, acc, self.factory(sup, creds[0], creds[1], acc)

    async def health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def config(self, _request: web.Request) -> web.Response:
        suppliers = [
            {"id": sup, "doska": next(k for k, v in DOSKA_SUPPLIER.items() if v == sup), "name": SUPPLIER_NAMES[sup],
             "accounts": [{"id": a, "name": ACCOUNT_NAMES.get(a, a)} for a in accs]}
            for sup, accs in self.accounts.items()
        ]
        return web.json_response({"live": self.live, "max_rub": self.max_rub, "suppliers": suppliers})

    async def quote(self, request: web.Request) -> web.Response:
        body = await request.json()
        items = body.get("items") or []
        if not isinstance(items, list) or not items:
            raise ValueError("нет позиций")
        if len(items) > MAX_ITEMS:
            raise ValueError(f"за раз не больше {MAX_ITEMS} позиций")
        _sup, _acc, supplier = self._supplier(body)
        sem = asyncio.Semaphore(3)

        async def one(it: dict) -> dict:
            text = str(it.get("text") or "").strip()[:200]
            out: dict[str, Any] = {"id": it.get("id"), "text": text, "candidates": [], "problem": None}
            if len(text) < 3:
                out["problem"] = "пустой текст"
                return out
            async with sem:
                try:
                    out["candidates"] = [asdict(c) for c in await supplier.quote(text)]
                except Exception as exc:  # noqa: BLE001
                    log.warning("quote %r: %s", text, exc)
                    out["problem"] = f"поиск не удался: {exc}"
            if not out["candidates"] and not out["problem"]:
                out["problem"] = "у поставщика не нашлось"
            return out

        try:
            result = await asyncio.gather(*(one(it) for it in items))
        finally:
            await supplier.close()
        return web.json_response({"items": result, "live": self.live})

    async def order(self, request: web.Request) -> web.Response:
        body = await request.json()
        request_id = str(body.get("request_id") or "")
        if not REQUEST_ID_RE.match(request_id):
            raise ValueError("нужен request_id")
        done = journal_get(request_id)
        if done and done.get("status") in ("placed", "unknown", "dry_run_ok", "rejected"):
            return web.json_response({**done, "repeat": True})
        lines = body.get("lines") or []
        wanted: "OrderedDict[str, int]" = OrderedDict()
        expected = 0
        for ln in lines:
            pid = str(ln.get("product_id") or "").strip()
            qty = int(ln.get("qty") or 0)
            if not pid.isdigit() or qty < 1 or qty > 50:
                raise ValueError("неверная позиция заказа")
            wanted[pid] = wanted.get(pid, 0) + qty
            expected += int(ln.get("price") or 0) * qty
        if not wanted:
            raise ValueError("нет позиций для заказа")
        if expected > self.max_rub:
            raise ValueError(f"сумма {expected} ₽ больше лимита {self.max_rub} ₽ (ZAKUPKA_MAX_RUB в .env)")
        sup, acc, supplier = self._supplier(body)
        comment = str(body.get("comment") or "Закупка iBlackMaster")[:250]
        lock = self.locks.setdefault(f"{sup}:{acc}", asyncio.Lock())
        if lock.locked():
            await supplier.close()
            raise ValueError(f"{SUPPLIER_NAMES[sup]}: уже оформляется другой заказ, подождите минуту")
        async with lock:
            journal_put({"request_id": request_id, "status": "started", "supplier": sup, "account": acc,
                         "wanted": wanted, "ts": time.time()})
            try:
                res = await supplier.order(wanted, expected, request_id, comment, self.live, self.max_rub)
            except Exception as exc:  # noqa: BLE001
                log.exception("order %s", request_id)
                # сбой посреди оформления: заказ МОГ уйти — пусть человек проверит сайт
                res = OrderOutcome(status="unknown", problems=[f"сбой во время оформления: {exc}"])
            finally:
                await supplier.close()
            row = {"request_id": request_id, "supplier": sup, "account": acc, "live": self.live,
                   "expected": expected, "ts": time.time(), **asdict(res)}
            journal_put(row)
        return web.json_response(row)


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    token = os.getenv("ZAKUPKA_TOKEN", "").strip()
    if len(token) < 24:
        raise SystemExit("Задайте ZAKUPKA_TOKEN в .env (openssl rand -hex 24)")
    app = App(token)
    log.info("zakupka_api: магазины %s, режим %s", {k: list(v) for k, v in app.accounts.items()},
             "БОЕВОЙ" if app.live else "пробный")
    web.run_app(app.build(), host=os.getenv("ZAKUPKA_HOST", "0.0.0.0"), port=int(os.getenv("ZAKUPKA_PORT", "8095")))


if __name__ == "__main__":
    main()
