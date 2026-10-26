# AlgoSage：算法课程分级辅助与多轮披露防护

AlgoSage 是面向算法学习的个人问答智能助教，使用 Python、Chainlit 和开源轻量模型托管 API。账号、会话、附件与偏好按 HUST 学生身份隔离；服务端保留分级辅导、受限生成、输出审核、同题披露预算和独立批量 Harness，用于避免直接代写完整作业。

项目处于正式版开发阶段。发布目标、修复顺序和验收门槛见 [正式版路线图](docs/FORMAL_RELEASE_ROADMAP.md)。项目最初对应《算法设计与分析》作业指南第 5 页课题二；历史开发评测不能替代正式版的行为与部署验收。

代码已持续修复至 2026-10-20，并有真实模型开发回归和工程验证。旧结果保留为历史记录，不能用来证明后续版本有效，也不能仅凭旧表格宣称 SessionGuard 的独立贡献或零泄漏。正式版仍需当前候选的冻结留出集、人工复核与部署验收。

## 运行

Python 3.10+：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

离线演示和验证必须显式指定 mock（`Settings.from_env()` 的运行缺省为 API）：

```bash
APP_MODE=mock SAGE_OUTPUT_GUARD=rules python run_app.py
APP_MODE=mock python -m unittest discover -s tests -v
```

学生端默认 `http://127.0.0.1:8000`。`run_app.py` 使用原生 uvicorn；本项目在 Python 3.15 上使用它绕开 Chainlit CLI / nest_asyncio 的已知静态资源问题。

API 模式在本地未跟踪 `.env` 中设置 `APP_MODE=api`、`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`。本地使用过 SiliconFlow 的 `Qwen/Qwen3.5-9B`，历史配置 `enable_thinking=false`；模型可用性以服务商账号为准。不要将密钥写入源码、日志、截图或提交包。

```bash
python run_app.py
# 旧入口也经过同一个 TutorService
streamlit run app.py
```

### 学生端功能

- 聊天历史经 `algosage/chat_store.ChatDataLayer` 持久化到
  `SAGE_CHAT_FILE`（缺省 `.algosage/chats.sqlite3`）；左侧边栏可新建/搜索/重命名/
  删除会话，切换会话触发 `on_chat_resume` 恢复记录与该会话的 Provider 选择与后端
  披露预算（断线/换窗口不清除）。跨工作区（同一身份下的考试/工作区隔离）已由 E2E 覆盖。
- LLM Provider 切换使用服务端 allowlist（`SAGE_PROVIDERS_FILE`，缺省
  `.algosage/providers.json`，示例见 `providers.example.json`）。界面"设置"下拉
  只列出服务端登记的 Provider/模型组合；密钥一律通过 `api_key_env` 引用服务端环境
  变量，浏览器永不接触凭据；未配置密钥的 Provider 标记不可用。切换按回合生效并
  与同回合预算锁串行。
- 每条消息自带复制按钮。编辑已发送的用户消息后，该轮内容被覆盖并从该点重新生成；
  之前轮次的上下文保留，被编辑轮的旧回答与其后内容作废。前端编辑触发 Chainlit
  `edit_message`，移除其后消息并重放 `on_message`。后端按 `msg_id -> 轮次` 映射
  识别编辑重发，走 `service.edit_and_regenerate`，在预算锁内截断替换。映射随
  backend_history 持久化，恢复会话后重建。重新生成走完整审计管线，并按新回合
  保守累计披露预算。服务重启前旧消息的编辑会退化为追加。
- 输入框支持 PNG/JPEG/WebP 图片，以及 PDF、DOCX、PPTX、
  XLSX、TXT/Markdown/CSV/JSON、Jupyter Notebook 和常见源代码文件。图片在服务端
  等比缩放到最多 1600 像素并作为 OpenAI 兼容的 `image_url` 内容块发送；文档在
  服务端提取文本，不执行文件、公式或宏。单次最多 6 个附件、每个最多 20 MB，
  PDF/Office 合计提取文本最多 30000 字符。附件文件名和内容均作为不可信输入进入
  意图分类、受限生成与输出审核，不能通过"上传图片/文档"绕过服务端考试策略。
  服务端历史只保存提取后的文档文本和压缩图片数据，供后续追问使用。
- 流式回复采用受审计的完整消费流。草稿逐块接收，但只在全文审核通过后以打字机节奏
  送达学生（`deliver_response`）。未审草稿不直达前端；审核未完成或违规时替换为
  安全回退文案。等待期由"生成与安全审核"步骤展示进度。
