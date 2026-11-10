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
import time
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace

from .config import Settings
from .exam_policy import ExamPolicy
from .hybrid_classifier import HybridIntentClassifier
from .llm import ChatBackend, make_backend
from .output_guard import BasicOutputGuard
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
    model_params: dict[str, float | int | str] = field(default_factory=dict)
    _budget: SafetyBudget | None = field(default=None, repr=False)


def normalize_help_level_cap(value):
    """学生等级上限归一化：auto/空/非法 → None（自动跟随策略）；1/2/3 → int。"""
    if isinstance(value, str):
        value = value.strip().lower()
    if value in (None, "", "auto"):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value in (1, 2, 3) else None


def normalize_model_params(
    params: dict, settings: Settings
) -> dict[str, float | int | str]:
    """Clamp user-owned sampling controls against the selected provider profile."""
    raw = params if isinstance(params, dict) else {}
    try:
        max_output = max(
            128,
            min(
                int(raw.get("max_output_tokens", settings.max_output_tokens)),
                settings.max_output_tokens,
            ),
        )
    except (TypeError, ValueError):
        max_output = settings.max_output_tokens

    def bounded_float(key: str, default: float, low: float, high: float) -> float:
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        return min(high, max(low, value))

    enter_behavior = raw.get("enter_behavior", "enter")
    font_size = raw.get("font_size", "standard")

    def optional_bool(key: str, default: bool) -> bool:
        value = raw.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    help_level_cap = normalize_help_level_cap(raw.get("help_level_cap"))

    return {
        "temperature": bounded_float("temperature", 0.7, 0.0, 2.0),
        "top_p": bounded_float("top_p", 0.95, 0.0, 1.0),
        "frequency_penalty": bounded_float("frequency_penalty", 0.0, 0.0, 2.0),
        "presence_penalty": bounded_float("presence_penalty", 0.0, 0.0, 2.0),
        "max_output_tokens": max_output,
        "help_level_cap": help_level_cap,
        "enter_behavior": (
            enter_behavior if enter_behavior in {"enter", "ctrl_enter"} else "enter"
        ),
        "autoscroll": optional_bool("autoscroll", True),
        "keep_drafts": optional_bool("keep_drafts", True),
        "animations": optional_bool("animations", True),
        "font_size": (
            font_size if font_size in {"small", "standard", "large"} else "standard"
        ),
        "audit_summary": optional_bool("audit_summary", True),
        # 输入区生成选项（深度思考 / 联网搜索）：随账号偏好保存并透传生成请求。
        "deep_think": optional_bool("deep_think", False),
        "web_search": optional_bool("web_search", False),
        # 分级辅助的学生上限（None=自动跟随策略；1/2/3=收紧到该级）。
        "help_level_cap": normalize_help_level_cap(
            raw.get("help_level_cap")
        ),
    }


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
        custom_provider_env_file: str | None = None,
    ):
        self.settings = settings
        self.providers = providers or ProviderRegistry(settings, entries=[])
        from .custom_providers import CustomProviderStore

        self.custom_providers = CustomProviderStore(
            settings, env_file=custom_provider_env_file or None
        )
        self._provider_tutors = {}
        self._custom_tutors: dict[tuple[str, str], AlgoSageTutor] = {}
        self._provider_lock = threading.RLock()
        self.backend = backend if backend is not None else make_backend(settings)
        # P2：可信策略文件（非法即启动失败，fail closed）。
        if settings.policy_file:
            self.policy_config = ExamPolicy.from_file(settings.policy_file)
        else:
            self.policy_config = ExamPolicy.default()
        # P1：hybrid 模式下注入规则+LLM 混合分类器；LLM 不可用时自动回退正则。
        # LLM 层走内置策略模型（CLASSIFIER_*），不随学生的 BYOK 模型漂移。
        classifier = None
        if settings.classifier_mode == "hybrid":
            classifier = HybridIntentClassifier(
                self.builtin_classifier_backend(), settings, self.policy_config
            )
        # P2：按配置选择输出审核层（basic / rules / layered）。
        output_guard = make_output_guard(settings, self.backend)
        self.tutor = AlgoSageTutor(
            self.backend,
            settings,
            classifier=classifier,
            output_guard=output_guard,
            policy_config=self.policy_config,
            policy_backend=(
                self.builtin_classifier_backend()
                if settings.classifier_mode == "hybrid"
                else None
            ),
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

    def forget_personal_state(self, exam_id: str, principal_id: str) -> None:
        """Remove all account-linked learning state after account closure.

        ``principal_id`` is the trusted identifier used by new_context
        (e.g. ``student:U202412345``); the stored key is ``principal:<id>``.
        The principal's BYOK providers, including encrypted API keys, go too.
        """
        principal_key = f"principal:{principal_id}"
        with self._context_lock:
            doomed_sessions = [
                session_id
                for session_id, context in self._contexts.items()
                if context._budget is not None
                and context._budget.exam_id == exam_id
                and context._budget.principal_key == principal_key
            ]
            for session_id in doomed_sessions:
                self._contexts.pop(session_id, None)
                self._session_bindings.pop(session_id, None)
        self.state_store.delete_principal(exam_id, principal_key)
        removed = self.custom_providers.remove_all(principal_id)
        if removed:
            with self._provider_lock:
                for selection in removed:
                    self._custom_tutors.pop((principal_id, selection), None)

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
        # 账号级预算锁由 state_store.turn 在这里获取（整回合持有）：上一回合
        # 若挂起，本回合会在此无声等待。回合计时用于诊断"发消息卡住"。
        budget_wait_started = time.monotonic()
        with self.state_store.turn(self._context_budget(context)):
            budget_waited = time.monotonic() - budget_wait_started
            if budget_waited > 15:
                logger.warning(
                    "budget lock waited %.1fs before this turn principal=%s",
                    budget_waited,
                    context._budget.principal_key,
                )
            self.state_store.consume_global_daily_request(
                self.settings.global_daily_requests
            )
            self.state_store.consume_daily_request(
                context._budget.principal_key,
                self.settings.daily_requests_per_student,
            )
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
        # 账号级预算锁由 state_store.turn 在这里获取（整回合持有）：上一回合
        # 若挂起，本回合会在此无声等待。回合计时用于诊断"发消息卡住"。
        budget_wait_started = time.monotonic()
        with self.state_store.turn(self._context_budget(context)):
            budget_waited = time.monotonic() - budget_wait_started
            if budget_waited > 15:
                logger.warning(
                    "budget lock waited %.1fs before this turn principal=%s",
                    budget_waited,
                    context._budget.principal_key,
                )
            self.state_store.consume_global_daily_request(
                self.settings.global_daily_requests
            )
            self.state_store.consume_daily_request(
                context._budget.principal_key,
                self.settings.daily_requests_per_student,
            )
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
        context.history.append(user_record)
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
            deep_think=bool(context.model_params.get("deep_think")),
            web_search=bool(context.model_params.get("web_search")),
            help_level_cap=(
                int(context.model_params["help_level_cap"])
                if context.model_params.get("help_level_cap") is not None
                else None
            ),
        )
        return self._selected_tutor(context).respond(
            request,
            history=context.history,
            session_guard=context.session_guard,
            generation_options=context.model_params or None,
        )

    def _resolve_selection_settings(
        self, context: ServiceContext, selection: str
    ) -> Settings:
        """Builtin allowlist selections plus this principal's BYOK entries."""
        if selection.startswith("custom:"):
            if not context.principal_id:
                raise ValueError("自定义模型需要已登录身份")
            return self.custom_providers.resolve(context.principal_id, selection)
        return self.providers.resolve(selection)

    def select_provider(self, context: ServiceContext, selection: str) -> None:
        # Serialize against an in-flight turn; every turn uses one immutable profile.
        with self._context_budget(context).lock:
            self.apply_provider_selection(context, selection)

    def apply_provider_selection(self, context: ServiceContext, selection: str) -> None:
        """Apply a selection without the budget lock (select_provider's body).

        Only for restoring a chat's state. The budget lock is per principal and
        held for a whole turn, so a restore that waits on it stalls every chat
        of that student until the generation ends. This is two attribute
        assignments; an in-flight turn has already captured its own profile.
        """
        settings = self._resolve_selection_settings(context, selection)
        context.provider_selection = selection
        # Sampling controls are re-clamped against the newly selected profile.
        context.model_params = normalize_model_params(context.model_params, settings)

    def select_provider_probe(
        self, selection: str, principal_id: str | None = None
    ) -> Settings:
        """Validate a selection without mutating any context (default-model checks)."""
        context = ServiceContext(
            session_id="probe",
            exam_mode=self.settings.exam_mode,
            provider_selection=selection,
            principal_id=principal_id,
        )
        return self._resolve_selection_settings(context, selection)

    # ------------------------------------------------------------------
    # BYOK custom providers (per principal, memory only)
    # ------------------------------------------------------------------
    def register_custom_provider(
        self,
        principal_id: str,
        *,
        base_url: str,
        api_key: str,
        model: str,
        label: str = "",
        capabilities: dict | None = None,
    ) -> dict:
        entry = self.custom_providers.register(
            principal_id,
            base_url=base_url,
            api_key=api_key,
            model=model,
            label=label,
            capabilities=capabilities,
        )
        # 同 selection 重建（如删除后重加、换了探测结果）时旧 tutor 携带
        # 旧能力，必须作废重建。
        with self._provider_lock:
            self._custom_tutors.pop((principal_id, entry["selection"]), None)
        return entry

    def list_custom_providers(self, principal_id: str) -> list[dict]:
        return self.custom_providers.list_for(principal_id)

    def remove_custom_provider(self, principal_id: str, selection: str) -> bool:
        removed = self.custom_providers.remove(principal_id, selection)
        if removed:
            with self._provider_lock:
                self._custom_tutors.pop((principal_id, selection), None)
        return removed

    def discover_custom_models(self, base_url: str, api_key: str) -> list[dict]:
        return self.custom_providers.discover(base_url, api_key)

    def set_model_params(self, context: ServiceContext, params: dict) -> None:
        try:
            settings = self._resolve_selection_settings(
                context, context.provider_selection
            )
        except ValueError:
            # 未选择模型（无任何提供商）：采样控件基于服务端基础参数。
            settings = self.settings
        context.model_params = normalize_model_params(params, settings)

    def normalize_model_params(self, params: dict, settings: Settings) -> dict:
        return normalize_model_params(params, settings)

    def provider_info(self, context: ServiceContext) -> dict:
        if context.provider_selection.startswith("custom:"):
            if not context.principal_id:
                raise ValueError("未知自定义模型")
            return self.custom_providers.info(
                context.principal_id, context.provider_selection
            )
        return self.providers.info(context.provider_selection)

    def generate_title(
        self,
        selection: str,
        question: str,
        timeout: float = 8,
        principal_id: str | None = None,
    ):
        """标题用内置小模型（TITLE_* 配置，缺省 Qwen3.5-4B 档）独立生成，
        与学生会话选择的模型无关：BYOK 模型可能是昂贵/慢速的大模型， auxiliary
        任务不应跟随；也可保证 BYOK 会话同样有标题。"""
        from .chat_titles import generate_title

        tutor = self._title_tutor()
        return generate_title(tutor.backend, tutor.settings, question, timeout)

    def _title_tutor(self) -> AlgoSageTutor:
        """懒构建内置标题 tutor；TITLE_MODEL 未配置时回退主模型配置。"""
        if getattr(self, "_cached_title_tutor", None) is not None:
            return self._cached_title_tutor
        title_settings = replace(
            self.settings,
            model=os.environ.get("TITLE_MODEL", "").strip() or self.settings.model,
            base_url=os.environ.get("TITLE_BASE_URL", "").strip()
            or self.settings.base_url,
            api_key=os.environ.get("TITLE_API_KEY", "").strip()
            or self.settings.api_key,
            # 标题是短文本一次性任务：小预算、关闭思考；结果另有 extractive
            # 校验兜底，不需要审核器。
            max_output_tokens=96,
            extra_body={"enable_thinking": False},
            capabilities=None,
        )
        backend = make_backend(title_settings)
        tutor = AlgoSageTutor(
            backend,
            title_settings,
            output_guard=BasicOutputGuard(),
            policy_config=self.policy_config,
        )
        self._cached_title_tutor = tutor
        return tutor

    def builtin_classifier_backend(self):
        """内置策略模型（意图分类 LLM 层 + 解法要素抽取）的 backend。

        与学生会话模型解耦：分类延迟与行为不随学生接入的 BYOK 模型漂移。
        CLASSIFIER_MODEL 未配置时回退主模型配置；不可用时上层自动回落
        正则分类器（hybrid 既有回退路径）。这里只给 backend，不建 tutor——
        分类与抽取走各自的小预算调用，不经回合 ledger。"""
        if getattr(self, "_cached_classifier_backend", None) is not None:
            return self._cached_classifier_backend
        classifier_settings = replace(
            self.settings,
            model=os.environ.get("CLASSIFIER_MODEL", "").strip()
            or self.settings.model,
            base_url=os.environ.get("CLASSIFIER_BASE_URL", "").strip()
            or self.settings.base_url,
            api_key=os.environ.get("CLASSIFIER_API_KEY", "").strip()
            or self.settings.api_key,
            extra_body={"enable_thinking": False},
            capabilities=None,
        )
        backend = make_backend(classifier_settings)
        self._cached_classifier_backend = backend
        return backend

    def _policy_backend_for(self, settings: Settings):
        """回合 tutor 的要素抽取 backend：hybrid 模式共享内置策略模型，
        其他模式未启用（抽取跳过，正则地板照常）。"""
        if settings.classifier_mode == "hybrid":
            return self.builtin_classifier_backend()
        return None

    def _selected_tutor(self, context: ServiceContext) -> AlgoSageTutor:
        selection = context.provider_selection
        if selection == self.providers.default:
            return self.tutor
        if selection.startswith("custom:"):
            if not context.principal_id:
                raise ValueError("自定义模型需要已登录身份")
            cache_key = (context.principal_id, selection)
            with self._provider_lock:
                tutor = self._custom_tutors.get(cache_key)
                if tutor is not None:
                    return tutor
                settings = self.custom_providers.resolve(
                    context.principal_id, selection
                )
                backend = make_backend(settings)
                classifier = (
                    HybridIntentClassifier(
                        self.builtin_classifier_backend(),
                        settings,
                        self.policy_config,
                    )
                    if settings.classifier_mode == "hybrid"
                    else None
                )
                tutor = AlgoSageTutor(
                    backend,
                    settings,
                    classifier=classifier,
                    output_guard=make_output_guard(settings, backend),
                    policy_config=self.policy_config,
                    policy_backend=self._policy_backend_for(settings),
                )
                self._custom_tutors[cache_key] = tutor
                return tutor
        with self._provider_lock:
            if selection not in self._provider_tutors:
                settings = self.providers.resolve(selection)
                backend = make_backend(settings)
                classifier = (
                    HybridIntentClassifier(
                        self.builtin_classifier_backend(),
                        settings,
                        self.policy_config,
                    )
                    if settings.classifier_mode == "hybrid"
                    else None
                )
                self._provider_tutors[selection] = AlgoSageTutor(
                    backend,
                    settings,
                    classifier=classifier,
                    output_guard=make_output_guard(settings, backend),
                    policy_config=self.policy_config,
                    policy_backend=self._policy_backend_for(settings),
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
