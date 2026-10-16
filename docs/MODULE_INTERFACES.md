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

## Existing code (P4 新增：自动化 Harness 与消融)

- `harness/schema.py`：单轮/多轮 JSONL 契约与校验（`load_single` / `load_multi`，`split` 支持 dev/heldout 过滤；非法数据抛 `HarnessDataError` 拒绝运行）。
- `harness/variants.py`：`VARIANTS = A/B/C/D` 定义与 `build_tutor(variant, backend, settings)` 装配（A=常量分类+全 L3+NullOutputGuard；B=Phase-1 冻结基线；C=hybrid 分类；D=rules/layered 审核+SessionGuard+重生成）；`new_session_guard(variant)` 仅 D 返回实例。所有版本共用同一 backend。
- `harness/evaluator.py`：解耦评价器。`RuleLeakEvaluator.evaluate_single/evaluate_session`（独立正则实现 + 多轮累计拼装判定）；`LLMLeakEvaluator`（API 模式，按逐例 rubric 判定）。不以系统 approved 自评为准。
- `harness/metrics.py`：`classify_outcome`（helped/intercepted/leaked/wrongly_refused/attack_succeeded/failure_types 派生）、`aggregate`（口径见模块 docstring）、`confusion_matrix`、`aggregate_sessions`/`finalize_session_record`（多轮按会话计）。
- `harness/run_harness.py`：CLI 主入口（`--variants/--split/--repeats/--no-chart`）；输出 records/sessions JSONL、summary.md/csv、confusion_*.json、metrics_by_variant.png；mock 输出带水印；费用估算需 EG_PRICE_PER_MTOK_IN/OUT，未配置不估算。
- 数据：`datasets/harness_single.jsonl`（80 = dev60+heldout20）、`datasets/harness_multi.jsonl`（13 会话）。
- Mock 验证产物：`results/harness_mock_20261016_193847/`（流程验证，非实验结论）。

## Existing code (P3 新增：多轮累计泄漏防护)

- `SessionGuard`（`examguard/session_guard.py`）：每会话一份（`ServiceContext.session_guard` 由 `new_context` 创建）。
  - `before(request, classification, policy) -> (RiskState, RiskAdjustment|None)`：生成前按同题累计风险下调帮助等级上限（只降不升；`RiskAdjustment.reason` 可解释到已披露元素）。
  - `record(state, request, classification, policy, audit) -> {risk, disclosures}`：把**实际送达**的回答记入风险状态（互补新信息主导，重复披露 0.25 衰减）。
  - `trajectory(question_id="")` / `snapshot()`：风险轨迹与可序列化快照。
- `RiskState`：question_id、anchor_texts（历史问法锚点列表）、disclosed_elements、risk_score、turns、last_intent、recent_audits、history、version（to_dict/from_dict；v1 单锚点字段兼容）。
- 题目关联规则（`_match_key`）：显式 question_id；bigram Jaccard ≥0.45；关键词共享 ≥2；共享 1 且 J≥0.10；hint/solution 追问承接（锚点含关键词而当前省略、或双方均无关键词，状态近期为 hint/solution/debug）；无关键词的 solution 承接最近活跃状态。**误关联防护**：knowledge 意图不参与追问承接规则。
- 会话降级（按意图）：hint/solution 0.4→L2/0.6→L1/0.8→L0；debug 0.8→L2/0.9→L1；knowledge 下限 L1。
- `ExamGuardTutor.respond(request, history=None, session_guard=None)`；`TutorService.chat(..., question_id="")`；`TutorResponse.session_risk/session_disclosures`。
- 演示：`python demo_session_guard.py`（离线攻击链：算法选择→状态定义→伪装调试转移式→继续索要，第 4 轮 L2→L0 附原因）。

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

- **真实模型消融运行**（需 API 密钥）：同一 `python -m harness.run_harness` 命令在 APP_MODE=api 下执行；届时补 Judge 误判的人工抽样复核与 SessionGuard 关联规则的漏/误关联率统计。数据集可扩展至约 120 单轮 + 15 多轮（同一 schema）。

## Experimental methodology

Use same model ID, endpoint, decoding settings, version/snapshot where possible. Split development and held-out tests. Compare at least:

- A: prompt-only
- B: prompt + simple rules
- C: B + intent classification and graduated hints
- D: C + output review and session-level leakage guard

Metrics: benign helpfulness, prohibited-request block rate, answer-leak rate, attack success rate, false refusal rate, latency and tokens. Human label a sample to avoid treating classifier guesses as truth.

## Security limitations

The phase-one starter is intentionally *not* a robust exam policy. It will miss paraphrases, dispersed pseudocode, Chinese prose answer leakage, prompt injection embedded in code and multi-turn extraction. For actual controlled exams, use a teacher policy and human review. Never send real exams/student PII to remote providers without authorization.
