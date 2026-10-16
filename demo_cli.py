"""Run without optional UI dependencies: python demo_cli.py '什么是KMP？'"""
import argparse
from dataclasses import replace
from examguard.config import Settings
from examguard.llm import make_backend
from examguard.pipeline import ExamGuardTutor
from examguard.schemas import TutorRequest


def main() -> None:
    parser = argparse.ArgumentParser(description="ExamGuard first-stage CLI")
    parser.add_argument("question", nargs="?", default="KMP 算法为什么可以在线性时间匹配？")
    parser.add_argument("--mode", choices=["mock", "api"], default=None)
    parser.add_argument("--code", default="", help="Optional existing student code to analyze")
    args = parser.parse_args()
    config = Settings.from_env()
    if args.mode:
        config = replace(config, mode=args.mode)
    tutor = ExamGuardTutor(make_backend(config), config)
    result = tutor.respond(TutorRequest(args.question, args.code, config.exam_mode))
    print(f"model={result.model} intent={result.classification.intent.value} "
          f"help_level={result.decision.max_help_level} audit={result.audit.reason}")
    print(result.answer)
    print(f"usage(input/output)={result.input_tokens}/{result.output_tokens}")


if __name__ == "__main__":
    main()
