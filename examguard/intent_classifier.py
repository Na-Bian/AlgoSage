"""Stage-one deterministic heuristic; NOT an adversarially robust classifier.

P1 说明：
- 五类意图的正则判定逻辑自第一阶段起**冻结不动**，作为消融实验（A/B/C/D）
  的稳定基线；
- 新增 ``extract_risk_flags``：从问题与已有代码中提取 Prompt Injection /
  变相索取等风险特征，供正则与混合分类器共用。特征仅作预留信号与审计
  展示，不直接改变帮助等级。
"""
import re
from .schemas import Classification, Intent, TutorRequest

_JAILBREAK = re.compile(
    r"忽略.{0,12}(指令|规则|限制|系统)|角色扮演|绕过.{0,10}(限制|审核)|"
    r"系统提示词|developer\s*mode|ignore\s+(all\s+)?previous\s+instructions",
    re.I,
)
_SOLUTION = re.compile(
    r"(完整|全部|整道|直接|可提交|标准答案).{0,14}(代码|程序|题解|解法|证明)|"
    r"(代码|程序|题解|解法).{0,12}(完整|可提交|直接)|"
    r"(给我|替我|帮我写|生成|输出).{0,18}(整题|完整代码|全部代码|最终答案)",
    re.I,
)
_DEBUG = re.compile(r"报错|错误|异常|超时|运行失败|调试|\bBUG\b|\bTLE\b|\bWA\b|这段代码|定位问题", re.I)
_HINT = re.compile(r"提示|思路|方向|启发|入手|怎么开始|如何分析", re.I)

# 风险特征词表：名字 -> 正则（作用于不可信的学生消息文本）。
_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("instruction_override", re.compile(
        r"忽略.{0,12}(指令|规则|限制|系统)|无视.{0,8}(规则|限制|指令)|"
        r"ignore\s+(all\s+)?(previous|above)|disregard\s+(the\s+)?(above|previous|instructions)",
        re.I)),
    ("roleplay_request", re.compile(
        r"角色扮演|你现在是|假装你是|扮演(一个|成)|请假装|"
        r"pretend\s+to\s+be|act\s+as\s+(if\s+)?(you\s+are|an?\b)", re.I)),
    ("system_prompt_probe", re.compile(
        r"系统提示词|system\s*prompt|你的(初始|原始|内部)指令|重复你的(指令|规则)", re.I)),
    ("completion_disguise", re.compile(
        r"补全(以下|成|为|一?下|这段)|帮我补全|把.{0,12}补全|继续写完|接着写完|"
        r"complete\s+the\s+following", re.I)),
    ("translation_disguise", re.compile(
        r"翻译(一下|以下|这段|上面).{0,12}(代码|答案|题解|解法|证明)|把.{0,12}翻译成", re.I)),
    ("full_solution_request", _SOLUTION),
    ("output_encoding", re.compile(
        r"base64|rot13|倒序(输出|打印|给出)|reverse\s+the\s+(text|answer|string)", re.I)),
)

# 学生已有代码中出现指令式文本时标记（代码是低信任输入）。
_CODE_INJECTION = re.compile(
    r"忽略.{0,12}(指令|规则|限制)|ignore\s+(all\s+)?previous|system\s*prompt|你现在是|假装你是",
    re.I,
)


def extract_risk_flags(question: str, existing_code: str = "") -> tuple[str, ...]:
    """从不可信输入中提取风险特征（去重、按命中顺序保留）。"""
    flags: list[str] = []
    text = f"{question}\n{existing_code}"
    for name, pattern in _RISK_PATTERNS:
        if pattern.search(text) and name not in flags:
            flags.append(name)
    if existing_code.strip() and _CODE_INJECTION.search(existing_code):
        if "untrusted_code_injection" not in flags:
            flags.append("untrusted_code_injection")
    return tuple(flags)


def classify(request: TutorRequest, history: list[dict[str, str]] | None = None) -> Classification:
    """冻结的正则基线（history 参数为接口兼容预留，本层不使用）。"""
    text = request.question.strip()
    flags = extract_risk_flags(text, request.existing_code)
    if _JAILBREAK.search(text):
        return Classification(Intent.JAILBREAK, "regex-v0", "检测到典型规则绕过措辞", risk_flags=flags)
    if _SOLUTION.search(text):
        return Classification(Intent.SOLUTION, "regex-v0", "检测到直接索取完整解答", risk_flags=flags)
    if _DEBUG.search(text):
        return Classification(Intent.DEBUG, "regex-v0", "检测到调试或错误分析需求", risk_flags=flags)
    if _HINT.search(text):
        return Classification(Intent.HINT, "regex-v0", "检测到方向性提示需求", risk_flags=flags)
    return Classification(Intent.KNOWLEDGE, "regex-v0", "按普通知识问答处理", risk_flags=flags)
