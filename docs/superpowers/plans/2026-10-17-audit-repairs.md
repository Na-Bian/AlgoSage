# ExamGuard audit repairs

Scope approved by the user's “开始修复”, following the audited design. Preserve the Chainlit interface and API compatibility where safe; no live model calls or new external services.

1. Regression tests first: turn the offline audit probes into explicit policy, unknown-audit, cumulative-candidate, concurrency/lifecycle, metrics and repeat-statistics tests. Observe failures before fixing.
2. Main work: one policy content contract for generator and judge; trusted overrides only tighten; validate classifier uncertainty/flags; strict Judge schema, whole-output review and fail-closed UNKNOWN; candidate preview before commit, information provenance and bounded risk state. Keep deterministic baselines for ablations.
3. Independent lifecycle work: service-level serialization and retained/persisted budgets, guarded reset, trusted question binding, unified CLI/Streamlit entrypoints and teacher data loading. Runtime state is separate from display history. Anonymous users have no authenticated cross-chat identity; document this limitation.
4. Independent evaluation work: explicit pending helpfulness/correctness review, provenance-aware proxy leakage checks, session success includes cumulative leakage, correct repeat IDs/statistics, isolated variants, full call/audit records and versioned manifests.
5. Main orchestration: per-turn call budget/deadline, per-role output limits, complete usage trace, zero-call deterministic denial. No unsafe drafts in student metadata.
6. Integrate and review all changes. Run targeted regressions, full offline tests, smoke, actual server Socket.IO E2E and offline Harness. Update docs and mark historical results as historical/proxy findings; do not invent new real-model effectiveness numbers.

Acceptance: no silent fail-open on Judge failure; no level-based “intercepted” success when a leak was delivered; preview the final candidate before recording disclosures; reconnect/eviction cannot erase the same identity's budget; same-budget requests serialize; every logical LLM call appears in usage accounting, with unknown usage preserved. Tests must continue to exercise ordinary conceptual explanation, complexity analysis, local debugging, independent questions and practice mode.
