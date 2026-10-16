"""ExamGuard Chainlit 学生端界面（方案 B 前端迁移）。

启动：
    .venv/bin/chainlit run chainlit_app.py          # 默认端口 8000
    .venv/bin/chainlit run chainlit_app.py -h 0.0.0.0 --port 8080

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
import re
import time

import chainlit as cl
from chainlit.input_widget import Switch

from examguard.config import Settings
from examguard.service import TutorService

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
- 直接提问算法概念或复杂度问题；
- 调试已有代码时，把代码放在 ``` 围栏代码块中一并粘贴（仅作文本分析，**不会执行**）；
- 每条回答下方的 **安全审计与策略** 步骤可展开，查看意图、帮助等级上限与审核结论。
"""

_service: TutorService | None = None


def get_service() -> TutorService:
    """惰性构建进程级 TutorService；Settings 只在服务端读取。"""
    global _service
    if _service is None:
        settings = Settings.from_env()
        _service = TutorService(settings)
        logger.info(
            "TutorService 已初始化 mode=%s model=%s exam_mode=%s allow_policy_override=%s",
            settings.mode, settings.model, settings.exam_mode, _service.allow_policy_override,
        )
    return _service


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
    """把白名单元数据渲染为前端展示表格（纯函数，便于测试）。

    只包含可展示字段；不含系统提示词、API Key 或用户原始输入。
    """
    badge = _LEVEL_BADGES.get(meta.get("help_level"), f"L{meta.get('help_level')}")
    rows = [
        ("请求意图", f"`{meta.get('intent')}`（{meta.get('classify_method')}）"),
        ("允许帮助等级", badge),
        ("审核结论", "✅ 放行" if meta.get("approved") else f"⛔ 拦截降级（{meta.get('audit_reason')}）"),
        ("模型", f"`{meta.get('model')}`"),
    ]
    confidence = meta.get("classify_confidence")
    if confidence is not None:
        rows.append(("分类置信度", f"{float(confidence):.2f}"))
    flags = meta.get("risk_flags") or []
    if flags:
        rows.append(("风险标记", "、".join(f"`{f}`" for f in flags)))
    def _token(value) -> str:
        return str(value) if value is not None else "—"

    tokens = f"{_token(meta.get('input_tokens'))} / {_token(meta.get('output_tokens'))}"
    rows.append(("Token (入/出)", tokens))
    if latency_ms is not None:
        rows.append(("耗时", f"{latency_ms} ms"))
    body = "\n".join(f"| {k} | {v} |" for k, v in rows)
    return f"| 项 | 值 |\n| --- | --- |\n{body}\n\n分类依据：{meta.get('classify_reason')}"


async def _sync_teacher_settings(service: TutorService) -> None:
    """教师演示模式下，把界面开关同步到后端策略（学生常规运行时无此控件）。"""
    if not service.allow_policy_override:
        return
    ui_settings = cl.user_session.get("chat_settings") or {}
    desired = ui_settings.get("exam_mode_switch")
    if desired is None:
        return
    ctx = service.get_context(cl.context.session.id)
    if bool(desired) != ctx.exam_mode:
        try:
            service.set_exam_mode(ctx, bool(desired))
            await cl.Message(
                content=f"⚙️ 教师演示开关：考试限制模式已切换为 **{'开启' if desired else '关闭'}**。"
            ).send()
        except PermissionError:  # pragma: no cover - 仅当配置竞态时出现
            logger.warning("策略切换被拒绝")


@cl.on_chat_start
async def on_chat_start() -> None:
    service = get_service()
    service.new_context(cl.context.session.id)
    settings = service.settings
    switch_note = (
        "教师演示可通过设置面板切换" if service.allow_policy_override else "界面不可切换"
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
                id="reset_session",
                label="🧹 重置会话",
                tooltip="清空当前会话历史与后端会话状态",
            )
        ]
    await welcome.send()
    if service.allow_policy_override:
        await cl.ChatSettings(
            [
                Switch(
                    id="exam_mode_switch",
                    label="考试限制模式（教师演示）",
                    initial=settings.exam_mode,
                    description="仅当服务端以 EG_ALLOW_POLICY_SWITCH=true 启动时可修改",
                )
            ]
        ).send()


@cl.action_callback("reset_session")
async def on_reset_session(action: cl.Action) -> None:
    service = get_service()
    session_id = cl.context.session.id
    service.drop_context(session_id)
    service.new_context(session_id)
    await cl.Message(content="🧹 会话已重置：历史与后端会话状态均已清空。").send()


@cl.on_chat_end
async def on_chat_end() -> None:
    service = get_service()
    service.drop_context(cl.context.session.id)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    service = get_service()
    await _sync_teacher_settings(service)
    context = service.get_context(cl.context.session.id)

    question, code = split_question_and_code(message.content)
    reply = cl.Message(content="")
    started = time.perf_counter()
    try:
        # 后端为同步调用（含网络 IO），放到工作线程避免阻塞事件循环。
        response = await asyncio.to_thread(service.chat, context, question, code)
    except ValueError as error:
        reply.content = f"⚠️ 输入未通过校验：{error}"
        await reply.send()
        return
    except Exception as error:  # noqa: BLE001 - 前端不向学生暴露堆栈细节
        logger.exception("处理消息失败 session=%s", cl.context.session.id)
        reply.content = f"⚠️ 请求未完成（{type(error).__name__}）。请稍后重试或简化问题。"
        await reply.send()
        return

    latency_ms = int((time.perf_counter() - started) * 1000)
    reply.content = response.answer
    await reply.send()

    meta = TutorService.display_metadata(response)
    async with cl.Step(name="🛡️ 安全审计与策略", type="run") as step:
        step.output = render_audit_markdown(meta, latency_ms)
