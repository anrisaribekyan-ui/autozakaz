# learning.py
"""
Обучение подбора на решениях человека.
Чистые функции + класс Learned, считающий статистику из match_feedback.
Без сети, без aiogram.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------- #
# Скоринг решений
# ---------------------------------------------------------------------- #
SCORES: dict[str, float] = {
    "chosen": 1.0,
    "confirmed": 2.0,
    "passed": -0.25,
    "rejected": -1.0,
    "declined": -2.0,
}

STRONG_NEGATIVES = {"rejected", "declined"}
POSITIVES = {"chosen", "confirmed"}


# ---------------------------------------------------------------------- #
# Нормализация хвоста качества moba
# ---------------------------------------------------------------------- #
_LABEL_CANON: list[tuple[str, str]] = [
    (r"(?<![\wА-Яа-яЁё])or\s?100", "or100"),  # «OR100 HD+, 400 Кд/м²…» — один класс
    (r"(?<![\wА-Яа-яЁё])or(?![\wА-Яа-яЁё])|\(sp\)|service\s*pack", "or (sp)"),
    (r"(?<![\wА-Яа-яЁё])amoled(?![\wА-Яа-яЁё])|(?<![\wА-Яа-яЁё])oled(?![\wА-Яа-яЁё])", "oled"),
    (r"(?<![\wА-Яа-яЁё])optima(?![\wА-Яа-яЁё])|(?<![\wА-Яа-яЁё])оптима(?![\wА-Яа-яЁё])", "optima"),
    (r"(?<![\wА-Яа-яЁё])incell(?![\wА-Яа-яЁё])|(?<![\wА-Яа-яЁё])in-cell(?![\wА-Яа-яЁё])", "incell"),
    (r"(?<![\wА-Яа-яЁё])standard(?![\wА-Яа-яЁё])|(?<![\wА-Яа-яЁё])стандарт(?![\wА-Яа-яЁё])", "standard"),
    (r"(?<![\wА-Яа-яЁё])tft(?![\wА-Яа-яЁё])", "tft"),
    (r"(?<![\wА-Яа-яЁё])ips(?![\wА-Яа-яЁё])", "ips"),
    (r"(?<![\wА-Яа-яЁё])lcd(?![\wА-Яа-яЁё])", "lcd"),
    (r"(?<![\wА-Яа-яЁё])premium(?![\wА-Яа-яЁё])", "premium"),
    (r"(?<![\wА-Яа-яЁё])hq(?![\wА-Яа-яЁё])", "hq"),
    (r"(?<![\wА-Яа-яЁё])копия(?![\wА-Яа-яЁё])", "копия"),
    (r"(?<![\wА-Яа-яЁё])оригинал(?![\wА-Яа-яЁё])", "оригинал"),
]


def norm_label(quality_label: str) -> str:
    if not quality_label:
        return ""
    low = quality_label.lower()
    for pattern, canon in _LABEL_CANON:
        if re.search(pattern, low):
            return canon
    # fallback: убрать скобки, сжать пробелы
    s = re.sub(r"[()\[\]]", " ", low)
    s = " ".join(s.split())
    return s[:40]


# ---------------------------------------------------------------------- #
# Learned
# ---------------------------------------------------------------------- #
class Learned:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = list(rows)

        self._scores: dict[tuple[str, str], float] = {}
        self._strong_negs: dict[tuple[str, str], int] = {}
        self._positives: dict[tuple[str, str], int] = {}
        # (brand, model) -> [(ts, query), ...]
        self._model_queries: dict[tuple[str, str], list[tuple[str, str]]] = {}
        # множество уникальных текстов запросов, которые привели к выбору
        self._unique_queries: set[str] = set()

        for r in self.rows:
            self._consume_row(r)

    # ------------------------------------------------------------------ #
    # Внутренняя обработка строки журнала
    # ------------------------------------------------------------------ #
    def _consume_row(self, r: dict) -> None:
        wbq = (r.get("wb_quality") or "").strip()
        label = norm_label(r.get("moba_quality_label") or "")
        decision = (r.get("decision") or "").strip()

        if wbq and label:
            key = (wbq, label)
            score = SCORES.get(decision, 0.0)
            self._scores[key] = self._scores.get(key, 0.0) + score
            if decision in STRONG_NEGATIVES:
                self._strong_negs[key] = self._strong_negs.get(key, 0) + 1
            if decision in POSITIVES:
                self._positives[key] = self._positives.get(key, 0) + 1

        # model_queries — только по успешным решениям
        if decision in POSITIVES:
            q = (r.get("query") or "").strip()
            if not q:
                return
            brand = (r.get("wb_brand") or "").lower()
            models_raw = r.get("wb_models") or "[]"
            try:
                models = (
                    json.loads(models_raw)
                    if isinstance(models_raw, str)
                    else list(models_raw or [])
                )
            except Exception:
                models = []
            ts = r.get("ts") or ""
            for model in models:
                mkey = (brand, str(model).lower())
                self._model_queries.setdefault(mkey, []).append((ts, q))
            self._unique_queries.add(q)

    # ------------------------------------------------------------------ #
    # Публичный API
    # ------------------------------------------------------------------ #
    def label_score(self, wb_quality: str, moba_quality_label: str) -> float:
        label = norm_label(moba_quality_label)
        if not wb_quality or not label:
            return 0.0
        return self._scores.get((wb_quality, label), 0.0)

    def is_blocked(self, wb_quality: str, moba_quality_label: str) -> bool:
        label = norm_label(moba_quality_label)
        if not wb_quality or not label:
            return False
        key = (wb_quality, label)
        return (
            self._strong_negs.get(key, 0) >= 3
            and self._positives.get(key, 0) == 0
        )

    def model_queries(self, wb_brand: str, wb_model: str) -> list[str]:
        brand = (wb_brand or "").lower()
        model = (wb_model or "").lower()
        if not model:
            return []
        entries = self._model_queries.get((brand, model), [])
        # свежие первыми
        entries_sorted = sorted(entries, key=lambda x: x[0], reverse=True)
        seen: set[str] = set()
        out: list[str] = []
        for _, q in entries_sorted:
            if q in seen:
                continue
            seen.add(q)
            out.append(q)
            if len(out) >= 3:
                break
        return out

    def summary(self) -> str:
        titles = {"copy": "Копия", "oled": "OLED", "original": "Оригинал"}

        # сгруппируем по wb_quality
        grouped: dict[str, list[tuple[str, float, bool, int]]] = {}
        for (wbq, label), score in self._scores.items():
            blocked = self.is_blocked(wbq, label)
            negs = self._strong_negs.get((wbq, label), 0)
            grouped.setdefault(wbq, []).append((label, score, blocked, negs))

        lines: list[str] = []
        for wbq in ("copy", "oled", "original"):
            entries = grouped.get(wbq)
            if not entries:
                continue
            entries_sorted = sorted(entries, key=lambda x: x[1], reverse=True)
            parts = []
            for label, score, blocked, negs in entries_sorted:
                if blocked:
                    parts.append(f"{label} 🚫 ({negs} отказов)")
                else:
                    parts.append(f"{label} {_fmt_score(score)}")
            lines.append(f"{titles[wbq]}: " + " · ".join(parts))

        # неизвестные категории (на всякий случай)
        for wbq, entries in grouped.items():
            if wbq in ("copy", "oled", "original"):
                continue
            entries_sorted = sorted(entries, key=lambda x: x[1], reverse=True)
            parts = []
            for label, score, blocked, negs in entries_sorted:
                if blocked:
                    parts.append(f"{label} 🚫 ({negs} отказов)")
                else:
                    parts.append(f"{label} {_fmt_score(score)}")
            lines.append(f"{wbq}: " + " · ".join(parts))

        total_queries = len(self._unique_queries)
        total_rows = len(self.rows)

        week_ago = (
            datetime.now(timezone.utc) - timedelta(days=7)
        ).isoformat()
        week_count = sum(
            1 for r in self.rows if (r.get("ts") or "") >= week_ago
        )

        lines.append(f"Своих запросов запомнено: {total_queries}")
        lines.append(
            f"Решений всего: {total_rows} (за 7 дней: {week_count})"
        )
        return "\n".join(lines)


def _fmt_score(score: float) -> str:
    if abs(score - round(score)) < 1e-9:
        v_int = int(round(score))
        return f"+{v_int}" if v_int >= 0 else f"−{abs(v_int)}"
    s = f"{abs(score):.2f}".rstrip("0").rstrip(".")
    return f"+{s}" if score >= 0 else f"−{s}"
