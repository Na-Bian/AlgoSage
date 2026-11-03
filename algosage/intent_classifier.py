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
_DEBUG = re.compile(
    r"报错|错误|异常|超时|运行失败|调试|\bBUG\b|\bTLE\b|\bWA\b|这段代码|定位问题", re.I
)
_HINT = re.compile(r"提示|思路|方向|启发|入手|怎么开始|如何分析", re.I)
_ATTACHMENT_REFERENCE = re.compile(
    r"附件|图片|图像|文档|文件|上一轮|上图|下图|截图", re.I
)
_ATTACHMENT_FACT = re.compile(
    r"(?:根节点|标记|编号|名称|数值|标题|作者|页数|表头|单元格)"
    r".{0,24}(?:是什么|是多少|多少|哪个|哪些|列出|给出|读取|提取)|"
    r"(?:是什么|是多少|多少|哪个|哪些).{0,24}"
    r"(?:根节点|标记|编号|名称|数值|标题|附件|图片|文档|文件)",
    re.I,
)

# 风险特征词表：名字 -> 正则（作用于不可信的学生消息文本）。
_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"忽略.{0,12}(指令|规则|限制|系统)|无视.{0,8}(规则|限制|指令)|"
            r"ignore\s+(all\s+)?(previous|above)|disregard\s+(the\s+)?(above|previous|instructions)",
            re.I,
        ),
    ),
    (
        "roleplay_request",
        re.compile(
            r"角色扮演|你现在是|假装你是|扮演(一个|成)|请假装|"
            r"pretend\s+to\s+be|act\s+as\s+(if\s+)?(you\s+are|an?\b)",
            re.I,
        ),
    ),
    (
        "system_prompt_probe",
        re.compile(
            r"系统提示词|system\s*prompt|你的(初始|原始|内部)指令|重复你的(指令|规则)",
            re.I,
        ),
    ),
    (
        "completion_disguise",
        re.compile(
            r"补全(以下|成|为|一?下|这段)|帮我补全|把.{0,12}补全|继续写完|接着写完|"
            r"complete\s+the\s+following",
            re.I,
        ),
    ),
    (
        "translation_disguise",
        re.compile(
            r"翻译(一下|以下|这段|上面).{0,12}(代码|答案|题解|解法|证明)|把.{0,12}翻译成",
            re.I,
        ),
    ),
    ("full_solution_request", _SOLUTION),
    (
        "output_encoding",
        re.compile(
            r"base64|rot13|倒序(输出|打印|给出)|reverse\s+the\s+(text|answer|string)",
            re.I,
        ),
    ),
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


def is_bounded_attachment_lookup(request: TutorRequest, history=None) -> bool:
    """Recognise a narrow factual lookup over content the student already supplied.

    This prevents a semantic classifier from turning phrases such as "只根据这张
    图片给出标记" into a jailbreak. It only applies to a few explicitly named
    facts and never grants more than the normal knowledge policy (L1).
    """
    text = request.question.strip()
    if (
        not text
        or not _ATTACHMENT_REFERENCE.search(text)
        or not _ATTACHMENT_FACT.search(text)
        or _JAILBREAK.search(text)
        or _SOLUTION.search(text)
    ):
        return False
    if request.attachments:
        return True
    return any(
        isinstance(message, dict)
        and (
            message.get("images")
            or "附件：" in str(message.get("content", ""))
            or "[附件 " in str(message.get("content", ""))
        )
        for message in (history or [])
    )


def classify(
    request: TutorRequest, history: list[dict[str, str]] | None = None
) -> Classification:
    """冻结的正则基线（history 参数为接口兼容预留，本层不使用）。"""
    text = request.question.strip()
    flags = extract_risk_flags(text, request.existing_code)
    if _JAILBREAK.search(text):
        return Classification(
            Intent.JAILBREAK, "regex-v0", "消息里有绕过考试规则的措辞", risk_flags=flags
        )
    if _SOLUTION.search(text):
        return Classification(
            Intent.SOLUTION, "regex-v0", "在直接索要完整解答", risk_flags=flags
        )
    if _DEBUG.search(text):
        return Classification(
            Intent.DEBUG, "regex-v0", "在调试代码或分析报错", risk_flags=flags
        )
    if _HINT.search(text):
        return Classification(
            Intent.HINT, "regex-v0", "只要方向性提示", risk_flags=flags
        )
    return Classification(
        Intent.KNOWLEDGE, "regex-v0", "一般的算法概念问题", risk_flags=flags
    )


_PROMPT_TOPIC = r"系统提示词|system\s*prompt|你的(?:初始|原始|内部)指令"
_DISCUSSION_PREFIX = re.compile(r"^(?:请)?(?:解释|介绍|说明|什么是|为什么)", re.I)
_PROBE_ACTION = re.compile(
    r"告诉我|给我|打印|重复|复述|输出|展示|显示|披露|提供|给出|逐字|原样|全文|reveal|print|show|repeat",
    re.I,
)
_NEGATED_SOLUTION = re.compile(
    r"(^|[，,。！？!?；;\n])\s*(?:请)?(?:不要|不需要|不必|无需|禁止|不得|别)"
    r"(?:重写|提供|输出|给出|编写|生成)?"
    r"(?:完整|全部|可提交)(?:的)?(?:代码|程序|题解|解法|证明|答案)\s*"
    r"(?=$|[，,。！？!?；;\n])"
)
_NEGATED_TOPIC_CLAUSE = re.compile(
    r"^(?:请)?(?:不要|不得|禁止|不)(?:展示|披露|打印|输出|提供|处理|涉及)"
    r"(?:你的)?(?:内部内容|内部指令|初始指令|原始指令|系统提示词|考试答案)(?:全文|原文)?$"
)


