"""Grounded solution-element signals and a shared cumulative content budget.

Static phrases catch explicit bindings, not bare topic names. The semantic Judge
can supply grounded evidence for paraphrases/aliases these patterns miss. Neither
layer establishes semantic correctness; quoted evidence remains reviewable.
"""
from __future__ import annotations

import re
from .content_analysis import STATE, TRANSITION, mask_existing_code

BRANCH_ELEMENTS = ("include_branch", "exclude_branch", "branch_comparison")
ELEMENTS = frozenset({"algorithm_choice", "state_definition", "transition", "initialization", "traversal", *BRANCH_ELEMENTS})
_PATTERNS = {
    "state_definition": re.compile(
        r"(?:状态|表格|dp|memo|记录|保存).{0,55}(?:表示|定义为|代表|记录|存储|保存|含义.{0,6}是).{0,100}(?:前.{0,12}(?:件|项|个)|容量|前缀|后缀|区间|最大价值|最优值)"
        r"|(?:代表|表示|记录|保存)(?:(?!(?:代表|表示|记录|保存)).){0,85}"
        r"(?:前.{0,16}(?:物品|件|项)|容量.{0,8}(?:下|限制))"
        r"(?:(?!(?:代表|表示|记录|保存)).){0,85}(?:最大|最优|最长|最短|最小)", re.I),
    "transition": re.compile(
        r"当前总价值.{0,15}(?:=|等于).{0,90}"
        r"|当前(?:状态|最优值|结果).{0,20}(?:等于|取|应当是).{0,40}(?:两种|两者|较大|较小|最大值|最小值|加上|之和|上一|前一|左上|上方)"
        r"|(?:两者|二者|上述两种选择).{0,25}(?:取较大|取最大|取较小|取最小|收益较大|较大值|较小值)"
        r"|(?:装入|选入|选择该物品).{0,35}(?:加上|价值\s*[+＋]|价值加).{0,70}(?:剩余|前一|前\s*[ij])", re.I),
    "initialization": re.compile(
        r"(?:没有物品|容量为?零|容量为?\s*0|i\s*=\s*0|空前缀|边界|初始化|初值|表格).{0,70}(?:设为|置为|初始化为|赋值为|填[为入]?\s*(?:0|零|无穷|INF|[-−]?\d+)|为\s*0|是\s*0|为零)", re.I),
    "traversal": re.compile(
        r"(?:外层|内层|逐个|依次|遍历|枚举|倒序|逆序|正序).{0,65}(?:物品|容量|重量|区间长度|分割点|节点|顶点|状态|所有边)", re.I),
}
# A recurrence can be distributed across prose: included-case value/effect,
# excluded-case retained value/effect, then an optimization over both branches.
# Bare mentions of 'choose/not choose' or resource constraints do not suffice.
_BRANCH_BINDINGS = (
    re.compile(r"(?:选入|选择放入|放入背包|情况\s*A)[\s\S]{0,120}(?:累加|加上|增加)[\s\S]{0,40}(?:价值|收益)|(?:收益|价值).{0,12}(?:加上|累加).{0,45}(?:收益|价值)"),
    re.compile(r"(?:不选|不放入|跳过|情况\s*B)[\s\S]{0,120}(?:容量不(?:减少|变)|(?:保留|继承|沿用).{0,45}(?:价值|收益|结果|记录)|(?:价值|结果).{0,10}不变)"),
    re.compile(r"(?:比较|对比|选取|选择|取)[\s\S]{0,45}(?:上述两种|两者|二者|哪一边|较大|最大|更大|最小)"),
)


