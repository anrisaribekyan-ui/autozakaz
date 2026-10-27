"""Приём прайс-листов в бот Анри: прислал файл → машина разобрала, сравнила со старым и сохранила.

Понимает: moba (Price_Moba…xlsx), Мастер Мобайл (PRICELIST_MASTERMOBILE…xlsx),
Liberti (оптовый xlsx «LIBERTY PROJECT»), SMG777 (pdf: дисплеи / рамки / аккумуляторы / Apple).
Старый прайс уходит в data/prices_archive/. Защита: если новых строк меньше половины старых — не сохраняет.
(Каркас написан DeepSeek по ТЗ, проверен и поправлен.)
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

import pdfplumber
from aiogram import F, Router
from aiogram.types import Message
from openpyxl import load_workbook

logger = logging.getLogger(__name__)

DATA = Path("data")
INCOMING = DATA / "incoming"
ARCHIVE = DATA / "prices_archive"
SMG_KINDS = ("batteries", "frames", "apple", "displays")

CONFIG: dict[str, dict[str, Any]] = {
    "moba": {"module": "moba_price", "file": "moba_price.json", "price": "price", "key": ("code",), "label": "moba"},
    "mm": {"module": "mm_price", "file": "mm_price.json", "price": "price", "key": ("code",),
           "label": "Мастер Мобайл"},
    "liberti": {"module": "liberti_price", "file": "liberti_price.json", "price": "opt", "key": ("code",),
                "label": "Liberti"},
    "smg": {"module": "smg_price", "file": "smg_price.json", "price": "price", "key": ("name", "col"),
            "label": "SMG"},
}

UNKNOWN = ("Не понял, что за файл. Понимаю: прайс moba (Price_Moba…xlsx), Мастер Мобайл "
           "(PRICELIST_MASTERMOBILE…xlsx), Liberti (опт xlsx), SMG (pdf)")


class _Guard(Exception):
    """Защита от сохранения явно неполного прайса."""


def _fmt(value: float | int) -> str:
    return f"{value:,}".replace(",", " ")


def _norm(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return "" if value is None else str(value).strip().upper()


def _key(kind: str, row: dict) -> str | None:
    parts = []
    for field in CONFIG[kind]["key"]:
        part = _norm(row.get(field))
        if not part:
            return None
        parts.append(part)
    return "|".join(parts)


def _price(kind: str, row: dict) -> float | None:
    try:
        return float(row.get(CONFIG[kind]["price"]))
    except (TypeError, ValueError):
        return None


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Не читается %s: %s", path, exc)
        return []
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _save(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _archive(path: Path) -> None:
    if path.exists():
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, ARCHIVE / f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}.json")


def _index(kind: str, rows: list[dict]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for row in rows:
        key = _key(kind, row)
        if key is not None:
            out[key] = _price(kind, row)
    return out


def _count_mitino(rows: list[dict]) -> int:
    return sum(1 for row in rows
               if "Ми" in [p.strip() for p in str(row.get("avail") or "").split(",")])


def _has_liberty(path: Path) -> bool:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        for row in wb.worksheets[0].iter_rows(max_row=10, values_only=True):
            if any(isinstance(c, str) and "LIBERTY PROJECT" in c.upper() for c in row):
                return True
    finally:
        wb.close()
    return False


def _smg_kind_from_pdf(path: Path) -> str:
    with pdfplumber.open(path) as pdf:
        text = (pdf.pages[0].extract_text() or "").upper() if pdf.pages else ""
    if "АККУМУЛЯТОР" in text:
        return "batteries"
    if "РАМКА" in text:
        return "frames"
    if "APPLE" in text:
        return "apple"
    return "displays"


def _smg_infer(row: dict) -> str:
    kind = _norm(row.get("kind")).lower()
    if kind in SMG_KINDS:
        return kind
    section, name = _norm(row.get("section")), _norm(row.get("name"))
    for text in (section, name):
        if text.startswith(("АККУМУЛЯТОР", "БАНКА", "АКБ")):
            return "batteries"
    if "РАМКА" in section or "РАМКА" in name:
        return "frames"
    if name.startswith("IPHONE"):
        return "apple"
    return "displays"


async def _detect_kind(name: str, path: Path) -> str | None:
    low = name.lower()
    if "price_moba" in low:
        return "moba"
    if "mastermobile" in low:
        return "mm"
    if path.suffix.lower() in (".xlsx", ".xlsm") and await asyncio.to_thread(_has_liberty, path):
        return "liberti"
    if path.suffix.lower() == ".pdf":
        return "smg"
    return None


def _report(kind: str, cfg: dict, old_all: list[dict], old_same: list[dict],
            new_rows: list[dict], merged: list[dict]) -> str:
    old_map, new_map = _index(kind, old_same), _index(kind, new_rows)
    changes: list[float] = []
    cheaper = pricier = 0
    for key, new_price in new_map.items():
        old_price = old_map.get(key)
        if old_price is None or old_price <= 0 or new_price is None:
            continue
        delta = (new_price - old_price) / old_price * 100
        changes.append(delta)
        cheaper += delta < 0
        pricier += delta > 0
    lines = [f"✅ Прайс {cfg['label']} обновлён: {_fmt(len(merged))} позиций (было {_fmt(len(old_all))})"]
    if changes:
        lines.append(f"Цены у общих позиций ({_fmt(len(changes))}): дешевле {_fmt(cheaper)} · "
                     f"дороже {_fmt(pricier)} · медиана изменения {median(changes):+.1f}%")
    lines.append(f"Новых позиций: {_fmt(sum(1 for k in new_map if k not in old_map))} · "
                 f"пропало: {_fmt(sum(1 for k in old_map if k not in new_map))}")
    if kind == "moba":
        lines.append(f"В Митино: {_fmt(_count_mitino(new_rows))}")
    elif kind == "mm":
        much = sum(1 for r in new_rows if "много" in str(r.get("avail") or "").lower())
        little = sum(1 for r in new_rows if "мало" in str(r.get("avail") or "").lower())
        lines.append(f"Много: {_fmt(much)} · Мало: {_fmt(little)}")
    return "\n".join(lines)


async def _process(kind: str, path: Path) -> str:
    cfg = CONFIG[kind]
    smg_kind = await asyncio.to_thread(_smg_kind_from_pdf, path) if kind == "smg" else None
    rows = await asyncio.to_thread(importlib.import_module(cfg["module"]).parse, path)
    rows = [dict(r) for r in rows if isinstance(r, dict)]
    if smg_kind:
        for r in rows:
            r["kind"] = smg_kind
    if not rows:
        raise ValueError("парсер не нашёл ни одной позиции")
    target = DATA / cfg["file"]
    old_all = _load(target)
    if smg_kind:
        old_same = [r for r in old_all if _smg_infer(r) == smg_kind]
        merged = [r for r in old_all if _smg_infer(r) != smg_kind] + rows
    else:
        old_same, merged = old_all, rows
    if len(old_same) > 100 and len(rows) < len(old_same) * 0.5:
        raise _Guard(f"⚠️ Не сохраняю прайс {cfg['label']}: было {_fmt(len(old_same))} позиций, "
                     f"новых {_fmt(len(rows))} — меньше половины. Похоже на сбой.")
    _archive(target)
    _save(target, merged)
    return _report(kind, cfg, old_all, old_same, rows, merged)


def register_price_intake(router: Router, svc: Any) -> None:
    @router.message(F.document)
    async def price_intake(message: Message) -> None:
        user = message.from_user
        if svc.owner != "anri" or user is None or svc.tg_id is None or user.id != svc.tg_id:
            return
        document = message.document
        name = document.file_name or f"file_{document.file_unique_id}"
        INCOMING.mkdir(parents=True, exist_ok=True)
        path = INCOMING / f"{datetime.now():%Y%m%d_%H%M%S}_{Path(name).name}"
        try:
            await svc.bot.download(document, destination=path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не смог скачать %s", name)
            await message.answer(f"⚠️ Не получилось разобрать {name}: {exc}")
            return
        await message.answer(f"Принял {name}, разбираю…")
        try:
            kind = await _detect_kind(name, path)
            if kind is None:
                await message.answer(UNKNOWN)
                return
            await message.answer(await _process(kind, path))
        except _Guard as exc:
            await message.answer(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Разбор %s не удался", name)
            await message.answer(f"⚠️ Не получилось разобрать {name}: {exc}")
