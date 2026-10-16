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
