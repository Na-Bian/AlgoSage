"""Small durable Chainlit data layer for local ExamGuard conversations."""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from chainlit.data.base import BaseDataLayer
from chainlit.data.utils import queue_until_user_message
from chainlit.context import ChainlitContextException
from chainlit.types import (
    Feedback,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadFilter,
)
from chainlit.user import PersistedUser, User

_MAX_PAGE = 100
_MAX_NAME = 200
_STEP_FIELDS = {
    "name",
    "type",
    "id",
    "threadId",
    "parentId",
    "command",
    "modes",
    "streaming",
    "waitForAnswer",
    "isError",
    "tags",
    "input",
    "output",
    "createdAt",
    "start",
    "end",
    "showInput",
    "defaultOpen",
    "autoCollapse",
    "language",
    "icon",
}


def _queue_local(method):
    queued = queue_until_user_message()(method)

    async def wrapper(self, *args, **kwargs):
        try:
            return await queued(self, *args, **kwargs)
        except ChainlitContextException:
            return await method(self, *args, **kwargs)

    wrapper.__name__ = method.__name__
    return wrapper


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _safe_thread_metadata(
    metadata: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    settings = (metadata or {}).get("chat_settings")
    if not isinstance(settings, dict):
        return None
    allowed = {
        key: settings[key]
        for key in ("provider_selection", "exam_mode_switch")
        if key in settings
        and isinstance(settings[key], (str, bool, int, float, type(None)))
    }
    return {"chat_settings": allowed} if allowed else None


def _safe_step(step: Dict[str, Any]) -> Dict[str, Any]:
    clean = {key: step[key] for key in _STEP_FIELDS if key in step}
    # Client-provided step metadata is never trusted for backend model state.
    clean["id"] = str(step.get("id") or uuid4())
    clean["threadId"] = str(step.get("threadId") or "")
    return clean


class ChatDataLayer(BaseDataLayer):
    """SQLite-backed Chainlit persistence with fail-closed thread scoping."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, identifier TEXT UNIQUE NOT NULL,
                    display_name TEXT, metadata TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, name TEXT,
                    user_id TEXT, user_identifier TEXT, tags TEXT, metadata TEXT
                );
                CREATE TABLE IF NOT EXISTS steps (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, created_at TEXT,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_threads_owner_time
                    ON threads(user_identifier, created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_steps_thread_time
                    ON steps(thread_id, created_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS backend_history (
                    thread_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL,
                    history TEXT NOT NULL, provider_selection TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deleted_threads (
                    thread_id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL
                );
                """)
            self._conn.commit()

    def _thread_row(self, thread_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()

    def _thread_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        steps = self._conn.execute(
            "SELECT data FROM steps WHERE thread_id = ? ORDER BY created_at ASC, id ASC",
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "createdAt": row["created_at"],
            "name": row["name"],
            "userId": row["user_id"],
            "userIdentifier": row["user_identifier"],
            "tags": _decode(row["tags"], None),
            "metadata": _decode(row["metadata"], None),
            "steps": [_decode(item["data"], {}) for item in steps],
            "elements": [],
        }

    async def get_user(self, identifier: str) -> Optional[PersistedUser]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE identifier = ?", (identifier,)
            ).fetchone()
        if row is None:
            return None
        return PersistedUser(
            id=row["id"],
            identifier=row["identifier"],
            display_name=row["display_name"],
            metadata=_decode(row["metadata"], {}),
            createdAt=row["created_at"],
        )

    async def create_user(self, user: User) -> Optional[PersistedUser]:
        created = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO users(id, identifier, display_name, metadata, created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(identifier) DO UPDATE SET display_name=excluded.display_name, metadata=excluded.metadata",
                (
                    user.identifier,
                    user.identifier,
                    user.display_name,
                    _json(user.metadata or {}),
                    created,
                ),
            )
            self._conn.commit()
        return await self.get_user(user.identifier)

    async def delete_feedback(self, feedback_id: str) -> bool:
        return False

    async def upsert_feedback(self, feedback: Feedback) -> str:
        return feedback.id or ""

    @_queue_local
    async def create_element(self, element):
        return None

    async def get_element(self, thread_id: str, element_id: str):
        return None

    @_queue_local
    async def delete_element(self, element_id: str, thread_id: Optional[str] = None):
        return None

    @_queue_local
    async def create_step(self, step_dict):
        clean = _safe_step(step_dict)
        thread_id = clean["threadId"]
        created = clean.get("createdAt") or clean.get("start") or _now()
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM deleted_threads WHERE thread_id = ?", (thread_id,)
            ).fetchone():
                raise PermissionError("deleted thread cannot be recreated")
            previous_step = self._conn.execute(
                "SELECT thread_id FROM steps WHERE id=?", (clean["id"],)
            ).fetchone()
            if previous_step and previous_step["thread_id"] != thread_id:
                raise PermissionError("step cannot move between conversations")
            self._conn.execute(
                "INSERT OR IGNORE INTO threads(id, created_at, name, user_id, user_identifier, tags, metadata) "
                "VALUES(?, ?, NULL, NULL, NULL, NULL, NULL)",
                (thread_id, created),
            )
            self._conn.execute(
                "INSERT INTO steps(id, thread_id, created_at, data) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET thread_id=excluded.thread_id, created_at=excluded.created_at, data=excluded.data",
                (clean["id"], thread_id, created, _json(clean)),
            )
            self._conn.commit()

    @_queue_local
    async def update_step(self, step_dict):
        await self.create_step(step_dict)

    @_queue_local
    async def delete_step(self, step_id: str):
        with self._lock:
            self._conn.execute("DELETE FROM steps WHERE id = ?", (step_id,))
            self._conn.commit()

    async def get_thread_author(self, thread_id: str) -> str:
        with self._lock:
            row = self._thread_row(thread_id)
        return (row["user_identifier"] or row["user_id"] or "") if row else ""

    async def delete_thread(self, thread_id: str):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO deleted_threads(thread_id, deleted_at) VALUES(?, ?)",
                (thread_id, _now()),
            )
            self._conn.execute("DELETE FROM steps WHERE thread_id = ?", (thread_id,))
            self._conn.execute(
                "DELETE FROM backend_history WHERE thread_id = ?", (thread_id,)
            )
            self._conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
            self._conn.commit()

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse:
        owner = filters.userId
        if not owner:
            return PaginatedResponse(
                pageInfo=PageInfo(hasNextPage=False, startCursor=None, endCursor=None),
                data=[],
            )
        first = max(0, min(int(pagination.first), _MAX_PAGE))
        cursor = _decode_cursor(pagination.cursor)
        query = "SELECT * FROM threads WHERE user_identifier = ?"
        args: List[Any] = [owner]
        if filters.search:
            query += " AND LOWER(COALESCE(name, '')) LIKE ?"
            args.append(f"%{filters.search.lower()}%")
        if cursor:
            query += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            args.extend([cursor[0], cursor[0], cursor[1]])
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(first + 1)
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
            data = [self._thread_dict(row) for row in rows[:first]]
        has_next = len(rows) > first
        start = _encode_cursor((rows[0]["created_at"], rows[0]["id"])) if data else None
        end = (
            _encode_cursor(
                (
                    rows[min(first, len(rows)) - 1]["created_at"],
                    rows[min(first, len(rows)) - 1]["id"],
                )
            )
            if data
            else None
        )
        return PaginatedResponse(
            pageInfo=PageInfo(hasNextPage=has_next, startCursor=start, endCursor=end),
            data=data,
        )

    async def get_thread(self, thread_id: str):
        with self._lock:
            row = self._thread_row(thread_id)
            return self._thread_dict(row) if row else None

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ):
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM deleted_threads WHERE thread_id = ?", (thread_id,)
            ).fetchone():
                raise PermissionError("deleted thread cannot be recreated")
            row = self._thread_row(thread_id)
            if row is None:
                self._conn.execute(
                    "INSERT INTO threads(id, created_at, name, user_id, user_identifier, tags, metadata) VALUES(?,?,?,?,?,?,?)",
                    (
                        thread_id,
                        _now(),
                        name[:_MAX_NAME] if name else None,
                        user_id,
                        user_id,
                        _json(tags) if tags is not None else None,
                        (
                            _json(_safe_thread_metadata(metadata))
                            if _safe_thread_metadata(metadata)
                            else None
                        ),
                    ),
                )
            else:
                owner = row["user_identifier"] or row["user_id"]
                if owner and user_id and owner != user_id:
                    raise PermissionError("thread owner cannot be changed")
                new_owner = owner or user_id
                self._conn.execute(
                    "UPDATE threads SET name=COALESCE(?, name), user_id=COALESCE(user_id, ?), "
                    "user_identifier=COALESCE(user_identifier, ?), tags=COALESCE(?, tags), metadata=COALESCE(?, metadata) WHERE id=?",
                    (
                        name[:_MAX_NAME] if name is not None else None,
                        new_owner,
                        new_owner,
                        _json(tags) if tags is not None else None,
                        (
                            _json(_safe_thread_metadata(metadata))
                            if _safe_thread_metadata(metadata)
                            else None
                        ),
                        thread_id,
                    ),
                )
            self._conn.commit()

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def save_backend_history(
        self,
        thread_id: str,
        principal_id: str,
        history: List[Dict[str, Any]],
        provider_selection: str,
    ) -> None:
        safe_history = []
        for message in history[-24:]:
            if not isinstance(message, dict):
                continue
            role, content = message.get("role"), message.get("content")
            if role in {"user", "assistant"} and isinstance(content, str):
                entry = {"role": role, "content": content}
                msg_id = message.get("msg_id")
                if isinstance(msg_id, str) and msg_id.strip():
                    entry["msg_id"] = msg_id.strip()[:64]
                safe_history.append(entry)
        provider = str(provider_selection or "")[:300]
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM deleted_threads WHERE thread_id = ?", (thread_id,)
            ).fetchone():
                raise PermissionError("deleted thread cannot receive backend history")
            row = self._thread_row(thread_id)
            if row is None:
                user = self._conn.execute(
                    "SELECT 1 FROM users WHERE identifier = ?", (principal_id,)
                ).fetchone()
                if user is None:
                    raise PermissionError("unknown backend history principal")
                self._conn.execute(
                    "INSERT INTO threads(id, created_at, name, user_id, user_identifier, tags, metadata) VALUES(?,?,?,?,?,?,?)",
                    (thread_id, _now(), None, principal_id, principal_id, None, None),
                )
                row = self._thread_row(thread_id)
            owner = row["user_identifier"] or row["user_id"]
            if owner and owner != principal_id:
                raise PermissionError("thread owner mismatch")
            if not owner:
                user = self._conn.execute(
                    "SELECT 1 FROM users WHERE identifier = ?", (principal_id,)
                ).fetchone()
                if user is None:
                    raise PermissionError("unknown backend history principal")
                self._conn.execute(
                    "UPDATE threads SET user_id=?, user_identifier=? WHERE id=?",
                    (principal_id, principal_id, thread_id),
                )
            metadata = _safe_thread_metadata(_decode(row["metadata"], {})) or {
                "chat_settings": {}
            }
            metadata["chat_settings"]["provider_selection"] = provider
            self._conn.execute(
                "UPDATE threads SET metadata=? WHERE id=?", (_json(metadata), thread_id)
            )
            self._conn.execute(
                "INSERT INTO backend_history(thread_id, principal_id, history, provider_selection) VALUES(?,?,?,?) "
                "ON CONFLICT(thread_id) DO UPDATE SET principal_id=excluded.principal_id, history=excluded.history, provider_selection=excluded.provider_selection",
                (thread_id, principal_id, _json(safe_history), provider),
            )
            self._conn.commit()

    async def load_backend_history(
        self, thread_id: str, principal_id: str
    ) -> tuple[List[Dict[str, Any]], str]:
        with self._lock:
            row = self._thread_row(thread_id)
            if row is None:
                return [], ""
            owner = row["user_identifier"] or row["user_id"]
            if owner != principal_id:
                raise PermissionError("thread owner mismatch")
            history = self._conn.execute(
                "SELECT history, provider_selection FROM backend_history WHERE thread_id=? AND principal_id=?",
                (thread_id, principal_id),
            ).fetchone()
        return (
            (_decode(history["history"], []), history["provider_selection"])
            if history
            else ([], "")
        )

    async def get_favorite_steps(self, user_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.data FROM steps s JOIN threads t ON t.id=s.thread_id "
                "WHERE t.user_identifier=? ORDER BY s.created_at DESC, s.id DESC",
                (user_id,),
            ).fetchall()
        return [
            data
            for row in rows
            if (data := _decode(row["data"], {})).get("metadata", {}).get("favorite")
        ]


def _encode_cursor(cursor: tuple[str, str]) -> str:
    return base64.urlsafe_b64encode(_json(cursor).encode()).decode().rstrip("=")


def _decode_cursor(value: Optional[str]) -> Optional[tuple[str, str]]:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(padded).decode())
        return (str(parsed[0]), str(parsed[1]))
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        return None
