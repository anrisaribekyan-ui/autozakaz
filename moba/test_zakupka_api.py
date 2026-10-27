"""zakupka_api: токен, config, подбор, защита от двойного заказа, пробный режим, лимит. Без сети — фейки."""
import asyncio

from aiohttp.test_utils import TestClient, TestServer

import zakupka_api as Z

TOKEN = "t" * 32
H = {"X-Zakupka-Token": TOKEN}


class FakeSupplier:
    orders = []

    def __init__(self, code, login, password, account):
        self.code, self.account = code, account

    async def quote(self, text):
        if "нет" in text:
            return []
        return [Z.Candidate(product_id="111", name=f"{text} копия", price=500, mitino=0),
                Z.Candidate(product_id="222", name=f"{text} оригинал", price=900, mitino=3, recommended=True)]

    async def order(self, wanted, expected_total, request_id, comment, live, max_rub):
        FakeSupplier.orders.append((self.code, self.account, dict(wanted), expected_total, live))
        await asyncio.sleep(0.05)
        return Z.OrderOutcome(status="placed" if live else "dry_run_ok", order_number="N1" if live else None,
                              total=expected_total, got=dict(wanted))

    async def close(self):
        pass


ACCOUNTS = {"moba": {"anri": ("l", "p")}, "mm": {"anri": ("l", "p")}}


def run(coro):
    return asyncio.run(coro)


async def client(tmp_path, monkeypatch, live=False):
    monkeypatch.setattr(Z, "DATA_DIR", tmp_path)
    monkeypatch.setattr(Z, "JOURNAL", tmp_path / "j.jsonl")
    FakeSupplier.orders = []
    app = Z.App(TOKEN, factory=FakeSupplier, accounts=ACCOUNTS, live=live, max_rub=5000)
    c = TestClient(TestServer(app.build()))
    await c.start_server()
    return c


def test_token_and_config(tmp_path, monkeypatch):
    async def go():
        c = await client(tmp_path, monkeypatch)
        try:
            assert (await c.get("/config")).status == 401
            assert (await c.get("/health")).status == 200
            cfg = await (await c.get("/config", headers=H)).json()
            assert cfg["live"] is False
            ids = {s["id"]: s for s in cfg["suppliers"]}
            assert set(ids) == {"moba", "mm"} and ids["mm"]["doska"] == "master"
            assert ids["moba"]["accounts"] == [{"id": "anri", "name": "Анри"}]
        finally:
            await c.close()
    run(go())


def test_quote_maps_doska_supplier_and_reports_misses(tmp_path, monkeypatch):
    async def go():
        c = await client(tmp_path, monkeypatch)
        try:
            r = await c.post("/quote", json={"supplier": "master", "items": [
                {"id": "a1", "text": "дисплей realme c30"}, {"id": "a2", "text": "нет такого"}]}, headers=H)
            body = await r.json()
            assert r.status == 200
            a1, a2 = body["items"]
            assert a1["id"] == "a1" and a1["candidates"][1]["mitino"] == 3
            assert a2["problem"] == "у поставщика не нашлось"
            r = await c.post("/quote", json={"supplier": "liberti", "items": [{"id": "x", "text": "акб"}]}, headers=H)
            assert r.status == 400 and "нет аккаунта" in (await r.json())["error"]
        finally:
            await c.close()
    run(go())


def test_order_dry_run_then_repeat_is_not_reordered(tmp_path, monkeypatch):
    async def go():
        c = await client(tmp_path, monkeypatch)
        try:
            body = {"supplier": "moba", "account": "anri", "request_id": "req-0001-abc",
                    "lines": [{"product_id": "222", "qty": 1, "price": 900}, {"product_id": "222", "qty": 1, "price": 900}]}
            r1 = await (await c.post("/order", json=body, headers=H)).json()
            assert r1["status"] == "dry_run_ok" and r1["got"] == {"222": 2} and r1["expected"] == 1800
            r2 = await (await c.post("/order", json=body, headers=H)).json()
            assert r2["repeat"] is True and len(FakeSupplier.orders) == 1
        finally:
            await c.close()
    run(go())


def test_live_order_limit_and_parallel_lock(tmp_path, monkeypatch):
    async def go():
        c = await client(tmp_path, monkeypatch, live=True)
        try:
            big = {"supplier": "moba", "request_id": "req-big-00001", "lines": [{"product_id": "1", "qty": 6, "price": 1000}]}
            r = await c.post("/order", json=big, headers=H)
            assert r.status == 400 and "лимита" in (await r.json())["error"]
            bad = {"supplier": "moba", "request_id": "req-bad-00001", "lines": [{"product_id": "x1", "qty": 1}]}
            assert (await c.post("/order", json=bad, headers=H)).status == 400
            a = {"supplier": "moba", "request_id": "req-par-00001", "lines": [{"product_id": "5", "qty": 1, "price": 100}]}
            b = {**a, "request_id": "req-par-00002"}
            ra, rb = await asyncio.gather(c.post("/order", json=a, headers=H), c.post("/order", json=b, headers=H))
            codes = sorted([ra.status, rb.status])
            assert codes == [200, 400]
            ok = await (ra if ra.status == 200 else rb).json()
            assert ok["status"] == "placed" and ok["order_number"] == "N1" and ok["live"] is True
        finally:
            await c.close()
    run(go())


def test_crash_mid_order_is_unknown(tmp_path, monkeypatch):
    class Boom(FakeSupplier):
        async def order(self, *a, **k):
            raise RuntimeError("браузер упал")

    async def go():
        monkeypatch.setattr(Z, "DATA_DIR", tmp_path)
        monkeypatch.setattr(Z, "JOURNAL", tmp_path / "j.jsonl")
        app = Z.App(TOKEN, factory=Boom, accounts=ACCOUNTS, live=True, max_rub=5000)
        c = TestClient(TestServer(app.build()))
        await c.start_server()
        try:
            r = await (await c.post("/order", json={"supplier": "moba", "request_id": "req-boom-0001",
                                                     "lines": [{"product_id": "5", "qty": 1, "price": 100}]}, headers=H)).json()
            assert r["status"] == "unknown" and "МОГ" not in r["problems"][0]
        finally:
            await c.close()
    run(go())


def test_accounts_from_env(monkeypatch):
    for k in ("MOBA_LOGIN", "MOBA_PASSWORD", "MOBA_GOR_LOGIN", "MOBA_GOR_PASSWORD", "MM_LOGIN", "MM_PASSWORD",
              "LIBERTI_LOGIN", "LIBERTI_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MOBA_LOGIN", "a"); monkeypatch.setenv("MOBA_PASSWORD", "b")
    monkeypatch.setenv("MOBA_GOR_LOGIN", "c")  # без пароля — аккаунта нет
    assert Z.accounts_from_env() == {"moba": {"anri": ("a", "b")}}
