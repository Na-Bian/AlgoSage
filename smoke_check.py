"""Cheap dataset sanity check (not the final security evaluation Harness)."""
import json
from pathlib import Path
from examguard.intent_classifier import classify
from examguard.schemas import TutorRequest


def main():
    cases = Path(__file__).parent / 'datasets' / 'smoke_tests.jsonl'
    total = 0
    passed = 0
    with cases.open(encoding='utf-8') as f:
        for line in f:
            case = json.loads(line)
            predicted = classify(TutorRequest(case['question'])).intent.value
            is_ok = predicted == case['expected_intent']
            total += 1
            passed += int(is_ok)
            print(f"{'PASS' if is_ok else 'FAIL'} {case['id']}: {predicted}")
    print(f"Smoke classification: {passed}/{total}")
    if passed != total:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
