# Module Contracts

本文档按模块主题描述系统内部契约；实现细节以代码与测试为准。

`TutorService` is the common Chainlit/Streamlit/CLI boundary. It owns trusted settings/policy and server question binding, retained exam/principal budgets and whole-turn serialization. `new_context(session_id, exam_mode=None, principal_id=None)` accepts only server-authenticated principal identities; anonymous sessions cannot be securely linked across new IDs. `drop_context` clears disposable history only; `reset_security_state` requires the teacher demo permission.

`RiskStateStore` uses memory in explicitly constructed default test settings, or SQLite under `SAGE_STATE_FILE` for runtime settings. A per-principal OS file lock covers risk read through model execution and snapshot commit; SQLite transactions stay short. Different principals do not wait on one global writer, while the same trusted principal remains serialized across service instances. Storage failure prevents response delivery. It refuses capacity overflow rather than forgetting security budgets.

`AlgoSageTutor.respond(request, history=None, session_guard=None)` opens a ContextVar invocation ledger, performs deterministic injection denial before remote work, classifies, applies policy, generates bounded candidates and audits them. With a SessionGuard it calls `preview` before committing approved content via `record`.

`decide` consumes classification flags and trusted ExamPolicy. `content_contract` is shared by generator and Judge. Per-question overrides may only tighten the effective global limit. L1/L2 do not gain a core-solution exemption from a knowledge/hint label; L3 requires existing substantive code for code repair. Practice still does not authorize complete programs.

`HybridIntentClassifier(backend, settings, policy_config=None)` preserves the regex classifier as a component baseline, validates JSON and self-reported confidence, uses effective policy limits for conflicts, and marks fallback as `uncertain`. Confidence is not a calibrated probability.

`RuleLeakageScreener` performs static v2 checks (including short complete Python functions). `LLMLeakageJudge` inspects the complete bounded candidate plus disclosed history, request and existing code. A `leaked=false` verdict tolerates a stray `other` category, while contradictory real leak categories, missing disclosure schema or fabricated evidence remain invalid. Malformed, low-confidence or incomplete output gets one compact repair request before `LayeredOutputGuard` can return UNKNOWN. The regex and semantic checks remain heuristic. `solution_semantics` extracts concrete solution bindings and applies a shared element budget. Judge v3 must include `disclosures` with valid element/source values and exact candidate/history quotes; invalid evidence for a negative verdict returns UNKNOWN, while a positive leak verdict with unverified evidence remains blocked.

`LLMAnswerQualityReviewer` runs after safety approval for knowledge, hint and debug answers. It checks high-confidence factual, arithmetic, internal-consistency and requested-scope errors with exact quotes, supplemented by deterministic checks for common asymptotic-notation, hash/encryption, Base64 example and over-expansion errors. Its own timeout or malformed JSON is non-blocking; a validated issue triggers the existing bounded regeneration path and never reaches the student as a raw judge response.

`AuditResult` includes developer-only `disclosures` and `evidence` (short source quotes), plus `status=pass|block|unknown`; only pass can approve. `TutorResponse` usage totals all logical calls, preserving unknown token values. `call_trace` and `audit_attempts` are developer evidence and must never enter `TutorService.display_metadata`, which is the student whitelist. Public metadata includes audit_status but no draft, raw Judge response, key, endpoint or system prompt.

`SessionGuard.preview` checks cumulative state/transition/algorithm disclosure and bounded code fragments before delivery. `record` ignores blocked/unknown drafts and student-supplied exact code, tracks content fingerprints, approved disclosed excerpts and interpretable risk. `RiskState.to_dict/from_dict` and snapshots deep-copy mutable audit data; serialization is backward compatible with older snapshots.

`invocation.generate` records stage, outcome, model, timing, token usage and developer-only classification/Judge response text. The production adapter accepts per-role output limits and remaining deadlines; hidden SDK retries are disabled. A custom synchronous backend can only be checked before/after returning, not forcibly interrupted by Python.

`harness` has an independent proxy evaluator, explicit pending quality review, repeat IDs/statistics, component controls and version/config hashes. Proxy false means “no proxy signal”, not a semantic proof. Metrics must state reviewed denominators. Old experiment results are historical; see README and results/README.md.

## Builtin Policy Models (Intent Classifier, Judge, Titles, Disclosure Extraction)

