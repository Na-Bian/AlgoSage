# 多对话、审核后流式回复与 Provider 切换

范围：在既有 Chainlit 学生端加入持久化历史、新建/搜索/重命名/删除/恢复、每对话 Provider/model 选择与流式传输。保留 Python TutorService 与考试门控。

1. 持久化：SQLite Chainlit DataLayer；本地浏览器签名身份跨对话复用；独立服务端历史用于模型上下文。删除对话不删除考试预算。
2. Provider：服务端 JSON allowlist 与环境变量密钥引用；一轮分类/生成/Judge 使用同一配置，所有 Provider 共享原有策略与身份预算。界面不接受任意 URL 或密钥。
3. 流式：上游真实 SSE 完整缓冲，资源账本/截止时间覆盖；整段审核完成后使用 Chainlit stream_token 发送最终答案。审核前等待时间仍然存在。
4. 验证：配置/运行路由、身份、持久化和流式异常测试；完整 pytest/unittest、HTTP/SocketIO、多对话浏览器交互、桌面和手机宽度、独立代码审查。

采用既有工作区原位修改，保留上一轮未提交修复；不创建提交或发布。用户已授权实现上述三项功能，按当前架构继续，无额外批准步骤。

参考（只参考交互结构，不移植 Agent 执行能力）：DeepSeek Harness ui-sidebar/SidebarRoot.tsx、ui-model-selection/ModelSelect.tsx、ui-workspace/session-actions/RenameSession.tsx，上游 HEAD 见本次实现说明。

进度：服务已先行重启；Provider 与流式定向测试已通过；SQLite 持久化测试已通过；统一浏览器与回归验证进行中。
