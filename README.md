# AlgoSage：算法课程分级辅助与多轮披露防护

## 产品概述

AlgoSage 是面向《算法设计与分析》课程的受限型编程与算法问答助教，基于 Python、Chainlit 与开源轻量模型 API 服务构建。账号使用 HUST 学号注册，会话、附件与偏好等数据按账号隔离存储。服务端内置分级辅导、受限生成、输出审核与同题披露预算机制，避免直接代写完整作业。

完整的产品说明（功能、实现、使用流程与实机测试）见 [docs/PRODUCT_DOCUMENTATION.md](docs/PRODUCT_DOCUMENTATION.md)。

## 核心功能

### 学生账号

- 注册、登录与找回密码均通过 HUST 教育邮箱获取验证码。
- 密码使用 Argon2id 存储，学号即用户名。
- 账号注销：删除账号与全部个人数据。

### 多轮会话

- 历史记录存储于 SQLite，支持新建、搜索、重命名与删除。
- 断线重连后恢复历史会话及所选模型。

### 自定义模型（BYOK）

- 学生可在「管理模型提供商」中添加 OpenAI 兼容端点；内置常见厂商预设（硅基流动、DeepSeek、智谱 GLM、Kimi、通义千问、火山方舟、小米 MiMo、MiniMax、腾讯混元、百度千帆、OpenAI），点选后自动预填 Base URL，补一个 API Key 即可获取模型列表并添加；也保留完全自定义填写。
- 可添加的模型参数量上限由 `SAGE_CUSTOM_MODEL_MAX_B` 控制（设为 0 表示暂不限制）。
- 密钥经 Fernet 加密后存入服务端 `.env` 托管区块，浏览器无法获取。
- 列表中的模型被点选后，才进行真实能力探测（思考、多模态、联网）；探测结果随注册信息持久化，并用于对话时相关开关的门控。
- 不支持图像的模型收到图片附件时，回答开头明确提醒「当前模型不支持图像输入」。

### 附件处理

- 可附加 PDF、DOCX、PPTX、XLSX、文本和常见代码文件，在服务端提取文本，不执行宏；PNG/JPEG/WebP 图片仅在当前模型支持视觉时可用。
- 图片按比例缩放；文档摘要进入后续追问上下文。

### 分级辅导

- L0：拒绝越权请求。
- L1：解释概念（不含具体状态定义与递推式）。
- L2：概念讲解、状态/转移的文字描述与方向性提示；不含代码块与伪代码。
- L3：对已有代码进行局部诊断（须以学生自己的代码为基础）。
- 完整代码、完整题解与可拼装的步骤序列在任何等级都被内容契约拦截；练习模式不等于允许代写。

### 学生自选辅助等级

- 输入区可选择本次对话的等级上限（自动/L1/L2/L3），选择按「对话」持久保存（刷新不丢失），设置面板可设置账号默认值。
- 「自动」由服务端逐条判定：意图分类 → 会话累计风险收紧 → 内容契约，等级只降不升。
- 学生上限只能收紧服务端策略判定，生效等级为 `min(策略, 学生上限)`；考试模式封顶不变。
- 当前等级计入审计依据。

### 流式回复与审后屏蔽

- 回答随模型生成逐步呈现，思考过程同步进入「思考过程」步骤（限高独立滚动）。
- 审核与流式并行。一旦全文审核未通过或触发安全策略，已播内容立即整体替换为安全回退文案，失败回合的过程步骤自动清理。
- 学生可能短暂看到未定稿内容，但不会保留任何违规输出。

### 审计面板

- 每条回答可展开「安全审计与策略」，显示请求意图、允许帮助等级、审核结论、置信度与耗时。
- 分类依据以自然语言说明规则与模型的判断。

### 内置辅助模型

回答生成始终使用学生自选的模型；**辅助任务全部由服务端内置模型承担**，判定尺度与成本不随学生的 BYOK 选择漂移：

| 任务 | 内置模型配置 | 说明 |
| --- | --- | --- |
| 意图分类（LLM 层） | `CLASSIFIER_MODEL` | hybrid 分类模式；正则保留为地板与回退 |
| 解法要素抽取 | `CLASSIFIER_MODEL` | 回答送达后异步补充正则漏记的风险要素（只增不删） |
| 语义审核 Judge + 质量审查 | `JUDGE_MODEL` | layered 审核模式；服务端策略执行 |
| 会话标题 | `TITLE_MODEL` | 回合开始即生成，亚秒级 |

