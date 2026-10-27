# storage.py
"""
Работа с таблицами links, tasks, search_choices, wb_titles, match_feedback.
Плюс вспомогательные запросы к wb_orders / shop_state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_FEEDBACK_COLS = [
    "ts", "owner", "shop_key", "task_id", "nm_id",
    "wb_title", "wb_type", "wb_brand", "wb_models", "wb_quality",
    "moba_url", "moba_name", "moba_quality", "moba_quality_label",
    "moba_frame", "query", "decision",
]


class Storage:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._initialized = False

    # ------------------------------------------------------------------ #
    # Инициализация
    # ------------------------------------------------------------------ #
    async def init(self) -> None:
        if self._initialized:
            return
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS wb_orders (
                    shop_key      TEXT NOT NULL,
                    wb_order_id   INTEGER NOT NULL,
                    nm_id         INTEGER,
                    article       TEXT,
                    chrt_id       INTEGER,
                    sku           TEXT,
                    price_kop     INTEGER,
                    created_at    TEXT,
                    raw_json      TEXT,
                    first_seen_at TEXT NOT NULL,
                    status        TEXT NOT NULL,
                    PRIMARY KEY (shop_key, wb_order_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS shop_state (
                    shop_key      TEXT PRIMARY KEY,
                    baselined_at  TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS links (
                    shop_key            TEXT NOT NULL,
                    nm_id               INTEGER NOT NULL,
                    supplier            TEXT DEFAULT 'moba',
                    supplier_url        TEXT,
                    supplier_product_id INTEGER,
                    supplier_title      TEXT,
                    supplier_article    TEXT,
                    created_at          TEXT,
                    PRIMARY KEY (shop_key, nm_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_key       TEXT NOT NULL,
                    wb_order_id    INTEGER NOT NULL,
                    owner          TEXT NOT NULL,
                    state          TEXT NOT NULL,
                    offer_json     TEXT,
                    tg_chat_id     INTEGER,
                    tg_message_id  INTEGER,
                    result_json    TEXT,
                    updated_at     TEXT,
                    UNIQUE (shop_key, wb_order_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS search_choices (
                    task_id  INTEGER NOT NULL,
                    idx      INTEGER NOT NULL,
                    url      TEXT,
                    name     TEXT,
                    PRIMARY KEY (task_id, idx)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS wb_titles (
                    shop_key  TEXT NOT NULL,
                    nm_id     INTEGER NOT NULL,
                    title     TEXT,
                    PRIMARY KEY (shop_key, nm_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS match_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    shop_key TEXT NOT NULL,
                    task_id INTEGER,
                    nm_id INTEGER,
                    wb_title TEXT,
                    wb_type TEXT,
                    wb_brand TEXT,
                    wb_models TEXT,
                    wb_quality TEXT,
                    moba_url TEXT,
                    moba_name TEXT,
                    moba_quality TEXT,
                    moba_quality_label TEXT,
                    moba_frame INTEGER,
                    query TEXT,
                    decision TEXT NOT NULL
                )
                """
            )
            await db.commit()
        self._initialized = True

    # ------------------------------------------------------------------ #
    # links
    # ------------------------------------------------------------------ #
    async def get_link(self, shop_key: str, nm_id: Optional[int]) -> Optional[dict]:
        if nm_id is None:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM links WHERE shop_key=? AND nm_id=?",
                (shop_key, int(nm_id)),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def save_link(
        self,
        shop_key: str,
        nm_id: int,
        supplier_url: str,
        supplier_product_id: Optional[int],
        supplier_title: str,
        supplier_article: Optional[str],
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO links
                  (shop_key, nm_id, supplier, supplier_url,
                   supplier_product_id, supplier_title, supplier_article, created_at)
                VALUES (?, ?, 'moba', ?, ?, ?, ?, ?)
                """,
                (
                    shop_key,
                    int(nm_id),
                    supplier_url,
                    supplier_product_id,
                    supplier_title,
                    supplier_article,
                    now_iso(),
                ),
            )
            await db.commit()

    async def delete_link(self, nm_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM links WHERE nm_id=?", (int(nm_id),)
            )
            await db.commit()
            return cur.rowcount or 0

    async def delete_link_one(self, shop_key: str, nm_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM links WHERE shop_key=? AND nm_id=?",
                (shop_key, int(nm_id)),
            )
            await db.commit()
            return cur.rowcount or 0

    async def list_links(self, limit: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM links ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            )
            return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------ #
    # tasks
    # ------------------------------------------------------------------ #
    async def get_or_create_task(
        self, shop_key: str, wb_order_id: int, owner: str
    ) -> Optional[int]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT task_id FROM tasks WHERE shop_key=? AND wb_order_id=?",
                (shop_key, int(wb_order_id)),
            )
            row = await cur.fetchone()
            if row:
                return None
            cur = await db.execute(
                "INSERT INTO tasks (shop_key, wb_order_id, owner, state, updated_at) "
                "VALUES (?, ?, ?, 'choose_link', ?)",
                (shop_key, int(wb_order_id), owner, now_iso()),
            )
            await db.commit()
            return cur.lastrowid

    async def get_task(self, task_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM tasks WHERE task_id=?", (int(task_id),)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_task_state(
        self,
        task_id: int,
        state: Optional[str] = None,
        *,
        offer_json: Optional[str] = None,
        result_json: Optional[str] = None,
        tg_chat_id: Optional[int] = None,
        tg_message_id: Optional[int] = None,
    ) -> None:
        fields = ["updated_at=?"]
        params: list[Any] = [now_iso()]
        if state is not None:
            fields.append("state=?")
            params.append(state)
        if offer_json is not None:
            fields.append("offer_json=?")
            params.append(offer_json)
        if result_json is not None:
            fields.append("result_json=?")
            params.append(result_json)
        if tg_chat_id is not None:
            fields.append("tg_chat_id=?")
            params.append(int(tg_chat_id))
        if tg_message_id is not None:
            fields.append("tg_message_id=?")
            params.append(int(tg_message_id))
        params.append(int(task_id))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE task_id=?",
                params,
            )
            await db.commit()

    async def claim_task(
        self, task_id: int, from_state: str, to_state: str
    ) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE tasks SET state=?, updated_at=? "
                "WHERE task_id=? AND state=?",
                (to_state, now_iso(), int(task_id), from_state),
            )
            await db.commit()
            return (cur.rowcount or 0) > 0

    async def find_tasks_in_state(self, state: str) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM tasks WHERE state=?", (state,)
            )
            return [dict(r) for r in await cur.fetchall()]

    async def count_tasks_by_state_today(self) -> dict[str, int]:
        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT state, COUNT(*) FROM tasks WHERE updated_at >= ? "
                "GROUP BY state",
                (today.isoformat(),),
            )
            return {r[0]: int(r[1]) for r in await cur.fetchall()}

    async def insert_task_unknown(
        self, shop_key: str, wb_order_id: int, owner: str
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO tasks "
                "(shop_key, wb_order_id, owner, state, updated_at) "
                "VALUES (?, ?, ?, 'unknown', ?)",
                (shop_key, int(wb_order_id), owner, now_iso()),
            )
            await db.commit()

    # ------------------------------------------------------------------ #
    # search_choices
    # ------------------------------------------------------------------ #
    async def save_choices(self, task_id: int, choices: list[dict]) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM search_choices WHERE task_id=?", (int(task_id),)
            )
            for idx, ch in enumerate(choices):
                await db.execute(
                    "INSERT INTO search_choices (task_id, idx, url, name) "
                    "VALUES (?, ?, ?, ?)",
                    (int(task_id), idx, ch.get("url"), ch.get("name")),
                )
            await db.commit()

    async def get_choices(self, task_id: int) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT idx, url, name FROM search_choices WHERE task_id=? "
                "ORDER BY idx",
                (int(task_id),),
            )
            return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------ #
    # wb_titles
    # ------------------------------------------------------------------ #
    async def get_wb_title(self, shop_key: str, nm_id: int) -> Optional[str]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT title FROM wb_titles WHERE shop_key=? AND nm_id=?",
                (shop_key, int(nm_id)),
            )
            row = await cur.fetchone()
            return row[0] if row else None

    async def save_wb_title(
        self, shop_key: str, nm_id: int, title: str
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO wb_titles (shop_key, nm_id, title) "
                "VALUES (?, ?, ?)",
                (shop_key, int(nm_id), title),
            )
            await db.commit()

    # ------------------------------------------------------------------ #
    # wb_orders (вспомогательные)
    # ------------------------------------------------------------------ #
    async def find_dispatched_without_task(self) -> list[tuple[str, int]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT o.shop_key, o.wb_order_id
                FROM wb_orders o
                LEFT JOIN tasks t
                  ON t.shop_key = o.shop_key AND t.wb_order_id = o.wb_order_id
                WHERE o.status = 'dispatched' AND t.task_id IS NULL
                """
            )
            return [(str(r[0]), int(r[1])) for r in await cur.fetchall()]

    # ------------------------------------------------------------------ #
    # match_feedback
    # ------------------------------------------------------------------ #
    async def add_feedback(self, **fields: Any) -> None:
        fields.setdefault("ts", now_iso())
        values = [fields.get(c) for c in _FEEDBACK_COLS]
        placeholders = ",".join("?" * len(_FEEDBACK_COLS))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"INSERT INTO match_feedback "
                f"({','.join(_FEEDBACK_COLS)}) VALUES ({placeholders})",
                values,
            )
            await db.commit()

    async def feedback_rows(
        self, owner: Optional[str] = None, limit: Optional[int] = None
    ) -> list[dict]:
        sql = "SELECT * FROM match_feedback"
        params: list[Any] = []
        if owner:
            sql += " WHERE owner=?"
            params.append(owner)
        sql += " ORDER BY ts ASC, id ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, params)
            return [dict(r) for r in await cur.fetchall()]
