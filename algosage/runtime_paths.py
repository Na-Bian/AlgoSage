"""Resolve runtime files against the application, never the launch directory."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def application_root() -> Path:
    root = Path(os.environ.get("CHAINLIT_APP_ROOT") or PROJECT_ROOT).expanduser()
    return (root if root.is_absolute() else PROJECT_ROOT / root).resolve()


def _migrate_legacy_runtime_root() -> None:
    """Move the pre-rename AlgoSage runtime directory once."""
    root = application_root()
    legacy = root / ".examguard"
    current = root / ".algosage"
    if legacy.is_dir() and not current.exists():
        try:
            legacy.rename(current)
        except OSError:
            # A second worker may have completed the rename; leave both intact
            # rather than deleting user chat/security data.
            pass


def runtime_path(name: str, default: str) -> str:
    _migrate_legacy_runtime_root()
    value = os.environ.get(name, default).strip()
    if value in {"", ":memory:"}:
        return value
    path = Path(value).expanduser()
    return str((path if path.is_absolute() else application_root() / path).resolve())
