# training.py
"""
Режим «Тренировка»: обучить подбор moba по всем товарам магазина WB.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

import aiosqlite
import httpx
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from matcher import find_candidates, parse_wb_title
from wb_orders import SHOPS

if TYPE_CHECKING:
    from bots import OwnerService

logger = logging.getLogger(__name__)

DEBUG_DIR = Path("debug")
WB_CONTENT_URL = "https://content-api.wildberries.ru/content/v2/get/cards/list"

_SESSIONS: dict[str, "TrainingSession"] = {}

# Этапы тренировки (по категориям). enabled=False — этап ещё не готов (нужны другие поставщики).
CATEGORIES: list[dict] = [
    {"key": "disp", "title": "📱 Дисплеи", "words": ["дисплей", "экран"], "enabled": True},
    {"key": "back", "title": "🔙 Крышки", "words": ["крышка", "корпус"], "enabled": False},
    {"key": "parts", "title": "🔌 Шлейфы, платы, разъёмы",
     "words": ["шлейф", "плата", "разъем", "разъём", "коннектор"], "enabled": False},
    {"key": "batt", "title": "🔋 Аккумуляторы", "words": ["аккумулятор", "акб", "батарея"], "enabled": False},
]

# Карточки, загруженные при выборе магазина: owner -> (shop_key, cards)
_CARDS_CACHE: dict[str, tuple[str, list[dict]]] = {}


# Аксессуары, которые упоминают «экран»/«дисплей», но запчастью не являются
EXCLUDE_WORDS = ["пленк", "плёнк", "стекло защит", "защитное стекло", "защитн", "гидрогел", "чехол",
                 "кабель", "зарядное", "наушник", "держател", "бампер", "кольцо", "попсокет"]


def card_category(card: dict) -> Optional[str]:
    text = f"{card.get('subject') or ''} {card.get('title') or ''}".lower()
    title = (card.get("title") or "").lower()
    if any(w in title for w in EXCLUDE_WORDS):
        return None
    first = (card.get("title") or "").lower().split(" ", 1)[0]
    for cat in CATEGORIES:  # сначала по первому слову названия — оно точнее
        if any(first.startswith(w) for w in cat["words"]):
            return cat["key"]
    for cat in CATEGORIES:
        if any(w in text for w in cat["words"]):
            return cat["key"]
    return None


# ---------------------------------------------------------------------- #
# Список карточек магазина
# ---------------------------------------------------------------------- #
async def fetch_shop_cards(token: str, progress=None, text_search: Optional[str] = None,
                           on_page=None) -> list[dict]:
    """progress — необязательная async-функция progress(n_cards), вызывается каждые 5 страниц."""
    out: list[dict] = []
    seen_nm: set[int] = set()
    pages = 0
    cursor: dict[str, Any] = {"limit": 100}
    first = True
    async with httpx.AsyncClient(timeout=20.0) as client:
        while True:
            body = {
                "settings": {
                    "cursor": cursor,
                    "filter": ({"withPhoto": -1, "textSearch": text_search}
                               if text_search else {"withPhoto": -1}),
                }
            }
            headers = {
                "Authorization": token,
                "Content-Type": "application/json",
            }
            r = None
            for attempt in range(5):
                try:
                    r = await client.post(
                        WB_CONTENT_URL, headers=headers, json=body
                    )
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    logger.warning("fetch_shop_cards сеть: %s", exc)
                    await asyncio.sleep(2)
                    continue
                if r.status_code == 429:
                    await asyncio.sleep(10)
                    continue
                if 500 <= r.status_code < 600:
                    await asyncio.sleep(2)
                    continue
                break
            if r is None:
                raise RuntimeError("WB content API не отвечает")
            if r.status_code != 200:
                raise RuntimeError(
                    f"WB content API HTTP {r.status_code}: {r.text[:200]}"
                )
            data = r.json()
            if first:
                _save_sample(data)
                first = False

            cards = data.get("cards") or []
            new_on_page = 0
            for c in cards:
                nm = c.get("nmID") or c.get("nmId")
                if not nm or int(nm) in seen_nm:
                    continue
                seen_nm.add(int(nm))
                new_on_page += 1
                out.append({
                    "nm_id": int(nm),
                    "title": c.get("title") or "",
                    "vendor_code": c.get("vendorCode") or "",
                    "subject": c.get("subjectName") or "",
                })

            pages += 1
            if on_page is not None:
                try:
                    await on_page(out[-new_on_page:] if new_on_page else [])
                except Exception as exc:
                    logger.warning("on_page: %s", exc)
            if progress is not None and pages % 5 == 0:
                try:
                    await progress(len(out))
                except Exception:
                    pass
            if new_on_page == 0 or pages >= 300:
                break  # курсор не двигается или слишком много страниц — защита от зацикливания
            cur = data.get("cursor") or {}
            total = int(cur.get("total") or 0)
            if total < int(cursor["limit"]):
                break
            upd = cur.get("updatedAt")
            nm2 = cur.get("nmID")
            if not upd or not nm2:
                break
            cursor = {"limit": 100, "updatedAt": upd, "nmID": nm2}
            await asyncio.sleep(0.6)
    logger.info("Карточек загружено: %d за %d страниц", len(out), pages)
    return out


CARDS_CACHE_DIR = Path("data")
CARDS_CACHE_TTL = 12 * 3600


async def load_shop_cards(shop_key: str, token: str, progress=None, force: bool = False) -> list[dict]:
    """Карточки с кэшем на диске на 12 часов — второй запуск тренировки мгновенный."""
    import time
    path = CARDS_CACHE_DIR / f"cards_{shop_key}.json"
    if not force and path.exists() and time.time() - path.stat().st_mtime < CARDS_CACHE_TTL:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    cards = await fetch_shop_cards(token, progress)
    try:
        CARDS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("cards cache: %s", exc)
    return cards


def _save_sample(data: dict) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        p = DEBUG_DIR / "train_cards_sample.json"
        if not p.exists():
            with p.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("save train_cards_sample: %s", exc)


# ---------------------------------------------------------------------- #
# Очередь
# ---------------------------------------------------------------------- #
NOT_FOUND_RECHECK_DAYS = 7  # «нет на moba» перепроверяем раз в неделю — вдруг появился


async def _skip_db(db_path: str):
    db = await aiosqlite.connect(db_path)
    await db.execute(
        "CREATE TABLE IF NOT EXISTS train_skip (shop_key TEXT, nm_id INTEGER, reason TEXT, ts TEXT, "
        "PRIMARY KEY (shop_key, nm_id))"
    )
    return db


async def mark_skip(svc: Any, shop_key: str, nm_id: int, reason: str) -> None:
    """reason: user — человек нажал «Пропустить» (больше не показываем); not_found — на moba ничего нет."""
    from datetime import datetime, timezone
    try:
        db = await _skip_db(svc.storage.db_path)
        await db.execute("INSERT OR REPLACE INTO train_skip VALUES (?,?,?,?)",
                         (shop_key, int(nm_id), reason, datetime.now(timezone.utc).isoformat()))
        await db.commit()
        await db.close()
    except Exception as exc:
        logger.warning("mark_skip: %s", exc)


async def skipped_ids(svc: Any, shop_key: str) -> set[int]:
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=NOT_FOUND_RECHECK_DAYS)).isoformat()
    try:
        db = await _skip_db(svc.storage.db_path)
        cur = await db.execute(
            "SELECT nm_id FROM train_skip WHERE shop_key=? AND (reason='user' OR ts>=?)", (shop_key, since))
        ids = {int(r[0]) for r in await cur.fetchall()}
        await db.close()
        return ids
    except Exception as exc:
        logger.warning("skipped_ids: %s", exc)
        return set()


async def build_queue(
    svc: "OwnerService", shop_key: str, cards: list[dict]
) -> tuple[list[dict], int]:
    """Возвращает (queue, unclear_count)."""
    queue: list[dict] = []
    unclear = 0
    skip = await skipped_ids(svc, shop_key)
    for card in cards:
        nm_id = card["nm_id"]
        if nm_id in skip:
            continue  # уже пропускали / на moba нет
        try:
            link = await svc.storage.get_link(shop_key, nm_id)
        except Exception as exc:
            logger.warning("build_queue get_link: %s", exc)
            link = None
        if link is not None:
            continue
        parsed = parse_wb_title(card["title"])
        if not parsed.get("models"):
            unclear += 1
            continue
        queue.append(card)

    order_counts: dict[int, int] = {}
    try:
        async with aiosqlite.connect(svc.storage.db_path) as db:
            cur = await db.execute(
                "SELECT nm_id, COUNT(*) FROM wb_orders "
                "WHERE shop_key=? GROUP BY nm_id",
                (shop_key,),
            )
            for nm, cnt in await cur.fetchall():
                if nm:
                    order_counts[int(nm)] = int(cnt)
    except Exception as exc:
        logger.warning("build_queue order_counts: %s", exc)

    def key(c: dict):
        cnt = order_counts.get(c["nm_id"], 0)
        return (-cnt, c["title"])

    queue.sort(key=key)
    return queue, unclear


# ---------------------------------------------------------------------- #
# Сессия
# ---------------------------------------------------------------------- #
@dataclass
class TrainingSession:
    owner: str
    shop_key: str
    shop_title: str
    svc: Any
    queue: list[dict]
    total: int
    unclear: int = 0
    current: Optional[dict] = None
    done: int = 0
    skipped: int = 0
    last: deque = field(default_factory=lambda: deque(maxlen=20))
    message_id: Optional[int] = None
    awaiting_query: bool = False
    prefetch: dict[int, asyncio.Task] = field(default_factory=dict)
    trained_shown: bool = False
    loading: bool = False
    loader: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    auto_done: int = 0          # связано ботом без вопроса
    confident_seen: int = 0     # сколько было уверенных карточек (каждая 10-я — на контроль)
    control_total: int = 0
    not_found: int = 0
    nf_streak: int = 0
    control_hits: int = 0


def _should_show_trained(sess: TrainingSession) -> bool:
    return (not sess.trained_shown) and len(sess.last) == 20 and all(sess.last)


# ---------------------------------------------------------------------- #
# Обратная связь и обучение
# ---------------------------------------------------------------------- #
async def _feedback_safe(
    svc: Any, shop_key: str, card: dict, item: dict,
    query: Optional[str], decision: str,
) -> None:
    task = {
        "task_id": None,
        "shop_key": shop_key,
        "offer_json": json.dumps({
            "wb_title": card.get("title"),
            "wb_nm_id": card.get("nm_id"),
            "last_query": query or None,
        }, ensure_ascii=False),
    }
    try:
        await svc._feedback(
            task, item.get("url") or "", item.get("name") or "", decision
        )
    except Exception as exc:
        logger.warning("_feedback ошибка: %s", exc)


async def _reload_learned_safe(svc: Any) -> None:
    try:
        await svc._reload_learned()
    except Exception as exc:
        logger.warning("_reload_learned: %s", exc)


# ---------------------------------------------------------------------- #
# Сборка сообщения и клавиатуры
# ---------------------------------------------------------------------- #
def _build_message(
    sess: TrainingSession, cur: dict
) -> tuple[str, InlineKeyboardMarkup]:
    card = cur["card"]
    result = cur.get("result") or {}
    items = result.get("items") or []
    hidden = int(result.get("hidden", 0))

    pos = sess.done + sess.skipped + 1
    if pos > sess.total:
        pos = sess.total

    hits_n = sum(1 for x in sess.last if x)
    last_n = len(sess.last)
    pct = (hits_n * 100 // last_n) if last_n else 0

    header = (
        f"🎯 Тренировка · {sess.shop_title} · {pos} из {sess.total}{'+' if sess.loading else ''} · "
        f"попадание {hits_n}/{last_n} ({pct}%)"
    )
    if sess.auto_done:
        header += f" · ⚡ сам: {sess.auto_done}"
    if sess.not_found:
        header += f" · нет на moba: {sess.not_found}"
    if sess.control_total:
        header += f" · контроль {sess.control_hits}/{sess.control_total}"
    if cur.get("control"):
        header += "\n🔍 Контроль: бот уверен в варианте №1 — проверь его"
    wb_line = f"WB: {card.get('title') or ''} (nmId {card.get('nm_id')})"

    lines = [header, wb_line, ""]
    if items:
        for i, it in enumerate(items, start=1):
            star = "⭐ " if it.get("recommended") else ""
            price = it.get("price")
            price_s = f"{price} ₽" if price is not None else "—"
            lines.append(f"{star}{i}. {it['name']} — {price_s}")
        if hidden > 0:
            lines.append(f"Скрыто {hidden} вариантов другого качества")
    else:
        lines.append("На moba ничего не нашлось")

    text = "\n".join(lines)[:4000]

    rows: list[list[InlineKeyboardButton]] = []
    for i, it in enumerate(items):
        star = "⭐ " if it.get("recommended") else ""
        price = it.get("price")
        price_s = f"{price} ₽" if price is not None else "—"
        ql = (it.get("quality_label") or "").strip() or "?"
        ql = ql[:16]
        parts = [f"{star}{i+1}.", price_s, ql]
        if it.get("frame") is True:
            parts.append("рамка")
        label = " · ".join(parts)
        if len(label) > 60:
            label = label[:60]
        rows.append([
            InlineKeyboardButton(
                text=label, callback_data=f"tr:pick:{i}"
            )
        ])

    if hidden > 0 and not cur.get("show_all"):
        rows.append([
            InlineKeyboardButton(
                text="👁 Показать все", callback_data="tr:all"
            )
        ])
    if items:
        rows.append([
            InlineKeyboardButton(
                text="🚫 Ничего не подходит", callback_data="tr:none"
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="🔎 Свой запрос", callback_data="tr:query"
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="⏭ Пропустить", callback_data="tr:skip"
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="⏹ Стоп", callback_data="tr:stop"
        )
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _render(sess: TrainingSession) -> None:
    if sess.current is None:
        return
    text, kb = _build_message(sess, sess.current)
    try:
        await sess.svc.bot.edit_message_text(
            chat_id=sess.svc.tg_id,
            message_id=sess.message_id,
            text=text,
            reply_markup=kb,
        )
    except Exception as exc:
        logger.warning("render edit: %s", exc)


async def _render_trained(sess: TrainingSession) -> None:
    left = len(sess.queue)
    text = (
        f"🎯 20 из 20 подряд! Подбор для магазина {sess.shop_title} обучен.\n"
        f"Осталось карточек: {left}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="▶️ Продолжить", callback_data="tr:cont"),
            InlineKeyboardButton(text="⏹ Стоп", callback_data="tr:stop"),
        ]
    ])
    try:
        await sess.svc.bot.edit_message_text(
            chat_id=sess.svc.tg_id,
            message_id=sess.message_id,
            text=text,
            reply_markup=kb,
        )
    except Exception as exc:
        logger.warning("render_trained: %s", exc)


# ---------------------------------------------------------------------- #
# Prefetch
# ---------------------------------------------------------------------- #
async def _safe_find_candidates(svc: Any, title: str) -> dict:
    try:
        return await find_candidates(
            svc.moba_client,
            title,
            show_all=False,
            learned=getattr(svc, "learned", None),
            check_stock=False,
            max_queries=4,
        )
    except Exception as exc:
        logger.warning("prefetch find_candidates: %s", exc)
        return {"items": [], "hidden": 0, "wb": parse_wb_title(title)}


def _prefetch_next(sess: TrainingSession) -> None:
    for card in sess.queue[:2]:
        nm_id = card["nm_id"]
        if nm_id in sess.prefetch:
            continue
        t = asyncio.create_task(
            _safe_find_candidates(sess.svc, card["title"])
        )
        sess.prefetch[nm_id] = t


def _cancel_prefetch(sess: TrainingSession) -> None:
    for t in list(sess.prefetch.values()):
        if not t.done():
            t.cancel()
    sess.prefetch.clear()


# ---------------------------------------------------------------------- #
# Движение по очереди
# ---------------------------------------------------------------------- #
CONTROL_EVERY = 10      # каждую 10-ю уверенную карточку показываем человеку — так меряем точность автопилота
CONFIDENT_SCORE = 5.0   # минимум баллов у лучшего варианта
CONFIDENT_GAP = 10.0    # отрыв от второго: Incell и Optima у тебя почти равны — такие решай сам


def is_confident(svc: Any, result: dict) -> bool:
    """Бот уверен: лучший вариант набрал баллы и сильно оторвался от остальных."""
    items = (result or {}).get("items") or []
    learned = getattr(svc, "learned", None)
    if not items or learned is None:
        return False
    wq = ((result.get("wb") or {}).get("quality")) or "copy"
    if items[0].get("quality") == "unknown":
        return False
    # код модели на WB (RMX2185) не совпадает с кодами на moba — пусть решает человек
    from matcher import codes_conflict
    if codes_conflict(((result.get("wb") or {}).get("raw")) or "", items[0].get("name") or ""):
        return False
    scores = [learned.label_score(wq, it.get("quality_label") or "") for it in items]
    if wq == "copy":
        # копия: правило «самая дешёвая» — уверены, если №1 однозначно дешевле остальных и пометка не нелюбимая
        p0 = items[0].get("price")
        if items[0].get("quality") != "copy" or p0 is None or scores[0] < -2:
            return False
        others = [it.get("price") for it in items[1:] if it.get("price") is not None]
        return all(p0 < p for p in others)
    if scores[0] < CONFIDENT_SCORE:
        return False
    return len(scores) == 1 or scores[0] - max(scores[1:]) >= CONFIDENT_GAP


async def _auto_link(sess: TrainingSession, card: dict, result: dict) -> None:
    item = result["items"][0]
    await sess.svc.storage.save_link(sess.shop_key, card["nm_id"], item.get("url") or "", None, item.get("name") or "", None)
    try:
        await sess.svc.storage.save_wb_title(sess.shop_key, card["nm_id"], card.get("title") or "")
    except Exception:
        pass
    # «auto» не меняет баллы — бот не учится на собственных решениях
    await _feedback_safe(sess.svc, sess.shop_key, card, item, None, "auto")
    sess.auto_done += 1


async def _show_next(sess: TrainingSession) -> None:
    while True:
        waited = 0.0
        while not sess.queue and sess.loading and waited < 60:
            await asyncio.sleep(0.5)  # карточки ещё догружаются в фоне
            waited += 0.5
        if not sess.queue:
            await _finish(sess)
            return

        card = sess.queue.pop(0)
        nm_id = card["nm_id"]

        task = sess.prefetch.pop(nm_id, None)
        result = None
        if task is not None:
            try:
                result = await task
            except Exception as exc:
                logger.warning("await prefetch: %s", exc)
                result = None

        if result is None:
            result = await _safe_find_candidates(sess.svc, card["title"])

        if (result or {}).get("items"):
            sess.nf_streak = 0
        if not (result or {}).get("items") and not (result or {}).get("hidden") and sess.nf_streak < 15:
            # защита: 15 «не найдено» подряд — похоже, moba не отвечает, дальше не пропускаем сами
            sess.nf_streak += 1
            # на moba ничего нет — не спрашиваем, запоминаем и идём дальше
            await mark_skip(sess.svc, sess.shop_key, nm_id, "not_found")
            sess.not_found += 1
            _prefetch_next(sess)
            continue

        control = False
        if is_confident(sess.svc, result):
            sess.confident_seen += 1
            if sess.confident_seen % CONTROL_EVERY != 0:
                await _auto_link(sess, card, result)
                _prefetch_next(sess)
                if sess.auto_done % 5 == 0:
                    try:
                        await sess.svc.bot.edit_message_text(
                            chat_id=sess.svc.tg_id, message_id=sess.message_id,
                            text=(f"⚡ {sess.shop_title}: бот уверен — связал сам {sess.auto_done}. "
                                  f"Ищу карточки, где нужен ты…"),
                        )
                    except Exception:
                        pass
                continue
            control = True  # уверенная, но на контроль человеку
        break

    sess.current = {
        "card": card,
        "result": result,
        "query": None,
        "show_all": False,
        "original_view": True,
        "control": control,
    }
    sess.awaiting_query = False
    await _render(sess)
    _prefetch_next(sess)


async def _finish(sess: TrainingSession, edit_message: bool = True) -> None:
    _SESSIONS.pop(sess.owner, None)
    _cancel_prefetch(sess)
    if sess.loader is not None and not sess.loader.done():
        sess.loader.cancel()

    hits_n = sum(1 for x in sess.last if x)
    n = len(sess.last)
    pct = (hits_n * 100 // n) if n else 0
    text = (
        f"Тренировка {sess.shop_title}: связано {sess.done}, "
        f"пропущено {sess.skipped}, нет на moba: {sess.not_found}, бот связал сам: {sess.auto_done}, "
        f"попадание на последних 20: {pct}%, "
        f"непонятных названий: {sess.unclear}"
    )
    if edit_message:
        try:
            await sess.svc.bot.edit_message_text(
                chat_id=sess.svc.tg_id,
                message_id=sess.message_id,
                text=text,
                reply_markup=None,
            )
        except Exception as exc:
            logger.warning("finish edit: %s", exc)


# ---------------------------------------------------------------------- #
# Действия
# ---------------------------------------------------------------------- #
async def _do_pick(sess: TrainingSession, idx: int) -> None:
    cur = sess.current
    if cur is None:
        return
    items = (cur.get("result") or {}).get("items", [])
    if idx < 0 or idx >= len(items):
        return

    svc = sess.svc
    card = cur["card"]
    chosen = items[idx]

    hit = bool(cur.get("original_view", True)) and idx == 0
    sess.last.append(hit)
    if cur.get("control"):
        sess.control_total += 1
        sess.control_hits += int(hit)

    try:
        await svc.storage.save_link(
            sess.shop_key, card["nm_id"],
            chosen.get("url") or "", None,
            chosen.get("name") or "", None,
        )
        await svc.storage.save_wb_title(
            sess.shop_key, card["nm_id"], card.get("title") or ""
        )
    except Exception as exc:
        logger.warning("save_link: %s", exc)

    query_used = cur.get("query") or ""
    for i, it in enumerate(items):
        decision = "chosen" if i == idx else "passed"
        await _feedback_safe(
            svc, sess.shop_key, card, it, query_used, decision
        )

    sess.done += 1
    await _reload_learned_safe(svc)

    if _should_show_trained(sess):
        sess.trained_shown = True  # показать один раз, дальше «Продолжить» идёт без повторов
        await _render_trained(sess)
        return

    await _show_next(sess)


async def _do_show_all(sess: TrainingSession) -> None:
    cur = sess.current
    if cur is None:
        return
    card = cur["card"]
    try:
        result = await find_candidates(
            sess.svc.moba_client,
            card["title"],
            show_all=True,
            learned=getattr(sess.svc, "learned", None),
            check_stock=False,
            max_queries=4,
        )
    except Exception as exc:
        logger.warning("show_all: %s", exc)
        return
    cur["result"] = result
    cur["show_all"] = True
    cur["original_view"] = False
    await _render(sess)


async def _ask_for_query(sess: TrainingSession) -> None:
    sess.awaiting_query = True
    try:
        await sess.svc.bot.edit_message_text(
            chat_id=sess.svc.tg_id,
            message_id=sess.message_id,
            text=(f"🎯 {sess.shop_title} · WB: {(sess.current or {}).get('card', {}).get('title', '')}\n\n"
                  "Пришли текст для поиска на moba (например: redmi 9a optima):"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⏭ Пропустить", callback_data="tr:skip"),
                InlineKeyboardButton(text="⏹ Стоп", callback_data="tr:stop"),
            ]]),
        )
    except Exception as exc:
        logger.warning("ask_for_query: %s", exc)


async def _do_none(sess: TrainingSession) -> None:
    cur = sess.current
    if cur is None:
        return
    card = cur["card"]
    items = (cur.get("result") or {}).get("items", [])
    for it in items:
        await _feedback_safe(
            sess.svc, sess.shop_key, card, it,
            cur.get("query") or "", "rejected",
        )
    await _reload_learned_safe(sess.svc)
    await _ask_for_query(sess)


async def _do_query(sess: TrainingSession) -> None:
    await _ask_for_query(sess)


async def _do_skip(sess: TrainingSession) -> None:
    sess.skipped += 1
    if sess.current:
        await mark_skip(sess.svc, sess.shop_key, sess.current["card"]["nm_id"], "user")
    await _show_next(sess)


async def _apply_custom_query(sess: TrainingSession, text: str) -> None:
    cur = sess.current
    if cur is None:
        return
    sess.awaiting_query = False
    card = cur["card"]
    parsed = parse_wb_title(card["title"])
    type_ = parsed.get("type") or ""
    query = f"{type_} для {text}" if type_ else text

    try:
        result = await find_candidates(
            sess.svc.moba_client,
            query,
            show_all=False,
            learned=getattr(sess.svc, "learned", None),
            check_stock=False,
            max_queries=4,
        )
    except Exception as exc:
        logger.warning("custom query: %s", exc)
        result = {"items": [], "hidden": 0, "wb": parsed}

    cur["result"] = result
    cur["query"] = text
    cur["show_all"] = False
    cur["original_view"] = False
    await _render(sess)


# ---------------------------------------------------------------------- #
# Старт сессии
# ---------------------------------------------------------------------- #
async def _choose_category(svc: Any, cq: CallbackQuery, shop_key: str) -> None:
    """Сразу показываем этапы — карточки грузим только выбранного этапа и по ходу тренировки."""
    shop = next((s for s in SHOPS if s["key"] == shop_key), None)
    if not shop:
        return
    rows = []
    for cat in CATEGORIES:
        mark = "" if cat["enabled"] else " · скоро"
        rows.append([InlineKeyboardButton(
            text=f"{cat['title']}{mark}", callback_data=f"tr:cat:{shop_key}:{cat['key']}",
        )])
    try:
        await cq.message.edit_text(f"🎯 {shop['title']}: выбери этап", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception as exc:
        logger.warning("choose_category: %s", exc)


async def _start_session(svc: Any, cq: CallbackQuery, shop_key: str, cat_key: str = "disp") -> None:
    shop = next((s for s in SHOPS if s["key"] == shop_key), None)
    if shop is None:
        try:
            await cq.message.edit_text("Магазин не найден")
        except Exception:
            pass
        return
    token = os.getenv(shop["env"], "").strip()
    if not token:
        try:
            await cq.message.edit_text(
                f"У магазина «{shop['title']}» нет токена"
            )
        except Exception:
            pass
        return

    try:
        await cq.message.edit_text("⏳ Загружаю карточки…")
    except Exception:
        pass

    cat = next((c for c in CATEGORIES if c["key"] == cat_key), None)
    if cat is None:
        return
    if not cat["enabled"]:
        try:
            await cq.message.edit_text(
                f"{cat['title']}: этап ещё не готов — сначала доучим дисплеи и подключим нужных поставщиков."
            )
        except Exception:
            pass
        return
    sess = TrainingSession(
        owner=svc.owner,
        shop_key=shop_key,
        shop_title=f"{shop['title']} · {cat['title']}",
        svc=svc,
        queue=[],
        total=0,
        message_id=cq.message.message_id,
    )
    old = _SESSIONS.get(svc.owner)
    if old is not None:
        await _finish(old, edit_message=False)
    _SESSIONS[svc.owner] = sess
    await _reload_learned_safe(svc)

    import time
    cache_path = CARDS_CACHE_DIR / f"cards_{shop_key}_{cat_key}.json"
    cached_cards = None
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < CARDS_CACHE_TTL:
        try:
            cached_cards = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cached_cards = None

    async def add_cards(page: list[dict]) -> None:
        page = [c for c in page if card_category(c) == cat_key]
        if not page:
            return
        q, unclear = await build_queue(svc, shop_key, page)
        sess.unclear += unclear
        sess.queue.extend(q)
        sess.total += len(q)

    if cached_cards is not None:
        await add_cards(cached_cards)
    else:
        sess.loading = True
        collected: list[dict] = []

        async def on_page(page: list[dict]) -> None:
            collected.extend(page)
            await add_cards(page)

        async def loader() -> None:
            try:
                await fetch_shop_cards(token, text_search=cat["words"][0], on_page=on_page)
                if not collected:  # поиск по слову ничего не дал — берём всё и фильтруем сами
                    await fetch_shop_cards(token, on_page=on_page)
                CARDS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(collected, ensure_ascii=False), encoding="utf-8")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("loader: %s", exc)
            finally:
                sess.loading = False

        sess.loader = asyncio.create_task(loader())

    _SESSIONS[svc.owner] = sess
    await _reload_learned_safe(svc)
    await _show_next(sess)


# ---------------------------------------------------------------------- #
# Регистрация хендлеров
# ---------------------------------------------------------------------- #
def register_training(router: Router, svc: "OwnerService") -> None:
    @router.message(Command("train"))
    async def cmd_train(m: Message) -> None:
        if svc.tg_id is None or m.from_user is None or m.from_user.id != svc.tg_id:
            return
        rows: list[list[InlineKeyboardButton]] = []
        for shop in SHOPS:
            if shop["owner"] != svc.owner:
                continue
            token = os.getenv(shop["env"], "").strip()
            mark = "" if token else " (нет токена)"
            rows.append([
                InlineKeyboardButton(
                    text=f"{shop['title']}{mark}",
                    callback_data=f"tr:shop:{shop['key']}",
                )
            ])
        if not rows:
            await m.answer("Нет магазинов для тренировки")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await m.answer("Выбери магазин для тренировки:", reply_markup=kb)

    @router.message(Command("train_stop"))
    async def cmd_train_stop(m: Message) -> None:
        if svc.tg_id is None or m.from_user is None or m.from_user.id != svc.tg_id:
            return
        sess = _SESSIONS.pop(svc.owner, None)
        if sess is None:
            await m.answer("Активной тренировки нет")
            return
        _cancel_prefetch(sess)
        try:
            await svc.bot.edit_message_text(
                chat_id=svc.tg_id,
                message_id=sess.message_id,
                text="Тренировка остановлена",
                reply_markup=None,
            )
        except Exception:
            pass

    @router.callback_query(F.data.startswith("tr:"))
    async def on_tr(cq: CallbackQuery) -> None:
        try:
            await cq.answer()
        except Exception:
            pass
        if svc.tg_id is None or cq.from_user is None or cq.from_user.id != svc.tg_id:
            return
        data = cq.data or ""
        parts = data.split(":")
        if len(parts) < 2:
            return
        action = parts[1]

        if action == "shop":
            if len(parts) < 3:
                return
            await _choose_category(svc, cq, parts[2])
            return
        if action == "cat":
            if len(parts) < 4:
                return
            await _start_session(svc, cq, parts[2], parts[3])
            return

        sess = _SESSIONS.get(svc.owner)
        if sess is None:
            return
        if cq.message is not None and sess.message_id is not None:
            if cq.message.message_id != sess.message_id:
                return
        if sess.lock.locked():
            return  # прошлое нажатие ещё обрабатывается
        try:
          async with sess.lock:
            if action == "pick":
                if len(parts) < 3:
                    return
                try:
                    idx = int(parts[2])
                except ValueError:
                    return
                await _do_pick(sess, idx)
            elif action == "all":
                await _do_show_all(sess)
            elif action == "none":
                await _do_none(sess)
            elif action == "query":
                await _do_query(sess)
            elif action == "skip":
                await _do_skip(sess)
            elif action == "stop":
                await _finish(sess)
            elif action == "cont":
                await _show_next(sess)
        except Exception as exc:
            logger.exception("tr handler: %s", exc)
            try:
                await cq.message.edit_text(f"⚠️ Ошибка: {exc}")
            except Exception:
                pass

    def _awaits_query(m: Message) -> bool:
        if svc.tg_id is None or m.from_user is None:
            return False
        if m.from_user.id != svc.tg_id:
            return False
        if not m.text or m.text.startswith("/"):
            return False
        sess = _SESSIONS.get(svc.owner)
        # любой текст во время тренировки = свой запрос для текущей карточки (кнопку жать не обязательно)
        return bool(sess and sess.current)

    @router.message(_awaits_query)
    async def on_tr_query_text(m: Message) -> None:
        sess = _SESSIONS.get(svc.owner)
        if sess is None:
            return
        text = (m.text or "").strip()
        if not text:
            return
        try:
            await _apply_custom_query(sess, text)
        except Exception as exc:
            logger.exception("custom query: %s", exc)
