"""Себесы Гора: весь прайс через машину (moba → Мастер Мобайл → poisk123), оптовые цены.

Правила (Анри, 18.10):
  * себес = закупка + 100 ₽;
  * у Гора крышки всегда премиум;
  * АКБ на moba — только если на WB написано BC / Zevo / JCID; остальные АКБ —
    сначала Liberti / SMG (самый дешёвый опт из них), потом прочие магазины poisk123 (вся Москва),
    Мастер Мобайл — последним;
  * прочее: moba или Мастер Мобайл — где дешевле; если нигде — poisk123.
moba берётся из прайса data/moba_price.json (оптовые цены прайса), MM и poisk123 — живьём.

Запуск:  python gor_costs.py costs_gor.xlsx            (продолжает с места остановки, кэш data/gor_costs_cache.json)
Результат: data/costs_gor_filled.xlsx
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import sys
import time
from pathlib import Path

import matcher
from matcher import _normalize, allowed, maker_ok, display_maker, find_candidates, is_battery, parse_wb_title, battery_tag
from suppliers_ext import MasterMobileClient, Poisk123Client, _fits, _p123_query, mm_battery_ok, MM_PREMIUM_RE

matcher._dump_match_debug = lambda *a, **k: None
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CACHE = DATA / "gor_costs_cache.json"
PACK = 100
PRIO_SHOPS = re.compile(r"liberti|либерти|smg|смг", re.I)   # АКБ: сначала они
TODAY, PREV_TO = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))

ROWS = json.load(open(DATA / "moba_price.json"))
IDX = collections.defaultdict(list)
for _i, _r in enumerate(ROWS):
    for _t in set(_normalize(_r["name"]).split()):
        IDX[_t].append(_i)
BATT = [r for r in ROWS if re.match(r"(акб|аккумулятор)\b", r["name"].lower())]


class Cat:
    """Поиск по прайсу moba (офлайн), интерфейс как у MobaClient.search."""
    async def search(self, q, n):
        toks = [t for t in _normalize(q).split() if len(t) >= 2]
        if not toks:
            return []
        toks.sort(key=lambda t: len(IDX.get(t, [])) or 10**9)
        out = []
        for i in IDX.get(toks[0], []):
            if all(t in _normalize(ROWS[i]["name"]).split() for t in toks):
                out.append({"url": str(i), "name": ROWS[i]["name"], "price": ROWS[i]["price"]})
        return out[:200]


_SPEC_RE = re.compile(r"\d+(?:[.,]\d+)?(?:\s*(?:v|a|w)|(?:вт|в|а))\b|\d(?:[.,]\d)?\s*[x×*х]\s*\d(?:[.,]\d+)?", re.I)


def _spec_norm(x: str) -> str:
    x = x.lower().replace(",", ".").replace(" ", "")
    x = re.sub(r"[×*х]", "x", x)
    return x.replace("в", "v").replace("а", "a").replace("вт", "w")


def spec_ok(title: str, name: str) -> bool:
    """Блоки питания и т.п.: вольты, амперы, размер штекера из названия WB должны быть у товара."""
    specs = {_spec_norm(m) for m in _SPEC_RE.findall(title)}
    have = {_spec_norm(m) for m in _SPEC_RE.findall(name)}
    return specs <= have


def gor_title(name: str) -> str:
    low = name.lower()
    if re.search(r"крышк|корпус", low) and not re.search(r"премиум|premium", low):
        return name + " Премиум"
    return name


# ---------- АКБ по коду в прайсе moba ----------
def _nc(s): return re.sub(r"[^A-Z0-9]", "", s.upper())


def _codes(t):
    out = set()
    for m in re.finditer(r"\b([A-Za-z]{1,4}-?[A-Za-z]{0,4}\d[A-Za-z0-9-]{1,10})\b", t):
        c = _nc(m.group(1))
        if len(c) >= 4 and re.search(r"[A-Z]", c) and re.search(r"\d", c):
            out.add(c)
    return out


def moba_battery(title):
    tag = battery_tag(title)
    cs = _codes(title)
    if not cs:
        return None
    def toks(r): return {_nc(x) for x in re.findall(r"[A-Za-z0-9-]+", r["name"])}
    hit = [r for r in BATT if cs & toks(r)] or \
          [r for r in BATT if any(t.startswith(c) and len(t) - len(c) <= 2 for c in cs for t in toks(r))]
    if not hit:
        return None
    pref = [r for r in hit if tag and (tag in r["name"].lower() or (tag == "battery collection" and re.search(r"\bbc\b", r["name"].lower())))]
    return min(pref or hit, key=lambda r: r["price"])


async def moba_best(title):
    try:
        r = await find_candidates(Cat(), title, check_stock=False, max_items=30, max_queries=6)
    except Exception:
        r = {"items": []}
    items = [i for i in (r.get("items") or []) if i.get("price") and spec_ok(title, i["name"])]
    wb = parse_wb_title(title)
    if is_battery(wb):
        tag = battery_tag(title)
        same = [i for i in items if tag and tag in i["name"].lower()]
        items = sorted(same or items, key=lambda i: i["price"])
        if not items:
            br = moba_battery(title)
            if br:
                items = [{"url": str(ROWS.index(br)), "name": br["name"], "price": br["price"]}]
    if not items:
        return None
    row = ROWS[int(items[0]["url"])]
    return {"src": "moba (прайс)", "price": int(row["price"]), "name": row["name"], "art": row.get("art", ""),
            "avail": row.get("avail", "")}


async def ext_best(title, mm, p123, use_p123=lambda mm_best: True):
    wb = parse_wb_title(title)
    res = {"mm": None, "p123": None, "prio": None, "err": ""}
    q = _p123_query(wb)
    if not q:
        return res
    try:
        items = await mm.search(f"{wb.get('type') or ''} {q}".strip())
    except Exception as e:  # noqa: BLE001
        items, res["err"] = [], f"MM: {e}"
    best = None
    for it in items:
        ok, ql = _fits(it["name"], wb, title)
        p = it.get("price_club") or it.get("price")
        if ok and p and spec_ok(title, it["name"]) and maker_ok(title, it["name"]) and mm_battery_ok(it["name"], wb) and allowed(wb["quality"], ql) \
                and "нет в наличии" not in (it.get("avail_text") or "").lower():
            if not best or p < best["price"]:
                best = {"src": "Мастер Мобайл", "price": int(p), "name": it["name"], "art": it.get("article", ""),
                        "avail": it.get("avail_text", "")}
    res["mm"] = best
    if not use_p123(best):
        return res
    if os.environ.get("NO_P123"):          # poisk123 сейчас не опрашиваем — пометить, чтобы потом добить
        res["err"] = (res["err"] + " p123 пропущен").strip()
        return res
    try:
        rows = await p123.search(q)
    except Exception as e:  # noqa: BLE001
        rows, res["err"] = [], (res["err"] + f" poisk123: {e}").strip()
    best = prio = None
    for r in rows:
        if r["shop"].lower().startswith(("moba", "master mobile")):
            continue
        ok, ql = _fits(r["name"], wb, title)
        p = r.get("opt") or r.get("price")
        if ok and p and spec_ok(title, r["name"]) and maker_ok(title, r["name"]) and allowed(wb["quality"], ql):
            cand = {"src": f"poisk123: {r['shop']}", "price": int(p), "name": r["name"], "art": "",
                    "avail": r.get("rest", "")}
            if not best or p < best["price"]:
                best = cand
            if PRIO_SHOPS.search(r["shop"]) and (not prio or p < prio["price"]):
                prio = cand
    res["p123"], res["prio"] = best, prio
    return res


SMG_PATH = DATA / "smg_price.json"
SMG = json.loads(SMG_PATH.read_text("utf-8")) if SMG_PATH.exists() else []
SMG_BATT = [r for r in SMG if re.match(r"(аккумулятор|акб)", r["name"].lower())
            and not r["name"].lower().startswith("банка")]


LIB_PATH = DATA / "liberti_price.json"
LIB = json.loads(LIB_PATH.read_text("utf-8")) if LIB_PATH.exists() else []
LIB_BATT = [r for r in LIB if r["cat"].startswith(("1_1_1.", "1_4_1.")) and r.get("qty", 0) > 0]


def liberti_best(title):
    """АКБ из оптового прайса Liberti (оффлайн). Повышенная ёмкость — только если на WB «усилен»."""
    wb = parse_wb_title(title)
    if not is_battery(wb) or battery_tag(title):
        return None
    strong = "усил" in title.lower()
    best = None
    for r in LIB_BATT:
        low = r["name"].lower()
        if (("ultra capacity" in low) or ("повышенн" in low)) != strong:
            continue
        ok, _q = _fits(r["name"], wb, title)
        if ok and spec_ok(title, r["name"]) and (not best or r["opt"] < best["price"]):
            best = {"src": "Liberti (опт прайс)", "price": int(r["opt"]), "name": r["name"], "art": r["code"],
                    "avail": f'{r["qty"]} шт (вся сеть)'}
    return best


MM_PATH = DATA / "mm_price.json"
MMP = json.loads(MM_PATH.read_text("utf-8")) if MM_PATH.exists() else []
MM_IDX: dict = collections.defaultdict(set)
for _i, _r in enumerate(MMP):
    for _t in set(_normalize(_r["name"]).split()):
        MM_IDX[_t].add(_i)


def mm_offline_best(title):
    """Мастер Мобайл по прайсу из кабинета (уровень Bronze), оффлайн."""
    if not MMP:
        return None
    wb = parse_wb_title(title)
    ids: set = set()
    for m in wb.get("models") or []:
        toks = [t for t in _normalize(m).split() if len(t) >= 2] or _normalize(m).split()
        if not toks:
            continue
        cur = None
        for t in toks:
            cur = set(MM_IDX.get(t, set())) if cur is None else cur & MM_IDX.get(t, set())
        ids |= cur or set()
    best = None
    for i in ids:
        r = MMP[i]
        ok, ql = _fits(r["name"], wb, title)
        if not ok or not allowed(wb["quality"], ql) or not mm_battery_ok(r["name"], wb) \
                or not maker_ok(title, r["name"]) or not spec_ok(title, r["name"]):
            continue
        if not best or r["price"] < best["price"]:
            best = {"src": "Мастер Мобайл", "price": int(r["price"]), "name": r["name"], "art": r["code"],
                    "avail": r["avail"]}
    return best


LIB_IDX: dict = collections.defaultdict(set)
for _i, _r in enumerate(LIB):
    for _t in set(_normalize(_r["name"]).split()):
        LIB_IDX[_t].add(_i)

_SMG_Q = [(r"SERVICE|ORIG|Переклей|ПЕРЕКЛЕЙ|СНЯТ|FOG", "OR"), (r"OLED|SOFT|S0FT", "OLED"), (r"IN CELL|ПРЕМИУМ|Премиум|FHD", "In-Cell")]
SMG_DISP = []
for _r in SMG:
    if _r["file"].startswith(("4594",)) or _r["col"] == "цена" and not re.search(r"рамк", _r.get("section", ""), re.I):
        pass
    if _r["col"] != "цена":
        q = next((v for k, v in _SMG_Q if re.search(k, _r["col"], re.I)), "")
        mk = " (JK)" if "JK" in _r["col"] else ""
        SMG_DISP.append({**_r, "full": f"Дисплей для {_r['name']} {q}{mk} {_r.get('note', '')}".strip()})
    elif re.search(r"рамк", _r.get("section", "") + _r["file"], re.I) or _r["file"].startswith("4594"):
        SMG_DISP.append({**_r, "full": f"Рамка дисплея для {_r['name']}"})
SMG_IDX: dict = collections.defaultdict(set)
for _i, _r in enumerate(SMG_DISP):
    for _t in set(_normalize(_r["full"]).split()):
        SMG_IDX[_t].add(_i)


def _offline_best(title, rows, idx, namekey, pricekey, src):
    wb = parse_wb_title(title)
    if is_battery(wb):
        return None
    ids: set = set()
    for m in wb.get("models") or []:
        toks = _normalize(m).split()
        cur = None
        for t in toks:
            cur = set(idx.get(t, ())) if cur is None else cur & set(idx.get(t, ()))
        ids |= cur or set()
    best = None
    for i in ids:
        r = rows[i]
        nm = r[namekey]
        if src.startswith("Liberti") and not r.get("qty"):
            continue
        ok, ql = _fits(nm, wb, title)
        if not ok or not allowed(wb["quality"], ql) or not maker_ok(title, nm) or not spec_ok(title, nm):
            continue
        if not best or r[pricekey] < best["price"]:
            best = {"src": src, "price": int(r[pricekey]), "name": r.get("name") if src.startswith("SMG") else nm,
                    "art": r.get("code", ""), "avail": ""}
    return best


MOBA_IDX = IDX


def moba_any(title):
    b = _offline_best(title, ROWS, MOBA_IDX, "name", "price", "moba (прайс)")
    if b:
        r = next((x for x in ROWS if x["name"] == b["name"]), {})
        b.update({"art": r.get("art", ""), "avail": r.get("avail", "")})
    return b


def liberti_any(title):
    return _offline_best(title, LIB, LIB_IDX, "name", "opt", "Liberti (опт прайс)")


def smg_any(title):
    return _offline_best(title, SMG_DISP, SMG_IDX, "full", "price", "SMG777 (прайс)")


def smg_best(title):
    """АКБ из прайса SMG777 (PDF менеджера, оффлайн). Усиленные — только если на WB «усилен»."""
    wb = parse_wb_title(title)
    if not is_battery(wb) or battery_tag(title):
        return None
    strong = "усил" in title.lower()
    best = None
    for r in SMG_BATT:
        if ("усил" in r["name"].lower()) != strong:
            continue
        ok, _q = _fits(r["name"], wb, title)
        if ok and spec_ok(title, r["name"]) and (not best or r["price"] < best["price"]):
            best = {"src": "SMG777 (прайс)", "price": int(r["price"]), "name": r["name"], "art": "", "avail": ""}
    return best


def choose(title, moba, ext):
    wb = parse_wb_title(title)
    if is_battery(wb) and not battery_tag(title):
        # АКБ не BC/Zevo/JCID: Liberti/SMG → прочие poisk123 → Мастер Мобайл (последний)
        prio = [x for x in (ext.get("prio"), smg_best(title), liberti_best(title)) if x]
        if prio:
            return min(prio, key=lambda x: x["price"])
        return ext.get("p123") or ext.get("mm")
    else:
        cands = [x for x in (moba, ext["mm"]) if x] or \
                [x for x in (ext.get("p123"), liberti_any(title), smg_any(title)) if x]
    if not cands:
        return None
    # при равной цене — Мастер Мобайл (возвраты крышек)
    return min(cands, key=lambda x: (x["price"], 0 if x["src"] == "Мастер Мобайл" else 1))


async def main(src):
    import openpyxl
    from openpyxl.styles import PatternFill
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    wbk = openpyxl.load_workbook(src)
    ws = wbk.worksheets[0]
    todo = [(row, str(ws.cell(row, 3).value)) for row in range(2, ws.max_row + 1)
            if ws.cell(row, 3).value and not str(ws.cell(row, 3).value).startswith("2+1")]
    mm, p123, p123all = MasterMobileClient(), Poisk123Client(), Poisk123Client(mitino_only=False)
    sem = asyncio.Semaphore(3)
    done = [0]
    if os.environ.get("REDO_MOBA"):          # пересчитать moba по прайсу (офлайн, быстро) после правок подбора
        for i, (n, v) in enumerate(list(cache.items())):
            if v is None:
                continue
            t = gor_title(n)
            wbp = parse_wb_title(t)
            v["moba"] = None if (is_battery(wbp) and not battery_tag(t)) else await moba_best(t)
        print("moba пересчитана по прайсу", flush=True)
    if os.environ.get("REDO_MISSES"):        # пересчитать «не найдено нигде» (после доработки подбора)
        for n, v in list(cache.items()):
            if v and not v.get("moba") and not v.get("mm") and not v.get("p123") and not v.get("prio"):
                v["err"] = (v.get("err") or "") + " redo"
    # 18.10: дисплеи с производителем (JK/ZY…) — пересчитать, раньше брался любой In-Cell
    for n, v in (list(cache.items()) if not os.environ.get("ONLY_REPORT") else []):
        if v is not None and display_maker(n):
            v["err"] = (v.get("err") or "") + " maker"
    # 18.10: АКБ из Мастер Мобайла — только Premium; старые записи с не-Premium пересчитать
    for n, v in (list(cache.items()) if not os.environ.get("ONLY_REPORT") else []):
        m = (v or {}).get("mm")
        if m and is_battery(parse_wb_title(gor_title(n))) and not MM_PREMIUM_RE.search(m.get("name", "")):
            v["err"] = (v.get("err") or "") + " mm-not-premium"
    need = sum(1 for _, n in todo if n not in cache or (cache[n] or {}).get("err"))
    print(f"осталось проверить: {need} (из них повтор после ошибок: {sum(1 for _, n in todo if n in cache and (cache[n] or {}).get('err'))})", flush=True)

    async def one(row, name):
        if name in cache and not (cache[name] or {}).get("err"):
            return
        title = gor_title(name)
        async with sem:
            wb = parse_wb_title(title)
            moba = None if (is_battery(wb) and not battery_tag(title)) else await moba_best(title)
            batt_ext = is_battery(wb) and not battery_tag(title)
            try:
                # poisk123 — только для АКБ без BC/Zevo/JCID и когда нет ни на moba, ни в Мастер Мобайле
                ext = await ext_best(title, mm, p123all if batt_ext else p123,
                                     use_p123=lambda mm_best: batt_ext or (not moba and not mm_best))
            except Exception as e:  # noqa: BLE001
                ext = {"mm": None, "p123": None, "prio": None, "err": str(e)}
        cache[name] = {"moba": moba, **ext}
        done[0] += 1
        if done[0] % 25 == 0:
            CACHE.write_text(json.dumps(cache, ensure_ascii=False))
            print(f"{done[0]}/{need}", flush=True)

    try:
        if not os.environ.get("ONLY_REPORT"):     # ONLY_REPORT=1 — без сайтов, только пересобрать таблицу
            await asyncio.gather(*(one(r, n) for r, n in todo))
    finally:
        CACHE.write_text(json.dumps(cache, ensure_ascii=False))
        await mm.close()
        await p123.close()
        await p123all.close()

    rep = wbk.create_sheet("Подбор себеса")
    rep.append(["Строка", "Артикул WB", "Название WB", "Было", "Стало (закупка+100)", "Закупка опт", "Где",
                "Товар у поставщика", "Артикул", "Наличие", "moba", "Мастер Мобайл", "poisk123", "Liberti/SMG", "Что сделано"])
    yellow, orange = PatternFill("solid", fgColor="FFF2CC"), PatternFill("solid", fgColor="FCE4D6")
    st = collections.Counter()
    for row, name in todo:
        c = cache.get(name) or {}
        if not c.get("moba"):
            _t = gor_title(name)
            _w = parse_wb_title(_t)
            if not (is_battery(_w) and not battery_tag(_t)):
                c = {**c, "moba": moba_any(_t)}
        mo = mm_offline_best(gor_title(name))
        if mo:
            c = {**c, "mm": mo}
        for sm in (smg_best(gor_title(name)), liberti_best(gor_title(name))):
            if sm and (not c.get("prio") or sm["price"] < c["prio"]["price"]):
                c = {**c, "prio": sm}
        best = choose(gor_title(name), c.get("moba"), c)
        old = ws.cell(row, 5).value
        pr = lambda k: (c.get(k) or {}).get("price")  # noqa: E731
        if not best:
            st["нет нигде"] += 1
            rep.append([row, ws.cell(row, 4).value, name, old, None, None, None, None, None, None,
                        pr("moba"), pr("mm"), pr("p123"), pr("prio"), "не найдено" + (f" ({c.get('err')})" if c.get("err") else "")])
            continue
        new = best["price"] + PACK
        try:
            oldi = int(float(old)) if old not in (None, "") else None
        except ValueError:
            oldi = None
        if not oldi:
            ws.cell(row, 5).value, ws.cell(row, 6).value = new, TODAY
            for k in (5, 6):
                ws.cell(row, k).fill = yellow
            act = "заполнен"
        elif oldi == new:
            act = "без изменений"
        else:
            ws.cell(row, 7).value, ws.cell(row, 8).value, ws.cell(row, 9).value = old, ws.cell(row, 6).value, PREV_TO
            ws.cell(row, 5).value, ws.cell(row, 6).value = new, TODAY
            for k in (5, 6, 7, 8, 9):
                ws.cell(row, k).fill = orange
            act = "обновлён"
        if False and c.get("err"):
            act += " (poisk123 не проверен)"
        st[act] += 1
        st["из " + best["src"].split(":")[0]] += 1
        rep.append([row, ws.cell(row, 4).value, name, old, new, best["price"], best["src"], best["name"],
                    best.get("art"), best.get("avail"), pr("moba"), pr("mm"), pr("p123"), pr("prio"), act])
    for col, w in zip("ABCDEFGHIJKLMNO", (7, 12, 55, 8, 10, 10, 26, 60, 18, 16, 8, 8, 8, 8, 16)):
        rep.column_dimensions[col].width = w
    rep.auto_filter.ref = rep.dimensions
    out = DATA / "costs_gor_filled.xlsx"
    wbk.save(out)
    print(dict(st))
    print("готово:", out)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else str(DATA / "costs_gor.xlsx")))