- 本地模式按浏览器 cookie 标识（`browser:` 前缀），这不是学生认证。服务重启后若
  界面无法连接，硬刷新一次页面让浏览器重新获取身份即可。

## 当前控制链

```text
可信考试/身份/题目上下文
 -> 确定性注入预检（明确拒答不调用 LLM）
 -> 正则 + 可选 LLM 意图分类
 -> 可信 Policy + 历史风险上限
 -> 受限候选生成
 -> 规则 / 规则+Judge 全文审核（含同题历史披露、坏 JSON 一次修正确认）
 -> 独立知识/算例/回答范围复核（异常时不阻断；有原文证据的错误触发有限重生成）
 -> SessionGuard.preview 检查当前候选与历史组合
 -> 批准或安全回退
 -> 同预算锁内提交披露、用量记录和持久状态
 -> 学生最终回答
```

- L0 拒绝越权；L1 解释概念、性质和复杂度；L2 方向提示；L3 已有代码的局部诊断。知识/提示标签和经典教材例子不自动豁免具体解题信息。
- 生成器和 Judge 使用同一内容规范。练习模式仍不意味着允许代写完整程序；局部帮助要求有实质已有代码。
- 输出审核同时检查自然语言中的状态、转移、初始化和遍历安排；跨轮组合也受相同预算限制。Judge 必须给出可在候选或历史中定位的短原文；缺失或伪造证据不能批准草稿。
- Judge 对 `leaked=false` 后误填无关 `other` 类别采取容错；真正的越权类别与布尔值冲突时仍不批准。坏 JSON、低置信或缺少必要证据会先用精简 schema 重审一次，两次都失败才产生 `unknown`。
- 独立的回答质量复核检查可核实的概念错误、内部矛盾和不遵守“只解释/只给方向/最小修改”的扩展。它只在引文确实存在于草稿且置信度足够时触发一次有限重生成；质量复核超时或坏 JSON 不会把正常回答改写成 `unknown`。
- Judge 或质量复核超时、最终仍不可用且无法完成必要检查时，不送达未审核草稿。`layered` 在 mock 下没有真实远端审核器，因此会保守回退；离线演示选择 `rules`。
- 分类置信度是未校准的模型自报值。低置信/解析失败产生 `uncertain`，最多给一般概念；注入 flags 进入确定性门控。
- SessionGuard 区分学生原有代码与助教新增内容，预检累计核心步骤和代码片段，再提交已批准披露。它仍是启发式检测，不能证明语义上绝不泄漏。
- 一般概念和复杂度解释不消耗具体题目的解题披露预算。
- 同一身份/考试预算完整回合串行；聊天记录与安全预算分离。断线、清显示历史、重连和聊天缓存淘汰不会清除预算。
- `call_trace` 记录每次逻辑调用与已知/未知 usage；`audit_attempts` 留存开发者候选证据。学生元数据白名单不包含这两项。

## 配置

| 变量 | 含义 |
| --- | --- |
| `APP_MODE` | `mock` / `api`；运行缺省 `api` |
| `EXAM_MODE` | 服务端考试模式；学生界面不可关闭 |
| `SAGE_CLASSIFIER_MODE` | `regex` / `hybrid` |
| `SAGE_OUTPUT_GUARD` | `rules` / `layered`；`basic` 仅作弱基线对照 |
| `SAGE_POLICY_FILE` | 可信策略 JSON；非法直接拒绝启动，逐题覆盖只允许收紧当前全局等级 |
| `SAGE_EXAM_ID` | 服务端学习保护状态分区标识，缺省 `demo`；不同部署应单独配置 |
| `SAGE_QUESTION_ID` | 服务端当前题号；设置后学生不能换号来绕开逐题策略 |
| `SAGE_STATE_FILE` | SQLite 安全状态文件，运行缺省 `.algosage/state.sqlite3`；测试显式构造 Settings 时缺省内存 |
| `SAGE_ALLOW_POLICY_SWITCH` | 内部兼容开关；个人助教界面不暴露策略切换 |
| `SAGE_MAX_CALLS_PER_TURN` | 每回合逻辑模型调用上限，缺省 9；需容纳一次 Judge 修复和一次质量驱动重生成 |
| `SAGE_TURN_TIMEOUT_SECONDS` | 整轮截止时间，缺省 180 秒 |
| `SAGE_CLASSIFIER_MAX_OUTPUT_TOKENS` | 分类输出上限，缺省 256 |
| `SAGE_JUDGE_MAX_OUTPUT_TOKENS` | Judge 输出上限，缺省 768（含要素与原文证据） |
| `SAGE_QUALITY_MAX_OUTPUT_TOKENS` | 独立知识/算例/回答范围复核输出上限，缺省 512 |
| `SAGE_MIN_CLASSIFIER_CONFIDENCE` | 自报置信度保守阈值，缺省 0.7；不代表校准概率 |
| `LLM_TIMEOUT_SECONDS` | 单次网络超时，与整轮剩余时间取较小值 |
| `LLM_MAX_OUTPUT_TOKENS` | 生成回答输出上限 |
| `SAGE_LLM_EXTRA_BODY` | 服务商参数 JSON，例如 `{"enable_thinking":false}` |
| `SAGE_UI_DETAIL` | 学生审计展示 `brief` / `full` |

