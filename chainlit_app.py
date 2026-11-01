"""AlgoSage Chainlit 学生端界面（方案 B 前端迁移）。

启动：
    .venv/bin/python run_app.py          # 默认端口 8000
    .venv/bin/python run_app.py --host 0.0.0.0 --port 8080

设计约束：
- 本文件只负责展示与输入拆分；意图分类、帮助等级、模型调用、输出审核
  全部经由 algosage.service.TutorService 完成（业务逻辑与前端解耦）。
- API Key 只存在于服务端进程；浏览器仅收到白名单元数据。
- 学习保护策略默认由服务端 EXAM_MODE 决定；学生界面只提供模型选择，不可关闭保护。
- 旧 Streamlit 界面 app.py 保留为回退方案。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager, contextmanager
import threading
import time
from pathlib import Path
from weakref import WeakValueDictionary

import chainlit as cl
from chainlit.input_widget import Select, Slider, Switch

from algosage.config import Settings
from algosage.service import TutorService, HISTORY_TURNS_KEPT
from algosage.providers import ProviderRegistry, short_model_name
from algosage.invocation import (
    GenerationAborted,
    generation_abort_requested,
    generation_abort_scope,
    generation_delta_sink,
    streaming_transport,
    timing_summary,
)
from algosage.runtime_paths import runtime_path
from algosage.attachments import prepare_attachment_elements
from algosage.runtime_security import configure_auth_cookies, install_origin_guard
from algosage.state_store import UsageLimitExceeded

logger = logging.getLogger("algosage.chainlit")

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
MAX_MESSAGE_CHARS = 20_000

_LEVEL_BADGES = {
    0: "L0 · 拒答越权请求",
    1: "L1 · 概念与背景知识",
    2: "L2 · 方向性提示",
    3: "L3 · 局部代码诊断",
}

WELCOME_TMPL = """### AlgoSage · 知衡

正式版开发中：面向算法学习的个人问答场景，提供分级辅导、代码调试与输出审核；**不构成代写完整作业的保障**。

| 运行项 | 值 |
| --- | --- |
| 模式 | {mode_label}（服务端配置，{switch_note}） |
| 后端 | `{backend}` |
| 模型 | `{model}` |
| 意图分类器 | `{classifier}` |

