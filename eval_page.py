"""AlgoSage 教师/开发者单轮冒烟调试页（正式评测使用 harness.run_harness）。

独立于学生端运行：
    .venv/bin/chainlit run eval_page.py --port 8081

用途：
- 在与 Student UI 完全相同的 TutorService / AlgoSageTutor 链路上批量跑
  datasets/ 下的 JSONL 用例，快速查看分类结果与策略行为；
- 这是开发调试入口，不是安全评测结论来源；正式评测（A/B/C/D 消融、
  多轮会话、指标报表）由后续 Harness 模块实现。

页面命令：
- `info`                 查看服务端配置摘要（不含密钥）
- `run smoke`            运行 datasets/smoke_tests.jsonl
- `run <文件名>`          运行 datasets/<文件名>.jsonl
- API 模式下追加 `confirm-api` 才会真正发起远程调用（成本保护）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from uuid import uuid4

import chainlit as cl

from algosage.config import Settings
from algosage.service import TutorService

logger = logging.getLogger("algosage.eval_page")

DATASET_DIR = Path(__file__).parent / "datasets"

_service: TutorService | None = None


def get_service() -> TutorService:
    global _service
    if _service is None:
        _service = TutorService(Settings.from_env())
    return _service


def resolve_dataset_path(filename: str) -> Path:
    """把简写名解析为 datasets/ 下的实际文件路径（如 smoke -> smoke_tests.jsonl）。"""
    name = Path(filename)
    if name.is_absolute() or ".." in name.parts:
        raise ValueError("仅允许 datasets 目录下的数据文件")
    root = DATASET_DIR.resolve()
    candidates = [
        filename,
        filename if filename.endswith(".jsonl") else filename + ".jsonl",
        filename + "_tests.jsonl",
    ]
    for candidate in dict.fromkeys(candidates):
        path = (root / candidate).resolve()
        if not path.is_relative_to(root):
            raise ValueError("仅允许 datasets 目录下的数据文件（禁止外部符号链接）")
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"未找到数据集 {DATASET_DIR / candidates[0]}；可用文件：{_available_datasets()}"
    )


def load_cases(filename: str) -> list[dict]:
    """读取 datasets/ 下的 JSONL；支持简写（如 smoke -> smoke_tests.jsonl）。"""
    path = resolve_dataset_path(filename)
    cases = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    if not cases:
        raise ValueError(f"数据集为空：{path}")
    return cases


def _available_datasets() -> str:
    files = (
        sorted(p.name for p in DATASET_DIR.glob("*.jsonl"))
        if DATASET_DIR.is_dir()
        else []
    )
    return ", ".join(files) or "（无）"


def run_cases(service: TutorService, cases: list[dict]) -> dict:
    """在独立会话上下文中逐条运行用例（纯同步，便于单元测试）。

    每个用例使用独立 context，避免用例间历史污染。返回机器可读摘要；
    正式评测的指标体系（拦截率/泄漏率等）不在此实现。
    """
    # Reject the whole batch before any call; never count an empty prompt as
    # a successful evaluation of formal single/multi-turn Harness data.
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("每条调试用例必须是 JSON 对象")
        if any(key in case for key in ("prompt", "turns", "case_id", "intent_gold")):
            raise ValueError(
                "正式 Harness 数据请使用 python -m harness.run_harness；本页仅支持 question/expected_intent 单轮冒烟格式。"
            )
        if not isinstance(case.get("question"), str) or not case["question"].strip():
            raise ValueError("调试用例缺少有效 question；正式评测请使用 Harness。")
        if not isinstance(case.get("existing_code", ""), str) or not isinstance(
            case.get("question_id", ""), str
        ):
            raise ValueError("existing_code/question_id 必须为字符串")
        # Page-supplied exam_mode/policy fields are deliberately ignored.
    results = []
    run_id = uuid4().hex
    for index, case in enumerate(cases):
        context = service.new_context(f"eval-{run_id}-{index}")
        try:
            response = service.chat(
                context,
                case["question"],
                case.get("existing_code", ""),
                question_id=case.get("question_id", ""),
            )
            predicted = response.classification.intent.value
            expected = case.get("expected_intent")
            results.append(
                {
                    "id": case.get("id", f"case-{index}"),
                    "predicted": predicted,
                    "expected": expected,
                    "pass": predicted == expected if expected else None,
                    "help_level": response.decision.max_help_level,
                    "audit_reason": response.audit.reason,
                    "model": response.model,
                }
            )
        finally:
            service.drop_context(context.session_id)
    labeled = [r for r in results if r["pass"] is not None]
    correct = sum(1 for r in labeled if r["pass"])
    return {
        "total": len(results),
        "labeled": len(labeled),
        "correct": correct,
        "accuracy": (correct / len(labeled)) if labeled else None,
        "results": results,
    }


def render_summary(summary: dict, dataset_name: str) -> str:
    lines = [
        f"### 📊 数据集 `{dataset_name}` 运行结果",
        f"共 {summary['total']} 条；有标注 {summary['labeled']} 条，"
        f"分类一致 {summary['correct']} 条"
        + (
            f"（accuracy={summary['accuracy']:.0%}）"
            if summary["accuracy"] is not None
            else ""
        ),
        "",
        "| 用例 | 预测 | 期望 | 结果 | 帮助等级 | 审核 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in summary["results"]:
        mark = "—" if r["pass"] is None else ("✅" if r["pass"] else "❌")
        lines.append(
            f"| {r['id']} | `{r['predicted']}` | `{r['expected']}` | {mark} "
            f"| L{r['help_level']} | {r['audit_reason']} |"
        )
    lines += [
        "",
        "⚠️ 这是开发调试输出，不是安全评测结论；正式指标请使用 `python -m harness.run_harness`。",
    ]
    return "\n".join(lines)


@cl.on_chat_start
async def on_chat_start() -> None:
    service = get_service()
    await cl.Message(
        content=(
            "### 🧪 AlgoSage 冒烟调试页（教师/开发者）\n"
            "与 Student UI 共用同一 `TutorService` 后端链路。\n\n"
            "命令：`info` ｜ `run smoke` ｜ `run <datasets 下的文件名>`"
            "（API 模式需追加 `confirm-api`）\n\n"
            f"可用数据集：{_available_datasets()}"
        )
    ).send()


@cl.on_chat_end
async def on_chat_end() -> None:
    get_service().drop_context(cl.context.session.id)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    service = get_service()
    text = message.content.strip()
    try:
        if text == "info":
            settings = service.settings
            await cl.Message(
                content=(
                    "| 配置项 | 值 |\n| --- | --- |\n"
                    f"| APP_MODE | `{settings.mode}` |\n"
                    f"| EXAM_MODE | `{settings.exam_mode}` |\n"
                    f"| 模型 | `{settings.model}` |\n"
                    f"| 服务商 Base URL | `{settings.base_url}` |\n"
                    f"| 最大输出 Token | {settings.max_output_tokens} |\n\n"
                    "（密钥仅存在于服务端进程，不在页面显示。）"
                )
            ).send()
            return

        parts = text.split()
        if len(parts) < 2 or parts[0] != "run":
            raise ValueError("未知命令。可用：info / run <数据集名> [confirm-api]")
        dataset_name = parts[1]
        if service.settings.mode == "api" and "confirm-api" not in parts[2:]:
            await cl.Message(
                content=(
                    f"⚠️ 当前为 **API 模式**，将产生真实远程调用与费用。\n"
                    f"如确认继续，请输入：`run {dataset_name} confirm-api`"
                )
            ).send()
            return
        cases = load_cases(dataset_name)
        summary = await asyncio.to_thread(run_cases, service, cases)
        await cl.Message(
            content=render_summary(summary, resolve_dataset_path(dataset_name).name)
        ).send()
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        await cl.Message(content=f"⚠️ {error}").send()
    except Exception as error:  # noqa: BLE001
        logger.exception("评测页处理失败")
        await cl.Message(
            content=f"⚠️ 运行失败（{type(error).__name__}），请查看服务端日志。"
        ).send()
