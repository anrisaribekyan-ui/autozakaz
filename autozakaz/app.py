# app.py
"""
Точка входа сервиса автозаказа.
- Загружает .env
- Инициализирует Storage, WbPoller, WbContent, WbSupply
- Поднимает OwnerService для каждого owner (anri, gor), у которого есть BOT_* и MOBA_*
- Запускает polling обоих ботов и WbPoller.run_forever через asyncio.gather
- Корректно завершается по Ctrl+C / SIGTERM
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from bots import OwnerService
from moba_client import MobaClient
from moba_order import MobaOrderer
from storage import Storage
from wb_content import WbContent
from wb_orders import SHOPS, WbOrder, WbPoller
from wb_supply import WbSupply

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "app.log"
DB_PATH = "data/autozakaz.db"


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    root.addHandler(fh)
    root.addHandler(ch)


logger = logging.getLogger("app")


class App:
    def __init__(self) -> None:
        self.storage = Storage(DB_PATH)
        self.poller = WbPoller(db_path=DB_PATH, rate_limit_sec=60.0)
        self.wb_content: Optional[WbContent] = None
        self.wb_supply: Optional[WbSupply] = None
        self.services: dict[str, OwnerService] = {}
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Построение сервисов
    # ------------------------------------------------------------------ #
    async def build(self) -> None:
        await self.storage.init()
        await self.poller.init()

        self.wb_content = WbContent(self.storage)
        self.wb_supply = WbSupply()

        dry_run = os.getenv("LIVE_ORDERS", "0") != "1"
        logger.info("Режим: %s", "DRY_RUN" if dry_run else "LIVE")

        # --- anri ---
        anri_login = os.getenv("MOBA_LOGIN", "").strip()
        anri_pass = os.getenv("MOBA_PASSWORD", "").strip()
        anri_bot = os.getenv("BOT_MAIN", "").strip()
        anri_id_raw = os.getenv("TG_ANRI_ID", "").strip()
        anri_id = int(anri_id_raw) if anri_id_raw.isdigit() else None

        if not anri_bot or not anri_login or not anri_pass:
            logger.warning(
                "Owner anri пропущен: нет BOT_MAIN или MOBA_LOGIN/MOBA_PASSWORD"
            )
        else:
            svc = await self._make_service(
                owner="anri",
                bot_token=anri_bot,
                tg_id=anri_id,
                login=anri_login,
                password=anri_pass,
                account_name="anri",
                dry_run=dry_run,
            )
            self.services["anri"] = svc

        # --- gor ---
        gor_login = os.getenv("MOBA_GOR_LOGIN", "").strip()
        gor_pass = os.getenv("MOBA_GOR_PASSWORD", "").strip()
        gor_bot = os.getenv("BOT_GOR", "").strip()
        gor_id_raw = os.getenv("TG_GOR_ID", "").strip()
        gor_id = int(gor_id_raw) if gor_id_raw.isdigit() else None

        if not gor_bot or not gor_login or not gor_pass:
            logger.warning(
                "Owner gor пропущен: нет BOT_GOR или "
                "MOBA_GOR_LOGIN/MOBA_GOR_PASSWORD"
            )
        else:
            svc = await self._make_service(
                owner="gor",
                bot_token=gor_bot,
                tg_id=gor_id,
                login=gor_login,
                password=gor_pass,
                account_name="gor",
                dry_run=dry_run,
            )
            self.services["gor"] = svc

        if not self.services:
            logger.error(
                "Ни один owner не сконфигурирован — сервис не сможет "
                "обрабатывать заказы"
            )

        for shop in SHOPS:
            has_owner = shop["owner"] in self.services
            logger.info(
                "Магазин %s (owner=%s) — %s",
                shop["title"], shop["owner"],
                "активен" if has_owner else "пропускается (нет сервиса)",
            )

    async def _make_service(
        self,
        owner: str,
        bot_token: str,
        tg_id: Optional[int],
        login: str,
        password: str,
        account_name: str,
        dry_run: bool,
    ) -> OwnerService:
        assert self.wb_content is not None
        assert self.wb_supply is not None
        moba_client = MobaClient(login, password, account_name)
        moba_orderer = MobaOrderer(
            login, password, account_name, dry_run=dry_run
        )
        svc = OwnerService(
            owner=owner,
            bot_token=bot_token,
            tg_id=tg_id,
            moba_client=moba_client,
            moba_orderer=moba_orderer,
            storage=self.storage,
            poller=self.poller,
            wb_content=self.wb_content,
            wb_supply=self.wb_supply,
            dry_run=dry_run,
        )
        return svc

    # ------------------------------------------------------------------ #
    # on_new
    # ------------------------------------------------------------------ #
    async def on_new(self, order: WbOrder) -> None:
        svc = self.services.get(order.owner)
        if svc is None:
            logger.warning(
                "Нет OwnerService для owner=%s, заказ %s/%s не обработан",
                order.owner, order.shop_key, order.wb_order_id,
            )
            return
        try:
            await svc.handle_new_order(order)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "handle_new_order упал для %s/%s: %s",
                order.shop_key, order.wb_order_id, exc,
            )

    # ------------------------------------------------------------------ #
    # Запуск
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        for owner, svc in self.services.items():
            try:
                await svc.moba_orderer.start()
                logger.info("MobaOrderer запущен для owner=%s", owner)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Не удалось запустить MobaOrderer для owner=%s: %s",
                    owner, exc,
                )
            try:
                await svc.recover()
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "recover упал для owner=%s: %s", owner, exc
                )

        tasks: list[asyncio.Task] = []

        tasks.append(
            asyncio.create_task(
                self.poller.run_forever(self.on_new),
                name="wb_poller",
            )
        )

        for owner, svc in self.services.items():
            tasks.append(
                asyncio.create_task(
                    svc.start_polling(),
                    name=f"bot_{owner}",
                )
            )

        stop_task = asyncio.create_task(
            self._stop_event.wait(), name="stop_event"
        )

        done, pending = await asyncio.wait(
            tasks + [stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in tasks:
            if not t.done():
                t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    def request_stop(self) -> None:
        if not self._stop_event.is_set():
            logger.info("Получен сигнал остановки")
            self._stop_event.set()

    # ------------------------------------------------------------------ #
    # Завершение
    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        for owner, svc in self.services.items():
            try:
                await svc.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Ошибка закрытия сервиса %s: %s", owner, exc)
        try:
            await self.poller.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ошибка закрытия poller: %s", exc)
        if self.wb_content is not None:
            try:
                await self.wb_content.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Ошибка закрытия wb_content: %s", exc)
        if self.wb_supply is not None:
            try:
                await self.wb_supply.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Ошибка закрытия wb_supply: %s", exc)


async def _amain() -> int:
    setup_logging()
    load_dotenv()

    app = App()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.request_stop)
        except NotImplementedError:
            pass

    try:
        await app.build()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка инициализации: %s", exc)
        return 2

    try:
        await app.run()
    finally:
        await app.close()

    logger.info("Остановлено")
    return 0


def main() -> int:
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
