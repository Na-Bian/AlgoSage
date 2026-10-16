"""可信考试策略配置（P2）：策略引擎与分类器解耦后的服务端策略来源。

设计要点：
- 策略属于**可信配置**：只从服务端文件/内嵌默认值加载，学生界面无法触碰；
  文件解析失败、意图名或等级非法时直接抛错（fail closed），绝不用宽松兜底
  值静默启动。
- 内嵌默认值与第一阶段硬编码映射**完全一致**（jailbreak=0, solution=1,
  knowledge=1, hint=2, debug=3），保证旧测试与消融基线可复现。
- 支持按 question_id 覆盖（教师可对单题收紧），覆盖只允许**收紧或持平**
  （不得超过默认映射的等级），防止配置文件被写得比基线更宽松。

策略文件格式（config/exam_policy.json 有示例）：
{
  "version": 1,
  "exam_mode_levels": {"jailbreak": 0, "request_solution": 1, ...},
  "practice_mode_level": 3,
  "question_overrides": {"q-dp-easy": {"hint": 1}},
  "max_regenerations": 1
}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .schemas import Intent

# 与第一阶段 policy.decide 的硬编码映射逐项一致（冻结基线）。
DEFAULT_EXAM_LEVELS: dict[Intent, int] = {
    Intent.JAILBREAK: 0,
    Intent.SOLUTION: 1,
    Intent.KNOWLEDGE: 1,
    Intent.HINT: 2,
    Intent.DEBUG: 3,
}
DEFAULT_PRACTICE_LEVEL = 3
DEFAULT_MAX_REGENERATIONS = 1


class PolicyConfigError(ValueError):
    """策略配置非法；调用方必须终止启动（fail closed）。"""


@dataclass(frozen=True)
class ExamPolicy:
    exam_mode_levels: dict[Intent, int] = field(default_factory=lambda: dict(DEFAULT_EXAM_LEVELS))
    practice_mode_level: int = DEFAULT_PRACTICE_LEVEL
    question_overrides: dict[str, dict[Intent, int]] = field(default_factory=dict)
    max_regenerations: int = DEFAULT_MAX_REGENERATIONS

    @classmethod
    def default(cls) -> "ExamPolicy":
        return cls()

    def max_level(self, intent: Intent, question_id: str = "") -> int:
        override = self.question_overrides.get(question_id) if question_id else None
        if override and intent in override:
            return override[intent]
        return self.exam_mode_levels[intent]

    def has_override(self, question_id: str, intent: Intent) -> bool:
        override = self.question_overrides.get(question_id) if question_id else None
        return bool(override and intent in override)

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, payload: dict) -> "ExamPolicy":
        """解析并严格校验；任何非法内容抛 PolicyConfigError。"""
        if not isinstance(payload, dict):
            raise PolicyConfigError("策略文件顶层必须是 JSON 对象")

        levels_raw = payload.get("exam_mode_levels")
        if levels_raw is None:
            levels = dict(DEFAULT_EXAM_LEVELS)
        else:
            if not isinstance(levels_raw, dict) or set(levels_raw) != {i.value for i in Intent}:
                raise PolicyConfigError("exam_mode_levels 必须恰好包含全部五类意图")
            levels = {}
            for name, value in levels_raw.items():
                try:
                    intent = Intent(name)
                except ValueError as error:
                    raise PolicyConfigError(f"未知意图: {name}") from error
                if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3:
                    raise PolicyConfigError(f"意图 {name} 的等级必须是 0..3 的整数")
                levels[intent] = value

        practice_level = payload.get("practice_mode_level", DEFAULT_PRACTICE_LEVEL)
        if not isinstance(practice_level, int) or isinstance(practice_level, bool) or not 0 <= practice_level <= 3:
            raise PolicyConfigError("practice_mode_level 必须是 0..3 的整数")

        overrides_raw = payload.get("question_overrides", {})
        if not isinstance(overrides_raw, dict):
            raise PolicyConfigError("question_overrides 必须是对象")
        overrides: dict[str, dict[Intent, int]] = {}
        for question_id, per_intent in overrides_raw.items():
            if not isinstance(question_id, str) or not question_id.strip():
                raise PolicyConfigError("question_overrides 的键必须是非空题目标识")
            if not isinstance(per_intent, dict) or not per_intent:
                raise PolicyConfigError(f"题目 {question_id} 的覆盖必须是非空对象")
            parsed: dict[Intent, int] = {}
            for name, value in per_intent.items():
                try:
                    intent = Intent(name)
                except ValueError as error:
                    raise PolicyConfigError(f"题目 {question_id} 含未知意图: {name}") from error
                if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3:
                    raise PolicyConfigError(f"题目 {question_id} 意图 {name} 的等级必须是 0..3")
                # 覆盖只允许收紧或持平，不得比默认映射更宽松。
                if value > DEFAULT_EXAM_LEVELS[intent]:
                    raise PolicyConfigError(
                        f"题目 {question_id} 意图 {name} 的等级 {value} 超过默认上限 "
                        f"{DEFAULT_EXAM_LEVELS[intent]}（只允许收紧）"
                    )
                parsed[intent] = value
            overrides[question_id] = parsed

        max_regen = payload.get("max_regenerations", DEFAULT_MAX_REGENERATIONS)
        if not isinstance(max_regen, int) or isinstance(max_regen, bool) or not 0 <= max_regen <= 2:
            raise PolicyConfigError("max_regenerations 必须是 0..2 的整数")

        return cls(
            exam_mode_levels=levels,
            practice_mode_level=practice_level,
            question_overrides=overrides,
            max_regenerations=max_regen,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "ExamPolicy":
        file = Path(path)
        if not file.is_file():
            raise PolicyConfigError(f"策略文件不存在: {file}")
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PolicyConfigError(f"策略文件不可读/非法 JSON: {error}") from error
        return cls.from_dict(payload)
