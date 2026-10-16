# ExamGuard: restricted algorithm exam tutor

Course project for **《算法设计与分析》**: lightweight open-weight LLM accessed via a hosted OpenAI-compatible API; explicit help policy and a future evaluation harness. **Prototype, not a production-safe exam system.**

Frontend track: **方案 B —— Chainlit**（学生端 `chainlit_app.py` + 教师评测页 `eval_page.py`），原 Streamlit 界面 `app.py` 保留为回退方案。业务逻辑（意图分类、帮助等级、模型调用、输出审核）全部位于 `examguard/` 包，与前端解耦。

## Files

```text
chainlit_app.py             方案 B 学生端 Chainlit UI（聊天、代码块、会话、帮助等级展示）
eval_page.py                教师/开发者评测页（预留接口，与正式 Harness 不同）
run_app.py                  Chainlit 启动器（见下方“重要：启动方式”）
app.py                      旧 Streamlit UI（回退方案）
demo_cli.py                 dependency-free offline CLI (mock)
examguard/config.py         settings via env
examguard/schemas.py        stable request/response structures
examguard/intent_classifier.py   5-way regex baseline + risk-flag extraction
examguard/hybrid_classifier.py   P1 规则+LLM 混合分类器（EG_CLASSIFIER_MODE=hybrid）
examguard/policy.py         0–3 help-level policy and prompt builder
examguard/llm.py            mock + remote API adapters
examguard/output_guard.py   *basic* demo audit
examguard/pipeline.py       orchestration
examguard/service.py        TutorService：前端无关服务层（会话、服务端策略、展示白名单）
.chainlit/config.toml       Chainlit 配置（浅色主题、custom_css、关闭文件上传）
public/custom.css           DeepSeek 风格轻量主题定制
e2e_chainlit_check.py       E2E 冒烟：真实起服 + socket.io 完整会话
datasets/smoke_tests.jsonl  5 starter categories for labeling
datasets/intent_labeled_v0.jsonl  P1 42 例人工标注分类测试集（含歧义/攻击改写）
report_classifier.py        P1 分类评测：混淆矩阵 + 逐类指标 + 错判清单 -> results/
smoke_check.py              lightweight static sanity check
README.md; docs/MODULE_INTERFACES.md; tests/
```

## Quickstart (Python 3.10+)

### 1. Offline quick test: no API key required

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt

