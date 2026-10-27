"""Прайсы SMG777 (PDF от менеджера) → data/smg_price.json.

Строка: {"file", "section", "name", "col", "price", "note"}.
  col — колонка прайса (SERVICE-PACK, Переклейка, OLED, ORIG, IN CELL, FHD (JK)…), для АКБ/рамок — «цена».
Запуск: venv/bin/python smg_price.py прайс1.pdf прайс2.pdf …
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pdfplumber

HEAD_WORDS = re.compile(r"^(SERVICE-PACK|SERVICE/new|Переклейка|ПЕРЕКЛЕЙКА|OLED|ORIG|IN|CELL|ПРЕМИУМ/IN|ПРЕМИУМ|"
                        r"СНЯТЫЙ|FOG-ORIG|SOFT|S0FT|\(DD\)|FHD|\(JK\)|120|герц|Наименование)$", re.I)
NUM = re.compile(r"^\d{2,6}(?:/\d{2,6})?$")
SKIP = re.compile(r"Номер|для связи|звязи|\+7|863-60-24|Актульный|АКТУАЛЬНЫЙ|Прайс", re.I)


def _lines(page) -> list[list[dict]]:
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    words.sort(key=lambda w: (round(w["top"]), w["x0"]))
    out: list[list[dict]] = []
    for w in words:
        if out and abs(out[-1][0]["top"] - w["top"]) <= 3:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(l, key=lambda w: w["x0"]) for l in out]


def _header(line: list[dict]) -> list[tuple[float, float, str]] | None:
    """Строка-шапка колонок → [(x0, x1, подпись)]. Слова, стоящие рядом, склеиваются в одну подпись."""
    heads = [w for w in line if HEAD_WORDS.match(w["text"]) and w["text"] != "Наименование"]
    if not heads:
        return None
    # строка-шапка: всё, что не подпись колонки, — название раздела слева и без цифр
    others = [w for w in line if not HEAD_WORDS.match(w["text"])]
    if others and re.match(r"(АКБ|Аккумулятор|Банка|Рамка)", others[0]["text"], re.I):
        return None   # «АКБ Apple iPhone ORIG Снятый» — заголовок раздела, а не колонки
    if any(w["x1"] > heads[0]["x0"] or re.search(r"\d|\[", w["text"]) for w in others):
        return None
    if len(heads) < 1 or not any(re.search(r"SERVICE|Переклей|ПЕРЕКЛЕЙ|OLED|ORIG|CELL|СНЯТ|FOG|SOFT|S0FT|FHD",
                                            w["text"], re.I) for w in heads):
        return None
    cols: list[list] = []
    for w in line:
        if w["text"] == "Наименование":
            continue
        if cols and w["x0"] - cols[-1][1] < 8:
            cols[-1][1] = w["x1"]
            cols[-1][2] += " " + w["text"]
        else:
            cols.append([w["x0"], w["x1"], w["text"]])
    cols = [c for c in cols if re.search(r"SERVICE|Переклей|ПЕРЕКЛЕЙ|OLED|ORIG|CELL|СНЯТ|FOG|SOFT|S0FT|FHD|ПРЕМИУМ",
                                         c[2], re.I)]
    return [(c[0], c[1], c[2].strip()) for c in cols] or None


def parse(path: Path) -> list[dict]:
    rows: list[dict] = []
    cols: list[tuple[float, float, str]] | None = None
    section = ""
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            right = page.width * 0.55
            for line in _lines(page):
                text = " ".join(w["text"] for w in line)
                if SKIP.search(text) and not any(NUM.match(w["text"]) for w in line[-2:]):
                    # шапка файла / заголовок раздела («Аккумулятор Samsung», «Рамка Дисплея для Xiaomi»)
                    continue
                h = _header(line)
                if h:
                    cols = h
                    # слова левее колонок — название раздела (XIAOMI, REALME/OPPO…)
                    sec = " ".join(w["text"] for w in line if w["x1"] < h[0][0] - 5 and w["text"] != "Наименование")
                    section = sec or section
                    continue
                nums = [i for i, w in enumerate(line) if NUM.match(w["text"])]
                if not nums:
                    if len(line) <= 4 and not cols:
                        section = text
                    elif len(line) <= 3:
                        section = text
                    continue
                if cols:
                    first_x = cols[0][0] - 40
                    # цены — числа, стоящие в зоне колонок
                    cells = [i for i in nums if line[i]["x0"] >= first_x]
                    if not cells:
                        continue
                    name = " ".join(w["text"] for w in line[:cells[0]]).strip()
                    for n, i in enumerate(cells):
                        w = line[i]
                        cx = (w["x0"] + w["x1"]) / 2
                        col = min(cols, key=lambda c: abs((c[0] + c[1]) / 2 - cx))
                        end = cells[n + 1] if n + 1 < len(cells) else len(line)
                        note = " ".join(x["text"] for x in line[i + 1:end]).strip()
                        price = int(w["text"].split("/")[0])
                        rows.append({"file": path.name, "section": section, "name": name, "col": col[2],
                                     "price": price, "note": (w["text"] + " " + note).strip() if "/" in w["text"] or note else ""})
                else:
                    i = nums[-1]
                    if i != len(line) - 1 or line[i]["x0"] < right:
                        continue   # цена — последнее слово справа; иначе цифра из названия (нет цены)
                    name = " ".join(w["text"] for w in line[:i]).strip()
                    rows.append({"file": path.name, "section": section, "name": name, "col": "цена",
                                 "price": int(line[i]["text"].split("/")[0]), "note": ""})
    return [r for r in rows if r["name"]]


def main() -> None:
    files = [Path(p) for p in sys.argv[1:]]
    rows: list[dict] = []
    for f in files:
        part = parse(f)
        print(f.name, len(part))
        rows += part
    out = Path("data/smg_price.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), "utf-8")
    print("всего", len(rows), "→", out)


if __name__ == "__main__":
    main()
