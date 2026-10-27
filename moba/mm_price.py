"""Прайс Мастер Мобайла (XLSX из кабинета) → data/mm_price.json.

Файл у них «кривой»: SharedStrings.xml с большой буквы, openpyxl его не видит — читаем XML сами.
Строка: {"code", "name", "price", "avail", "cat"}.
Запуск: venv/bin/python mm_price.py PRICELIST_MASTERMOBILE_….XLSX
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _cells(path: Path):
    z = zipfile.ZipFile(path)
    names = {n.lower(): n for n in z.namelist()}
    strings: list[str] = []
    if "xl/sharedstrings.xml" in names:
        root = ET.fromstring(z.read(names["xl/sharedstrings.xml"]))
        for si in root.iter(NS + "si"):
            strings.append("".join(t.text or "" for t in si.iter(NS + "t")))
    sheet = names.get("xl/worksheets/sheet1.xml")
    for ev, row in ET.iterparse(z.open(sheet), events=("end",)):
        if row.tag != NS + "row":
            continue
        vals: dict[str, str] = {}
        for c in row.findall(NS + "c"):
            col = re.match(r"[A-Z]+", c.get("r", "")).group()
            v = c.find(NS + "v")
            if v is None or v.text is None:
                is_ = c.find(NS + "is")
                if is_ is not None:
                    vals[col] = "".join(t.text or "" for t in is_.iter(NS + "t"))
                continue
            vals[col] = strings[int(v.text)] if c.get("t") == "s" else v.text
        yield int(row.get("r")), int(row.get("outlineLevel") or 0), vals
        row.clear()


def parse(path: Path) -> list[dict]:
    rows, cats = [], {}
    for _r, lvl, v in _cells(path):
        name = (v.get("C") or "").strip()
        if not name:
            continue
        price = v.get("E")
        if v.get("B") and price:
            try:
                p = float(price)
            except ValueError:
                continue
            rows.append({"code": v.get("B"), "name": name, "price": round(p),
                         "avail": (v.get("H") or "").strip(), "cat": " / ".join(cats[k] for k in sorted(cats) if k < lvl)})
        else:
            cats = {k: x for k, x in cats.items() if k < lvl}
            cats[lvl] = name
    return rows


if __name__ == "__main__":
    rows = parse(Path(sys.argv[1]))
    out = Path("data/mm_price.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
    print(len(rows), "позиций →", out)
