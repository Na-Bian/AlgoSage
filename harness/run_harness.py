"""P4 Harness 主入口：批量运行 A/B/C/D 消融并输出机器可读结果与图表。

用法（在工程根目录）：
    APP_MODE=mock .venv/bin/python -m harness.run_harness       # 离线，全部版本，dev 集
    .venv/bin/python -m harness.run_harness --variants A,D      # 指定版本
    .venv/bin/python -m harness.run_harness --split heldout     # 保留集
    .venv/bin/python -m harness.run_harness --repeats 3         # 重复运行看波动

输出（results/harness_<mode>_<时间戳>/）：
- records.jsonl / sessions.jsonl：逐例记录（含最终回答；仅本地存储）
- summary.md / summary.csv：指标汇总（口径见 harness/metrics.py）
- manifest.json：运行开始时的提交、脱敏配置和源码/数据 SHA-256
- repeat_metrics.json / repeat_stats.json：各独立轮次指标、均值与总体方差/标准差
- metrics_by_variant.png：A/B/C/D 对比图
- mock 模式下所有输出带【MOCK】水印：仅验证流程，不代表模型实验结论。

费用估算：需配置 SAGE_PRICE_PER_MTOK_IN / SAGE_PRICE_PER_MTOK_OUT（每百万
token 价格）；未配置则明确输出"未估算"，绝不伪造。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import statistics
import subprocess
import time
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from algosage.config import Settings
from algosage.llm import make_backend
from algosage.schemas import TutorRequest

from .evaluator import LLMLeakEvaluator, RuleLeakEvaluator
from .metrics import (
    aggregate,
    aggregate_sessions,
    classify_outcome,
    confusion_matrix,
    finalize_session_record,
)
from .schema import MultiCase, SingleCase, load_multi, load_single
from .variants import VARIANTS, build_tutor, new_session_guard, normalize_variant

ROOT = Path(__file__).parent.parent
METRIC_KEYS = [
    "helped_rate",
    "wrong_refusal_rate",
    "block_rate",
    "leak_rate",
    "jailbreak_success_rate",
    "intent_accuracy",
    "audit_unknown_rate",
]


def _plain(value):
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _token_total(calls: list[dict], key: str):
    values = [call.get(key) for call in calls]
    return (
        sum(values)
        if all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in values
        )
        else None
    )


def _response_fields(response) -> dict:
    trace = _plain(getattr(response, "call_trace", ()))
    return {
        "intent_pred": response.classification.intent.value,
        "classification_method": response.classification.method,
        "classification_flags": list(response.classification.risk_flags),
        "classification_reason": response.classification.reason,
        "classification_confidence": response.classification.confidence,
        "help_level": response.decision.max_help_level,
        "policy_reason": response.decision.reason,
        "approved": response.audit.approved,
        "audit_status": getattr(
            response.audit, "status", "pass" if response.audit.approved else "block"
        ),
        "audit_method": response.audit.method,
        "audit_categories": list(response.audit.categories),
        "audit_reason": response.audit.reason,
        "audit_attempts": _plain(getattr(response, "audit_attempts", ())),
        "call_trace": trace,
        "regenerations": response.regenerations,
        "session_risk": response.session_risk,
        "session_disclosures": list(getattr(response, "session_disclosures", ())),
        "input_tokens": (
            _token_total(trace, "input_tokens") if trace else response.input_tokens
        ),
        "output_tokens": (
            _token_total(trace, "output_tokens") if trace else response.output_tokens
        ),
        "token_source": "call_trace" if trace else "response_totals_legacy",
        "model": response.model,
        "answer": response.answer,
        "error": "",
    }


def run_single_case(
    tutor, case: SingleCase, variant: str, session_guard, history=None
) -> dict:
    request = TutorRequest(
        question=case.prompt,
        existing_code=case.existing_code,
        exam_mode=True,
        question_id=case.question_id,
    )
    started = time.perf_counter()
    try:
        fields = _response_fields(
            tutor.respond(request, history=history or [], session_guard=session_guard)
        )
    except Exception as exc:  # preserve failure, never turn it into a clean review
        fields = {
            "answer": "",
            "intent_pred": "unknown",
            "help_level": -1,
            "approved": False,
            "audit_status": "unknown",
            "audit_reason": "harness_error",
            "audit_method": "",
            "audit_categories": [],
            "classification_method": "error",
            "classification_flags": [],
            "classification_reason": "",
            "classification_confidence": None,
            "call_trace": [],
            "audit_attempts": [],
            "regenerations": 0,
            "session_risk": None,
            "input_tokens": None,
            "output_tokens": None,
            "model": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "kind": "single",
        "variant": variant,
        **asdict(case),
        **fields,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "correctness": "unknown",
        "helpfulness": "unknown",
    }


def _attach_evaluation(record: dict, evaluation) -> None:
    record.update(
        evaluator_leak=None if record.get("error") else evaluation.leak,
        evaluator_evidence=evaluation.evidence,
        evaluator_method=evaluation.method,
        evaluator_is_ground_truth=False,
        evaluator_call_trace=evaluation.call_trace,
    )


def run_multi_case(
    tutor, case: MultiCase, variant: str, session_guard, evaluator
) -> dict:
    history: list[dict[str, str]] = []
    turn_records: list[dict] = []
    answers: list[str] = []
    supplied: list[dict] = []
    for index, turn in enumerate(case.turns):
        turn_case = SingleCase(
            case_id=case.case_id,
            split=case.split,
            category=turn["intent_gold"],
            intent_gold=turn["intent_gold"],
            is_allowed=turn["is_allowed"],
            prompt=turn["prompt"],
            existing_code=turn.get("existing_code", ""),
            question_id=case.question_id,
            leakage_rubric=turn.get(
                "leakage_rubric", case.leakage_rubric or case.notes
            ),
        )
        record = run_single_case(tutor, turn_case, variant, session_guard, history)
        record.update(kind="turn", turn=index)
        answer = record["answer"]
        history.extend(
            [
                {"role": "user", "content": turn["prompt"]},
                {"role": "assistant", "content": answer},
            ]
        )
        supplied.append(
            {"prompt": turn["prompt"], "existing_code": turn.get("existing_code", "")}
        )
        evaluation_case = replace(
            turn_case,
            prompt="\n\n".join(
                item["prompt"] + "\n" + item["existing_code"] for item in supplied
            ),
        )
        _attach_evaluation(record, evaluator.evaluate_single(evaluation_case, answer))
        classify_outcome(record)
        turn_records.append(record)
        answers.append(answer)
    session_eval = evaluator.evaluate_session(case, answers)
    session_record = {
        "kind": "multi",
        "variant": variant,
        "case_id": case.case_id,
        "split": case.split,
        "session_type": case.session_type,
        "question_id": case.question_id,
        "leakage_rubric": case.leakage_rubric,
        "case": asdict(case),
        "turns": len(turn_records),
        "turn_records": turn_records,
        "disclosures": session_eval.disclosures,
    }
    _attach_evaluation(session_record, session_eval)
    if (
        any(record["error"] for record in turn_records)
        and session_eval.leak is not True
    ):
        session_record["evaluator_leak"] = None
    finalize_session_record(session_record)
    session_record["pending_review"] = sum(
        record["pending_review"] for record in turn_records
    )
    return session_record


def render_summary(
    variant_summaries: dict,
    session_summaries: dict,
    settings: Settings,
    repeats: int,
    repeat_stats: dict | None,
    out_dir: Path,
) -> str:
    watermark = (
        "【MOCK】本报告由 mock 后端生成，仅验证 Harness 流程；数字不代表真实模型实验结论。C 版在 mock 下回退并标记 uncertain，帮助上限更保守，与 B 不等价。"
        if settings.mode == "mock"
        else ""
    )
    lines = [
        "# AlgoSage Harness 消融汇总",
        "",
        f"- 时间：{datetime.now().isoformat(timespec='seconds')}；模式：`{settings.mode}`；模型：`{settings.model}`",
        f"- 重复运行：{repeats}；输出目录：{out_dir}",
        f"- 分类器/审核配置：SAGE_CLASSIFIER_MODE={settings.classifier_mode}，SAGE_OUTPUT_GUARD={settings.output_guard}",
        "- 指标口径：合法辅助完成率=correctness 且 helpfulness 为 true 的合法回答/两项均已复核的合法回答；"
        "未复核记 unknown，单列 pending_review。拦截率/泄漏率/攻击成功率只在相应标签已知的样本上计算；"
        "误拒率=文本拒答代理/合法数，多轮按会话计。",
        "- 规则与可选 LLM 评测均为代理标签，不是语义真值或人工真值；未检出泄漏不证明合规。"
        "拦截需独立显式合规复核或拒答文本代理，不能由 approved 或低等级推断；泄漏时绝不计拦截。",
        "- audit_unknown_rate 独立统计内部审核 unknown；A 的意图准确率只是常量分类基线。"
        "实际组件与配置见 manifest.json；D-no-judge 在 rules/mock 下与 D 没有 Judge 差异。",
        "",
    ]
    if watermark:
        lines += [f"> {watermark}", ""]
    lines += [
        "## 单轮指标（按版本）",
        "",
        "| 版本 | 说明 | 样本 | 合法辅助完成率 | 误拒率 | 拦截率 | 泄漏率 | 越狱成功 | 意图准确 | 重生成总数 | 待复核 | 审核unknown率 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for key in sorted(variant_summaries):
        s = variant_summaries[key]
        label = VARIANTS[key].label
        lines.append(
            f"| {key} | {label} | {s['cases']} | {fmt(s['helped_rate'])} | {fmt(s['wrong_refusal_rate'])} "
            f"| {fmt(s['block_rate'])} | {fmt(s['leak_rate'])} | {fmt(s['jailbreak_success_rate'])} "
            f"| {fmt(s['intent_accuracy'])} | {s['regenerations_total']} | {s['pending_review']} | {fmt(s['audit_unknown_rate'])} |"
        )
    if repeats > 1 and repeat_stats:
        lines += [
            "",
            f"## 重复运行波动（{repeats} 次，均值 ± 总体标准差；方差见 repeat_stats.json）",
            "",
            "| 版本 | 指标 | 均值 | 标准差 |",
            "| --- | --- | --- | --- |",
        ]
        for (key, metric), (mean, std) in sorted(repeat_stats.items()):
            lines.append(f"| {key} | {metric} | {mean:.4f} | {std:.4f} |")
    lines += [
        "",
        "## 多轮会话指标（按版本，每会话计一次）",
        "",
        "| 版本 | 会话 | 泄漏会话率 | 良性误拒率 | 攻击成功率 | 泄漏会话 | 待复核回答 | 审核unknown率 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for key in sorted(session_summaries):
        s = session_summaries[key]
        lines.append(
            f"| {key} | {s['sessions']} | {fmt(s['session_leak_rate'])} | {fmt(s['benign_wrong_refusal_rate'])} "
            f"| {fmt(s['attack_success_rate'])} | {', '.join(s['leaked_sessions']) or '—'} | {s['pending_review']} | {fmt(s['audit_unknown_rate'])} |"
        )
    # 失败案例
    lines += ["", "## 失败案例（泄漏 / 误拒，按版本）"]
    for key in sorted(variant_summaries):
        s = variant_summaries[key]
        failures = [("泄漏", c) for c in s["leaked_cases"]] + [
            ("误拒", c) for c in s["wrongly_refused_cases"]
        ]
        if failures:
            listed = "、".join(f"{tag}:{cid}" for tag, cid in failures[:12])
            lines.append(f"- {key}: {listed}")
        else:
            lines.append(f"- {key}: 无")
    lines += [
        "",
        "## 费用估算",
        "",
        cost_note(settings, out_dir),
        "",
        "## 复现命令",
        "",
        "```bash",
        ".venv/bin/python -m harness.run_harness --variants A,B,C,D --split dev",
        "```",
    ]
    return "\n".join(lines)


def fmt(value) -> str:
    return "—" if value is None else f"{value:.1%}"


def cost_note(settings: Settings, out_dir: Path) -> str:
    price_in = os.environ.get("SAGE_PRICE_PER_MTOK_IN", "")
    price_out = os.environ.get("SAGE_PRICE_PER_MTOK_OUT", "")
    if not (price_in and price_out):
        return "未配置 SAGE_PRICE_PER_MTOK_IN/OUT，跳过费用估算（不伪造数字）。"
    calls = []
    for filename in ("records.jsonl", "sessions.jsonl"):
        path = out_dir / filename
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                records = (
                    record.get("turn_records", [])
                    if filename == "sessions.jsonl"
                    else [record]
                )
                for item in records:
                    calls.extend(item.get("call_trace") or [item])
                    calls.extend(item.get("evaluator_call_trace", []))
                if filename == "sessions.jsonl":
                    calls.extend(record.get("evaluator_call_trace", []))
    total_in = _token_total(calls, "input_tokens")
    total_out = _token_total(calls, "output_tokens")
    if total_in is None or total_out is None:
        return f"token 用量 unknown（含多轮与评测调用）；输入={total_in if total_in is not None else 'unknown'}，输出={total_out if total_out is not None else 'unknown'}，无法估算总费用。"
    cost = total_in / 1e6 * float(price_in) + total_out / 1e6 * float(price_out)
    return f"输入 {total_in} tok × {price_in} + 输出 {total_out} tok × {price_out} ≈ {cost:.4f} 元（含多轮与评测调用）"


def _redact(value):
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if re.search(
                    r"api[_-]?key|secret|password|credential|authorization|access[_-]?token|refresh[_-]?token|cookie",
                    key,
                    re.I,
                )
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        parts = urlsplit(value)
        return urlunsplit(
            (parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", "")
        )
    return value


def write_manifest(out_dir: Path, settings: Settings, args, components: dict) -> dict:
    def git(*arguments):
        result = subprocess.run(
            ["git", *arguments], cwd=ROOT, capture_output=True, text=True, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    source_paths = sorted(
        {
            *ROOT.glob("*.py"),
            *ROOT.joinpath("algosage").rglob("*.py"),
            *ROOT.joinpath("harness").rglob("*.py"),
        }
    )
    policy = Path(settings.policy_file) if settings.policy_file else None
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "git": {
            "commit": git("rev-parse", "HEAD"),
            "status": git("status", "--porcelain"),
        },
        "settings": _redact(asdict(settings)),
        "arguments": _redact(vars(args)),
        "components": components,
        "evaluator": {
            "method": "rule_proxy" if args.evaluator == "rules" else "llm_proxy",
            "is_ground_truth": False,
        },
        "source_sha256": {
            str(path.relative_to(ROOT)): digest(path) for path in source_paths
        },
        "data_sha256": {
            str(Path(path).resolve()): digest(Path(path))
            for path in (args.single, args.multi)
        },
        "policy_sha256": digest(policy) if policy and policy.is_file() else None,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def plot_chart(variant_summaries: dict, out_path: Path) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("未安装 matplotlib，跳过图表输出")
        return False
    keys = sorted(variant_summaries)
    metrics = [
        "helped_rate",
        "wrong_refusal_rate",
        "block_rate",
        "leak_rate",
        "jailbreak_success_rate",
    ]
    labels = ["合法辅助完成率", "误拒率", "拦截率", "泄漏率", "越狱成功率"]
    width = 0.8 / len(keys)
    fig, axis = plt.subplots(figsize=(9, 4.5))
    for index, key in enumerate(keys):
        values = [
            (
                float("nan")
                if variant_summaries[key][m] is None
                else variant_summaries[key][m] * 100
            )
            for m in metrics
        ]
        axis.bar(
            [i + index * width for i in range(len(metrics))],
            values,
            width=width,
            label=f"{key}: {VARIANTS[key].label}",
        )
    axis.set_xticks([i + width * (len(keys) - 1) / 2 for i in range(len(metrics))])
    axis.set_xticklabels(labels)
    axis.set_ylabel("%")
    axis.set_title("AlgoSage proxy metrics (unknown omitted)")
    axis.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return True


def main() -> int:
    # 批量运行时静默回退告警（回退仍写入 records.jsonl 的 method 字段）。
    logging.getLogger("algosage").setLevel(logging.ERROR)

    parser = argparse.ArgumentParser(description="AlgoSage P4 Harness")
    parser.add_argument("--variants", default="A,B,C,D")
    parser.add_argument("--split", choices=["dev", "heldout", "all"], default="dev")
    parser.add_argument(
        "--single", default=str(ROOT / "datasets" / "harness_single.jsonl")
    )
    parser.add_argument(
        "--multi", default=str(ROOT / "datasets" / "harness_multi.jsonl")
    )
    parser.add_argument("--out-root", default=str(ROOT / "results"))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="并发工作线程数（真实 API 批量运行用；会话内部保持串行）",
    )
    parser.add_argument("--no-chart", action="store_true")
    parser.add_argument(
        "--evaluator",
        choices=["rules", "llm"],
        default="rules",
        help="外部评测代理；二者都不代表人工真值。llm 会额外调用配置的后端",
    )
    args = parser.parse_args()

    if args.repeats < 1 or args.workers < 1:
        parser.error("repeats 和 workers 必须为正整数")
    variant_keys = [normalize_variant(v) for v in args.variants.split(",") if v.strip()]
    if not variant_keys or len(variant_keys) != len(set(variant_keys)):
        parser.error("variants 必须非空且不重复")
    split = None if args.split == "all" else args.split
    single_cases = load_single(args.single, split)
    multi_cases = load_multi(args.multi, split)
    settings = Settings.from_env()
    backend = make_backend(settings)
    evaluator = (
        LLMLeakEvaluator(backend, settings)
        if args.evaluator == "llm"
        else RuleLeakEvaluator()
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_root) / f"harness_{settings.mode}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    all_records: list[dict] = []
    all_sessions: list[dict] = []
    repeat_stats: dict[tuple[str, str], tuple[float, float]] = {}
    per_run_metric_values: dict[tuple[str, str], list[float]] = {}
    per_run_summaries = []

    components = {}
    manifest = write_manifest(out_dir, settings, args, components)
    for repeat in range(args.repeats):
        for key in variant_keys:
            tutor = build_tutor(key, backend, settings)
            components[key] = {
                "output_guard": type(getattr(tutor, "output_guard", None)).__name__,
                "use_session_guard": VARIANTS[key].use_session_guard,
                "max_regenerations": getattr(
                    getattr(tutor, "policy_config", None), "max_regenerations", None
                ),
                "judge_enabled": getattr(
                    getattr(tutor, "output_guard", None), "judge", None
                )
                is not None,
                "classifier": (
                    "constant" if key == "A" else "regex" if key == "B" else "hybrid"
                ),
                "policy": _plain(getattr(tutor, "policy_config", None)),
                "request_exam_mode": True,
            }

            # 单轮：可并发（每个用例独立 guard/历史，互不污染）
            def _run_single(case, _tutor=tutor, _key=key):
                guard = new_session_guard(_key)
                record = run_single_case(_tutor, case, _key, guard)
                evaluation = evaluator.evaluate_single(case, record["answer"])
                _attach_evaluation(record, evaluation)
                record["repeat_id"] = repeat
                classify_outcome(record)
                return record

            if args.workers > 1:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=args.workers) as pool:
                    all_records.extend(pool.map(_run_single, single_cases))
            else:
                for case in single_cases:
                    all_records.append(_run_single(case))

            # 多轮：会话间可并发，会话内部保持串行（历史与风险状态有依赖）
            def _run_multi(case, _tutor=tutor, _key=key):
                guard = new_session_guard(_key)
                record = run_multi_case(_tutor, case, _key, guard, evaluator)
                record["repeat_id"] = repeat
                for turn in record["turn_records"]:
                    turn["repeat_id"] = repeat
                return record

            if args.workers > 1:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=args.workers) as pool:
                    all_sessions.extend(pool.map(_run_multi, multi_cases))
            else:
                for case in multi_cases:
                    all_sessions.append(_run_multi(case))
        # 每轮指标（用于重复运行波动）
        for key in variant_keys:
            records = [
                r
                for r in all_records
                if r["variant"] == key and r["repeat_id"] == repeat
            ]
            summary = aggregate(records)
            session_summary = aggregate_sessions(
                [
                    s
                    for s in all_sessions
                    if s["variant"] == key and s["repeat_id"] == repeat
                ]
            )
            per_run_summaries.append(
                {
                    "variant": key,
                    "repeat_id": repeat,
                    "single": summary,
                    "multi": session_summary,
                }
            )
            for metric in (
                "session_leak_rate",
                "benign_wrong_refusal_rate",
                "attack_success_rate",
            ):
                value = session_summary[metric]
                if value is not None:
                    per_run_metric_values.setdefault((key, metric), []).append(value)
            for metric in METRIC_KEYS:
                value = summary[metric]
                if value is not None:
                    per_run_metric_values.setdefault((key, metric), []).append(value)

    for metric_key, values in per_run_metric_values.items():
        if len(values) >= 2:
            repeat_stats[metric_key] = (
                statistics.mean(values),
                statistics.pstdev(values),
            )

    manifest["completed_at"] = datetime.now().isoformat(timespec="seconds")
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    stats_json = {key: {} for key in variant_keys}
    for (key, metric), values in per_run_metric_values.items():
        stats_json[key][metric] = {
            "mean": statistics.mean(values),
            "variance": statistics.pvariance(values),
            "stddev": statistics.pstdev(values),
            "n": len(values),
        }
    (out_dir / "repeat_stats.json").write_text(
        json.dumps(stats_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "repeat_metrics.json").write_text(
        json.dumps(per_run_summaries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with (out_dir / "records.jsonl").open("w", encoding="utf-8") as handle:
        for record in all_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    with (out_dir / "sessions.jsonl").open("w", encoding="utf-8") as handle:
        for record in all_sessions:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    variant_summaries = {
        key: aggregate([r for r in all_records if r["variant"] == key])
        for key in variant_keys
    }
    session_summaries = {
        key: aggregate_sessions([s for s in all_sessions if s["variant"] == key])
        for key in variant_keys
    }

    summary_md = render_summary(
        variant_summaries,
        session_summaries,
        settings,
        args.repeats,
        repeat_stats,
        out_dir,
    )
    (out_dir / "summary.md").write_text(summary_md + "\n", encoding="utf-8")

    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["variant", "label"]
            + METRIC_KEYS
            + [
                "cases",
                "legal_reviewed_total",
                "pending_review",
                "pending_compliance_review",
                "pending_leak_review",
            ]
        )
        for key in sorted(variant_summaries):
            row = (
                [key, VARIANTS[key].label]
                + [variant_summaries[key][m] for m in METRIC_KEYS]
                + [
                    variant_summaries[key][field]
                    for field in (
                        "cases",
                        "legal_reviewed_total",
                        "pending_review",
                        "pending_compliance_review",
                        "pending_leak_review",
                    )
                ]
            )
            writer.writerow(row)

    for key in sorted(variant_summaries):
        matrix = confusion_matrix([r for r in all_records if r["variant"] == key])
        (out_dir / f"confusion_{key}.json").write_text(
            json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    chart_ok = (
        False
        if args.no_chart
        else plot_chart(variant_summaries, out_dir / "metrics_by_variant.png")
    )

    print(summary_md)
    print(f"\n输出目录：{out_dir}")
    if chart_ok:
        print("图表：metrics_by_variant.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
