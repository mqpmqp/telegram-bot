"""Persistent reliability state for the Telegram image-chart workflow."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ImageReliabilityStore:
    """SQLite-backed dedupe, session, and audit state."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self._migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._connect() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS telegram_image_events (
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  bot_id TEXT NOT NULL,
                  update_id TEXT,
                  chat_id TEXT NOT NULL,
                  message_id TEXT,
                  user_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_image_event_update
                  ON telegram_image_events(bot_id, update_id)
                  WHERE update_id IS NOT NULL AND update_id <> '';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_image_event_message
                  ON telegram_image_events(bot_id, chat_id, message_id)
                  WHERE message_id IS NOT NULL AND message_id <> '';

                CREATE TABLE IF NOT EXISTS telegram_image_hashes (
                  image_hash_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  bot_id TEXT NOT NULL,
                  chat_id TEXT NOT NULL,
                  user_id TEXT NOT NULL,
                  image_hash TEXT NOT NULL,
                  event_id INTEGER,
                  created_at TEXT NOT NULL,
                  UNIQUE(bot_id, chat_id, user_id, image_hash)
                );

                CREATE TABLE IF NOT EXISTS image_sessions (
                  session_id TEXT PRIMARY KEY,
                  trace_id TEXT NOT NULL UNIQUE,
                  platform TEXT NOT NULL,
                  bot_id TEXT NOT NULL,
                  chat_id TEXT NOT NULL,
                  user_id TEXT NOT NULL,
                  update_id TEXT,
                  image_message_id TEXT,
                  image_hash TEXT,
                  vision_provider TEXT,
                  vision_request_id TEXT,
                  candidate_json TEXT NOT NULL,
                  state TEXT NOT NULL,
                  runtime_idempotency_key TEXT NOT NULL UNIQUE,
                  candidate_reply_count INTEGER NOT NULL DEFAULT 0,
                  runtime_invocation_count INTEGER NOT NULL DEFAULT 0,
                  runtime_called_at TEXT,
                  runtime_result_hash TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  expires_at REAL NOT NULL,
                  UNIQUE(platform, bot_id, chat_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS image_audits (
                  trace_id TEXT PRIMARY KEY,
                  case_id TEXT NOT NULL,
                  telegram_user_id TEXT NOT NULL,
                  telegram_chat_id TEXT NOT NULL,
                  image_hash TEXT,
                  vision_provider TEXT,
                  vision_request_id TEXT,
                  candidate_pillars TEXT NOT NULL,
                  confirmed_pillars TEXT,
                  runtime_called_at TEXT,
                  runtime_result_hash TEXT,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                """
            )

    def claim_event(
        self,
        *,
        bot_id: str,
        update_id: str | None,
        chat_id: str,
        message_id: str | None,
        user_id: str,
        event_type: str,
    ) -> int | None:
        now = _now()
        try:
            with self._connect() as database:
                cursor = database.execute(
                    """
                    INSERT INTO telegram_image_events(
                      bot_id, update_id, chat_id, message_id, user_id,
                      event_type, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'PROCESSING', ?, ?)
                    """,
                    (
                        str(bot_id),
                        str(update_id) if update_id not in (None, "") else None,
                        str(chat_id),
                        str(message_id) if message_id not in (None, "") else None,
                        str(user_id),
                        event_type,
                        now,
                        now,
                    ),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def complete_event(self, event_id: int, status: str) -> None:
        with self._connect() as database:
            database.execute(
                "UPDATE telegram_image_events SET status=?, updated_at=? WHERE event_id=?",
                (status, _now(), event_id),
            )

    def claim_image_hash(
        self,
        *,
        bot_id: str,
        chat_id: str,
        user_id: str,
        image_hash: str,
        event_id: int | None,
    ) -> bool:
        try:
            with self._connect() as database:
                database.execute(
                    """
                    INSERT INTO telegram_image_hashes(
                      bot_id, chat_id, user_id, image_hash, event_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (bot_id, chat_id, user_id, image_hash, event_id, _now()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def release_image_hash(
        self,
        *,
        bot_id: str,
        chat_id: str,
        user_id: str,
        image_hash: str,
        event_id: int,
    ) -> bool:
        with self._connect() as database:
            cursor = database.execute(
                """
                DELETE FROM telegram_image_hashes
                WHERE bot_id=? AND chat_id=? AND user_id=?
                  AND image_hash=? AND event_id=?
                """,
                (bot_id, chat_id, user_id, image_hash, event_id),
            )
        return cursor.rowcount == 1

    def save_session(self, record: Mapping[str, Any]) -> None:
        candidate = record["candidate"]
        values = (
            record["session_id"],
            record["trace_id"],
            record["platform"],
            record["bot_id"],
            record["chat_id"],
            record["user_id"],
            record.get("update_id"),
            record.get("image_message_id"),
            record.get("image_hash"),
            record.get("vision_provider"),
            record.get("vision_request_id"),
            _json(candidate),
            record["state"],
            record["runtime_idempotency_key"],
            int(record.get("candidate_reply_count", 0)),
            int(record.get("runtime_invocation_count", 0)),
            record["created_at"],
            record["updated_at"],
            float(record["expires_at"]),
        )
        with self._connect() as database:
            database.execute(
                """
                INSERT INTO image_sessions(
                  session_id, trace_id, platform, bot_id, chat_id, user_id,
                  update_id, image_message_id, image_hash, vision_provider,
                  vision_request_id, candidate_json, state,
                  runtime_idempotency_key, candidate_reply_count,
                  runtime_invocation_count, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, bot_id, chat_id, user_id) DO UPDATE SET
                  session_id=excluded.session_id,
                  trace_id=excluded.trace_id,
                  update_id=excluded.update_id,
                  image_message_id=excluded.image_message_id,
                  image_hash=excluded.image_hash,
                  vision_provider=excluded.vision_provider,
                  vision_request_id=excluded.vision_request_id,
                  candidate_json=excluded.candidate_json,
                  state=excluded.state,
                  runtime_idempotency_key=excluded.runtime_idempotency_key,
                  candidate_reply_count=excluded.candidate_reply_count,
                  runtime_invocation_count=excluded.runtime_invocation_count,
                  created_at=excluded.created_at,
                  updated_at=excluded.updated_at,
                  expires_at=excluded.expires_at
                """,
                values,
            )
            database.execute(
                """
                INSERT INTO image_audits(
                  trace_id, case_id, telegram_user_id, telegram_chat_id,
                  image_hash, vision_provider, vision_request_id,
                  candidate_pillars, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                  candidate_pillars=excluded.candidate_pillars,
                  status=excluded.status,
                  updated_at=excluded.updated_at
                """,
                (
                    record["trace_id"],
                    record["session_id"],
                    record["user_id"],
                    record["chat_id"],
                    record.get("image_hash"),
                    record.get("vision_provider"),
                    record.get("vision_request_id"),
                    _json(candidate),
                    record["state"],
                    record["created_at"],
                    record["updated_at"],
                ),
            )

    def load_sessions(self, *, now: float) -> list[dict[str, Any]]:
        with self._connect() as database:
            rows = database.execute(
                """
                SELECT * FROM image_sessions
                WHERE expires_at > ?
                  AND state NOT IN ('CANCELLED', 'EXPIRED', 'REJECTED', 'REPLACED')
                ORDER BY created_at
                """,
                (float(now),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "session_id": row["session_id"],
                    "trace_id": row["trace_id"],
                    "platform": row["platform"],
                    "bot_id": row["bot_id"],
                    "chat_id": row["chat_id"],
                    "user_id": row["user_id"],
                    "update_id": row["update_id"],
                    "image_message_id": row["image_message_id"],
                    "image_hash": row["image_hash"],
                    "vision_provider": row["vision_provider"],
                    "vision_request_id": row["vision_request_id"],
                    "candidate": json.loads(row["candidate_json"]),
                    "state": row["state"],
                    "runtime_idempotency_key": row["runtime_idempotency_key"],
                    "candidate_reply_count": row["candidate_reply_count"],
                    "runtime_invocation_count": row["runtime_invocation_count"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "expires_at": row["expires_at"],
                }
            )
        return result

    def delete_session(self, session_id: str, *, status: str) -> None:
        now = _now()
        with self._connect() as database:
            database.execute(
                """
                UPDATE image_sessions SET state=?, updated_at=?
                WHERE session_id=? AND state NOT IN ('COMPLETED', 'FAILED')
                """,
                (status, now, session_id),
            )
            database.execute(
                """
                UPDATE image_audits SET status=?, updated_at=?
                WHERE case_id=? AND status NOT IN ('COMPLETED', 'FAILED')
                """,
                (status, now, session_id),
            )

    def claim_runtime(self, session_id: str, idempotency_key: str) -> bool:
        now = _now()
        with self._connect() as database:
            cursor = database.execute(
                """
                UPDATE image_sessions
                SET runtime_invocation_count=1, state='RUNTIME_PENDING',
                    runtime_called_at=?, updated_at=?
                WHERE session_id=? AND runtime_idempotency_key=?
                  AND runtime_invocation_count=0
                """,
                (now, now, session_id, idempotency_key),
            )
            if cursor.rowcount != 1:
                return False
            database.execute(
                """
                UPDATE image_audits
                SET runtime_called_at=?, status='RUNTIME_PENDING', updated_at=?
                WHERE case_id=?
                """,
                (now, now, session_id),
            )
        return True

    def complete_runtime(
        self,
        session_id: str,
        *,
        confirmed_pillars: object,
        runtime_result_hash: str | None,
        status: str,
    ) -> None:
        now = _now()
        with self._connect() as database:
            database.execute(
                """
                UPDATE image_sessions
                SET state=?, runtime_result_hash=?, updated_at=?
                WHERE session_id=?
                """,
                (status, runtime_result_hash, now, session_id),
            )
            database.execute(
                """
                UPDATE image_audits
                SET confirmed_pillars=?, runtime_result_hash=?,
                    status=?, updated_at=?
                WHERE case_id=?
                """,
                (_json(confirmed_pillars), runtime_result_hash, status, now, session_id),
            )

    def get_audit(self, trace_id: str) -> dict[str, Any] | None:
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM image_audits WHERE trace_id=?", (trace_id,)
            ).fetchone()
        return dict(row) if row is not None else None
