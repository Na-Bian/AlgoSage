"""ExamGuard Chainlit 启动器（绕过 chainlit CLI 的 nest_asyncio 补丁）。

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
import sys
from pathlib import Path

from examguard.runtime_paths import application_root, runtime_path, PROJECT_ROOT

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ExamGuard Chainlit launcher (no nest_asyncio)"
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
    args = parser.parse_args()

    os.environ["CHAINLIT_HOST"] = args.host
    os.environ["CHAINLIT_PORT"] = str(args.port)

    # load_module 按文件路径加载目标模块（与 chainlit run <file>.py 一致）
    target = Path(args.target if args.target.endswith(".py") else args.target + ".py")
    target_file = str(PROJECT_ROOT / target) if target.parent == Path(".") else str(target.resolve())
    # Chainlit reads these values on import, including its .env/config/public paths.
    os.environ["CHAINLIT_APP_ROOT"] = str(application_root())
    os.environ["CHAINLIT_ENV_FILE"] = runtime_path("CHAINLIT_ENV_FILE", ".env")

    import uvicorn

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
        from examguard.local_identity import ensure_local_secret, install_browser_identity

        ensure_local_secret()
        install_browser_identity(app)
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

    uvicorn.run(app, host=args.host, port=args.port, log_level="info", ws="auto")


if __name__ == "__main__":
    main()