未配置时依次回退 `CLASSIFIER_*` → 主模型配置（`TITLE_*` 直接回退主模型）。规则预筛、SessionGuard、注入预检均为确定性逻辑，不调任何模型。

### 界面

- 提供深色/浅色主题；移动端自适应布局。
- 输入区提供生成选项（思考模式/联网搜索/辅助等级），等级按对话持久保存。
- 历史会话平滑切换，支持自动会话标题（回合开始即生成，侧栏即时更新）。
- 会话生成中，侧栏对应条目显示转圈指示。

## 运行方式

要求 Python 3.10+：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python run_app.py --host 127.0.0.1 --port 8000
```

学生端默认地址：`http://127.0.0.1:8000`。

若使用 Python 3.15，由于 Chainlit CLI 的 `nest_asyncio` 存在静态资源问题，`run_app.py` 直接使用 uvicorn。离线验证需显式指定 mock（`Settings.from_env()` 运行缺省为 API）：

```bash
APP_MODE=mock python run_app.py
```

API 模式需在本地未跟踪的 `.env` 中设置：

```env
APP_MODE=api
LLM_API_KEY=...
LLM_BASE_URL=...
LLM_MODEL=...
```

模型可用性以服务商账号为准。

## 控制链

1. 可信考试/身份/题目上下文。
2. 确定性注入预检（明确拒答时不调用 LLM）。
3. 正则 + 可选 LLM 意图分类（LLM 层走内置策略模型；不一致取更保守判定）。
4. 可信 Policy + 历史风险上限。
5. 受限候选生成（按探测到的能力透传 `enable_thinking` / `enable_search`；输出上限由学生设置面板与模型档案共同决定）。
6. 规则 / 规则 + Judge 全文审核（Judge 走内置审核模型；含同题历史披露、坏 JSON 一次修正确认）。
7. 独立知识/算例/回答范围复核（异常时不阻断；有原文证据的错误触发有限重生成）。
8. `SessionGuard.preview` 检查当前候选与历史组合；回答送达后由内置模型补充抽取正则漏记的解法要素（只增不删）。
9. 批准或安全回退。
10. 同预算锁内提交披露、用量记录和持久状态。
11. 返回学生最终回答。

分类置信度为模型自报值，未校准；低置信或解析失败按 `uncertain` 处理，最多提供一般概念。Judge 必须给出能在候选或历史中定位的原文，缺失或伪造证据不批准草稿。SessionGuard 区分学生原有代码与助教新增内容，预检累计核心步骤和代码片段，但仍属启发式检测。一般概念和复杂度解释不消耗具体题目的披露预算。同一身份的预算整回合串行，断线、清空显示历史、重连均不会清除。`call_trace` 与 `audit_attempts` 仅保留在开发者侧，学生元数据白名单不包含这两项。

## 配置参数