Auxiliary tasks run on server-owned backends, decoupled from the student-selected BYOK model. `builtin_classifier_backend()` resolves `CLASSIFIER_MODEL`/`CLASSIFIER_BASE_URL`/`CLASSIFIER_API_KEY` (falling back to the primary settings) and serves the hybrid intent classifier's LLM tier plus `disclosure_extractor.extract_disclosures_llm`. `builtin_judge_backend()` resolves `JUDGE_*` (falling back to `CLASSIFIER_*`, then primary) and backs `LLMLeakageJudge`/`LLMAnswerQualityReviewer` via the optional `judge_backend` argument of `make_output_guard`. `TITLE_*` backs `generate_title`. All auxiliary calls are short, non-streaming, and never enter the per-turn invocation ledger except titles (own ledger).

`extract_disclosures_llm(backend, answer_text, timeout)` extracts solution elements from **delivered** answers as a supplement to the regex floor: allowed-element whitelist, verbatim quote validation (whitespace-tolerant), empty-on-failure semantics. `SessionGuard.record_extra(state, elements)` merges LLM-only elements into the persistent risk budget — additive only; regex-recorded elements are never dropped or re-scored. Disclosure extraction runs on a daemon thread after delivery; any failure is a silent no-op.

The stop handler only arms the abort event when the session's current task is still running; frontend unload emits `stop` as cleanup, and turns already in the audit/delivery stage complete normally. `ChatDataLayer` treats `chat_settings.model_params` (student help-level cap) as server-owned state: `_safe_thread_metadata` strips it from browser metadata updates, `_merged_thread_metadata` restores it from the raw existing record, and `load_thread_settings` reads it back — the cap survives message-driven metadata writes and page refreshes, and cannot be forged from the browser.

The provider-manager dialog appends to `document.body` and explicitly clears `inert` on its subtree: mobile settings drawers are modal Radix dialogs that mark outside elements inert, which previously made the dialog untappable. Do not portal the dialog into the drawer subtree — Radix Content transforms change the containing block of the fixed backdrop and clip the fullscreen overlay.

## Semantic Disclosure Budget

RiskState v4 adds initialization/traversal elements while retaining v1-v3 deserialization. Same-problem preview checks complementary semantic elements before delivery. Explicit follow-ups can retain the prior implicit question even with knowledge intent; distinct named topics remain separate. Committed Judge-only evidence contributes to the persistent budget. Complete approved prose is retained up to the 20,000-character context budget, including text following a refusal prefix. Rejected drafts never enter delivered history or spend disclosure budget. Association remains heuristic; trusted problem identity and independent human evaluation remain requirements for stronger deployments.
`attachments.prepare_attachment_elements` is the Chainlit-to-service boundary. It validates the server-side uploaded path and byte limits, extracts PDF/DOCX/PPTX/XLSX/text/code content, and normalises images to a bounded PNG/JPEG data URL. `AttachmentPayload` carries only the display name, MIME, extracted text and image data URL. The pipeline sends images as OpenAI-compatible `image_url` content parts while all attachment names/text enter classification and review as untrusted data. `ChatDataLayer.save_backend_history` preserves at most six validated image data URLs per user message for follow-up turns; it drops malformed or non-image entries.

## Visual Context

`visual_context.select_images(attachments, history)` 选取当前附件及最近12条用户/助教消息中的图片，当前附件优先，总计最多6张，按规范化图片数据URL去重；只接受服务端图片数据URL，不接受远程地址。每张图携带稳定来源编号，并保留最近12条匹配用户消息的原始附图说明，每条最多1000字符，截断时明确提示。

`with_images(text, images)` 生成兼容 Chat Completions 的文本/图片内容块；同一原始说明在一次请求中仅输出一次。分类、生成、安全Judge、JSON修正重审和知识复核均使用同一选择。图片和说明始终是不可信数据，不改变可信政策。明确索取图片数值或标记而缺少图片时返回重新上传提示；一般概念问答不受影响。

`LayeredOutputGuard.review(..., history=None, images=())`、`LLMLeakageJudge.review(..., history=None, images=())` 和 `LLMAnswerQualityReviewer.review(..., images=())` 保持纯文本调用兼容。知识复核的短回答豁免不适用于图片回答；模型看图失败仍可能触发原有UNKNOWN/质量复核降级路径，不能由此宣称视觉判断绝对可靠。
