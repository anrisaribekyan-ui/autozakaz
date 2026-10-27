"""Прайс moba (Price_Moba-Msk_*.xlsx) → data/moba_price.json.

Строка: {"code", "art", "name", "extra", "price", "avail", "sub", "incoming"}; avail — точки (Ми = Митино).
Запуск: venv/bin/python moba_price.py Price_Moba-Msk_21-10-26.xlsx
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl


def parse(path: Path) -> list[dict]:
    ws = openpyxl.load_workbook(str(path), read_only=True).worksheets[0]
    rows, sub = [], ""
    for r in ws.iter_rows(values_only=True):
        code, art, name, extra, price, avail, _order, inc = (list(r) + [None] * 8)[:8]
        if code and not name and price is None:
            sub = str(code).strip()
            continue
        if not name or price in (None, ""):
            continue
        try:
            p = int(round(float(price)))
        except (TypeError, ValueError):
            continue
        rows.append({"code": code, "art": art, "name": str(name).strip(), "extra": extra, "price": p,
                     "avail": (avail or "").strip(), "sub": sub, "incoming": inc})
    return rows


if __name__ == "__main__":
    rows = parse(Path(sys.argv[1]))
    out = Path("data/moba_price.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
    print(len(rows), "позиций →", out)
