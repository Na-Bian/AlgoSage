"""Small durable Chainlit data layer for local AlgoSage conversations."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sqlite3
import threading
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from chainlit.context import ChainlitContextException
from chainlit.data.base import BaseDataLayer
from chainlit.data.utils import queue_until_user_message
from chainlit.types import (
    Feedback,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadFilter,
)
from chainlit.user import PersistedUser, User

from .runtime_paths import protect_runtime_file

_MAX_PAGE = 100
_MAX_NAME = 200
_MAX_HISTORY_IMAGES = 6
_MAX_HISTORY_IMAGE_CHARS = 4_000_000
_MAX_ELEMENT_BYTES = 20 * 1024 * 1024
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
    allowed_keys = (
        "provider_selection",
        "exam_mode_switch",
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "max_output_tokens",
        "enter_behavior",
        "autoscroll",
        "keep_drafts",
        "animations",
        "font_size",
        "audit_summary",
    )
    allowed = {
        key: settings[key]
        for key in allowed_keys
        if key in settings
        and isinstance(settings[key], (str, bool, int, float, type(None)))
    }
    return {"chat_settings": allowed} if allowed else None


def _merged_thread_metadata(
    existing: Optional[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Merge a browser metadata update without losing durable thread state.

    Chainlit clients can refresh generic chat settings without knowing the
    server-selected conversation model. Treat provider_selection as absent,
    not cleared, in those updates.
    """
    incoming = _safe_thread_metadata(metadata)
    if incoming is None:
        return None
    saved = _safe_thread_metadata(existing)
    if saved is None:
        return incoming
    settings = {**saved["chat_settings"], **incoming["chat_settings"]}
    if "provider_selection" not in incoming["chat_settings"]:
        settings["provider_selection"] = saved["chat_settings"]["provider_selection"]
    return {"chat_settings": settings}


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
        self._temporary_element_root = None
        if path == ":memory:":
            self._temporary_element_root = tempfile.mkdtemp(prefix="algosage-elements-")
            self._element_root = Path(self._temporary_element_root)
        else:
            self._element_root = Path(path).parent / "elements"
        self._element_root.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS thread_titles (
                    thread_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    source_hash TEXT, initial_name TEXT
                );
                CREATE TABLE IF NOT EXISTS elements (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, for_id TEXT,
                    type TEXT NOT NULL, name TEXT NOT NULL, mime TEXT,
                    display TEXT NOT NULL, size TEXT, language TEXT,
                    storage_path TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_elements_thread
                    ON elements(thread_id, created_at ASC, id ASC);
                CREATE TABLE IF NOT EXISTS feedbacks (
                    id TEXT PRIMARY KEY, for_id TEXT NOT NULL UNIQUE,
                    thread_id TEXT NOT NULL, value INTEGER NOT NULL,
                    comment TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedbacks_thread
                    ON feedbacks(thread_id);
                """)
            self._conn.commit()
            if path != ":memory:":
                protect_runtime_file(path)
                self._element_root.chmod(0o700)

    def _thread_row(self, thread_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()

    def _thread_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        steps = self._conn.execute(
            "SELECT data FROM steps WHERE thread_id = ? ORDER BY created_at ASC, id ASC",
            (row["id"],),
        ).fetchall()
        feedbacks = self._conn.execute(
            "SELECT * FROM feedbacks WHERE thread_id = ?", (row["id"],)
        ).fetchall()
        feedback_by_step = {
            item["for_id"]: {
                "forId": item["for_id"],
                "id": item["id"],
                "value": item["value"],
                "comment": item["comment"],
            }
            for item in feedbacks
        }
        step_dicts = []
        for item in steps:
            step = _decode(item["data"], {})
            if step.get("id") in feedback_by_step:
                step["feedback"] = feedback_by_step[step["id"]]
            step_dicts.append(step)
        elements = self._conn.execute(
            "SELECT * FROM elements WHERE thread_id = ? ORDER BY created_at ASC, id ASC",
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
            "steps": step_dicts,
            "elements": [self._element_dict(item) for item in elements],
        }

    @staticmethod
    def _element_url(thread_id: str, element_id: str) -> str:
        return f"/project/element/{thread_id}/{element_id}/content"

    def _element_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "threadId": row["thread_id"],
            "type": row["type"],
            "chainlitKey": row["id"],
            "url": self._element_url(row["thread_id"], row["id"]),
            "name": row["name"],
            "display": row["display"],
            "size": row["size"],
            "language": row["language"],
            "forId": row["for_id"],
            "mime": row["mime"],
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

    async def update_user_display_name(
        self, identifier: str, display_name: str
    ) -> Optional[PersistedUser]:
        clean_name = display_name.strip()[:200]
        if not clean_name:
            raise ValueError("display name cannot be empty")
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE users SET display_name=?, metadata=metadata WHERE identifier=?",
                (clean_name, identifier),
            )
            self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return await self.get_user(identifier)

    async def update_user_metadata(
        self,
        identifier: str,
        metadata: Dict[str, Any],
        display_name: str | None = None,
    ) -> Optional[PersistedUser]:
        """Update or create persisted metadata for a HUST profile sync."""
        clean_metadata = _json(metadata)
        created = _now()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE users SET metadata=?, "
                "display_name=COALESCE(?, display_name) WHERE identifier=?",
                (
                    clean_metadata,
                    display_name[:200] if display_name else None,
                    identifier,
                ),
            )
            if cursor.rowcount == 0:
                self._conn.execute(
                    "INSERT INTO users(id, identifier, display_name, metadata, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        str(uuid4()),
                        identifier,
                        (display_name or "学员")[:200],
                        clean_metadata,
                        created,
                    ),
                )
            self._conn.commit()
        return await self.get_user(identifier)

    async def delete_feedback(self, feedback_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM feedbacks WHERE id = ?", (feedback_id,)
            )
            self._conn.commit()
        return cursor.rowcount > 0

    async def delete_feedback_for(self, feedback_id: str, principal_id: str) -> bool:
        """Delete feedback only when the feedback's thread belongs to principal."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT f.thread_id, t.user_identifier, t.user_id
                FROM feedbacks AS f
                JOIN threads AS t ON t.id = f.thread_id
                WHERE f.id = ?
                """,
                (feedback_id,),
            ).fetchone()
            if row is None:
                return False
            owner = row["user_identifier"] or row["user_id"]
            if owner != principal_id:
                raise PermissionError("feedback does not belong to the caller")
            cursor = self._conn.execute(
                "DELETE FROM feedbacks WHERE id = ?", (feedback_id,)
            )
            self._conn.commit()
        return cursor.rowcount > 0

    async def upsert_feedback(self, feedback: Feedback) -> str:
        feedback_id = str(feedback.id or uuid4())
        thread_id = str(feedback.threadId or "")
        if not thread_id:
            row = self._conn.execute(
                "SELECT thread_id FROM steps WHERE id = ?", (feedback.forId,)
            ).fetchone()
            thread_id = row["thread_id"] if row else ""
        if not thread_id or int(feedback.value) not in {0, 1}:
            raise ValueError(
                "feedback must target a valid thread and have value 0 or 1"
            )
        with self._lock:
            step = self._conn.execute(
                "SELECT thread_id FROM steps WHERE id = ?", (feedback.forId,)
            ).fetchone()
            if step is None or step["thread_id"] != thread_id:
                raise PermissionError("feedback target does not belong to thread")
            self._conn.execute(
                "INSERT INTO feedbacks(id, for_id, thread_id, value, comment, created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(for_id) DO UPDATE SET id=excluded.id, value=excluded.value, comment=excluded.comment",
                (
                    feedback_id,
                    feedback.forId,
                    thread_id,
                    int(feedback.value),
                    (
                        str(feedback.comment)[:2000]
                        if feedback.comment is not None
                        else None
                    ),
                    _now(),
                ),
            )
            self._conn.commit()
        return feedback_id

    async def upsert_feedback_for(self, feedback: Feedback, principal_id: str) -> str:
        """Create or update feedback after checking the target thread owner."""
        feedback_id = str(feedback.id or uuid4())
        thread_id = str(feedback.threadId or "")
        if not thread_id:
            with self._lock:
                row = self._conn.execute(
                    "SELECT thread_id FROM steps WHERE id = ?", (feedback.forId,)
                ).fetchone()
                thread_id = row["thread_id"] if row else ""
        if not thread_id or int(feedback.value) not in {0, 1}:
            raise ValueError(
                "feedback must target a valid thread and have value 0 or 1"
            )
        with self._lock:
            row = self._conn.execute(
                """
                SELECT s.thread_id AS step_thread_id,
                       t.user_identifier, t.user_id
                FROM steps AS s
                JOIN threads AS t ON t.id = s.thread_id
                WHERE s.id = ?
                """,
                (feedback.forId,),
            ).fetchone()
            if row is None or row["step_thread_id"] != thread_id:
                raise PermissionError("feedback target does not belong to thread")
            owner = row["user_identifier"] or row["user_id"]
            if owner != principal_id:
                raise PermissionError("feedback target does not belong to the caller")
            self._conn.execute(
                "INSERT INTO feedbacks(id, for_id, thread_id, value, comment, created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(for_id) DO UPDATE SET id=excluded.id, value=excluded.value, comment=excluded.comment",
                (
                    feedback_id,
                    feedback.forId,
                    thread_id,
                    int(feedback.value),
                    (
                        str(feedback.comment)[:2000]
                        if feedback.comment is not None
                        else None
                    ),
                    _now(),
                ),
            )
            self._conn.commit()
        return feedback_id

    @_queue_local
    async def create_element(self, element):
        element_id = str(element.id)
        thread_id = str(element.thread_id or "")
        if not thread_id:
            raise ValueError("element thread_id is required")
        if element.path:
            source = Path(element.path)
            content = source.read_bytes()
        elif isinstance(element.content, str):
            content = element.content.encode()
        elif isinstance(element.content, (bytes, bytearray)):
            content = bytes(element.content)
        else:
            raise ValueError("element must provide a local path or content")
        if not content or len(content) > _MAX_ELEMENT_BYTES:
            raise ValueError("element content exceeds the persistent size limit")
        relative = Path(hashlib.sha256(element_id.encode()).hexdigest())
        target_dir = (
            self._element_root / hashlib.sha256(thread_id.encode()).hexdigest()[:24]
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target_dir.chmod(0o700)
        target = target_dir / relative.name
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(target)
        target.chmod(0o600)
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM deleted_threads WHERE thread_id = ?", (thread_id,)
            ).fetchone():
                target.unlink(missing_ok=True)
                raise PermissionError("deleted thread cannot receive elements")
            self._conn.execute(
                "INSERT OR IGNORE INTO threads(id, created_at, name, user_id, user_identifier, tags, metadata) "
                "VALUES(?, ?, NULL, NULL, NULL, NULL, NULL)",
                (thread_id, _now()),
            )
            self._conn.execute(
                "INSERT INTO elements(id, thread_id, for_id, type, name, mime, display, size, language, storage_path, size_bytes, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET thread_id=excluded.thread_id, for_id=excluded.for_id, "
                "type=excluded.type, name=excluded.name, mime=excluded.mime, display=excluded.display, "
                "size=excluded.size, language=excluded.language, storage_path=excluded.storage_path, "
                "size_bytes=excluded.size_bytes",
                (
                    element_id,
                    thread_id,
                    getattr(element, "for_id", None),
                    str(element.type),
                    str(element.name)[:200],
                    str(element.mime or "")[:100],
                    str(element.display or "inline"),
                    getattr(element, "size", None),
                    getattr(element, "language", None),
                    str(target),
                    len(content),
                    _now(),
                ),
            )
            self._conn.commit()

    async def get_element(self, thread_id: str, element_id: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM elements WHERE thread_id = ? AND id = ?",
                (thread_id, element_id),
            ).fetchone()
        return self._element_dict(row) if row else None

    def get_element_path(self, thread_id: str, element_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT storage_path FROM elements WHERE thread_id = ? AND id = ?",
                (thread_id, element_id),
            ).fetchone()
        return row["storage_path"] if row else None

    @_queue_local
    async def delete_element(self, element_id: str, thread_id: Optional[str] = None):
        with self._lock:
            query = "SELECT storage_path FROM elements WHERE id = ?"
            args = [element_id]
            if thread_id:
                query += " AND thread_id = ?"
                args.append(thread_id)
            row = self._conn.execute(query, args).fetchone()
            if row:
                self._conn.execute(
                    "DELETE FROM elements WHERE id = ?"
                    + (" AND thread_id = ?" if thread_id else ""),
                    args,
                )
                self._conn.commit()
        if row:
            Path(row["storage_path"]).unlink(missing_ok=True)
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
            element_rows = self._conn.execute(
                "SELECT storage_path FROM elements WHERE thread_id = ?", (thread_id,)
            ).fetchall()
            self._conn.execute(
                "INSERT OR IGNORE INTO deleted_threads(thread_id, deleted_at) VALUES(?, ?)",
                (thread_id, _now()),
            )
            self._conn.execute("DELETE FROM steps WHERE thread_id = ?", (thread_id,))
            self._conn.execute(
                "DELETE FROM backend_history WHERE thread_id = ?", (thread_id,)
            )
            self._conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
            self._conn.execute(
                "DELETE FROM thread_titles WHERE thread_id = ?", (thread_id,)
            )
            self._conn.execute("DELETE FROM elements WHERE thread_id = ?", (thread_id,))
            self._conn.execute(
                "DELETE FROM feedbacks WHERE thread_id = ?", (thread_id,)
            )
            self._conn.commit()
        for item in element_rows:
            Path(item["storage_path"]).unlink(missing_ok=True)

    async def delete_all_personal_data(self, principal_id: str) -> int:
        """Delete all conversations and the persisted user record for one owner."""
        if not isinstance(principal_id, str) or not principal_id.strip():
            raise ValueError("principal_id must be nonempty")
        with self._lock:
            thread_ids = [
                row["id"]
                for row in self._conn.execute(
                    "SELECT id FROM threads "
                    "WHERE user_identifier = ? OR user_id = ?",
                    (principal_id, principal_id),
                ).fetchall()
            ]
        for thread_id in thread_ids:
            await self.delete_thread(thread_id)
        with self._lock:
            self._conn.execute(
                "DELETE FROM users WHERE identifier = ?", (principal_id,)
            )
            self._conn.commit()
        return len(thread_ids)

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
                if name is not None:
                    if user_id is None:
                        # Chainlit's rename endpoint omits user_id; its first-
                        # interaction initialization supplies the authenticated id.
                        self._conn.execute(
                            "INSERT INTO thread_titles(thread_id,status) VALUES(?, 'manual') "
                            "ON CONFLICT(thread_id) DO UPDATE SET status='manual'",
                            (thread_id,),
                        )
                    else:
                        title_job = self._conn.execute(
                            "SELECT status FROM thread_titles WHERE thread_id=?",
                            (thread_id,),
                        ).fetchone()
                        if title_job:
                            if title_job["status"] == "pending" and row["name"] is None:
                                self._conn.execute(
                                    "UPDATE thread_titles SET initial_name=? WHERE thread_id=?",
                                    (name[:_MAX_NAME], thread_id),
                                )
                            else:
                                name = None  # Late initialization cannot undo a title.
                merged_metadata = _merged_thread_metadata(
                    _decode(row["metadata"], {}), metadata
                )
                self._conn.execute(
                    "UPDATE threads SET name=COALESCE(?, name), user_id=COALESCE(user_id, ?), "
                    "user_identifier=COALESCE(user_identifier, ?), tags=COALESCE(?, tags), metadata=COALESCE(?, metadata) WHERE id=?",
                    (
                        name[:_MAX_NAME] if name is not None else None,
                        new_owner,
                        new_owner,
                        _json(tags) if tags is not None else None,
                        _json(merged_metadata) if merged_metadata else None,
                        thread_id,
                    ),
                )
            self._conn.commit()

    def _title_history(self, thread_id: str, principal_id: str):
        row = self._thread_row(thread_id)
        if row is None or row["user_identifier"] != principal_id:
            return None
        saved = self._conn.execute(
            "SELECT history FROM backend_history WHERE thread_id=? AND principal_id=?",
            (thread_id, principal_id),
        ).fetchone()
        history = _decode(saved["history"], []) if saved else []
        users = [m for m in history if m.get("role") == "user"]
        if not users:
            return None
        first_hash = hashlib.sha256(_json(users[0]).encode()).hexdigest()
        return row, users, first_hash

    async def claim_title(self, thread_id: str, principal_id: str) -> bool:
        """Persist one first-turn job, separately from browser-controlled metadata."""
        with self._lock:
            current = self._title_history(thread_id, principal_id)
            if current is None or len(current[1]) != 1:
                return False
            row, _, source_hash = current
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO thread_titles(thread_id,status,source_hash,initial_name) "
                "VALUES(?, 'pending', ?, ?)",
                (thread_id, source_hash, row["name"]),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    async def finish_title(
        self, thread_id: str, principal_id: str, title: str | None
    ) -> bool:
        with self._lock:
            job = self._conn.execute(
                "SELECT * FROM thread_titles WHERE thread_id=?", (thread_id,)
            ).fetchone()
            current = self._title_history(thread_id, principal_id)
            if not job or job["status"] != "pending" or current is None:
                return False
            row, _, source_hash = current
            valid = (
                bool(title)
                and isinstance(title, str)
                and len(title) <= 24
                and "\n" not in title
                and "\r" not in title
                and job["source_hash"] == source_hash
                and job["initial_name"] == row["name"]
            )
            if valid:
                self._conn.execute(
                    "UPDATE threads SET name=? WHERE id=?", (title, thread_id)
                )
            self._conn.execute(
                "UPDATE thread_titles SET status=? WHERE thread_id=?",
                ("auto" if valid else "failed", thread_id),
            )
            self._conn.commit()
            return bool(valid)

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        with self._lock:
            self._conn.close()
        if self._temporary_element_root:
            shutil.rmtree(self._temporary_element_root, ignore_errors=True)

    async def save_backend_history(
        self,
        thread_id: str,
        principal_id: str,
        history: List[Dict[str, Any]],
        provider_selection: str,
        model_params: dict | None = None,
        *,
        only_existing: bool = False,
    ) -> None:
        """Persist a turn, or update an existing chat for account settings.

        Settings may arrive after a chat was deleted or before its first turn.
        Their optional update must not create/revive a thread. Check under the
        same lock as the write so concurrent deletion cannot race this decision.
        Ordinary message writes retain the strict tombstone/ownership checks.
        """
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
                images = message.get("images")
                if role == "user" and isinstance(images, list):
                    safe_images = []
                    for image in images[:_MAX_HISTORY_IMAGES]:
                        if not isinstance(image, dict):
                            continue
                        name = image.get("name")
                        mime = image.get("mime")
                        data_url = image.get("data_url")
                        if (
                            isinstance(name, str)
                            and isinstance(mime, str)
                            and isinstance(data_url, str)
                            and data_url.startswith("data:image/")
                            and len(data_url) <= _MAX_HISTORY_IMAGE_CHARS
                        ):
                            safe_images.append(
                                {
                                    "name": name[:200],
                                    "mime": mime[:100],
                                    "data_url": data_url,
                                }
                            )
                    if safe_images:
                        entry["images"] = safe_images
                safe_history.append(entry)
        provider = str(provider_selection or "")[:300]
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM deleted_threads WHERE thread_id = ?", (thread_id,)
            ).fetchone():
                if only_existing:
                    return
                raise PermissionError("deleted thread cannot receive backend history")
            row = self._thread_row(thread_id)
            if row is None:
                if only_existing:
                    return
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
            if model_params:
                metadata["chat_settings"].update(model_params)
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

    async def load_thread_settings(self, thread_id: str, principal_id: str) -> dict:
        with self._lock:
            row = self._thread_row(thread_id)
            if row is None:
                return {}
            owner = row["user_identifier"] or row["user_id"]
            if owner != principal_id:
                raise PermissionError("thread owner mismatch")
        metadata = _safe_thread_metadata(_decode(row["metadata"], {})) or {}
        return metadata.get("chat_settings", {})

    async def load_user_app_preferences(self, identifier: str) -> dict:
        with self._lock:
            user = await self.get_user(identifier)
        prefs = (user.metadata or {}).get("app_preferences") if user else None
        return prefs if isinstance(prefs, dict) else {}

    async def save_user_app_preferences(
        self, identifier: str, preferences: dict
    ) -> dict:
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("user identifier is required")
        if not isinstance(preferences, dict):
            raise ValueError("preferences must be an object")

        allowed_keys = {
            "temperature",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
            "max_output_tokens",
            "enter_behavior",
            "autoscroll",
            "keep_drafts",
            "animations",
            "font_size",
            "audit_summary",
            "disabled_providers",
            "deleted_providers",
            "default_model_selection",
            "deep_think",
            "web_search",
        }
        provider_list_keys = {"disabled_providers", "deleted_providers"}
        clean = {
            key: preferences[key]
            for key in allowed_keys
            if key in preferences
            and (
                (
                    key in provider_list_keys
                    and isinstance(preferences[key], list)
                    and all(isinstance(v, str) for v in preferences[key])
                )
                or isinstance(preferences[key], (str, bool, int, float, type(None)))
            )
        }

        with self._lock:
            user = await self.get_user(identifier)
            metadata = dict(user.metadata or {}) if user else {}
            old = metadata.get("app_preferences")
            metadata["app_preferences"] = (
                {**old, **clean} if isinstance(old, dict) else clean
            )
            if user:
                self._conn.execute(
                    "UPDATE users SET metadata=? WHERE identifier=?",
                    (_json(metadata), identifier),
                )
            else:
                self._conn.execute(
                    "INSERT INTO users(id, identifier, display_name, metadata, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (str(uuid4()), identifier, "学员", _json(metadata), _now()),
                )
            self._conn.commit()
        return metadata.get("app_preferences", {})

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
