"""Per-principal BYOK provider registrations.

Users may bring their own OpenAI-compatible endpoint (Base URL + API Key).
Entries are keyed by the trusted principal id:

- The browser never keeps the key: plaintext keys exist only in this process
  at runtime. Optional persistence mirrors registrations into a managed
  block of the local .env file with the API key sealed by Fernet
  authenticated encryption (a one-way hash cannot work here — the key must
  stay usable to call the upstream endpoint);
- persisted entries survive restarts; the .env is rewritten atomically and
  kept owner-private (0600);
- every entry is still executed through the full audited AlgoSage pipeline
  (classification → graded help policy → output guard), only the model backend
  changes;
- model discovery reuses the same HTTPS-only URL policy and only accepts
  models whose id carries a parameter-size marker within the custom limit.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path

from .config import Settings
from .providers import (
    build_custom_settings,
    custom_model_max_b,
    custom_selection_id,
    infer_model_capabilities,
    is_chat_model,
    parse_model_params_b,
    validate_public_base_url,
)

logger = logging.getLogger(__name__)

DISCOVERY_MODEL_LIMIT = 200
CUSTOM_PROVIDER_LABEL = "自定义 API"

_ENV_BLOCK_BEGIN = "# >>> algosage custom providers (auto-managed) >>>"
_ENV_BLOCK_END = "# <<< algosage custom providers <<<"
_ENV_SECRET_KEY = "SAGE_CUSTOM_PROVIDER_SECRET"
_ENV_FIELDS = ("PRINCIPAL", "LABEL", "BASE_URL", "MODEL", "KEY", "CAPABILITIES")
_FORBIDDEN_CHARS = re.compile(r"[\r\n\x00]")

# 能力探测用的 64x64 纯色 PNG（data URL）：VL 模型对过小图片会报尺寸错误。
# 块 CRC 必须合法——上游会严格校验图片文件，损坏的 PNG 被当成 400 误判为不支持。
_PROBE_IMAGE_DATA_URL = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAf0lEQVR4nNXOMREAIBDAsFL/wpgxBCJ+4BoFWftcyiRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4iRO4vwdmHqZHgMZgZAxXgAAAABJRU5ErkJggg=="
)


def probe_model_capabilities(base_url: str, api_key: str, model_id: str) -> dict:
    """真实调用上游判断模型能力：每个能力一个构造好的最小请求，观察行为。

    - 思考：流式请求 enable_thinking=true，一旦出现 reasoning_content 即
      判支持并中断（不等整个响应生成完）；content 先到且无 reasoning 则
      判不支持（上游对不支持的模型会静默忽略参数，必须看输出）；
    - 多模态：携带 64x64 data-url 图片，非视觉模型返回 400（not a VLM）；
    - 联网搜索：enable_search=true，上游拒绝（4xx）即不支持；
    - is_chat：纯文本最小请求，全部探测失败时用于区分"模型不存在"。
    四项检查相互独立，并发执行以缩短点击后的等待；单模型耗时≈最慢一项。
    后三项只看上游是否接受请求：流式请求、收到首个事件即断开。默认开启
    思考的模型（Qwen3/Qwen3.5 等）不受 max_tokens 限制推理，等完整响应
    要先生成数百个推理 token（实测单项 4–31 秒）。不改用 enable_thinking=
    false：那是 SiliconFlow 等的私有参数，其他 OpenAI 兼容端点可能直接 400，
    会被误判为不支持。
    异常/超时统一记为 None（未知），不冒充支持。
    """
    from concurrent.futures import ThreadPoolExecutor

    from openai import OpenAI

    # 思考探测要等上游产出首个增量，实测可达 25s+；15s 会误判超时。
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=30, max_retries=0)

    import time as _time

    from openai import APIStatusError

    def attempt(payload, check=None):
        # 4xx（非限流）= 上游明确拒绝 → 该能力不支持；
        # 429/5xx/超时 = 暂时失败 → 短退避重试，最终仍失败记为未知。
        last_error = None
        settled = None
        for round_index in range(3):
            try:
                if check is not None:
                    result = check()
                    if result is not None:
                        settled = result
                        break
                    # 流式检查返回 None 表示本轮无结论，重试
                    last_error = None
                else:
                    # 4xx 拒绝在建立流时即抛出；接受则取首个事件后断开，
                    # 不等完整生成（流内错误事件会在迭代时抛出，记为未知）。
                    stream = client.chat.completions.create(**payload, stream=True)
                    try:
                        next(iter(stream), None)
                    finally:
                        stream.close()
                    settled = True
                    break
            except APIStatusError as error:
                status = getattr(error, "status_code", None)
                if isinstance(status, int) and 400 <= status < 500 and status != 429:
                    return False
                last_error = error
            except Exception as error:  # noqa: BLE001 - 超时/连接失败归为未知
                last_error = error
            _time.sleep(1.2 * (round_index + 1))
        if settled is None:
            logger.info(
                "capability probe inconclusive model=%s last_error=%s",
                model_id,
                type(last_error).__name__ if last_error else "unknown",
            )
            return None
        return settled

    def probe_thinking():
        """流式早停：出现 reasoning_content 即 True；content 先到无 reasoning
        即 False；无增量产出返回 None 触发重试。"""

        def check():
            stream = client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": "1+1等于几？先想一想再回答。"}],
                max_tokens=30,
                extra_body={"enable_thinking": True},
                stream=True,
            )
            try:
                for chunk in stream:
                    choice = chunk.choices[0] if chunk.choices else None
                    delta = choice.delta if choice else None
                    if delta is None:
                        continue
                    reasoning = (getattr(delta, "reasoning_content", "") or "").strip()
                    if reasoning:
                        return True
                    content = (getattr(delta, "content", "") or "").strip()
                    if content:
                        return False
                return None
            finally:
                stream.close()

        return attempt(None, check=check)

    def probe_multimodal():
        return attempt(
            {
                "model": model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "hi"},
                            {
                                "type": "image_url",
                                "image_url": {"url": _PROBE_IMAGE_DATA_URL},
                            },
                        ],
                    }
                ],
                "max_tokens": 1,
            }
        )

    def probe_web_search():
        return attempt(
            {
                "model": model_id,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "extra_body": {"enable_search": True},
            }
        )

    def probe_is_chat():
        return attempt(
            {
                "model": model_id,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            }
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            "thinking": pool.submit(probe_thinking),
            "multimodal": pool.submit(probe_multimodal),
            "web_search": pool.submit(probe_web_search),
            "is_chat": pool.submit(probe_is_chat),
        }
        result = {name: future.result() for name, future in futures.items()}
    return result


def _env_quote(value: str) -> str:
    """Quote a dotenv value when necessary (spaces, #, quotes, leading/trailing ws)."""
    if value == "" or re.search(r"[\s#\"']", value) or value != value.strip():
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _env_unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        inner = value[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return value


def _extract_managed_block(text: str) -> str | None:
    begin = text.find(_ENV_BLOCK_BEGIN)
    end = text.find(_ENV_BLOCK_END)
    if begin == -1 or end == -1 or end < begin:
        return None
    return text[begin : end + len(_ENV_BLOCK_END)]


class CustomProviderStore:
    def __init__(self, base: Settings, fetch_models=None, env_file: str | None = None):
        self._base = base
        # principal_id -> selection -> entry dict (LRU capped per principal)
        self._entries: dict[str, OrderedDict[str, dict]] = {}
        self._lock = threading.RLock()
        # Injectable for tests: fetch_models(base_url, api_key) -> [model_id]
        self._fetch_models = fetch_models or self._default_fetch_models
        self._env_file = Path(env_file) if env_file else None
        self._secret: bytes | None = None
        if self._env_file:
            self._load_from_env()

    # ------------------------------------------------------------------
    # .env persistence (managed block)
    # ------------------------------------------------------------------
    def _fernet(self):
        from cryptography.fernet import Fernet

        if self._secret is None:
            self._secret = Fernet.generate_key()
        return Fernet(self._secret)

    def _read_env_text(self) -> str:
        try:
            return self._env_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except OSError as error:
            logger.warning("custom provider env 读取失败: %s", error)
            raise

    def _load_from_env(self) -> None:
        """Restore registrations from the managed .env block (tolerant of corruption)."""
        from cryptography.fernet import Fernet, InvalidToken

        text = self._read_env_text()
        block = _extract_managed_block(text)
        if block is None:
            return
        raw: dict[str, dict[str, str]] = {}
        for line in block.splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            m = re.fullmatch(
                rf"SAGE_CUSTOM_PROVIDER_(\d+)_({'|'.join(_ENV_FIELDS)})", key
            )
            if not m:
                continue
            idx, field = m.group(1), m.group(2)
            raw.setdefault(idx, {})[field] = _env_unquote(value.strip())
        # Secret lives on its own line inside the block.
        secret_raw = None
        for line in block.splitlines():
            if line.startswith(_ENV_SECRET_KEY + "="):
                secret_raw = _env_unquote(line.partition("=")[2].strip())
                break
        if not secret_raw:
            logger.info("custom provider 托管区块缺少加密密钥，忽略已有条目")
            return
        try:
            fernet = Fernet(secret_raw.encode("ascii"))
            self._secret = secret_raw.encode("ascii")
        except (ValueError, TypeError):
            logger.info("custom provider 加密密钥格式无效，忽略已有条目")
            return
        # 按序号数值排序：字符串排序会把 10、11 排在 2 之前，条目满 10 个后
        # 每次重启都会打乱各账号的模型顺序。
        for idx in sorted(raw, key=int):
            item = raw[idx]
            if not all(
                field in item for field in ("PRINCIPAL", "BASE_URL", "MODEL", "KEY")
            ):
                continue
            try:
                api_key = fernet.decrypt(item["KEY"].encode("ascii")).decode("utf-8")
            except (InvalidToken, ValueError):
                logger.info("custom provider 条目解密失败（idx=%s），跳过", idx)
                continue
            # 恢复注册时真实探测并持久化的能力；标签只含 True 项。
            capability_tags = item.get("CAPABILITIES", "")
            capabilities = {
                tag.strip(): True for tag in capability_tags.split(",") if tag.strip()
            }
            try:
                self.register(
                    item["PRINCIPAL"],
                    base_url=item["BASE_URL"],
                    api_key=api_key,
                    model=item["MODEL"],
                    label=item.get("LABEL", ""),
                    capabilities=capabilities or None,
                    _persist=False,
                )
            except ValueError:
                logger.info("custom provider 条目校验失败（idx=%s），跳过", idx)

    def _persist_to_env(self) -> None:
        """Rewrite the managed block from memory; atomic, owner-private."""
        from cryptography.fernet import InvalidToken

        fernet = self._fernet()
        lines = [_ENV_BLOCK_BEGIN, f"{_ENV_SECRET_KEY}={self._secret.decode('ascii')}"]
        index = 0
        for principal, bucket in self._entries.items():
            for entry in bucket.values():
                index += 1
                prefix = f"SAGE_CUSTOM_PROVIDER_{index}_"
                try:
                    sealed = fernet.encrypt(entry["api_key"].encode("utf-8")).decode(
                        "ascii"
                    )
                except (InvalidToken, ValueError):
                    logger.warning("custom provider 密钥加密失败，跳过条目")
                    continue
                lines.append(f"{prefix}PRINCIPAL={_env_quote(principal)}")
                lines.append(f"{prefix}LABEL={_env_quote(entry['label'])}")
                lines.append(f"{prefix}BASE_URL={_env_quote(entry['base_url'])}")
                lines.append(f"{prefix}MODEL={_env_quote(entry['model'])}")
                caps = entry.get("capabilities") or {}
                tags = ",".join(name for name, on in sorted(caps.items()) if on)
                lines.append(f"{prefix}CAPABILITIES={_env_quote(tags)}")
                lines.append(f"{prefix}KEY={sealed}")
        lines.append(_ENV_BLOCK_END)

        text = self._read_env_text()
        pattern = re.compile(
            re.escape(_ENV_BLOCK_BEGIN) + r".*?" + re.escape(_ENV_BLOCK_END) + r"\n?",
            re.DOTALL,
        )
        block_text = "\n".join(lines) + "\n"
        if pattern.search(text):
            text = pattern.sub(lambda _: block_text, text, count=1)
        else:
            text = (
                text + ("\n" if text and not text.endswith("\n") else "") + block_text
            )
        # 同目录临时文件 + 原子替换，保留原权限。
        tmp = self._env_file.with_name(self._env_file.name + ".algosage-tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
            if self._env_file.exists():
                os.chmod(self._env_file, 0o600)
            os.replace(tmp, self._env_file)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Registration / listing
    # ------------------------------------------------------------------
    def register(
        self,
        principal_id: str,
        *,
        base_url: str,
        api_key: str,
        model: str,
        label: str = "",
        capabilities: dict | None = None,
        _persist: bool = True,
    ) -> dict:
        if not isinstance(principal_id, str) or not principal_id.strip():
            raise ValueError("缺少可信身份，不能注册自定义模型")
        name = (label or "").strip() or CUSTOM_PROVIDER_LABEL
        if len(name) > 40:
            name = name[:40]
        for field, value in (
            ("显示名称", name),
            ("Base URL", base_url),
            ("API Key", api_key),
            ("模型 ID", model),
        ):
            if isinstance(value, str) and _FORBIDDEN_CHARS.search(value):
                raise ValueError(f"{field} 不能包含换行或控制字符")
        # 显式传入（真实探测结果）优先；未探测时回退命名推断仅作展示。
        entry_capabilities = (
            capabilities
            if isinstance(capabilities, dict) and capabilities
            else infer_model_capabilities(model)
        )
        settings = build_custom_settings(
            self._base, base_url, api_key, model, capabilities=entry_capabilities
        )
        selection = custom_selection_id(name, settings.base_url, model)
        entry = {
            "id": "custom",
            "label": name,
            "model": model,
            "selection": selection,
            "mode": "api",
            "available": True,
            "params_b": parse_model_params_b(model),
            "capabilities": entry_capabilities,
            "base_url": settings.base_url,
            # Runtime-only plaintext; on disk this is Fernet-sealed.
            "api_key": api_key,
            "settings": settings,
        }
        with self._lock:
            bucket = self._entries.setdefault(principal_id, OrderedDict())
            bucket.pop(selection, None)
            bucket[selection] = entry
            if self._env_file and _persist:
                self._persist_to_env()
        return self._public_entry(entry)

    def remove(self, principal_id: str, selection: str) -> bool:
        with self._lock:
            bucket = self._entries.get(principal_id)
            if not bucket or selection not in bucket:
                return False
            del bucket[selection]
            if self._env_file:
                self._persist_to_env()
            return True

    def remove_all(self, principal_id: str) -> list[str]:
        """Drop every provider (and its encrypted key) owned by one principal."""
        with self._lock:
            bucket = self._entries.pop(principal_id, None)
            if not bucket:
                return []
            if self._env_file:
                self._persist_to_env()
            return list(bucket)

    def list_for(self, principal_id: str) -> list[dict]:
        with self._lock:
            bucket = self._entries.get(principal_id)
            entries = list(bucket.values()) if bucket else []
        return [self._public_entry(entry) for entry in entries]

    def resolve(self, principal_id: str, selection: str) -> Settings:
        # 只读查询，不调整顺序：条目按添加先后排列（列表展示与"回退到第一个
        # 可用模型"都依赖它）。早期按使用先后做 LRU 淘汰时会把命中项移到
        # 末尾，淘汰取消后若保留，每次使用都会打乱下拉列表的顺序。
        with self._lock:
            bucket = self._entries.get(principal_id)
            entry = bucket.get(selection) if bucket else None
            if entry is None:
                raise ValueError("未知自定义模型，或所属会话已失效，请重新添加")
            settings: Settings = entry["settings"]
            return settings

    def info(self, principal_id: str, selection: str) -> dict:
        with self._lock:
            bucket = self._entries.get(principal_id)
            entry = bucket.get(selection) if bucket else None
            if entry is None:
                raise ValueError("未知自定义模型")
            return {"label": entry["label"], "model": entry["model"], "id": "custom"}

    @staticmethod
    def _public_entry(entry: dict) -> dict:
        """Whitelisted view for the browser; never includes the API key."""
        return {
            "id": "custom",
            "label": entry["label"],
            "model": entry["model"],
            "selection": entry["selection"],
            "mode": "api",
            "available": True,
            "params_b": entry["params_b"],
            "capabilities": entry.get("capabilities")
            or infer_model_capabilities(entry["model"]),
        }

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------
    def discover(self, base_url: str, api_key: str) -> list[dict]:
        url = validate_public_base_url(base_url)
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("API Key 不能为空")
        limit_b = custom_model_max_b()
        unlimited = limit_b == float("inf")
        models = []
        # 列表默认不显示能力标签；is_chat 用命名规则仅做非对话排除，
        # 真实能力由前端点选该模型后调用 /models/probe 探测。
        for model_id in self._fetch_models(url, api_key)[:DISCOVERY_MODEL_LIMIT]:
            params_b = parse_model_params_b(model_id)
            is_chat = is_chat_model(model_id)
            models.append(
                {
                    "id": model_id,
                    "params_b": params_b,
                    "allowed": (
                        is_chat
                        and (
                            unlimited
                            or (params_b is not None and params_b <= limit_b)
                        )
                    ),
                    "capabilities": {"is_chat": is_chat},
                }
            )
        models.sort(
            key=lambda item: (
                not item["allowed"],
                item["params_b"] is None,
                str(item["id"]).lower(),
            )
        )
        return models

    @staticmethod
    def _default_fetch_models(base_url: str, api_key: str) -> list[str]:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url, timeout=15, max_retries=0)
        # OpenAI-compatible /models listing; some vendors paginate but a single
        # page is enough for interactive picking.
        response = client.models.list()
        return [item.id for item in response.data]
