"""Settings: keys only from environment / untracked .env file."""
from dataclasses import dataclass
import os


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


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
    # P1：意图分类器模式。regex=冻结的正则基线（默认，离线可用）；
    # hybrid=规则+LLM 混合（需要 APP_MODE=api，LLM 不可用时自动回退正则）。
    classifier_mode: str = "regex"
    # P2：输出审核层。basic=第一阶段冻结基线（供消融对照）；rules=规则预筛
    # （默认，离线可用）；layered=规则+LLM Judge（API 模式下启用，Judge 不可
    # 用时降级为规则层结论并标注）。
    output_guard: str = "rules"
    # P2：教师侧策略文件（可选）。缺省使用内嵌默认映射；文件非法时启动失败。
    policy_file: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        mode = os.environ.get("APP_MODE", "mock").lower().strip()
        if mode not in {"mock", "api"}:
            raise ValueError("APP_MODE must be mock or api")
        token_parameter = os.environ.get("LLM_TOKEN_PARAMETER", "max_tokens")
        if token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("Unsupported LLM_TOKEN_PARAMETER")
        classifier_mode = os.environ.get("EG_CLASSIFIER_MODE", "regex").lower().strip()
        if classifier_mode not in {"regex", "hybrid"}:
            raise ValueError("EG_CLASSIFIER_MODE must be regex or hybrid")
        output_guard = os.environ.get("EG_OUTPUT_GUARD", "rules").lower().strip()
        if output_guard not in {"basic", "rules", "layered"}:
            raise ValueError("EG_OUTPUT_GUARD must be basic, rules or layered")
        return cls(
            mode=mode,
            api_key=os.environ.get("LLM_API_KEY", ""),
            base_url=os.environ.get("LLM_BASE_URL", "https://api.siliconflow.cn/v1").strip(),
            model=os.environ.get("LLM_MODEL", "Qwen/Qwen3-8B").strip(),
            exam_mode=os.environ.get("EXAM_MODE", "true").lower() in {"true", "1", "yes"},
            timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "30")),
            max_output_tokens=int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "512")),
            token_parameter=token_parameter,
            classifier_mode=classifier_mode,
            output_guard=output_guard,
            policy_file=os.environ.get("EG_POLICY_FILE", "").strip(),
        )
