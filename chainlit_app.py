"""ExamGuard Chainlit 学生端界面（方案 B 前端迁移）。

启动：
    .venv/bin/python run_app.py          # 默认端口 8000
    .venv/bin/python run_app.py --host 0.0.0.0 --port 8080

设计约束：
- 本文件只负责展示与输入拆分；意图分类、帮助等级、模型调用、输出审核
  全部经由 examguard.service.TutorService 完成（业务逻辑与前端解耦）。
- API Key 只存在于服务端进程；浏览器仅收到白名单元数据。
- 考试模式默认由服务端 EXAM_MODE 决定；仅当以 EG_ALLOW_POLICY_SWITCH=true
  启动（教师演示）时，界面上才会出现模式开关，学生常规运行无法绕过。
- 旧 Streamlit 界面 app.py 保留为回退方案。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from weakref import WeakValueDictionary

import chainlit as cl
from chainlit.input_widget import Select, Switch

from examguard.config import Settings
from examguard.service import TutorService, HISTORY_TURNS_KEPT
from examguard.providers import ProviderRegistry
from examguard.invocation import streaming_transport
from examguard.local_identity import browser_user

logger = logging.getLogger("examguard.chainlit")

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)

_LEVEL_BADGES = {
    0: "L0 · 拒答越权请求",
    1: "L1 · 概念与背景知识",
    2: "L2 · 方向性提示",
    3: "L3 · 局部代码诊断",
}

WELCOME_TMPL = """### 🛡️ ExamGuard · 算法考试受限助教

课程原型，用于演示分级辅助与输出审核，**不构成可靠的防泄漏保障**。

| 运行项 | 值 |
| --- | --- |
| 模式 | {mode_label}（服务端配置，{switch_note}） |
| 后端 | `{backend}` |
| 模型 | `{model}` |
| 意图分类器 | `{classifier}` |

