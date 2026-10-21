"""P1 分类器评测报告：混淆矩阵、逐类指标与典型错判清单。

运行（真实执行后输出到 results/，报告中数字均为实际运行结果）：
    python report_classifier.py                        # 正则基线（离线可用）
    python report_classifier.py --mode hybrid           # 混合分类（需 APP_MODE=api）

说明：
- 标签来自人工标注（datasets/intent_labeled_v0.jsonl 的 expected_intent）；
- hybrid 模式需要真实远端模型；APP_MODE=mock 时会拒绝运行（拒绝把回退
  结果冒充混合分类结果）；
- 本脚本是开发期分析工具；正式的安全评测口径（拦截率/泄漏率等）在 P4 Harness。
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from algosage.config import Settings
from algosage.hybrid_classifier import HybridIntentClassifier
from algosage.intent_classifier import classify
from algosage.llm import make_backend
from algosage.schemas import Intent, TutorRequest

INTENTS = [i.value for i in Intent]
DATASET_DIR = Path(__file__).parent / "datasets"
OUT_DIR = Path(__file__).parent / "results"


def load_cases(name: str) -> list[dict]:
    path = DATASET_DIR / (name if name.endswith(".jsonl") else name + ".jsonl")
    cases = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                cases.append(json.loads(line))
    return cases


def run(classifier, cases: list[dict]) -> list[dict]:
    rows = []
    for index, case in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {case.get('id')} ...", flush=True)
        request = TutorRequest(
            question=case.get("question", ""),
            existing_code=case.get("existing_code", ""),
            exam_mode=True,
        )
        result = classifier(request)
        rows.append({
            "id": case.get("id"),
            "note": case.get("note", ""),
            "gold": case.get("expected_intent"),
            "pred": result.intent.value,
            "method": result.method,
            "flags": ",".join(result.risk_flags) or "-",
            "question": case.get("question", ""),
        })
    return rows


def confusion(rows: list[dict]) -> dict[str, dict[str, int]]:
    matrix = {g: {p: 0 for p in INTENTS} for g in INTENTS}
    for row in rows:
        matrix[row["gold"]][row["pred"]] += 1
    return matrix


def per_intent_metrics(matrix: dict[str, dict[str, int]]) -> list[dict]:
    metrics = []
    for intent in INTENTS:
        tp = matrix[intent][intent]
        fp = sum(matrix[g][intent] for g in INTENTS if g != intent)
        fn = sum(matrix[intent][p] for p in INTENTS if p != intent)
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        metrics.append({
            "intent": intent, "support": tp + fn,
            "precision": precision, "recall": recall,
            "f1": (2 * precision * recall / (precision + recall))
                  if precision and recall else None,
        })
    return metrics


def fmt(value) -> str:
    return f"{value:.3f}" if isinstance(value, float) else ("—" if value is None else str(value))


def render_report(mode: str, dataset: str, rows: list[dict]) -> str:
    matrix = confusion(rows)
    metrics = per_intent_metrics(matrix)
    correct = sum(1 for r in rows if r["gold"] == r["pred"])
    lines = [
        f"# 意图分类评测报告（{mode} 模式，数据集 {dataset}）",
        "",
        f"- 样本数：{len(rows)}；分类一致：{correct}（accuracy={correct / len(rows):.1%}）",
        "- 标签：人工标注；运行环境：本仓库当前解释器（数字为实际运行结果）",
        "",
        "## 混淆矩阵（行=标注 gold，列=预测 pred）",
        "",
        "| gold\\pred | " + " | ".join(INTENTS) + " |",
        "| --- | " + " | ".join(["---"] * len(INTENTS)) + " |",
    ]
    for gold in INTENTS:
        lines.append(f"| {gold} | " + " | ".join(str(matrix[gold][p]) for p in INTENTS) + " |")
    lines += [
        "",
        "## 逐类指标",
        "",
        "| intent | support | precision | recall | F1 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for m in metrics:
        lines.append(f"| {m['intent']} | {m['support']} | {fmt(m['precision'])} | {fmt(m['recall'])} | {fmt(m['f1'])} |")
    errors = [r for r in rows if r["gold"] != r["pred"]]
    lines += [
        "",
        f"## 典型错判（{len(errors)} 条）",
        "",
        "| id | gold | pred | note | 问题 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in errors:
        question = r["question"].replace("|", "\\|")
        lines.append(f"| {r['id']} | {r['gold']} | {r['pred']} | {r['note']} | {question} |")
    lines += [
        "",
        "## 备注",
        "",
        "- 正则基线的五类判定自第一阶段冻结，错判集中在改写/口语化表达，",
        "  这是引入混合分类器（规则+LLM）的动机；",
        "- `risk_flags` 列在 CSV 中给出，供 Prompt Injection 特征分析。",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="AlgoSage 意图分类评测报告")
    parser.add_argument("--dataset", default="intent_labeled_v0")
    parser.add_argument("--mode", choices=["regex", "hybrid"], default="regex")
    args = parser.parse_args()

    settings = Settings.from_env()
    if args.mode == "hybrid":
        if settings.mode != "api":
            print("hybrid 模式需要 APP_MODE=api 与可用密钥；mock 下只会得到回退结果，拒绝生成误导性报告。")
            return 2
        backend = make_backend(settings)
        classifier = HybridIntentClassifier(backend, settings).classify
        label = f"hybrid-{settings.model}".replace("/", "-")
    else:
        classifier = classify
        label = "regex"

    cases = load_cases(args.dataset)
    rows = run(classifier, cases)
    report = render_report(label, args.dataset, rows)
    OUT_DIR.mkdir(exist_ok=True)
    md_path = OUT_DIR / f"classifier_{args.dataset}_{label}.md"
    csv_path = OUT_DIR / f"classifier_{args.dataset}_{label}.csv"
    md_path.write_text(report + "\n", encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(report)
    print(f"\n已写出：{md_path} 与 {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
