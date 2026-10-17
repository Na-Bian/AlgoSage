"""Per-turn invocation ledger and shared resource limits, isolated by ContextVar."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import time


class TurnBudgetExceeded(RuntimeError):
    pass


@dataclass
class TurnLedger:
    max_calls: int
    deadline: float
    calls: list[dict] = field(default_factory=list)

    def total(self, key):
        if not self.calls:
            return 0
        values = [c.get(key) for c in self.calls]
        return (
            sum(values)
            if all(
                isinstance(v, int) and not isinstance(v, bool) and v >= 0
                for v in values
            )
            else None
        )


_current: ContextVar[TurnLedger | None] = ContextVar(
    "examguard_turn_ledger", default=None
)
_streaming: ContextVar[bool] = ContextVar(
    "examguard_streaming_transport", default=False
)


@contextmanager
def turn_ledger(settings):
    ledger = TurnLedger(
        getattr(settings, "max_calls_per_turn", 7),
        time.monotonic() + getattr(settings, "turn_timeout", 180.0),
    )
    token = _current.set(ledger)
    try:
        yield ledger
    finally:
        _current.reset(token)


@contextmanager
def streaming_transport(enabled=True):
    """Opt in to the audited, fully-consumed generation stream transport."""
    token = _streaming.set(bool(enabled))
    try:
        yield
    finally:
        _streaming.reset(token)


def generate(backend, messages, stage="generation", max_output_tokens=None):
    ledger = _current.get()
    remaining = None
    if ledger:
        remaining = ledger.deadline - time.monotonic()
        if remaining <= 0 or len(ledger.calls) >= ledger.max_calls:
            raise TurnBudgetExceeded("本轮调用预算已耗尽")
    started = time.monotonic()
    entry = {
        "stage": stage,
        "status": "error",
        "input_tokens": None,
        "output_tokens": None,
    }
    if ledger:
        ledger.calls.append(entry)
    try:
        use_stream = (
            stage == "generation"
            and _streaming.get()
            and callable(getattr(backend, "generate_stream_with_options", None))
        )
        if use_stream:
            result = backend.generate_stream_with_options(
                messages, max_output_tokens=max_output_tokens, timeout=remaining
            )
        elif hasattr(backend, "generate_with_options"):
            result = backend.generate_with_options(
                messages, max_output_tokens=max_output_tokens, timeout=remaining
            )
        else:
            result = backend.generate(messages)
        entry.update(
            status="ok",
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        if stage in {"classification", "judge"}:
            entry["response_text"] = result.text
        if ledger and time.monotonic() > ledger.deadline:
            entry["status"] = "deadline_exceeded"
            raise TurnBudgetExceeded("本轮截止时间已到，未送达候选回答")
        return result
    except Exception as error:
        entry["error_type"] = type(error).__name__
        raise
    finally:
        entry["latency_ms"] = round((time.monotonic() - started) * 1000)
