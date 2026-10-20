"""Stable local-browser identity for chat history; this is not student authentication."""

from __future__ import annotations
import os
import secrets
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from uuid import UUID, uuid4

from .runtime_paths import runtime_path

_WORKSPACE_COOKIE = "eg_workspace"
_WORKSPACE_AUDIENCE = "examguard.local-workspace.v1"
_WORKSPACE_DAYS = 365


def ensure_local_secret(path: Path | None = None) -> str:
    existing = os.environ.get("CHAINLIT_AUTH_SECRET")
    if existing:
        return existing
    path = path or Path(runtime_path("EG_AUTH_SECRET_FILE", ".examguard/auth.secret"))
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


def _browser_identifier(identifier):
    if not isinstance(identifier, str) or not identifier.startswith("browser:"):
        return False
    try:
        UUID(identifier.removeprefix("browser:"))
    except ValueError:
        return False
    return True


def _auth_user(cookies):
    from chainlit.auth import decode_jwt, get_token_from_cookies

    token = get_token_from_cookies(cookies)
    if token:
        try:
            user = decode_jwt(token)
            if _browser_identifier(user.identifier):
                return user
        except Exception:
            pass
    return None


def _workspace_payload(cookies):
    import jwt

    try:
        payload = jwt.decode(
            cookies.get(_WORKSPACE_COOKIE, ""), os.environ["CHAINLIT_AUTH_SECRET"],
            algorithms=["HS256"], audience=_WORKSPACE_AUDIENCE,
            options={"require": ["sub", "exp", "aud"]},
        )
        return payload if _browser_identifier(payload["sub"]) else None
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return None


def browser_user(headers):
    from chainlit.user import User
    from starlette.requests import cookie_parser

    cookies = cookie_parser(headers.get("cookie", ""))
    # Migrate a valid existing login first, preserving history and risk ownership.
    if user := _auth_user(cookies):
        return user
    payload = _workspace_payload(cookies)
    return User(
        identifier=payload["sub"] if payload else "browser:" + str(uuid4()),
        display_name="本地工作区",
        metadata={"provider": "header", "identity": "local-browser"},
    )


def install_browser_identity(app):
    """Issue a separate durable credential, without extending API JWT validity."""
    import jwt

    @app.middleware("http")
    async def persist_workspace(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if response.status_code != 200 or path not in {"/user", "/auth/header"}:
            return response
        user = _auth_user(request.cookies)
        if path == "/auth/header":
            issued = SimpleCookie()
            for value in response.headers.getlist("set-cookie"):
                issued.load(value)
            user = _auth_user({key: value.value for key, value in issued.items()}) or user
        if not user:
            return response
        previous = _workspace_payload(request.cookies)
        now = datetime.now(timezone.utc)
        if (previous and previous["sub"] == user.identifier
                and previous["exp"] > (now + timedelta(days=30)).timestamp()):
            return response
        token = jwt.encode(
            {"sub": user.identifier, "aud": _WORKSPACE_AUDIENCE,
             "iat": now, "exp": now + timedelta(days=_WORKSPACE_DAYS)},
            os.environ["CHAINLIT_AUTH_SECRET"], algorithm="HS256",
        )
        response.set_cookie(
            _WORKSPACE_COOKIE, token, httponly=True,
            secure=request.url.scheme == "https", samesite="strict", path="/",
            max_age=_WORKSPACE_DAYS * 24 * 60 * 60,
        )
        response.headers["Cache-Control"] = "no-store"
        return response
