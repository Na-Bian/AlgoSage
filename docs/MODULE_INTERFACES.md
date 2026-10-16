# Module interface contracts (phase 1 and roadmap)

## Existing code (phase 1)

- `classify(TutorRequest) -> Classification`: provisional regex-based 5-way classifier (`knowledge`, `debug`, `hint`, `request_solution`, `jailbreak`).
- `decide(Classification, exam_mode: bool) -> PolicyDecision`: outputs max allowed help level (0–3).
- `ChatBackend.generate(messages: list[{role, content}]) -> LLMResult`: API-independent model adapter, returns model and token usage when available.
- `BasicOutputGuard.review(draft, decision, exam_mode) -> AuditResult`: simplistic markdown-code-length heuristic, **not leakproof**.
- `ExamGuardTutor.respond(TutorRequest, history) -> TutorResponse`: top-level orchestrator.

## Existing code (方案 B 前端迁移新增)

- `TutorService(settings, backend=None, allow_policy_override=None)`: 前端无关服务层（`examguard/service.py`），所有 UI 一律经由它调用 pipeline。
  - `new_context(session_id, exam_mode=None) -> ServiceContext`: 创建会话上下文；`exam_mode` 仅在 `EG_ALLOW_POLICY_SWITCH=true`（教师演示）时可偏离服务端配置，否则抛 `PermissionError`。
  - `get_context(session_id) / drop_context(session_id)`: 会话取用与清理（进程内、带上限的 LRU）。
  - `set_exam_mode(context, exam_mode)`: 教师开关专用；未授权时拒绝。
  - `chat(context, question, existing_code="") -> TutorResponse`: 单轮辅导，追加会话历史（保留最近 12 条角色消息，与 pipeline 口径一致）。
  - `TutorService.display_metadata(response) -> dict`: 前端可展示字段的**白名单**（intent/method/reason、help_level、policy_reason、approved、audit_reason、model、token 用量）；API Key、base URL、系统提示词一律不出现在其中。
- `chainlit_app.py`: 学生端 UI；`split_question_and_code(message) -> (question, code)` 把围栏代码块拆为已有代码；`render_audit_markdown(meta, latency_ms)` 渲染审计步骤。
- `eval_page.py`: 教师/开发者评测页（预留接口）；`load_cases(name)`、`run_cases(service, cases) -> summary`、`render_summary(summary, name)` 为纯函数，便于测试。正式 Harness 不在此实现。
- `run_app.py`: 绕过 chainlit CLI `nest_asyncio` 缺陷的启动器（Python 3.15 兼容）。

## Planned upgrades (not implemented)

- `IntentClassifier.classify(request, history) -> Classification`: LLM + rules; return confidence, evidence, and ambiguity handling.
- `HelpController.decide(classification, exam_config, state) -> PolicyDecision`: max help level and safe template; support per-question teacher policy.
- `SessionGuard.update(question_id, history, prior_state) -> RiskState`: track accumulated *semantic* disclosures across same question and closely-related restatements. Should not reset on superficial paraphrases.
- `LeakageJudge.review(question, answer, rubric, state) -> AuditResult`: content-aware leak detection (whole solution, proof, pseudocode, key steps); model-judge + human review, threshold tuning.
- `Harness.run(dataset, variant, model_config) -> ExperimentReport`: fixed evaluation data, variant flags, latency/token/accuracy, per-category failure records, confidence intervals if sample allows.

## Experimental methodology

Use same model ID, endpoint, decoding settings, version/snapshot where possible. Split development and held-out tests. Compare at least:

- A: prompt-only
- B: prompt + simple rules
- C: B + intent classification and graduated hints
- D: C + output review and session-level leakage guard

Metrics: benign helpfulness, prohibited-request block rate, answer-leak rate, attack success rate, false refusal rate, latency and tokens. Human label a sample to avoid treating classifier guesses as truth.

## Security limitations

The phase-one starter is intentionally *not* a robust exam policy. It will miss paraphrases, dispersed pseudocode, Chinese prose answer leakage, prompt injection embedded in code and multi-turn extraction. For actual controlled exams, use a teacher policy and human review. Never send real exams/student PII to remote providers without authorization.
