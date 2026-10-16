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

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        mode = os.environ.get("APP_MODE", "mock").lower().strip()
        if mode not in {"mock", "api"}:
            raise ValueError("APP_MODE must be mock or api")
        token_parameter = os.environ.get("LLM_TOKEN_PARAMETER", "max_tokens")
        if token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("Unsupported LLM_TOKEN_PARAMETER")
        return cls(
            mode=mode,
            api_key=os.environ.get("LLM_API_KEY", ""),
            base_url=os.environ.get("LLM_BASE_URL", "https://api.siliconflow.cn/v1").strip(),
            model=os.environ.get("LLM_MODEL", "Qwen/Qwen3-8B").strip(),
            exam_mode=os.environ.get("EXAM_MODE", "true").lower() in {"true", "1", "yes"},
            timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "30")),
            max_output_tokens=int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "512")),
            token_parameter=token_parameter,
        )
