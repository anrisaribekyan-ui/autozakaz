"""Дозаполнение карточек WB по правилам Анри (22.10.2026).

Для всех карточек (кроме защитных стёкол и плёнок — их страну и прочее не трогаем):
  * габариты 20 × 10 × 5 см, вес с упаковкой 0,25 кг;
  * страна производства «Китай» (если пусто или «КНДР»);
  * код ТН ВЭД (как в уже заполненных карточках Анри): дисплеи и запчасти 8517790009, аккумуляторы 8507600000,
    блоки питания 8504403008 — если пусто;
  * «документ, подтверждающий безопасность, не требуется» (documents.excludeDocuments);
  * модель и совместимость — из названия (если пусто);
  * дисплеи: наличие рамки «Да/Нет» по названию; диагональ — только если она есть в названии;
  * аккумуляторы: тип «Li-Ion» (если пусто), ёмкость из названия («5000 мА*ч»), комплектация «аккумулятор,
    клей, набор инструментов».
Существующие значения не затираем (кроме страны «КНДР» и комплектации АКБ без клея).

Пробно (по умолчанию): таблица data/cards_fix_<время>.xlsx «было → станет», в WB ничего не отправляет.
--apply и CARDS_FIX_LIVE=1: отправляет пачками по 100; перед этим сохраняет исходные карточки в data/cards_backup_*.json.
--limit N — только первые N карточек, которые надо менять (для проверки на малом числе).

Запуск: venv/bin/python cards_fix.py --shops shop3 [--limit 20] [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx
import openpyxl
from dotenv import load_dotenv

from price_plan import SHOPS, _retry_after

API = "https://content-api.wildberries.ru"
SKIP_SUBJECTS = {1571, 453}                 # защитные стёкла, плёнки — не трогаем
DISPLAYS = {5488, 5489}
BATTERIES = {1309, 3125, 468}
TNVED = {5488: "8517790009", 5489: "8517790009", 6492: "8517790009",
         1309: "8507600000", 3125: "8507600000", 468: "8507600000", 3128: "8504403008"}
CH = {"compat": 746, "model": 5023, "country": 14177451, "tnved_old": 15000001, "tnved": 15004139,
      "frame": 355027, "diag": 4474, "batt_type": 11455, "capacity": 90878, "kit": 378533}
DIMS = {"length": 20, "width": 10, "height": 5, "weightBrutto": 0.25}
BATT_KIT = ["аккумулятор", "клей", "набор инструментов"]


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


BRANDS = ("xiaomi", "redmi", "poco", "samsung", "galaxy", "huawei", "honor", "iphone", "apple", "realme", "tecno",
          "infinix", "oppo", "vivo", "nokia", "zte", "itel", "google", "pixel", "oneplus", "motorola", "moto", "tcl",
          "meizu", "lenovo", "sony", "lg", "asus", "nothing", "blackview", "doogee", "oukitel", "ulefone", "wiko", "bq")
_STOP = re.compile(r"\s(?:\(|-\s|копия|оригинал|original|or\b|or100|in-?cell|incell|oled|amoled|tft|ips|lcd|"
                   r"в\s+рам|с\s+рам|модуль|премиум|premium|прем\b|на\s|нижн|верхн|с\s+компон|без\s+компон|скотч|"
                   r"узк|широк|черн|бел|син|сер|зол|красн|зел|фиол|розов|голуб|оранж|желт|бирюз|графит|титан|"
                   r"сиреневый|фиолет|black|white|blue|green|gold|silver)", re.I)
_BATT_CODE = re.compile(r"\b(?:B[NMPL][A-Z]?\d{2}[A-Z]?|BLP\d{3}|HB\d{4,}\w*|EB-?[A-Z0-9]{5,}|BL-\w+|BP\d{2}\w?)\b")


def models_from_title(title: str) -> tuple[str | None, list[str]]:
    """(модель, совместимость) из названия, регистр как в названии:
    «Дисплей для Huawei Honor 20/Nova 5T Оригинал» → Huawei Honor 20, Huawei Nova 5T."""
    t = " " + (title or "") + " "
    m = re.search(r"\sдля\s+(.+)", t, re.I)
    if m:
        t = " " + m.group(1)
    else:  # «Аккумулятор Xiaomi …», «Шлейф iPhone …», «Задняя крышка Xiaomi …»
        words = t.split()
        i = next((k for k, w in enumerate(words) if w.lower().strip("(") in BRANDS), None)
        if i is None:
            return None, []
        t = " " + " ".join(words[i:])
    m = _STOP.search(t)
    if m:
        t = t[:m.start()]
    t = _BATT_CODE.sub(" ", t)
    segs = [re.sub(r"\s+", " ", x).strip(" ,.-") for x in t.split("/")]
    segs = [x for x in segs if x]
    if not segs or segs[0].split()[0].lower() not in BRANDS:
        return None, []
    first = segs[0].split()
    out = [segs[0]]
    for sg in segs[1:]:
        w = sg.split()
        if w[0].lower() in BRANDS:
            out.append(sg)
            continue
        low1 = [x.lower() for x in first]
        if w[0].lower() in low1:
            pref = first[:low1.index(w[0].lower())]
        elif w[0][0].isdigit():
            k = next((j for j, x in enumerate(first) if any(ch.isdigit() for ch in x)), len(first))
            pref = first[:k]
        else:
            pref = first[:1]
        out.append(" ".join(pref + w))
    compat = []
    for x in out:
        if x not in compat and len(x) <= 60 and len(x.split()) >= 2:
            compat.append(x)
    return (" / ".join(compat)[:100] or None), compat[:10]


def plan_card(cd: dict) -> tuple[dict | None, list[str]]:
    """Новая карточка для update (или None) и список изменений «поле: было → станет»."""
    sid = cd.get("subjectID")
    if sid in SKIP_SUBJECTS:
        return None, []
    title = cd.get("title") or ""
    chars = {ch["id"]: ch.get("value") for ch in cd.get("characteristics") or []}
    new = dict(chars)
    changes: list[str] = []

    def setv(key: str, val, label: str, force: bool = False):
        cid = CH[key]
        old = chars.get(cid)
        if old and not force:
            return
        if old == val:
            return
        new[cid] = val
        changes.append(f"{label}: {old if old else '—'} → {val}")

    dm = cd.get("dimensions") or {}
    if any(float(dm.get(k) or 0) != v for k, v in DIMS.items()):
        changes.append(f"габариты: {dm.get('length')}×{dm.get('width')}×{dm.get('height')} {dm.get('weightBrutto')} кг"
                       f" → 20×10×5 0.25 кг")
    country = chars.get(CH["country"])
    if not country or any("КНДР" in str(x) for x in (country if isinstance(country, list) else [country])):
        setv("country", ["Китай"], "страна", force=True)
    tn = TNVED.get(sid)
    if tn:
        if not chars.get(CH["tnved"]):
            setv("tnved", [tn], "код ТН ВЭД")
        if not chars.get(CH["tnved_old"]):
            setv("tnved_old", [tn], "ТНВЭД")
    model, compat = models_from_title(title)
    if model:
        setv("model", [model], "модель")
    if compat:
        setv("compat", compat, "совместимость")
    low = title.lower()
    if sid in DISPLAYS:
        setv("frame", ["Да" if re.search(r"рамк|в раме|с рамой|модуль", low) else "Нет"], "наличие рамки")
        m = re.search(r"(\d{1,2}[.,]\d{1,2})\s*(?:\"|''|дюйм|inch)", title)
        if m:
            setv("diag", [m.group(1).replace(",", ".") + '"'], "диагональ")
    if sid in BATTERIES:
        setv("batt_type", ["Li-Pol" if re.search(r"li-?pol|полимер", low) else "Li-Ion"], "тип АКБ")
        m = re.search(r"(\d{3,5})\s*(?:mah|мач|ма\*?ч|mA\*?h)", title, re.I)
        if m:
            setv("capacity", [f"{int(m.group(1))} мА*ч"], "ёмкость")
        kit = chars.get(CH["kit"])
        if not kit or not any("клей" in str(x).lower() for x in kit):
            setv("kit", BATT_KIT, "комплектация", force=True)
    docs = cd.get("documents") or {}
    need_docs_flag = not docs.get("excludeDocuments")
    if need_docs_flag:
        changes.append("документ: требуется → не требуется")
    if not changes:
        return None, []
    upd = {
        "nmID": cd["nmID"], "vendorCode": cd.get("vendorCode"), "brand": cd.get("brand") or "",  # см. BRAND_MODE
        "title": title, "description": cd.get("description") or "",
        "dimensions": dict(DIMS),
        "characteristics": [{"id": k, "value": v} for k, v in new.items() if v not in (None, [], "")],
        "sizes": [{k: s.get(k) for k in ("chrtID", "techSize", "wbSize", "skus") if k in s}
                  for s in cd.get("sizes") or []],
    }
    if need_docs_flag:
        upd["documents"] = {"excludeDocuments": True}
    return upd, changes


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shops", default="shop2,shop3,shop4")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--brand", choices=("omit", "keep", "none"), default="omit",
                    help="omit — не отправлять бренд; keep — как есть; none — «Нет бренда»")
    ap.add_argument("--nm", help="только эти nmID через запятую (проверка)")
    args = ap.parse_args()
    load_dotenv(".env")
    live = args.apply and os.getenv("CARDS_FIX_LIVE", "0") == "1"
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    wbk = openpyxl.Workbook()
    ws = wbk.active
    ws.title = "Изменения"
    ws.append(["Магазин", "nmID", "Предмет", "Название", "Изменения"])
    async with httpx.AsyncClient(timeout=120) as c:
        for shop in SHOPS:
            if shop["key"] not in args.shops.split(","):
                continue
            tok = os.getenv(shop["env"], "").strip()
            cards = await all_cards(c, tok)
            todo = []
            for cd in cards:
                upd, ch = plan_card(cd)
                if upd:
                    todo.append((cd, upd, ch))
            if args.nm:
                want_nm = {int(x) for x in args.nm.split(",")}
                todo = [t for t in todo if t[0]["nmID"] in want_nm]
            if args.limit:
                todo = todo[:args.limit]
            print(f"{shop['title']}: карточек {len(cards)}, к изменению {len(todo)}", flush=True)
            for cd, _u, ch in todo:
                ws.append([shop["title"], cd["nmID"], cd.get("subjectName"), cd.get("title"), " | ".join(ch)])
            if not live or not todo:
                continue
            bk = Path("data") / f"cards_backup_{shop['key']}_{stamp}.json"
            bk.write_text(json.dumps([cd for cd, _u, _c in todo], ensure_ascii=False), "utf-8")
            ok = bad = 0
            for i in range(0, len(todo), 100):
                part = [dict(u) for _cd, u, _c in todo[i:i + 100]]
                for u in part:   # 22.10: бренда «mobi» нет в справочнике WB — по умолчанию не отправляем поле бренда
                    if args.brand == "omit":
                        u.pop("brand", None)
                    elif args.brand == "none":
                        u["brand"] = "Нет бренда"
                r = await call(c, "POST", "/content/v2/cards/update", tok, json=part)
                d = {}
                try:
                    d = r.json()
                except Exception:  # noqa: BLE001
                    pass
                if r.status_code == 200 and not d.get("error"):
                    ok += len(part)
                else:
                    bad += len(part)
                    print(f"   пачка {i // 100 + 1}: {r.status_code} {str(d.get('errorText') or r.text)[:300]}", flush=True)
                    if i == 0:
                        print("   первая пачка не принята — останавливаюсь, чтобы не повторять ошибку", flush=True)
                        break
            print(f"   отправлено: принято {ok}, ошибок {bad}; копия до изменений: {bk}", flush=True)
    for col, w in zip("ABCDE", (10, 12, 28, 60, 120)):
        ws.column_dimensions[col].width = w
    out = Path("data") / f"cards_fix_{stamp}.xlsx"
    wbk.save(out)
    print("таблица:", out)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