def _benign_topic_discussion(text: str, flag: str) -> bool:
    """Allow only positively recognized discussion clauses, not unknown syntax.

    An unmatched imperative, alternate word order or newline keeps the old gate.
    This deliberately sacrifices recall instead of treating 'no regex match' as
    evidence that an instruction is harmless.
    """
    marker = dict(_RISK_PATTERNS)[flag]
    clauses = [c.strip() for c in re.split(r"[。！？!?；;\n，,]", text) if c.strip()]
    if flag == "output_encoding":
        # The entire request matters: do not exempt an encoding lesson followed
        # by an answer request in a separate clause.
        substantive = [c for c in clauses if not _NEGATED_TOPIC_CLAUSE.fullmatch(c)]
        if re.search(
            r"考试|答案|解答|题解|程序|answer|solution|exam",
            " ".join(substantive),
            re.I,
        ):
            return False
    found = False
    for clause in clauses:
        if not marker.search(clause):
            # A request split before its sensitive noun is not a topic question.
            if _PROBE_ACTION.search(clause) and not _NEGATED_TOPIC_CLAUSE.fullmatch(
                clause
            ):
                return False
            continue
        found = True
        if _NEGATED_TOPIC_CLAUSE.fullmatch(clause):
            continue
        if re.search(
            r"然后|接着|同时|并且|以及|而要|而是|并(?:打印|输出|展示|提供|披露)|告诉我|给我|逐字|原样|全文|\bthen\b",
            clause,
            re.I,
        ):
            return False
        if flag == "system_prompt_probe":
            # General security discussion, including a negated disclosure example.
            negative_discussion = re.match(
                r"^(?:请)?(?:解释)?为什么(?:不能|不应|不应该)", clause
            )
            if negative_discussion and len(_PROBE_ACTION.findall(clause)) > 1:
                return False
            dependency_discussion = re.search(
                r"(?:只依赖|仅靠|单靠|只靠).{0,12}(?:系统提示词|system\s*prompt)",
                clause,
                re.I,
            )
            meaning_discussion = re.search(
                r"(?:系统提示词|system\s*prompt).{0,8}(?:一般含义|含义|作用|局限|区别)",
                clause,
                re.I,
            )
            if not (
                _DISCUSSION_PREFIX.search(clause)
                and (negative_discussion or dependency_discussion or meaning_discussion)
            ):
                return False
            if _PROBE_ACTION.search(clause) and not negative_discussion:
                return False
        else:
            lesson = _DISCUSSION_PREFIX.search(clause) or re.match(
                r"^(?:base64|rot13)\s*是", clause, re.I
            )
            if not lesson or not re.search(
                r"编码|加密|区别|原理|含义|普通字符串", clause
            ):
                return False
    return found


def classify_contextual(request: TutorRequest, history=None) -> Classification:
    """Runtime adapter; regex-v0 remains unchanged for the historical baseline.

    Topic mentions are not extraction/encoding instructions. Explicit overrides,
    code injection and semantic classifier findings retain their existing gates.
    This is a narrow heuristic repair, not a general semantic safety proof.
    """
    base = classify(request, history)
    flags = list(base.risk_flags)
    if request.attachment_text:
        attachment_flags = extract_risk_flags(request.attachment_text)
        for flag in attachment_flags:
            if flag in {
                "instruction_override",
                "roleplay_request",
                "system_prompt_probe",
            }:
                if flag not in flags:
                    flags.append(flag)
    text = request.question + "\n" + request.existing_code
    intent = base.intent
    if "full_solution_request" in flags:
        positive_question = _NEGATED_SOLUTION.sub(r"\1", request.question)
        positive_code = _NEGATED_SOLUTION.sub(r"\1", request.existing_code)
        if not _SOLUTION.search(positive_question + "\n" + positive_code):
            flags.remove("full_solution_request")
            if intent is Intent.SOLUTION:
                intent = classify(
                    TutorRequest(
                        positive_question,
                        positive_code,
                        request.exam_mode,
                        request.question_id,
                    )
                ).intent
    if "system_prompt_probe" in flags and _benign_topic_discussion(
        text, "system_prompt_probe"
    ):
        flags.remove("system_prompt_probe")
        remaining = re.sub(_PROMPT_TOPIC, "", request.question, flags=re.I)
        if intent is Intent.JAILBREAK and not _JAILBREAK.search(remaining):
            intent = Intent.KNOWLEDGE
    if "output_encoding" in flags and _benign_topic_discussion(text, "output_encoding"):
        flags.remove("output_encoding")
    return Classification(
        intent, "regex-context-v1", base.reason, risk_flags=tuple(flags)
    )
