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
import time
from pathlib import Path
from weakref import WeakValueDictionary

import chainlit as cl
from chainlit.input_widget import Select, Slider, Switch

from algosage.config import Settings
from algosage.service import TutorService, HISTORY_TURNS_KEPT
from algosage.providers import ProviderRegistry
from algosage.invocation import streaming_transport, timing_summary
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
            try:
                await asyncio.to_thread(service.select_provider, context, selection)
            except ValueError:
                await asyncio.to_thread(
                    service.select_provider, context, service.providers.default
                )
        if principal:
            preferences = await get_chat_store().load_user_app_preferences(principal)
            await asyncio.to_thread(service.set_model_params, context, preferences)
    return context


_model_selection_locks: dict[str, asyncio.Lock] = {}


def model_selection_lock(thread_id: str) -> asyncio.Lock:
    key = str(thread_id)
    lock = _model_selection_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _model_selection_locks[key] = lock
    return lock


async def send_model_catalog(context) -> None:
    """Push the authorized catalog and thread-local selection to the composer."""
    service = get_service()
    groups: dict[str, dict] = {}
    for option in service.providers.options():
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
    if context.principal_id:
        for entry in service.list_custom_providers(context.principal_id):
            group = groups.setdefault(
                "custom",
                {
                    "id": "custom",
                    "label": "自定义 API（我的密钥）",
                    "available": True,
                    "models": [],
                },
            )
            group["models"].append(
                {
                    "selection": entry["selection"],
                    "model": entry["model"],
                    "label": entry["label"],
                    "available": True,
                }
            )
    info = service.provider_info(context)
    try:
        await cl.send_window_message(
            {
                "type": "algosage-model-state",
                "thread_id": context_id(),
                "current_selection": context.provider_selection,
                "current_label": f"{info['label']} · {info['model']}",
                "default_selection": service.providers.default,
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
        _service = TutorService(settings, providers=ProviderRegistry(settings))
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

    @models_router.get("/models/catalog", include_in_schema=False)
    async def read_model_catalog(thread_id: str = "", *, current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        service = get_service()
        groups = _catalog_groups(service, service.providers.options())
        custom = service.list_custom_providers(current_user.identifier)
        if custom:
            groups.append(
                {
                    "id": "custom",
                    "label": "自定义 API（我的密钥）",
                    "available": True,
                    "models": [
                        {
                            "selection": entry["selection"],
                            "model": entry["model"],
                            "label": entry["label"],
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
        current = current or service.providers.default
        try:
            current_info = service.providers.info(current)
            current_available = True
        except ValueError:
            current_info = {"label": current, "model": current}
            current_available = False
        return {
            "default_selection": service.providers.default,
            "current_selection": current,
            "current_available": current_available,
            "current_label": f"{current_info['label']} · {current_info['model']}",
            "providers": groups,
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
        return {"models": models}

    @models_router.post("/models/custom", include_in_schema=False)
    async def register_custom_model(payload: dict, current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid payload")
        service = get_service()
        try:
            entry = await asyncio.to_thread(
                service.register_custom_provider,
                current_user.identifier,
                base_url=str(payload.get("base_url") or "").strip(),
                api_key=str(payload.get("api_key") or "").strip(),
                model=str(payload.get("model") or "").strip(),
                label=str(payload.get("label") or "").strip(),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
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
        return {"success": True}

    @models_router.post("/models/select", include_in_schema=False)
    async def select_thread_model(payload: dict, current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        thread_id = str(payload.get("thread_id") or "").strip()
        selection = str(payload.get("selection") or "").strip()
        if not thread_id or len(thread_id) > 128:
            raise HTTPException(status_code=400, detail="Invalid thread")
        if not selection:
            raise HTTPException(status_code=400, detail="Model selection is required")
        service = get_service()
        try:
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
            "label": f"{info['label']} · {info['model']}",
            "model": info["model"],
        }

    _move_routes_before_catchall(
        _chainlit_router(_chainlit_server_app),
        (
            "/models/catalog",
            "/models/select",
            "/models/discover",
            "/models/custom",
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

    from algosage.student_auth import _chainlit_router, _move_routes_before_catchall
    from algosage.student_auth import install_profile_routes

    from algosage.student_auth import install_student_auth

    install_student_auth(_chainlit_server_app, get_student_auth)
    _move_routes_before_catchall(
        _chainlit_router(_chainlit_server_app), ("/auth/preferences",)
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
    items = {f"{o['label']} · {o['model']}": o["selection"] for o in options}
    if context.principal_id:
        for entry in service.list_custom_providers(context.principal_id):
            items[f"{entry['label']} · {entry['model']}"] = entry["selection"]
    provider = service._resolve_selection_settings(context, context.provider_selection)
    params = service.normalize_model_params(context.model_params or {}, provider)
    max_output = min(
        int(params.get("max_output_tokens", provider.max_output_tokens)),
        provider.max_output_tokens,
    )
    widgets = [
        Select(
            id="model_catalog",
            label="模型与 Provider",
            items=items,
            initial_value=context.provider_selection,
            description="仅限27B以内模型",
        ),
        Slider(
            id="temperature",
            label="Temperature（创造性）",
            minimum=0,
            maximum=2,
            step=0.05,
            initial=float(params.get("temperature", 0.7)),
            description="控制回答的发散程度：越低越稳定、越高越有创造性。",
        ),
        Slider(
            id="frequency_penalty",
            label="Frequency Penalty（减少重复）",
            minimum=0,
            maximum=2,
            step=0.05,
            initial=float(params.get("frequency_penalty", 0)),
            description="越高越不容易反复使用相同的词句。",
        ),
        Slider(
            id="presence_penalty",
            label="Presence Penalty（引入新话题）",
            minimum=0,
            maximum=2,
            step=0.05,
            initial=float(params.get("presence_penalty", 0)),
            description="越高越倾向于展开新内容、不纠缠旧话题。",
        ),
        Slider(
            id="max_output_tokens",
            label="最大输出长度（Token）",
            minimum=128,
            maximum=provider.max_output_tokens,
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
    normalized = dict(settings)
    if "enter_behavior" in normalized:
        if normalized["enter_behavior"] not in {"enter", "ctrl_enter"}:
            normalized["enter_behavior"] = "enter"
    elif current and "enter_behavior" in current:
        # 移动端面板不展示发送快捷键；保存其余设置时保留既有偏好。
        normalized["enter_behavior"] = current["enter_behavior"]
    if normalized.get("font_size") not in {"small", "standard", "large"}:
        normalized["font_size"] = "standard"
    for key in ("autoscroll", "keep_drafts", "animations", "audit_summary"):
        normalized[key] = bool(normalized.get(key, True))
    for key in (
        "temperature",
        "frequency_penalty",
        "presence_penalty",
        "max_output_tokens",
    ):
        try:
            normalized[key] = float(normalized.get(key, 0))
        except (TypeError, ValueError):
            normalized.pop(key, None)
    normalized.pop("top_p", None)  # 面板已移除该控件；保留原值而非强改为 0
    return normalized


@cl.on_settings_update
async def on_settings_update(settings: dict) -> None:
    service = get_service()
    async with conversation_lock():
        context = await restore_conversation(service)
        requested_model = str(settings.get("model_catalog") or "").strip()
        if requested_model and requested_model != context.provider_selection:
            try:
                await asyncio.to_thread(
                    service.select_provider, context, requested_model
                )
            except ValueError:
                pass  # 未知/失效选择保留原模型，后续仍按原参数保存
        await asyncio.to_thread(
            service.set_model_params,
            context,
            _normalize_ui_settings(settings, current=context.model_params),
        )
        principal = authenticated_principal_id()
        if principal:
            await get_chat_store().save_user_app_preferences(
                principal, context.model_params
            )
        await persist_backend_history(context)
        await send_model_catalog(context)
        # 重新下发设置：面板里的模型列表需要包含新添加的自定义模型。
        await send_provider_settings(context)
    info = service.provider_info(context)
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
    await cl.Message(
        content=(
            f"设置已保存。当前模型：**{info['label']} · {info['model']}**；"
            f"最大输出长度：**{context.model_params.get('max_output_tokens')} Token**。"
        )
    ).send()


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


async def persist_backend_history(context) -> None:
    principal = authenticated_principal_id()
    if principal:
        history, selection = await asyncio.to_thread(_snapshot_context, context)
        await get_chat_store().save_backend_history(
            cl.context.session.thread_id,
            principal,
            history,
            selection,
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
    if not authenticated_principal_id() or thread["id"] != context_id():
        raise PermissionError("恢复对话需要对应工作区身份")
    async with conversation_lock():
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


async def run_turn(
    service,
    context,
    question,
    code,
    msg_id="",
    edit_turn=None,
    attachments=(),
):
    started = time.perf_counter()
    async with conversation_lock():
        # Every window reloads the latest private transcript before generating.
        context = await restore_conversation(service)
        mapping = _msg_turn_map()
        if edit_turn is None:
            edit_turn = mapping.get(msg_id)  # 命中即编辑重发
        async with cl.Step(
            name="generation-safety-review", type="run", icon="shield-check"
        ) as step:
            step.output = "正在生成并审核回答…"
            with streaming_transport():
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

    展示密度由 SAGE_UI_DETAIL 控制（默认 brief 学生视图；full 供研究/调试用）：
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

    if os.environ.get("SAGE_UI_DETAIL", "brief").strip().lower() != "full":
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


@cl.on_message
async def on_message(message: cl.Message) -> None:
    service = get_service()
    reply = cl.Message(content="")
    started = time.perf_counter()
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
        response = await run_turn(
            service,
            context,
            question,
            code,
            msg_id=message.id,
            attachments=attachments,
        )
    except ValueError as error:
        reply.content = f"⚠️ 输入未通过校验：{error}"
        await reply.send()
        return
    except UsageLimitExceeded as error:
        reply.content = f"⚠️ {error}"
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
    delivery_started = time.perf_counter()
    await deliver_response(reply, response.answer)
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

    meta = TutorService.display_metadata(response)
    async with cl.Step(
        name="security-audit-policy", type="run", icon="shield-check"
    ) as step:
        step.output = render_audit_markdown(meta, latency_ms)