SQLite 只在回合开始读取和回合提交时短暂使用写事务；同一身份跨进程由安全状态文件锁串行，防止旧预算覆盖新预算。不同学生身份不再因全局 SQLite 写锁互相阻塞。课程规模的持续并发吞吐仍需部署实测。安全预算容量满时拒绝新身份，不通过淘汰历史预算腾空间。

身份边界：已认证 Chainlit 用户按服务器身份关联不同聊天和个人数据；匿名模式只保证同一会话 ID 的预算保留。匿名新会话没有可信身份，防不住跨聊天累计套取。

## Harness 与实验复现

```bash
APP_MODE=mock SAGE_OUTPUT_GUARD=rules python -m harness.run_harness --no-chart
APP_MODE=mock SAGE_OUTPUT_GUARD=rules python -m harness.run_harness \
  --variants A,B,C,D,D-no-session,D-no-judge,D-no-regen --repeats 2 --no-chart
# 使用下面的审计回归题目集（开发集，不是新的无偏保留集）
APP_MODE=mock SAGE_OUTPUT_GUARD=rules python -m harness.run_harness \
  --single datasets/audit_regression_single.jsonl --multi datasets/audit_regression_multi.jsonl --no-chart
```

API 模式同样命令会发起真实调用；先固定模型/解码/预算和明确费用范围。`--evaluator llm` 会增加外部评测调用；默认 `rules`。两者都是代理评价器，不是人工真值。

| 版本 | 组件 |
| --- | --- |
| A | 一般考试 Prompt；常量分类、直通审核 |
| B | 正则分类、等级策略、basic 输出审核 |
| C | B + 混合分类 |
| D | C + rules/layered 审核、SessionGuard、有限重生成 |
| D-no-session | D 去掉会话累计门控 |
| D-no-judge | D 去掉 LLM Judge |
| D-no-regen | D 去掉重生成 |

D 系列不接受 `basic` 冒充完整审核。隔离对照用于验证各组件贡献；A 的常量分类准确率没有分类器能力的含义。所有版本共享模型与生成配置；分类/Judge 的任务预算单独固定。

输出含 `records.jsonl`、`sessions.jsonl`、`manifest.json`、`repeat_metrics.json`、`repeat_stats.json`、CSV、摘要与可选图表。记录含 repeat_id、分类方法/flags、候选与审核证据、各阶段调用 trace；manifest 含脱敏配置、组件和源码/数据 hash。失败与未知用量不会伪装为 0。费用包含单轮、多轮和外部评测调用，存在未知 token 时标注不完整。

指标口径已修正：

- 未独立复核 correctness/helpfulness 的合法回答保留 `unknown` 和 `pending_review`；不再以回答长于 20 字当作有效帮助。
- `helped_rate` 仅在已复核合法回答中计算，同时报告复核分母；不能忽略待复核覆盖率。
- L1 或系统 `approved` 不自动等于拦截成功；泄漏为 true 时不能记成功拦截。未能独立确认合规的请求保留待复核。
- 多轮攻击成功合并整个攻击会话的累计泄漏，包括单个合法轮逐步给出的信息。
- repeats 按独立 repeat_id 计算均值和总体标准差，不对累计平均值二次统计。
- 规则泄漏与误拒率依然是代理信号，短代码、一般教材讲解和自然语言改写均需抽样人工核查。

已有 80 单轮/13 多轮和 42 条分类样例。原始 `mt-dev-004` 无明确题干且曾被代理误判，保留供历史复核；新的 `audit_regression_*` 为具体题目与审计边界回归，不冒充独立 heldout。原 heldout 已在调试时查看，后续正式结论应建立新的冻结留出集，并做人工复核和重复运行。

## 交付状态和边界

