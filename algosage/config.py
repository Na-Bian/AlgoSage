"""Settings: keys only from environment / untracked .env file."""

from dataclasses import dataclass, field
import json
import math
import os

from .runtime_paths import runtime_path


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(runtime_path("CHAINLIT_ENV_FILE", ".env"))
    # AlgoSage uses SAGE_* names. Existing AlgoSage deployments continue to
    # work by promoting EG_* values only when a SAGE_* override is absent.
    for key, value in list(os.environ.items()):
        if key.startswith("EG_"):
            new_key = f"SAGE_{key[3:]}"
            if not os.environ.get(new_key):
                os.environ[new_key] = value


@dataclass(frozen=True)
class Settings:
    mode: str = "mock"
    api_key: str = ""
    base_url: str = "https://api.siliconflow.cn/v1"
    model: str = "Qwen/Qwen3-8B"
    exam_mode: bool = True
    timeout: float = 30.0
    max_output_tokens: int = 512
    token_parameter: str = "max_tokens"
    # P1：意图分类器模式。regex=带主题上下文过滤的规则（默认，离线可用）；
    # hybrid=规则+LLM 混合（需要 APP_MODE=api，LLM 不可用时自动回退正则）。
    classifier_mode: str = "regex"
    # P2：输出审核层。basic=第一阶段冻结基线（供消融对照）；rules=规则预筛
    # （默认，离线可用）；layered=规则+LLM Judge（API 模式下启用，Judge 不可
    # 用时返回 unknown 并安全回退）。
    output_guard: str = "rules"
    # P2：教师侧策略文件（可选）。缺省使用内嵌默认映射；文件非法时启动失败。
    policy_file: str = ""
    # 服务商专属附加参数（如 SiliconFlow Qwen3.5 的 {"enable_thinking": false}），
    # 经 OpenAI SDK extra_body 透传；消融实验各版本共用同一份（解码配置一致）。
    extra_body: dict = field(default_factory=dict)
    question_id: str = ""
    exam_id: str = "demo"
    # Empty in explicitly constructed offline/test settings; runtime defaults to SQLite.
    state_path: str = ""
    # 9 covers classification + generation + one Judge repair + quality review,
    # plus one audited regeneration cycle with Judge and quality review.
    max_calls_per_turn: int = 9
    max_input_chars: int = 20_000
    daily_requests_per_student: int = 100
    global_daily_requests: int = 1_000
    turn_timeout: float = 180.0
    classifier_max_output_tokens: int = 256
    judge_max_output_tokens: int = 768
    quality_max_output_tokens: int = 512
    concise_max_output_tokens: int = 768
    parallel_reviews: bool = True
    min_classifier_confidence: float = 0.7
    # BYOK 注册时真实探测并持久化的模型能力（thinking/web_search/…）。
    # None 表示无探测数据（预置模型），门控回退命名推断；缺失/None 视为不支持。
    capabilities: dict | None = None
    # 内置标题小模型：与学生会话模型无关（BYOK 模型也可能不支持快速出短
    # 文本），服务启动时按此配置独立构建；留空 model 时回退主模型配置。
    title_model: str = ""
    title_base_url: str = ""
    title_api_key: str = ""

    def __post_init__(self) -> None:
        for name in (
            "max_calls_per_turn",
            "classifier_max_output_tokens",
            "judge_max_output_tokens",
            "quality_max_output_tokens",
            "concise_max_output_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in (
            "max_input_chars",
            "daily_requests_per_student",
            "global_daily_requests",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not math.isfinite(self.turn_timeout) or self.turn_timeout <= 0:
            raise ValueError("turn_timeout must be finite and positive")
        if (
            not math.isfinite(self.min_classifier_confidence)
            or not 0 <= self.min_classifier_confidence <= 1
        ):
            raise ValueError("min_classifier_confidence must be in [0, 1]")
        if not isinstance(self.exam_id, str) or not self.exam_id.strip():
            raise ValueError("exam_id must not be empty")

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        # 默认即真实 API（业务逻辑不再走 mock）；mock 仅作为单元测试夹具，
        # 由测试代码显式构造，不作为运行默认值。
        mode = os.environ.get("APP_MODE", "api").lower().strip()
        if mode not in {"mock", "api"}:
            raise ValueError("APP_MODE must be mock or api")
        token_parameter = os.environ.get("LLM_TOKEN_PARAMETER", "max_tokens")
        if token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("Unsupported LLM_TOKEN_PARAMETER")
        classifier_mode = (
            os.environ.get("SAGE_CLASSIFIER_MODE", "regex").lower().strip()
        )
        if classifier_mode not in {"regex", "hybrid"}:
            raise ValueError("SAGE_CLASSIFIER_MODE must be regex or hybrid")
        output_guard = os.environ.get("SAGE_OUTPUT_GUARD", "rules").lower().strip()
        if output_guard not in {"basic", "rules", "layered"}:
            raise ValueError("SAGE_OUTPUT_GUARD must be basic, rules or layered")
        extra_raw = os.environ.get("SAGE_LLM_EXTRA_BODY", "").strip()
        if extra_raw:
            try:
                extra_body = json.loads(extra_raw)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"SAGE_LLM_EXTRA_BODY 必须是合法 JSON: {error}"
                ) from error
            if not isinstance(extra_body, dict):
                raise ValueError("SAGE_LLM_EXTRA_BODY 必须是 JSON 对象")
        else:
            extra_body = {}
        return cls(
            mode=mode,
            api_key=os.environ.get("LLM_API_KEY", ""),
            base_url=os.environ.get(
                "LLM_BASE_URL", "https://api.siliconflow.cn/v1"
            ).strip(),
            model=os.environ.get("LLM_MODEL", "Qwen/Qwen3-8B").strip(),
            exam_mode=os.environ.get("EXAM_MODE", "true").lower()
            in {"true", "1", "yes"},
            timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "30")),
            max_output_tokens=int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "512")),
            token_parameter=token_parameter,
            classifier_mode=classifier_mode,
            output_guard=output_guard,
            policy_file=runtime_path("SAGE_POLICY_FILE", ""),
            extra_body=extra_body,
            question_id=os.environ.get("SAGE_QUESTION_ID", "").strip(),
            exam_id=os.environ.get("SAGE_EXAM_ID", "demo").strip(),
            state_path=runtime_path("SAGE_STATE_FILE", ".algosage/state.sqlite3"),
            max_calls_per_turn=int(os.environ.get("SAGE_MAX_CALLS_PER_TURN", "9")),
            max_input_chars=int(os.environ.get("SAGE_MAX_INPUT_CHARS", "20000")),
            daily_requests_per_student=int(
                os.environ.get("SAGE_DAILY_REQUESTS_PER_STUDENT", "100")
            ),
            global_daily_requests=int(
                os.environ.get("SAGE_GLOBAL_DAILY_REQUESTS", "1000")
            ),
            turn_timeout=float(os.environ.get("SAGE_TURN_TIMEOUT_SECONDS", "180")),
            classifier_max_output_tokens=int(
                os.environ.get("SAGE_CLASSIFIER_MAX_OUTPUT_TOKENS", "256")
            ),
            judge_max_output_tokens=int(
                os.environ.get("SAGE_JUDGE_MAX_OUTPUT_TOKENS", "768")
            ),
            quality_max_output_tokens=int(
                os.environ.get("SAGE_QUALITY_MAX_OUTPUT_TOKENS", "512")
            ),
            concise_max_output_tokens=int(
                os.environ.get("SAGE_CONCISE_MAX_OUTPUT_TOKENS", "768")
            ),
            parallel_reviews=os.environ.get("SAGE_PARALLEL_REVIEWS", "true").lower()
            in {"true", "1", "yes"},
            min_classifier_confidence=float(
                os.environ.get("SAGE_MIN_CLASSIFIER_CONFIDENCE", ".7")
            ),
            title_model=os.environ.get("TITLE_MODEL", "").strip(),
            title_base_url=os.environ.get("TITLE_BASE_URL", "").strip(),
            title_api_key=os.environ.get("TITLE_API_KEY", "").strip(),
        )
