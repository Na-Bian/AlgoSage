"""Stable message contracts for future classifier, guards, and benchmark harness."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Intent(str, Enum):
    KNOWLEDGE = "knowledge"
    DEBUG = "debug"
    HINT = "hint"
    SOLUTION = "request_solution"
    JAILBREAK = "jailbreak"


@dataclass(frozen=True)
class AttachmentPayload:
    """Server-prepared attachment content; never contains a local file path."""

    name: str
    mime: str
    kind: str
    text: str = ""
    image_data_url: str = ""
    size_bytes: int = 0

    def history_entry(self) -> dict:
        return {
            "name": self.name,
            "mime": self.mime,
            "kind": self.kind,
            "text": self.text,
            "image_data_url": self.image_data_url,
        }


@dataclass(frozen=True)
class TutorRequest:
    question: str
    existing_code: str = ""
    exam_mode: bool = True
    # P2 起新增：题目标识（教师策略按题覆盖、P3 会话风险跟踪用），默认空。
    question_id: str = ""
    # 由前端经服务端解析后的附件；文档文本与图片数据均视为不可信输入。
    attachments: tuple[AttachmentPayload, ...] = ()

    @property
    def attachment_text(self) -> str:
        return "\n\n".join(
            f"[附件 {index}：{item.name}]\n{item.text}"
            for index, item in enumerate(self.attachments, start=1)
            if item.text
        )


@dataclass(frozen=True)
class Classification:
    intent: Intent
    method: str
    reason: str
    # P1 起新增（带默认值，保持旧调用兼容）：
    # Model-reported confidence is uncalibrated. Low/invalid values become uncertain;
    # trusted policy consumes injection and uncertainty flags before generation.
    confidence: Optional[float] = None
    risk_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyDecision:
    intent: Intent
    max_help_level: int  # 0=deny, 1=concept, 2=hint, 3=local code diagnosis
    reason: str


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


@dataclass(frozen=True)
class AuditResult:
    approved: bool
    output: str
    reason: str
    # P2 起新增：审核层标识（如 rules-v1 / rules+judge-v1 / judge-v1），
    # 用于审计展示与消融实验记录；空字符串表示旧基线未标注。
    method: str = ""
    # 拦截/泄漏类别（如 full_code / key_steps / proof），仅语义层填充。
    categories: tuple[str, ...] = ()
    status: str = ""
    # Grounded developer evidence; never include quotes in student metadata.
    disclosures: tuple[str, ...] = ()
    evidence: tuple[dict, ...] = ()
    # High-confidence factual, logic or scope issues in an otherwise safe
    # draft. They trigger one controlled regeneration and stay developer-only.
    quality_issues: tuple[dict, ...] = ()

    def __post_init__(self):
        status = self.status or ("pass" if self.approved else "block")
        if status not in {"pass", "block", "unknown"}:
            raise ValueError("Invalid audit status")
        if self.approved != (status == "pass"):
            raise ValueError("Only a pass may approve a draft")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True)
class TutorResponse:
    answer: str
    classification: Classification
    decision: PolicyDecision
    audit: AuditResult
    model: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    # P2 起新增：为通过审核而额外重生成的次数（0 表示一次生成即通过/未启用）。
    regenerations: int = 0
    # P3 起新增：本会话该题的累计风险与已披露元素（无 SessionGuard 时为 None/空）。
    session_risk: Optional[float] = None
    session_disclosures: tuple[str, ...] = ()
    # Developer-side evidence only; never include these fields in student metadata.
    call_trace: tuple[dict, ...] = ()
    audit_attempts: tuple[dict, ...] = ()
