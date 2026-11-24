"""Persistencia ligera en SQLite asíncrono (aiosqlite) con migración automática segura.

Almacena lista blanca, miniaturas personalizadas, Modo Lotes (/auto), preset
favorito, marca de agua por usuario, ajustes del panel /admin y estadísticas.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite


@dataclass(slots=True)
class UserRecord:
    user_id: int
    username: str
    added_by: int
    added_at: int
    custom_thumb_file_id: str | None
    send_as_document: bool
    auto_mode: bool = False
    default_options_json: str | None = None
    watermark_text: str = ""
    watermark_pos: str = "br"


@dataclass(slots=True)
class StatsRecord:
    total_jobs: int
    total_link_deliveries: int
    total_input_bytes: int
    total_output_bytes: int
    total_saved_bytes: int


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(
        self,
        owner_id: int,
        default_access_mode: str,
        default_concurrent_jobs: int,
        default_max_threads: int,
        default_uploader: str,
    ) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")

        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                added_by INTEGER NOT NULL DEFAULT 0,
                added_at INTEGER NOT NULL,
                custom_thumb_file_id TEXT,
                send_as_document INTEGER NOT NULL DEFAULT 0,
                auto_mode INTEGER NOT NULL DEFAULT 0,
                default_options_json TEXT,
                watermark_text TEXT NOT NULL DEFAULT '',
                watermark_pos TEXT NOT NULL DEFAULT 'br'
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total_jobs INTEGER NOT NULL DEFAULT 0,
                total_link_deliveries INTEGER NOT NULL DEFAULT 0,
                total_input_bytes INTEGER NOT NULL DEFAULT 0,
                total_output_bytes INTEGER NOT NULL DEFAULT 0,
                total_saved_bytes INTEGER NOT NULL DEFAULT 0
            );
            """
        )

        # Migración segura de columnas nuevas sobre bases existentes (no pierde datos)
        await self._migrate_users_table()

        await self._conn.execute("INSERT OR IGNORE INTO stats (id) VALUES (1);")
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO users (user_id, username, added_by, added_at)
            VALUES (?, 'owner', ?, ?);
            """,
            (owner_id, owner_id, int(time.time())),
        )

        defaults = {
            "access_mode": default_access_mode,
            "max_concurrent_jobs": str(default_concurrent_jobs),
            "max_threads": str(default_max_threads),
            "large_file_uploader": default_uploader,
            "error_alerts": "1",
        }
        for key, value in defaults.items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);",
                (key, value),
            )
        await self._conn.commit()

    async def _migrate_users_table(self) -> None:
        async with self.conn.execute("PRAGMA table_info(users);") as cursor:
            existing = {str(r["name"]) for r in await cursor.fetchall()}

        migrations = {
            "auto_mode": "ALTER TABLE users ADD COLUMN auto_mode INTEGER NOT NULL DEFAULT 0;",
            "default_options_json": "ALTER TABLE users ADD COLUMN default_options_json TEXT;",
            "watermark_text": "ALTER TABLE users ADD COLUMN watermark_text TEXT NOT NULL DEFAULT '';",
            "watermark_pos": "ALTER TABLE users ADD COLUMN watermark_pos TEXT NOT NULL DEFAULT 'br';",
        }
        for col, sql in migrations.items():
            if col not in existing:
                await self.conn.execute(sql)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("La base de datos SQLite no está inicializada.")
        return self._conn

    # ── Ajustes globales ──────────────────────────────────────────────────────

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self.conn.execute(
            "SELECT value FROM settings WHERE key = ?;", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return str(row["value"]) if row else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value;
            """,
            (key, str(value)),
        )
        await self.conn.commit()

    async def get_all_settings(self) -> dict[str, str]:
        async with self.conn.execute("SELECT key, value FROM settings;") as cursor:
            rows = await cursor.fetchall()
            return {str(r["key"]): str(r["value"]) for r in rows}

    # ── Lista blanca y preferencias de usuario ────────────────────────────────

    async def is_authorized(self, user_id: int, owner_id: int) -> bool:
        if user_id == owner_id:
            return True
        access_mode = await self.get_setting("access_mode", "private")
        if access_mode == "public":
            return True
        async with self.conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?;", (user_id,)
        ) as cursor:
            return (await cursor.fetchone()) is not None

    async def add_user(
        self, user_id: int, added_by: int, username: str = ""
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO users (user_id, username, added_by, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = CASE WHEN excluded.username != '' THEN excluded.username ELSE users.username END;
            """,
            (user_id, username.lstrip("@"), added_by, int(time.time())),
        )
        await self.conn.commit()

    async def remove_user(self, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "DELETE FROM users WHERE user_id = ?;", (user_id,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def list_users(self) -> list[UserRecord]:
        async with self.conn.execute(
            """
            SELECT user_id, username, added_by, added_at,
                   custom_thumb_file_id, send_as_document,
                   auto_mode, default_options_json, watermark_text, watermark_pos
            FROM users
            ORDER BY added_at ASC;
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                UserRecord(
                    user_id=int(r["user_id"]),
                    username=str(r["username"] or ""),
                    added_by=int(r["added_by"]),
                    added_at=int(r["added_at"]),
                    custom_thumb_file_id=r["custom_thumb_file_id"],
                    send_as_document=bool(r["send_as_document"]),
                    auto_mode=bool(r["auto_mode"]),
                    default_options_json=r["default_options_json"],
                    watermark_text=str(r["watermark_text"] or ""),
                    watermark_pos=str(r["watermark_pos"] or "br"),
                )
                for r in rows
            ]

    async def get_user(self, user_id: int) -> UserRecord | None:
        async with self.conn.execute(
            """
            SELECT user_id, username, added_by, added_at,
                   custom_thumb_file_id, send_as_document,
                   auto_mode, default_options_json, watermark_text, watermark_pos
            FROM users WHERE user_id = ?;
            """,
            (user_id,),
        ) as cursor:
            r = await cursor.fetchone()
            if not r:
                return None
            return UserRecord(
                user_id=int(r["user_id"]),
                username=str(r["username"] or ""),
                added_by=int(r["added_by"]),
                added_at=int(r["added_at"]),
                custom_thumb_file_id=r["custom_thumb_file_id"],
                send_as_document=bool(r["send_as_document"]),
                auto_mode=bool(r["auto_mode"]),
                default_options_json=r["default_options_json"],
                watermark_text=str(r["watermark_text"] or ""),
                watermark_pos=str(r["watermark_pos"] or "br"),
            )

    async def ensure_user_profile(self, user_id: int, username: str = "") -> None:
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO users (user_id, username, added_by, added_at)
            VALUES (?, ?, 0, ?);
            """,
            (user_id, username.lstrip("@"), int(time.time())),
        )
        if username:
            await self.conn.execute(
                "UPDATE users SET username = ? WHERE user_id = ?;",
                (username.lstrip("@"), user_id),
            )
        await self.conn.commit()

    async def set_custom_thumb(self, user_id: int, file_id: str | None) -> None:
        await self.ensure_user_profile(user_id)
        await self.conn.execute(
            "UPDATE users SET custom_thumb_file_id = ? WHERE user_id = ?;",
            (file_id, user_id),
        )
        await self.conn.commit()

    async def get_custom_thumb(self, user_id: int) -> str | None:
        async with self.conn.execute(
            "SELECT custom_thumb_file_id FROM users WHERE user_id = ?;", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return str(row["custom_thumb_file_id"]) if row and row["custom_thumb_file_id"] else None

    async def set_send_as_document(self, user_id: int, send_as_doc: bool) -> None:
        await self.ensure_user_profile(user_id)
        await self.conn.execute(
            "UPDATE users SET send_as_document = ? WHERE user_id = ?;",
            (1 if send_as_doc else 0, user_id),
        )
        await self.conn.commit()

    async def get_send_as_document(self, user_id: int) -> bool:
        async with self.conn.execute(
            "SELECT send_as_document FROM users WHERE user_id = ?;", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row["send_as_document"]) if row else False

    async def set_auto_mode(self, user_id: int, enabled: bool) -> None:
        await self.ensure_user_profile(user_id)
        await self.conn.execute(
            "UPDATE users SET auto_mode = ? WHERE user_id = ?;",
            (1 if enabled else 0, user_id),
        )
        await self.conn.commit()

    async def save_default_options(self, user_id: int, options_dict: dict[str, Any]) -> None:
        await self.ensure_user_profile(user_id)
        payload = json.dumps(options_dict, ensure_ascii=False)
        await self.conn.execute(
            "UPDATE users SET default_options_json = ? WHERE user_id = ?;",
            (payload, user_id),
        )
        await self.conn.commit()

    async def get_default_options(self, user_id: int) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT default_options_json FROM users WHERE user_id = ?;", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row or not row["default_options_json"]:
                return None
            try:
                data = json.loads(str(row["default_options_json"]))
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None

    async def set_watermark(
        self, user_id: int, text: str, pos: str | None = None
    ) -> None:
        await self.ensure_user_profile(user_id)
        if pos is not None and pos in {"tl", "tr", "bl", "br"}:
            await self.conn.execute(
                "UPDATE users SET watermark_text = ?, watermark_pos = ? WHERE user_id = ?;",
                (text.strip()[:48], pos, user_id),
            )
        else:
            await self.conn.execute(
                "UPDATE users SET watermark_text = ? WHERE user_id = ?;",
                (text.strip()[:48], user_id),
            )
        await self.conn.commit()

    # ── Estadísticas acumuladas ───────────────────────────────────────────────

    async def record_job_completion(
        self,
        input_bytes: int,
        output_bytes: int,
        delivered_via_link: bool = False,
    ) -> None:
        saved = max(0, input_bytes - output_bytes)
        await self.conn.execute(
            """
            UPDATE stats
            SET total_jobs = total_jobs + 1,
                total_link_deliveries = total_link_deliveries + ?,
                total_input_bytes = total_input_bytes + ?,
                total_output_bytes = total_output_bytes + ?,
                total_saved_bytes = total_saved_bytes + ?
            WHERE id = 1;
            """,
            (1 if delivered_via_link else 0, input_bytes, output_bytes, saved),
        )
        await self.conn.commit()

    async def get_stats(self) -> StatsRecord:
        async with self.conn.execute(
            """
            SELECT total_jobs, total_link_deliveries,
                   total_input_bytes, total_output_bytes, total_saved_bytes
            FROM stats WHERE id = 1;
            """
        ) as cursor:
            r = await cursor.fetchone()
            if not r:
                return StatsRecord(0, 0, 0, 0, 0)
            return StatsRecord(
                total_jobs=int(r["total_jobs"]),
                total_link_deliveries=int(r["total_link_deliveries"]),
                total_input_bytes=int(r["total_input_bytes"]),
                total_output_bytes=int(r["total_output_bytes"]),
                total_saved_bytes=int(r["total_saved_bytes"]),
            )
