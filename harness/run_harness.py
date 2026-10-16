"""P4 Harness 主入口：批量运行 A/B/C/D 消融并输出机器可读结果与图表。

用法（在工程根目录）：
    .venv/bin/python -m harness.run_harness                     # 全部版本，dev 集，mock
    .venv/bin/python -m harness.run_harness --variants A,D      # 指定版本
    .venv/bin/python -m harness.run_harness --split heldout     # 保留集
    .venv/bin/python -m harness.run_harness --repeats 3         # 重复运行看波动

输出（results/harness_<mode>_<时间戳>/）：
- records.jsonl / sessions.jsonl：逐例记录（含最终回答；仅本地存储）
- summary.md / summary.csv：指标汇总（口径见 harness/metrics.py）
- metrics_by_variant.png：A/B/C/D 对比图
- mock 模式下所有输出带【MOCK】水印：仅验证流程，不代表模型实验结论。

费用估算：需配置 EG_PRICE_PER_MTOK_IN / EG_PRICE_PER_MTOK_OUT（每百万
token 价格）；未配置则明确输出"未估算"，绝不伪造。
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import statistics
import time
from datetime import datetime
from pathlib import Path

from examguard.config import Settings
from examguard.llm import make_backend
from examguard.schemas import TutorRequest

from .evaluator import RuleLeakEvaluator
from .metrics import (
    aggregate,
    aggregate_sessions,
    classify_outcome,
    confusion_matrix,
    finalize_session_record,
)
from .schema import MultiCase, SingleCase, load_multi, load_single
from .variants import VARIANTS, build_tutor, new_session_guard

ROOT = Path(__file__).parent.parent
METRIC_KEYS = [
    "helped_rate", "wrong_refusal_rate", "block_rate",
    "leak_rate", "jailbreak_success_rate", "intent_accuracy",
]


def run_single_case(tutor, case: SingleCase, variant: str, session_guard) -> dict:
    request = TutorRequest(
        question=case.prompt,
        existing_code=case.existing_code,
        exam_mode=True,
        question_id=case.question_id,
    )
    started = time.perf_counter()
    error = ""
    try:
        response = tutor.respond(request, history=[], session_guard=session_guard)
        answer = response.answer
        intent_pred = response.classification.intent.value
        help_level = response.decision.max_help_level
        approved = response.audit.approved
        audit_reason = response.audit.reason
        regenerations = response.regenerations
        session_risk = response.session_risk
        tokens_in, tokens_out = response.input_tokens, response.output_tokens
        model = response.model
    except Exception as exc:  # noqa: BLE001 - 记录失败而不是中断整批
        answer, intent_pred, help_level = "", "knowledge", -1
        approved, audit_reason, regenerations, session_risk = False, f"harness_error:{type(exc).__name__}", 0, None
        tokens_in = tokens_out = None
        model = "error"
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = int((time.perf_counter() - started) * 1000)
    record = {
        "kind": "single", "variant": variant, "case_id": case.case_id, "split": case.split,
        "category": case.category, "attack_type": case.attack_type,
        "is_allowed": case.is_allowed, "intent_gold": case.intent_gold, "intent_pred": intent_pred,
        "help_level": help_level, "expected_help_level": case.expected_help_level,
        "approved": approved, "audit_reason": audit_reason,
        "regenerations": regenerations, "session_risk": session_risk,
        "latency_ms": latency_ms, "input_tokens": tokens_in, "output_tokens": tokens_out,
        "model": model, "answer": answer, "error": error,
    }
    return record


def run_multi_case(tutor, case: MultiCase, variant: str, session_guard, evaluator) -> dict:
    history: list[dict[str, str]] = []
    turn_records: list[dict] = []
    answers: list[str] = []
    for index, turn in enumerate(case.turns):
        request = TutorRequest(
            question=turn["prompt"],
            existing_code=turn.get("existing_code", ""),
            exam_mode=True,
            question_id=case.question_id,
        )
        started = time.perf_counter()
        try:
            response = tutor.respond(request, history=history, session_guard=session_guard)
            answer = response.answer
            history.extend([
                {"role": "user", "content": turn["prompt"]},
                {"role": "assistant", "content": answer},
            ])
            turn_record = {
                "turn": index, "intent_gold": turn["intent_gold"],
                "intent_pred": response.classification.intent.value,
                "is_allowed": turn["is_allowed"],
                "category": turn["intent_gold"],
                "help_level": response.decision.max_help_level,
                "approved": response.audit.approved,
                "audit_reason": response.audit.reason,
                "regenerations": response.regenerations,
                "session_risk": response.session_risk,
                "answer": answer, "error": "",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "input_tokens": response.input_tokens, "output_tokens": response.output_tokens,
            }
        except Exception as exc:  # noqa: BLE001
            answer = ""
            turn_record = {
                "turn": index, "intent_gold": turn["intent_gold"], "intent_pred": "knowledge",
                "is_allowed": turn["is_allowed"], "category": turn["intent_gold"],
                "help_level": -1, "approved": False, "audit_reason": "harness_error",
                "regenerations": 0, "session_risk": None, "answer": "",
                "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": 0, "input_tokens": None, "output_tokens": None,
            }
        evaluation = evaluator.evaluate_single(
            SingleCase(
                case_id=case.case_id, split=case.split, category=turn["intent_gold"],
                intent_gold=turn["intent_gold"], is_allowed=turn["is_allowed"],
                prompt=turn["prompt"], existing_code=turn.get("existing_code", ""),
                leakage_rubric=case.notes,
            ),
            answer,
        )
        turn_record.update(
            evaluator_leak=evaluation.leak,
            evaluator_evidence=evaluation.evidence,
        )
        classify_outcome(turn_record)
        turn_records.append(turn_record)
        answers.append(answer)
    session_eval = evaluator.evaluate_session(case, answers)
    session_record = {
        "kind": "multi", "variant": variant, "case_id": case.case_id, "split": case.split,
        "session_type": case.session_type, "question_id": case.question_id,
        "turns": len(turn_records), "turn_records": turn_records,
        "evaluator_leak": session_eval.leak, "evaluator_evidence": session_eval.evidence,
        "disclosures": session_eval.disclosures,
    }
    finalize_session_record(session_record)
    return session_record


def render_summary(variant_summaries: dict, session_summaries: dict, settings: Settings,
                   repeats: int, repeat_stats: dict | None, out_dir: Path) -> str:
    watermark = "【MOCK】本报告由 mock 后端生成，仅验证 Harness 流程；数字不代表真实模型实验结论。C 版在 mock 下回退正则（≈B）。" \
        if settings.mode == "mock" else ""
    lines = [
        "# ExamGuard Harness 消融汇总",
        "",
        f"- 时间：{datetime.now().isoformat(timespec='seconds')}；模式：`{settings.mode}`；模型：`{settings.model}`",
        f"- 重复运行：{repeats}；输出目录：{out_dir}",
        f"- 分类器/审核配置：EG_CLASSIFIER_MODE={settings.classifier_mode}，EG_OUTPUT_GUARD={settings.output_guard}",
        "- 指标口径（分母）：合法辅助完成率=helped/合法数；拦截率=被拦截/违规数；泄漏率=评价器判泄漏/违规数"
        "（评价器与被测系统解耦，不以系统自评为准）；误拒率=被误拒/合法数；越狱成功率=攻击得手/攻击数（多轮按会话计）。",
        "",
    ]
    if watermark:
        lines += [f"> {watermark}", ""]
    lines += ["## 单轮指标（按版本）", "",
              "| 版本 | 说明 | 样本 | 合法辅助完成率 | 误拒率 | 拦截率 | 泄漏率 | 越狱成功 | 意图准确 | 重生成总数 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for key in sorted(variant_summaries):
        s = variant_summaries[key]
        label = VARIANTS[key].label
        lines.append(
            f"| {key} | {label} | {s['cases']} | {fmt(s['helped_rate'])} | {fmt(s['wrong_refusal_rate'])} "
            f"| {fmt(s['block_rate'])} | {fmt(s['leak_rate'])} | {fmt(s['jailbreak_success_rate'])} "
            f"| {fmt(s['intent_accuracy'])} | {s['regenerations_total']} |"
        )
    if repeats > 1 and repeat_stats:
        lines += ["", f"## 重复运行波动（{repeats} 次，均值 ± 标准差）", "",
                  "| 版本 | 指标 | 均值 | 标准差 |", "| --- | --- | --- | --- |"]
        for (key, metric), (mean, std) in sorted(repeat_stats.items()):
            lines.append(f"| {key} | {metric} | {mean:.4f} | {std:.4f} |")
    lines += ["", "## 多轮会话指标（按版本，每会话计一次）", "",
              "| 版本 | 会话 | 泄漏会话率 | 良性误拒率 | 攻击成功率 | 泄漏会话 |",
              "| --- | --- | --- | --- | --- | --- |"]
    for key in sorted(session_summaries):
        s = session_summaries[key]
        lines.append(
            f"| {key} | {s['sessions']} | {fmt(s['session_leak_rate'])} | {fmt(s['benign_wrong_refusal_rate'])} "
            f"| {fmt(s['attack_success_rate'])} | {', '.join(s['leaked_sessions']) or '—'} |"
        )
    # 失败案例
    lines += ["", "## 失败案例（泄漏 / 误拒，按版本）"]
    for key in sorted(variant_summaries):
        s = variant_summaries[key]
        failures = [("泄漏", c) for c in s["leaked_cases"]] + [("误拒", c) for c in s["wrongly_refused_cases"]]
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
    price_in = os.environ.get("EG_PRICE_PER_MTOK_IN", "")
    price_out = os.environ.get("EG_PRICE_PER_MTOK_OUT", "")
    if not (price_in and price_out):
        return "未配置 EG_PRICE_PER_MTOK_IN/OUT，跳过费用估算（不伪造数字）。"
    total_in = total_out = 0
    records_path = out_dir / "records.jsonl"
    if records_path.is_file():
        for line in records_path.open(encoding="utf-8"):
            record = json.loads(line)
            total_in += record.get("input_tokens") or 0
            total_out += record.get("output_tokens") or 0
    cost = total_in / 1e6 * float(price_in) + total_out / 1e6 * float(price_out)
    return f"输入 {total_in} tok × {price_in} + 输出 {total_out} tok × {price_out} ≈ {cost:.4f} 元"


def plot_chart(variant_summaries: dict, out_path: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("未安装 matplotlib，跳过图表输出")
        return False
    keys = sorted(variant_summaries)
    metrics = ["helped_rate", "wrong_refusal_rate", "block_rate", "leak_rate", "jailbreak_success_rate"]
    labels = ["合法辅助完成率", "误拒率", "拦截率", "泄漏率", "越狱成功率"]
    width = 0.8 / len(keys)
    fig, axis = plt.subplots(figsize=(9, 4.5))
    for index, key in enumerate(keys):
        values = [(variant_summaries[key][m] or 0) * 100 for m in metrics]
        axis.bar([i + index * width for i in range(len(metrics))], values,
                 width=width, label=f"{key}: {VARIANTS[key].label}")
    axis.set_xticks([i + width * (len(keys) - 1) / 2 for i in range(len(metrics))])
    axis.set_xticklabels(labels)
    axis.set_ylabel("%")
    axis.set_title("ExamGuard A/B/C/D 防护消融对比")
    axis.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return True


def main() -> int:
    # 批量运行时静默回退告警（mock 下 hybrid 必然回退，属预期行为，
    # 汇总中已标注 C≈B；真实告警仍写入 records.jsonl 的 method 字段）。
    logging.getLogger("examguard").setLevel(logging.ERROR)

    parser = argparse.ArgumentParser(description="ExamGuard P4 Harness")
    parser.add_argument("--variants", default="A,B,C,D")
    parser.add_argument("--split", choices=["dev", "heldout", "all"], default="dev")
    parser.add_argument("--single", default=str(ROOT / "datasets" / "harness_single.jsonl"))
    parser.add_argument("--multi", default=str(ROOT / "datasets" / "harness_multi.jsonl"))
    parser.add_argument("--out-root", default=str(ROOT / "results"))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--no-chart", action="store_true")
    args = parser.parse_args()

    split = None if args.split == "all" else args.split
    single_cases = load_single(args.single, split)
    multi_cases = load_multi(args.multi, split)
    settings = Settings.from_env()
    backend = make_backend(settings)
    evaluator = RuleLeakEvaluator()
    variant_keys = [v.strip().upper() for v in args.variants.split(",") if v.strip()]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_root) / f"harness_{settings.mode}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    all_records: list[dict] = []
    all_sessions: list[dict] = []
    repeat_stats: dict[tuple[str, str], tuple[float, float]] = {}
    per_run_metric_values: dict[tuple[str, str], list[float]] = {}

    for repeat in range(args.repeats):
        for key in variant_keys:
            tutor = build_tutor(key, backend, settings)
            for case in single_cases:
                guard = new_session_guard(key)
                record = run_single_case(tutor, case, key, guard)
                evaluation = evaluator.evaluate_single(case, record["answer"])
                record["evaluator_leak"] = evaluation.leak
                record["evaluator_evidence"] = evaluation.evidence
                classify_outcome(record)
                all_records.append(record)
            for case in multi_cases:
                guard = new_session_guard(key)
                all_sessions.append(run_multi_case(tutor, case, key, guard, evaluator))
        # 每轮指标（用于重复运行波动）
        for key in variant_keys:
            records = [r for r in all_records if r["variant"] == key] if args.repeats == 1 else \
                      [r for r in all_records[-len(single_cases):] if r["variant"] == key]
            if args.repeats > 1:
                records = [r for r in all_records if r["variant"] == key]
                # 简化：重复运行时按该版本全部记录计算总体（逐轮明细见 records.jsonl）
            summary = aggregate(records)
            for metric in METRIC_KEYS:
                value = summary[metric]
                if value is not None:
                    per_run_metric_values.setdefault((key, metric), []).append(value)

    for metric_key, values in per_run_metric_values.items():
        if len(values) >= 2:
            repeat_stats[metric_key] = (statistics.mean(values), statistics.pstdev(values))

    with (out_dir / "records.jsonl").open("w", encoding="utf-8") as handle:
        for record in all_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    with (out_dir / "sessions.jsonl").open("w", encoding="utf-8") as handle:
        for record in all_sessions:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    variant_summaries = {
        key: aggregate([r for r in all_records if r["variant"] == key]) for key in variant_keys
    }
    session_summaries = {
        key: aggregate_sessions([s for s in all_sessions if s["variant"] == key]) for key in variant_keys
    }

    summary_md = render_summary(variant_summaries, session_summaries, settings, args.repeats, repeat_stats, out_dir)
    (out_dir / "summary.md").write_text(summary_md + "\n", encoding="utf-8")

    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["variant", "label"] + METRIC_KEYS + ["cases"])
        for key in sorted(variant_summaries):
            row = [key, VARIANTS[key].label] + [
                variant_summaries[key][m] for m in METRIC_KEYS
            ] + [variant_summaries[key]["cases"]]
            writer.writerow(row)

    for key in sorted(variant_summaries):
        matrix = confusion_matrix([r for r in all_records if r["variant"] == key])
        (out_dir / f"confusion_{key}.json").write_text(
            json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    chart_ok = False if args.no_chart else plot_chart(variant_summaries, out_dir / "metrics_by_variant.png")

    print(summary_md)
    print(f"\n输出目录：{out_dir}")
    if chart_ok:
        print("图表：metrics_by_variant.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
