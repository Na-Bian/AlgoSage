"""HUST student accounts, email verification codes, and bounded rate limits."""

from __future__ import annotations

import hashlib
import hmac
import io
import asyncio
import json
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from argon2 import PasswordHasher
from fastapi import Request
from PIL import Image as PillowImage
from pydantic import BaseModel
from chainlit.server import UserParam
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .emailer import EmailMessage, EmailSender
from .runtime_paths import protect_runtime_file

_HUST_PATTERN = re.compile(r"^U202[0-9]1[0-9]{4}$")
_CODE_ALPHABET = "0123456789"
_AVATAR_SIZE = 256
_MAX_AVATAR_UPLOAD = 5 * 1024 * 1024


class AuthValidationError(ValueError):
    pass


class AuthRateLimitError(RuntimeError):
    pass


class AuthCodeError(PermissionError):
    pass


class AuthCredentialsError(PermissionError):
    pass


@dataclass(frozen=True)
class AuthConfig:
    mode: str = "hust"
    email_domain: str = "hust.edu.cn"
    student_id_pattern: str = r"^U202[0-9]1[0-9]{4}$"
    password_min_length: int = 12
    code_ttl_seconds: int = 600
    code_max_attempts: int = 5
    resend_interval_seconds: int = 60
    account_hourly_limit: int = 5
    ip_hourly_limit: int = 10
    lockout_attempts: int = 5
    lockout_seconds: int = 900

    @classmethod
    def from_env(cls) -> "AuthConfig":
        return cls(
            mode="hust",
            email_domain=os.environ.get("SAGE_STUDENT_EMAIL_DOMAIN", "hust.edu.cn")
            .strip()
            .lower(),
            student_id_pattern=os.environ.get(
                "SAGE_STUDENT_ID_PATTERN", cls.student_id_pattern
            ),
            password_min_length=int(
                os.environ.get("SAGE_PASSWORD_MIN_LENGTH", cls.password_min_length)
            ),
            code_ttl_seconds=int(
                os.environ.get("SAGE_AUTH_CODE_TTL_SECONDS", cls.code_ttl_seconds)
            ),
            code_max_attempts=int(
                os.environ.get("SAGE_AUTH_CODE_MAX_ATTEMPTS", cls.code_max_attempts)
            ),
            resend_interval_seconds=int(
                os.environ.get(
                    "SAGE_AUTH_CODE_RESEND_SECONDS", cls.resend_interval_seconds
                )
            ),
            account_hourly_limit=int(
                os.environ.get(
                    "SAGE_AUTH_HOURLY_ACCOUNT_LIMIT", cls.account_hourly_limit
                )
            ),
            ip_hourly_limit=int(
                os.environ.get("SAGE_AUTH_HOURLY_IP_LIMIT", cls.ip_hourly_limit)
            ),
            lockout_attempts=int(
                os.environ.get("SAGE_AUTH_LOCKOUT_ATTEMPTS", cls.lockout_attempts)
            ),
            lockout_seconds=int(
                os.environ.get("SAGE_AUTH_LOCKOUT_SECONDS", cls.lockout_seconds)
            ),
        )

    def validate(self) -> None:
        if self.mode != "hust":
            raise ValueError("AlgoSage authentication is HUST-only")
        re.compile(self.student_id_pattern)
        if self.password_min_length < 12:
            raise ValueError("password minimum length cannot be below 12")
        if not (300 <= self.code_ttl_seconds <= 900):
            raise ValueError("verification code lifetime must be 5-15 minutes")
        if self.code_max_attempts < 1:
            raise ValueError("verification code attempts must be positive")
        if self.resend_interval_seconds < 30:
            raise ValueError("verification resend interval must be at least 30 seconds")


@dataclass(frozen=True)
class AuthenticatedStudent:
    student_id: str
    nickname: str
    session_version: int

    @property
    def identifier(self) -> str:
        return f"student:{self.student_id}"

    @property
    def email(self) -> str:
        return f"{self.student_id}@hust.edu.cn"


class RegistrationCodeRequest(BaseModel):
    nickname: str
    student_id: str
    password: str


class RegistrationConfirmRequest(BaseModel):
    student_id: str
    code: str


class ResetCodeRequest(BaseModel):
    student_id: str


class ResetConfirmRequest(BaseModel):
    student_id: str
    code: str
    new_password: str


class ProfileUpdateRequest(BaseModel):
    nickname: str
    current_password: str | None = None
    new_password: str | None = None


class AccountClosureRequest(BaseModel):
    current_password: str
    confirmation: str


def _student_from_request(request: Request) -> Any:
    from chainlit.auth import decode_jwt, get_token_from_cookies

    token = get_token_from_cookies(request.cookies)
    if not token:
        return None
    try:
        user = decode_jwt(token)
    except Exception:
        return None
    identifier = getattr(user, "identifier", "")
    return user if identifier.startswith("student:") else None


