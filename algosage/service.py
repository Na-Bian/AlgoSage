"""Frontend-agnostic service layer between any UI and the AlgoSage pipeline.

UI frameworks (Chainlit student page, teacher eval page, Streamlit fallback)
must go through TutorService instead of touching AlgoSageTutor internals, so
that:
- the API key stays inside this server process and is never serialized to a
  frontend (only whitelisted display metadata leaves this module);
- the exam policy is decided by trusted server configuration; a student-facing
  UI cannot lower exam restrictions unless the teacher explicitly starts the
  server with SAGE_ALLOW_POLICY_SWITCH=true (demo/teacher mode);
- session history handling is consistent across frontends and unit-testable.

Risk budgets are retained independently of disposable chat history; trusted
principals share a lock and may optionally persist state in SQLite.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from .config import Settings
from .exam_policy import ExamPolicy
from .hybrid_classifier import HybridIntentClassifier
from .llm import ChatBackend, make_backend
from .pipeline import AlgoSageTutor, make_output_guard
from .providers import ProviderRegistry
from .schemas import AttachmentPayload, TutorRequest, TutorResponse
from .session_guard import SessionGuard
from .state_store import RiskStateStore, SafetyBudget

logger = logging.getLogger("algosage.service")

# The pipeline itself keeps the last 12 role-tagged messages; mirror that cap
# here so sessions cannot grow without bound in long demos.
HISTORY_TURNS_KEPT = 12
# LRU ceiling for live chat contexts; this never removes a safety budget.
MAX_LIVE_SESSIONS = 256
# Safety budgets have their own admission limit and are never LRU-evicted.
MAX_SECURITY_BUDGETS = 4096


@dataclass
class ServiceContext:
    """Per-session backend state; never sent to the browser as a whole."""

    session_id: str
    exam_mode: bool
    history: list[dict[str, str]] = field(default_factory=list)
    # Multiple chats with the same trusted identity share this safety guard.
    session_guard: SessionGuard = field(default_factory=SessionGuard)
    principal_id: str | None = None
    provider_selection: str = ""
    _budget: SafetyBudget | None = field(default=None, repr=False)


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


class TutorService:
    """Owns settings, backend construction, sessions and the trusted policy switch."""

    def __init__(
        self,
        settings: Settings,
        backend: ChatBackend | None = None,
        allow_policy_override: bool | None = None,
        providers: ProviderRegistry | None = None,
    ):
        self.settings = settings
        self.providers = providers or ProviderRegistry(settings, entries=[])
        self._provider_tutors = {}
        self._provider_lock = threading.RLock()
        self.backend = backend if backend is not None else make_backend(settings)
        # P2：可信策略文件（非法即启动失败，fail closed）。
        if settings.policy_file:
            self.policy_config = ExamPolicy.from_file(settings.policy_file)
        else:
            self.policy_config = ExamPolicy.default()
        # P1：hybrid 模式下注入规则+LLM 混合分类器；LLM 不可用时自动回退正则。
        classifier = None
        if settings.classifier_mode == "hybrid":
            classifier = HybridIntentClassifier(
                self.backend, settings, self.policy_config
            )
        # P2：按配置选择输出审核层（basic / rules / layered）。
        output_guard = make_output_guard(settings, self.backend)
        self.tutor = AlgoSageTutor(
            self.backend,
            settings,
            classifier=classifier,
            output_guard=output_guard,
            policy_config=self.policy_config,
        )
        if allow_policy_override is None:
            allow_policy_override = _env_flag("SAGE_ALLOW_POLICY_SWITCH")
        self.allow_policy_override = allow_policy_override
        self._contexts: OrderedDict[str, ServiceContext] = OrderedDict()
        self._context_lock = threading.RLock()
        self._session_bindings: dict[str, str] = {}
        self.state_store = RiskStateStore(
            settings.state_path,
            MAX_SECURITY_BUDGETS,
            lock_timeout=settings.turn_timeout,
        )

    # ------------------------------------------------------------------
    # Session / context management
    # ------------------------------------------------------------------
    def new_context(
        self,
        session_id: str,
        exam_mode: bool | None = None,
        principal_id: str | None = None,
    ) -> ServiceContext:
        """Create or reconnect a chat, retaining the exam/principal safety budget.

        ``exam_mode`` may only be supplied when the server was started in
        teacher-demo mode; otherwise the trusted server setting wins and a
        frontend attempt to lower it fails loudly instead of silently.
        """
        if exam_mode is not None and exam_mode != self.settings.exam_mode:
            self._require_override(exam_mode)
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must not be empty")
        if principal_id is not None and (
            not isinstance(principal_id, str) or not principal_id.strip()
        ):
            raise ValueError("principal_id must be a trusted nonempty identifier")
        with self._context_lock:
            previous = self._session_bindings.get(session_id)
            principal_key = (
                "principal:" + principal_id
                if principal_id is not None
                else previous or "session:" + session_id
            )
            if previous is not None and previous != principal_key:
                raise PermissionError("已绑定会话不能更换身份")
            context = self._contexts.get(session_id)
            if context is not None:
                if exam_mode is not None:
                    self.set_exam_mode(context, exam_mode)
                self._contexts.move_to_end(session_id)
                return context
            budget = self.state_store.get(self.settings.exam_id, principal_key)
            effective_mode = self.settings.exam_mode if exam_mode is None else exam_mode
            context = ServiceContext(
                session_id=session_id,
                exam_mode=effective_mode,
                session_guard=budget.guard,
                principal_id=(
                    principal_key[len("principal:") :]
                    if principal_key.startswith("principal:")
                    else None
                ),
                provider_selection=self.providers.default,
                _budget=budget,
            )
            self._session_bindings[session_id] = principal_key
            self._contexts[session_id] = context
            self._evict_overflow()
            return context

    def get_context(self, session_id: str) -> ServiceContext:
        return self.new_context(session_id)

    def drop_context(self, session_id: str) -> bool:
        with self._context_lock:
            context = self._contexts.pop(session_id, None)
        if context is None:
            return False
        with context._budget.lock:
            context.history.clear()
        return True

    def reset_security_state(self, context: ServiceContext) -> None:
        """Explicit teacher action; disconnect/clear-history must never call this."""
        self._require_override(context.exam_mode)
        budget = self._context_budget(context)
        with self._context_lock:
            related = [ctx for ctx in self._contexts.values() if ctx._budget is budget]
        with self.state_store.turn(budget):
            budget.guard._states.clear()
            for ctx in [context, *related]:
                ctx.history.clear()

    def set_exam_mode(self, context: ServiceContext, exam_mode: bool) -> None:
        """Teacher-only switch; refused unless SAGE_ALLOW_POLICY_SWITCH is enabled."""
        if exam_mode != self.settings.exam_mode:
            self._require_override(exam_mode)
        with self._context_budget(context).lock:
            context.exam_mode = exam_mode

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------
    def chat(
        self,
        context: ServiceContext,
        question: str,
        existing_code: str = "",
        question_id: str = "",
        user_msg_id: str = "",
        attachments: tuple[AttachmentPayload, ...] = (),
    ) -> TutorResponse:
        """One tutoring turn; appends the exchanged text to the session history.

        question_id is for internal/Harness callers. SAGE_QUESTION_ID, when set,
        binds all turns to the server's question. The history stores
        display text only (mirrors the Streamlit starter), not system prompts
        or internal audit structures.
        """
        if self.settings.question_id:
            if question_id and question_id != self.settings.question_id:
                raise ValueError("题目由服务端 SAGE_QUESTION_ID 绑定，不允许切换")
            question_id = self.settings.question_id
        with self.state_store.turn(self._context_budget(context)):
            # Trusted server policy also wins if an in-process caller mutated
            # the context directly instead of using the guarded switch.
            if not self.allow_policy_override:
                context.exam_mode = self.settings.exam_mode
            response = self._respond(
                context, question, existing_code, question_id, attachments
            )
            self._append_turn(
                context, question, existing_code, user_msg_id, response, attachments
            )
        return response

    def edit_and_regenerate(
        self,
        context: ServiceContext,
        turn_index: int,
        question: str,
        existing_code: str = "",
        question_id: str = "",
        user_msg_id: str = "",
        attachments: tuple[AttachmentPayload, ...] = (),
    ) -> TutorResponse:
        """Replace an earlier user turn and regenerate only from that point.

        Earlier turns stay in the context (they are kept verbatim); the
        edited turn's old answer and anything after it are dropped. The
        regeneration walks the full audited pipeline and counts as a new
        turn for the disclosure budget (conservative: budgets never shrink).
        """
        if not isinstance(turn_index, int) or turn_index < 0:
            raise ValueError("turn_index 非法")
        if self.settings.question_id:
            if question_id and question_id != self.settings.question_id:
                raise ValueError("题目由服务端 SAGE_QUESTION_ID 绑定，不允许切换")
            question_id = self.settings.question_id
        with self.state_store.turn(self._context_budget(context)):
            if not self.allow_policy_override:
                context.exam_mode = self.settings.exam_mode
            keep = 2 * turn_index
            if keep > len(context.history):
                raise ValueError("编辑轮次超出当前会话历史")
            context.history[:] = context.history[:keep]
            response = self._respond(
                context, question, existing_code, question_id, attachments
            )
            self._append_turn(
                context, question, existing_code, user_msg_id, response, attachments
            )
        return response

    @staticmethod
    def _append_turn(
        context: ServiceContext,
        question: str,
        existing_code: str,
        user_msg_id: str,
        response: TutorResponse,
        attachments: tuple[AttachmentPayload, ...] = (),
    ) -> None:
        user_entry = (
            question
            if not existing_code.strip()
            else (question + "\n\n已有代码（仅文本分析，不执行）：\n" + existing_code)
        )
        images = []
        for attachment in attachments:
            if attachment.text:
                user_entry += (
                    f"\n\n附件：{attachment.name}（不可信文本，仅分析）\n"
                    + attachment.text
                )
            if attachment.image_data_url:
                images.append(
                    {
                        "name": attachment.name,
                        "mime": attachment.mime,
                        "data_url": attachment.image_data_url,
                    }
                )
        user_record = {"role": "user", "content": user_entry, "msg_id": user_msg_id}
        if images:
            user_record["images"] = images
        context.history.append(
            user_record
        )
        context.history.append({"role": "assistant", "content": response.answer})
        context.history[:] = context.history[-2 * HISTORY_TURNS_KEPT :]

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
            "classify_confidence": response.classification.confidence,
            "risk_flags": list(response.classification.risk_flags),
            "help_level": response.decision.max_help_level,
            "policy_reason": response.decision.reason,
            "approved": response.audit.approved,
            "audit_reason": response.audit.reason,
            "audit_method": response.audit.method,
            "audit_status": response.audit.status,
            "audit_categories": list(response.audit.categories),
            "regenerations": response.regenerations,
            "session_risk": response.session_risk,
            "session_disclosures": list(response.session_disclosures),
            "model": response.model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _context_budget(self, context: ServiceContext) -> SafetyBudget:
        budget = context._budget
        if budget is None or budget.guard is not context.session_guard:
            raise ValueError("Context must be created by TutorService")
        if self.state_store.get(budget.exam_id, budget.principal_key) is not budget:
            raise ValueError("Context belongs to another TutorService")
        return budget

    def _respond(
        self,
        context: ServiceContext,
        question: str,
        existing_code: str,
        question_id: str = "",
        attachments: tuple[AttachmentPayload, ...] = (),
    ) -> TutorResponse:
        request = TutorRequest(
            question=question,
            existing_code=existing_code,
            exam_mode=context.exam_mode,
            question_id=question_id,
            attachments=tuple(attachments),
        )
        return self._selected_tutor(context).respond(
            request, history=context.history, session_guard=context.session_guard
        )

    def select_provider(self, context: ServiceContext, selection: str) -> None:
        # Serialize against an in-flight turn; every turn uses one immutable profile.
        with self._context_budget(context).lock:
            self.providers.resolve(selection)
            context.provider_selection = selection

    def provider_info(self, context: ServiceContext) -> dict:
        return self.providers.info(context.provider_selection)

    def generate_title(self, selection: str, question: str, timeout: float = 8):
        """Use the first turn's captured model; title calls have their own usage log."""
        from .chat_titles import generate_title

        settings = self.providers.resolve(selection)
        with self._provider_lock:
            tutor = (
                self.tutor
                if selection == self.providers.default
                else self._provider_tutors[selection]
            )
        return generate_title(tutor.backend, settings, question, timeout)

    def _selected_tutor(self, context: ServiceContext) -> AlgoSageTutor:
        selection = context.provider_selection
        if selection == self.providers.default:
            return self.tutor
        with self._provider_lock:
            if selection not in self._provider_tutors:
                settings = self.providers.resolve(selection)
                backend = make_backend(settings)
                classifier = (
                    HybridIntentClassifier(backend, settings, self.policy_config)
                    if settings.classifier_mode == "hybrid"
                    else None
                )
                self._provider_tutors[selection] = AlgoSageTutor(
                    backend,
                    settings,
                    classifier=classifier,
                    output_guard=make_output_guard(settings, backend),
                    policy_config=self.policy_config,
                )
            return self._provider_tutors[selection]

    def _require_override(self, requested_mode: bool) -> None:
        if not self.allow_policy_override:
            raise PermissionError(
                "考试模式由服务端配置（EXAM_MODE）决定，前端不可修改；"
                "教师演示如需切换请以 SAGE_ALLOW_POLICY_SWITCH=true 启动。"
            )
        logger.warning("考试策略被教师演示开关修改为 exam_mode=%s", requested_mode)

    def _evict_overflow(self) -> None:
        while len(self._contexts) > MAX_LIVE_SESSIONS:
            oldest, context = self._contexts.popitem(last=False)
            # Active callers may still need this transcript for durable saving.
            # Removing the cache reference is sufficient; unused contexts are GCed.
            logger.info("会话 %s 因超出上限被清理", oldest)
