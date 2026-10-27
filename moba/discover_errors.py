"""Ошибки карточек WB (только чтение): пробует варианты запроса списка ошибок, печатает частые тексты.
Плюс полный список ТН ВЭД для дисплеев и справочник стран. Запуск: venv/bin/python discover_errors.py"""
import asyncio
import collections
import json
import os

import httpx
from dotenv import load_dotenv

from discover_cards import API, OUT, call
from price_plan import SHOPS

load_dotenv(".env")


async def main():
    async with httpx.AsyncClient(timeout=60) as c:
        for shop in SHOPS:
            tok = os.getenv(shop["env"], "").strip()
            print("=====", shop["title"])
            data, ok = [], False
            for body in ({"cursor": {"limit": 100}, "order": {"ascending": False}}, {"cursor": {"limit": 100}}, {}):
                cursor = dict(body.get("cursor") or {})
                data = []
                for _ in range(60):
                    b = dict(body)
                    if cursor:
                        b["cursor"] = cursor
                    r = await call(c, "POST", "/content/v2/cards/error/list", tok, params={"locale": "ru"}, json=b)
                    if r.status_code != 200:
                        print("  вариант", list(body), "→", r.status_code, r.text[:150])
                        break
                    d = r.json()
                    part = (d.get("data") or {}).get("items") if isinstance(d.get("data"), dict) else d.get("data")
                    part = part or []
                    data += part
                    cur = (d.get("data") or {}).get("cursor") if isinstance(d.get("data"), dict) else None
                    if not cur or not cur.get("next") or len(part) < 100:
                        ok = True
                        break
                    cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "batchUUID": cur.get("batchUUID")}
                if ok:
                    break
            (OUT / f"errors_{shop['key']}.json").write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            cnt = collections.Counter()
            for e in data:
                errs = e.get("errors") or []
                if isinstance(errs, dict):
                    errs = [f"{k}: {v}" for k, vv in errs.items() for v in (vv if isinstance(vv, list) else [vv])]
                for t in errs:
                    cnt[str(t)[:140]] += 1
            print(f"  записей {len(data)}; пример: {json.dumps(data[:1], ensure_ascii=False)[:600]}")
            for t, n in cnt.most_common(15):
                print(f"   {n:>6} · {t}")
        tok = os.getenv("WB_SHOP2", "").strip()
        for q in ("", "дисплей", "модуль"):
            r = await call(c, "GET", "/content/v2/directory/tnved", tok, params={"subjectID": 5488, "search": q, "locale": "ru"})
            print("ТН ВЭД дисплеи", repr(q), [t.get("tnved") for t in (r.json().get("data") or [])] if r.status_code == 200 else r.text[:150])
        r = await call(c, "GET", "/content/v2/directory/countries", tok, params={"locale": "ru"})
        print("страны:", [x for x in (r.json().get("data") or []) if "итай" in json.dumps(x, ensure_ascii=False)][:3]
              if r.status_code == 200 else r.text[:150])

asyncio.run(main())
