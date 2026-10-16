"""P3 多轮累计泄漏防护：SessionGuard 与 RiskState。

目标（对应作业指南"多轮累计泄漏"）：
- 同一考试题目（显式 question_id 或相近问题关联）内，跟踪已披露的知识
  元素、提示等级、审计结论与累计风险分；
- 风险更新**不以轮数或关键词命中次数**为主，而以"新增披露元素 × 元素
  权重 × 新颖度"累计：同一元素重复披露只按 0.25 衰减因子计入，真正推动
  风险上升的是**互补性新信息**（选算法 → 状态定义 → 转移式 → 完整代码）；
- 高风险自动下调该会话内此题的帮助等级上限，并给出可解释原因；
  knowledge 意图的等级下限保持 L1（避免完全拒绝普通概念问题）。

题目关联规则（deterministic，可在测试中逐条验证）：
1. 显式 question_id 相同 -> 同一状态（主要路径，P4 Harness 将使用）；
2. 归一化文本 bigram Jaccard >= 0.45 -> 同题（强改写相似）；
3. 共享 >= 2 个算法关键词 -> 同题；
4. 共享 1 个算法关键词且 Jaccard >= 0.10 -> 同题；
5. 当前为 hint/solution 意图、状态近期为 hint/solution/debug、锚点含算法
   关键词而当前问题省略了它，且 Jaccard >= 0.03 -> 同题（"状态怎么定义？"
   接着上一轮的追问）；
6. 当前为 hint/solution 意图、状态近期为 hint/solution/debug 且双方都无
   算法关键词 -> 同题（连续无关键词追问）；
7. solution 意图且自身无算法关键词 -> 关联到**最近活跃**状态
   （"那把完整代码给我吧"承接上下文；可能轻微过度关联，P4 量化）。

不匹配的典型（防过度阻止）：KMP 调试后转 Kruskal 提示（无共享关键词且
Jaccard 低）、考题讨论后的一般知识问题（knowledge 意图不参与 5/6/7 规则）。

已知边界：纯意译改写（无共享关键词、字面相似度低）在无 question_id 时
可能漏关联——这是本原型的诚实局限，报告与 P4 有效性威胁分析中说明。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .leakage_guard import code_block_line_counts, has_proof_completion, has_transition
from .schemas import AuditResult, Classification, Intent, PolicyDecision, TutorRequest

# ---- 可解释的风险模型参数（常量集中，便于消融与调参） ----
ELEMENT_WEIGHTS: dict[str, float] = {
    "algorithm_choice": 0.15,
    "state_definition": 0.25,
    "transition": 0.35,
    "key_steps": 0.40,
    "pseudocode": 0.50,
    "full_code": 1.00,
    "full_proof": 1.00,
    "local_fix": 0.10,
    "snippet": 0.05,
    "complexity": 0.05,
    "concept": 0.05,
}
REPEAT_FACTOR = 0.25          # 已披露元素再次出现时的边际贡献
RISK_CAP = 1.0
# 会话风险对帮助等级的上限（按意图分级，可解释）：
# - hint/solution（考题实质路径）：>=0.4 -> L2、>=0.6 -> L1、>=0.8 -> L0；
# - debug：仅极高风险降级（>=0.8 -> L2、>=0.9 -> L1）——内容审核始终兜底，
#   学生自己代码的局部诊断不因先前提示被过度牵连；
# - knowledge：最低保持 L1，不因会话风险完全拒答概念问题。
HINT_SOLUTION_CAPS: tuple[tuple[float, int], ...] = ((0.8, 0), (0.6, 1), (0.4, 2))
DEBUG_CAPS: tuple[tuple[float, int], ...] = ((0.9, 1), (0.8, 2))
KNOWLEDGE_FLOOR = 1
HISTORY_LIMIT = 50
AUDIT_LIMIT = 10

_PUNCT = re.compile(r"[\s，。？！,.?!:：;；、\"'‘’“”（）()\[\]<>《》]+")
_ALGO_KEYWORDS = (
    "动态规划", "dp", "贪心", "并查集", "kmp", "前缀函数", "线段树", "树状数组",
    "二分", "三分", "最短路", "拓扑", "网络流", "记忆化", "分治", "哈希", "堆",
    "单调栈", "前缀和", "差分", "kruskal", "prim", "dijkstra", "floyd",
    "bellman", "背包", "lis", "lcs", "编辑距离", "快速幂", "矩阵快速幂",
)
_D_ALGO_CHOICE = re.compile(
    r"(?:用|选|考虑|尝试|适合|可(?:以)?用)\s{0,3}(?:动态规划|dp|贪心|并查集|kmp|线段树|"
    r"树状数组|二分|最短路|拓扑|网络流|记忆化|分治|哈希|堆|单调栈|前缀和|差分|"
    r"kruskal|prim|dijkstra|floyd|背包|快速幂)"
    r"|(?:动态规划|dp|贪心|并查集|kmp|线段树|二分|最短路|拓扑|记忆化|分治|哈希|堆)"
    r".{0,8}(?:即可|就行|可以通过|可过)",
    re.I,
)
_D_STATE_DEF = re.compile(
    r"(?:设|定义|令)\s*(?:dp|f|g|memo)\s*\["
    r"|状态(?:的)?(?:定义|表示|含义|是)"
    r"|(?:dp|f|g)\s*\[[^\]]+\]\s*(?:表示|定义为|含义是)",
    re.I,
)
_D_COMPLEXITY = re.compile(r"[OT]\s*\(")
_D_PSEUDO = re.compile(r"伪代码|伪码|算法流程如下", re.I)
_D_STEP = re.compile(r"^\s*(?:步骤\s*)?\d[\.、）)]\s*\S+", re.M)


def normalize_text(text: str) -> str:
    return _PUNCT.sub("", text.lower())


def extract_keywords(text: str) -> set[str]:
    lowered = text.lower()
    return {kw for kw in _ALGO_KEYWORDS if kw in lowered}


def _bigrams(normalized: str) -> set[str]:
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)} if len(normalized) >= 2 else {normalized}


def bigram_jaccard(a_norm: str, b_norm: str) -> float:
    a, b = _bigrams(a_norm), _bigrams(b_norm)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class RiskState:
    """单题会话风险状态；字段可版本化序列化（to_dict/from_dict）。"""

    question_id: str = ""
    # 锚点保留该题历史问法的归一化文本（去重、上限内），避免滚动锚点丢失
    # 早期题面关键词（"动态规划"被后续追问省略后仍可关联）。
    anchor_texts: list[str] = field(default_factory=list)
    disclosed_elements: list[str] = field(default_factory=list)
    risk_score: float = 0.0
    turns: int = 0
    last_intent: Optional[str] = None
    recent_audits: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    version: int = 2

    ANCHOR_LIMIT = 20

    def add_anchor(self, normalized: str) -> None:
        if normalized and normalized not in self.anchor_texts:
            self.anchor_texts.append(normalized)
            del self.anchor_texts[: -self.ANCHOR_LIMIT]

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "question_id": self.question_id,
            "anchor_texts": list(self.anchor_texts),
            "disclosed_elements": list(self.disclosed_elements),
            "risk_score": round(self.risk_score, 4),
            "turns": self.turns,
            "last_intent": self.last_intent,
            "recent_audits": list(self.recent_audits),
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "RiskState":
        anchors = payload.get("anchor_texts")
        if anchors is None:  # 兼容 v1 的单锚点字段
            anchors = [payload["anchor_text_norm"]] if payload.get("anchor_text_norm") else []
        return cls(
            version=int(payload.get("version", 1)),
            question_id=str(payload.get("question_id", "")),
            anchor_texts=[str(a) for a in anchors],
            disclosed_elements=list(payload.get("disclosed_elements", [])),
            risk_score=float(payload.get("risk_score", 0.0)),
            turns=int(payload.get("turns", 0)),
            last_intent=payload.get("last_intent"),
            recent_audits=list(payload.get("recent_audits", [])),
            history=list(payload.get("history", [])),
        )


@dataclass(frozen=True)
class RiskAdjustment:
    level: int
    reason: str


def detect_disclosures(answer_text: str, intent: Intent, help_level: int) -> list[str]:
    """从**实际送达学生**的回答文本中识别已披露元素（放行文本才会被分析）。"""
    if not answer_text.strip():
        return []
    found: list[str] = []
    if intent is Intent.KNOWLEDGE:
        found.append("concept")
    if intent is Intent.DEBUG:
        counts = code_block_line_counts(answer_text)
        if counts:
            found.append("local_fix" if max(counts) > 2 else "snippet")
        else:
            found.append("local_fix")
    if _D_COMPLEXITY.search(answer_text):
        found.append("complexity")
    if _D_ALGO_CHOICE.search(answer_text):
        found.append("algorithm_choice")
    if _D_STATE_DEF.search(answer_text):
        found.append("state_definition")
    if has_transition(answer_text):
        found.append("transition")
    if has_proof_completion(answer_text):
        found.append("full_proof")
    counts = code_block_line_counts(answer_text)
    if counts and max(counts) > 8:
        found.append("full_code")
    steps = _D_STEP.findall(answer_text)
    if _D_PSEUDO.search(answer_text) and len(steps) >= 3:
        found.append("pseudocode")
    elif len(steps) >= 3 and help_level >= 2:
        found.append("key_steps")
    return [f for f in dict.fromkeys(found) if f in ELEMENT_WEIGHTS]


class SessionGuard:
    """每个聊天会话一份；按题维护 RiskState，提供风险上限与披露记录。"""

    def __init__(self):
        self._states: dict[str, RiskState] = {}

    # ------------------------------------------------------------------
    # 题目定位（关联规则见模块 docstring）
    # ------------------------------------------------------------------
    def _match_key(self, request: TutorRequest, intent: Intent) -> Optional[str]:
        if request.question_id:
            key = f"qid:{request.question_id}"
            return key if key in self._states else None
        text_norm = normalize_text(request.question)
        kw = extract_keywords(request.question)
        for key, state in reversed(self._states.items()):
            if key.startswith("qid:"):
                continue
            best_j = 0.0
            kw_overlap = 0
            any_anchor_kw = False
            for anchor in state.anchor_texts:
                best_j = max(best_j, bigram_jaccard(text_norm, anchor))
                anchor_kw = extract_keywords(anchor)
                any_anchor_kw = any_anchor_kw or bool(anchor_kw)
                kw_overlap = max(kw_overlap, len(kw & anchor_kw))
            if best_j >= 0.45:
                return key
            if kw_overlap >= 2:
                return key
            if kw_overlap == 1 and best_j >= 0.10:
                return key
            if (
                intent.value in {"hint", "request_solution"}
                and state.last_intent in {"hint", "request_solution", "debug"}
            ):
                if any_anchor_kw and not kw and best_j >= 0.03:
                    return key
                if not any_anchor_kw and not kw:
                    return key
        # 规则 7：无关键词的"把完整代码给我"承接最近活跃状态
        if intent is Intent.SOLUTION and not kw and self._states:
            for key, state in reversed(self._states.items()):
                if state.last_intent in {"hint", "request_solution", "debug"}:
                    return key
        return None

    def _locate(self, request: TutorRequest, intent: Intent) -> RiskState:
        key = self._match_key(request, intent)
        if key is not None:
            return self._states[key]
        if request.question_id:
            key = f"qid:{request.question_id}"
        else:
            import hashlib

            key = "txt:" + hashlib.sha1(normalize_text(request.question).encode()).hexdigest()[:12]
        state = RiskState(question_id=request.question_id)
        state.add_anchor(normalize_text(request.question))
        self._states[key] = state
        return state

    # ------------------------------------------------------------------
    # 生成前：按累计风险下调帮助等级上限（只降不升，附可解释原因）
    # ------------------------------------------------------------------
    def before(self, request: TutorRequest, classification: Classification, policy: PolicyDecision):
        state = self._locate(request, classification.intent)
        cap = self._cap_for(state.risk_score, classification.intent)
        if cap is None or not policy.max_help_level:
            return state, None
        effective = min(policy.max_help_level, cap)
        if effective >= policy.max_help_level:
            return state, None
        disclosed = "、".join(state.disclosed_elements) or "无"
        reason = (
            f"会话累计风险 {state.risk_score:.2f}（本会话该题已披露：{disclosed}），"
            f"帮助等级上限 {policy.max_help_level} -> {effective}"
        )
        return state, RiskAdjustment(effective, reason)

    @staticmethod
    def _cap_for(risk: float, intent: Intent) -> Optional[int]:
        """按意图返回会话风险对应的服务等级上限；None 表示不下调。"""
        if intent is Intent.DEBUG:
            caps = DEBUG_CAPS
        elif intent is Intent.KNOWLEDGE:
            caps = ((0.8, KNOWLEDGE_FLOOR), (0.6, KNOWLEDGE_FLOOR))  # L1 恒等于原等级
        else:
            caps = HINT_SOLUTION_CAPS
        for threshold, level in caps:
            if risk >= threshold:
                return level
        return None

    # ------------------------------------------------------------------
    # 审核后：记录实际披露并更新风险（互补新信息主导，重复衰减）
    # ------------------------------------------------------------------
    def record(
        self,
        state: RiskState,
        request: TutorRequest,
        classification: Classification,
        policy: PolicyDecision,
        audit: AuditResult,
    ) -> dict:
        disclosures = detect_disclosures(audit.output, classification.intent, policy.max_help_level)
        added: list[str] = []
        for element in disclosures:
            novelty = 1.0 if element not in state.disclosed_elements else REPEAT_FACTOR
            state.risk_score = min(RISK_CAP, state.risk_score + ELEMENT_WEIGHTS[element] * novelty)
            if element not in state.disclosed_elements:
                state.disclosed_elements.append(element)
                added.append(element)
        state.turns += 1
        state.last_intent = classification.intent.value
        state.add_anchor(normalize_text(request.question))
        state.recent_audits.append(
            {"turn": state.turns, "approved": audit.approved, "reason": audit.reason}
        )
        state.recent_audits = state.recent_audits[-AUDIT_LIMIT:]
        state.history.append(
            {
                "turn": state.turns,
                "intent": classification.intent.value,
                "level": policy.max_help_level,
                "approved": audit.approved,
                "audit_reason": audit.reason,
                "disclosures_added": added,
                "risk_after": round(state.risk_score, 4),
            }
        )
        state.history = state.history[-HISTORY_LIMIT:]
        return {"risk": round(state.risk_score, 4), "disclosures": tuple(state.disclosed_elements)}

    # ------------------------------------------------------------------
    def trajectory(self, question_id: str = "") -> list[dict]:
        if question_id:
            state = self._states.get(f"qid:{question_id}")
            return list(state.history) if state else []
        return [entry for state in self._states.values() for entry in state.history]

    def snapshot(self) -> dict:
        return {key: state.to_dict() for key, state in self._states.items()}