python -m unittest discover -s tests -v   # 单元 + 集成测试（含 Chainlit 模块用例）
python smoke_check.py
python demo_cli.py '什么是前缀函数？'
```

The CLI intentionally prints `【模拟输出】`; no model is called.

### 2. Web demo (Chainlit, mock)

> **重要：启动方式。** 本仓库用 `run_app.py` 而不是 `chainlit run`。
> 原因：chainlit 2.11 CLI 依赖 `nest_asyncio`，在 Python 3.15 上会破坏
> anyio 的线程卸载，导致 `/assets/*.js`、`/public/custom.css` 等静态资源 500、
> 页面白屏（已在本机实测）。`run_app.py` 按 chainlit CLI 相同的初始化序列装配，
> 但以原生 uvicorn 启动，绕开该缺陷；若你的 Python ≤3.13，`chainlit run chainlit_app.py`
> 通常也可用。

```bash
cp .env.example .env            # 默认 APP_MODE=mock，无需密钥
python run_app.py               # 学生端，默认 http://127.0.0.1:8000
python run_app.py eval_page --port 8081   # 教师/开发者评测页
```

学生端功能：

- 聊天 + Markdown 代码块渲染；把已有代码放在 ``` 围栏块中一起发送即可（仅文本分析，不执行）；
- 每条回答下方可展开「🛡️ 安全审计与策略」步骤：意图、帮助等级上限（L0–L3）、审核结论、模型与 Token 用量；
- 会话管理：Chainlit 原生“新对话”会触发后端 `on_chat_start`/`on_chat_end`，`TutorService` 按会话隔离历史；
- 考试模式由服务端 `EXAM_MODE` 决定并只读展示；教师演示可 `EG_ALLOW_POLICY_SWITCH=true python run_app.py` 后在界面切换（正常部署不要开启）。

评测页命令：`info`（配置摘要，不含密钥）、`run smoke`（跑 `datasets/smoke_tests.jsonl`，API 模式需追加 `confirm-api` 确认以控制成本）。

E2E 冒烟（可选，需要 venv）：

```bash
python e2e_chainlit_check.py    # 起真实服务 + socket.io 客户端完整会话
```

### 3. Real hosted LLM API

Edit your local `.env`:

```dotenv
APP_MODE=api
LLM_API_KEY=PASTE_YOUR_OWN_KEY_HERE
LLM_BASE_URL=https://api.siliconflow.cn/v1
LLM_MODEL=Qwen/Qwen3-8B
LLM_TOKEN_PARAMETER=max_tokens
```

Run:

```bash
python demo_cli.py --mode api '解释 KMP 的前缀函数有什么作用'
python run_app.py
```

If your provider does not accept `max_tokens`, set `LLM_TOKEN_PARAMETER=max_completion_tokens`. For Alibaba Cloud Model Studio, use the matching region + workspace-specific `base_url` and a model ID available in that region; do **not** paste an example `{WorkspaceId}` literally. For OpenAI, set `LLM_BASE_URL=https://api.openai.com/v1`, choose an available model, and adjust token parameter to suit the model. API availability and fees depend on your account and provider.

`LLM_API_KEY` is used exclusively server-side (read by `examguard/config.py` inside the server process; the browser only ever receives whitelisted display metadata from `TutorService.display_metadata`). `.env` is in `.gitignore`. Never push any real API keys to Git or include them in a zip submission. Config changes to `.env` may require restarting the app.

### 4. Streamlit fallback

```bash
streamlit run app.py
```

`app.py` 未改动；在迁移验证期内保留作为回退。注意其侧栏“考试限制模式”开关是演示用途——真实考试场景的策略控制以 Chainlit 版的 `EG_ALLOW_POLICY_SWITCH` 机制为准。

## Architecture (方案 B)

```text
浏览器 (Chainlit React UI)
  └─ websocket ──> chainlit_app.py / eval_page.py   （只做展示与输入拆分）
        └─> examguard.service.TutorService          （前端无关服务层）
              ├─ 会话上下文 / 服务端考试策略 / 展示白名单
              └─> ExamGuardTutor.respond            （原 pipeline 不变）
                    ├─ classify → decide → system_prompt
                    ├─ ChatBackend.generate (mock / OpenAI-compatible)
                    └─ BasicOutputGuard.review
```

安全约束：前端不持有 API Key；`display_metadata()` 是唯一面向前端的元数据出口（白名单字段）；
学生无法从界面降低考试限制（默认无开关，`PermissionError` 兜底）。

## Stage-one acceptance

- Five example input categories are classified (heuristically, not reliably in adversarial cases).
- Exam policy maps category to one of four help levels.
- Mock mode runs and tests pass offline (58 tests: 10 原有 + 48 新增服务层/前端/分类器集成).
- Real API can be enabled with `.env` credentials; model metadata and token counts (if present) are returned.
- Basic code-block output audit can trigger a safe fallback.
- Chainlit 学生端与教师评测页可运行；E2E 冒烟覆盖欢迎消息、正常问答、越权拒答与密钥不泄漏.

## P1：混合输入意图识别（已交付）

- 正则基线**冻结**（`classify()` 判定逻辑不变），新增 `extract_risk_flags()` 提取
  Prompt Injection / 变相索取特征（`instruction_override`、`roleplay_request`、
  `system_prompt_probe`、`completion_disguise`、`translation_disguise` 等，含已有代码中的注入标记
  `untrusted_code_injection`）；特征为预留信号，不改变帮助等级。
- `HybridIntentClassifier`（`EG_CLASSIFIER_MODE=hybrid` 启用）：规则 + LLM 严格 JSON 输出；
  超时 / 解析失败 / 字段非法一律回退正则（离线也可安全运行）；
  冲突合并策略保守可解释——正则 knowledge 视为无信号，其余冲突取考试帮助等级更严格的一方
  （伪装成调试的完整答案索取不会按 debug 放行）。
- 分类评测（真实运行数据）：

```bash
python report_classifier.py          # 正则基线（离线）
python report_classifier.py --mode hybrid   # 需 APP_MODE=api
```

  正则基线在 42 例人工标注集上 **accuracy=78.6%（33/42）**；9 条错判全部为改写/伪装类
  （8 条落入 knowledge 兜底、1 条 jailbreak 降级为 request_solution——后者仍是保守方向），
  详见 `results/classifier_intent_labeled_v0_regex.md` 与同名 CSV。
  hybrid 模式的真实远端评测待 API 密钥后在 P4 Harness 统一进行（本仓库拒绝在 mock 下
  生成冒充 hybrid 的报告）。

## P2：分级辅助与输出安全审核（已交付）

- **策略解耦**：帮助等级来源改为可信 `ExamPolicy` 配置（`examguard/exam_policy.py`）。
  服务端文件 `EG_POLICY_FILE=config/exam_policy.json`（可选），支持按 `question_id`
  **只收紧**的等级覆盖与重生成预算；文件非法时启动直接失败（fail closed）。
  缺省内嵌映射与第一阶段硬编码逐项一致，旧测试与消融基线可复现。
- **输出审核分层**（`EG_OUTPUT_GUARD=basic|rules|layered`，默认 rules）：
  - `basic`：第一阶段冻结基线（消融对照）；
  - `rules`（`RuleLeakageScreener`）：新增状态转移式（L≤2）、证明收尾标记（L≤1）、
    完整解答自称（任意级，否定词排除）、编号算法步骤（≥3 步且含算法动词，L≤2）检测；
    **误拒控制**：T(n)/O() 复杂度递推、概念性编号列表、L3 局部小代码段均放行；
  - `layered`：规则先筛（拦截即短路，不耗 Judge 调用）→ 独立 `LLMLeakageJudge`
    JSON 复核；Judge 不可用（非 API/超时/解析失败）降级为规则层结论并标注
    `rules-v1(judge_unavailable)`，不静默放行也不硬拒。
- **有限次重生成**：审核未通过按策略预算（默认 1 次）携拦截原因重生成；耗尽后替换为
  确定性保守回退文案。只有审核通过的回答（或保守回退）会发给学生。
- 审计元数据新增 `audit_method`/`audit_categories`/`regenerations`（Chainlit 审计步骤
  可视展示）。
- **边界**：多轮累计泄漏检测（与历史提示合并判定）属 P3；Judge 在真实模型上的
  误判率评估属 P4（模型自评不当作真值）。

## P3：多轮累计泄漏防护（已交付）

- **SessionGuard**（`examguard/session_guard.py`，每个聊天会话一份，`new_context` 自动创建）：
  - 以显式 `question_id` 为主、相近问题关联为辅定位同一道题；关联规则 7 条（关键
    词共享 + bigram Jaccard + 意图连续性），锚点保留**历史问法列表**，题目改写、
    省略关键词的追问、"那把完整代码给我吧"式承接都不会重置风险；
  - `RiskState` 可版本化序列化（`to_dict/from_dict`，v1 单锚点字段兼容）；
  - **互补性风险累计**：新增披露元素 × 权重 × 新颖度（重复披露仅按 0.25 衰减
    因子计入），而非按轮数/关键词次数累计。元素权重：算法选择 0.15、状态定义
    0.25、转移式 0.35、关键步骤 0.40、伪代码 0.50、完整代码/证明 1.0、局部修
    复 0.10、复杂度/概念 0.05；
  - **按意图分级的会话降级**：hint/solution 自风险 0.4 起阶梯降级（0.4→L2、
    0.6→L1、0.8→L0）；debug 仅极高风险降级（内容审核始终兜底，学生自己代码的
    局部诊断不被先前提示过度牵连）；knowledge 下限 L1（不因会话风险完全拒答
    概念问题）。降级原因写入 `PolicyDecision.reason`，可解释到具体已披露元素；
  - 风险轨迹与状态快照：`guard.trajectory()` / `guard.snapshot()`；演示：
    `python demo_session_guard.py`（离线 mock 完整攻击链：算法选择→状态定义→
    伪装调试取得转移式→继续索要，第 4 轮 L2→L0 并给出原因）。
- **误关联防护**（有测试）：KMP 讨论后转 Kruskal、考题讨论后的普通知识问题
  不受高风险牵连；会话之间状态完全隔离。
- **已知边界**：纯意译改写（无共享关键词、字面相似度低）在无 question_id 时
  可能漏关联；规则 7 的"无关键词索答承接"可能轻微过度关联——均在报告有效
  性威胁中说明，P4 用标注数据量化。

## P4：自动化 Harness 与消融实验（框架已交付，真实模型评测待密钥）

- **测试数据**（结构化 JSONL，带 schema 校验、fail closed）：
  - `datasets/harness_single.jsonl`：80 例（dev 60 = k14/d12/h12/s12/j10；heldout 20），
    字段含 case_id/split/intent_gold/is_allowed/question_id/prompt/existing_code/
    expected_help_level/leakage_rubric/attack_type，覆盖正常问法、歧义、攻击改写
    （角色扮演/规则覆盖/翻译/伪装补全/编码套取）；
  - `datasets/harness_multi.jsonl`：13 组多轮会话（良性 5 / 攻击链 6 / 混合 2，
    dev 11 + heldout 2），含分段拆解、伪装调试逐轮套取、无关键词渐进链等。
- **消融版本**（`harness/variants.py`，共用同一 backend/模型/解码参数）：
  A 仅考试 System Prompt（常量分类+全 L3+直通审核）→ B +正则分类+默认策略+basic 审核
  （Phase-1 冻结基线）→ C +混合意图分类（mock 下回退≈B，报告中标注）→
  D +rules/layered 审核+SessionGuard+有限重生成。
- **评价器与被测系统解耦**（`harness/evaluator.py`）：`RuleLeakEvaluator` 只看最终
  送达文本与 rubric（独立实现，不以系统 approved 自评为准）；多轮含"累计披露是否
  足以拼装"判定；`LLMLeakEvaluator`（API 模式）按逐例 rubric 语义复核。
- **指标口径**（`harness/metrics.py`，分母写入汇总表头）：合法辅助完成率、误拒率、
  拦截率、泄漏率、越狱成功率（多轮按会话计，攻击成功仅统计 jailbreak 类得手）、
  意图准确率与混淆矩阵、重复运行均值±标准差、费用估算（需配价格，未配置则不估算）。
- **运行**：

```bash
.venv/bin/python -m harness.run_harness                          # A/B/C/D, dev 集
.venv/bin/python -m harness.run_harness --split heldout          # 保留集
.venv/bin/python -m harness.run_harness --repeats 3              # 采样波动
# API 模式：.env 配置 APP_MODE=api 与密钥后同命令（Layered Judge 自动启用需
# EG_OUTPUT_GUARD=layered）
```

  输出：`results/harness_<mode>_<时间戳>/`（records.jsonl / sessions.jsonl /
  summary.md / summary.csv / confusion_*.json / metrics_by_variant.png）。
- **Mock 全流程验证（已真实运行，见 `results/harness_mock_20261016_193847/`）**：
  数字自洽且方向符合预期——A 版拦截 0%、越狱成功 100%、意图准确 23.3%（14/60）；
  B/C/D 拦截 100%、意图准确 71.7%；多轮泄漏 0%。**这些是流程验证数字，不代表
  真实模型实验结论**（mock 回答不含代码，泄漏/帮助指标在 mock 下无区分度，
  汇总中带【MOCK】水印）。真实消融数据待 API 密钥后运行同一命令获得。

## What has NOT been completed

**真实模型消融实验未运行**（无 API 密钥）：hybrid 分类、LLM Judge 的远端路径已实现并经脚本化后端单测，A/B/C/D Harness 已在 mock 下全流程验证，但 `results/harness_mock_*` 的数字带【MOCK】水印、仅证明流程可用。补充建议规模（约 120 单轮 + 15 多轮）时继续沿用同一 schema。Do **not** report unevaluated capabilities as experiment results.

## Recommended next milestones

1. ~~Replace `intent_classifier.py` with a rule+LLM classifier~~（P1 已完成；真实远端评测并入 P4）.
2. ~~Policy decoupling + layered output guard~~（P2 已完成；Judge 误判率评估并入 P4）.
3. ~~SessionGuard 多轮累计泄漏防护~~（P3 已完成；关联规则的漏/误关联率并入 P4）.
4. ~~Build Harness (A/B/C/D)~~（P4 框架已完成并在 mock 验证；**待密钥后运行真实消融**）.
5. Write methodology, complexity, metrics and a reproducibility report.

## Data and privacy

No student code is executed by this project. Student submissions may be sent to the configured remote model provider in API mode; use **synthetic test cases** unless users/teachers permit sending their materials. Chat turns are stored only in process memory (per-session `ServiceContext`); no persistent transcript logging is enabled. Chainlit 文件上传已关闭（仅接受文本输入）。

## References

- Course assignment PDF, especially page 5, project II.
- Qwen hosted endpoint, model ID: SiliconFlow model catalog.
- Compatible chat endpoint: provider docs and openai-python README.