def extract_solution_evidence(text: str, existing_code: str = "", source: str = "candidate") -> tuple[dict, ...]:
    """Keep short exact source quotes; refuse no content merely for its intent."""
    found = {}
    masked = mask_existing_code(text, existing_code)
    for match in re.finditer(r"[^\n。；;]+", text):
        raw = match.group(0).strip()
        if not raw:
            continue
        # Keep original quote spans; redact provenance only in the detector view.
        plain = re.sub(r"[$*`_#]", "", masked[match.start():match.end()])
        hits = {name for name, pattern in _PATTERNS.items() if pattern.search(plain)}
        if STATE.search(plain):
            hits.add("state_definition")
        if TRANSITION.search(plain):
            hits.add("transition")
        for name in sorted(hits):
            # A quote is evidence, not a reconstructed or model-generated claim.
            if raw in text:
                found.setdefault(name, {"element": name, "quote": raw[:240], "source": source})
    # Preserve offsets into the original while removing presentation markup.
    view = re.sub(r"[$*`_#]", " ", masked)
    branches = [pattern.search(view) for pattern in _BRANCH_BINDINGS]
    evidence = list(found.values())
    for element, match in zip(BRANCH_ELEMENTS, branches):
        if match:
            evidence.append({"element": element, "quote": text[match.start():match.end()], "source": source})
    if all(branches):
        for match in branches:
            evidence.append({"element": "transition", "quote": text[match.start():match.end()], "source": source})
    return tuple(evidence)


def core_budget_reason(current, previous=(), level: int = 2) -> str:
    """Enforce server policy even when a model's boolean says 'no leak'."""
    current, previous = set(current), set(previous)
    combined = current | previous
    if set(BRANCH_ELEMENTS) <= combined and current & set(BRANCH_ELEMENTS):
        return "cumulative_assembly" if previous else "semantic_transition"
    if level <= 1 and "state_definition" in current:
        return "semantic_state_definition"
    if level <= 2 and "transition" in current:
        return "semantic_transition"
    assemblies = (
        {"state_definition", "transition"},
        {"transition", "initialization"},
        {"transition", "traversal"},
        {"state_definition", "initialization", "traversal"},
    )
    if any(a <= combined and current & a for a in assemblies):
        return "cumulative_assembly" if previous else "semantic_assembly"
    return ""


def evidence_units(candidate: str, history: list[str]) -> dict[str, dict]:
    """Stable source references avoid asking a model to reproduce Markdown/LaTeX.

    All source text is retained, bounded by the caller's context cap. Quotes are
    taken from these exact slices on the server, never reconstructed by the LLM.
    """
    units = {}
    for prefix, source, text in [("C", "candidate", candidate), ("H", "history", "\n\n".join(history))]:
        n = 0
        for part in re.split(r"(?<=[。；;\n])", text):
            for start in range(0, len(part), 240):
                quote = part[start:start+240]
                if quote.strip():
                    n += 1
                    units[f"{prefix}{n}"] = {"source": source, "quote": quote}
    return units


def validate_judge_evidence(payload, candidate: str, history: list[str], existing_code: str = ""):
    """Reject missing schema, invented quotes, invalid types or excessive evidence."""
    raw = payload.get("disclosures")
    if not isinstance(raw, list) or len(raw) > 12:
        return None
    sources = {"candidate": candidate, "history": "\n\n".join(history)}
    units = evidence_units(candidate, history)
    result = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        element, quote, source = (item.get(k) for k in ("element", "quote", "source"))
        if "ref" in item:
            ref = item["ref"]
            if not isinstance(ref, str) or ref not in units:
                return None
            quote, source = units[ref]["quote"], units[ref]["source"]
        if (not isinstance(element, str) or element not in ELEMENTS
            or not isinstance(source, str) or source not in sources
            or not isinstance(quote, str) or not 1 <= len(quote) <= 240
            or quote not in sources[source]):
            return None
        # Quoting the student's own unchanged code cannot spend disclosure budget.
        compact_quote = re.sub(r"\s+", "", quote).strip('`')
        compact_existing = re.sub(r"\s+", "", existing_code)
        if compact_quote and compact_quote in compact_existing:
            continue
        result.append({"element": element, "quote": quote, "source": source})
    return tuple(result)
