"""Runtime origin and authentication-cookie hardening."""

from __future__ import annotations

import os


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def auth_cookie_name() -> str:
    """Session cookie name; changing it abandons stale cookies users cannot
    clear from a downgraded (non-HTTPS) origin, e.g. a leftover Secure cookie
    that makes browsers silently drop every new login Set-Cookie."""
    name = os.environ.get("SAGE_AUTH_COOKIE_NAME", "access_token").strip()
    if not name or len(name) > 64 or not all(c.isalnum() or c in "-_" for c in name):
        raise ValueError("SAGE_AUTH_COOKIE_NAME must be 1-64 chars of [A-Za-z0-9_-]")
    return name


def _normalise_origin(origin: str) -> str:
    return origin.strip().rstrip("/")


def allowed_origins() -> set[str]:
    configured = os.environ.get("SAGE_ALLOWED_ORIGINS", "").strip()
    if configured:
        origins = {
            _normalise_origin(item)
            for item in configured.split(",")
            if _normalise_origin(item)
        }
        if origins:
            return origins

    port = os.environ.get("CHAINLIT_PORT", "8000").strip() or "8000"
    return {
        f"http://localhost:{port}",
        f"http://127.0.0.1:{port}",
    }


def configure_auth_cookies() -> None:
    """Apply explicit local/production cookie policy to Chainlit's cookie module."""
    from chainlit.auth import cookie

    same_site = os.environ.get("CHAINLIT_COOKIE_SAMESITE", "strict").strip().lower()
    if same_site not in {"strict", "lax", "none"}:
        raise ValueError("CHAINLIT_COOKIE_SAMESITE must be strict, lax, or none")
    cookie._cookie_samesite = same_site  # type: ignore[attr-defined]
    cookie._cookie_secure = _flag("SAGE_SECURE_COOKIES")  # type: ignore[attr-defined]
    cookie._auth_cookie_name = auth_cookie_name()  # type: ignore[attr-defined]


def install_origin_guard(app) -> None:
    """Reject browser cross-origin requests before Chainlit route handlers."""
    if getattr(app.state, "algosage_origin_guard_installed", False):
        return
    app.state.algosage_origin_guard_installed = True
    allowed = allowed_origins()

    @app.middleware("http")
    async def reject_disallowed_origin(request, call_next):
        origin = request.headers.get("origin", "").strip()
        if origin and _normalise_origin(origin) not in allowed:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                {"detail": "Cross-origin request rejected"}, status_code=403
            )
        return await call_next(request)