def install_profile_routes(
    app: Any,
    student_auth_factory,
    chat_store_factory=None,
    state_store_factory=None,
    personal_state_factory=None,
) -> None:
    """Install HUST profile APIs, avatar delivery, and the standalone profile page."""
    if getattr(app.state, "algosage_profile_routes_installed", False):
        return
    app.state.algosage_profile_routes_installed = True

    router = _chainlit_router(app)

    from fastapi import HTTPException, Request, Response
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

    profile_html_path = (
        Path(__file__).resolve().parent.parent / "public" / "profile-page.html"
    )
    security_html_path = (
        Path(__file__).resolve().parent.parent / "public" / "security-page.html"
    )
    auth_html_path = (
        Path(__file__).resolve().parent.parent / "public" / "auth-page.html"
    )

    def student_auth():
        return student_auth_factory()

    def _student_id(current_user: Any) -> str:
        identifier = getattr(current_user, "identifier", "")
        if not identifier.startswith("student:"):
            raise HTTPException(status_code=403, detail="HUST student account required")
        return identifier.removeprefix("student:")

    def _profile_payload(account: dict[str, Any]) -> dict[str, Any]:
        avatar_updated_at = account.get("avatar_updated_at") or ""
        has_avatar = bool(account.get("avatar_mime"))
        return {
            "mode": "hust",
            "identifier": f"student:{account['student_id']}",
            "nickname": account["nickname"],
            "student_id": account["student_id"],
            "email": account["email"],
            "created_at": account["created_at"],
            "updated_at": account["updated_at"],
            "session_version": int(account["session_version"]),
            "can_change_password": True,
            "has_avatar": has_avatar,
            "avatar_updated_at": avatar_updated_at,
            "avatar_url": (
                f"/auth/avatar/me?v={avatar_updated_at}" if has_avatar else ""
            ),
        }

    def _issue_profile_cookie(
        request: Request, account: dict[str, Any], payload: dict[str, Any]
    ):
        from chainlit.auth import create_jwt
        from chainlit.auth.cookie import set_auth_cookie
        from chainlit.user import User

        avatar_url = (
            f"/auth/avatar/me?v={account.get('avatar_updated_at') or ''}"
            if account.get("avatar_mime")
            else ""
        )
        user = User(
            identifier=f"student:{account['student_id']}",
            display_name=account["nickname"],
            metadata={
                "provider": "hust-student",
                "student_id": account["student_id"],
                "session_version": int(account["session_version"]),
                "image": avatar_url,
            },
        )
        response = JSONResponse(payload)
        set_auth_cookie(request, response, create_jwt(user))
        return response

    @router.get("/profile", include_in_schema=False)
    async def profile_page(request: Request):
        current_user = _student_from_request(request)
        if not current_user:
            return RedirectResponse(url="/login", status_code=303)
        account = student_auth().store.get_account(_student_id(current_user))
        if not account:
            response = RedirectResponse(url="/login", status_code=303)
            response.delete_cookie("access_token", path="/")
            return response
        return HTMLResponse(profile_html_path.read_text(encoding="utf-8"))

    @router.get("/profile/security", include_in_schema=False)
    async def account_security_page(request: Request):
        current_user = _student_from_request(request)
        if not current_user:
            return RedirectResponse(url="/login", status_code=303)
        account = student_auth().store.get_account(_student_id(current_user))
        if not account:
            response = RedirectResponse(url="/login", status_code=303)
            response.delete_cookie("access_token", path="/")
            return response
        return HTMLResponse(security_html_path.read_text(encoding="utf-8"))

    @router.get("/auth/avatar/me", include_in_schema=False)
    async def read_avatar(current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        avatar = student_auth().store.get_avatar(_student_id(current_user))
        if not avatar:
            raise HTTPException(status_code=404, detail="Avatar not found")
        content, mime = avatar
        return Response(
            content=content,
            media_type=mime,
            headers={"Cache-Control": "no-store"},
        )

    @router.put("/auth/avatar", include_in_schema=False)
    async def update_avatar(request: Request, current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        student_id = _student_id(current_user)
        origin = request.headers.get("origin", "")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Cross-origin request rejected")
        content = await request.body()
        try:
            account = student_auth().update_avatar(
                student_id=student_id, content=content
            )
        except AuthValidationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        payload = _profile_payload(account)
        return _issue_profile_cookie(request, account, payload)

    @router.get("/auth/profile")
    async def read_profile(request: Request, current_user: UserParam):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        account = student_auth().store.get_account(_student_id(current_user))
        if not account:
            raise HTTPException(status_code=404, detail="Profile not found")
        avatar_url = (
            f"/auth/avatar/me?v={account['avatar_updated_at']}"
            if account.get("avatar_mime")
            else ""
        )
        principal_id = _student_id(current_user)
        if chat_store_factory is not None:
            identifier = f"student:{principal_id}"
            previous_user = await chat_store_factory().get_user(identifier)
            app_preferences = (
                (previous_user.metadata or {}).get("app_preferences")
                if previous_user
                else None
            )
            auth_metadata = {
                "provider": "hust-student",
                "student_id": account["student_id"],
                "session_version": int(account["session_version"]),
                "image": avatar_url,
            }
            if isinstance(app_preferences, dict):
                auth_metadata["app_preferences"] = app_preferences
            await chat_store_factory().update_user_metadata(
                identifier,
                auth_metadata,
                account["nickname"],
            )
        # Re-sign the session so a pre-avatar login cookie cannot keep asking
        # for an image that was never uploaded.
        return _issue_profile_cookie(request, account, _profile_payload(account))

    @router.post("/auth/profile/close", include_in_schema=False)
    async def close_account(
        payload: AccountClosureRequest, request: Request, current_user: UserParam
    ):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        student_id = _student_id(current_user)
        origin = request.headers.get("origin", "")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Cross-origin request rejected")
        try:
            result = student_auth().close_account(
                student_id=student_id,
                current_password=payload.current_password,
                confirmation=payload.confirmation,
            )
            if chat_store_factory is not None:
                await chat_store_factory().delete_all_personal_data(
                    f"student:{student_id}"
                )
            if state_store_factory is not None:
                state_store_factory().delete_principal(
                    "personal", f"principal:{student_id}"
                )
            if personal_state_factory is not None:
                learning_state = personal_state_factory()
                await asyncio.to_thread(
                    learning_state.forget_personal_state,
                    learning_state.settings.exam_id,
                    student_id,
                )
                if state_store_factory is not None:
                    state_store_factory().delete_principal(
                        learning_state.settings.exam_id,
                        f"principal:{student_id}",
                    )
        except AuthValidationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except AuthCredentialsError as error:
            raise HTTPException(status_code=423, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        response = JSONResponse(result)
        response.delete_cookie("access_token", path="/")
        return response

    @router.patch("/auth/profile")
    async def update_profile(
        payload: ProfileUpdateRequest, request: Request, current_user: UserParam
    ):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        student_id = _student_id(current_user)
        origin = request.headers.get("origin", "")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Cross-origin request rejected")
        try:
            account = student_auth().update_profile(
                student_id=student_id,
                nickname=payload.nickname,
                current_password=payload.current_password,
                new_password=payload.new_password,
            )
        except AuthValidationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except AuthCredentialsError as error:
            raise HTTPException(status_code=423, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        payload = _profile_payload(account)
        return _issue_profile_cookie(request, account, payload)

    auth_html_path = (
        Path(__file__).resolve().parent.parent / "public" / "auth-page.html"
    )

    def auth_page(mode: str):
        html = auth_html_path.read_text(encoding="utf-8")
        titles = {
            "login": "登录 AlgoSage",
            "register": "注册 AlgoSage",
            "reset": "找回密码",
        }
        page_title = {
            "login": "登录 · AlgoSage",
            "register": "注册 · AlgoSage",
            "reset": "找回密码 · AlgoSage",
        }
        # ICP 备案号：备案通过后配置 SAGE_ICP_NUMBER，页脚自动展示；空则隐藏。
        icp = os.environ.get("SAGE_ICP_NUMBER", "").strip()
        icp_html = (
            f'<a href="https://beian.miit.gov.cn" target="_blank" rel="noopener">'
            f'{icp}</a>'
            if icp
            else ""
        )
        rendered = html.replace('data-view="login"', f'data-view="{mode}"')
        rendered = rendered.replace(
            ">登录 AlgoSage</h1>", f">{titles[mode]}</h1>"
        ).replace("<title>登录 · AlgoSage</title>", f"<title>{page_title[mode]}</title>")
        rendered = rendered.replace(
            '<footer class="auth-icp" data-icp-slot hidden>',
            f'<footer class="auth-icp" data-icp-slot{" hidden" if not icp_html else ""}>{icp_html}</footer>',
        )
        return HTMLResponse(rendered)

    @router.get("/login", include_in_schema=False)
    async def custom_login_page(request: Request):
        if _student_from_request(request):
            return RedirectResponse(url="/", status_code=303)
        return auth_page("login")

    @router.get("/register", include_in_schema=False)
    async def custom_register_page(request: Request):
        if _student_from_request(request):
            return RedirectResponse(url="/", status_code=303)
        return auth_page("register")

    @router.get("/forgot-password", include_in_schema=False)
    async def custom_forgot_password_page(request: Request):
        if _student_from_request(request):
            return RedirectResponse(url="/", status_code=303)
        return auth_page("reset")

    _move_routes_before_catchall(
        router,
        (
            "/auth/profile",
            "/auth/avatar",
            "/profile",
            "/login",
            "/register",
            "/forgot-password",
        ),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _is_expired(value: str | None) -> bool:
    if not value:
        return True
    try:
        return datetime.fromisoformat(value) <= _now()
    except ValueError:
        return True


class StudentAuthStore:
    """SQLite storage for student accounts and one-time verification codes."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS student_accounts (
                student_id TEXT PRIMARY KEY,
                nickname TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                email_verified_at TEXT,
                session_version INTEGER NOT NULL DEFAULT 1,
                failed_login_count INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verification_codes (
                id TEXT PRIMARY KEY,
                purpose TEXT NOT NULL,
                student_id TEXT NOT NULL,
                email TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                extra_json TEXT,
                ip_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                consumed_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_auth_codes_target_time
                ON verification_codes(purpose, student_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_auth_codes_ip_time
                ON verification_codes(ip_hash, created_at DESC);
            """)
        self._conn.commit()
        columns = {
            row["name"]
            for row in self._conn.execute(
                "PRAGMA table_info(student_accounts)"
            ).fetchall()
        }
        migrations = {
            "avatar_mime": "ALTER TABLE student_accounts ADD COLUMN avatar_mime TEXT",
            "avatar_data": "ALTER TABLE student_accounts ADD COLUMN avatar_data BLOB",
            "avatar_updated_at": "ALTER TABLE student_accounts ADD COLUMN avatar_updated_at TEXT",
        }
        for column, statement in migrations.items():
            if column not in columns:
                self._conn.execute(statement)
        self._conn.commit()
        if path != ":memory:":
            protect_runtime_file(path)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_account(self, student_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT student_id,nickname,email,password_hash,status,email_verified_at,
                          session_version,failed_login_count,locked_until,created_at,updated_at,
                          avatar_mime,avatar_updated_at
                   FROM student_accounts WHERE student_id = ?""",
                (student_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_avatar(self, student_id: str) -> tuple[bytes, str] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT avatar_data,avatar_mime FROM student_accounts WHERE student_id=?",
                (student_id,),
            ).fetchone()
        if not row or not row["avatar_data"]:
            return None
        return bytes(row["avatar_data"]), row["avatar_mime"] or "image/png"

    def set_avatar(self, student_id: str, content: bytes, mime: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE student_accounts SET avatar_data=?,avatar_mime=?,avatar_updated_at=?,updated_at=? "
                "WHERE student_id=?",
                (content, mime, _iso(_now()), _iso(_now()), student_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def create_account(
        self,
        *,
        student_id: str,
        nickname: str,
        email: str,
        password_hash: str,
    ) -> dict[str, Any]:
        created = _iso(_now())
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO student_accounts(student_id,nickname,email,password_hash,status,email_verified_at,created_at,updated_at) "
                    "VALUES(?,?,?,?, 'active', ?, ?, ?)",
                    (
                        student_id,
                        nickname,
                        email,
                        password_hash,
                        created,
                        created,
                        created,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as error:
                raise PermissionError("student account cannot be created") from error
        return self.get_account(student_id)  # type: ignore[return-value]

    def latest_code(
        self, purpose: str, student_id: str, created_after: datetime
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM verification_codes WHERE purpose=? AND student_id=? AND created_at>=? "
                "ORDER BY created_at DESC LIMIT 1",
                (purpose, student_id, _iso(created_after)),
            ).fetchone()
        return dict(row) if row else None

    def count_recent_codes(
        self, *, purpose: str, student_id: str, ip_hash: str, since: datetime
    ) -> tuple[int, int]:
        with self._lock:
            account_count = self._conn.execute(
                "SELECT COUNT(*) FROM verification_codes WHERE purpose=? AND student_id=? AND created_at>=?",
                (purpose, student_id, _iso(since)),
            ).fetchone()[0]
            ip_count = self._conn.execute(
                "SELECT COUNT(*) FROM verification_codes WHERE purpose=? AND ip_hash=? AND created_at>=?",
                (purpose, ip_hash, _iso(since)),
            ).fetchone()[0]
        return int(account_count), int(ip_count)

    def save_code(
        self,
        *,
        purpose: str,
        student_id: str,
        email: str,
        code_hash: str,
        ip_hash: str,
        expires_at: str,
        extra: dict[str, Any] | None = None,
    ) -> str:
        code_id = str(uuid4())
        with self._lock:
            self._conn.execute(
                "INSERT INTO verification_codes(id,purpose,student_id,email,code_hash,extra_json,ip_hash,expires_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    code_id,
                    purpose,
                    student_id,
                    email,
                    code_hash,
                    json.dumps(extra or {}, ensure_ascii=False, separators=(",", ":")),
                    ip_hash,
                    expires_at,
                    _iso(_now()),
                ),
            )
            self._conn.commit()
        return code_id

    def consume_code(
        self, row: dict[str, Any], expected_hash: str, max_attempts: int
    ) -> bool:
        with self._lock:
            current = self._conn.execute(
                "SELECT attempts, consumed_at, code_hash, expires_at FROM verification_codes WHERE id=?",
                (row["id"],),
            ).fetchone()
            if (
                current is None
                or current["consumed_at"]
                or _is_expired(current["expires_at"])
            ):
                return False
            if int(current["attempts"]) >= max_attempts:
                return False
            if not hmac.compare_digest(str(current["code_hash"]), expected_hash):
                self._conn.execute(
                    "UPDATE verification_codes SET attempts=attempts+1 WHERE id=?",
                    (row["id"],),
                )
                self._conn.commit()
                return False
            self._conn.execute(
                "UPDATE verification_codes SET consumed_at=?, attempts=attempts+1 WHERE id=?",
                (_iso(_now()), row["id"]),
            )
            self._conn.commit()
        return True

    def close_account(self, student_id: str) -> bool:
        """Close an account, invalidate sessions, and minimize retained identity."""
        changed = _iso(_now())
        with self._lock:
            self._conn.execute(
                "DELETE FROM verification_codes WHERE student_id=?",
                (student_id,),
            )
            cursor = self._conn.execute(
                "UPDATE student_accounts SET status='closed', failed_login_count=0, locked_until=NULL, "
                "nickname='已注销用户', email='closed.'||hex(randomblob(12))||'@invalid', "
                "password_hash='!closed-account', "
                "avatar_data=NULL, avatar_mime=NULL, avatar_updated_at='', "
                "session_version=session_version+1, updated_at=? WHERE student_id=? AND status='active'",
                (changed, student_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def update_password(self, student_id: str, password_hash: str) -> bool:
        changed = _iso(_now())
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE student_accounts SET password_hash=?, failed_login_count=0, locked_until=NULL, "
                "session_version=session_version+1, updated_at=? WHERE student_id=?",
                (password_hash, changed, student_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def update_nickname(self, student_id: str, nickname: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE student_accounts SET nickname=?, updated_at=? WHERE student_id=?",
                (nickname, _iso(_now()), student_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def mark_login_failure(
        self, student_id: str, max_attempts: int, lock_seconds: int
    ) -> int:
        now = _now()
        locked_until = _iso(now + timedelta(seconds=lock_seconds))
        with self._lock:
            row = self._conn.execute(
                "SELECT failed_login_count FROM student_accounts WHERE student_id=?",
                (student_id,),
            ).fetchone()
            if row is None:
                return 0
            count = int(row["failed_login_count"]) + 1
            self._conn.execute(
                "UPDATE student_accounts SET failed_login_count=?, locked_until=? WHERE student_id=?",
                (count, locked_until if count >= max_attempts else None, student_id),
            )
            self._conn.commit()
        return count

    def mark_login_success(self, student_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE student_accounts SET failed_login_count=0, locked_until=NULL WHERE student_id=?",
                (student_id,),
            )
            self._conn.commit()


class StudentAuthService:
    def __init__(
        self,
        *,
        store: StudentAuthStore,
        email_sender: EmailSender,
        config: AuthConfig,
        code_pepper: str,
    ):
        self.store = store
        self.email_sender = email_sender
        self.config = config
        self.config.validate()
        self._student_id_pattern = re.compile(config.student_id_pattern)
        self._hasher = PasswordHasher(
            time_cost=2, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16
        )
        if not code_pepper or len(code_pepper) < 32:
            raise ValueError("student auth code pepper is too short")
        self._code_pepper = code_pepper.encode()

    def email_for(self, student_id: str) -> str:
        return f"{student_id}@{self.config.email_domain}"

    def _validate_student_id(self, student_id: str) -> str:
        value = student_id.strip().upper()
        if not self._student_id_pattern.fullmatch(value):
            raise AuthValidationError("学号格式不正确")
        return value

    def _validate_password(self, password: str, student_id: str, nickname: str) -> None:
        if len(password) < self.config.password_min_length:
            raise AuthValidationError(f"密码至少 {self.config.password_min_length} 位")
        lower_password = password.casefold()
        if (
            student_id.casefold() in lower_password
            or nickname.casefold() in lower_password
        ):
            raise AuthValidationError("密码不能包含学号或昵称")

    def _check_send_limits(self, purpose: str, student_id: str, ip: str) -> None:
        now = _now()
        latest = self.store.latest_code(
            purpose,
            student_id,
            now - timedelta(seconds=self.config.resend_interval_seconds),
        )
        if latest:
            raise AuthRateLimitError("发送太频繁，请稍后再试")
        since = now - timedelta(hours=1)
        account_count, ip_count = self.store.count_recent_codes(
            purpose=purpose,
            student_id=student_id,
            ip_hash=self._ip_hash(ip),
            since=since,
        )
        if account_count >= self.config.account_hourly_limit:
            raise AuthRateLimitError("验证码请求次数已达上限")
        if ip_count >= self.config.ip_hourly_limit:
            raise AuthRateLimitError("当前网络请求次数已达上限")

    def _ip_hash(self, ip: str) -> str:
        return hmac.new(self._code_pepper, ip.encode(), hashlib.sha256).hexdigest()

    def _code_hash(self, code: str) -> str:
        return hmac.new(self._code_pepper, code.encode(), hashlib.sha256).hexdigest()

    def _generate_code(self) -> str:
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))

    async def _send_code(
        self,
        *,
        purpose: str,
        title: str,
        student_id: str,
        ip: str,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        self._check_send_limits(purpose, student_id, ip)
        code = self._generate_code()
        email = self.email_for(student_id)
        mail = EmailMessage(
            to_address=email,
            subject=f"{title}验证码：{code}",
            text_body=(
                f"你的 AlgoSage {title}验证码是：{code}\n\n"
                f"验证码 {self.config.code_ttl_seconds // 60} 分钟内有效。"
                "如果不是你发起的操作，请忽略这封邮件。\n"
            ),
        )
        # Send first so mail-provider failure does not consume the resend window.
        await self.email_sender.send(mail)
        expires = _iso(_now() + timedelta(seconds=self.config.code_ttl_seconds))
        self.store.save_code(
            purpose=purpose,
            student_id=student_id,
            email=email,
            code_hash=self._code_hash(code),
            ip_hash=self._ip_hash(ip),
            expires_at=expires,
            extra=extra,
        )
        return True

    async def request_registration_code(
        self, *, nickname: str, student_id: str, password: str, ip: str
    ) -> None:
        nickname = nickname.strip()
        clean_student_id = self._validate_student_id(student_id)
        # Reject obvious repeats before running Argon2 for attacker-supplied input.
        self._check_send_limits("register", clean_student_id, ip)
        if not 1 <= len(nickname) <= 40:
            raise AuthValidationError("昵称长度需为 1-40 个字符")
        self._validate_password(password, clean_student_id, nickname)
        password_hash = self._hasher.hash(password)
        # Registration responses intentionally do not reveal whether the account exists.
        await self._send_code(
            purpose="register",
            title="注册",
            student_id=clean_student_id,
            ip=ip,
            extra={"nickname": nickname, "password_hash": password_hash},
        )

    def confirm_registration(
        self, *, student_id: str, code: str
    ) -> AuthenticatedStudent:
        clean_student_id = self._validate_student_id(student_id)
        account = self.store.get_account(clean_student_id)
        if account:
            raise AuthCodeError("验证码无效或已过期")
        row = self.store.latest_code(
            "register",
            clean_student_id,
            _now() - timedelta(seconds=self.config.code_ttl_seconds * 2),
        )
        if not row:
            raise AuthCodeError("验证码无效或已过期")
        if not self.store.consume_code(
            row, self._code_hash(code), self.config.code_max_attempts
        ):
            raise AuthCodeError("验证码无效或已过期")
        extra = json.loads(row["extra_json"] or "{}")
        nickname = str(extra.get("nickname", "")).strip()
        password_hash = str(extra.get("password_hash", ""))
        if not nickname or not password_hash:
            raise AuthCodeError("验证码无效或已过期")
        created = self.store.create_account(
            student_id=clean_student_id,
            nickname=nickname,
            email=self.email_for(clean_student_id),
            password_hash=password_hash,
        )
        return AuthenticatedStudent(
            student_id=created["student_id"],
            nickname=created["nickname"],
            session_version=int(created["session_version"]),
        )

    async def request_password_reset_code(self, *, student_id: str, ip: str) -> None:
        clean_student_id = self._validate_student_id(student_id)
        self._check_send_limits("password_reset", clean_student_id, ip)
        # No code is generated for an unknown account, but the response remains generic.
        if not self.store.get_account(clean_student_id):
            # Persist the attempt so unknown IDs cannot bypass request limits.
            self.store.save_code(
                purpose="password_reset",
                student_id=clean_student_id,
                email=self.email_for(clean_student_id),
                code_hash=self._code_hash(self._generate_code()),
                ip_hash=self._ip_hash(ip),
                expires_at=_iso(
                    _now() + timedelta(seconds=self.config.code_ttl_seconds)
                ),
            )
            return
        await self._send_code(
            purpose="password_reset",
            title="密码重置",
            student_id=clean_student_id,
            ip=ip,
        )

    def confirm_password_reset(
        self, *, student_id: str, code: str, new_password: str
    ) -> None:
        clean_student_id = self._validate_student_id(student_id)
        account = self.store.get_account(clean_student_id)
        if not account:
            raise AuthCodeError("验证码无效或已过期")
        self._validate_password(new_password, clean_student_id, account["nickname"])
        row = self.store.latest_code(
            "password_reset",
            clean_student_id,
            _now() - timedelta(seconds=self.config.code_ttl_seconds * 2),
        )
        if not row:
            raise AuthCodeError("验证码无效或已过期")
        if not self.store.consume_code(
            row, self._code_hash(code), self.config.code_max_attempts
        ):
            raise AuthCodeError("验证码无效或已过期")
        self.store.update_password(clean_student_id, self._hasher.hash(new_password))

    def authenticate(
        self, student_id: str, password: str
    ) -> AuthenticatedStudent | None:
        clean_student_id = student_id.strip().upper()
        account = self.store.get_account(clean_student_id)
        if not account:
            # Keep missing and incorrect passwords on a similar hashing path.
            try:
                self._hasher.verify(
                    "$argon2id$v=19$m=65536,t=2,p=1$c29tZXNhbHQ$invalid", password
                )
            except Exception:
                pass
            return None
        if account["locked_until"] and not _is_expired(account["locked_until"]):
            raise AuthCredentialsError("账号已暂时锁定，请稍后再试")
        try:
            self._hasher.verify(account["password_hash"], password)
        except (VerifyMismatchError, InvalidHashError):
            failures = self.store.mark_login_failure(
                clean_student_id,
                self.config.lockout_attempts,
                self.config.lockout_seconds,
            )
            if failures >= self.config.lockout_attempts:
                raise AuthCredentialsError("账号已暂时锁定，请稍后再试") from None
            return None
        if account["status"] != "active":
            return None
        self.store.mark_login_success(clean_student_id)
        return AuthenticatedStudent(
            student_id=account["student_id"],
            nickname=account["nickname"],
            session_version=int(account["session_version"]),
        )

    def close_account(
        self,
        *,
        student_id: str,
        current_password: str,
        confirmation: str,
    ) -> dict[str, Any]:
        account = self.store.get_account(student_id)
        if not account:
            raise PermissionError("student account was not found")
        if confirmation != "注销账号":
            raise AuthValidationError("请输入“注销账号”确认操作")
        try:
            self._hasher.verify(account["password_hash"], current_password)
        except (VerifyMismatchError, InvalidHashError) as error:
            raise AuthCredentialsError("当前密码不正确") from error
        if not self.store.close_account(student_id):
            raise PermissionError("student account was not found")
        return {"status": "closed", "redirect_url": "/login"}

    def update_profile(
        self,
        *,
        student_id: str,
        nickname: str,
        current_password: str | None = None,
        new_password: str | None = None,
    ) -> dict[str, Any]:
        account = self.store.get_account(student_id)
        if not account:
            raise PermissionError("student account was not found")
        nickname = nickname.strip()
        if not 1 <= len(nickname) <= 40:
            raise AuthValidationError("昵称长度需为 1-40 个字符")

        password_changed = new_password is not None
        if password_changed:
            if not current_password:
                raise AuthValidationError("修改密码需要当前密码")
            try:
                self._hasher.verify(account["password_hash"], current_password)
            except (VerifyMismatchError, InvalidHashError) as error:
                raise AuthCredentialsError("当前密码不正确") from error
            self._validate_password(new_password, student_id, nickname)

        if nickname != account["nickname"]:
            if not self.store.update_nickname(student_id, nickname):
                raise PermissionError("student account was not found")
        if password_changed:
            self.store.update_password(student_id, self._hasher.hash(new_password))
        updated = self.store.get_account(student_id)
        if not updated:
            raise PermissionError("student account was not found")
        return updated

    def update_avatar(self, *, student_id: str, content: bytes) -> dict[str, Any]:
        if not content:
            raise AuthValidationError("请选择头像文件")
        if len(content) > _MAX_AVATAR_UPLOAD:
            raise AuthValidationError("头像文件不能超过 5MB")
        try:
            with PillowImage.open(io.BytesIO(content)) as source:
                source.load()
                image = source.convert("RGBA")
        except Exception as error:
            raise AuthValidationError("头像仅支持 PNG、JPEG 或 WebP") from error

        # Center-crop before resizing, so mobile photos do not become stretched.
        width, height = image.size
        edge = min(width, height)
        left = (width - edge) // 2
        top = (height - edge) // 2
        avatar = image.crop((left, top, left + edge, top + edge)).resize(
            (_AVATAR_SIZE, _AVATAR_SIZE), PillowImage.Resampling.LANCZOS
        )
        output = io.BytesIO()
        avatar.save(output, format="PNG", optimize=True)
        normalized = output.getvalue()
        if not self.store.set_avatar(student_id, normalized, "image/png"):
            raise PermissionError("student account was not found")
        updated = self.store.get_account(student_id)
        if not updated:
            raise PermissionError("student account was not found")
        return updated


def auth_service_from_env() -> StudentAuthService:
    from .emailer import email_sender_from_env

    config = AuthConfig.from_env()
    if config.mode != "hust":
        raise ValueError("student authentication service is not configured")
    secret = os.environ.get("CHAINLIT_AUTH_SECRET", "")
    if not secret:
        raise ValueError("CHAINLIT_AUTH_SECRET is required for HUST authentication")
    return StudentAuthService(
        store=StudentAuthStore(runtime_auth_db()),
        email_sender=email_sender_from_env(),
        config=config,
        code_pepper=secret,
    )


def runtime_auth_db() -> str:
    from .runtime_paths import runtime_path

    return runtime_path("SAGE_AUTH_DB_FILE", ".algosage/auth.sqlite3")


def _client_ip(request: Any) -> str:
    if os.environ.get("SAGE_TRUST_PROXY_HEADERS", "").lower() in {"true", "1", "yes"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return getattr(getattr(request, "client", None), "host", "") or "unknown"


def _chainlit_router(app: Any):
    """Return the router that owns Chainlit's UI catch-all route."""
    included = next(
        (route for route in app.routes if type(route).__name__ == "_IncludedRouter"),
        None,
    )
    if included is None:
        return app.router
    return included.original_router


def _move_routes_before_catchall(router: Any, prefixes: tuple[str, ...]) -> None:
    owned = [
        (index, route)
        for index, route in enumerate(router.routes)
        if getattr(route, "path", "").startswith(prefixes)
    ]
    if not owned:
        return
    owned_indices = {index for index, route in owned}
    routes = [route for index, route in owned]
    remaining = [
        route for index, route in enumerate(router.routes) if index not in owned_indices
    ]
    catchall_index = next(
        (
            index
            for index, route in enumerate(remaining)
            if getattr(route, "path", "") == "/{full_path:path}"
        ),
        len(remaining),
    )
    router.routes[:] = remaining[:catchall_index] + routes + remaining[catchall_index:]


def install_student_auth(app: Any, service_factory) -> None:
    """Install JSON endpoints and simple same-origin registration/reset pages."""
    if getattr(app.state, "algosage_student_auth_installed", False):
        return
    app.state.algosage_student_auth_installed = True

    router = _chainlit_router(app)

    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

    def service():
        return service_factory()

    def failure(error: Exception) -> HTTPException:
        if isinstance(error, AuthValidationError):
            return HTTPException(status_code=400, detail=str(error))
        if isinstance(error, AuthRateLimitError):
            return HTTPException(status_code=429, detail=str(error))
        if isinstance(error, AuthCodeError):
            return HTTPException(status_code=400, detail=str(error))
        if isinstance(error, AuthCredentialsError):
            return HTTPException(status_code=423, detail=str(error))
        return HTTPException(status_code=503, detail="请求暂未完成，请稍后再试")

    @router.get("/auth/hust/register", include_in_schema=False)
    async def legacy_register_page():
        return RedirectResponse(url="/register", status_code=301)

    @router.post("/auth/hust/register/request-code")
    async def register_code(payload: RegistrationCodeRequest, request: Request):
        try:
            await service().request_registration_code(
                nickname=payload.nickname,
                student_id=payload.student_id,
                password=payload.password,
                ip=_client_ip(request),
            )
        except Exception as error:
            if isinstance(error, (AuthValidationError, AuthRateLimitError)):
                raise failure(error) from error
            raise failure(error) from error
        return {"status": "sent"}

    @router.post("/auth/hust/register/confirm")
    async def register_confirm(payload: RegistrationConfirmRequest):
        try:
            account = service().confirm_registration(
                student_id=payload.student_id, code=payload.code
            )
        except Exception as error:
            raise failure(error) from error
        return {"status": "created", "identifier": account.identifier}

    @router.get("/auth/hust/forgot-password", include_in_schema=False)
    async def legacy_reset_page():
        return RedirectResponse(url="/forgot-password", status_code=301)

    @router.post("/auth/hust/password-reset/request-code")
    async def password_reset_code(payload: ResetCodeRequest, request: Request):
        try:
            await service().request_password_reset_code(
                student_id=payload.student_id, ip=_client_ip(request)
            )
        except Exception as error:
            raise failure(error) from error
        # Unknown accounts get the same successful shape as existing accounts.
        return {"status": "sent"}

    @router.post("/auth/hust/password-reset/confirm")
    async def password_reset_confirm(payload: ResetConfirmRequest):
        try:
            service().confirm_password_reset(
                student_id=payload.student_id,
                code=payload.code,
                new_password=payload.new_password,
            )
        except Exception as error:
            raise failure(error) from error
        return {"status": "reset"}

    @router.get("/auth/hust/profile")
    async def get_profile(current_user: UserParam):
        principal = getattr(current_user, "identifier", "")
        if not current_user or not principal.startswith("student:"):
            raise HTTPException(status_code=401, detail="Unauthorized")
        account = service().store.get_account(principal.removeprefix("student:"))
        if not account:
            raise HTTPException(status_code=404, detail="Profile not found")
        return {
            "student_id": account["student_id"],
            "nickname": account["nickname"],
            "email": account["email"],
            "created_at": account["created_at"],
            "updated_at": account["updated_at"],
            "session_version": int(account["session_version"]),
        }

    @router.patch("/auth/hust/profile")
    async def update_profile(
        payload: ProfileUpdateRequest, request: Request, current_user: UserParam
    ):
        principal = getattr(current_user, "identifier", "")
        if not current_user or not principal.startswith("student:"):
            raise HTTPException(status_code=401, detail="Unauthorized")
        origin = request.headers.get("origin", "")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Cross-origin request rejected")
        try:
            account = service().update_profile(
                student_id=principal.removeprefix("student:"),
                nickname=payload.nickname,
                current_password=payload.current_password,
                new_password=payload.new_password,
            )
        except Exception as error:
            raise failure(error) from error

        if payload.new_password:
            from chainlit.auth import create_jwt
            from chainlit.auth.cookie import set_auth_cookie
            from chainlit.user import User

            user = User(
                identifier=principal,
                display_name=account["nickname"],
                metadata={
                    "provider": "hust-student",
                    "student_id": account["student_id"],
                    "session_version": int(account["session_version"]),
                },
            )
            response = JSONResponse(
                {
                    "student_id": account["student_id"],
                    "nickname": account["nickname"],
                    "email": account["email"],
                    "created_at": account["created_at"],
                    "updated_at": account["updated_at"],
                    "session_version": int(account["session_version"]),
                }
            )
            set_auth_cookie(request, response, create_jwt(user))
            return response
        return {
            "student_id": account["student_id"],
            "nickname": account["nickname"],
            "email": account["email"],
            "created_at": account["created_at"],
            "updated_at": account["updated_at"],
            "session_version": int(account["session_version"]),
        }

    @app.middleware("http")
    async def invalidate_reset_sessions(request: Request, call_next):
        path = request.url.path
        if path.startswith(
            (
                "/auth/hust",
                "/login",
                "/register",
                "/forgot-password",
                "/logout",
                "/assets",
                "/public",
                "/favicon",
            )
        ):
            return await call_next(request)
        from chainlit.auth import decode_jwt, get_token_from_cookies

        if path == "/" and request.method == "GET":
            token = get_token_from_cookies(request.cookies)
            try:
                decoded = decode_jwt(token) if token else None
            except Exception:
                decoded = None
            if not decoded:
                from fastapi.responses import RedirectResponse

                return RedirectResponse(url="/login", status_code=303)

        token = get_token_from_cookies(request.cookies)
        if not token:
            return await call_next(request)
        try:
            user = decode_jwt(token)
        except Exception:
            return await call_next(request)
        identifier = getattr(user, "identifier", "")
        if identifier.startswith("browser:"):
            from fastapi.responses import RedirectResponse

            response = RedirectResponse(url="/login", status_code=303)
            response.delete_cookie("access_token", path="/")
            response.headers["Cache-Control"] = "no-store"
            return response
        if not identifier.startswith("student:"):
            return await call_next(request)
        account = service().store.get_account(identifier.removeprefix("student:"))
        expected = int((account or {}).get("session_version", -1))
        # 陈旧或被篡改的 token 里 session_version 可能不是数字；按会话失效
        # 处理跳登录页，而不是让 int() 抛 ValueError 变成整页 500。
        try:
            actual = int((getattr(user, "metadata", None) or {}).get("session_version", -2))
        except (TypeError, ValueError):
            actual = -3
        if not account or account.get("status") != "active" or actual != expected:
            from fastapi.responses import RedirectResponse

            logger.info(
                "auth-middleware reject path=%s expected=%s actual=%s has_account=%s",
                path, expected, actual, bool(account),
            )

            response = RedirectResponse(url="/login", status_code=303)
            response.delete_cookie("access_token", path="/")
            response.headers["Cache-Control"] = "no-store"
            return response
        return await call_next(request)

    _move_routes_before_catchall(
        router,
        (
            "/auth/hust/",
            "/auth/profile",
            "/auth/profile/close",
            "/profile/security",
            "/login",
            "/register",
            "/forgot-password",
        ),
    )
