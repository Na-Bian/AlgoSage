"""Stable local-browser identity for chat history; this is not student authentication."""

from __future__ import annotations
import os
import secrets
from pathlib import Path
from uuid import uuid4


def ensure_local_secret(path: Path | None = None) -> str:
    existing = os.environ.get("CHAINLIT_AUTH_SECRET")
    if existing:
        return existing
    path = path or Path(os.environ.get("EG_AUTH_SECRET_FILE", ".examguard/auth.secret"))
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL prevents two launchers from silently using different identity secrets.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        value = path.read_text(encoding="utf-8").strip()
    else:
        value = secrets.token_urlsafe(48)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
    if len(value) < 40:
        raise ValueError("本地身份签名密钥无效")
    os.environ["CHAINLIT_AUTH_SECRET"] = value
    return value


def browser_user(headers):
    from chainlit.auth import decode_jwt, get_token_from_cookies
    from chainlit.user import User
    from starlette.requests import cookie_parser

    token = get_token_from_cookies(cookie_parser(headers.get("cookie", "")))
    if token:
        try:
            user = decode_jwt(token)
            if user.identifier.startswith("browser:"):
                return user
        except Exception:
            pass
    return User(
        identifier="browser:" + str(uuid4()),
        display_name="本地工作区",
        metadata={"provider": "header", "identity": "local-browser"},
    )
