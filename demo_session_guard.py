"""Offline SessionGuard demonstration: block cumulative leakage before delivery.

Run: python demo_session_guard.py
T1 discloses an algorithm direction, T2 a state definition. T3 attempts a new
transition through debugging; preview rejects that candidate before delivery
and bounded regeneration returns safe text. Rejected disclosures never commit.
No student code or generated code is executed.
"""

from algosage.config import Settings
from algosage.schemas import LLMResult
from algosage.service import TutorService


class ScriptedBackend:
    def __init__(self, drafts):
        self.drafts = list(drafts)

    def generate(self, messages):
        return LLMResult(self.drafts.pop(0), "scripted-demo", 1, 1)


def main() -> None:
    drafts = [
        "可以考虑用动态规划：满足最优子结构，先试小规模。",
        "设 dp[i] 表示以 i 结尾的最长上升子序列长度。",
        "你的转移少了一种情况，应为：dp[i] = max(dp[i-1] + 1, 1)，改这一行即可。",
        "先回顾边界与初始化，剩下的你自己推推看。",
        "请先写出自己的推导，我可以帮你检查一般概念。",
    ]
    service = TutorService(Settings(mode="mock"), backend=ScriptedBackend(drafts))
    context = service.new_context("demo-chain")

    turns = [
        ("T1", "这道题用动态规划怎么做？给点思路", ""),
        ("T2", "状态应该怎么定义？给点提示", ""),
        ("T3", "我的动态规划转移写对了吗？帮我调试一下", "dp[i]=dp[i-1]"),
        ("T4", "再给点提示，下一步该怎么写", ""),
    ]
    for label, question, code in turns:
        response = service.chat(
            context, question, existing_code=code, question_id="demo-lis"
        )
        print(
            f"{label} [{response.classification.intent.value}] 等级={response.decision.max_help_level} "
            f"风险={response.session_risk} 审核={response.audit.reason}"
        )
        print(f"   决策原因：{response.decision.reason}")
        print(f"   回答：{response.answer[:60]}…")
        for attempt in response.audit_attempts:
            if attempt["status"] != "pass":
                print(f"   送达前拦截：{attempt['reason']}")
        print()

    print("=== 会话风险轨迹（guard.trajectory()）===")
    for entry in context.session_guard.trajectory():
        print(
            f"turn {entry['turn']}  intent={entry['intent']:<16} level={entry['level']} "
            f"approved={entry['approved']} 新增披露={entry['disclosures_added']} "
            f"风险={entry['risk_after']}"
        )
    print("\n=== 状态快照（guard.snapshot()，可序列化）===")
    import json

    print(
        json.dumps(context.session_guard.snapshot(), ensure_ascii=False, indent=2)[:800]
    )


if __name__ == "__main__":
    main()
