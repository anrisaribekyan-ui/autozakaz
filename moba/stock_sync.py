"""Остатки WB по наличию у поставщиков (своих остатков нет — покупаем под заказ).

Правила (Анри, 21.10.2026):
  * есть у нескольких поставщиков / много → остаток 5–10;
  * мало и только у одного (например, 3 шт. в Мастер Мобайле) → 1–2;
  * нет нигде → 0;
  * считается Митино, а другие точки — только если товар есть больше чем в одной точке
    (из некоторых точек moba не довезти);
  * раз в час; у каждого магазина один FBS-склад.
Предохранители: товар, для которого подбор не нашёл запчасть, НЕ трогаем (только в отчёт).
По умолчанию — пробный режим (ничего не меняет). --apply — записать остатки в WB.

Наличие — только Митино: moba — точка «Ми» в прайсе; Мастер Мобайл — живой остаток склада Митино (кэш 24 ч,
проверяется только если у moba в Митино нет или мало). Liberti/SMG по Митино пока не проверяются.

Запуск: venv/bin/python stock_sync.py [--shops shop2,shop4] [--apply] [--no-tg]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

import gor_costs as G
from matcher import is_or, is_premium
from price_plan import SHOPS, goods
from unit_econ import _prices_token, rates

MP = "https://marketplace-api.wildberries.ru"
CARDS_URL = "https://content-api.wildberries.ru/content/v2/get/cards/list"
LOG = Path("data/stock_sync.jsonl")


# ---------------- наличие у поставщиков ----------------
LIB_BATT_IDX: dict = {}
for _i, _r in enumerate(G.LIB_BATT):
    for _t in set(G._normalize(_r["name"]).split()):
        LIB_BATT_IDX.setdefault(_t, set()).add(_i)


def _all(title, rows, idx, namekey, mm=False):
    """Все подходящие позиции поставщика (как в подборе себеса, но без выбора самой дешёвой)."""
    wb = G.parse_wb_title(title)
    ids: set = set()
    for m in wb.get("models") or []:
        cur = None
        for t in G._normalize(m).split():
            cur = set(idx.get(t, ())) if cur is None else cur & set(idx.get(t, ()))
        ids |= cur or set()
    out = []
    for i in ids:
        r = rows[i]
        nm = r[namekey]
        ok, ql = G._fits(nm, wb, title)
        if not ok or not G.allowed(wb["quality"], ql) or not G.maker_ok(title, nm) or not G.spec_ok(title, nm):
            continue
        if mm and not G.mm_battery_ok(nm, wb):
            continue
        out.append(r)
    return out


def candidates(title: str, owner: str) -> dict:
    """Что подходит у поставщиков: moba (точки), Мастер Мобайл (строки прайса для проверки Митино)."""
    t = G.gor_title(title) if owner == "gor" else title
    wb = G.parse_wb_title(t)
    batt_not_moba = G.is_battery(wb) and not G.battery_tag(t)
    out = {"matched": False, "moba_mitino": False, "moba_points": 0, "mm": [], "other": [],
           "moba_or": None, "mm_or": [], "moba_rows": [], "lib": []}
    # крышка «Премиум» на WB: OR — замена получше (Анри 21.10), берём только если в плюс
    up = (wb.get("type") or "").startswith(("крышк", "корпус")) and is_premium(t)

    def _or(name: str) -> bool:
        return up and is_or(name) and not is_premium(name)

    if not batt_not_moba:
        for r in _all(t, G.ROWS, G.MOBA_IDX, "name"):
            out["matched"] = True
            pts = [p.strip() for p in (r.get("avail") or "").split(",") if p.strip()]
            if "Ми" not in pts:
                if not _or(r["name"]):
                    out["moba_rows"].append(r)   # по прайсу нет в Митино — перепроверим на сайте
                continue
            if _or(r["name"]):
                if out["moba_or"] is None or r["price"] < out["moba_or"]["price"]:
                    out["moba_or"] = {"price": r["price"], "points": len(pts)}
                continue
            out["moba_mitino"] = True
            out["moba_points"] = max(out["moba_points"], len(pts))
    for r in _all(t, G.MMP, G.MM_IDX, "name", mm=True):
        out["matched"] = True
        (out["mm_or"] if _or(r["name"]) else out["mm"]).append(r)
    lib_rows, lib_idx = (G.LIB_BATT, LIB_BATT_IDX) if G.is_battery(wb) else (G.LIB, G.LIB_IDX)
    for r in (_all(t, lib_rows, lib_idx, "name") if not (G.is_battery(wb) and G.battery_tag(t)) else []):
        out["matched"] = True
        if (r.get("qty") or 0) > 0 and r.get("code"):
            out["lib"].append(r)          # Митино проверим на сайте Liberti
        else:
            out["other"].append(f"Liberti {r.get('qty') or 0} шт. по сети (Митино не проверено)")
    if G.is_battery(wb) and G.smg_best(t):
        out["matched"] = True
        out["other"].append("SMG в прайсе (Митино не проверено)")
    return out


# ---------------- Мастер Мобайл: живой остаток в Митино (кэш 6 ч) ----------------
MM_CACHE = Path("data/mm_mitino_cache.json")
MM_TTL = 24 * 3600


MOBA_CACHE = Path("data/moba_mitino_cache.json")
MOBA_TTL = 3 * 3600


class MobaLive:
    """Живое наличие в Митино на сайте moba (прайс утренний и к вечеру устаревает — Анри 21.10).
    Проверяем только то, что по прайсу собирались обнулить."""

    def __init__(self) -> None:
        from moba_client import MobaClient
        self.c = MobaClient(os.getenv("MOBA_LOGIN", "").strip(), os.getenv("MOBA_PASSWORD", "").strip(), "anri")
        try:
            self.cache = json.loads(MOBA_CACHE.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            self.cache = {}
        self.checked = 0

    async def status(self, row: dict) -> str:
        """yes / no / unknown."""
        import time as _t
        code = str(row.get("code") or row["name"])
        hit = self.cache.get(code)
        if hit and _t.time() - hit.get("ts", 0) < MOBA_TTL:
            return hit["st"]
        url = (hit or {}).get("url")
        st = "unknown"
        try:
            if not url:
                items = await self.c.search(row["name"][:100], limit=20)
                want = row["name"].strip().lower()
                m = next((i for i in items if i["name"].strip().lower() == want), None)
                url = m["url"] if m else None
            if url:
                st = await self.c.mitino_status(url)
        except Exception:  # noqa: BLE001
            st = "unknown"
        self.checked += 1
        self.cache[code] = {"ts": _t.time(), "st": st, "url": url}
        if self.checked % 30 == 0:
            self.save()
        return st

    def save(self) -> None:
        MOBA_CACHE.parent.mkdir(exist_ok=True)
        MOBA_CACHE.write_text(json.dumps(self.cache, ensure_ascii=False), "utf-8")

    async def close(self) -> None:
        self.save()
        await self.c.close()


class MMMitino:
    def __init__(self) -> None:
        from suppliers_ext import MasterMobileClient
        self.c = MasterMobileClient()
        try:
            self.cache = json.loads(MM_CACHE.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            self.cache = {}
        self.checked = 0

    async def qty(self, row: dict) -> int | None:
        import time as _t
        code = str(row.get("code"))
        hit = self.cache.get(code)
        if hit and _t.time() - hit.get("ts", 0) < MM_TTL:
            return hit.get("qty")
        pid, url = (hit or {}).get("pid"), (hit or {}).get("url")
        if not pid:
            try:
                items = await self.c.search(row["name"][:90])
            except Exception:  # noqa: BLE001
                items = []
            m = next((i for i in items if str(i.get("article")) == code), None) or \
                next((i for i in items if i["name"].strip().lower() == row["name"].strip().lower()), None)
            if not m:
                self.cache[code] = {"ts": _t.time(), "qty": None}
                return None
            pid, url = m["product_id"], m.get("url")
        q = await self.c.mitino_amount(pid, url)
        self.checked += 1
        self.cache[code] = {"ts": _t.time(), "qty": q, "pid": pid, "url": url}
        if self.checked % 50 == 0:
            self.save()
        return q

    def save(self) -> None:
        MM_CACHE.parent.mkdir(exist_ok=True)
        MM_CACHE.write_text(json.dumps(self.cache, ensure_ascii=False), "utf-8")

    async def close(self) -> None:
        self.save()
        await self.c.close()


LIB_CACHE = Path("data/liberti_mitino_cache.json")
LIB_TTL = 12 * 3600
LIB_SNAP = Path("data/liberti_mitino.json")


class LibLive:
    """Остаток «Либерти - Митино» с сайта (кэш 12 ч). Только чтение, корзину не трогает."""

    def __init__(self) -> None:
        from liberti_client import LibertiClient
        self.c = LibertiClient(os.getenv("LIBERTI_LOGIN", "").strip(), os.getenv("LIBERTI_PASSWORD", "").strip())
        try:
            self.cache = json.loads(LIB_CACHE.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            self.cache = {}
        self.checked = 0

    snap: dict | None = None

    async def _snapshot(self) -> dict:
        """Весь каталог запчастей страницами — остаток Митино (свежий файл < 1 ч — берём его)."""
        import time as _t
        if self.snap is not None:
            return self.snap
        try:
            d = json.loads(LIB_SNAP.read_text("utf-8"))
            if _t.time() - d.get("ts", 0) < 3600:
                self.snap = d["items"]
                return self.snap
        except Exception:  # noqa: BLE001
            pass
        try:
            items = await self.c.mitino_snapshot()
            LIB_SNAP.write_text(json.dumps({"ts": _t.time(), "items": items}, ensure_ascii=False), "utf-8")
            self.snap = items
            print(f"   Liberti: каталог Митино — {len(items)} позиций", flush=True)
        except Exception as exc:  # noqa: BLE001
            print("   Liberti: каталог не выгрузился —", exc, flush=True)
            self.snap = {}
        return self.snap

    async def qty(self, code: str) -> int | None:
        import time as _t
        snap = await self._snapshot()
        if code in snap:
            return int(snap[code].get("mitino") or 0)
        hit = self.cache.get(code)
        if hit and _t.time() - hit.get("ts", 0) < LIB_TTL:
            return hit.get("qty")
        q = None
        try:
            it = await self.c.search_code(code)
            if it is None:
                q = 0
            else:
                await self.c.mitino(it)
                q = it.mitino
        except Exception:  # noqa: BLE001
            q = None
        self.checked += 1
        if q is not None:
            self.cache[code] = {"ts": _t.time(), "qty": q}
        if self.checked % 30 == 0:
            self.save()
        return q

    def save(self) -> None:
        LIB_CACHE.parent.mkdir(exist_ok=True)
        LIB_CACHE.write_text(json.dumps(self.cache, ensure_ascii=False), "utf-8")

    async def close(self) -> None:
        self.save()
        await self.c.close()


def sale_from_plan(shop_title: str) -> dict[int, float]:
    """Наши цены из последней таблицы price_plan (новая цена, если её записали) — без запросов в WB."""
    import openpyxl
    files = sorted(Path("data").glob("price_plan_*.xlsx"))
    if not files:
        return {}
    try:
        wb = openpyxl.load_workbook(files[-1], read_only=True)
        if shop_title not in wb.sheetnames:
            return {}
        rows = wb[shop_title].iter_rows(values_only=True)
        h = {n: i for i, n in enumerate(next(rows))}
        out = {}
        for r in rows:
            v = r[h.get("Станет (после скидки)", -1)] if "Станет (после скидки)" in h else None
            v = v or r[h["Наша цена сейчас"]]
            if v:
                out[int(r[h["nmID"]])] = float(v)
        return out
    except Exception:  # noqa: BLE001
        return {}


def profit_ok(sale: float | None, cost: float, shop: str) -> bool | None:
    """В плюс ли товар при текущей цене WB (None — цены нет)."""
    if not sale:
        return None
    r = rates(shop)
    return sale * (1 - (r["wb"] + r["ads"] + r["tax"]) / 100) - cost - r["pack"] >= 0


async def availability(title: str, owner: str, mmm: "MMMitino", sale: float | None = None,
                       ml: "MobaLive | None" = None, ll: "LibLive | None" = None) -> dict:
    """Наличие ИМЕННО в Митино (Анри, 21.10): moba — точка «Ми» в прайсе; Мастер Мобайл — живой остаток склада 10.
    Liberti и SMG по Митино пока не проверяются — в остаток не идут, только в пометку."""
    cd = candidates(title, owner)
    src: list[tuple[str, int, str]] = []
    if cd["moba_mitino"]:
        src.append(("moba", 2 if cd["moba_points"] >= 3 else 1, f"Митино ({cd['moba_points']} точ.)"))
    best_mm = None
    moba_strong = cd["moba_mitino"] and cd["moba_points"] >= 3
    for r in ([] if moba_strong else sorted(cd["mm"], key=lambda r: r["price"])[:3]):  # до 3 позиций MM
        q = await mmm.qty(r)
        if q and (best_mm is None or q > best_mm):
            best_mm = q
    if best_mm:
        src.append(("Мастер Мобайл", 2 if best_mm >= 5 else 1, f"Митино {best_mm} шт."))
    notes, undecided = list(cd["other"]), False
    if not src and cd["moba_rows"] and ml is not None:
        # по прайсу в Митино нет — перед обнулением смотрим сайт moba (до 3 самых дешёвых позиций)
        sts = []
        for r in sorted(cd["moba_rows"], key=lambda r: r["price"])[:3]:
            st = await ml.status(r)
            sts.append(st)
            if st == "yes":
                src.append(("moba", 1, "Митино (по сайту)"))
                break
        if not src and "unknown" in sts:
            undecided = True
            notes.append("moba: сайт не ответил — не трогаю")
    lib_q = 0
    if not src and cd["lib"]:
        if ll is None:
            notes.append("Liberti по сети есть (Митино не проверено)")
        else:
            unk = False
            for r in sorted(cd["lib"], key=lambda r: r.get("opt") or 0)[:2]:
                q = await ll.qty(str(r["code"]))
                if q is None:
                    unk = True
                elif q > lib_q:
                    lib_q = q
            if lib_q:
                src.append(("Liberti", 2 if lib_q >= 5 else 1, f"Митино {lib_q} шт."))
            elif unk:
                undecided = True
                notes.append("Liberti: сайт не ответил — не трогаю")
    if not src and (cd["moba_or"] or cd["mm_or"]):
        # премиума в Митино нет — смотрим OR, если продажа в плюс
        or_src = None
        if cd["moba_or"]:
            o = cd["moba_or"]
            or_src = ("moba", 2 if o["points"] >= 3 else 1, f"Митино OR вместо премиума ({o['points']} точ.)", o["price"])
        else:
            for r in sorted(cd["mm_or"], key=lambda r: r["price"])[:2]:
                q = await mmm.qty(r)
                if q:
                    or_src = ("Мастер Мобайл", 2 if q >= 5 else 1, f"Митино OR вместо премиума {q} шт.", r["price"])
                    best_mm = q
                    break
        if or_src:
            ok = profit_ok(sale, or_src[3], owner)
            if ok:
                src.append(or_src[:3])
            elif ok is None:
                undecided = True
                notes.append(f"OR в Митино за {or_src[3]:g} ₽, цены WB нет — не трогаю")
            else:
                notes.append(f"OR в Митино за {or_src[3]:g} ₽ — в минус при текущей цене")
    if not best_mm and lib_q:
        best_mm = lib_q   # мало у единственного поставщика → 1 шт.
    return {"matched": cd["matched"], "sources": src, "mm_qty": best_mm or 0, "notes": notes,
            "undecided": undecided}


def target_stock(av: dict) -> int | None:
    """None — не трогать (подбор не нашёл запчасть)."""
    if not av["matched"]:
        return None
    s = av["sources"]
    if not s:
        # Нет в Митино у moba/MM, но есть у Liberti/SMG — их Митино пока не проверяем, поэтому не обнуляем.
        return None if (av.get("undecided") or any("не проверено" in n for n in av.get("notes") or [])) else 0
    strong = sum(1 for x in s if x[1] >= 2)
    if len(s) >= 2 and strong >= 1:
        return 10
    if strong:
        return 5
    if av.get("mm_qty", 0) and av["mm_qty"] <= 2 and len(s) == 1:
        return 1
    return 2


# ---------------- WB ----------------
async def cards(c: httpx.AsyncClient, token: str) -> list[dict]:
    out, cursor = [], {"limit": 100}
    while True:
        r = await c.post(CARDS_URL, json={"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}},
                         headers={"Authorization": token})
        if r.status_code == 429:
            await asyncio.sleep(10)
            continue
        r.raise_for_status()
        d = r.json()
        for card in d.get("cards") or []:
            skus = [s for size in card.get("sizes") or [] for s in size.get("skus") or []]
            out.append({"nm": card["nmID"], "title": card.get("title") or "", "vendor": card.get("vendorCode"),
                        "skus": skus})
        cur = d.get("cursor") or {}
        if (cur.get("total") or 0) < 100:
            return out
        cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
        await asyncio.sleep(0.7)


async def warehouse(c: httpx.AsyncClient, token: str) -> int:
    r = await c.get(MP + "/api/v3/warehouses", headers={"Authorization": token})
    r.raise_for_status()
    whs = r.json()
    fbs = [w for w in whs if "dbs" not in str(w.get("name", "")).lower() and w.get("deliveryType") in (None, 1)]
    pick = (fbs or whs)[0]
    if len(whs) != 1:
        print("   складов:", [(w.get("id"), w.get("name"), w.get("deliveryType")) for w in whs], "→", pick.get("name"))
    return int(pick["id"])


async def get_stocks(c, token, wh, skus) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in range(0, len(skus), 1000):
        r = await c.post(f"{MP}/api/v3/stocks/{wh}", json={"skus": skus[i:i + 1000]},
                         headers={"Authorization": token})
        r.raise_for_status()
        for s in r.json().get("stocks") or []:
            out[s["sku"]] = int(s.get("amount") or 0)
        await asyncio.sleep(0.3)
    return out


async def put_stocks(c, token, wh, items: list[dict]) -> list[str]:
    """Пишет остатки пачками. 409 по части товаров (например, КГТ «ODC» не для этого склада) — убирает их
    из пачки и повторяет; возвращает список пропущенных с причиной (22.10, магазин Бороды)."""
    skipped: list[str] = []
    for i in range(0, len(items), 1000):
        part = items[i:i + 1000]
        for _attempt in range(5):
            r = await c.put(f"{MP}/api/v3/stocks/{wh}", json={"stocks": part}, headers={"Authorization": token})
            if r.status_code in (200, 204):
                break
            if r.status_code == 429:
                await asyncio.sleep(5)
                continue
            bad: set[str] = set()
            try:
                for e in r.json() or []:
                    for x in e.get("data") or []:
                        if x.get("sku"):
                            bad.add(str(x["sku"]))
                            skipped.append(f"{x['sku']}: {e.get('code')}")
            except Exception:  # noqa: BLE001
                pass
            if not bad:
                raise RuntimeError(f"WB stocks PUT {r.status_code}: {r.text[:300]}")
            part = [x for x in part if str(x["sku"]) not in bad]
            if not part:
                break
        await asyncio.sleep(0.3)
    return skipped


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shops", default=",".join(s["key"] for s in SHOPS))
    ap.add_argument("--apply", action="store_true", help="записать остатки в WB (иначе — пробно)")
    ap.add_argument("--no-tg", action="store_true")
    args = ap.parse_args()
    load_dotenv(".env")
    live = args.apply and os.getenv("STOCK_SYNC_LIVE", "0") == "1"
    want = set(args.shops.split(","))
    report = [f"📦 <b>Остатки WB{' ' if live else ' (пробно)'}</b> · {datetime.now():%d.%m %H:%M}"]
    rows_x = []
    mmm = MMMitino()
    ml = MobaLive() if os.getenv("MOBA_LOGIN") else None
    ll = LibLive() if os.getenv("LIBERTI_LOGIN") else None
    async with httpx.AsyncClient(timeout=40) as c:
        for shop in SHOPS:
            if shop["key"] not in want:
                continue
            tok = os.getenv(shop["env"], "").strip()
            if not tok:
                continue
            try:
                cl = await cards(c, tok)
                wh = await warehouse(c, tok)
                cur = await get_stocks(c, tok, wh, [s for x in cl for s in x["skus"]])
                # 22.10: цены — из таблицы price_plan (раздел WB «Цены и скидки» на строгом лимите)
                sale = sale_from_plan(shop["title"])
            except httpx.HTTPStatusError as exc:
                report.append(f"⚠️ {shop['title']}: WB {exc.response.status_code} — нет доступа (токен Маркетплейс/Контент?)")
                continue
            changes, unknown, zeroed, raised = [], 0, 0, 0
            for x in cl:
                if not x["skus"]:
                    continue
                av = await availability(x["title"], shop["key"], mmm, sale.get(int(x["nm"])), ml, ll)
                tgt = target_stock(av)
                for sku in x["skus"]:
                    now = cur.get(sku, 0)
                    rows_x.append({"shop": shop["title"], "nm": x["nm"], "title": x["title"], "sku": sku,
                                   "now": now, "target": tgt,
                                   "sources": "; ".join([f"{a}: {t}" for a, _s, t in av["sources"]] + av["notes"])
                                   or "нет"})
                    if tgt is None:
                        unknown += 1
                        continue
                    if tgt != now:
                        changes.append({"sku": sku, "amount": tgt})
                        if tgt == 0:
                            zeroed += 1
                        elif now == 0:
                            raised += 1
            if live and changes:
                skipped = await put_stocks(c, tok, wh, changes)
                if skipped:
                    report.append(f"⚠️ {shop['title']}: WB не принял {len(skipped)} (например {skipped[0]}) — "
                                  f"для таких нужен отдельный склад")
                with LOG.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": datetime.now().isoformat(), "shop": shop["key"], "wh": wh,
                                        "changes": changes}, ensure_ascii=False) + "\n")
            report.append(f"• <b>{shop['title']}</b>: товаров {len(cl)} · изменить {len(changes)} "
                          f"(обнулить {zeroed}, вернуть {raised}) · не определил {unknown}")
    await mmm.close()
    if ml is not None:
        await ml.close()
    if ll is not None:
        await ll.close()
    out = Path("data") / f"stock_sync_{datetime.now():%Y%m%d_%H%M}.json"
    out.write_text(json.dumps(rows_x, ensure_ascii=False, indent=0), "utf-8")
    xlsx = out.with_suffix(".xlsx")
    try:
        import openpyxl
        wbk = openpyxl.Workbook()
        ws = wbk.active
        ws.append(["Магазин", "nmID", "Название", "Баркод", "Сейчас", "Станет", "Наличие у поставщиков"])
        for r in rows_x:
            ws.append([r["shop"], r["nm"], r["title"], r["sku"], r["now"],
                       "не трогаю" if r["target"] is None else r["target"], r["sources"]])
        ws.auto_filter.ref = ws.dimensions
        for col, w in zip("ABCDEFG", (10, 12, 60, 16, 8, 10, 70)):
            ws.column_dimensions[col].width = w
        wbk.save(xlsx)
    except Exception:  # noqa: BLE001
        xlsx = None
    text = "\n".join(report)
    print(text.replace("<b>", "").replace("</b>", ""))
    if not args.no_tg:
        from test_sborka import tg_send
        await tg_send(text, [xlsx] if xlsx else [], "BOT_MAIN", "TG_ANRI_ID", parse_mode="HTML")


if __name__ == "__main__":
    asyncio.run(main())
