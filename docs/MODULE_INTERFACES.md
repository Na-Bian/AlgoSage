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

## Existing code (P1 新增：混合意图识别)

- `extract_risk_flags(question, existing_code="") -> tuple[str, ...]`（`examguard/intent_classifier.py`）：从不可信输入提取 Prompt Injection / 变相索取特征（`instruction_override`、`roleplay_request`、`system_prompt_probe`、`completion_disguise`、`translation_disguise`、`full_solution_request`、`output_encoding`、`untrusted_code_injection`）。**预留信号，不直接改变帮助等级**。
- `Classification` 数据契约新增 `confidence: Optional[float]` 与 `risk_flags: tuple[str, ...]`（带默认值，旧调用兼容）。
- `classify(request, history=None) -> Classification`：正则基线**判定逻辑冻结**（供 A/B/C/D 消融对照），但会填充 risk_flags。
- `HybridIntentClassifier(backend, settings).classify(request, history=None) -> Classification`（`examguard/hybrid_classifier.py`）：
  - LLM 输出严格 JSON（intent/confidence/reason/risk_flags）；解析失败、超时、字段非法一律回退正则（method=`regex-fallback(...)`）；
  - `merge_intents(regex_intent, llm_intent) -> Intent`：正则 knowledge 视为无信号采纳 LLM；LLM knowledge 保留正则正向命中；其余冲突取考试帮助等级**更低（更严格）**的一方，solution/knowledge 平级取 solution；
  - 通过 `EG_CLASSIFIER_MODE=hybrid` 启用（`Settings.classifier_mode`），由 `TutorService` 自动接线到 `ExamGuardTutor(classifier=...)`。
- `report_classifier.py --mode regex|hybrid --dataset intent_labeled_v0`：混淆矩阵 + 逐类指标 + 错判清单，输出 `results/classifier_*.md/.csv`；hybrid 报告要求 APP_MODE=api（拒绝把回退结果冒充混合结果）。
- 现状数据：正则基线在 42 例人工标注集上 accuracy=78.6%（见 `results/classifier_intent_labeled_v0_regex.md`）；hybrid 的真实远端评测待密钥后在 P4 Harness 中进行。

## Planned upgrades (not implemented)

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
