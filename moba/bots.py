# bots.py
"""
Telegram-хендлеры и логика обработки заказа WB.
Один OwnerService на одного owner (anri / gor).
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
from typing import Optional, TYPE_CHECKING

import aiosqlite
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from learning import Learned
from training import register_training
from matcher import classify_moba, find_candidates, parse_wb_title
from moba_client import MobaClient
from moba_order import MobaOrderer, OrderRequest
from storage import now_iso
from wb_orders import SHOPS, WbOrder
from wb_supply import WbSupply, WbSupplyError, supply_name_for_today

if TYPE_CHECKING:
    from storage import Storage
    from wb_content import WbContent
    from wb_orders import WbPoller

logger = logging.getLogger(__name__)

SHOPS_BY_KEY = {s["key"]: s for s in SHOPS}

_FINAL_STATES = {"done", "declined", "failed", "unknown"}


def token_for_shop(shop_key: str) -> str:
    for s in SHOPS:
        if s["key"] == shop_key:
            return os.getenv(s["env"], "").strip()
    return ""


def shop_title_by_key(shop_key: str) -> str:
    s = SHOPS_BY_KEY.get(shop_key)
    return s["title"] if s else shop_key


class OwnerService:
    def __init__(
        self,
        owner: str,
        bot_token: str,
        tg_id: Optional[int],
        moba_client: MobaClient,
        moba_orderer: MobaOrderer,
        storage: "Storage",
        poller: "WbPoller",
        wb_content: "WbContent",
        wb_supply: WbSupply,
        dry_run: bool,
    ) -> None:
        self.owner = owner
        tg_proxy = os.getenv("TG_PROXY", "").strip()
        if tg_proxy:
            from aiogram.client.session.aiohttp import AiohttpSession
            self.bot = Bot(token=bot_token, session=AiohttpSession(proxy=tg_proxy))
            logger.info("Бот %s ходит в Telegram через прокси %s", owner, tg_proxy)
        else:
            self.bot = Bot(token=bot_token)
        self.tg_id = tg_id
        self.moba_client = moba_client
        self.moba_orderer = moba_orderer
        self.storage = storage
        self.poller = poller
        self.wb_content = wb_content
        self.wb_supply = wb_supply
        self.dry_run = dry_run
        self.learned: Learned = Learned([])

        self.dp = Dispatcher()
        self.router = Router()
        self.dp.include_router(self.router)
        self._pending_query: dict[int, int] = {}
        self._register_handlers()

    # ------------------------------------------------------------------ #
    # Регистрация хендлеров
    # ------------------------------------------------------------------ #
    def _register_handlers(self) -> None:
        router = self.router
        register_training(router, self)  # «🎯 Тренировка» — раньше общих хендлеров
        try:  # приём прайсов не должен ронять бота (21.10: упал из-за отсутствия pdfplumber)
            from price_intake import register_price_intake
            register_price_intake(router, self)  # прайсы поставщиков файлом в чат (только бот Анри)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Приём прайсов отключён: %s", exc)

        @router.message(Command("start"))
        async def cmd_start(m: Message) -> None:
            uid = m.from_user.id if m.from_user else 0
            if self.tg_id is None:
                env_name = "TG_ANRI_ID" if self.owner == "anri" else "TG_GOR_ID"
                logger.info(
                    "впиши этот id в .env: owner=%s %s=%d",
                    self.owner, env_name, uid,
                )
                try:
                    await m.answer(
                        f"Ваш Telegram id: {uid}\n"
                        f"Впишите его в .env как {env_name}"
                    )
                except Exception:
                    pass
                return
            if uid != self.tg_id:
                return
            mode = "LIVE" if not self.dry_run else "DRY_RUN"
            await m.answer(
                f"Привет! Owner={self.owner}, режим={mode}, id={uid}"
            )

        @router.message(Command("status"))
        async def cmd_status(m: Message) -> None:
            if not self._is_owner(m):
                return
            counts = await self.storage.count_tasks_by_state_today()
            lines = [f"{k}: {v}" for k, v in sorted(counts.items())] or ["(пусто)"]
            mode = "LIVE" if not self.dry_run else "DRY_RUN"
            await m.answer(f"Режим: {mode}\nЗа сегодня:\n" + "\n".join(lines))

        @router.message(Command("links"))
        async def cmd_links(m: Message) -> None:
            if not self._is_owner(m):
                return
            links = await self.storage.list_links(20)
            if not links:
                await m.answer("Связок нет")
                return
            lines = []
            for L in links:
                lines.append(
                    f"{L['shop_key']} nmId={L['nm_id']} → "
                    f"{L['supplier_title']} ({L['supplier_url']})"
                )
            await m.answer("\n".join(lines)[:3900])

        @router.message(Command("unlink"))
        async def cmd_unlink(m: Message) -> None:
            if not self._is_owner(m):
                return
            parts = (m.text or "").split()
            if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
                await m.answer("Использование: /unlink <nmId>")
                return
            nm_id = int(parts[1])
            n = await self.storage.delete_link(nm_id)
            await m.answer(f"Удалено связок: {n}")

        @router.message(Command("supply"))
        async def cmd_supply(m: Message) -> None:
            if not self._is_owner(m):
                return
            parts = (m.text or "").split()
            if len(parts) < 2 or not parts[1].isdigit():
                await m.answer("Использование: /supply <wb_order_id>")
                return
            wb_order_id = int(parts[1])
            task = await self._find_task_by_wb_order(wb_order_id)
            if task is None:
                await m.answer(f"Задача для WB №{wb_order_id} не найдена")
                return
            if task["state"] != "done":
                await m.answer(
                    f"Задача WB №{wb_order_id}: состояние "
                    f"{task['state']}, перенос невозможен"
                )
                return
            rj = self._parse_result(task)
            if rj.get("status") != "placed":
                await m.answer(
                    f"Заказ WB №{wb_order_id} не был оформлен на moba"
                )
                return
            supply_val = rj.get("supply")
            if supply_val not in ("pending", "skipped"):
                await m.answer(
                    f"Заказ WB №{wb_order_id}: supply={supply_val!r}, "
                    f"перенос невозможен"
                )
                return
            await self._act_supply(task)

        @router.message(Command("learn"))
        async def cmd_learn(m: Message) -> None:
            if not self._is_owner(m):
                return
            await self._reload_learned()
            await m.answer((self.learned.summary() or "")[:3900] or "(пока нет решений)")

        @router.message(Command("learn_export"))
        async def cmd_learn_export(m: Message) -> None:
            if not self._is_owner(m):
                return
            rows = await self.storage.feedback_rows(owner=self.owner)
            if not rows:
                await m.answer("Пока нет решений")
                return
            from pathlib import Path
            Path("debug").mkdir(exist_ok=True)
            path = Path("debug") / f"match_feedback_{self.owner}.csv"
            cols = list(rows[0].keys())
            with path.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                w.writerows(rows)
            await m.answer_document(FSInputFile(str(path)))

        @router.message()
        async def on_message(m: Message) -> None:
            if not self._is_owner(m):
                return
            uid = m.from_user.id if m.from_user else 0
            task_id = self._pending_query.pop(uid, None)
            if task_id is None:
                return
            query = (m.text or "").strip()
            if not query:
                await m.answer("Пустой запрос, повтори.")
                return
            task = await self.storage.get_task(task_id)
            if task is None or task["state"] != "choose_link":
                await m.answer("Задача уже неактуальна.")
                return
            meta = self._meta(task)
            wb_type = parse_wb_title(meta.get("wb_title") or "").get("type") or "дисплей"
            if not query.lower().startswith(wb_type):
                query = f"{wb_type} для {query}"
            await self._show_choose_link(task_id, query, from_wb=False)

        @router.callback_query()
        async def on_callback(cq: CallbackQuery) -> None:
            if cq.from_user is None or cq.from_user.id != self.tg_id:
                try:
                    await cq.answer("Нет доступа", show_alert=False)
                except Exception:
                    pass
                return
            data = cq.data or ""
            parts = data.split(":")
            if len(parts) < 2:
                await cq.answer()
                return
            action = parts[0]
            try:
                task_id = int(parts[1])
            except ValueError:
                await cq.answer()
                return
            try:
                await cq.answer()  # сразу, иначе Telegram показывает «загрузку» и таймаут
            except Exception:
                pass
            try:
                await self._on_action(action, task_id, parts, cq)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Ошибка обработки callback %s: %s", data, exc)
                await self._safe_send(f"⚠️ Ошибка обработки действия: {exc}")

    def _is_owner(self, m: Message) -> bool:
        if self.tg_id is None or m.from_user is None:
            return False
        return m.from_user.id == self.tg_id

    # ------------------------------------------------------------------ #
    # Внешний API сервиса
    # ------------------------------------------------------------------ #
    async def start_polling(self) -> None:
        logger.info("Бот %s стартует polling", self.owner)
        try:
            await self.bot.delete_webhook(drop_pending_updates=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete_webhook не удался: %s", exc)
        await self.dp.start_polling(self.bot)

    async def close(self) -> None:
        try:
            await self.bot.session.close()
        except Exception:
            pass
        try:
            await self.moba_client.close()
        except Exception:
            pass
        try:
            await self.moba_orderer.close()
        except Exception:
            pass

    async def recover(self) -> None:
        await self._reload_learned()
        try:
            n = await self._reset_stuck_supply()
            if n:
                await self._safe_send(
                    f"⚠️ Перенос в сборку прервался перезапуском ({n} шт.). "
                    f"Повтори командой /supply <номер заказа WB>"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reset_stuck_supply: %s", exc)
        try:
            rows = await self.storage.find_dispatched_without_task()
            for shop_key, wb_order_id in rows:
                shop = SHOPS_BY_KEY.get(shop_key)
                if shop is None or shop["owner"] != self.owner:
                    continue
                await self._safe_send(
                    f"⚠️ Заказ WB №{wb_order_id} остался необработанным "
                    f"после перезапуска, проверь вручную"
                )
                await self.storage.insert_task_unknown(
                    shop_key, wb_order_id, self.owner
                )

            tasks = await self.storage.find_tasks_in_state("ordering")
            for t in tasks:
                if t["owner"] != self.owner:
                    continue
                await self._safe_send(
                    f"⚠️ Заказ WB №{t['wb_order_id']} остался необработанным "
                    f"после перезапуска, проверь вручную"
                )
                await self.storage.update_task_state(t["task_id"], "unknown")
                try:
                    await self.poller.set_status(
                        t["shop_key"], t["wb_order_id"], "unknown"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("set_status при recover: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("recover упал для %s: %s", self.owner, exc)

    # ------------------------------------------------------------------ #
    # Обработка нового заказа WB
    # ------------------------------------------------------------------ #
    async def handle_new_order(self, order: WbOrder) -> None:
        try:
            task_id = await self.storage.get_or_create_task(
                order.shop_key, order.wb_order_id, order.owner
            )
            if task_id is None:
                logger.info(
                    "Задача для %s/%s уже есть, пропуск",
                    order.shop_key, order.wb_order_id,
                )
                return

            wb_token = token_for_shop(order.shop_key)
            wb_title: Optional[str] = None
            try:
                wb_title = await self.wb_content.get_title(
                    order.shop_key, order.nm_id, wb_token
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("wb_content.get_title упал: %s", exc)
            if not wb_title:
                wb_title = order.article or f"nmId {order.nm_id}"

            sale, sale_src = None, ""
            try:
                from unit_econ import seller_price
                sale, sale_src = await seller_price(wb_token, order.nm_id, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("seller_price: %s", exc)
            if sale is None and order.price_rub is not None:
                sale, sale_src = order.price_rub, "≈ из заказа"
            meta = {
                "sale_price": sale,
                "sale_src": sale_src,
                "wb_title": wb_title,
                "wb_article": order.article,
                "wb_nm_id": order.nm_id,
                "wb_price_rub": order.price_rub,
                "shop_title": order.shop_title,
            }
            await self.storage.update_task_state(
                task_id, "choose_link",
                offer_json=json.dumps(meta, ensure_ascii=False),
            )

            link = await self.storage.get_link(order.shop_key, order.nm_id)
            if link is None:
                await self._show_choose_link(task_id, wb_title, from_wb=True)
            else:
                fresh = await self.storage.get_task(task_id)
                if fresh is not None:
                    await self._check_and_offer(fresh)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "handle_new_order упал для %s/%s: %s",
                order.shop_key, order.wb_order_id, exc,
            )
            await self._safe_send(
                f"⚠️ Ошибка при обработке заказа №{order.wb_order_id}: {exc}"
            )

    # ------------------------------------------------------------------ #
    # Шаг 3: выбор связки
    # ------------------------------------------------------------------ #
    async def _show_choose_link(
        self,
        task_id: int,
        query: str,
        from_wb: bool = False,
        show_all: bool = False,
    ) -> None:
        task = await self.storage.get_task(task_id)
        if task is None:
            return

        # Запоминаем запрос человека (для обучения); поиск по названию WB не запоминаем
        meta = self._meta(task)
        meta["last_query"] = None if from_wb else query
        await self.storage.update_task_state(
            task_id, offer_json=json.dumps(meta, ensure_ascii=False)
        )
        task = await self.storage.get_task(task_id) or task

        # 1. Мгновенно показываем «ищу»
        try:
            await self._edit_or_send(
                task,
                self._order_header(task) + "\n\n🔎 Ищу на moba…",
                None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Не удалось показать «ищу»: %s", exc)

        # 2. Определяем, как искать
        parsed = parse_wb_title(query)
        use_candidates = from_wb or bool(parsed.get("models"))

        items: list[dict] = []
        hidden = 0
        skip_reason = None
        if use_candidates:
            try:
                res = await find_candidates(
                    self.moba_client, query, show_all=show_all,
                    learned=self.learned,
                )
                items = res.get("items", [])
                hidden = int(res.get("hidden", 0))
                skip_reason = res.get("skip_reason")
            except Exception as exc:  # noqa: BLE001
                logger.exception("find_candidates упал: %s", exc)
                items = []
                hidden = 0

        # fallback: обычный поиск, как раньше
        if not items and not use_candidates:
            try:
                raw = await self.moba_client.search(query, 8)
            except Exception as exc:  # noqa: BLE001
                logger.warning("search moba упал: %s", exc)
                raw = []
            for r in raw:
                items.append({
                    "url": r.get("url") or "",
                    "name": r.get("name") or "",
                    "price": None,
                    "article": None,
                    "product_id": None,
                    "mitino": "unknown",
                    "quality": "unknown",
                    "quality_label": "",
                    "frame": None,
                })

        # 3. Сохраняем варианты
        choices = [{"url": it["url"], "name": it["name"]} for it in items]
        await self.storage.save_choices(task_id, choices)
        await self.storage.update_task_state(task_id, "choose_link")

        # 4. Текст
        header = self._order_header(task)
        if items:
            lines = [header, "", "Товар ещё не связан с moba. Выбери подходящий:"]
            for i, it in enumerate(items, start=1):
                mark = "✅" if it.get("mitino") == "yes" else "❌"
                price = it.get("price")
                price_s = f"{price} ₽" if price is not None else "—"
                star = "⭐ " if it.get("recommended") else ""
                lines.append(f"{star}{i}. {it['name']} — {price_s} · Митино {mark}")
            if hidden > 0:
                lines.append(
                    f"Скрыто {hidden} вариантов другого качества (OLED/оригинал)"
                )
            text = "\n".join(lines)
        else:
            text = (
                f"{header}\n\n"
                + (f"ℹ️ {skip_reason}." if skip_reason else
                   f"На moba ничего не нашлось по запросу «{query}».")
            )
        # Нет на moba в Митино — ищем у Мастер Мобайла и на poisk123 (только чтение)
        if from_wb and not any(it.get("mitino") == "yes" for it in items):
            try:
                from suppliers_ext import find_elsewhere, format_elsewhere
                ext = await asyncio.wait_for(find_elsewhere(query), timeout=90)
                text += "\n\n" + format_elsewhere(ext)
            except Exception as exc:  # noqa: BLE001
                logger.warning("find_elsewhere: %s", exc)
                text += "\n\n⚠️ Не удалось проверить Мастер Мобайл / poisk123"

        # 5. Клавиатура
        kb = self._kb_choose(task_id, items, hidden, show_all)
        await self._replace_or_send(task, text, kb)

    def _kb_choose(
        self,
        task_id: int,
        items: list[dict],
        hidden: int = 0,
        show_all: bool = False,
    ) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for idx, it in enumerate(items):
            price = it.get("price")
            price_s = f"{price} ₽" if price is not None else "—"
            ql = (it.get("quality_label") or "").strip()
            if not ql:
                ql = (it.get("quality") or "").strip() or "?"
            ql = ql[:16]
            parts = [price_s, ql]
            if it.get("frame") is True:
                parts.append("рамка")
            mark = "✅" if it.get("mitino") == "yes" else "❌"
            parts.append(mark)
            star = "⭐ " if it.get("recommended") else ""
            label = f"{star}{idx + 1}. " + " · ".join(parts)
            if len(label) > 60:
                label = label[:60]
            rows.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"choose:{task_id}:{idx}",
                )
            ])

        if hidden > 0 and not show_all:
            rows.append([
                InlineKeyboardButton(
                    text="👁 Показать все варианты",
                    callback_data=f"showall:{task_id}",
                )
            ])

        if items:
            rows.append([
                InlineKeyboardButton(
                    text="🚫 Ничего не подходит",
                    callback_data=f"none:{task_id}",
                )
            ])
        rows.append([
            InlineKeyboardButton(
                text="🔎 Другой запрос",
                callback_data=f"other_query:{task_id}",
            )
        ])
        rows.append([
            InlineKeyboardButton(
                text="❌ Пропустить",
                callback_data=f"skip:{task_id}",
            )
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    # ------------------------------------------------------------------ #
    # Шаг 4: проверка стока
    # ------------------------------------------------------------------ #
    async def _check_and_offer(
        self, task: dict, cq: Optional[CallbackQuery] = None
    ) -> None:
        meta = self._meta(task)
        nm_id = meta.get("wb_nm_id")
        link = await self.storage.get_link(task["shop_key"], nm_id)
        if link is None:
            await self._show_choose_link(
                task["task_id"], meta.get("wb_title") or ""
                , from_wb=True
            )
            return

        url = link.get("supplier_url") or ""
        if not url:
            await self._show_choose_link(
                task["task_id"], meta.get("wb_title") or ""
                , from_wb=True
            )
            return

        product = None
        try:
            product = await self.moba_client.get_product(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_product упал: %s", exc)
            product = None

        status = "unknown"
        try:
            status = await self.moba_client.mitino_status(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mitino_status упал: %s", exc)
            status = "unknown"

        header = self._order_header(task)

        if product is None:
            await self.storage.update_task_state(task["task_id"], "no_stock")
            text = header + "\n\n⚠️ Не удалось получить карточку moba"
            kb = self._kb_no_stock(task["task_id"])
            await self._replace_or_send(task, text, kb, cq)
            return

        if status == "yes":
            new_meta = dict(meta)
            new_meta.update({
                "url": url,
                "product_id": product.product_id,
                "title": product.name,
                "article": product.article,
                "price": product.price,
            })
            await self.storage.update_task_state(
                task["task_id"], "await_confirm",
                offer_json=json.dumps(new_meta, ensure_ascii=False),
            )
            try:
                from unit_econ import profit_line
                pl = profit_line(meta.get("sale_price") or meta.get("wb_price_rub"),
                                 meta.get("sale_src") or "", product.price, task["shop_key"])
            except Exception:  # noqa: BLE001
                pl = ""
            text = (
                f"{header}\n\n"
                f"moba: {product.name}\n"
                f"арт. {product.article} · {product.price} ₽ · "
                f"Митино: в наличии ✅\n"
                + (f"💰 {pl}\n" if pl else "") +
                f"Заказываем в Митино (самовывоз, оплата при получении)?"
            )
            kb = self._kb_confirm(task["task_id"])
            await self._replace_or_send(task, text, kb, cq)
        elif status == "no":
            await self.storage.update_task_state(task["task_id"], "no_stock")
            text = header + f"\n\n❌ В Митино нет: {product.name}"
            kb = self._kb_no_stock(task["task_id"])
            await self._replace_or_send(task, text, kb, cq)
        else:
            await self.storage.update_task_state(task["task_id"], "no_stock")
            text = (
                header
                + "\n\n⚠️ Не удалось определить наличие, проверь вручную"
            )
            kb = self._kb_no_stock(task["task_id"])
            await self._replace_or_send(task, text, kb, cq)

    def _kb_confirm(self, task_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да", callback_data=f"yes:{task_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Нет", callback_data=f"no:{task_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔁 Другой товар",
                    callback_data=f"other_product:{task_id}",
                ),
            ],
        ])

    def _kb_no_stock(self, task_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 Другой товар",
                    callback_data=f"other_product:{task_id}",
                ),
                InlineKeyboardButton(
                    text="🔄 Проверить снова",
                    callback_data=f"check_again:{task_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Пропустить",
                    callback_data=f"skip:{task_id}",
                ),
            ],
        ])

    def _kb_retry(self, task_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Попробовать снова",
                    callback_data=f"retry:{task_id}",
                ),
            ],
        ])

    def _kb_supply(self, task_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 В сборку",
                    callback_data=f"supply:{task_id}",
                ),
                InlineKeyboardButton(
                    text="Не сейчас",
                    callback_data=f"nosupply:{task_id}",
                ),
            ],
        ])

    # ------------------------------------------------------------------ #
    # Действия callback
    # ------------------------------------------------------------------ #
    async def _on_action(
        self, action: str, task_id: int, parts: list[str], cq: CallbackQuery
    ) -> None:
        task = await self.storage.get_task(task_id)
        if task is None:
            try:
                await cq.answer("Задача не найдена")
            except Exception:
                pass
            return

        if action == "choose":
            if len(parts) < 3:
                return
            try:
                idx = int(parts[2])
            except ValueError:
                return
            await self._act_choose(task, idx)
        elif action == "showall":
            await self._act_showall(task)
        elif action == "other_query":
            await self._act_other_query(task, cq)
        elif action == "none":
            await self._act_none(task)
        elif action == "skip" or action == "no":
            await self._act_skip(task)
        elif action == "yes":
            await self._act_yes(task)
        elif action == "other_product":
            await self._act_other_product(task)
        elif action == "check_again":
            await self._act_check_again(task)
        elif action == "retry":
            await self._act_retry(task)
        elif action == "supply":
            await self._act_supply(task)
        elif action == "nosupply":
            await self._act_nosupply(task)

    async def _act_choose(self, task: dict, idx: int) -> None:
        if task["state"] != "choose_link":
            return
        choices = await self.storage.get_choices(task["task_id"])
        if idx < 0 or idx >= len(choices):
            return
        ch = choices[idx]
        meta = self._meta(task)
        nm_id = meta.get("wb_nm_id")
        if nm_id is None:
            return
        for i, c in enumerate(choices):
            await self._feedback(task, c.get("url"), c.get("name"),
                                 "chosen" if i == idx else "passed")
        await self._reload_learned()
        await self.storage.save_link(
            task["shop_key"], int(nm_id),
            ch.get("url") or "", None,
            ch.get("name") or "", None,
        )
        fresh = await self.storage.get_task(task["task_id"])
        if fresh is not None:
            await self._check_and_offer(fresh)

    async def _act_showall(self, task: dict) -> None:
        if task["state"] != "choose_link":
            return
        meta = self._meta(task)
        wb_title = meta.get("wb_title") or ""
        if not wb_title:
            return
        await self._show_choose_link(
            task["task_id"], wb_title, from_wb=True, show_all=True
        )

    async def _act_none(self, task: dict) -> None:
        if task["state"] != "choose_link":
            return
        for c in await self.storage.get_choices(task["task_id"]):
            await self._feedback(task, c.get("url"), c.get("name"), "rejected")
        await self.storage.save_choices(task["task_id"], [])
        await self._reload_learned()
        if self.tg_id is not None:
            self._pending_query[self.tg_id] = task["task_id"]
        await self._replace_or_send(
            task,
            self._order_header(task)
            + "\n\n🚫 Учёл: ничего не подходит.\nПришли текст для поиска на moba "
              "(например: redmi 9a optima):",
            self._kb_choose(task["task_id"], [], 0, True),
        )

    async def _act_other_query(
        self, task: dict, cq: CallbackQuery
    ) -> None:
        if task["state"] != "choose_link":
            return
        if self.tg_id is not None:
            self._pending_query[self.tg_id] = task["task_id"]
        await self._send_task_message(
            task["task_id"], "Пришли текст для поиска на moba:"
        )

    async def _act_skip(self, task: dict) -> None:
        if task["state"] == "ordering" or task["state"] in _FINAL_STATES:
            return
        await self.storage.update_task_state(task["task_id"], "declined")
        try:
            await self.poller.set_status(
                task["shop_key"], task["wb_order_id"], "declined"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_status declined: %s", exc)
        text = self._order_header(task) + "\n\n❌ Пропущено"
        await self._replace_or_send(task, text, None)

    async def _act_other_product(self, task: dict) -> None:
        if task["state"] == "ordering" or task["state"] in _FINAL_STATES:
            return
        meta = self._meta(task)
        nm_id = meta.get("wb_nm_id")
        if task["state"] == "await_confirm" and meta.get("url"):
            # человек увидел товар и сказал «не тот» — сильный минус
            await self._feedback(task, meta.get("url"), meta.get("title"), "declined")
            await self._reload_learned()
        if nm_id is not None:
            await self.storage.delete_link_one(task["shop_key"], int(nm_id))
        await self._show_choose_link(
            task["task_id"], meta.get("wb_title") or ""
            , from_wb=True
        )

    async def _act_check_again(self, task: dict) -> None:
        if task["state"] != "no_stock":
            return
        await self._check_and_offer(task)

    async def _act_retry(self, task: dict) -> None:
        if task["state"] != "failed":
            return
        await self._check_and_offer(task)

    async def _act_yes(self, task: dict) -> None:
        claimed = await self.storage.claim_task(
            task["task_id"], "await_confirm", "ordering"
        )
        if not claimed:
            return
        meta = self._meta(task)
        try:
            await self._feedback(task, meta.get("url"), meta.get("title"), "confirmed")
            await self._reload_learned()
        except Exception as exc:  # noqa: BLE001
            logger.warning("feedback confirmed: %s", exc)
        fresh = await self.storage.get_task(task["task_id"])
        if fresh is not None:
            await self._edit_or_send(fresh, self._order_header(fresh) + "\n\n⏳ Оформляю…", None)
        await self._do_place_order(fresh or task)

    # ------------------------------------------------------------------ #
    # Шаг 5: оформление
    # ------------------------------------------------------------------ #
    async def _do_place_order(self, task: dict) -> None:
        meta = self._meta(task)
        product_id = int(meta.get("product_id") or 0)
        offer_price = int(meta.get("price") or 0)
        title_hint = meta.get("title") or ""
        shop_title = meta.get("shop_title") or shop_title_by_key(task["shop_key"])

        if not product_id or not offer_price:
            await self.storage.update_task_state(task["task_id"], "failed")
            try:
                await self.poller.set_status(
                    task["shop_key"], task["wb_order_id"], "failed"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("set_status failed: %s", exc)
            await self._safe_send(
                f"⚠️ Не удалось оформить заказ №{task['wb_order_id']}: "
                f"в задаче нет product_id/price"
            )
            return

        req = OrderRequest(
            request_id=f"wb-{task['shop_key']}-{task['wb_order_id']}",
            product_id=product_id,
            quantity=1,
            expected_price=offer_price,
            title_hint=title_hint,
            comment=f"WB {shop_title} №{task['wb_order_id']}",
        )

        try:
            result = await self.moba_orderer.place_order(req)
        except Exception as exc:  # noqa: BLE001
            logger.exception("place_order упал: %s", exc)
            await self.storage.update_task_state(
                task["task_id"], "failed",
                result_json=json.dumps(
                    {"error": str(exc)}, ensure_ascii=False
                ),
            )
            try:
                await self.poller.set_status(
                    task["shop_key"], task["wb_order_id"], "failed"
                )
            except Exception as exc2:  # noqa: BLE001
                logger.warning("set_status failed: %s", exc2)
            await self._safe_send(
                f"⚠️ Ошибка при оформлении заказа №{task['wb_order_id']}: {exc}"
            )
            return

        result_dict: dict = {
            "status": result.status,
            "order_number": result.order_number,
            "total": result.total,
            "message": result.message,
            "screenshot_path": result.screenshot_path,
        }
        if result.status == "placed":
            new_state = "done"
            result_dict["supply"] = "pending"
        elif result.status == "dry_run_ok":
            new_state = "done"
        elif result.status == "rejected":
            new_state = "failed"
        else:
            new_state = "unknown"

        await self.storage.update_task_state(
            task["task_id"], new_state,
            result_json=json.dumps(result_dict, ensure_ascii=False),
        )
        try:
            await self.poller.set_status(
                task["shop_key"], task["wb_order_id"], new_state
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_status %s: %s", new_state, exc)

        fresh = await self.storage.get_task(task["task_id"])
        target = fresh or task
        header = self._order_header(target) + "\n\n"

        if result.status == "placed":
            supply_name = supply_name_for_today()
            text = header + (
                f"✅ Заказано на moba, заказ №{result.order_number}, "
                f"{result.total} ₽. Забрать в Митино.\n\n"
                f"📦 Перенести в сборку «{supply_name}»?"
            )
            kb = self._kb_supply(target["task_id"])
            await self._edit_or_send(target, text, kb)
        elif result.status == "dry_run_ok":
            text = header + (
                f"🧪 Пробный режим: всё проверено, заказ НЕ отправлен "
                f"({result.total} ₽).\n"
                f"В боевом режиме здесь будет кнопка переноса в сборку."
            )
            await self._edit_or_send(target, text, None)
        elif result.status == "rejected":
            text = header + f"❌ Не заказано: {result.message}"
            kb = self._kb_retry(target["task_id"])
            await self._edit_or_send(target, text, kb)
        else:
            text = header + f"⚠️ {result.message}"
            await self._edit_or_send(target, text, None)
            if result.screenshot_path:
                try:
                    await self._send_photo(
                        result.screenshot_path, caption=None
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Не удалось отправить скриншот: %s", exc
                    )

    # ------------------------------------------------------------------ #
    # Перенос в сборку WB
    # ------------------------------------------------------------------ #
    def _supply_base_text(self, task: dict, rj: dict) -> str:
        return (
            self._order_header(task) + "\n\n"
            f"✅ Заказано на moba, заказ №{rj.get('order_number')}, "
            f"{rj.get('total')} ₽. Забрать в Митино."
        )

    async def _act_supply(self, task: dict) -> None:
        if task["state"] != "done":
            return
        rj = self._parse_result(task)
        if rj.get("status") != "placed":
            return

        # Атомарно: supply pending/skipped -> moving (двойное нажатие не пройдёт)
        if not await self._claim_supply_moving(task["task_id"]):
            return

        base = self._supply_base_text(task, rj)
        await self._edit_or_send(task, base + "\n\n⏳ Переношу в сборку…", None)

        async def back_to_pending(reason: str) -> None:
            rj["supply"] = "pending"
            await self.storage.update_task_state(
                task["task_id"],
                result_json=json.dumps(rj, ensure_ascii=False),
            )
            await self._edit_or_send(
                task,
                base + f"\n\n⚠️ Не удалось перенести в сборку: {reason}",
                self._kb_supply(task["task_id"]),
            )

        token = token_for_shop(task["shop_key"])
        if not token:
            await back_to_pending(f"нет токена WB для {task['shop_key']}")
            return

        try:
            supply_id, name = await self.wb_supply.move_to_today(
                token, int(task["wb_order_id"])
            )
        except WbSupplyError as exc:
            await back_to_pending(str(exc)[:1500])
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("move_to_today упал: %s", exc)
            await back_to_pending(str(exc)[:1500])
            return

        rj["supply"] = {"id": supply_id, "name": name}
        await self.storage.update_task_state(
            task["task_id"],
            result_json=json.dumps(rj, ensure_ascii=False),
        )
        await self._edit_or_send(
            task, base + f"\n\n📦 В сборке «{name}» ({supply_id})", None
        )

    async def _act_nosupply(self, task: dict) -> None:
        if task["state"] != "done":
            return
        async with aiosqlite.connect(self.storage.db_path) as db:
            cur = await db.execute(
                "UPDATE tasks SET "
                "result_json = json_set(result_json, '$.supply', 'skipped'), "
                "updated_at = ? "
                "WHERE task_id = ? AND state = 'done' "
                "AND json_extract(result_json, '$.supply') = 'pending'",
                (now_iso(), int(task["task_id"])),
            )
            await db.commit()
            if (cur.rowcount or 0) == 0:
                return
        rj = self._parse_result(task)
        await self._edit_or_send(
            task,
            self._supply_base_text(task, rj)
            + f"\nСборка — вручную (или /supply {task['wb_order_id']}).",
            None,
        )

    # ------------------------------------------------------------------ #
    # Вспомогательные: result_json, атомарный перевод supply
    # ------------------------------------------------------------------ #
    async def _reload_learned(self) -> None:
        try:
            self.learned = Learned(await self.storage.feedback_rows(owner=self.owner))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось загрузить обучение: %s", exc)

    async def _feedback(
        self, task: dict, url: Optional[str], name: Optional[str], decision: str
    ) -> None:
        meta = self._meta(task)
        wb = parse_wb_title(meta.get("wb_title") or "")
        cls = classify_moba(name or "")
        frame = cls.get("frame")
        await self.storage.add_feedback(
            owner=self.owner,
            shop_key=task["shop_key"],
            task_id=task["task_id"],
            nm_id=meta.get("wb_nm_id"),
            wb_title=meta.get("wb_title"),
            wb_type=wb.get("type"),
            wb_brand=wb.get("brand"),
            wb_models=json.dumps(wb.get("models") or [], ensure_ascii=False),
            wb_quality=wb.get("quality"),
            moba_url=url,
            moba_name=name,
            moba_quality=cls.get("quality"),
            moba_quality_label=cls.get("quality_label"),
            moba_frame=None if frame is None else int(frame),
            query=meta.get("last_query"),
            decision=decision,
        )

    def _parse_result(self, task: dict) -> dict:
        raw = task.get("result_json")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    async def _claim_supply_moving(self, task_id: int) -> bool:
        async with aiosqlite.connect(self.storage.db_path) as db:
            cur = await db.execute(
                "UPDATE tasks SET "
                "result_json = json_set(result_json, '$.supply', 'moving'), "
                "updated_at = ? "
                "WHERE task_id = ? "
                "AND state = 'done' "
                "AND json_extract(result_json, '$.status') = 'placed' "
                "AND json_extract(result_json, '$.supply') IN ('pending', 'skipped')",
                (now_iso(), int(task_id)),
            )
            await db.commit()
            return (cur.rowcount or 0) > 0

    async def _reset_stuck_supply(self) -> int:
        """После перезапуска: supply='moving' -> 'pending' (повтор безопасен:
        move_to_today сначала проверяет, не лежит ли заказ уже в поставке)."""
        async with aiosqlite.connect(self.storage.db_path) as db:
            cur = await db.execute(
                "UPDATE tasks SET "
                "result_json = json_set(result_json, '$.supply', 'pending'), "
                "updated_at = ? "
                "WHERE owner = ? AND state = 'done' "
                "AND json_extract(result_json, '$.supply') = 'moving'",
                (now_iso(), self.owner),
            )
            await db.commit()
            return cur.rowcount or 0

    async def _find_task_by_wb_order(
        self, wb_order_id: int
    ) -> Optional[dict]:
        async with aiosqlite.connect(self.storage.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM tasks WHERE owner=? AND wb_order_id=?",
                (self.owner, int(wb_order_id)),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # Вспомогательные методы работы с сообщениями
    # ------------------------------------------------------------------ #
    def _order_header(self, task: dict) -> str:
        meta = self._meta(task)
        shop_title = meta.get("shop_title") or shop_title_by_key(
            task["shop_key"]
        )
        wb_title = meta.get("wb_title") or "—"
        wb_article = meta.get("wb_article") or "—"
        wb_nm = meta.get("wb_nm_id")
        sale = meta.get("sale_price")
        if sale is None:
            sale = meta.get("wb_price_rub")
        price_str = f"{sale:,.0f} ₽".replace(",", " ") if sale is not None else "—"
        src = meta.get("sale_src") or ""
        return (
            f"🆕 Заказ WB · {shop_title} · №{task['wb_order_id']}\n"
            f"{wb_title} (арт. {wb_article}, nmId {wb_nm})\n"
            f"Продали: {price_str}" + (f" ({src})" if src else "")
        )

    def _meta(self, task: dict) -> dict:
        raw = task.get("offer_json")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    async def _safe_send(self, text: str) -> None:
        if self.tg_id is None:
            logger.warning("Нет TG id для %s, сообщение не отправлено", self.owner)
            return
        try:
            await self.bot.send_message(self.tg_id, text[:4000])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось отправить сообщение: %s", exc)

    async def _send_photo(
        self, path: str, caption: Optional[str]
    ) -> None:
        if self.tg_id is None:
            return
        await self.bot.send_photo(
            self.tg_id,
            FSInputFile(path),
            caption=caption[:1000] if caption else None,
        )

    async def _send_task_message(
        self, task_id: int, text: str, kb: Optional[InlineKeyboardMarkup] = None
    ) -> None:
        if self.tg_id is None:
            return
        try:
            sent = await self.bot.send_message(
                self.tg_id, text[:4000], reply_markup=kb
            )
            await self.storage.update_task_state(
                task_id,
                tg_chat_id=sent.chat.id,
                tg_message_id=sent.message_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось отправить сообщение задачи: %s", exc)

    async def _replace_or_send(
        self,
        task: dict,
        text: str,
        kb: Optional[InlineKeyboardMarkup],
        cq: Optional[CallbackQuery] = None,
    ) -> None:
        chat_id = task.get("tg_chat_id")
        message_id = task.get("tg_message_id")
        if cq is not None:
            try:
                await cq.message.edit_text(
                    text[:4000],
                    reply_markup=kb,
                )
                await self.storage.update_task_state(
                    task["task_id"],
                    tg_chat_id=cq.message.chat.id,
                    tg_message_id=cq.message.message_id,
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug("edit_text не удался, шлём новое: %s", exc)
        if chat_id and message_id:
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text[:4000],
                    reply_markup=kb,
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug("edit_message_text не удался: %s", exc)
        await self._send_task_message(task["task_id"], text, kb)

    async def _edit_or_send(
        self,
        task: dict,
        text: str,
        kb: Optional[InlineKeyboardMarkup],
    ) -> None:
        chat_id = task.get("tg_chat_id")
        message_id = task.get("tg_message_id")
        if chat_id and message_id:
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text[:4000],
                    reply_markup=kb,
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug("edit_message_text не удался: %s", exc)
        await self._send_task_message(task["task_id"], text, kb)
