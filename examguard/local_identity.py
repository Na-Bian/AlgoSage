"""Runtime secret helpers for the HUST-authenticated student deployment."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from .runtime_paths import runtime_path


def ensure_local_secret(path: Path | None = None) -> str:
    """Create or load the signing secret used by Chainlit login sessions."""
    existing = os.environ.get("CHAINLIT_AUTH_SECRET")
    if existing:
        return existing
    path = path or Path(runtime_path("EG_AUTH_SECRET_FILE", ".examguard/auth.secret"))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        value = path.read_text(encoding="utf-8").strip()
    else:
        value = secrets.token_urlsafe(48)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(value)
    if len(value) < 40:
        raise ValueError("本地身份签名密钥无效")
    os.environ["CHAINLIT_AUTH_SECRET"] = value
    return value
