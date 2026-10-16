"""Stable message contracts for future classifier, guards, and benchmark harness."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Intent(str, Enum):
    KNOWLEDGE = "knowledge"
    DEBUG = "debug"
    HINT = "hint"
    SOLUTION = "request_solution"
    JAILBREAK = "jailbreak"


@dataclass(frozen=True)
class TutorRequest:
    question: str
    existing_code: str = ""
    exam_mode: bool = True


@dataclass(frozen=True)
class Classification:
    intent: Intent
    method: str
    reason: str
    # P1 起新增（带默认值，保持旧调用兼容）：
    # confidence 仅在可可靠估计时给出（正则基线为 None，混合分类取 LLM 置信度）；
    # risk_flags 为 Prompt Injection / 变相索取等风险特征标记，现阶段仅作
    # 预留信号与审计展示，不直接改变策略等级。
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


@dataclass(frozen=True)
class TutorResponse:
    answer: str
    classification: Classification
    decision: PolicyDecision
    audit: AuditResult
    model: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    # No raw question/code stored in the response.