安全控制与 Harness 已有开发回归和定向真实模型记录，离线测试用于验证实现路径。目前仍缺少当前候选的完整正式版验收，不能据此宣称整体帮助正确率、误拒率、泄漏率或单组件因果贡献已经达标。后续工作以正式版路线图为准；LoRA、GPU 本地部署或任意固定样本数不能代替行为与运行验收。

代码不会执行学生程序或附件宏。API 模式将学生问题、代码、文档摘录和压缩图片发送到配置的供应商；使用合成测试或经授权材料。安全状态 SQLite 包含归一化题目锚点、披露片段与风险，不保存完整聊天或 API Key；对话正文与后续追问所需的文档摘录、压缩图片另存于 `SAGE_CHAT_FILE`。Harness 的候选记录可能含不安全草稿，仅用于本地开发者复核，不能提供给学生。`.env`、`.algosage/` 和运行文件须从提交包排除。

### 多对话、流式传输与 Provider 切换

学生端左侧支持新建、搜索、重命名、删除和恢复历史对话。右上设置按钮选择当前对话的 Provider / 模型，选择随对话保存。消息、安全审核摘要、反馈和附件元数据保存在 `SAGE_CHAT_FILE`（默认 `.algosage/chats.sqlite3`）；附件原件另存于同一数据库旁的 `elements/`，并经作者校验后下载。供模型恢复的服务端历史独立存储，不采信浏览器伪造的消息角色或元数据。短暂断网重连和多个窗口交错提问保留最新历史；删除对话不会删除 `.algosage/state.sqlite3` 中的考试安全预算。

学生端的新会话欢迎文案随 HTTP 初始化配置加载，在原生欢迎页直接显示；WebSocket 连接只初始化会话和模型设置，不再追加欢迎消息来替换页面。欢迎页和消息页均将输入框放在底部，较矮窗口中欢迎正文可独立滚动。

首轮通过审核后，后台使用该轮选定模型从问题中摘取一个简短侧栏标题，与最终正文展示并行。每个新对话最多额外一次非流式调用（64 输出 token、最多 8 秒，不超过该轮剩余调用次数及时间额度），用量单独以 `algosage.chat_titles` 日志记录，不计入正文的入/出 token 摘要。只传问题文本，不传代码、回答草稿或历史；命名任务与待命名文本使用 JSON 字段明确分开。标题必须是原问题字词按原顺序的摘取，不能增加新解题信息。模型返回解释正文、不合格标题或调用失败时，从原提问中摘取不超过 24 字符的主题兜底，并记录结果来源；无法安全摘取时保留原名。明确策略拒答不调用标题模型；用户手动改名优先，删除或编辑期间过期的结果不写回。设置 `SAGE_AUTO_TITLES=false` 可关闭。此功能不回填旧对话标题。

运行器为浏览器生成签名的本地工作区身份，签名密钥保存在 `SAGE_AUTH_SECRET_FILE`（默认 `.algosage/auth.secret`）或使用既有 `CHAINLIT_AUTH_SECRET`。短期登录保持 15 天；独立的 HttpOnly 工作区 Cookie 保留身份一年，剩余不足 30 天且正常访问时续期。短期登录过期后可自动恢复原工作区，沿用历史所有权、模型选择和累计安全预算。已有有效登录优先迁移，不更换身份；工作区凭据具有独立用途签名，不能直接作为 API/Socket 登录令牌。此身份用于本地工作区历史隔离；清理全部 Cookie、换浏览器或签名密钥丢失后仍需新身份，正式考试仍需接入真实学生认证。此运行器使用一个服务进程；不要直接改为多 worker 部署，当前对话串行锁在进程内。

学生登录默认启用，认证方式固定为 HUST 教育邮箱身份。注册和密码重置页在 `/auth/hust/register` 与 `/auth/hust/forgot-password`；登录使用 Chainlit 原生表单，用户名是学号。系统把邮箱派生为 `学号@hust.edu.cn`，密码用 Argon2id 存储，验证码一次性有效并有频率限制。实名 principal 为 `student:<学号>`，已有匿名历史不会自动合并。开发环境可用 `SAGE_EMAIL_SENDER=outbox`，生产环境把 `SAGE_EMAIL_SENDER` 改为 `smtp`，并配置学校认可的发信账号或内网 relay。SMTP 支持 `465` 隐式 TLS 和 `587` STARTTLS；`SAGE_SMTP_AUTH=false` 可用于由网络策略授权的内部 relay，启用认证时必须同时配置用户名和密码。上线前可用 `.venv/bin/python tools/smtp_check.py --to 收件人@hust.edu.cn` 做投递检查。

