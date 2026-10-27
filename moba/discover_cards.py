"""Разведка карточек WB (только чтение): какие ошибки, какие предметы, какие характеристики у предметов.

Для каждого магазина:
  * список карточек с ошибками (content/v2/cards/error/list) — счётчик по тексту ошибки;
  * все карточки (content/v2/get/cards/list) — предметы и сколько карточек без габаритов / страны / ТН ВЭД;
  * для самых частых предметов — справочник характеристик и коды ТН ВЭД;
  * образцы карточек — в data/cards_disc/.
Запуск: venv/bin/python discover_cards.py [--shops shop2,shop3,shop4,gor]
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from price_plan import SHOPS, _retry_after

API = "https://content-api.wildberries.ru"
OUT = Path("data/cards_disc")


async def call(c: httpx.AsyncClient, method: str, url: str, token: str, **kw) -> httpx.Response:
    while True:
        r = await c.request(method, API + url, headers={"Authorization": token}, **kw)
        if r.status_code == 429:
            w = _retry_after(r)
            print(f"   429 — жду {w} с", flush=True)
            await asyncio.sleep(w)
            continue
        await asyncio.sleep(0.7)
        return r


async def all_cards(c, token) -> list[dict]:
    out, cursor = [], {"limit": 100}
    while True:
        r = await call(c, "POST", "/content/v2/get/cards/list", token,
                       json={"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}})
        r.raise_for_status()
        d = r.json()
        out += d.get("cards") or []
        cur = d.get("cursor") or {}
        if (cur.get("total") or 0) < 100 or not d.get("cards"):
            return out
        cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shops", default="shop2,shop3,shop4,gor")
    args = ap.parse_args()
    load_dotenv(".env")
    OUT.mkdir(parents=True, exist_ok=True)
    subj_total: collections.Counter = collections.Counter()
    subj_names: dict[int, str] = {}
    async with httpx.AsyncClient(timeout=60) as c:
        for shop in SHOPS:
            if shop["key"] not in args.shops.split(","):
                continue
            tok = os.getenv(shop["env"], "").strip()
            print(f"\n===== {shop['title']}", flush=True)
            # ошибки
            for method, url in (("GET", "/content/v2/cards/error/list"), ("POST", "/content/v2/cards/error/list")):
                r = await call(c, method, url, tok, params={"locale": "ru"})
                if r.status_code == 200:
                    break
            errs = collections.Counter()
            if r.status_code == 200:
                data = r.json().get("data") or []
                (OUT / f"errors_{shop['key']}.json").write_text(json.dumps(data, ensure_ascii=False), "utf-8")
                for e in data:
                    for t in e.get("errors") or []:
                        errs[str(t)[:120]] += 1
                print(f"карточек с ошибками загрузки: {len(data)}")
            else:
                print("ошибки:", r.status_code, r.text[:200])
            for t, n in errs.most_common(15):
                print(f"   {n:>6} · {t}")
            # все карточки
            cards = await all_cards(c, tok)
            (OUT / f"cards_{shop['key']}.json").write_text(json.dumps(cards, ensure_ascii=False), "utf-8")
            no_dim = no_country = 0
            ch_missing = collections.Counter()
            for cd in cards:
                sid = cd.get("subjectID")
                subj_total[sid] += 1
                subj_names[sid] = cd.get("subjectName") or ""
                dm = cd.get("dimensions") or {}
                if not dm.get("length") or not dm.get("weightBrutto"):
                    no_dim += 1
                names = {x.get("name") for x in cd.get("characteristics") or []}
                if "Страна производства" not in names:
                    no_country += 1
                for nm in ("ТНВЭД", "Код ТН ВЭД", "Модель", "Совместимость", "Диагональ экрана", "Наличие рамки",
                           "Тип аккумулятора", "Емкость аккумулятора", "Комплектация"):
                    if nm not in names:
                        ch_missing[nm] += 1
            print(f"карточек всего {len(cards)} · без габаритов/веса {no_dim} · без страны {no_country}")
            print("   нет характеристики:", dict(ch_missing))
    print("\n===== предметы (все магазины)")
    for sid, n in subj_total.most_common(15):
        print(f"   {sid} {subj_names[sid]}: {n}")
    tok = os.getenv("WB_SHOP2", "").strip()
    async with httpx.AsyncClient(timeout=60) as c:
        for sid, _n in subj_total.most_common(8):
            r = await call(c, "GET", f"/content/v2/object/charcs/{sid}", tok, params={"locale": "ru"})
            ch = (r.json().get("data") or []) if r.status_code == 200 else []
            (OUT / f"charcs_{sid}.json").write_text(json.dumps(ch, ensure_ascii=False), "utf-8")
            r2 = await call(c, "GET", "/content/v2/directory/tnved", tok, params={"subjectID": sid, "locale": "ru"})
            tn = (r2.json().get("data") or []) if r2.status_code == 200 else r2.text[:200]
            (OUT / f"tnved_{sid}.json").write_text(json.dumps(tn, ensure_ascii=False), "utf-8")
            print(f"\n--- {sid} {subj_names[sid]}: характеристик {len(ch)}; ТН ВЭД: {tn if isinstance(tn, str) else tn[:3]}")
            for x in ch:
                print(f"   {x.get('charcID')} · {x.get('name')} · обяз={x.get('required')} · ед={x.get('unitName')} "
                      f"· макс={x.get('maxCount')} · тип={x.get('charcType')}")
    print("\nсохранено в", OUT)


if __name__ == "__main__":
    asyncio.run(main())