**使用方式**
- 左侧可新建、搜索和管理历史对话；右上设置可选择 Provider 与模型；
- 直接提问算法概念或复杂度问题，回答会尽量讲透、配合具体例子；
- 调试已有代码时，把代码放在 ``` 围栏代码块中一并粘贴（仅作文本分析，**不会执行**）；
- 每条回答下方有一行安全摘要（意图 · 帮助等级 · 审核结论）；完整审计明细可用 `EG_UI_DETAIL=full` 启动查看。

"""

_service: TutorService | None = None
_chat_store = None
_conversation_locks = WeakValueDictionary()


def context_id():
    session = cl.context.session
    return getattr(session, "thread_id", session.id)


def conversation_lock():
    key = context_id()
    lock = _conversation_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _conversation_locks[key] = lock
    return lock


async def restore_conversation(service):
    principal = authenticated_principal_id()
    context = await asyncio.to_thread(
        service.new_context, context_id(), principal_id=principal
    )
    if principal:
        history, selection = await get_chat_store().load_backend_history(
            context_id(), principal
        )
        await asyncio.to_thread(_restore_context_history, context, history)
        if selection:
            try:
                await asyncio.to_thread(service.select_provider, context, selection)
            except ValueError:
                await asyncio.to_thread(
                    service.select_provider, context, service.providers.default
                )
    return context


@cl.header_auth_callback
def local_browser_auth(headers):
    return browser_user(headers)


@cl.data_layer
def get_chat_store():
    global _chat_store
    if _chat_store is None:
        from examguard.chat_store import ChatDataLayer

        _chat_store = ChatDataLayer(
            os.environ.get("EG_CHAT_FILE", ".examguard/chats.sqlite3")
        )
    return _chat_store


async def send_provider_settings(context) -> None:
    service = get_service()
    options = service.providers.options()
    items = {
        f"{o['label']} · {o['model']}": o["selection"]
        for o in options
        if o["available"]
    }
    widgets = [
        Select(
            id="provider_selection",
            label="LLM Provider / 模型",
            items=items,
            initial_value=context.provider_selection,
        )
    ]
    if service.allow_policy_override:
        widgets.append(
            Switch(
                id="exam_mode_switch",
                label="考试限制模式（教师演示）",
                initial=context.exam_mode,
            )
        )
    await cl.ChatSettings(widgets).send()


@cl.on_settings_update
async def on_settings_update(settings: dict) -> None:
    service = get_service()
    async with conversation_lock():
        context = await restore_conversation(service)
        selection = settings.get("provider_selection", context.provider_selection)
        try:
            await asyncio.to_thread(service.select_provider, context, selection)
        except ValueError:
            await cl.Message(
                content="⚠️ 所选 Provider / 模型不可用，请选择已配置的选项。"
            ).send()
            await send_provider_settings(context)
            return
        await _sync_teacher_settings(service)
        await persist_backend_history(context)
    info = service.provider_info(context)
    await cl.Message(content=f"当前模型：**{info['label']} · {info['model']}**").send()


def _snapshot_context(context):
    with context._budget.lock:
        return [dict(m) for m in context.history], context.provider_selection


def _restore_context_history(context, history):
    with context._budget.lock:
        context.history[:] = [dict(m) for m in history[-2 * HISTORY_TURNS_KEPT :]]


async def persist_backend_history(context) -> None:
    principal = authenticated_principal_id()
    if principal:
        history, selection = await asyncio.to_thread(_snapshot_context, context)
        await get_chat_store().save_backend_history(
            cl.context.session.thread_id, principal, history, selection
        )


@cl.on_chat_resume
async def on_chat_resume(thread: dict) -> None:
    service = get_service()
    if not authenticated_principal_id() or thread["id"] != context_id():
        raise PermissionError("恢复对话需要对应工作区身份")
    async with conversation_lock():
        context = await restore_conversation(service)
    await send_provider_settings(context)


async def deliver_response(
    reply, answer: str, chunk_size: int = 24, delay: float = 0.016
) -> None:
    """Deliver the final audited answer with a typewriter cadence.

    Only the approved answer reaches this function (transport drafts are
    consumed and audited upstream); the pacing below is purely cosmetic
    (DeepSeek-style streaming feel) and can be disabled by passing delay=0.
    """
    for offset in range(0, len(answer), chunk_size):
        await reply.stream_token(answer[offset : offset + chunk_size])
        if delay > 0:
            await asyncio.sleep(delay)
    await reply.send()


async def run_turn(service, context, question, code):
    async with conversation_lock():
        # Every window reloads the latest private transcript before generating.
        context = await restore_conversation(service)
        await _sync_teacher_settings(service)
        async with cl.Step(
            name="生成与安全审核", type="run", icon="shield-check"
        ) as step:
            step.output = "正在生成并审核回答…"
            with streaming_transport():
                response = await asyncio.to_thread(
                    service.chat, context, question, code
                )
            await persist_backend_history(context)
            step.output = "审核已完成"
    return response


def get_service() -> TutorService:
    """惰性构建进程级 TutorService；Settings 只在服务端读取。"""
    global _service
    if _service is None:
        settings = Settings.from_env()
        _service = TutorService(settings, providers=ProviderRegistry(settings))
        logger.info(
            "TutorService 已初始化 mode=%s model=%s exam_mode=%s allow_policy_override=%s",
            settings.mode,
            settings.model,
            settings.exam_mode,
            _service.allow_policy_override,
        )
    return _service


def authenticated_principal_id() -> str | None:
    """Only Chainlit's authenticated user is trusted, never message/settings data."""
    user = cl.user_session.get("user")
    identifier = getattr(user, "identifier", None)
    return identifier if isinstance(identifier, str) and identifier.strip() else None


def split_question_and_code(message: str) -> tuple[str, str]:
    """把用户消息中的围栏代码块拆为 (问题, 已有代码)。

    无代码块时原样返回问题；只有代码块时使用中性默认问题（后续由
    分类器判定意图，本函数不做任何安全判断）。
    """
    blocks = [b.strip() for b in _FENCE.findall(message) if b.strip()]
    if not blocks:
        return message.strip(), ""
    code = "\n\n".join(blocks)[-10000:]
    question = _FENCE.sub("", message).strip()
    if not question:
        question = "请基于我粘贴的代码提供辅导。"
    return question, code


def render_audit_markdown(meta: dict, latency_ms: int | None = None) -> str:
    """把白名单元数据渲染为前端审计面板（纯函数，便于测试）。

    展示密度由 EG_UI_DETAIL 控制（默认 brief 学生视图；full 供教师/研究用）：
    - brief：一行摘要——意图 · 帮助等级 · 审核结论 ·（会话累计风险非零时）；
    - full：完整表格（分类方法/置信度/风险标记/审核层/重生成/Token/耗时等）。
    任何模式都不含系统提示词、API Key 或用户原始输入。
    """
    badge = _LEVEL_BADGES.get(meta.get("help_level"), f"L{meta.get('help_level')}")
    if meta.get("audit_reason") == "policy_denial":
        verdict = "⛔ 策略拒答"
    elif meta.get("audit_status") == "unknown":
        verdict = "⚠️ 审核未完成，已安全回退"
    elif meta.get("approved"):
        verdict = "✅ 审核放行"
    else:
        verdict = f"⛔ 已拦截（{meta.get('audit_reason')}）"
    session_risk = meta.get("session_risk")

    if os.environ.get("EG_UI_DETAIL", "brief").strip().lower() != "full":
        parts = [f"意图 `{meta.get('intent')}`", badge, verdict]
        if session_risk is not None and float(session_risk) > 0:
            parts.append(f"会话累计风险 {float(session_risk):.2f}")
        return " · ".join(parts)

    rows = [
        ("请求意图", f"`{meta.get('intent')}`（{meta.get('classify_method')}）"),
        ("允许帮助等级", badge),
        ("审核结论", verdict.replace("审核放行", "放行")),
        ("模型", f"`{meta.get('model')}`"),
    ]
    confidence = meta.get("classify_confidence")
    if confidence is not None:
        rows.append(("分类置信度", f"{float(confidence):.2f}"))
    flags = meta.get("risk_flags") or []
    if flags:
        rows.append(("风险标记", "、".join(f"`{f}`" for f in flags)))
    audit_method = meta.get("audit_method")
    if audit_method:
        rows.append(("审核层", f"`{audit_method}`"))
    regenerations = meta.get("regenerations")
    if regenerations:
        rows.append(("安全重生成", f"{regenerations} 次（首次回答未过审）"))
    if session_risk is not None:
        rows.append(("本会话该题累计风险", f"{float(session_risk):.2f}"))
    disclosures = meta.get("session_disclosures") or []
    if disclosures:
        rows.append(("已披露元素", "、".join(f"`{d}`" for d in disclosures)))

    def _token(value) -> str:
        return str(value) if value is not None else "—"

    tokens = f"{_token(meta.get('input_tokens'))} / {_token(meta.get('output_tokens'))}"
    rows.append(("Token (入/出)", tokens))
    if latency_ms is not None:
        rows.append(("耗时", f"{latency_ms} ms"))
    body = "\n".join(f"| {k} | {v} |" for k, v in rows)
    return (
        f"| 项 | 值 |\n| --- | --- |\n{body}\n\n分类依据：{meta.get('classify_reason')}"
    )


async def _sync_teacher_settings(service: TutorService) -> None:
    """教师演示模式下，把界面开关同步到后端策略（学生常规运行时无此控件）。"""
    if not service.allow_policy_override:
        return
    ui_settings = cl.user_session.get("chat_settings") or {}
    desired = ui_settings.get("exam_mode_switch")
    if desired is None:
        return
    ctx = service.get_context(context_id())
    if bool(desired) != ctx.exam_mode:
        try:
            await asyncio.to_thread(service.set_exam_mode, ctx, bool(desired))
            await cl.Message(
                content=f"⚙️ 教师演示开关：考试限制模式已切换为 **{'开启' if desired else '关闭'}**。"
            ).send()
        except PermissionError:  # pragma: no cover - 仅当配置竞态时出现
            logger.warning("策略切换被拒绝")


@cl.on_chat_start
async def on_chat_start() -> None:
    service = get_service()
    # The welcome text depends only on server settings, so send it first to
    # replace the frontend's blank new-chat view as early as possible; the
    # SQLite restore and provider settings follow.
    settings = service.settings
    switch_note = (
        "教师演示可通过设置面板切换"
        if service.allow_policy_override
        else "界面不可切换"
    )
    mode_label = "考试限制" if settings.exam_mode else "练习演示"
    welcome = cl.Message(
        content=WELCOME_TMPL.format(
            mode_label=mode_label,
            switch_note=switch_note,
            backend=settings.mode,
            model=settings.model,
            classifier=settings.classifier_mode,
        )
    )
    if service.allow_policy_override:
        welcome.actions = [
            cl.Action(
                name="reset_session",
                payload={},
                label="🧹 教师重置安全状态",
                tooltip="教师明确重置：清空该身份本次考试的累计风险及聊天历史",
            )
        ]
    await welcome.send()
    async with conversation_lock():
        context = await restore_conversation(service)
    await send_provider_settings(context)


@cl.action_callback("reset_session")
async def on_reset_session(action: cl.Action) -> None:
    service = get_service()
    session_id = context_id()
    try:
        context = await asyncio.to_thread(
            service.new_context, session_id, principal_id=authenticated_principal_id()
        )
        await asyncio.to_thread(service.reset_security_state, context)
    except PermissionError:
        await cl.Message(
            content="⚠️ 无权限重置安全状态，需服务端启用教师演示权限。"
        ).send()
        return
    await cl.Message(
        content="🧹 教师已重置该身份本次考试的累计安全状态与聊天历史。"
    ).send()


@cl.on_chat_end
async def on_chat_end() -> None:
    # Socket disconnect is temporary. Native thread contexts are shared across
    # windows; durable history is restored before the next turn and bounded by LRU.
    pass


@cl.on_message
async def on_message(message: cl.Message) -> None:
    service = get_service()
    question, code = split_question_and_code(message.content)
    reply = cl.Message(content="")
    started = time.perf_counter()
    try:
        context = await asyncio.to_thread(
            service.new_context,
            context_id(),
            principal_id=authenticated_principal_id(),
        )
        response = await run_turn(service, context, question, code)
    except ValueError as error:
        reply.content = f"⚠️ 输入未通过校验：{error}"
        await reply.send()
        return
    except Exception as error:  # noqa: BLE001 - 前端不向学生暴露堆栈细节
        logger.exception("处理消息失败 session=%s", cl.context.session.id)
        reply.content = (
            f"⚠️ 请求未完成（{type(error).__name__}）。请稍后重试或简化问题。"
        )
        await reply.send()
        return

    latency_ms = int((time.perf_counter() - started) * 1000)
    await deliver_response(reply, response.answer)

    meta = TutorService.display_metadata(response)
    async with cl.Step(
        name="🛡️ 安全审计与策略", type="run", icon="shield-check"
    ) as step:
        step.output = render_audit_markdown(meta, latency_ms)
