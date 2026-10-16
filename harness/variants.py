"""P4 消融版本装配：A/B/C/D 共用同一模型与解码配置，只切换防护组件。

- A（prompt-only）   ：常量分类器（恒 knowledge）+ 全意图 L3 策略 + 直通审核。
                       唯一防线是 System Prompt 中的考试约束文本。
- B（A+简单规则）    ：正则分类 + 默认策略映射 + 第一阶段 basic 审核
                       （即 Phase-1 Starter 行为，作为冻结基线）。
- C（B+混合分类）    ：HybridIntentClassifier（规则+LLM；离线/mock 下自动
                       回退正则，此时行为与 B 一致——报告中如实标注）。
- D（C+语义审核+会话）：rules-v1 规则预筛（API 模式再叠 LLM Judge）+
                       多轮 SessionGuard + 有限次重生成。

所有版本共用调用方传入的同一个 backend 实例（相同模型与解码参数），
保证消融差异只来自防护组件。
"""
from __future__ import annotations

from dataclasses import dataclass

from examguard.config import Settings
from examguard.exam_policy import ExamPolicy
from examguard.hybrid_classifier import HybridIntentClassifier
from examguard.leakage_guard import RuleLeakageScreener
from examguard.llm import ChatBackend
from examguard.output_guard import BasicOutputGuard
from examguard.pipeline import ExamGuardTutor
from examguard.session_guard import SessionGuard
from examguard.schemas import AuditResult, Classification, Intent, PolicyDecision, TutorRequest


class NullOutputGuard:
    """直通审核（A 版专用）：不做任何检查，用于暴露无审核时的风险。"""

    def review(self, draft, decision, exam_mode, question="", existing_code=""):
        if not draft.strip():
            return AuditResult(False, "（无输出）", "null_empty")
        return AuditResult(True, draft, "null_passthrough", method="null")


def _constant_classifier(request: TutorRequest, history=None) -> Classification:
    return Classification(Intent.KNOWLEDGE, "constant(A)", "A 版：不做输入分类")


@dataclass(frozen=True)
class Variant:
    key: str          # "A" | "B" | "C" | "D"
    label: str
    use_session_guard: bool
    notes: str


VARIANTS: dict[str, Variant] = {
    "A": Variant("A", "仅考试 System Prompt", False, "常量分类 + 全 L3 + 直通审核"),
    "B": Variant("B", "A + 简单规则", False, "正则分类 + 默认策略 + basic 审核（Phase-1 冻结基线）"),
    "C": Variant("C", "B + 混合意图分类与等级控制", False, "hybrid 分类（mock 下回退正则，行为≈B）"),
    "D": Variant("D", "C + 语义输出审核 + 多轮 SessionGuard", True, "rules-v1(+judge) 审核 + 会话风险 + 重生成"),
}


def build_tutor(variant_key: str, backend: ChatBackend, settings: Settings) -> ExamGuardTutor:
    """按消融版本构建 ExamGuardTutor；所有版本共用同一 backend。"""
    key = variant_key.upper()
    if key not in VARIANTS:
        raise ValueError(f"未知消融版本: {variant_key}")
    if key == "A":
        return ExamGuardTutor(
            backend, settings,
            classifier=_constant_classifier,
            output_guard=NullOutputGuard(),
            policy_config=ExamPolicy.from_dict({
                "exam_mode_levels": {
                    "knowledge": 3, "debug": 3, "hint": 3,
                    "request_solution": 3, "jailbreak": 3,
                },
                "max_regenerations": 0,
            }),
        )
    if key == "B":
        return ExamGuardTutor(
            backend, settings,
            output_guard=BasicOutputGuard(),
            policy_config=ExamPolicy.from_dict({"max_regenerations": 0}),
        )
    if key == "C":
        return ExamGuardTutor(
            backend, settings,
            classifier=HybridIntentClassifier(backend, settings),
            output_guard=BasicOutputGuard(),
            policy_config=ExamPolicy.from_dict({"max_regenerations": 0}),
        )
    # D：审核层按环境选择——mock 下为 rules-v1；API + EG_OUTPUT_GUARD=layered
    # 时为 规则+LLM Judge。会话风险与重生成始终启用。
    from examguard.pipeline import make_output_guard

    return ExamGuardTutor(
        backend, settings,
        classifier=HybridIntentClassifier(backend, settings),
        output_guard=make_output_guard(settings, backend),
        policy_config=ExamPolicy.from_dict({"max_regenerations": 1}),
    )


def new_session_guard(variant_key: str) -> SessionGuard | None:
    return SessionGuard() if VARIANTS[variant_key.upper()].use_session_guard else None
