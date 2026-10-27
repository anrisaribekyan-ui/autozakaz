"""Оптовый прайс Liberti (xlsx из оптового отдела) → data/liberti_price.json.

Строка: {"code", "barcode", "name", "cat", "opt", "qty"}. Наличия по Митино в прайсе нет (только на сайте).
Запуск: venv/bin/python liberti_price.py liberti_opt_price.xlsx
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl


def parse(path: Path) -> list[dict]:
    ws = openpyxl.load_workbook(str(path), read_only=True).worksheets[0]
    rows, cat, started = [], "", False
    for r in ws.iter_rows(values_only=True):
        if not started:
            started = (r[0] == "Код товара")
            continue
        code, bar, name, opt, qty = (list(r) + [None] * 5)[:5]
        if code and not name:
            cat = str(code).strip()
            continue
        if not name:
            continue
        try:
            price = float(str(opt).replace(",", "."))
        except (TypeError, ValueError):
            continue
        try:
            q = int(float(qty)) if qty not in (None, "") else 0
        except ValueError:
            q = 0
        rows.append({"code": code, "barcode": bar, "name": str(name).strip(), "cat": cat,
                     "opt": round(price), "qty": q})
    return rows


if __name__ == "__main__":
    rows = parse(Path(sys.argv[1]))
    out = Path("data/liberti_price.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
    print(len(rows), "позиций →", out)
