# Module contracts — audit repair version (2026-10-17)

`TutorService` is the common Chainlit/Streamlit/CLI boundary. It owns trusted settings/policy and server question binding, retained exam/principal budgets and whole-turn serialization. `new_context(session_id, exam_mode=None, principal_id=None)` accepts only server-authenticated principal identities; anonymous sessions cannot be securely linked across new IDs. `drop_context` clears disposable history only; `reset_security_state` requires the teacher demo permission.

`RiskStateStore` uses memory in explicitly constructed default test settings, or SQLite under `EG_STATE_FILE` for runtime settings. Its transaction covers risk read through response/history update and snapshot commit; storage failure prevents response delivery. It refuses capacity overflow rather than forgetting security budgets.

`ExamGuardTutor.respond(request, history=None, session_guard=None)` opens a ContextVar invocation ledger, performs deterministic injection denial before remote work, classifies, applies policy, generates bounded candidates and audits them. With a SessionGuard it calls `preview` before committing approved content via `record`.

`decide` consumes classification flags and trusted ExamPolicy. `content_contract` is shared by generator and Judge. Per-question overrides may only tighten the effective global limit. L1/L2 do not gain a core-solution exemption from a knowledge/hint label; L3 requires existing substantive code for code repair. Practice still does not authorize complete programs.

`HybridIntentClassifier(backend, settings, policy_config=None)` preserves the regex classifier as a component baseline, validates JSON and self-reported confidence, uses effective policy limits for conflicts, and marks fallback as `uncertain`. Confidence is not a calibrated probability.

`RuleLeakageScreener` performs static v2 checks (including short complete Python functions). `LLMLeakageJudge` inspects the complete bounded candidate plus disclosed history, request and existing code. `LayeredOutputGuard` returns UNKNOWN on unavailable/invalid/unconfident semantic review; the candidate is replaced with a safe message. The regex and semantic checks remain heuristic.

`AuditResult` includes `status=pass|block|unknown`; only pass can approve. `TutorResponse` usage totals all logical calls, preserving unknown token values. `call_trace` and `audit_attempts` are developer evidence and must never enter `TutorService.display_metadata`, which is the student whitelist. Public metadata includes audit_status but no draft, raw Judge response, key, endpoint or system prompt.

`SessionGuard.preview` checks cumulative state/transition/algorithm disclosure and bounded code fragments before delivery. `record` ignores blocked/unknown drafts and student-supplied exact code, tracks content fingerprints, approved disclosed excerpts and interpretable risk. `RiskState.to_dict/from_dict` and snapshots deep-copy mutable audit data; serialization is backward compatible with older snapshots.

`invocation.generate` records stage, outcome, model, timing, token usage and developer-only classification/Judge response text. The production adapter accepts per-role output limits and remaining deadlines; hidden SDK retries are disabled. A custom synchronous backend can only be checked before/after returning, not forcibly interrupted by Python.

`harness` has an independent proxy evaluator, explicit pending quality review, repeat IDs/statistics, component controls and version/config hashes. Proxy false means “no proxy signal”, not a semantic proof. Metrics must state reviewed denominators. Old experiment results are historical; see README and results/README.md.
