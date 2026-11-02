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


# 应用外壳（/ 与 /thread/*）的首帧引导片段：在浏览器第一次绘制前完成
# ① 主题 class（深色模式不再闪白）② /thread/* 的品牌加载遮罩。样式必须
# 内联——外部 CSS 到达晚于首帧。遮罩由 chat-ui.js 在内容渲染后接管移除。
APP_SHELL_BOOT_SNIPPET = """
    <style id="eg-early-theme-bg">
      /* 首帧背景：颜色与 custom.css 的 --background 一致（浅 210 45% 98% /
         深 228 36% 9%）。custom.css 到达前浏览器先画这层底色，避免刷新与
         会话切换时出现一帧白底。 */
      html { background-color: hsl(210 45% 98%); }
      html.dark { background-color: hsl(228 36% 9%); }
    </style>
    <script id="eg-early-boot-script">
      // 首帧同步应用主题（深色不再闪白）。只做这一件事，不注入任何遮罩。
      (function () {
        try {
          var theme = localStorage.getItem("vite-ui-theme") || "system";
          var dark =
            theme === "dark" ||
            (theme !== "light" &&
              window.matchMedia("(prefers-color-scheme: dark)").matches);
          if (dark) document.documentElement.classList.add("dark");
        } catch (e) {}
      })();
    </script>
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AlgoSage Chainlit launcher (no nest_asyncio)"
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="chainlit_app",
        help="应用模块名：chainlit_app（学生端）",
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

    async def html_no_cache(request, call_next):
        # SPA 外壳（/、/login、/thread/*）必须每次回源：发版后 HTML 里带
        # 新的 ?v= 静态地址，若 HTML 被启发式缓存，刷新也会继续跑旧脚本。
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=html_no_cache)

    async def inject_app_shell_boot(request, call_next):
        # 应用外壳首帧引导：主题 class 与 /thread/* 品牌遮罩必须在浏览器第
        # 一次绘制前就位——外部 CSS/JS 的到达晚于首帧，深色模式下会先闪白。
        # 仅缓冲 / 与 /thread/* 的 HTML（体量 <3KB），其余请求直通。
        path = request.url.path
        if path != "/" and not path.startswith("/thread/"):
            return await call_next(request)
        # 剥离 Accept-Encoding：Chainlit 内层的 GZip 中间件会压缩响应体，
        # 压缩流无法按明文注入（也不会含 <head> 标记）。无该头则下游不压缩。
        request.scope["headers"] = [
            (k, v)
            for k, v in request.scope["headers"]
            if k.lower() != b"accept-encoding"
        ]
        response = await call_next(request)
        if (
            not response.headers.get("content-type", "").startswith("text/html")
            or response.headers.get("content-encoding")
        ):
            return response
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        html = body.decode("utf-8", "ignore")
        if "<head>" not in html or "eg-early-boot-script" in html:
            # 注入点缺失或已注入：原样回放，避免重建损坏未压缩流。
            from starlette.responses import Response as StarletteResponse

            return StarletteResponse(
                content=body,
                status_code=response.status_code,
                headers={
                    "Cache-Control": "no-cache",
                    "Content-Type": "text/html; charset=utf-8",
                },
            )
        html = html.replace("<head>", "<head>" + APP_SHELL_BOOT_SNIPPET, 1)
        from starlette.responses import HTMLResponse

        return HTMLResponse(
            html,
            status_code=response.status_code,
            headers={"Cache-Control": "no-cache"},
        )

    app.add_middleware(BaseHTTPMiddleware, dispatch=inject_app_shell_boot)

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
