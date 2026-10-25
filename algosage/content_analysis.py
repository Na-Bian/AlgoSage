"""Bounded, static evidence extraction; never executes submitted or generated code.

These are conservative signals, not a semantic proof of answer leakage. Keep
student-supplied text distinct from new disclosures and avoid counting bare
mentions of a concept as a solved subproblem.
"""

import ast
import re

FENCES = re.compile(r"```[^\n]*\n(.*?)```", re.S)
TRANSITION = re.compile(
    r"(?:^|[^A-Za-z])(?:dp|f|g|memo|d)\s*(?:\[[^\]\n]+\])+\s*=\s*[^=]", re.M | re.I
)
REFUSAL = re.compile(
    r"不能提供|无法提供|不提供|不给出|暂时不能|请先描述|这里不给|不展示|不输出"
)
STATE = re.compile(
    r"(?:设|定义|令)\s*(?:dp|f|g|memo)\s*\[|(?:dp|f|g)\s*(?:\[[^\]\n]+\])+\s*(?:表示|定义为|含义是)",
    re.I,
)


def _supplied_line_patterns(existing_code: str):
    """Exact supplied code lines, allowing whitespace-only formatting changes."""
    for line in existing_code.splitlines():
        line = line.strip()
        if len(line) < 5 or not re.search(r"[=(){};\[\]]", line):
            continue
        pattern = r"\s*".join(re.escape(c) for c in re.sub(r"\s+", "", line))
        # A continuation (old_rhs + a[i]) is new, not an exact quote.
        pattern += r"(?![ \t]*[A-Za-z0-9_+\-*/%&|<>=()\[\].])"
        yield pattern


def mask_existing_code(answer: str, existing_code: str) -> str:
    """Length-preserving provenance view, so original evidence spans stay valid."""
    text = answer
    for pattern in _supplied_line_patterns(existing_code):
        text = re.sub(pattern, lambda match: " " * len(match.group(0)), text)
    return text


def novel_text(
    answer: str, existing_code: str = "", *, keep_refusals: bool = False
) -> str:
    text = answer
    for pattern in _supplied_line_patterns(existing_code):
        text = re.sub(pattern, " [学生原有内容] ", text)
    # Semantic extraction must inspect content after a refusal/disclaimer too.
    if keep_refusals:
        return text
    # Refusal and general concept names alone do not reveal a state/algorithm.
    return "\n".join(
        line
        for line in text.splitlines()
        if not (
            REFUSAL.search(line)
            and not TRANSITION.search(line)
            and not re.search(r"^\s*(def |for |while |return )", line)
        )
    )


def code_fragments(text: str) -> list[str]:
    blocks = FENCES.findall(text)
    if blocks:
        return blocks
    lines = text.splitlines()
    if any(
        re.match(r"\s*(?:def |class |#include|int\s+main|public\s+static)", line)
        for line in lines
    ):
        return [text]
    return []


def complete_program(text: str) -> bool:
    for block in code_fragments(text):
        if re.search(
            r"\b(?:int\s+main\s*\(|public\s+static\s+void\s+main\s*\(|function\s+solve\b)",
            block,
        ):
            return True
        try:
            tree = ast.parse(block.strip())
        except (SyntaxError, ValueError, RecursionError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(n, ast.Return) for n in ast.walk(node)
            ):
                return True
        if any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "input"
            for n in ast.walk(tree)
        ):
            return True
    return False


def has_new_code(text: str, existing_code: str) -> bool:
    """Compare provenance separately; keep original code intact for AST checks."""
    known = re.sub(r"\s+", "", existing_code)
    return any(
        re.sub(r"\s+", "", line) not in known
        for block in code_fragments(text)
        for line in block.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def substantive_code(text: str) -> bool:
    return any(
        line.strip() not in {"", "pass", "...", "TODO", "{", "}"}
        and not line.lstrip().startswith(("#", "def ", "class ", "//"))
        for line in text.splitlines()
    )
