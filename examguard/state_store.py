"""Bounded safety budgets, separate from disposable chat contexts.

SQLite transactions are short: a per-principal OS file lock covers the model
turn, while the database is read before generation and written only after the
turn commits. Different principals therefore do not block each other on a
global SQLite writer. The OS lock still serializes the same trusted principal
across processes, preventing an older budget from overwriting a newer one.
Only SessionGuard snapshots are stored here; no chat transcripts, rejected
drafts or API credentials.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

from .session_guard import RiskState, SessionGuard


class StateCapacityError(RuntimeError):
    """Reject admission instead of silently forgetting a safety budget."""


class StateLockTimeout(RuntimeError):
    """The same trusted principal already has an active safety-critical turn."""


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
    def __init__(
        self,
        path: str = "",
        capacity: int = 4096,
        *,
        lock_timeout: float = 30.0,
    ):
        self.path = path
        self.capacity = capacity
        self.lock_timeout = max(0.0, float(lock_timeout))
        self._budgets: dict[tuple[str, str], SafetyBudget] = {}
        self._lock = threading.RLock()
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._lock_dir.mkdir(parents=True, exist_ok=True)
            with self._database() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS risk_states (
                    exam_id TEXT NOT NULL, principal_key TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    PRIMARY KEY (exam_id, principal_key))""")

    @property
    def _lock_dir(self) -> Path:
        return Path(self.path).parent / ".state-locks"

    def _lock_path(self, budget: SafetyBudget) -> Path:
        identity = f"{budget.exam_id}\0{budget.principal_key}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        return self._lock_dir / f"{digest}.lock"

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def _database(self, *, write: bool = True):
        db = self._connect()
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _principal_lock(self, budget: SafetyBudget):
        lock_path = self._lock_path(budget)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StateLockTimeout(
                            "同一身份的上一轮安全状态仍在处理，请稍后重试。"
                        ) from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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
                with self._database(write=False) as db:
                    row = db.execute(
                        "SELECT snapshot FROM risk_states WHERE exam_id=? AND principal_key=?",
                        key,
                    ).fetchone()
                if row is not None:
                    _restore(budget.guard, json.loads(row[0]))
                else:
                    with self._database() as db:
                        # BEGIN IMMEDIATE serializes a concurrent creation in
                        # another process, so this capacity check is current.
                        count = db.execute(
                            "SELECT COUNT(*) FROM risk_states"
                        ).fetchone()[0]
                        if count >= self.capacity:
                            raise StateCapacityError(
                                "持久安全预算容量已满，拒绝新身份。"
                            )
                        db.execute(
                            "INSERT OR IGNORE INTO risk_states VALUES (?, ?, ?)",
                            (*key, "{}"),
                        )
            self._budgets[key] = budget
            return budget

    @contextmanager
    def turn(self, budget: SafetyBudget):
        """Lock one principal from risk read through durable save."""
        with budget.lock:
            if budget.failed:
                raise RuntimeError("安全状态存储失败，当前预算已停用；请联系管理员。")
            if not self.path:
                yield budget.guard
                return
            turn_error = None
            try:
                with self._principal_lock(budget):
                    with self._database(write=False) as db:
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
                    with self._database() as db:
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
                # A persistence failure must not leave this budget usable; an
                # ordinary turn error is re-raised after a successful commit.
                budget.failed = True
                raise
            if turn_error is not None:
                raise turn_error
