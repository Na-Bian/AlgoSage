# AlgoSage：算法课程分级辅助与多轮披露防护

AlgoSage 是面向算法学习的个人问答助教，基于 Python、Chainlit 和开源轻量模型的托管 API。账号、会话、附件与偏好按 HUST 学生身份隔离。服务端保留分级辅导、受限生成、输出审核和同题披露预算，避免直接代写完整作业。

## 功能

- **学生账号**：注册、登录、找回密码走 HUST 教育邮箱验证码，密码用 Argon2id 存储，学号即用户名。
- **多轮会话**：历史存 SQLite，支持新建、搜索、重命名、删除；断线重连后恢复历史和所选模型。
- **自定义模型（BYOK）**：学生在界面里添加自己的 OpenAI 兼容端点，只接受参数量 27B 以内的对话模型。密钥经 Fernet 加密存进服务端 `.env` 托管区块，浏览器拿不到。列表里的模型点选后才做真实能力探测（思考、多模态、联网），结果随注册持久化，也用于对话时开关的门控。
- **附件**：图片（PNG/JPEG/WebP）和 PDF、Office、代码等文档在服务端提取文本，不执行宏；图片等比缩放，文档摘要进入后续追问。
- **分级辅导**：L0 拒绝越权、L1 解释概念、L2 方向提示、L3 已有代码的局部诊断。练习模式不等于允许代写。
- **学生自选辅助等级**：输入区可选择本次对话的等级上限（自动/L1/L2/L3），设置面板可设账号默认。上限只能收紧服务端策略判定（生效等级 = min(策略, 学生上限)），考试模式封顶不变。当前等级计入审计依据。
- **真流式 + 审后屏蔽**：回答随生成逐步直播，思考过程同步进"思考过程"步骤（限高独立滚动、标题实时显示思考字数）；审核与流式并行，一旦全文审核未通过或触发安全策略，已播内容立即被整体替换为安全回退文案，失败回合的过程步骤自动清理。学生可能短暂看到未定稿内容，但不会保留任何违规输出。
- **审计面板**：每条回答可展开「安全审计与策略」，显示请求意图（中文）、允许帮助等级、审核结论、置信度与耗时；分类依据用自然语言说明规则与模型的判断。
- **界面**：深色/浅色主题（首帧同步，无闪白）、输入区生成选项（思考模式/联网搜索/辅助等级）、历史会话平滑切换、自动会话标题。

## 运行

Python 3.10+（开发机实际用 3.15）：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python run_app.py --host 127.0.0.1 --port 8000
```

学生端默认 `http://127.0.0.1:8000`。`run_app.py` 直接用 uvicorn；Python 3.15 上 Chainlit CLI 的 nest_asyncio 有静态资源问题，所以绕开了它。

离线验证要显式指定 mock（`Settings.from_env()` 运行缺省是 API）：

```bash
APP_MODE=mock python run_app.py
```

API 模式在本地未跟踪的 `.env` 里设 `APP_MODE=api`、`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`。模型可用性以服务商账号为准。密钥不要写进源码、日志、截图或提交包。

## 控制链

```text
可信考试/身份/题目上下文
 -> 确定性注入预检（明确拒答不调用 LLM）
 -> 正则 + 可选 LLM 意图分类
 -> 可信 Policy + 历史风险上限
 -> 受限候选生成（按探测到的能力透传 enable_thinking / enable_search）
 -> 规则 / 规则+Judge 全文审核（含同题历史披露、坏 JSON 一次修正确认）
 -> 独立知识/算例/回答范围复核（异常时不阻断；有原文证据的错误触发有限重生成）
 -> SessionGuard.preview 检查当前候选与历史组合
 -> 批准或安全回退
 -> 同预算锁内提交披露、用量记录和持久状态
 -> 学生最终回答
```

分类置信度是模型自报值，未校准；低置信或解析失败按 `uncertain` 处理，最多给一般概念。Judge 必须给出能在候选或历史里定位的原文，缺失或伪造证据不批准草稿。SessionGuard 区分学生原有代码与助教新增内容，预检累计核心步骤和代码片段，但它仍是启发式检测。一般概念和复杂度解释不消耗具体题目的披露预算。同一身份的预算整回合串行，断线、清显示历史、重连都不会清除。`call_trace` 与 `audit_attempts` 只留开发者侧，学生元数据白名单不含这两项。

## 配置

