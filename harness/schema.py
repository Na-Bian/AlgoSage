"""Harness 数据契约与校验（P4）。

单轮用例字段（datasets/harness_single.jsonl，每行一个 JSON 对象）：
- case_id: 全局唯一
- split: "dev" | "heldout"（保留集不得用于调参）
- category: knowledge | debug | hint | request_solution | jailbreak
- intent_gold: 人工标注意图（与 category 一致，冗余存储便于交叉校验）
- is_allowed: 该请求在考试模式下是否合法（k/d/h=true；solution/jailbreak=false）
- question_id: 题目标识（同题多问/多轮关联用；可为空）
- prompt: 学生问题文本
- existing_code: 可选已有代码
- expected_help_level: 期望（默认策略下的）帮助等级上限
- leakage_rubric: 本例"实质泄漏"的人工判定标准（评价器与人工复核共用）
- attack_type: none | direct | roleplay | instruction_override | system_prompt_probe
               | translation | completion | encoding
- notes: 备注

多轮会话字段（datasets/harness_multi.jsonl）：
- case_id / split / session_type: benign | attack_chain | mixed
- question_id: 会话主题题（mixed 会话记录主题）
- turns: [{prompt, existing_code, intent_gold, is_allowed}, ...]（3-5 轮）
- should_leak: 期望系统表现（恒 false——我们希望防住；true 仅用于标注
  "若系统无防护会发生泄漏"的高危会话，供解释用）
- should_help: benign 会话为 true（合法会话应获得帮助）
- notes

校验失败抛 HarnessDataError（fail closed，不静默跳过坏行）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

INTENTS = {"knowledge", "debug", "hint", "request_solution", "jailbreak"}
ATTACK_TYPES = {
    "none",
    "direct",
    "roleplay",
    "instruction_override",
    "system_prompt_probe",
    "translation",
    "completion",
    "encoding",
}
CATEGORY_TO_LEVEL = {
    "knowledge": 1,
    "debug": 3,
    "hint": 2,
    "request_solution": 1,
    "jailbreak": 0,
}


class HarnessDataError(ValueError):
    """测试数据非法；Harness 拒绝运行。"""


@dataclass(frozen=True)
class SingleCase:
    case_id: str
    split: str
    category: str
    intent_gold: str
    is_allowed: bool
    prompt: str
    question_id: str = ""
    existing_code: str = ""
    expected_help_level: int = 1
    leakage_rubric: str = ""
    attack_type: str = "none"
    notes: str = ""


@dataclass(frozen=True)
class MultiCase:
    case_id: str
    split: str
    session_type: str
    question_id: str
    turns: list[dict] = field(default_factory=list)
    should_leak: bool = False
    should_help: bool = True
    notes: str = ""
    leakage_rubric: str = ""


def _load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise HarnessDataError(f"数据文件不存在: {path}")
    rows = []
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise HarnessDataError(f"{path}:{lineno} 非法 JSON: {error}") from error
    if not rows:
        raise HarnessDataError(f"数据文件为空: {path}")
    return rows


def load_single(path: str | Path, split: str | None = None) -> list[SingleCase]:
    cases: list[SingleCase] = []
    seen: set[str] = set()
    for row in _load_jsonl(Path(path)):
        case = SingleCase(
            case_id=str(row.get("case_id", "")),
            split=str(row.get("split", "dev")),
            category=str(row.get("category", "")),
            intent_gold=str(row.get("intent_gold", "")),
            is_allowed=bool(row.get("is_allowed", False)),
            prompt=str(row.get("prompt", "")),
            question_id=str(row.get("question_id", "")),
            existing_code=str(row.get("existing_code", "")),
            expected_help_level=int(row.get("expected_help_level", 1)),
            leakage_rubric=str(row.get("leakage_rubric", "")),
            attack_type=str(row.get("attack_type", "none")),
            notes=str(row.get("notes", "")),
        )
        _validate_common(
            case.case_id, case.split, case.category, case.intent_gold, case.attack_type
        )
        if not case.prompt.strip():
            raise HarnessDataError(f"{case.case_id}: prompt 为空")
        if case.category not in CATEGORY_TO_LEVEL:
            raise HarnessDataError(f"{case.case_id}: 未知 category {case.category}")
        if case.case_id in seen:
            raise HarnessDataError(f"case_id 重复: {case.case_id}")
        seen.add(case.case_id)
        cases.append(case)
    if split:
        cases = [c for c in cases if c.split == split]
    return cases


def load_multi(path: str | Path, split: str | None = None) -> list[MultiCase]:
    cases: list[MultiCase] = []
    seen: set[str] = set()
    for row in _load_jsonl(Path(path)):
        case = MultiCase(
            case_id=str(row.get("case_id", "")),
            split=str(row.get("split", "dev")),
            session_type=str(row.get("session_type", "")),
            question_id=str(row.get("question_id", "")),
            turns=list(row.get("turns", [])),
            should_leak=bool(row.get("should_leak", False)),
            should_help=bool(row.get("should_help", True)),
            notes=str(row.get("notes", "")),
            leakage_rubric=str(row.get("leakage_rubric", "")),
        )
        if case.case_id in seen:
            raise HarnessDataError(f"case_id 重复: {case.case_id}")
        if case.split not in {"dev", "heldout"}:
            raise HarnessDataError(f"{case.case_id}: split 必须是 dev/heldout")
        if case.session_type not in {"benign", "attack_chain", "mixed"}:
            raise HarnessDataError(
                f"{case.case_id}: 未知 session_type {case.session_type}"
            )
        if not 1 <= len(case.turns) <= 8:
            raise HarnessDataError(f"{case.case_id}: turns 数量须在 1..8")
        for index, turn in enumerate(case.turns):
            _validate_common(
                f"{case.case_id}#t{index}",
                case.split,
                str(turn.get("category", "")),
                str(turn.get("intent_gold", "")),
                str(turn.get("attack_type", "none")),
            )
            if not str(turn.get("prompt", "")).strip():
                raise HarnessDataError(f"{case.case_id}#t{index}: prompt 为空")
            if turn.get("intent_gold") not in INTENTS:
                raise HarnessDataError(f"{case.case_id}#t{index}: intent_gold 非法")
        seen.add(case.case_id)
        cases.append(case)
    if split:
        cases = [c for c in cases if c.split == split]
    return cases


def _validate_common(
    case_id: str, split: str, category: str, intent_gold: str, attack_type: str
) -> None:
    if not case_id:
        raise HarnessDataError("case_id 为空")
    if split not in {"dev", "heldout"}:
        raise HarnessDataError(f"{case_id}: split 必须是 dev/heldout")
    if intent_gold not in INTENTS:
        raise HarnessDataError(f"{case_id}: intent_gold 非法: {intent_gold}")
    if category and category != intent_gold:
        raise HarnessDataError(f"{case_id}: category 与 intent_gold 不一致")
    if attack_type not in ATTACK_TYPES:
        raise HarnessDataError(f"{case_id}: attack_type 非法: {attack_type}")
