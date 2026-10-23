# 阿里云 ECS 首次部署（101.37.35.197）

目标形态：Nginx 监听 ECS 的 80 端口，AlgoSage 仅在 Docker 内网监听 8000，数据保存在 `algosage-runtime` 卷。

## 1. 阿里云控制台要求

在 ECS 安全组中放行：

- 本机运维 SSH 端口：默认 `22/tcp`，来源建议限制为你的当前公网 IP。
- 用户访问：`80/tcp`，来源 `0.0.0.0/0`。
- 不要公网放行 `8000/tcp`。

## 2. 本地上传

在项目根目录执行。首次登录方式确定后可由 Codex 自动完成：

```bash
ssh user@101.37.35.197 'mkdir -p /opt/algosage'
git archive HEAD | ssh user@101.37.35.197 'tar -x -C /opt/algosage'
scp .env user@101.37.35.197:/opt/algosage/.env
mkdir -p /opt/algosage/deploy/ecs
scp deploy/ecs/nginx.conf deploy/ecs/nginx-top.conf user@101.37.35.197:/opt/algosage/deploy/ecs/
```

`.env` 不在 Git 里，必须单独安全复制。不要把它打印到聊天或日志。

## 3. 服务器构建与启动

```bash
ssh user@101.37.35.197
cd /opt/algosage
docker compose -f docker-compose.prod.yml up -d --build
```

如果服务器尚未安装 Docker，先按当前系统安装 Docker Engine 和 Docker Compose plugin，再执行上面的启动命令。

## 4. 验证

```bash
curl -fsS http://127.0.0.1/health
curl -fsS http://101.37.35.197/health
```

浏览器访问：

```text
http://101.37.35.197
```

## 5. 正式上线前

当前使用裸 IP 时只能先配置 HTTP Origin。正式使用前建议绑定域名并启用 HTTPS，然后把 `.env` 改为：

```bash
SAGE_ALLOWED_ORIGINS=https://your.domain
SAGE_SECURE_COOKIES=true
```

同时把 Nginx 配置换成 TLS 证书版本，并用 `docker compose -f docker-compose.prod.yml up -d` 重新生效。
