"""Frontend-agnostic service layer between any UI and the ExamGuard pipeline.

UI frameworks (Chainlit student page, teacher eval page, Streamlit fallback)
must go through TutorService instead of touching ExamGuardTutor internals, so
that:
- the API key stays inside this server process and is never serialized to a
  frontend (only whitelisted display metadata leaves this module);
- the exam policy is decided by trusted server configuration; a student-facing
  UI cannot lower exam restrictions unless the teacher explicitly starts the
  server with EG_ALLOW_POLICY_SWITCH=true (demo/teacher mode);
- session history handling is consistent across frontends and unit-testable.

This module adds no new safety capability by itself; the underlying classifier,
policy, and output guard are unchanged (see docs/MODULE_INTERFACES.md).
"""
from __future__ import annotations

import logging
import os
from collections import OrderedDict
from dataclasses import dataclass, field

from .config import Settings
from .llm import ChatBackend, make_backend
from .pipeline import ExamGuardTutor
from .schemas import TutorRequest, TutorResponse

logger = logging.getLogger("examguard.service")

# The pipeline itself keeps the last 12 role-tagged messages; mirror that cap
# here so sessions cannot grow without bound in long demos.
HISTORY_TURNS_KEPT = 12
# FIFO ceiling for live session contexts (demo-grade memory bound).
MAX_LIVE_SESSIONS = 256


@dataclass
class ServiceContext:
    """Per-session backend state; never sent to the browser as a whole."""

    session_id: str
    exam_mode: bool
    history: list[dict[str, str]] = field(default_factory=list)


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


class TutorService:
    """Owns settings, backend construction, sessions and the trusted policy switch."""

    def __init__(
        self,
        settings: Settings,
        backend: ChatBackend | None = None,
        allow_policy_override: bool | None = None,
    ):
        self.settings = settings
        self.backend = backend if backend is not None else make_backend(settings)
        self.tutor = ExamGuardTutor(self.backend, settings)
        if allow_policy_override is None:
            allow_policy_override = _env_flag("EG_ALLOW_POLICY_SWITCH")
        self.allow_policy_override = allow_policy_override
        self._contexts: OrderedDict[str, ServiceContext] = OrderedDict()

    # ------------------------------------------------------------------
    # Session / context management
    # ------------------------------------------------------------------
    def new_context(self, session_id: str, exam_mode: bool | None = None) -> ServiceContext:
        """Create (or replace) the context for a session.

        ``exam_mode`` may only be supplied when the server was started in
        teacher-demo mode; otherwise the trusted server setting wins and a
        frontend attempt to lower it fails loudly instead of silently.
        """
        if exam_mode is not None and exam_mode != self.settings.exam_mode:
            self._require_override(exam_mode)
        effective_mode = self.settings.exam_mode if exam_mode is None else exam_mode
        context = ServiceContext(session_id=session_id, exam_mode=effective_mode)
        self._contexts[session_id] = context
        self._evict_overflow()
        return context

    def get_context(self, session_id: str) -> ServiceContext:
        context = self._contexts.get(session_id)
        if context is None:
            context = self.new_context(session_id)
        else:
            self._contexts.move_to_end(session_id)
        return context

    def drop_context(self, session_id: str) -> bool:
        return self._contexts.pop(session_id, None) is not None

    def set_exam_mode(self, context: ServiceContext, exam_mode: bool) -> None:
        """Teacher-only switch; refused unless EG_ALLOW_POLICY_SWITCH is enabled."""
        if exam_mode != self.settings.exam_mode:
            self._require_override(exam_mode)
        context.exam_mode = exam_mode

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------
    def chat(self, context: ServiceContext, question: str, existing_code: str = "") -> TutorResponse:
        """One tutoring turn; appends the exchanged text to the session history.

        The history stores display text only (mirrors the Streamlit starter),
        not system prompts or internal audit structures.
        """
        response = self._respond(context, question, existing_code)
        user_entry = question if not existing_code.strip() else (
            question + "\n\n已有代码（仅文本分析，不执行）：\n" + existing_code
        )
        context.history.append({"role": "user", "content": user_entry})
        context.history.append({"role": "assistant", "content": response.answer})
        context.history = context.history[-2 * HISTORY_TURNS_KEPT:]
        return response

    # ------------------------------------------------------------------
    # Safe display metadata (frontend whitelist)
    # ------------------------------------------------------------------
    @staticmethod
    def display_metadata(response: TutorResponse) -> dict:
        """Whitelisted metadata that any frontend may render.

        Deliberately excludes: API key, base URL, system prompts, raw user
        inputs and internal regex patterns. Nothing here is security-critical.
        """
        return {
            "intent": response.classification.intent.value,
            "classify_method": response.classification.method,
            "classify_reason": response.classification.reason,
            "help_level": response.decision.max_help_level,
            "policy_reason": response.decision.reason,
            "approved": response.audit.approved,
            "audit_reason": response.audit.reason,
            "model": response.model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _respond(self, context: ServiceContext, question: str, existing_code: str) -> TutorResponse:
        request = TutorRequest(
            question=question,
            existing_code=existing_code,
            exam_mode=context.exam_mode,
        )
        return self.tutor.respond(request, history=context.history)

    def _require_override(self, requested_mode: bool) -> None:
        if not self.allow_policy_override:
            raise PermissionError(
                "考试模式由服务端配置（EXAM_MODE）决定，前端不可修改；"
                "教师演示如需切换请以 EG_ALLOW_POLICY_SWITCH=true 启动。"
            )
        logger.warning("考试策略被教师演示开关修改为 exam_mode=%s", requested_mode)

    def _evict_overflow(self) -> None:
        while len(self._contexts) > MAX_LIVE_SESSIONS:
            oldest, _ = self._contexts.popitem(last=False)
            logger.info("会话 %s 因超出上限被清理", oldest)
