"""Bounded safety budgets, separate from disposable chat contexts.

SQLite uses a write transaction for the entire turn: independent services and
processes cannot read an old budget and then overwrite a newer disclosure.
This intentionally serializes SQLite writers, even for different principals.
Only SessionGuard snapshots (including its question anchors and disclosed code
lines) are stored here; no chat transcripts, rejected drafts or API credentials.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import sqlite3
import threading

from .session_guard import RiskState, SessionGuard


class StateCapacityError(RuntimeError):
    """Reject admission instead of silently forgetting a safety budget."""


@dataclass
class SafetyBudget:
    exam_id: str
    principal_key: str
    guard: SessionGuard = field(default_factory=SessionGuard)
    lock: object = field(default_factory=threading.RLock)
    failed: bool = False


def _restore(guard: SessionGuard, payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("Invalid persisted risk snapshot")
    states = {}
    for key, item in payload.items():
        if not isinstance(key, str) or not isinstance(item, dict):
            raise ValueError("Invalid persisted risk state")
        state = RiskState.from_dict(item)
        if (
            not math.isfinite(state.risk_score)
            or not 0 <= state.risk_score <= 1
            or state.turns < 0
        ):
            raise ValueError("Invalid persisted risk budget")
        states[key] = state
    # SessionGuard currently exposes snapshot only; hydrate its RiskState map.
    guard._states = states


class RiskStateStore:
    def __init__(self, path: str = "", capacity: int = 4096):
        self.path = path
        self.capacity = capacity
        self._budgets: dict[tuple[str, str], SafetyBudget] = {}
        self._lock = threading.RLock()
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with self._database() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS risk_states (
                    exam_id TEXT NOT NULL, principal_key TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    PRIMARY KEY (exam_id, principal_key))""")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    @contextmanager
    def _database(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, exam_id: str, principal_key: str) -> SafetyBudget:
        key = (exam_id, principal_key)
        with self._lock:
            if key in self._budgets:
                return self._budgets[key]
            if len(self._budgets) >= self.capacity:
                raise StateCapacityError(
                    "安全预算容量已满，拒绝新身份；已有预算不会被淘汰。"
                )
            budget = SafetyBudget(exam_id, principal_key)
            if self.path:
                with self._database() as db:
                    row = db.execute(
                        "SELECT snapshot FROM risk_states WHERE exam_id=? AND principal_key=?",
                        key,
                    ).fetchone()
                    if row is not None:
                        _restore(budget.guard, json.loads(row[0]))
                    else:
                        count = db.execute(
                            "SELECT COUNT(*) FROM risk_states"
                        ).fetchone()[0]
                        if count >= self.capacity:
                            raise StateCapacityError(
                                "持久安全预算容量已满，拒绝新身份。"
                            )
                        db.execute(
                            "INSERT INTO risk_states VALUES (?, ?, ?)", (*key, "{}")
                        )
            self._budgets[key] = budget
            return budget

    @contextmanager
    def turn(self, budget: SafetyBudget):
        """Lock from risk read through response/history update and durable save."""
        with budget.lock:
            if budget.failed:
                raise RuntimeError("安全状态存储失败，当前预算已停用；请联系管理员。")
            if not self.path:
                yield budget.guard
                return
            turn_error = None
            try:
                with self._database() as db:
                    row = db.execute(
                        "SELECT snapshot FROM risk_states WHERE exam_id=? AND principal_key=?",
                        (budget.exam_id, budget.principal_key),
                    ).fetchone()
                    if row is None:
                        raise RuntimeError("安全预算缺失，拒绝重新创建。")
                    _restore(budget.guard, json.loads(row[0]))
                    try:
                        yield budget.guard
                    except BaseException as error:
                        turn_error = error
                    finally:
                        db.execute(
                            "UPDATE risk_states SET snapshot=? WHERE exam_id=? AND principal_key=?",
                            (
                                json.dumps(
                                    budget.guard.snapshot(),
                                    ensure_ascii=False,
                                    allow_nan=False,
                                ),
                                budget.exam_id,
                                budget.principal_key,
                            ),
                        )
            except BaseException:
                # The response is never returned after a persistence failure. Mark
                # this object unusable so a later request cannot silently resume.
                budget.failed = True
                raise
            if turn_error is not None:
                raise turn_error
