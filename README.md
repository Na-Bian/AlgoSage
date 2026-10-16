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

## What has NOT been completed

No multi-turn answer-leakage detector (与历史提示合并的累计泄漏检测), and no batch harness. The hybrid classifier and the LLM Judge remote paths are implemented and unit-tested with scripted backends, but have **not** been evaluated with a real model (no API key used so far). Do **not** report unevaluated capabilities as experiment results. See `docs/MODULE_INTERFACES.md` for subsequent milestones and ablation plan. `eval_page.py` 是开发调试入口，不是正式评测 Harness。

## Recommended next milestones

1. ~~Replace `intent_classifier.py` with a rule+LLM classifier~~（P1 已完成；真实远端评测并入 P4）.
2. ~~Policy decoupling + layered output guard~~（P2 已完成；Judge 误判率评估并入 P4）.
3. Add contextual `SessionGuard`（多轮累计泄漏、题目改写不重置风险）.
4. Build Harness (JSONL cases; baseline A/B/C/D; same held-out test set, same model setup).
5. Write methodology, complexity, metrics and a reproducibility report.

## Data and privacy

No student code is executed by this project. Student submissions may be sent to the configured remote model provider in API mode; use **synthetic test cases** unless users/teachers permit sending their materials. Chat turns are stored only in process memory (per-session `ServiceContext`); no persistent transcript logging is enabled. Chainlit 文件上传已关闭（仅接受文本输入）。

## References

- Course assignment PDF, especially page 5, project II.
- Qwen hosted endpoint, model ID: SiliconFlow model catalog.
- Compatible chat endpoint: provider docs and openai-python README.