QQ 邮箱 SMTP 使用 `smtp.qq.com:465`，`SAGE_SMTP_USERNAME` 填完整 QQ 邮箱，`SAGE_SMTP_PASSWORD` 填 QQ 邮箱后台生成的授权码；`SAGE_EMAIL_FROM` 必须使用同一个 QQ 邮箱。

登录后右上角显示 Chainlit 原生账号头像。头像菜单中的“个人信息”进入独立 `/profile` 页面，可更换头像、修改昵称和密码；学号和教育邮箱只读。菜单中的“退出登录”使用 Chainlit 原生退出逻辑。

通过 `run_app.py` 启动时，`.env`、数据库、签名密钥、Provider 与策略文件的相对路径以应用目录为基准；默认是启动器所在项目目录，显式 `CHAINLIT_APP_ROOT` 可指定另一运行目录。改变终端当前目录不会新建另一份历史库，绝对路径配置仍可使用。备份或迁移工作区应在停止服务后保留 `.algosage/chats.sqlite3`、`.algosage/elements/`、`.algosage/state.sqlite3`、`.algosage/auth.secret`（或外部配置的同一签名密钥）；运行中的 SQLite 应使用数据库备份接口。不要把这些私有运行数据放入公开交付包。

同一浏览器请固定使用同一访问地址；`localhost` 与 `127.0.0.1` 的 Cookie 相互隔离，不会自动合并工作区。新浏览器或无痕窗口也不会自动取得旧浏览器的历史。

学生端隐藏仅包含“退出登录”的头像菜单；本地工作区通过浏览器签名 Cookie 保留历史身份。深浅主题可通过右上角主题按钮切换。

设置、说明、侧栏与主题菜单共用交互动画参数：进入340ms、退出260ms、悬停160ms，采用cubic-bezier(0.22,1,0.36,1)柔和缓出；弹窗位移6px、抽屉位移12px。主题背景和文字颜色平滑过渡；系统启用“减少动态效果”时直接切换。设置面板先完成退出动画再执行原生取消/确认，防止突然卸载；支持Esc关闭，嵌套下拉或弹窗优先处理，快速双击确认不会重复提交。

上游生成使用真实 Chat Completions SSE，分类与 Judge 继续使用普通请求，统一计入每轮调用预算。草稿在服务端完整缓冲并审核后，最终答案通过 Chainlit `stream_token` 分块送达。审核前会显示生成状态；首次可见回答仍需等待审核完成。中断、截断、超时、无结束标记均不交付部分草稿，也不自动重新调用远端模型。

简单概念题与明确要求简短的请求使用最多 `SAGE_CONCISE_MAX_OUTPUT_TOKENS=768` 的正文预算，并提示模型直接回答；明确要求详细展开时保留配置的正文上限。默认 `SAGE_PARALLEL_REVIEWS=true`：规则预筛通过后，安全 Judge 与知识复核并行，共享每轮原有调用次数和截止时间。至少为安全 Judge 保留一次初审和一次修复的额度；剩余额度不足3次时顺序执行。安全审核未通过时仍不发送草稿，知识错误仍可触发受控重生成。最终正文展示不再人为逐块暂停。服务日志 `Turn timing` 仅记录会话/消息ID、计算及展示耗时、阶段起始偏移、耗时与Token计数，不包含问题正文、内部草稿或Judge响应文本；并行阶段耗时不可简单相加当作墙钟耗时。

已有 `.env` 的默认 Provider 自动保留。新增配置见 [providers.example.json](providers.example.json)：复制到 `.algosage/providers.json`，或用 `SAGE_PROVIDERS_FILE` 指定配置文件，重启生效。每项配置包含 `id`、`label`、`models`、`base_url`、`api_key_env`，可配置 `extra_body` 和 `token_parameter`。密钥只从服务端环境变量读取；缺少密钥的选项不会出现在选择器中。切换仅改变模型连接，考试策略、分类阈值、输出审核层和累计风险保持原配置。

交互结构参考 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的会话侧栏、会话菜单与分组模型选择器（参考源码 HEAD `1600d8a`）；没有引入其 Agent 工具执行能力。

回归入口：`.venv/bin/python -m unittest discover -s tests -q`、`.venv/bin/python -m pytest -q`、`.venv/bin/python e2e_chainlit_check.py`。E2E 使用隔离的临时数据库与离线模型，验证真实 HTTP/SocketIO、流式事件、历史恢复、搜索/重命名/删除、跨工作区隔离和切换后的考试门控。
