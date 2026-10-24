"""AlgoSage Chainlit 启动器（绕过 chainlit CLI 的 nest_asyncio 补丁）。

为什么需要它：chainlit CLI 在导入时执行 nest_asyncio.apply()；在
Python 3.15 + nest_asyncio 1.6.0 环境下，该补丁会让 sniffio 无法识别
asyncio 后端，anyio.to_thread 随之失效，导致所有 FileResponse 静态资源
（/assets/*.js、/public/custom.css）返回 500，页面白屏。

本启动器按 chainlit.cli.run_chainlit 的同一初始化序列手工装配，
再用原生 uvicorn 启动；未验证的建议保留 `chainlit run` 作为对照。
若未来 chainlit/nest_asyncio 修复此兼容性，可直接改回官方命令。

用法：
    .venv/bin/python run_app.py                       # 学生端 chainlit_app
    .venv/bin/python run_app.py eval_page --port 8081 # 教师/开发者评测页
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import urllib.parse
from pathlib import Path

from algosage.runtime_paths import application_root, runtime_path, PROJECT_ROOT

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AlgoSage Chainlit launcher (no nest_asyncio)"
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="chainlit_app",
        help="应用模块名：chainlit_app（学生端）或 eval_page（教师评测页）",
    )
    parser.add_argument("--host", default=os.environ.get("CHAINLIT_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("CHAINLIT_PORT", "8000"))
    )
    parser.add_argument(
        "--ssl-certfile",
        default=os.environ.get("SAGE_TLS_CERT_FILE"),
        help="TLS certificate PEM; enables HTTPS when paired with --ssl-keyfile",
    )
    parser.add_argument(
        "--ssl-keyfile",
        default=os.environ.get("SAGE_TLS_KEY_FILE"),
        help="TLS private key PEM; enables HTTPS when paired with --ssl-certfile",
    )
    args = parser.parse_args()
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        parser.error("--ssl-certfile and --ssl-keyfile must be provided together")
    if args.ssl_certfile and not (Path(args.ssl_certfile).is_file() and Path(args.ssl_keyfile).is_file()):
        parser.error("TLS certificate or private key does not exist")

    os.environ["CHAINLIT_HOST"] = args.host
    os.environ["CHAINLIT_PORT"] = str(args.port)

    # load_module 按文件路径加载目标模块（与 chainlit run <file>.py 一致）
    target = Path(args.target if args.target.endswith(".py") else args.target + ".py")
    target_file = str(PROJECT_ROOT / target) if target.parent == Path(".") else str(target.resolve())
    # Chainlit reads these values on import, including its .env/config/public paths.
    os.environ["CHAINLIT_APP_ROOT"] = str(application_root())
    os.environ["CHAINLIT_ENV_FILE"] = runtime_path("CHAINLIT_ENV_FILE", ".env")

    import uvicorn
    from fastapi.responses import FileResponse
    from starlette.middleware.base import BaseHTTPMiddleware

    # —— 与 chainlit.cli.run_chainlit 相同的初始化（不含 nest_asyncio）——
    from chainlit.auth import ensure_jwt_secret
    from chainlit.cache import init_lc_cache
    from chainlit.config import config, init_config, load_module
    from chainlit.markdown import init_markdown
    from chainlit.server import app

    init_config()
    config.run.host = args.host
    config.run.port = args.port
    config.run.module_name = target_file
    if os.path.basename(target_file) == "chainlit_app.py":
        from algosage.local_identity import ensure_local_secret

        ensure_local_secret()
    load_module(target_file)
    ensure_jwt_secret()
    if not (
        config.code.on_chat_start
        or config.code.on_message
        or config.code.on_audio_chunk
    ):
        raise SystemExit(f"{args.target} 未注册任何 on_chat_start/on_message 回调")
    init_markdown(config.root)
    init_lc_cache()

    # Chainlit 1.x only accepts ASCII avatar IDs. Runs persisted before the
    # safe internal step IDs use Chinese display names and would otherwise
    # trigger harmless but noisy 400s when the browser requests their avatar.
    # Return the project favicon for those legacy names; valid IDs still use
    # Chainlit's own avatar lookup.
    legacy_avatar_pattern = re.compile(r"^[a-zA-Z0-9_ .-]+$")

    async def legacy_avatar_fallback(request, call_next):
        if request.url.path.startswith("/avatars/"):
            raw_path = request.scope.get("raw_path")
            raw = raw_path.decode("utf-8", "ignore") if raw_path else request.url.path
            avatar_id = urllib.parse.unquote(raw.removeprefix("/avatars/"))
            if not legacy_avatar_pattern.fullmatch(avatar_id):
                return FileResponse(PROJECT_ROOT / "public/favicon.png")
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=legacy_avatar_fallback)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        ws="auto",
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
    )


if __name__ == "__main__":
    main()
