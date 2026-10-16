"""Stage-one deterministic heuristic; NOT an adversarially robust classifier."""
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


def classify(request: TutorRequest) -> Classification:
    text = request.question.strip()
    if _JAILBREAK.search(text):
        return Classification(Intent.JAILBREAK, "regex-v0", "检测到典型规则绕过措辞")
    if _SOLUTION.search(text):
        return Classification(Intent.SOLUTION, "regex-v0", "检测到直接索取完整解答")
    if _DEBUG.search(text):
        return Classification(Intent.DEBUG, "regex-v0", "检测到调试或错误分析需求")
    if _HINT.search(text):
        return Classification(Intent.HINT, "regex-v0", "检测到方向性提示需求")
    return Classification(Intent.KNOWLEDGE, "regex-v0", "按普通知识问答处理")