| 变量 | 含义 |
| --- | --- |
| `APP_MODE` | `mock` / `api`；运行缺省为 `api` |
| `EXAM_MODE` | 服务端考试模式；学生界面不可关闭 |
| `SAGE_CLASSIFIER_MODE` | `regex` / `hybrid` |
| `SAGE_OUTPUT_GUARD` | `rules` / `layered`；`basic` 仅作弱基线对照 |
| `SAGE_POLICY_FILE` | 可信策略 JSON；非法时直接拒绝启动，逐题覆盖只允许收紧当前全局等级 |
| `SAGE_EXAM_ID` | 安全状态分区标识，缺省 `demo`；不同部署应单独配置 |
| `SAGE_QUESTION_ID` | 服务端当前题号；设置后学生不能换号绕开逐题策略 |
| `SAGE_STATE_FILE` | SQLite 安全状态文件，缺省 `.algosage/state.sqlite3` |
| `SAGE_ALLOW_POLICY_SWITCH` | 内部兼容开关；学生界面不暴露策略切换 |
| `SAGE_MAX_CALLS_PER_TURN` | 每回合逻辑模型调用上限，缺省 9 |
| `SAGE_TURN_TIMEOUT_SECONDS` | 整轮截止时间，缺省 180 秒；生产部署建议 600–900（思考模型长推理） |
| `SAGE_MIN_CLASSIFIER_CONFIDENCE` | 自报置信度阈值，缺省 0.7 |
| `LLM_TIMEOUT_SECONDS` | 单次网络超时，与整轮剩余时间取较小值；思考模型建议 480+ |
| `LLM_MAX_OUTPUT_TOKENS` | 生成回答输出上限；思考模型的推理内容计入该预算 |
| `SAGE_LLM_EXTRA_BODY` | 服务商参数 JSON，例如 `{"enable_thinking":false}` |
| `SAGE_UI_DETAIL` | 学生审计展示 `brief` / `full` |
| `SAGE_EMAIL_SENDER` | `outbox`（开发） / `smtp`（生产） |
| `SAGE_SMTP_*` | SMTP 主机、端口、账号；支持 465 隐式 TLS 和 587 STARTTLS |
| `SAGE_AUTO_TITLES` | 自动会话标题，默认开启；`false` 关闭 |
| `SAGE_CUSTOM_MODEL_MAX_B` | 自定义模型参数量上限，设为 0 表示暂不限制 |
| `CLASSIFIER_MODEL` / `_BASE_URL` / `_API_KEY` | 内置策略模型：hybrid 分类 LLM 层 + 解法要素抽取；未配置回退主模型 |
| `JUDGE_MODEL` / `_BASE_URL` / `_API_KEY` | 内置审核模型：LLM Judge + 质量审查；未配置依次回退 `CLASSIFIER_*` → 主模型 |
| `TITLE_MODEL` / `_BASE_URL` / `_API_KEY` | 内置标题模型：会话标题生成；未配置回退主模型 |
| `SAGE_ALLOWED_ORIGINS` | 允许的浏览器 Origin 列表（跨域防护），缺省本机地址 |

其余见 `.env.example`。

## BYOK 能力探测

在设置面板的「管理模型提供商」中添加 OpenAI 兼容端点。支持厂商预设（硅基流动、DeepSeek、智谱 GLM、Kimi、通义千问、火山方舟、小米 MiMo、MiniMax、腾讯混元、百度千帆、OpenAI）：点选预设自动预填 Base URL，补 API Key 后「获取模型列表」实时拉取可选模型（厂商随时发新模型，预设不锁定模型名）。也保留完全自定义填写。

发现列表只列出参数量在 `SAGE_CUSTOM_MODEL_MAX_B` 以内的对话模型；点选某个模型后发起模型能力探测：

- 思考能力：观察流式增量中是否出现 `reasoning_content`。
- 多模态能力：观察 64×64 图片请求是否被 4xx 拒绝。
- 联网能力：观察 `enable_search` 是否被 4xx 拒绝。

确认后，探测结果随注册信息持久化。服务重启后，条目从 `.env` 托管区块（Fernet 加密密钥加能力标签）还原。对话时，「思考模式/联网搜索」开关按探测到的能力门控；模型不支持时，即使开关打开也不透传参数。删除提供商即时生效；全部删除后发送功能被禁用，并引导重新添加。

## 学生账号与数据

AlgoSage 使用 HUST 学号注册，并通过教育邮箱接收验证码。注册与密码重置入口为 `/auth/hust/register` 与 `/auth/hust/forgot-password`，登录使用 Chainlit 表单，用户名为学号。系统将邮箱派生为 `学号@hust.edu.cn`，验证码一次性有效并有频率限制。实名 principal 为 `student:<学号>`。设置面板提供账号注销入口，删除账号即删除全部个人数据。

开发环境使用 `SAGE_EMAIL_SENDER=outbox`，生产环境改为 `smtp`。上线前可用以下命令检查投递：

```bash
.venv/bin/python tools/smtp_check.py --to 收件人@hust.edu.cn
```

邮箱服务器 SMTP 服务使用 QQ 邮箱时，配置为 `smtp.qq.com:465`；`SAGE_SMTP_USERNAME` 填完整邮箱，`SAGE_SMTP_PASSWORD` 填授权码，`SAGE_EMAIL_FROM` 使用同一邮箱。

聊天历史经 `algosage/chat_store.ChatDataLayer` 持久化到 `SAGE_CHAT_FILE`（缺省 `.algosage/chats.sqlite3`），附件原件位于同目录 `elements/`。`run_app.py` 启动时，`.env`、数据库、签名密钥等相对路径以应用目录为基准。备份工作区应在停服后保留 `.algosage/` 下的运行文件，不要将这些文件放入公开交付包。同一浏览器应固定使用同一访问地址；`localhost` 与 `127.0.0.1` 的 Cookie 互相隔离。
