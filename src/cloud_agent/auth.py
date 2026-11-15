import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cloud_agent.models import AuthAttempt, LoginSession, Membership, Tenant, User, now

router = APIRouter(prefix="/api/auth", tags=["authentication"])
COOKIE = "cloud_session"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return f"scrypt${salt}${result.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt, _ = encoded.split("$")
        return hmac.compare_digest(hash_password(password, salt), encoded)
    except (ValueError, TypeError):
        return False


DUMMY_HASH = hash_password("no-user-matches-this-placeholder")


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    email: str
    csrf_token: str


def require_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    expected = request.app.state.settings.public_origin.rstrip("/")
    if origin and origin != expected:
        raise HTTPException(403, "Untrusted origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site request denied")


def principal(request: Request) -> Principal:
    token = request.cookies.get(COOKIE, "")
    if not token:
        raise HTTPException(401, "Sign in required")
    with request.app.state.sessions() as db:
        login = db.get(LoginSession, digest(token))
        if not login or login.expires_at <= now():
            raise HTTPException(401, "Session expired")
        membership = db.get(Membership, (login.user_id, login.tenant_id))
        user = db.get(User, login.user_id)
        if not user or not membership:
            raise HTTPException(401, "Membership revoked")
        value = Principal(user.id, login.tenant_id, user.email, login.csrf_token)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        require_origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(csrf, value.csrf_token):
            raise HTTPException(403, "CSRF token required")
    return value


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)
    workspace: str = Field(default="My workspace", min_length=1, max_length=120)
    signup_code: str = Field(default="", max_length=256)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if value.count("@") != 1 or any(c.isspace() for c in value):
            raise ValueError("Invalid email")
        local, domain = value.split("@")
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("Invalid email")
        return value


def throttle(request: Request, email: str) -> None:
    # Uvicorn normalizes the address only for operator-configured trusted proxies.
    identity = request.client.host if request.client else "unknown"
    stamp = now()
    for scope in ("ip:" + identity, "email:" + email):
        key = digest(scope)
        for retry in range(2):
            try:
                with request.app.state.sessions.begin() as db:
                    if db.bind.dialect.name == "sqlite":
                        from sqlalchemy import text

                        db.execute(text("BEGIN IMMEDIATE"))
                    attempt = db.scalar(
                        select(AuthAttempt).where(AuthAttempt.key == key).with_for_update()
                    )
                    if attempt is None:
                        db.add(AuthAttempt(key=key, window_start=stamp, count=1))
                    else:
                        if stamp - attempt.window_start >= 900:
                            attempt.window_start, attempt.count = stamp, 0
                        if attempt.count >= 30:
                            raise HTTPException(
                                429, "Too many authentication attempts; retry later"
                            )
                        attempt.count += 1
                break
            except IntegrityError:
                if retry:
                    raise HTTPException(429, "Retry later") from None


def issue_session(db, request: Request, response: Response, user: User, tenant_id: str) -> dict:
    settings = request.app.state.settings
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.add(
        LoginSession(
            token_hash=digest(token),
            user_id=user.id,
            tenant_id=tenant_id,
            csrf_token=csrf,
            expires_at=now() + settings.session_hours * 3600,
        )
    )
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
        max_age=settings.session_hours * 3600,
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"user_id": user.id, "tenant_id": tenant_id, "email": user.email, "csrf_token": csrf}


@router.post("/register", status_code=201)
def register(payload: Credentials, request: Request, response: Response):
    require_origin(request)
    settings = request.app.state.settings
    if not settings.registration_enabled:
        raise HTTPException(403, "Registration is closed")
    throttle(request, payload.email)
    code = settings.signup_code.get_secret_value()
    if code and not hmac.compare_digest(payload.signup_code, code):
        raise HTTPException(403, "A valid signup code is required")
    encoded = hash_password(payload.password)
    try:
        with request.app.state.sessions.begin() as db:
            tenant = Tenant(
                name=payload.workspace,
                daily_tasks=settings.daily_tasks,
                concurrent_tasks=settings.concurrent_tasks,
                monthly_tokens=settings.monthly_tokens,
            )
            user = User(email=payload.email, password_hash=encoded)
            db.add_all([tenant, user])
            db.flush()
            db.add(Membership(user_id=user.id, tenant_id=tenant.id))
            result = issue_session(db, request, response, user, tenant.id)
        return result
    except IntegrityError:
        raise HTTPException(409, "Unable to register this account") from None


@router.post("/login")
def login(payload: Credentials, request: Request, response: Response):
    require_origin(request)
    throttle(request, payload.email)
    with request.app.state.sessions.begin() as db:
        user = db.scalar(select(User).where(User.email == payload.email))
        # Perform equivalent password work for unknown users.
        encoded = user.password_hash if user else DUMMY_HASH
        if not verify_password(payload.password, encoded) or not user:
            raise HTTPException(401, "Invalid email or password")
        membership = db.scalar(select(Membership).where(Membership.user_id == user.id))
        if not membership:
            raise HTTPException(403, "No workspace membership")
        return issue_session(db, request, response, user, membership.tenant_id)


@router.get("/me")
def me(response: Response, actor: Principal = Depends(principal)):
    response.headers["Cache-Control"] = "no-store"
    return actor


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, actor: Principal = Depends(principal)):
    with request.app.state.sessions.begin() as db:
        login = db.get(LoginSession, digest(request.cookies.get(COOKIE, "")))
        if login:
            db.delete(login)
    response.delete_cookie(
        COOKIE,
        path="/",
        secure=request.app.state.settings.secure_cookies,
        httponly=True,
        samesite="strict",
    )
