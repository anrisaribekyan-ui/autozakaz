"""Переслать отчёт о прошедшей пачке в новом виде (сообщение + HTML-документ).

venv/bin/python resend_report.py                 — последние пачки Анри и Гора
venv/bin/python resend_report.py 20261019_1000   — пачки с этой меткой
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

from report_fmt import html_report, tg_summary
from test_sborka import MSK, tg_send
from zakaz_batch import OWNERS

RUNS = Path("data/batch_runs")


def mm_status(request_id: str) -> SimpleNamespace | None:
    j = Path("data/orders_mm.jsonl")
    last = None
    if j.exists():
        for line in j.read_text("utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("request_id") == request_id:
                last = d
    if not last:
        return None
    probs = [] if last.get("status") == "placed" else ["не увидел номер заказа после нажатия — заказ МОГ уйти"]
    return SimpleNamespace(status=last.get("status"), order_number=last.get("order"), total=last.get("total"),
                           problems=probs, ok=last.get("status") == "placed")


async def one(run_dir: Path) -> None:
    stamp, owner = run_dir.name.rsplit("_", 1)
    own = OWNERS[owner]
    by_title = {s["title"]: s for s in own["shops"]}
    lines = json.loads((run_dir / "lines.json").read_text("utf-8"))
    for ln in lines:
        ln["shop"] = by_title.get(ln["shop"], {"key": "", "title": ln["shop"]})
    mres = json.loads((run_dir / "moba.json").read_text("utf-8")) if (run_dir / "moba.json").exists() else {}
    mm = mm_status(f"mm-{stamp}-{owner}")
    when = datetime.strptime(stamp, "%Y%m%d_%H%M").replace(tzinfo=MSK)
    title = "Гор" if owner == "gor" else "Анри · Лёша · Борода"
    f = run_dir / f"Закупка_{title.split(' ')[0]}_{when:%d.%m_%H%M}.html"
    f.write_text(html_report(title, lines, mres, mm, True, [], when), "utf-8")
    await tg_send(tg_summary(title, lines, mres, mm, True, [], when), [f] if lines else [], *own["bot"],
                  parse_mode="HTML")
    print("отправлено:", run_dir.name, len(lines), "заказов")


async def main() -> None:
    load_dotenv(".env")
    tag = sys.argv[1] if len(sys.argv) > 1 else None
    for owner in OWNERS:
        dirs = sorted(d for d in RUNS.glob(f"*_{owner}") if (d / "lines.json").exists()
                      and (tag is None or d.name.startswith(tag)))
        if dirs:
            await one(dirs[-1])


if __name__ == "__main__":
    asyncio.run(main())
