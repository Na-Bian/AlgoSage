# AlgoSage 个人助教部署说明

## 本地开发

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python run_app.py --host 127.0.0.1 --port 8000
```

正式镜像和 CI 使用 `requirements.lock`（当前锁定 Python 3.13 依赖集）；本地开发可继续使用 `requirements.txt` 的版本范围。

本地默认只允许 `http://localhost:8000` 与 `http://127.0.0.1:8000`。生产部署必须把 `SAGE_ALLOWED_ORIGINS` 改为用户实际访问的 HTTPS Origin。

## Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
```

Compose 只把宿主机回环地址 `127.0.0.1:8000` 暴露到本机；公网访问应经过 HTTPS 反向代理。学生数据保存在命名卷 `algosage-runtime`。

## 生产检查清单

1. 在 `.env` 配置真实模型密钥，不要提交或打印。
2. 设置 `SAGE_ALLOWED_ORIGINS=https://你的域名`。
3. HTTPS 反向代理生效后设置 `SAGE_SECURE_COOKIES=true`。
4. 保持 `CHAINLIT_COOKIE_SAMESITE=strict`，除非经过安全评审确认需要放宽。
5. 配置真实 SMTP，并用 `tools/smtp_check.py --to test@example.com` 做投递检查。
6. 备份 Docker 卷或 `.algosage` 运行目录；恢复演练必须覆盖聊天库、认证库、安全状态库、签名密钥和附件。
7. 定期检查磁盘、日志、API 费用和每学生请求限额。

当前镜像为单进程 Uvicorn。不要直接改成多 worker；会话内锁和 SQLite 写路径按单服务进程设计。横向扩容前需要先把学习保护状态与配额迁移到支持并发一致性的服务。

## 账号注销

学生注销账号会删除：

- 该账号的全部会话、消息、后端模型历史、反馈和附件；
- 个人学习保护状态快照；
- 每日请求计数；
- Chainlit 持久化用户记录。

认证库中的关闭状态记录会保留，用于防止旧登录 Cookie 继续使用账号；后续如需缩短保留期或清理安全事件计数，应先通过法律与安全评审确定保留清单。

## 无域名 ECS 的临时 HTTPS

当前 ECS 使用 IP 访问，公网 CA 通常不能为裸 IP 签发可信证书。首次上线可生成包含 ECS IP 的自签名证书，浏览器第一次访问时选择继续访问；绑定域名后应换成 CA 证书并删除该临时证书。

```bash
.venv/bin/python tools/generate_selfsigned_cert.py \
  --cert .algosage/tls/algosage.crt \
  --key .algosage/tls/algosage.key \
  --host 101.37.35.197 --host localhost

python run_app.py --host 0.0.0.0 --port 443 \
  --ssl-certfile .algosage/tls/algosage.crt \
  --ssl-keyfile .algosage/tls/algosage.key
```

同时在 `.env` 中设置：

```bash
SAGE_ALLOWED_ORIGINS=https://101.37.35.197
SAGE_SECURE_COOKIES=true
```

云安全组必须放行 `443/tcp`；不要公网暴露 `8000/tcp`。

## Windows Server 原生部署（当前 ECS）

当前阿里云 ECS 是 Windows Server 2022，采用原生 Python 部署而不是 Docker：

- Python：`C:\AlgoSage\Python313`
- 应用：`C:\AlgoSage\app`
- 虚拟环境：`C:\AlgoSage\app\.venv`
- 运行数据：`C:\AlgoSage\app\.algosage`
- 日志：`C:\AlgoSage\app\logs`
- Windows 服务：`AlgoSage`，使用 NSSM 自启动
- 监听：`0.0.0.0:80`
- Windows 入站防火墙规则：`AlgoSage HTTP`

常用服务命令：

```powershell
nssm status AlgoSage
nssm restart AlgoSage
nssm stop AlgoSage
Get-Content C:\AlgoSage\app\logs\algosage-service.err.log -Tail 100
```

更新代码后至少同步修改过的源码文件并重启服务。`state_store.py` 已兼容 Windows；文件锁在 Windows 使用 `msvcrt`，在 Linux/macOS 继续使用 `fcntl`。Windows 环境读取 JSON/源码文件必须显式使用 UTF-8，不要依赖系统默认 ANSI 代码页。
