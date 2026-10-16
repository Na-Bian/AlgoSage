"""P3 多轮累计泄漏防护演示：攻击链 + 风险轨迹（离线 mock，无 API 调用）。

运行：
    python demo_session_guard.py

演示场景（同一会话、同一题）：
    T1 hint   ：问算法选择      -> 披露 algorithm_choice（风险 +0.15）
    T2 hint   ：追问状态定义    -> 披露 state_definition（风险 +0.25）
    T3 debug  ：伪装调试取得转移式（debug 不因中等风险降级，审核允许 L3）
                -> 披露 transition + local_fix（风险 +0.35 +0.10）
    T4 hint   ：继续要提示      -> 会话降级 L2->L1，回答附可解释原因
"""
from examguard.config import Settings
from examguard.schemas import LLMResult
from examguard.service import TutorService


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
        response = service.chat(context, question, existing_code=code)
        print(f"{label} [{response.classification.intent.value}] 等级={response.decision.max_help_level} "
              f"风险={response.session_risk} 审核={response.audit.reason}")
        print(f"   决策原因：{response.decision.reason}")
        print(f"   回答：{response.answer[:60]}…")
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

    print(json.dumps(context.session_guard.snapshot(), ensure_ascii=False, indent=2)[:800])


if __name__ == "__main__":
    main()