| 变量 | 含义 |
| --- | --- |
| `APP_MODE` | `mock` / `api`；运行缺省 `api` |
| `EXAM_MODE` | 服务端考试模式；学生界面不可关闭 |
| `SAGE_CLASSIFIER_MODE` | `regex` / `hybrid` |
| `SAGE_OUTPUT_GUARD` | `rules` / `layered`；`basic` 仅作弱基线对照 |
| `SAGE_POLICY_FILE` | 可信策略 JSON；非法直接拒绝启动，逐题覆盖只允许收紧当前全局等级 |
| `SAGE_EXAM_ID` | 安全状态分区标识，缺省 `demo`；不同部署应单独配置 |
| `SAGE_QUESTION_ID` | 服务端当前题号；设置后学生不能换号绕开逐题策略 |
| `SAGE_STATE_FILE` | SQLite 安全状态文件，缺省 `.algosage/state.sqlite3` |
| `SAGE_ALLOW_POLICY_SWITCH` | 内部兼容开关；学生界面不暴露策略切换 |
| `SAGE_MAX_CALLS_PER_TURN` | 每回合逻辑模型调用上限，缺省 9 |
| `SAGE_TURN_TIMEOUT_SECONDS` | 整轮截止时间，缺省 180 秒；生产部署建议 600–900（思考模型长推理）|
| `SAGE_MIN_CLASSIFIER_CONFIDENCE` | 自报置信度阈值，缺省 0.7 |
| `LLM_TIMEOUT_SECONDS` | 单次网络超时，与整轮剩余时间取较小值；思考模型建议 480+ |
| `LLM_MAX_OUTPUT_TOKENS` | 生成回答输出上限；思考模型的推理内容计入该预算 |
| `SAGE_LLM_EXTRA_BODY` | 服务商参数 JSON，例如 `{"enable_thinking":false}` |
| `SAGE_UI_DETAIL` | 学生审计展示 `brief` / `full` |
| `SAGE_EMAIL_SENDER` | `outbox`（开发） / `smtp`（生产） |
| `SAGE_SMTP_*` | SMTP 主机、端口、账号；支持 465 隐式 TLS 和 587 STARTTLS |
| `SAGE_AUTO_TITLES` | 自动会话标题，默认开，`false` 关闭 |
| `SAGE_CUSTOM_MODEL_MAX_B` | 自定义模型参数量上限，缺省 27 |

其余见 `.env.example`。

## 自定义模型（BYOK）

设置面板的"管理模型提供商"里添加 OpenAI 兼容端点。发现列表只列出参数量 27B 以内的对话模型；点选某个模型后发起真实探测：思考看流式增量里是否出现 reasoning_content，多模态看 64×64 图片请求是否被 4xx 拒绝，联网看 `enable_search` 是否被 4xx 拒绝。确认后探测结果随注册持久化。

服务重启后条目从 `.env` 托管区块（Fernet 加密密钥加能力标签）还原，能力不回退到命名推断。对话时"思考模式/联网搜索"开关按探测到的能力门控：模型不支持时开关打开也不透传参数。删除提供商即时生效；全部删完后发送被禁用，引导重新添加。

模型供给只支持学生自选的 BYOK 模式（见上方「自定义模型」）；服务端预置 Provider 列表（`providers.json`）已移除，不再提供统一供给模式。

## 学生账号与数据

登录认证固定为 HUST 教育邮箱身份。注册和密码重置在 `/auth/hust/register` 与 `/auth/hust/forgot-password`，登录用 Chainlit 原生表单，用户名是学号。系统把邮箱派生为 `学号@hust.edu.cn`，验证码一次性有效且有频率限制。实名 principal 为 `student:<学号>`。

开发环境用 `SAGE_EMAIL_SENDER=outbox`，生产改 `smtp`。上线前可用 `.venv/bin/python tools/smtp_check.py --to 收件人@hust.edu.cn` 检查投递。QQ 邮箱用 `smtp.qq.com:465`，`SAGE_SMTP_USERNAME` 填完整邮箱，`SAGE_SMTP_PASSWORD` 填授权码，`SAGE_EMAIL_FROM` 用同一邮箱。

聊天历史经 `algosage/chat_store.ChatDataLayer` 持久化到 `SAGE_CHAT_FILE`（缺省 `.algosage/chats.sqlite3`），附件原件在同目录 `elements/`。`run_app.py` 启动时，`.env`、数据库、签名密钥等相对路径以应用目录为基准；备份工作区在停服后保留 `.algosage/` 下的运行文件，不要把这些放进公开交付包。同一浏览器固定用同一访问地址，`localhost` 和 `127.0.0.1` 的 Cookie 互相隔离。

## 交付边界

代码不执行学生程序或附件宏。API 模式会把学生问题、代码、文档摘录和压缩图片发到配置的供应商，测试请用合成数据或授权材料。安全状态 SQLite 只存归一化题目锚点、披露片段和风险，不存完整聊天或 API Key。

`.env`、`.algosage/` 和运行文件必须排除在提交包外。仓库只放产品运行所需；`tests/`、`docs/`、`datasets/`、`deploy/`、`results/`、`harness/`、`tools/` 等开发支持内容留在本地，不进公开仓库。

交互结构参考了 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的会话侧栏、会话菜单与分组模型选择器（源码 HEAD `1600d8a`），没有引入其 Agent 工具执行能力。

## 回归验证

`tests/` 等开发目录不在公开仓库里；有完整工作副本时运行：

```bash
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m pytest -q
```
