"""HUST student accounts, email verification codes, and bounded rate limits."""

from __future__ import annotations

import hashlib
import hmac
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
from pydantic import BaseModel
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .emailer import EmailMessage, EmailSender

_HUST_PATTERN = re.compile(r"^U202[0-9]1[0-9]{4}$")
_CODE_ALPHABET = "0123456789"


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
    mode: str = "local"
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
            mode=os.environ.get("EG_AUTH_MODE", "local").strip().lower(),
            email_domain=os.environ.get(
                "EG_STUDENT_EMAIL_DOMAIN", "hust.edu.cn"
            ).strip().lower(),
            student_id_pattern=os.environ.get(
                "EG_STUDENT_ID_PATTERN", cls.student_id_pattern
            ),
            password_min_length=int(
                os.environ.get("EG_PASSWORD_MIN_LENGTH", cls.password_min_length)
            ),
            code_ttl_seconds=int(
                os.environ.get("EG_AUTH_CODE_TTL_SECONDS", cls.code_ttl_seconds)
            ),
            code_max_attempts=int(
                os.environ.get("EG_AUTH_CODE_MAX_ATTEMPTS", cls.code_max_attempts)
            ),
            resend_interval_seconds=int(
                os.environ.get("EG_AUTH_CODE_RESEND_SECONDS", cls.resend_interval_seconds)
            ),
            account_hourly_limit=int(
                os.environ.get("EG_AUTH_HOURLY_ACCOUNT_LIMIT", cls.account_hourly_limit)
            ),
            ip_hourly_limit=int(
                os.environ.get("EG_AUTH_HOURLY_IP_LIMIT", cls.ip_hourly_limit)
            ),
            lockout_attempts=int(
                os.environ.get("EG_AUTH_LOCKOUT_ATTEMPTS", cls.lockout_attempts)
            ),
            lockout_seconds=int(
                os.environ.get("EG_AUTH_LOCKOUT_SECONDS", cls.lockout_seconds)
            ),
        )

    def validate(self) -> None:
        if self.mode not in {"local", "hust"}:
            raise ValueError("EG_AUTH_MODE must be 'local' or 'hust'")
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
        self._conn.executescript(
            """
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
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_account(self, student_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM student_accounts WHERE student_id = ?", (student_id,)
            ).fetchone()
        return dict(row) if row else None

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
                    (student_id, nickname, email, password_hash, created, created, created),
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
            if current is None or current["consumed_at"] or _is_expired(current["expires_at"]):
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

    def mark_login_failure(self, student_id: str, max_attempts: int, lock_seconds: int) -> int:
        now = _now()
        locked_until = _iso(now + timedelta(seconds=lock_seconds))
        with self._lock:
            row = self._conn.execute(
                "SELECT failed_login_count FROM student_accounts WHERE student_id=?", (student_id,)
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
        if student_id.casefold() in lower_password or nickname.casefold() in lower_password:
            raise AuthValidationError("密码不能包含学号或昵称")

    def _check_send_limits(self, purpose: str, student_id: str, ip: str) -> None:
        now = _now()
        latest = self.store.latest_code(
            purpose, student_id, now - timedelta(seconds=self.config.resend_interval_seconds)
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
                f"你的 ExamGuard {title}验证码是：{code}\n\n"
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

    def confirm_registration(self, *, student_id: str, code: str) -> AuthenticatedStudent:
        clean_student_id = self._validate_student_id(student_id)
        account = self.store.get_account(clean_student_id)
        if account:
            raise AuthCodeError("验证码无效或已过期")
        row = self.store.latest_code(
            "register", clean_student_id, _now() - timedelta(seconds=self.config.code_ttl_seconds * 2)
        )
        if not row:
            raise AuthCodeError("验证码无效或已过期")
        if not self.store.consume_code(row, self._code_hash(code), self.config.code_max_attempts):
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
        if not self.store.consume_code(row, self._code_hash(code), self.config.code_max_attempts):
            raise AuthCodeError("验证码无效或已过期")
        self.store.update_password(clean_student_id, self._hasher.hash(new_password))

    def authenticate(self, student_id: str, password: str) -> AuthenticatedStudent | None:
        clean_student_id = student_id.strip().upper()
        account = self.store.get_account(clean_student_id)
        if not account:
            # Keep missing and incorrect passwords on a similar hashing path.
            try:
                self._hasher.verify("$argon2id$v=19$m=65536,t=2,p=1$c29tZXNhbHQ$invalid", password)
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


def auth_service_from_env() -> StudentAuthService:
    from .emailer import email_sender_from_env

    config = AuthConfig.from_env()
    if config.mode != "hust":
        raise ValueError("student auth is only available when EG_AUTH_MODE=hust")
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

    return runtime_path("EG_AUTH_DB_FILE", ".examguard/auth.sqlite3")


def _client_ip(request: Any) -> str:
    if os.environ.get("EG_TRUST_PROXY_HEADERS", "").lower() in {"true", "1", "yes"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return getattr(getattr(request, "client", None), "host", "") or "unknown"


def _auth_page(title: str, endpoint_prefix: str, require_password: bool) -> str:
    password_fields = """
      <label>密码<input id="password" type="password" autocomplete="new-password" required></label>
    """ if require_password else ""
    confirm_fields = """
      <label>新密码<input id="new-password" type="password" autocomplete="new-password" required></label>
    """ if endpoint_prefix.endswith("password-reset") else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · ExamGuard</title>
<style>
body{{font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;background:#f6f7fb;color:#172033;margin:0}}
main{{max-width:430px;margin:8vh auto;background:white;padding:28px;border-radius:16px;box-shadow:0 14px 40px #17203314}}
h1{{font-size:22px}} label{{display:block;margin:14px 0;font-weight:600}} input{{width:100%;box-sizing:border-box;margin-top:6px;padding:10px;border:1px solid #ccd2dd;border-radius:9px}}
button{{width:100%;margin-top:16px;padding:11px;border:0;border-radius:999px;background:#4d6bfe;color:white;font-weight:700;cursor:pointer}}
.secondary{{display:block;text-align:center;margin-top:14px;color:#4d6bfe}} .message{{min-height:22px;color:#7a2f31}}
</style></head><body><main><h1>{title}</h1>
<form id="form">
  <label>学号<input id="student-id" placeholder="U202113566" required></label>
  {password_fields}{confirm_fields}
  <label>验证码<input id="code" inputmode="numeric" pattern="[0-9]{{6}}" required></label>
  <button type="button" id="send">发送验证码</button>
  <button type="submit">提交</button><p class="message" id="message"></p>
</form>
<a class="secondary" href="/login">已有账号？登录</a>
</main><script>
const message=document.querySelector('#message');
async function post(path, body) {{
  const response=await fetch(path,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
  const data=await response.json().catch(()=>({{}}));
  if(!response.ok) throw new Error(data.detail||'请求未完成');
  return data;
}}
document.querySelector('#send').addEventListener('click',async event=>{{
  event.preventDefault(); message.textContent='';
  const id=document.querySelector('#student-id').value.trim().toUpperCase();
  try {{
    const body={{student_id:id}}; const password=document.querySelector('#password'); const nickname=document.querySelector('#nickname');
    if(password) body.password=password.value; if(nickname) body.nickname=nickname.value;
    await post('{endpoint_prefix}/request-code',body); message.textContent='验证码已发送，请查收教育邮箱。';
  }} catch(error) {{ message.textContent=error.message; }}
}});
document.querySelector('#form').addEventListener('submit',async event=>{{
  event.preventDefault(); message.textContent='';
  const id=document.querySelector('#student-id').value.trim().toUpperCase();
  const code=document.querySelector('#code').value.trim();
  try {{
    const body={{student_id:id,code}}; const password=document.querySelector('#password'); const nickname=document.querySelector('#nickname'); const newPassword=document.querySelector('#new-password');
    if(password) body.password=password.value; if(nickname) body.nickname=nickname.value; if(newPassword) body.new_password=newPassword.value;
    await post('{endpoint_prefix}/confirm',body); location.href='/login';
  }} catch(error) {{ message.textContent=error.message; }}
}});
</script></body></html>"""


def install_student_auth(app: Any, service_factory) -> None:
    """Install JSON endpoints and simple same-origin registration/reset pages."""
    if getattr(app.state, "examguard_student_auth_installed", False):
        return
    app.state.examguard_student_auth_installed = True

    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse
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

    @app.get("/auth/hust/register", include_in_schema=False)
    async def register_page():
        return HTMLResponse(_auth_page("注册 ExamGuard", "/auth/hust/register", True))

    @app.post("/auth/hust/register/request-code")
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

    @app.post("/auth/hust/register/confirm")
    async def register_confirm(payload: RegistrationConfirmRequest):
        try:
            account = service().confirm_registration(
                student_id=payload.student_id, code=payload.code
            )
        except Exception as error:
            raise failure(error) from error
        return {"status": "created", "identifier": account.identifier}

    @app.get("/auth/hust/forgot-password", include_in_schema=False)
    async def reset_page():
        return HTMLResponse(
            _auth_page("重置密码", "/auth/hust/password-reset", False)
        )

    @app.post("/auth/hust/password-reset/request-code")
    async def password_reset_code(payload: ResetCodeRequest, request: Request):
        try:
            await service().request_password_reset_code(
                student_id=payload.student_id, ip=_client_ip(request)
            )
        except Exception as error:
            raise failure(error) from error
        # Unknown accounts get the same successful shape as existing accounts.
        return {"status": "sent"}

    @app.post("/auth/hust/password-reset/confirm")
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

    @app.middleware("http")
    async def invalidate_reset_sessions(request: Request, call_next):
        path = request.url.path
        if path.startswith(
            ("/auth/hust", "/login", "/logout", "/assets", "/public", "/favicon")
        ):
            return await call_next(request)
        from chainlit.auth import decode_jwt, get_token_from_cookies

        token = get_token_from_cookies(request.cookies)
        if not token:
            return await call_next(request)
        try:
            user = decode_jwt(token)
        except Exception:
            return await call_next(request)
        identifier = getattr(user, "identifier", "")
        if not identifier.startswith("student:"):
            return await call_next(request)
        account = service().store.get_account(identifier.removeprefix("student:"))
        expected = int((account or {}).get("session_version", -1))
        actual = int(
            (getattr(user, "metadata", None) or {}).get("session_version", -2)
        )
        if not account or account.get("status") != "active" or actual != expected:
            from fastapi.responses import RedirectResponse

            response = RedirectResponse(url="/login", status_code=303)
            response.delete_cookie("access_token", path="/")
            response.headers["Cache-Control"] = "no-store"
            return response
        return await call_next(request)
