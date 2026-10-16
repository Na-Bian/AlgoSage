"""Minimal visible demo guard. Intentionally incomplete; replace in phase two."""
import re
from .schemas import AuditResult, PolicyDecision

_CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.S)


class BasicOutputGuard:
    """Conservative markdown-code heuristic; cannot detect paraphrased algorithm leaks."""
    def review(self, draft: str, decision: PolicyDecision, exam_mode: bool) -> AuditResult:
        if not draft.strip():
            return AuditResult(False, "这次没有得到有效回答，请稍后重试。", "empty_output")
        if not exam_mode:
            return AuditResult(True, draft, "practice_mode")
        for block in _CODE_BLOCK.findall(draft):
            nonempty_lines = [line for line in block.splitlines() if line.strip()]
            limit = 8 if decision.max_help_level == 3 else 2
            if len(nonempty_lines) > limit:
                return AuditResult(
                    False,
                    "为避免直接给出完整实现，我可以改为解释关键概念或定位已有代码的局部问题。",
                    "code_block_over_limit",
                )
        if re.search(r"if\s+__name__\s*==\s*['\"]__main__['\"]", draft):
            return AuditResult(
                False,
                "这里不提供可直接提交的完整程序。你希望先检查哪一段逻辑？",
                "entrypoint_detected",
            )
        return AuditResult(True, draft, "baseline_heuristic_pass")
