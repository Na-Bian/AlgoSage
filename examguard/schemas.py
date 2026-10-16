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
    # P2 起新增：题目标识（教师策略按题覆盖、P3 会话风险跟踪用），默认空。
    question_id: str = ""


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
    # P2 起新增：审核层标识（如 rules-v1 / rules+judge-v1 / judge-v1），
    # 用于审计展示与消融实验记录；空字符串表示旧基线未标注。
    method: str = ""
    # 拦截/泄漏类别（如 full_code / key_steps / proof），仅语义层填充。
    categories: tuple[str, ...] = ()


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
    # No raw question/code stored in the response.
