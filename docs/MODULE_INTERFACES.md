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

## Existing code (P2 新增：分级辅助与输出安全审核)

- `ExamPolicy`（`examguard/exam_policy.py`）：可信服务端策略配置。`from_file`/`from_dict` 严格校验（fail closed：未知意图、等级越界、按题覆盖比默认更宽松都直接抛 `PolicyConfigError`）；`question_overrides` 只允许**收紧或持平**；`max_regenerations`（0..2）为重生成预算。`config/exam_policy.json` 为示例（与内嵌默认一致 + q-dp-easy 覆盖示例）。
- `policy.decide(result, exam_mode, exam_policy=None, question_id="")`：等级来源解耦到 `ExamPolicy`；缺省内嵌默认与第一阶段硬编码**逐项一致**（旧测试兼容）。
- `TutorRequest.question_id`、`AuditResult.method/categories`、`TutorResponse.regenerations`：新增默认字段。
- `RuleLeakageScreener`（`examguard/leakage_guard.py`，method=`rules-v1`）：规则预筛。在旧检查之上新增：状态转移式（L≤2 拦截）、证明收尾标记（L≤1）、完整解答自称（任意级，带否定排除）、编号算法步骤（≥3 步且 ≥2 算法动词，L≤2）。**误拒控制**：复杂度递推 T(n)/O()、概念编号列表、L3 局部小代码段放行。
- `LLMLeakageJudge`（method=`judge-v1`）：独立 JSON 判定 leaked/categories；不可用（非 API、超时、解析失败）返回 None。
- `LayeredOutputGuard`：规则先筛（拦截则短路，不耗 Judge 调用）→ Judge 复核；Judge 不可用降级规则层结论并标注 `rules-v1(judge_unavailable)`。
- `make_output_guard(settings, backend)`（pipeline）：`EG_OUTPUT_GUARD=basic|rules|layered` 三档，`basic` 保留第一阶段行为供消融。
- `ExamGuardTutor.respond`：有限次重生成循环——审核失败按策略预算重生成（重生成提示含拦截原因），耗尽则替换为确定性保守回退文案；**只有审核通过的回答（或保守回退）会发给学生**。
- `TutorService`：按 `EG_POLICY_FILE` 加载策略（非法即构造失败）；`display_metadata` 白名单新增 `audit_method/audit_categories/regenerations`。

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

- `SessionGuard.update(question_id, history, prior_state) -> RiskState`: track accumulated *semantic* disclosures across same question and closely-related restatements. Should not reset on superficial paraphrases.（P2 的单轮审核不含历史合并检测。）
- `Harness.run(dataset, variant, model_config) -> ExperimentReport`: fixed evaluation data, variant flags, latency/token/accuracy, per-category failure records, confidence intervals if sample allows.（Judge 误判率评估、真实远端 hybrid/layered 评测在此阶段完成；模型自评不当作真值。）

## Experimental methodology

Use same model ID, endpoint, decoding settings, version/snapshot where possible. Split development and held-out tests. Compare at least:

- A: prompt-only
- B: prompt + simple rules
- C: B + intent classification and graduated hints
- D: C + output review and session-level leakage guard

Metrics: benign helpfulness, prohibited-request block rate, answer-leak rate, attack success rate, false refusal rate, latency and tokens. Human label a sample to avoid treating classifier guesses as truth.

## Security limitations

The phase-one starter is intentionally *not* a robust exam policy. It will miss paraphrases, dispersed pseudocode, Chinese prose answer leakage, prompt injection embedded in code and multi-turn extraction. For actual controlled exams, use a teacher policy and human review. Never send real exams/student PII to remote providers without authorization.