**使用方式**
- 左侧可新建、搜索和管理历史对话；右上设置可选择 Provider 与模型；
- 直接提问算法概念或复杂度问题，回答会尽量讲透、配合具体例子；
- 可附加 PNG/JPEG/WebP 图片，以及 PDF、DOCX、PPTX、XLSX、文本和常见代码文件；附件在服务端解析并按不可信输入审核；
- 调试已有代码时，把代码放在 ``` 围栏代码块中一并粘贴（仅作文本分析，**不会执行**）；
- 每条回答下方有一行安全摘要（意图 · 帮助等级 · 审核结论）；完整审计明细可用 `SAGE_UI_DETAIL=full` 启动查看。

"""

_service: TutorService | None = None
_chat_store = None
_student_auth = None
_conversation_locks = WeakValueDictionary()
_title_tasks: set[asyncio.Task] = set()


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


# 进行中的回合（thread_id -> stream_out）：供恢复页面时快照推送在途回答
_turn_streams: dict[str, dict] = {}
# 当前回合的中断事件（thread_id -> threading.Event）。@cl.on_stop 置位，
# run_turn 的 worker 线程轮询它以掐断上游流式请求。
_turn_abort_events: dict[str, threading.Event] = {}


def request_turn_abort() -> None:
    event = _turn_abort_events.get(context_id())
    if event is not None:
        event.set()


@asynccontextmanager
async def _turn_lock_quiet_if_stopping(previous_abort_event):
    """会话锁获取：若上一回合已被用户停止（中断事件已置位），静默等待
    其收尾（秒级）而不显示排队提示；超时才回落为排队提示。"""
    lock = conversation_lock()
    if lock.locked():
        abort_event = previous_abort_event
        if abort_event is None or not abort_event.is_set():
            await cl.Message(content="⏳ 上一条回答仍在生成，本条已排队等待。").send()
            await lock.acquire()
        else:
            # 上一回合已被用户停止：静默等待其中断收尾（秒级），不排队提示。
            try:
                await asyncio.wait_for(lock.acquire(), timeout=8)
            except asyncio.TimeoutError:
                await cl.Message(content="⏳ 上一条回答仍在生成，本条已排队等待。").send()
                await lock.acquire()
    else:
        await lock.acquire()
    try:
        yield
    finally:
        lock.release()


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
        _rebuild_msg_turn_map(context.history)
        if selection:
            # 已有会话沿用该线程上次的选择；记录失效时回退账号默认。
            try:
                await asyncio.to_thread(service.select_provider, context, selection)
            except ValueError:
                await _select_account_default(service, context, principal)
        else:
            # 新会话以账号级“默认模型”起步；无任何提供商时保持未选择。
            await _select_account_default(service, context, principal)
        preferences = await get_chat_store().load_user_app_preferences(principal)
        await asyncio.to_thread(service.set_model_params, context, preferences)
    return context


async def _select_account_default(service, context, principal) -> None:
    """Apply the account default when one exists; else leave the context
    unselected (provider_selection="") so发送被引导而非报错。"""
    target = await user_default_selection(service, principal)
    if not target:
        context.provider_selection = ""
        return
    await asyncio.to_thread(service.select_provider, context, target)


_model_selection_locks: dict[str, asyncio.Lock] = {}


def model_selection_lock(thread_id: str) -> asyncio.Lock:
    key = str(thread_id)
    lock = _model_selection_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _model_selection_locks[key] = lock
    return lock


async def preset_removal_state(principal: str | None) -> tuple[set[str], set[str]]:
    """Preset provider ids the user removed: (hidden-restorable, permanently-deleted)."""
    if not principal:
        return set(), set()
    preferences = await get_chat_store().load_user_app_preferences(principal)

    def as_ids(key: str) -> set[str]:
        value = preferences.get(key)
        return {str(item) for item in value} if isinstance(value, list) else set()

    return as_ids("disabled_providers"), as_ids("deleted_providers")


async def hidden_preset_providers(principal: str | None) -> set[str]:
    """Provider ids invisible in the user's catalog (removed either way)."""
    hidden, purged = await preset_removal_state(principal)
    return hidden | purged


async def user_default_selection(service, principal: str | None) -> str:
    """The account-level default model for new chats.

    服务端不兜底：没有存储的账号默认、或它已失效（条目删除/提供商移除）时
    返回 providers.default —— 操作员未配置 providers.json 时它是空串，
    上层据此禁用发送并引导用户先添加提供商。
    """
    fallback = service.providers.default
    if not principal:
        return fallback
    preferences = await get_chat_store().load_user_app_preferences(principal)
    stored = preferences.get("default_model_selection")
    if not isinstance(stored, str) or not stored:
        return fallback
    try:
        await asyncio.to_thread(service.select_provider_probe, stored, principal)
    except ValueError:
        return fallback
    if not stored.startswith("custom:"):
        if stored.split("::", 1)[0] in await hidden_preset_providers(principal):
            return fallback
    return stored


async def send_model_catalog(context) -> None:
    """Push the authorized catalog and thread-local selection to the composer."""
    service = get_service()
    hidden, purged = await preset_removal_state(context.principal_id)
    default_id = service.providers.default.split("::", 1)[0]
    groups: dict[str, dict] = {}
    for option in service.providers.options():
        if option["id"] in purged:
            continue  # 彻底删除的预置提供商不再出现在任何列表
        group = groups.setdefault(
            option["id"],
            {
                "id": option["id"],
                "label": option["label"],
                "available": bool(option["available"]),
                "source": "preset",
                "locked": option["id"] == default_id,
                "hidden": option["id"] in hidden,
                "models": [],
            },
        )
        group["models"].append(
            {
                "selection": option["selection"],
                "model": option["model"],
                "short_name": short_model_name(option["model"]),
                "available": bool(option["available"]),
            }
        )
    if context.principal_id:
        for entry in service.list_custom_providers(context.principal_id):
            group = groups.setdefault(
                "custom",
                {
                    "id": "custom",
                    "label": "自定义 API（我的密钥）",
                    "available": True,
                    "source": "custom",
                    "locked": False,
                    "hidden": False,
                    "models": [],
                },
            )
            group["models"].append(
                {
                    "selection": entry["selection"],
                    "model": entry["model"],
                    "label": entry["label"],
                    "capabilities": entry.get("capabilities") or {},
                    "available": True,
                }
            )
    try:
        info = (
            service.provider_info(context)
            if context.provider_selection
            else {"label": "未配置模型", "model": ""}
        )
    except ValueError:
        info = {"label": "未配置模型", "model": ""}
    try:
        await cl.send_window_message(
            {
                "type": "algosage-model-state",
                "thread_id": context_id(),
                "current_selection": context.provider_selection,
                "current_label": (
                    short_model_name(info["model"])
                    if info.get("model")
                    else info["label"]
                ),
                "default_selection": await user_default_selection(
                    service, context.principal_id
                ),
                "providers": list(groups.values()),
            }
        )
    except Exception:
        # Catalog state is presentation-only; model execution remains server-authoritative.
        pass


def get_student_auth():
    global _student_auth
    if _student_auth is None:
        from algosage.student_auth import auth_service_from_env

        _student_auth = auth_service_from_env()
    return _student_auth


@cl.password_auth_callback
async def hust_password_auth(username: str, password: str):
    from chainlit.user import User

    try:
        account = get_student_auth().authenticate(username, password)
    except Exception:
        logger.exception(
            "HUST login authentication failed without exposing credentials"
        )
        return None
    if account is None:
        return None
    avatar = get_student_auth().store.get_avatar(account.student_id)
    return User(
        identifier=account.identifier,
        display_name=account.nickname,
        metadata={
            "provider": "hust-student",
            "student_id": account.student_id,
            "session_version": account.session_version,
            "image": (f"/auth/avatar/me?v={account.session_version}" if avatar else ""),
        },
    )


@cl.data_layer
def get_chat_store():
    global _chat_store
    if _chat_store is None:
        from algosage.chat_store import ChatDataLayer

        _chat_store = ChatDataLayer(
            runtime_path("SAGE_CHAT_FILE", ".algosage/chats.sqlite3")
        )
    return _chat_store


def get_service() -> TutorService:
    """惰性构建进程级 TutorService；Settings 只在服务端读取。"""
    global _service
    if _service is None:
        settings = Settings.from_env()
        # 自定义 Provider 持久化到本地 .env（空值 = 仅内存，供测试隔离）。
        custom_env = runtime_path("SAGE_CUSTOM_PROVIDERS_ENV_FILE", ".env")
        _service = TutorService(
            settings,
            providers=ProviderRegistry(settings),
            custom_provider_env_file=custom_env or None,
        )
        logger.info(
            "TutorService 已初始化 mode=%s model=%s exam_mode=%s allow_policy_override=%s",
            settings.mode,
            settings.model,
            settings.exam_mode,
            _service.allow_policy_override,
        )
    return _service


# Chainlit's standard element endpoint returns metadata. Local uploads also
# need a durable byte endpoint because the temporary upload path is removed
# after the websocket session ends.
try:  # pragma: no cover - exercised through the running Chainlit server
    from fastapi import HTTPException
    from fastapi.routing import APIRoute
    from fastapi.responses import FileResponse
    from chainlit.types import DeleteFeedbackRequest, UpdateFeedbackRequest
    from chainlit.data.acl import is_thread_author
    from chainlit.server import UserParam, app as _chainlit_server_app

    configure_auth_cookies()
    install_origin_guard(_chainlit_server_app)

    @_chainlit_server_app.get(
        "/project/element/{thread_id}/{element_id}/content",
        include_in_schema=False,
    )
    async def get_persisted_element_content(
        thread_id: str, element_id: str, current_user: UserParam
    ):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        await is_thread_author(current_user.identifier, thread_id)
        store = get_chat_store()
        path = store.get_element_path(thread_id, element_id)
        if not path or not Path(path).is_file():
            raise HTTPException(status_code=404, detail="Element not found")
        element = await store.get_element(thread_id, element_id)
        return FileResponse(
            path,
            media_type=(element or {}).get("mime") or "application/octet-stream",
            filename=(element or {}).get("name") or element_id,
        )

    # Chainlit authenticates /feedback but delegates ownership entirely to the
    # data layer without passing the caller. Replace its routes with owner-
    # checked versions so one workspace cannot alter another workspace's rating.
    # FastAPI >= 0.143 的 include_router 是组合式委托路由：chainlit 原版
    # 路由实际保留在源 APIRouter（chainlit.server.router）内部，仅从
    # app.router.routes 按 path 移除是空操作，原版无 owner 检查的
    # /feedback 仍会先命中。必须把源 router 内的 PUT/DELETE /feedback 一并
    # 剥离，改写版才可达。
    from chainlit.server import router as _chainlit_source_router

    _feedback_route_targets = (
        _chainlit_server_app.router.routes,
        _chainlit_source_router.routes,
    )
    for _target in _feedback_route_targets:
        for route in list(_target):
            if (
                isinstance(route, APIRoute)
                and route.path == "/feedback"
                and route.methods.intersection({"PUT", "DELETE"})
            ):
                _target.remove(route)

    @_chainlit_server_app.put("/feedback", include_in_schema=False)
    async def update_owned_feedback(
        update: UpdateFeedbackRequest, current_user: UserParam
    ):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        try:
            feedback_id = await get_chat_store().upsert_feedback_for(
                update.feedback, current_user.identifier
            )
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"success": True, "feedbackId": feedback_id}

    @_chainlit_server_app.delete("/feedback", include_in_schema=False)
    async def delete_owned_feedback(
        payload: DeleteFeedbackRequest, current_user: UserParam
    ):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        try:
            deleted = await get_chat_store().delete_feedback_for(
                payload.feedbackId, current_user.identifier
            )
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        if not deleted:
            raise HTTPException(status_code=404, detail="Feedback not found")
        return {"success": True}

    from algosage.student_auth import _chainlit_router, _move_routes_before_catchall

    models_router = _chainlit_router(_chainlit_server_app)

    async def push_provider_views() -> None:
        """Re-push panel settings + catalog after provider changes.

        ChatSettings.send() updates the drawer's select in place, so the
        默认模型 list reflects the change without the collapse/reopen cycle
        a confirm submit would cause. Best-effort: outside an active
        websocket context (tests, detached calls) this is a no-op.
        """
        try:
            context = await restore_conversation(get_service())
            await send_provider_settings(context)
            await send_model_catalog(context)
        except LookupError:
            pass  # 无运行中的 Chainlit 上下文（测试/无连接），静默跳过
        except Exception as error:  # noqa: BLE001 - 刷新失败不影响操作本身
            logger.info(
                "provider views re-push skipped error_type=%s", type(error).__name__
            )

    # 提供商增删发生在 HTTP 路由里，那里没有 websocket 上下文，
    # ChatSettings.send() 必然抛 ChainlitContextException——上面的推送
    # 在真实页面里从未生效。改为客户端注册成功后经 socket 直发本事件，
    # 在 ws 上下文里执行重发，面板的"默认模型"列表即时更新。
    from chainlit.socket import sio as _chainlit_sio, init_ws_context

    @_chainlit_sio.on("algosage_refresh_provider_views")
    async def refresh_provider_views(sid: str, _payload: dict | None = None) -> None:
        try:
            init_ws_context(sid)
            context = await restore_conversation(get_service())
            await send_provider_settings(context)
            await send_model_catalog(context)
        except Exception as error:  # noqa: BLE001 - 刷新失败不影响操作本身
            logger.info(
                "provider views refresh skipped error_type=%s", type(error).__name__
            )

    @_chainlit_sio.on("chat_settings_change")
    async def handle_chat_settings_change(
        sid: str, payload: dict | None = None
    ) -> None:
        """Persist partial settings updates sent by the autosave UI.

        The browser keeps the settings drawer open by sending a small delta over
        the current Socket.IO connection.  It must enter the same callback as
        Chainlit's native confirm action; otherwise controls appear to change
        locally but account defaults (especially ``model_catalog``) disappear
        when the drawer is reopened.
        """
        try:
            init_ws_context(sid)
            if isinstance(payload, dict):
                await on_settings_update(payload)
        except Exception as error:  # noqa: BLE001 - UI autosave must not kill ws
            logger.info(
                "chat settings change skipped error_type=%s", type(error).__name__
            )

    def _catalog_groups(service, options):
        groups: dict[str, dict] = {}
        for option in options:
            group = groups.setdefault(
                option["id"],
                {
                    "id": option["id"],
                    "label": option["label"],
                    "available": bool(option["available"]),
                    "models": [],
                },
            )
            group["models"].append(
                {
                    "selection": option["selection"],
                    "model": option["model"],
                    "available": bool(option["available"]),
                }
            )
        return list(groups.values())

    @models_router.post("/debug/ui-trace", include_in_schema=False)
    async def ui_trace(payload: dict, current_user: UserParam):
        """前端 UI 时序埋点：composer 注入失败/成功的毫秒级时间线上报。

        仅写本地日志，用于排查"设置按钮在重挂载窗口消失"这类与帧时序相关
        的偶发问题；事件内容不含消息正文，只有 DOM 注入事件名与时间戳。
        """
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        events = (payload or {}).get("events") or []
        if not isinstance(events, list) or not events:
            return {"ok": True}
        try:
            compact = json.dumps(events[:20], ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return {"ok": True}
        logger.info(
            "ui-trace principal=%s events=%s", current_user.identifier, compact[:800]
        )
        return {"ok": True}

    @models_router.get("/models/catalog", include_in_schema=False)
    async def read_model_catalog(thread_id: str = "", *, current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        service = get_service()
        hidden, purged = await preset_removal_state(current_user.identifier)
        default_id = service.providers.default.split("::", 1)[0]
        groups = _catalog_groups(service, service.providers.options())
        groups = [group for group in groups if group["id"] not in purged]
        for group in groups:
            group["source"] = "preset"
            group["locked"] = group["id"] == default_id
            group["hidden"] = group["id"] in hidden
        custom = service.list_custom_providers(current_user.identifier)
        if custom:
            groups.append(
                {
                    "id": "custom",
                    "label": "自定义 API（我的密钥）",
                    "available": True,
                    "source": "custom",
                    "locked": False,
                    "hidden": False,
                    "models": [
                        {
                            "selection": entry["selection"],
                            "model": entry["model"],
                            "label": entry["label"],
                            "short_name": short_model_name(entry["model"]),
                            "capabilities": entry.get("capabilities") or {},
                            "available": True,
                        }
                        for entry in custom
                    ],
                }
            )
        current = service.providers.default
        if thread_id:
            try:
                _, current = await get_chat_store().load_backend_history(
                    thread_id, current_user.identifier
                )
            except PermissionError as error:
                raise HTTPException(
                    status_code=403, detail="Thread owner mismatch"
                ) from error
        current = current or await user_default_selection(
            service, current_user.identifier
        )
        try:
            current_info = service.providers.info(current)
            current_available = True
        except ValueError:
            # 自定义模型不在预置注册表里，从用户条目解析展示名。
            entry = next(
                (
                    e
                    for e in service.list_custom_providers(current_user.identifier)
                    if e["selection"] == current
                ),
                None,
            )
            if entry:
                current_info = {"label": entry["label"], "model": entry["model"]}
                current_available = True
            else:
                current_info = {"label": "未配置模型", "model": ""}
                current_available = False
        current_label = short_model_name(current_info["model"])
        if current_info.get("model") is None:
            current_label = current_info["label"]
        return {
            "default_selection": await user_default_selection(
                service, current_user.identifier
            ),
            "current_selection": current,
            "current_available": current_available,
            "current_label": current_label,
            "providers": groups,
        }

    @models_router.post("/models/preset", include_in_schema=False)
    async def set_preset_provider_visibility(payload: dict, current_user: UserParam):
        """Hide / restore / permanently delete a preset provider for this user only."""
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        provider_id = str((payload or {}).get("provider_id") or "").strip()
        # 兼容旧载荷 {"hidden": bool}；新载荷用 action 表达三种状态。
        action = str((payload or {}).get("action") or "").strip().lower()
        if not action:
            action = "hide" if payload.get("hidden") else "restore"
        if action not in {"hide", "restore", "delete"}:
            raise HTTPException(status_code=400, detail="Unknown action")
        service = get_service()
        preset_ids = {group["id"] for group in service.providers.options()}
        default_id = service.providers.default.split("::", 1)[0]
        if provider_id not in preset_ids:
            raise HTTPException(status_code=404, detail="Provider not found")
        if provider_id == default_id:
            raise HTTPException(status_code=400, detail="默认 Provider 不能移除")
        preferences = await get_chat_store().load_user_app_preferences(
            current_user.identifier
        )

        def as_ids(key: str) -> set[str]:
            value = preferences.get(key)
            return {str(item) for item in value} if isinstance(value, list) else set()

        hidden = as_ids("disabled_providers")
        purged = as_ids("deleted_providers")
        hidden.discard(provider_id)
        purged.discard(provider_id)
        if action == "hide":
            hidden.add(provider_id)
        elif action == "delete":
            purged.add(provider_id)
        preferences["disabled_providers"] = sorted(hidden)
        preferences["deleted_providers"] = sorted(purged)
        await get_chat_store().save_user_app_preferences(
            current_user.identifier, preferences
        )
        await push_provider_views()
        return {
            "success": True,
            "provider_id": provider_id,
            "action": action,
        }

    @models_router.post("/models/discover", include_in_schema=False)
    async def discover_models(payload: dict, current_user: UserParam):
        """List models on a user-supplied endpoint; ≤27B ones are addable."""
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid payload")
        base_url = str(payload.get("base_url") or "").strip()
        api_key = str(payload.get("api_key") or "").strip()
        if not base_url or not api_key:
            raise HTTPException(status_code=400, detail="Base URL 和 API Key 不能为空")
        service = get_service()
        try:
            models = await asyncio.to_thread(
                service.discover_custom_models, base_url, api_key
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except (
            Exception
        ) as error:  # noqa: BLE001 - vendor errors surface as one message
            logger.info("model discovery failed error_type=%s", type(error).__name__)
            raise HTTPException(
                status_code=400,
                detail="无法获取模型列表：请检查 Base URL、API Key 与网络连通性",
            ) from error
        # 列表默认不探测能力；前端点选模型后经 /models/probe 逐个探测。
        return {"models": models}

    @models_router.post("/models/probe", include_in_schema=False)
    async def probe_single_model(payload: dict, current_user: UserParam):
        """对单个模型做真实能力探测（思考/多模态/联网），按需调用。"""
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid payload")
        base_url = str(payload.get("base_url") or "").strip()
        api_key = str(payload.get("api_key") or "").strip()
        model = str(payload.get("model") or "").strip()
        if not base_url or not api_key or not model:
            raise HTTPException(
                status_code=400, detail="Base URL、API Key 与模型不能为空"
            )
        service = get_service()
        from algosage.providers import validate_public_base_url

        try:
            validate_public_base_url(base_url)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if not model.strip():
            raise HTTPException(status_code=400, detail="模型不能为空")
        from algosage.custom_providers import probe_model_capabilities

        try:
            capabilities = await asyncio.wait_for(
                asyncio.to_thread(probe_model_capabilities, base_url, api_key, model),
                timeout=90,
            )
        except asyncio.TimeoutError:
            capabilities = {
                "thinking": None,
                "multimodal": None,
                "web_search": None,
                "is_chat": None,
            }
        except Exception as error:  # noqa: BLE001 - 探测失败不影响响应
            logger.info(
                "capability probe failed model=%s error_type=%s",
                model,
                type(error).__name__,
            )
            capabilities = {
                "thinking": None,
                "multimodal": None,
                "web_search": None,
                "is_chat": None,
            }
        return {"capabilities": capabilities}

    @models_router.post("/models/custom", include_in_schema=False)
    async def register_custom_model(payload: dict, current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid payload")
        service = get_service()
        try:
            raw_capabilities = payload.get("capabilities")
            # 探测不明确的能力为 None：保留真实探测结果，不回退命名推断。
            capabilities = (
                raw_capabilities
                if isinstance(raw_capabilities, dict)
                and raw_capabilities
                and all(
                    isinstance(value, bool) or value is None
                    for value in raw_capabilities.values()
                )
                else None
            )
            entry = await asyncio.to_thread(
                service.register_custom_provider,
                current_user.identifier,
                base_url=str(payload.get("base_url") or "").strip(),
                api_key=str(payload.get("api_key") or "").strip(),
                model=str(payload.get("model") or "").strip(),
                label=str(payload.get("label") or "").strip(),
                capabilities=capabilities,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        await push_provider_views()
        return {"success": True, "entry": entry}

    @models_router.delete("/models/custom", include_in_schema=False)
    async def remove_custom_model(payload: dict, current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        selection = str((payload or {}).get("selection") or "").strip()
        if not selection.startswith("custom:"):
            raise HTTPException(status_code=400, detail="Invalid selection")
        service = get_service()
        removed = await asyncio.to_thread(
            service.remove_custom_provider, current_user.identifier, selection
        )
        if not removed:
            raise HTTPException(status_code=404, detail="Entry not found")
        await push_provider_views()
        return {"success": True}

    @models_router.post("/models/select", include_in_schema=False)
    async def select_thread_model(payload: dict, current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        thread_id = str(payload.get("thread_id") or "").strip()
        selection = str(payload.get("selection") or "").strip()
        if len(thread_id) > 128:
            raise HTTPException(status_code=400, detail="Invalid thread")
        if not selection:
            raise HTTPException(status_code=400, detail="Model selection is required")
        service = get_service()
        try:
            if not thread_id:
                # 欢迎页（尚无线程）选模型：落为账号级默认，新对话由此继承。
                principal = current_user.identifier
                await asyncio.to_thread(
                    service.select_provider_probe, selection, principal
                )
                preferences = await get_chat_store().load_user_app_preferences(
                    principal
                )
                preferences["default_model_selection"] = selection
                await get_chat_store().save_user_app_preferences(
                    principal, preferences
                )
                settings = await asyncio.to_thread(
                    service.select_provider_probe, selection, principal
                )
                return {
                    "success": True,
                    "selection": selection,
                    "label": short_model_name(settings.model),
                    "model": settings.model,
                }
            async with model_selection_lock(thread_id):
                context = await asyncio.to_thread(
                    service.new_context, thread_id, principal_id=current_user.identifier
                )
                await asyncio.to_thread(service.select_provider, context, selection)
                info = service.provider_info(context)
                thread = await get_chat_store().get_thread(thread_id)
                if thread is not None:
                    history, _ = await get_chat_store().load_backend_history(
                        thread_id, current_user.identifier
                    )
                    await get_chat_store().save_backend_history(
                        thread_id,
                        current_user.identifier,
                        history,
                        selection,
                    )
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "success": True,
            "selection": context.provider_selection,
            # 与目录 current_label、菜单行一致：展示用短名（去组织前缀），
            # 否则切换后按钮回退成 "Provider · 完整模型名"。
            "label": short_model_name(info["model"]),
            "model": info["model"],
        }

    _move_routes_before_catchall(
        _chainlit_router(_chainlit_server_app),
        (
            "/models/catalog",
            "/models/select",
            "/models/discover",
            "/models/custom",
            "/models/preset",
            "/models/probe",
        ),
    )

    preferences_router = _chainlit_router(_chainlit_server_app)

    @preferences_router.get("/auth/preferences", include_in_schema=False)
    async def read_preferences(current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return await get_chat_store().load_user_app_preferences(current_user.identifier)

    @preferences_router.put("/auth/preferences", include_in_schema=False)
    async def update_preferences(payload: dict, current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid preferences")
        service = get_service()
        normalized = service.normalize_model_params(
            payload, service.providers.resolve(service.providers.default)
        )
        saved = await get_chat_store().save_user_app_preferences(
            current_user.identifier, normalized
        )
        return {"success": True, "preferences": saved}

    @preferences_router.post("/chat/options", include_in_schema=False)
    async def set_chat_options(payload: dict, current_user: UserParam):
        """输入区的生成选项（深度思考 / 联网搜索）：账号级即时保存。"""
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        allowed = {"deep_think", "web_search"}
        preferences = await get_chat_store().load_user_app_preferences(
            current_user.identifier
        )
        changed = {}
        for key in sorted(allowed):
            if isinstance(payload, dict) and key in payload:
                preferences[key] = bool(payload[key])
                changed[key] = preferences[key]
        if not changed:
            raise HTTPException(status_code=400, detail="No options provided")
        await get_chat_store().save_user_app_preferences(
            current_user.identifier, preferences
        )
        return {"success": True, "options": changed}

    from algosage.student_auth import _chainlit_router, _move_routes_before_catchall
    from algosage.student_auth import install_profile_routes

    from algosage.student_auth import install_student_auth

    install_student_auth(_chainlit_server_app, get_student_auth)
    _move_routes_before_catchall(
        _chainlit_router(_chainlit_server_app),
        ("/auth/preferences", "/chat/options"),
    )
    install_profile_routes(
        _chainlit_server_app,
        get_student_auth,
        get_chat_store,
        lambda: get_service().state_store,
        get_service,
    )
except ImportError:
    # The service layer and unit tests do not require the HTTP route.
    pass


async def send_provider_settings(context) -> None:
    service = get_service()
    options = service.providers.options()
    hidden = await hidden_preset_providers(context.principal_id)
    items: dict[str, str] = {}
    for o in options:
        if o["id"] in hidden:
            continue
        short = short_model_name(o["model"])
        label = short if list(items).count(short) < 1 else f"{short}（{o['label']}）"
        items[label] = o["selection"]
    if context.principal_id:
        for entry in service.list_custom_providers(context.principal_id):
            short = short_model_name(entry["model"])
            label = short if short not in items else f"{short}（{entry['label']}）"
            items[label] = entry["selection"]
    try:
        provider = service._resolve_selection_settings(
            context, context.provider_selection
        )
    except ValueError:
        # 无可用提供商：采样控件仍基于服务端基础参数渲染。
        provider = service.settings
    params = service.normalize_model_params(context.model_params or {}, provider)
    max_output = min(
        int(params.get("max_output_tokens", provider.max_output_tokens)),
        provider.max_output_tokens,
    )
    widgets = []
    if items:
        widgets.append(
            Select(
                id="model_catalog",
                label="默认模型",
                items=items,
                initial_value=await user_default_selection(
                    service, context.principal_id
                ),
                description="仅支持参数量在27B以内的模型。",
            )
        )
    widgets.extend(
        [
            Slider(
                id="temperature",
                label="Temperature（创造性）",
                min=0,
                max=2,
                step=0.05,
                initial=float(params.get("temperature", 0.7)),
                description="控制回答的发散程度：越低越稳定、越高越有创造性。",
            ),
            Slider(
                id="frequency_penalty",
                label="Frequency Penalty（减少重复）",
                min=0,
                max=2,
                step=0.05,
                initial=float(params.get("frequency_penalty", 0)),
                description="越高越不容易反复使用相同的词句。",
            ),
            Slider(
                id="presence_penalty",
                label="Presence Penalty（引入新话题）",
                min=0,
                max=2,
                step=0.05,
                initial=float(params.get("presence_penalty", 0)),
                description="越高越倾向于展开新内容、不纠缠旧话题。",
            ),
            Slider(
                id="max_output_tokens",
                label="最大输出长度（Token）",
                min=128,
                max=provider.max_output_tokens,
                step=128,
                initial=max_output,
            ),
            Switch(
                id="autoscroll",
                label="回复时自动滚动",
                initial=bool(params.get("autoscroll", True)),
            ),
            Switch(
                id="keep_drafts",
                label="切换会话时保留草稿",
                initial=bool(params.get("keep_drafts", True)),
            ),
            Switch(
                id="animations",
                label="启用界面动画",
                initial=bool(params.get("animations", True)),
            ),
            Switch(
                id="audit_summary",
                label="显示安全摘要",
                initial=bool(params.get("audit_summary", True)),
            ),
            Select(
                id="font_size",
                label="字体大小",
                initial_value=str(params.get("font_size", "standard")),
                items={"小号": "small", "标准": "standard", "大号": "large"},
            ),
        ]
    )
    platform = _client_platform()
    if platform != "mobile":  # 移动端虚拟键盘无修饰键，不展示该设置
        widgets.append(
            Select(
                id="enter_behavior",
                label="发送快捷键",
                initial_value=str(params.get("enter_behavior", "enter")),
                # Chainlit 2.11 以 {显示名: 内部值} 序列化 items，方向反了会渲染成空下拉框。
                items={
                    "Enter": "enter",
                    (
                        "Command+Enter" if platform == "mac" else "Ctrl+Enter"
                    ): "ctrl_enter",
                },
            )
        )
    await cl.ChatSettings(widgets).send()
    await send_model_catalog(context)


def _client_platform() -> str:
    """Classify the client from the socket handshake: mobile / mac / desktop.

    The behaviour layer already accepts Ctrl and Command interchangeably; this
    only picks the modifier label and whether the shortcut setting appears.
    """
    context = getattr(cl, "context", None)
    session = getattr(context, "session", None)
    environ = getattr(session, "environ", None) or {}
    agent = str(environ.get("HTTP_USER_AGENT") or "").lower()
    if any(token in agent for token in ("iphone", "ipad", "ipod", "android", "mobile")):
        return "mobile"
    if "macintosh" in agent or "mac os x" in agent:
        return "mac"
    return "desktop"


def _normalize_ui_settings(settings: dict, current: dict | None = None) -> dict:
    """Merge a settings payload onto the current params.

    即时保存会让单控件变更以不完整载荷提交；缺失键一律继承现值，
    只有显式提供的键才参与校验与覆盖。
    """
    current = current or {}
    normalized = dict(settings)
    if "enter_behavior" in normalized:
        if normalized["enter_behavior"] not in {"enter", "ctrl_enter"}:
            normalized["enter_behavior"] = "enter"
    elif "enter_behavior" in current:
        normalized["enter_behavior"] = current["enter_behavior"]
    if "font_size" in normalized:
        if normalized["font_size"] not in {"small", "standard", "large"}:
            normalized["font_size"] = "standard"
    elif "font_size" in current:
        normalized["font_size"] = current["font_size"]
    for key in ("autoscroll", "keep_drafts", "animations", "audit_summary"):
        if key in normalized or key in current:
            normalized[key] = bool(
                normalized[key] if key in normalized else current[key]
            )
    for key in ("deep_think", "web_search"):
        if key in normalized or key in current:
            normalized[key] = bool(
                normalized[key] if key in normalized else current[key]
            )
    for key in (
        "temperature",
        "frequency_penalty",
        "presence_penalty",
        "max_output_tokens",
    ):
        source = normalized.get(key, current.get(key))
        try:
            normalized[key] = float(source)
        except (TypeError, ValueError):
            normalized.pop(key, None)
    normalized.pop("top_p", None)  # 面板已移除该控件；保留原值而非强改为 0
    return normalized


@cl.on_settings_update
async def on_settings_update(settings: dict) -> None:
    service = get_service()
    async with conversation_lock():
        context = await restore_conversation(service)
        # “默认模型”是账号级偏好：只持久化，不切换当前线程的模型。
        requested_model = str(settings.get("model_catalog") or "").strip()
        principal = authenticated_principal_id()
        if principal and requested_model:
            try:
                await asyncio.to_thread(
                    service.select_provider_probe,
                    requested_model,
                    principal,
                )
                if not requested_model.startswith("custom:") and (
                    requested_model.split("::", 1)[0]
                    in await hidden_preset_providers(principal)
                ):
                    raise ValueError("provider hidden")
                preferences = _normalize_ui_settings(
                    settings, current=context.model_params
                )
                preferences["default_model_selection"] = requested_model
                await get_chat_store().save_user_app_preferences(principal, preferences)
                # 当前界面是"新建对话"（尚无任何轮次）时，跟随新默认立即切换，
                # 让窗口与面板所见一致；已打开的对话保持自身选择不受影响。
                if not context.history:
                    try:
                        await asyncio.to_thread(
                            service.select_provider, context, requested_model
                        )
                    except ValueError:
                        pass
            except ValueError:
                pass  # 未知/失效/已移除的提供商不落为默认模型
        await asyncio.to_thread(
            service.set_model_params,
            context,
            _normalize_ui_settings(settings, current=context.model_params),
        )
        if principal:
            await get_chat_store().save_user_app_preferences(
                principal, context.model_params
            )
        # Account preferences are already saved above. A late settings event
        # may reference a deleted chat; do not create/revive one as a side effect.
        await persist_backend_history(context, only_existing=True)
        await send_model_catalog(context)
        # 重新下发设置：面板里的模型列表需要包含新添加的自定义模型。
        await send_provider_settings(context)
    try:
        await cl.send_window_message(
            {
                "type": "algosage-preferences",
                "preferences": {
                    key: context.model_params.get(key)
                    for key in (
                        "enter_behavior",
                        "autoscroll",
                        "keep_drafts",
                        "animations",
                        "font_size",
                        "audit_summary",
                    )
                },
            }
        )
    except Exception:
        pass  # 偏好仍会持久化；旧客户端下次打开设置时可读取元数据。
    # 不在对话流里回显设置结果：面板自身的确认与控件值即为反馈。


def _msg_turn_map():
    mapping = cl.user_session.get("msg_turn_index")
    if mapping is None:
        mapping = {}
        try:
            cl.user_session.set("msg_turn_index", mapping)
        except AttributeError:
            pass  # 测试 fake 或无 set 能力时仅用局部映射
    return mapping


def _store_msg_turn_map(mapping) -> None:
    try:
        cl.user_session.set("msg_turn_index", mapping)
    except AttributeError:
        pass


def _rebuild_msg_turn_map(history):
    """重建 msg_id -> 轮次映射（历史恢复后；仅 user 消息携带 msg_id）。"""
    mapping = {}
    for index, entry in enumerate(history):
        msg_id = entry.get("msg_id")
        if entry.get("role") == "user" and msg_id:
            mapping[msg_id] = index // 2
    _store_msg_turn_map(mapping)
    return mapping


def _snapshot_context(context):
    with context._budget.lock:
        return [dict(m) for m in context.history], context.provider_selection


def _restore_context_history(context, history):
    with context._budget.lock:
        context.history[:] = [dict(m) for m in history[-2 * HISTORY_TURNS_KEPT :]]


async def persist_backend_history(context, *, only_existing: bool = False) -> None:
    principal = authenticated_principal_id()
    if principal:
        history, selection = await asyncio.to_thread(_snapshot_context, context)
        await get_chat_store().save_backend_history(
            cl.context.session.thread_id,
            principal,
            history,
            selection,
            only_existing=only_existing,
        )


async def schedule_title(
    service, context, response, question, emitter, session, timeout=8
):
    """Start one ancillary job after audit, concurrently with final answer delivery."""
    if (
        emitter is None
        or not context.principal_id
        or getattr(session, "restored", False)
        or os.environ.get("SAGE_AUTO_TITLES", "true").lower() in {"false", "0", "no"}
        or not response.audit.approved
        or response.audit.reason == "policy_denial"
        or len(response.call_trace) >= service.settings.max_calls_per_turn
        or timeout <= 0
    ):
        return None
    store = get_chat_store()
    thread_id, principal = context.session_id, context.principal_id
    if not await store.claim_title(thread_id, principal):
        return None
    selection = context.provider_selection

    async def complete():
        try:
            title = await asyncio.to_thread(
                service.generate_title,
                selection,
                question,
                min(8, timeout),
                principal,
            )
            if (
                await store.finish_title(thread_id, principal, title)
                and session.thread_id == thread_id
                and not getattr(session, "to_clear", False)
            ):
                # Chainlit 2.11 refetches persisted sidebar titles on this event.
                # Do not call init_thread(): that would persist the raw question again.
                await emitter.emit(
                    "first_interaction",
                    {"interaction": title, "thread_id": thread_id},
                )
        except (
            Exception
        ) as error:  # noqa: BLE001 - optional title failures must not affect chat
            logger.info("Sidebar title skipped error_type=%s", type(error).__name__)

    task = asyncio.create_task(complete())
    _title_tasks.add(task)
    task.add_done_callback(_title_tasks.discard)
    return task


@cl.on_chat_resume
async def on_chat_resume(thread: dict) -> None:
    service = get_service()
    logger.info(
        "resume-check thread=%s context_id=%s principal=%s",
        thread.get("id"),
        context_id(),
        authenticated_principal_id(),
    )
    if not authenticated_principal_id() or thread["id"] != context_id():
        logger.info("resume DENIED")
        raise PermissionError("恢复对话需要对应工作区身份")
    logger.info("resume OK")
    # 回合进行中恢复页面时，把在途回答的当前快照推流给刚恢复的界面：
    # 前端会在同一消息 id 上继续追加后续增量，实现“接续生成一半的状态”。
    stream = _turn_streams.get(thread["id"])
    if stream is not None and stream.get("reply") is not None and stream.get("text"):
        try:
            await cl.context.emitter.emit(
                "stream_token", {"id": stream["reply"].id, "token": stream["text"]}
            )
        except Exception as error:  # noqa: BLE001 - 快照推送失败不影响主流程
            logger.info(
                "Resume stream snapshot skipped error_type=%s", type(error).__name__
            )
    # 不等待生成锁：回合进行中点侧边栏切换/恢复必须即时可用，
    # 否则整个流式生成期间（可达一两分钟）会话切换都表现为"没反应"。
    # 恢复只读历史并重发设置，与进行中的回合共享只读数据并无冲突。
    context = await restore_conversation(service)
    await send_provider_settings(context)
    await send_model_catalog(context)


async def deliver_response(
    reply, answer: str, chunk_size: int = 24, delay: float = 0
) -> None:
    """Deliver the final audited answer without artificial pacing by default.

    Only the approved answer reaches this function (transport drafts are
    consumed and audited upstream); the pacing below is purely cosmetic
    Optional pacing is retained for callers that explicitly pass a delay.
    """
    for offset in range(0, len(answer), chunk_size):
        await reply.stream_token(answer[offset : offset + chunk_size])
        if delay > 0:
            await asyncio.sleep(delay)
    await reply.send()


_MATH_DISPLAY_RE = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
_MATH_INLINE_RE = re.compile(r"\\\((.+?)\\\)", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)", re.DOTALL)


def normalize_math_delimiters(text: str) -> str:
    """把模型输出的 \\[…\\] / \\[…\\) 定界符统一成 Chainlit 渲染器认识的
    $$…$$ / $…$（Chainlit 的 KaTeX 管线不处理方括号/圆括号写法，公式会
    以原文形式露出）。代码块内容保持原样。"""
    if "\\" not in text:
        return text

    def convert(segment: str) -> str:
        segment = _MATH_DISPLAY_RE.sub(r"$$\g<1>$$", segment)
        return _MATH_INLINE_RE.sub(r"$\g<1>$", segment)

    parts = _CODE_FENCE_RE.split(text)
    return "".join(
        part if i % 2 else convert(part) for i, part in enumerate(parts)
    )


def _make_delta_sink(context, stream_out):
    """Live consumer for generation deltas (runs on the worker thread while
    the upstream stream loop iterates). Schedules UI updates onto the main
    event loop; the audited answer may still replace everything streamed."""
    loop = asyncio.get_running_loop()

    def sink(text_so_far, reasoning_so_far):
        async def apply():
            async with stream_out["ui_lock"]:
                step = stream_out.get("reasoning_step")
                if reasoning_so_far and step is None:
                    step = cl.Step(name="model-reasoning", type="run", icon="brain")
                    await step.send()
                    stream_out["reasoning_step"] = step
                if step is not None and reasoning_so_far != stream_out.get("reasoning", ""):
                    step.output = normalize_math_delimiters(reasoning_so_far)
                    await step.update()
                    stream_out["reasoning"] = reasoning_so_far
                reply = stream_out.get("reply")
                if reply is None:
                    reply = cl.Message(content="")
                    stream_out["reply"] = reply
                normalized = normalize_math_delimiters(text_so_far)
                if len(normalized) < len(stream_out.get("streamed_text", "")):
                    # 重生成：文本从头开始，重置已播内容。
                    reply.content = ""
                    stream_out["streamed_text"] = ""
                    stream_out["streamed"] = 0
                # 尾部若是未完成的转义序列（如单独的反斜杠），留待更多
                # 字符到达后再定形，避免定界符转换改动已播出的内容。
                streamable = normalized
                if streamable.endswith("\\"):
                    streamable = streamable[:-1]
                streamed_len = len(stream_out.get("streamed_text", ""))
                tail = streamable[streamed_len:]
                if tail:
                    await reply.stream_token(tail)
                    stream_out["streamed_text"] = streamable
                    stream_out["streamed"] = streamed_len
                    stream_out["text"] = normalized


        asyncio.run_coroutine_threadsafe(apply(), loop)

    return sink


async def run_turn(
    service,
    context,
    question,
    code,
    msg_id="",
    edit_turn=None,
    attachments=(),
    stream_out=None,
):
    started = time.perf_counter()
    stream_out = stream_out if stream_out is not None else {}
    # 流式 UI 更新串行锁：思考/正文增量回调从 worker 线程调度到主循环，
    # await 期间后续回调若并发执行会在 Step 创建前重复创建（4ms 内 4 条）。
    stream_out.setdefault("ui_lock", asyncio.Lock())
    # 覆盖前先捕获上一回合的中断事件：若用户刚点过停止，本次获取会话锁
    # 时应静默等待旧回合收尾，而不是显示排队提示。
    previous_abort_event = _turn_abort_events.get(context_id())
    socket_at_start = getattr(cl.context.session, "socket_id", None)
    abort_event = threading.Event()
    _turn_abort_events[context_id()] = abort_event
    _turn_streams[context_id()] = stream_out
    async with _turn_lock_quiet_if_stopping(previous_abort_event):
        # Every window reloads the latest private transcript before generating.
        context = await restore_conversation(service)
        if not context.provider_selection:
            # 无任何可用提供商：不调用模型，直接引导添加。
            await cl.Message(
                content=(
                    "尚未添加模型提供商，暂时无法回答。\n\n"
                    "请点击右上角 ⚙ **设置** →「管理模型提供商」，"
                    "用 Base URL + API Key 添加你的模型（支持参数量 ≤27B 的 "
                    "OpenAI 兼容接口，如 SiliconFlow / DeepSeek / 本地 vLLM），"
                    "添加后在「默认模型」中选择并确认即可开始对话。"
                )
            ).send()
            return
        mapping = _msg_turn_map()
        if edit_turn is None:
            edit_turn = mapping.get(msg_id)  # 命中即编辑重发
        async with cl.Step(
            name="generation-safety-review", type="run", icon="shield-check"
        ) as step:
            step.output = "正在生成并审核回答…"
            with (
                streaming_transport(),
                generation_delta_sink(_make_delta_sink(context, stream_out)),
                generation_abort_scope(abort_event),
            ):
                if edit_turn is not None:
                    # 编辑重发：覆盖该轮用户内容并从此重新生成；
                    # 之前轮次的上下文保留（预算保守累计，只增不减）。
                    response = await asyncio.to_thread(
                        service.edit_and_regenerate,
                        context,
                        edit_turn,
                        question,
                        code,
                        user_msg_id=msg_id,
                        attachments=attachments,
                    )
                    for known_id, known_turn in list(mapping.items()):
                        if known_turn >= edit_turn and known_id != msg_id:
                            del mapping[known_id]
                else:
                    response = await asyncio.to_thread(
                        service.chat,
                        context,
                        question,
                        code,
                        user_msg_id=msg_id,
                        attachments=attachments,
                    )
            if msg_id:
                mapping[msg_id] = (
                    len([m for m in context.history if m.get("role") == "user"]) - 1
                )
            await persist_backend_history(context)
            step.output = "审核已完成"
            # 真流式路径收敛：审核结论与已直播内容不一致时（被拦截、触发
            # 重生成等），立即把整条消息替换为最终答案——这就是"先直播、
            # 被拦截再屏蔽"的屏蔽动作。一致则保持已播内容不动。
            reply = stream_out.get("reply")
            if reply is not None and response.answer != stream_out.get("text", ""):
                reply.content = response.answer
                await reply.update()
        try:
            await schedule_title(
                service,
                context,
                response,
                question,
                getattr(cl.context, "emitter", None),
                cl.context.session,
                timeout=service.settings.turn_timeout - (time.perf_counter() - started),
            )
        except (
            Exception
        ) as error:  # noqa: BLE001 - optional title failures must not affect chat
            logger.info(
                "Sidebar title not scheduled error_type=%s", type(error).__name__
            )
    return response

@cl.on_stop
async def on_stop() -> None:
    """用户点停止：置位当前回合的中断事件。worker 线程在下一个流式
    chunk 边界掐断上游请求并立刻让出会话锁——被取消的 on_message 任务
    因 to_thread 不可强杀、会等生成自然结束，这正是此前停止后新消息
    仍显示"排队等待"的根源。"""
    request_turn_abort()


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


def prepare_message_inputs(
    content: str, elements=(), max_chars: int = MAX_MESSAGE_CHARS
) -> tuple[str, str, tuple]:
    """Split text, code blocks, and uploaded elements into one safe turn."""
    if not isinstance(content, str):
        raise ValueError("消息内容必须是文本")
    if len(content) > max_chars:
        raise ValueError(f"消息过长，请控制在 {max_chars} 字符以内")
    question, code = split_question_and_code(content)
    prepared = prepare_attachment_elements(elements)
    attachments = prepared.files
    if not question and attachments:
        question = (
            "请分析我上传的图片附件。"
            if prepared.has_images and not prepared.has_documents
            else "请分析我上传的文档附件。"
        )
    return question, code, attachments


def render_audit_markdown(meta: dict, latency_ms: int | None = None) -> str:
    """把白名单元数据渲染为前端审计面板（纯函数，便于测试）。

    展示密度默认 full（完整审计明细开放给学生查看）；设置 SAGE_UI_DETAIL=brief
    可收紧为一行摘要（研究对照用）。任何模式都不含系统提示词、API Key 或
    用户原始输入。
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

    if os.environ.get("SAGE_UI_DETAIL", "full").strip().lower() == "brief":
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


def welcome_markdown(service) -> str:
    settings = service.settings
    mode_label = "学习保护" if settings.exam_mode else "宽松演示"
    return WELCOME_TMPL.format(
        mode_label=mode_label,
        switch_note="服务端固定",
        backend=settings.mode,
        model=settings.model,
        classifier=settings.classifier_mode,
    )


@cl.set_chat_profiles
async def chat_profiles(_current_user=None, _language=None):
    # /project/settings supplies this before the WebSocket connects. One
    # default profile renders the welcome natively without a profile selector.
    return [
        cl.ChatProfile(
            name="AlgoSage",
            default=True,
            icon="/favicon",
            markdown_description=welcome_markdown(get_service()),
        )
    ]


@cl.on_chat_start
async def on_chat_start() -> None:
    service = get_service()
    async with conversation_lock():
        context = await restore_conversation(service)
    await send_provider_settings(context)
    await send_model_catalog(context)


@cl.on_chat_end
async def on_chat_end() -> None:
    # Socket disconnect is temporary. Native thread contexts are shared across
    # windows; durable history is restored before the next turn and bounded by LRU.
    pass


async def _mask_or_reply(stream_out: dict, reply, content: str) -> None:
    """生成中途出错或触达用量上限时，优先把已直播的草稿整体替换为提示
    文案（即时屏蔽）；还没有直播过任何内容时才另发一条新消息。"""
    streamed = stream_out.get("reply")
    if streamed is not None:
        streamed.content = content
        await streamed.update()
        return
    reply.content = content
    await reply.send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    service = get_service()
    reply = cl.Message(content="")
    started = time.perf_counter()
    stream_out: dict = {}
    try:
        question, code, attachments = prepare_message_inputs(
            message.content,
            getattr(message, "elements", ()),
            max_chars=service.settings.max_input_chars,
        )
        context = await asyncio.to_thread(
            service.new_context,
            context_id(),
            principal_id=authenticated_principal_id(),
        )
        # 回合开始时的连接 id：收尾时对比（见下方 socket id 变化检测）。
        socket_at_start = getattr(cl.context.session, "socket_id", None)
        response = await run_turn(
            service,
            context,
            question,
            code,
            msg_id=message.id,
            attachments=attachments,
            stream_out=stream_out,
        )
        streamed_reply = stream_out.get("reply")
        if response is None:
            # 无提供商引导已由 run_turn 发出，无需再送达任何内容。
            return
    except ValueError as error:
        reply.content = f"⚠️ 输入未通过校验：{error}"
        await reply.send()
        return
    except UsageLimitExceeded as error:
        await _mask_or_reply(stream_out, reply, f"⚠️ {error}")
        return
    except Exception as error:  # noqa: BLE001 - 前端不向学生暴露堆栈细节
        logger.exception("处理消息失败 session=%s", cl.context.session.id)
        await _mask_or_reply(
            stream_out,
            reply,
            f"⚠️ 请求未完成（{type(error).__name__}）。请稍后重试或简化问题。",
        )
        return

    latency_ms = int((time.perf_counter() - started) * 1000)
    delivery_started = time.perf_counter()
    if streamed_reply is not None:
        # 真流式路径：内容已在生成过程中直播；审核后不一致的部分
        # 已在 run_turn 里整体替换为最终答案，这里把最终内容（含
        # 数学定界符规范化）整体落库并推平尾部留白的转义残余。
        streamed_reply.content = normalize_math_delimiters(response.answer)
        await streamed_reply.send()
    else:
        # 思考模型的推理过程：宽松演示模式下以独立可折叠 Step 展示
        # （前端默认折叠）；学习保护模式下 pipeline 不返回 reasoning，
        # 防止推理内容泄漏题解。
        reasoning = (getattr(response, "reasoning", "") or "").strip()
        if reasoning:
            async with cl.Step(
                name="model-reasoning", type="run", icon="brain"
            ) as step:
                step.output = reasoning
        await deliver_response(reply, normalize_math_delimiters(response.answer))
    logger.info(
        "Turn timing %s",
        json.dumps(
            {
                "thread_id": context_id(),
                "message_id": message.id,
                "compute_ms": latency_ms,
                "delivery_ms": round((time.perf_counter() - delivery_started) * 1000),
                "regenerations": response.regenerations,
                "stages": timing_summary(response.call_trace),
            },
            ensure_ascii=False,
        ),
    )

    # 回合进行中页面被刷新/重连过（socket id 变化）时，把含最终答案的
    # 完整线程状态推送给当前连接——否则最终答案只送到了已断开的旧连接，
    # 用户在生成中新建对话再点回来会永远看不到答案。
    _turn_streams.pop(context_id(), None)
    if getattr(cl.context.session, 'socket_id', None) != socket_at_start:
        try:
            thread = await get_chat_store().get_thread(context_id())
            if thread:
                await cl.context.emitter.resume_thread(thread)
        except Exception as error:  # noqa: BLE001 - 推送失败不影响主流程
            logger.info(
                "Live session refresh skipped error_type=%s", type(error).__name__
            )
    meta = TutorService.display_metadata(response)
    async with cl.Step(
        name="security-audit-policy", type="run", icon="shield-check"
    ) as step:
        step.output = render_audit_markdown(meta, latency_ms)
