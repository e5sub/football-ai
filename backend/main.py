from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import re
import subprocess
import sys
import threading
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, create_engine, func, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'football_ai.db'}")
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "365"))

engine_options = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}
engine = create_engine(DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(default=False)
    activation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    bets: Mapped[list["Bet"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class ActivationToken(Base):
    __tablename__ = "activation_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionToken(Base):
    __tablename__ = "session_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ActivationCode(Base):
    __tablename__ = "activation_codes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    code_hint: Mapped[str] = mapped_column(String(12))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grant_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class DataSnapshot(Base):
    __tablename__ = "data_snapshots"
    dataset_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Bet(Base):
    __tablename__ = "bets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    match_id: Mapped[str] = mapped_column(String(120), index=True)
    play_type: Mapped[str] = mapped_column(String(16), default="spf", index=True)
    selection: Mapped[str] = mapped_column(String(10))
    handicap: Mapped[float | None] = mapped_column(Float, nullable=True)
    odds: Mapped[float] = mapped_column(Float)
    stake: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user: Mapped[User] = relationship(back_populates="bets")


def initialize_database() -> None:
    """Create all tables and apply the small compatibility migration on startup."""
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    with engine.begin() as connection:
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        if "is_admin" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
        if "activation_expires_at" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN activation_expires_at DATETIME NULL"))
        admin_columns = {column["name"] for column in inspector.get_columns("admin_sessions")}
        if "user_id" not in admin_columns:
            connection.execute(text("ALTER TABLE admin_sessions ADD COLUMN user_id INTEGER NULL"))
        has_admin = connection.execute(text("SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1")).first()
        if not has_admin:
            first_user = connection.execute(text("SELECT MIN(id) FROM users")).scalar()
            if first_user is not None:
                connection.execute(text("UPDATE users SET is_admin = 1 WHERE id = :user_id"), {"user_id": first_user})
        connection.execute(text("UPDATE users SET is_active = 1, activation_expires_at = NULL WHERE is_admin = 1"))
        bet_columns = {column["name"] for column in inspector.get_columns("bets")}
        if "play_type" not in bet_columns:
            connection.execute(text("ALTER TABLE bets ADD COLUMN play_type VARCHAR(16) NOT NULL DEFAULT 'spf'"))
        if "handicap" not in bet_columns:
            connection.execute(text("ALTER TABLE bets ADD COLUMN handicap FLOAT NULL"))
        activation_code_columns = {column["name"] for column in inspector.get_columns("activation_codes")}
        if "grant_days" not in activation_code_columns:
            connection.execute(text("ALTER TABLE activation_codes ADD COLUMN grant_days INTEGER NULL"))


initialize_database()
app = FastAPI(title="Football AI Command Center API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def prevent_cdn_caching_api(request: Request, call_next):
    """Authentication and user data must never be replayed by an edge cache."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Surrogate-Control"] = "no-store"
        response.headers["CDN-Cache-Control"] = "no-store"
        response.headers["Vary"] = "Cookie, Authorization, X-CSRF-Token"
    return response
CSRF_COOKIE = "football_ai_csrf"
AUTH_COOKIE = "football_ai_auth"
ADMIN_COOKIE = "football_ai_admin"
REMEMBER_COOKIE = "football_ai_remember"


def load_auth_signing_secret() -> str:
    """Keep cookie signatures identical across restarts and all app replicas."""
    configured_secret = os.getenv("AUTH_SECRET_KEY", "").strip()
    if configured_secret:
        return configured_secret
    # All replicas receive the same DATABASE_URL. This fallback is therefore
    # deterministic, unlike a per-container generated file which would cause
    # a signed cookie from instance A to be rejected by instance B.
    return hashlib.sha256(f"football-ai:session-signing:{DATABASE_URL}".encode("utf-8")).hexdigest()


AUTH_SIGNING_SECRET = load_auth_signing_secret()
DATA_UPDATE_LOCK = threading.Lock()
DATA_UPDATE_STATUS_LOCK = threading.Lock()
DATA_UPDATE_LOG = ROOT / "logs" / "data_update.log"
DATA_UPDATE_STATUS = {"running": False, "message": "", "output": "", "started_at": None, "finished_at": None, "duration_seconds": None}
JSON_SNAPSHOT_FILES = {
    "matches": ROOT / "data" / "matches.json",
    "history": ROOT / "data" / "jc_history.json",
    "analysis_archive": ROOT / "data" / "analysis_archive.json",
    "fixture_catalog": ROOT / "data" / "fixture_catalog.json",
}


def write_update_log(message: str, output: str = "") -> None:
    try:
        DATA_UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DATA_UPDATE_LOG.open("a", encoding="utf-8") as log_file:
            log_file.write(f"[{now().isoformat()}] {message}\n")
            if output:
                log_file.write(f"{output[-4000:]}\n")
    except OSError:
        return


def import_json_snapshots() -> dict[str, str]:
    """Import the generated data files directly into database snapshots."""
    db = SessionLocal()
    results: dict[str, str] = {}
    try:
        for dataset_key, path in JSON_SNAPSHOT_FILES.items():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                record = db.get(DataSnapshot, dataset_key)
                if record is None:
                    db.add(DataSnapshot(dataset_key=dataset_key, payload=payload))
                else:
                    record.payload = payload
                    record.updated_at = now()
                db.commit()
                results[dataset_key] = "success"
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                results[dataset_key] = f"failed: {exc}"
    finally:
        db.close()
    return results


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    activation_code: str | None = Field(default=None, min_length=8, max_length=64)


class LoginRequest(RegisterRequest):
    pass


class BetRequest(BaseModel):
    match_id: str = Field(min_length=1, max_length=120)
    play_type: str = Field(default="spf", pattern="^(spf|rqspf|bf|zjq|bqc)$")
    selection: str
    handicap: float | None = Field(default=None, ge=-10, le=10)
    odds: float = Field(gt=1, le=1000)
    stake: float = Field(gt=0, le=1000000)


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class AdminPasswordResetRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


class UserStatusRequest(BaseModel):
    is_active: bool


class ActivationCodeRequest(BaseModel):
    expires_hours: int = Field(default=72, ge=1, le=8760)
    duration: Literal["month", "half_year", "year", "permanent"] = "month"


def now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Treat timezone-less database datetimes as UTC before comparing them."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def remember_token(user: User, expires_at: datetime) -> str:
    """Create a tamper-proof cookie that is invalidated when password changes."""
    payload = f"{user.id}:{int(as_utc(expires_at).timestamp())}:{digest(user.password_hash)}"
    signature = hmac.new(AUTH_SIGNING_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlsafe_b64encode(f"{payload}:{signature}".encode("utf-8")).decode("ascii")


def remembered_user(token: str | None, db: Session) -> User | None:
    if not token:
        return None
    try:
        payload, signature = urlsafe_b64decode(token.encode("ascii")).decode("utf-8").rsplit(":", 1)
        expected = hmac.new(AUTH_SIGNING_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        user_id, expires_at, password_fingerprint = payload.split(":", 2)
        if not hmac.compare_digest(signature, expected) or int(expires_at) < int(now().timestamp()):
            return None
        user = db.get(User, int(user_id))
        if not user or not hmac.compare_digest(password_fingerprint, digest(user.password_hash)) or not user_has_access(user):
            return None
        return user
    except (BinasciiError, ValueError, UnicodeDecodeError, TypeError):
        return None


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240_000)
    return f"{salt.hex()}${derived.hex()}"


def check_password(password: str, encoded: str) -> bool:
    try:
        salt_hex, hash_hex = encoded.split("$", 1)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 240_000)
        return hmac.compare_digest(derived.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def csrf_protect(
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    csrf_cookie: Annotated[str | None, Cookie(alias=CSRF_COOKIE)] = None,
) -> None:
    if not csrf_header or not csrf_cookie or not hmac.compare_digest(csrf_header, csrf_cookie):
        raise HTTPException(status_code=403, detail="CSRF token 无效或缺失")


def user_has_access(user: User) -> bool:
    return user.is_active and (user.is_admin or user.activation_expires_at is None or as_utc(user.activation_expires_at) > now())


def current_user(
    authorization: Annotated[str | None, Header()] = None,
    auth_cookie: Annotated[str | None, Cookie(alias=AUTH_COOKIE)] = None,
    admin_cookie: Annotated[str | None, Cookie(alias=ADMIN_COOKIE)] = None,
    remember_cookie: Annotated[str | None, Cookie(alias=REMEMBER_COOKIE)] = None,
    db: Session = Depends(db_session),
) -> User:
    header_token = authorization.split(" ", 1)[1].strip() if authorization and authorization.lower().startswith("bearer ") else None
    tokens = [token for token in (header_token, auth_cookie, admin_cookie) if token]
    # Primary path: a permanent signed browser session, equivalent to Flask's
    # built-in session cookie. It survives an application restart because its
    # signature key is persisted independently of the session-token table.
    for token in tokens:
        signed_user = remembered_user(token, db)
        if signed_user:
            return signed_user
    if not tokens:
        user = remembered_user(remember_cookie, db)
        if user:
            return user
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    session_token = None
    user = None
    for token in tokens:
        candidate = db.scalar(select(SessionToken).where(SessionToken.token_hash == digest(token)))
        if candidate and as_utc(candidate.expires_at) >= now():
            candidate_user = db.get(User, candidate.user_id)
            if candidate_user and user_has_access(candidate_user):
                session_token, user = candidate, candidate_user
                break
        # An administrator is also a valid signed-in user.  This lets a
        # direct admin login return to the homepage without requiring a second
        # login just to create a regular session token.
        admin_session = db.scalar(select(AdminSession).where(AdminSession.token_hash == digest(token)))
        if admin_session and as_utc(admin_session.expires_at) >= now():
            candidate_user = db.get(User, admin_session.user_id) if admin_session.user_id else None
            if candidate_user and candidate_user.is_admin and user_has_access(candidate_user):
                session_token, user = admin_session, candidate_user
                break
    if not user:
        user = remembered_user(remember_cookie, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")
    # The browser token already has the same fixed lifetime.  Avoid writing on
    # every authenticated GET: a transient database write failure here used to
    # turn a page refresh into a failed login and also blocked admin actions
    # before their handler ran.
    return user


def current_admin(authorization: Annotated[str | None, Header()] = None, admin_cookie: Annotated[str | None, Cookie(alias=ADMIN_COOKIE)] = None, auth_cookie: Annotated[str | None, Cookie(alias=AUTH_COOKIE)] = None, db: Session = Depends(db_session)) -> AdminSession | SessionToken:
    header_token = authorization.split(" ", 1)[1].strip() if authorization and authorization.lower().startswith("bearer ") else None
    tokens = [token for token in (header_token, admin_cookie, auth_cookie) if token]
    if not tokens:
        raise HTTPException(status_code=401, detail="需要管理员登录")
    admin_session = None
    user = None
    for token in tokens:
        session = db.scalar(select(AdminSession).where(AdminSession.token_hash == digest(token)))
        if session and as_utc(session.expires_at) >= now():
            candidate_user = db.get(User, session.user_id) if session.user_id else None
            if candidate_user and candidate_user.is_admin and user_has_access(candidate_user):
                admin_session, user = session, candidate_user
                break
        user_session = db.scalar(select(SessionToken).where(SessionToken.token_hash == digest(token)))
        if user_session and as_utc(user_session.expires_at) >= now():
            candidate_user = db.get(User, user_session.user_id)
            if candidate_user and candidate_user.is_admin and user_has_access(candidate_user):
                admin_session, user = user_session, candidate_user
                break
    if not admin_session or not user:
        raise HTTPException(status_code=403, detail="没有管理员权限")
    # Keep authentication checks read-only; see current_user above.  Admin
    # requests must not depend on a session-renewal write succeeding.
    return admin_session


def user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "is_active": user_has_access(user),
        "activation_expires_at": user.activation_expires_at.isoformat() if user.activation_expires_at else None,
        "is_admin": user.is_admin,
    }


def bet_payload(bet: Bet) -> dict:
    return {
        "id": bet.id,
        "match_id": bet.match_id,
        "play_type": bet.play_type,
        "selection": bet.selection,
        "handicap": bet.handicap,
        "odds": bet.odds,
        "stake": bet.stake,
        "status": bet.status,
        "profit": bet.profit,
        "result": bet.result,
        "created_at": bet.created_at.isoformat(),
        "settled_at": bet.settled_at.isoformat() if bet.settled_at else None,
    }


def activation_code_payload(record: ActivationCode) -> dict:
    duration_labels = {30: "一个月", 180: "半年", 365: "一年", None: "永久"}
    return {
        "id": record.id,
        "code_hint": record.code_hint,
        "duration": duration_labels.get(record.grant_days, f"{record.grant_days}天"),
        "grant_days": record.grant_days,
        "expires_at": record.expires_at.isoformat(),
        "used_at": record.used_at.isoformat() if record.used_at else None,
        "used_by": record.used_by,
        "created_at": record.created_at.isoformat(),
    }


@app.get("/api/health")
def health() -> dict:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok", "database": engine.dialect.name}


@app.get("/api/data/matches")
def data_matches(response: Response, db: Session = Depends(db_session)) -> dict:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    snapshot = db.get(DataSnapshot, "matches")
    if snapshot:
        return snapshot.payload
    try:
        return json.loads((ROOT / "data" / "matches.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"matches": []}


@app.get("/api/data/analysis-archive")
def data_analysis_archive(response: Response, db: Session = Depends(db_session)) -> dict:
    """Return the same database snapshot written by the data-update task."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    snapshot = db.get(DataSnapshot, "analysis_archive")
    if snapshot:
        return snapshot.payload
    try:
        return json.loads((ROOT / "data" / "analysis_archive.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"matches": []}


@app.get("/api/data/matches/{match_id}/plays")
def match_plays(match_id: str, db: Session = Depends(db_session)) -> dict:
    match = next((item for item in load_matches(db) if str(item.get("id")) == match_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="赛事不存在")
    play_odds = (match.get("odds") or {}).get("plays") or {}
    return {
        "match_id": str(match.get("id")),
        "fixture_id": match.get("fixtureId"),
        "kickoff": match.get("date"),
        "handicap": match.get("handicap"),
        "plays": [
            {"type": "spf", "label": "胜平负", "selections": ["home", "draw", "away"]},
            {"type": "rqspf", "label": "让球胜平负", "handicap": match.get("handicap"), "selections": ["home", "draw", "away"]},
            {"type": "bf", "label": "比分", "selections": sorted((play_odds.get("bf") or {}).keys())},
            {"type": "zjq", "label": "总进球", "selections": sorted((play_odds.get("zjq") or {}).keys()) or [str(value) for value in range(8)]},
            {"type": "bqc", "label": "半全场", "selections": sorted((play_odds.get("bqc") or {}).keys()) or sorted(PLAY_SELECTIONS["bqc"])},
        ],
    }


def latest_play_odds(match: dict, play_type: str) -> dict | None:
    odds = match.get("odds") or {}
    candidates = [
        odds.get(play_type),
        (odds.get("plays") or {}).get(play_type),
        (match.get("playOdds") or {}).get(play_type),
        (match.get("officialOdds") or {}).get(play_type),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            prices = candidate.get("current", candidate)
            if play_type == "bqc" and isinstance(prices, dict):
                # Read snapshots created before the bqc normalization as well.
                return {BQC_SOURCE_SELECTIONS.get(str(key), str(key)): value for key, value in prices.items()}
            return prices
    if play_type == "spf" and isinstance(odds.get("current"), dict):
        return odds["current"]
    return None


@app.get("/api/data/matches/{match_id}/odds")
def match_latest_odds(match_id: str, play_type: str = "spf", db: Session = Depends(db_session)) -> dict:
    if play_type not in PLAY_SELECTIONS:
        raise HTTPException(status_code=422, detail="不支持的足彩玩法")
    match = next((item for item in load_matches(db) if str(item.get("id")) == match_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="赛事不存在")
    return {
        "match_id": match_id,
        "play_type": play_type,
        "fixture_id": match.get("fixtureId"),
        "updated_at": match.get("updatedAt") or match.get("odds", {}).get("updatedAt") or (db.get(DataSnapshot, "matches").updated_at.isoformat() if db.get(DataSnapshot, "matches") else None),
        "odds": latest_play_odds(match, play_type),
        "source": "当前数据库赛事快照",
    }


@app.get("/api/auth/csrf")
def csrf_token(response: Response) -> dict:
    token = secrets.token_urlsafe(32)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.set_cookie(CSRF_COOKIE, token, httponly=False, secure=os.getenv("COOKIE_SECURE", "0") == "1", samesite="strict", max_age=86400)
    return {"csrf_token": token}


@app.post("/api/auth/logout", dependencies=[Depends(csrf_protect)])
def logout(response: Response) -> dict:
    response.delete_cookie(AUTH_COOKIE, path="/")
    response.delete_cookie(ADMIN_COOKIE, path="/")
    response.delete_cookie(REMEMBER_COOKIE, path="/")
    return {"message": "已退出登录"}


@app.post("/api/auth/register", dependencies=[Depends(csrf_protect)])
def register(payload: RegisterRequest, db: Session = Depends(db_session)) -> dict:
    email = str(payload.email).lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="邮箱已注册")
    activation_record = None
    if payload.activation_code:
        activation_record = db.scalar(select(ActivationCode).where(ActivationCode.code_hash == digest(payload.activation_code.strip().upper())))
        if not activation_record or activation_record.used_at or as_utc(activation_record.expires_at) < now():
            raise HTTPException(status_code=400, detail="激活码无效、已使用或已过期")
    first_user = db.scalar(select(User.id).limit(1)) is None
    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        is_active=True,
        activation_expires_at=(
            None
            if first_user or (activation_record and activation_record.grant_days is None)
            else now() + timedelta(days=activation_record.grant_days if activation_record else 3)
        ),
        is_admin=first_user,
    )
    db.add(user)
    db.flush()
    if activation_record:
        activation_record.used_at = now()
        activation_record.used_by = user.id
    db.commit()
    return {
        "message": "注册成功，激活码已生效，请登录" if activation_record else ("注册成功，账号已激活，请登录" if first_user else "注册成功，已赠送 3 天使用权"),
        "activation_url": None,
        **user_payload(user),
    }


@app.get("/api/auth/activate")
def activate() -> None:
    raise HTTPException(status_code=403, detail="账号需由管理员激活")


@app.post("/api/auth/login", dependencies=[Depends(csrf_protect)])
def login(payload: LoginRequest, response: Response, db: Session = Depends(db_session)) -> dict:
    email = str(payload.email).lower()
    user = db.scalar(select(User).where(User.email == email))
    if not user or not check_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    if not user_has_access(user):
        raise HTTPException(status_code=403, detail="账号使用权已过期，请使用激活码或联系管理员")
    expires_at = now() + timedelta(days=SESSION_DAYS)
    # The signed cookie is the primary session, mirroring the persistent Flask
    # session used by the reference project. No database session row is needed
    # to restore an ordinary user after a restart.
    raw_token = remember_token(user, expires_at)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.set_cookie(AUTH_COOKIE, raw_token, httponly=True, secure=os.getenv("COOKIE_SECURE", "0") == "1", samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    response.set_cookie(REMEMBER_COOKIE, remember_token(user, expires_at), httponly=True, secure=os.getenv("COOKIE_SECURE", "0") == "1", samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    return {"token": raw_token, "user": user_payload(user)}


@app.get("/api/auth/me")
def me(response: Response, user: User = Depends(current_user)) -> dict:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    expires_at = now() + timedelta(days=SESSION_DAYS)
    response.set_cookie(AUTH_COOKIE, remember_token(user, expires_at), httponly=True, secure=os.getenv("COOKIE_SECURE", "0") == "1", samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    response.set_cookie(REMEMBER_COOKIE, remember_token(user, expires_at), httponly=True, secure=os.getenv("COOKIE_SECURE", "0") == "1", samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    return user_payload(user)


@app.post("/api/admin/login", dependencies=[Depends(csrf_protect)])
def admin_login(payload: AdminLoginRequest, response: Response, db: Session = Depends(db_session)) -> dict:
    user = db.scalar(select(User).where(User.email == str(payload.email).lower()))
    if not user or not user.is_admin or not user_has_access(user) or not check_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="管理员账号或密码错误")
    raw_token = secrets.token_urlsafe(32)
    expires_at = now() + timedelta(days=SESSION_DAYS)
    user_token = remember_token(user, expires_at)
    db.add(AdminSession(user_id=user.id, token_hash=digest(raw_token), expires_at=expires_at))
    db.commit()
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.set_cookie(ADMIN_COOKIE, raw_token, httponly=True, secure=os.getenv("COOKIE_SECURE", "0") == "1", samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    response.set_cookie(AUTH_COOKIE, user_token, httponly=True, secure=os.getenv("COOKIE_SECURE", "0") == "1", samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    response.set_cookie(REMEMBER_COOKIE, remember_token(user, expires_at), httponly=True, secure=os.getenv("COOKIE_SECURE", "0") == "1", samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    return {"token": raw_token, "user_token": user_token, "expires_in_days": SESSION_DAYS, "user": user_payload(user)}


@app.post("/api/admin/password", dependencies=[Depends(csrf_protect)])
def reset_admin_password(payload: AdminPasswordResetRequest, session: AdminSession = Depends(current_admin), db: Session = Depends(db_session)) -> dict:
    user = db.get(User, session.user_id)
    user.password_hash = hash_password(payload.new_password)
    db.query(AdminSession).filter(AdminSession.user_id == user.id).delete()
    db.query(SessionToken).filter(SessionToken.user_id == user.id).delete()
    db.commit()
    return {"message": "管理员密码已重置，请使用新密码重新登录"}


@app.post("/api/admin/update-data", dependencies=[Depends(csrf_protect)])
def admin_update_data(_: AdminSession = Depends(current_admin)) -> dict:
    with DATA_UPDATE_STATUS_LOCK:
        if DATA_UPDATE_STATUS["running"]:
            raise HTTPException(status_code=409, detail="赛事数据更新正在进行中，请稍后查看")
        DATA_UPDATE_STATUS.update(running=True, message="赛事数据更新已在后台启动", output="", started_at=now().isoformat(), finished_at=None, duration_seconds=None)
    write_update_log("赛事数据更新启动")
    threading.Thread(target=run_data_update, daemon=True).start()
    return {"status": "started", "message": "赛事数据更新已在后台启动，请稍后查看结果"}


def run_data_update() -> None:
    if not DATA_UPDATE_LOCK.acquire(blocking=False):
        with DATA_UPDATE_STATUS_LOCK:
            DATA_UPDATE_STATUS.update(running=False, message="赛事数据更新正在进行中，请稍后查看", output="", finished_at=now().isoformat())
        return
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "update_daily_data.py"), "--history-days", "10", "--history-retention", "400"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=1800,
            check=False,
        )
        # Keep stderr as well as stdout.  The updater prints its summary to
        # stdout, so using `stdout or stderr` previously hid the actual MySQL
        # connection/permission error from the administrator.
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        finished_at = now()
        with DATA_UPDATE_STATUS_LOCK:
            started_at = DATA_UPDATE_STATUS.get("started_at")
        duration = None
        if started_at:
            duration = round((finished_at - datetime.fromisoformat(started_at)).total_seconds(), 2)
        with DATA_UPDATE_STATUS_LOCK:
            DATA_UPDATE_STATUS.update(
                running=False,
                message="赛事数据更新完成" if result.returncode == 0 else "赛事数据更新失败",
                output=output[-4000:],
                finished_at=finished_at.isoformat(),
                duration_seconds=duration,
            )
        write_update_log(DATA_UPDATE_STATUS["message"], output)
    except subprocess.TimeoutExpired:
        with DATA_UPDATE_STATUS_LOCK:
            DATA_UPDATE_STATUS.update(running=False, message="赛事数据更新超过 30 分钟，任务已终止", output="", finished_at=now().isoformat())
        write_update_log("赛事数据更新超时")
    except Exception as exc:
        with DATA_UPDATE_STATUS_LOCK:
            DATA_UPDATE_STATUS.update(running=False, message=f"赛事数据更新失败：{exc}", output="", finished_at=now().isoformat())
        write_update_log("赛事数据更新异常", repr(exc))
    finally:
        DATA_UPDATE_LOCK.release()


@app.get("/api/admin/update-data/status")
def admin_update_data_status(_: AdminSession = Depends(current_admin)) -> dict:
    with DATA_UPDATE_STATUS_LOCK:
        return dict(DATA_UPDATE_STATUS)


@app.post("/api/admin/import-data", dependencies=[Depends(csrf_protect)])
def admin_import_data(_: AdminSession = Depends(current_admin)) -> dict:
    results = import_json_snapshots()
    if results.get("matches") != "success":
        raise HTTPException(status_code=500, detail=f"赛事 JSON 导入数据库失败：{results.get('matches', '未知错误')}")
    return {"message": "JSON 数据已导入数据库", "results": results}


@app.get("/api/admin/users")
def admin_users(_: AdminSession = Depends(current_admin), db: Session = Depends(db_session)) -> dict:
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    return {
        "items": [
            {**user_payload(user), "created_at": user.created_at.isoformat(), "bet_count": len(user.bets)}
            for user in users
        ]
    }


@app.patch("/api/admin/users/{user_id}", dependencies=[Depends(csrf_protect)])
def admin_update_user(user_id: int, payload: UserStatusRequest, _: AdminSession = Depends(current_admin), db: Session = Depends(db_session)) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.is_active = payload.is_active
    user.activation_expires_at = None
    if not payload.is_active:
        db.query(SessionToken).filter(SessionToken.user_id == user.id).delete()
    db.commit()
    return user_payload(user)


@app.delete("/api/admin/users/{user_id}", dependencies=[Depends(csrf_protect)])
def admin_delete_user(user_id: int, session: AdminSession | SessionToken = Depends(current_admin), db: Session = Depends(db_session)) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.is_admin or user.id == session.user_id:
        raise HTTPException(status_code=400, detail="不能删除管理员账号")
    db.query(ActivationCode).filter(ActivationCode.used_by == user.id).update({ActivationCode.used_by: None})
    db.query(SessionToken).filter(SessionToken.user_id == user.id).delete()
    db.query(Bet).filter(Bet.user_id == user.id).delete()
    db.delete(user)
    db.commit()
    return {"message": "用户已删除"}


@app.post("/api/admin/activation-codes", dependencies=[Depends(csrf_protect)])
def create_activation_code(payload: ActivationCodeRequest, _: AdminSession = Depends(current_admin), db: Session = Depends(db_session)) -> dict:
    grant_days = {"month": 30, "half_year": 180, "year": 365, "permanent": None}[payload.duration]
    raw_code = f"FC-{secrets.token_hex(6).upper()}"
    record = ActivationCode(
        code_hash=digest(raw_code),
        code_hint=f"...{raw_code[-4:]}",
        expires_at=now() + timedelta(hours=payload.expires_hours),
        grant_days=grant_days,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {"code": raw_code, **activation_code_payload(record)}


@app.get("/api/admin/activation-codes")
def list_activation_codes(_: AdminSession = Depends(current_admin), db: Session = Depends(db_session)) -> dict:
    records = db.scalars(select(ActivationCode).order_by(ActivationCode.created_at.desc()).limit(100)).all()
    return {"items": [activation_code_payload(record) for record in records]}


@app.delete("/api/admin/activation-codes/{code_id}", dependencies=[Depends(csrf_protect)])
def delete_activation_code(code_id: int, _: AdminSession = Depends(current_admin), db: Session = Depends(db_session)) -> dict:
    record = db.get(ActivationCode, code_id)
    if not record:
        raise HTTPException(status_code=404, detail="激活码不存在")
    db.delete(record)
    db.commit()
    return {"message": "激活码已删除"}


PLAY_SELECTIONS = {
    "spf": {"home", "draw", "away"},
    "rqspf": {"home", "draw", "away"},
    "bf": set(),
    "zjq": {str(value) for value in range(0, 8)},
    "bqc": {"home/home", "home/draw", "home/away", "draw/home", "draw/draw", "draw/away", "away/home", "away/draw", "away/away"},
}
BQC_SOURCE_SELECTIONS = {
    "3-3": "home/home", "3-1": "home/draw", "3-0": "home/away",
    "1-3": "draw/home", "1-1": "draw/draw", "1-0": "draw/away",
    "0-3": "away/home", "0-1": "away/draw", "0-0": "away/away",
}
VALID_SELECTIONS = PLAY_SELECTIONS["spf"]


def selection_is_valid(play_type: str, selection: str) -> bool:
    if play_type == "bf":
        return bool(re.fullmatch(r"\d{1,2}-\d{1,2}", selection))
    return selection in PLAY_SELECTIONS.get(play_type, set())


@app.post("/api/bets", dependencies=[Depends(csrf_protect)])
def create_bet(payload: BetRequest, user: User = Depends(current_user), db: Session = Depends(db_session)) -> dict:
    if not any(str(match.get("id")) == payload.match_id for match in load_matches(db)):
        raise HTTPException(status_code=404, detail="赛事不存在或尚未同步")
    if not selection_is_valid(payload.play_type, payload.selection):
        raise HTTPException(status_code=422, detail="玩法选项不符合该足彩玩法")
    if payload.play_type == "rqspf" and payload.handicap is None:
        raise HTTPException(status_code=422, detail="让球胜平负必须填写让球数")
    bet = Bet(user_id=user.id, match_id=payload.match_id, play_type=payload.play_type, selection=payload.selection, handicap=payload.handicap, odds=payload.odds, stake=payload.stake)
    db.add(bet)
    db.commit()
    db.refresh(bet)
    return bet_payload(bet)


@app.get("/api/bets")
def list_bets(user: User = Depends(current_user), db: Session = Depends(db_session)) -> dict:
    settle_all(db)
    bets = db.scalars(select(Bet).where(Bet.user_id == user.id).order_by(Bet.created_at.desc())).all()
    return {"items": [bet_payload(bet) for bet in bets]}


@app.get("/api/bets/summary")
def bet_summary(user: User = Depends(current_user), db: Session = Depends(db_session)) -> dict:
    settle_all(db)
    bets = db.scalars(select(Bet).where(Bet.user_id == user.id)).all()
    settled = [bet for bet in bets if bet.status != "pending"]
    return {
        "count": len(bets),
        "pending": sum(bet.status == "pending" for bet in bets),
        "won": sum(bet.status == "won" for bet in bets),
        "lost": sum(bet.status == "lost" for bet in bets),
        "stake": round(sum(bet.stake for bet in settled), 2),
        "profit": round(sum(bet.profit or 0 for bet in settled), 2),
    }


def load_matches(db: Session | None = None) -> list[dict]:
    if db is not None:
        snapshot = db.get(DataSnapshot, "matches")
        if snapshot and isinstance(snapshot.payload.get("matches"), list):
            return snapshot.payload["matches"]
    try:
        return json.loads((ROOT / "data" / "matches.json").read_text(encoding="utf-8")).get("matches", [])
    except (OSError, json.JSONDecodeError):
        return []


def match_result(match: dict) -> str | None:
    result = match.get("result") or match.get("outcome") or match.get("resultKey")
    if result in VALID_SELECTIONS:
        return result
    score = match.get("score") or match.get("finalScore")
    if isinstance(score, dict):
        home, away = score.get("home"), score.get("away")
        if isinstance(home, (int, float)) and isinstance(away, (int, float)):
            return "home" if home > away else "away" if away > home else "draw"
    return None


def score_pair(value: object) -> tuple[int, int] | None:
    if isinstance(value, dict):
        home, away = value.get("home"), value.get("away")
        if isinstance(home, (int, float)) and isinstance(away, (int, float)):
            return int(home), int(away)
    if isinstance(value, str):
        found = re.fullmatch(r"\s*(\d{1,2})\s*[-:]\s*(\d{1,2})\s*", value)
        if found:
            return int(found.group(1)), int(found.group(2))
    return None


def match_score(match: dict) -> tuple[int, int] | None:
    return score_pair(match.get("finalScore")) or score_pair(match.get("score")) or score_pair({"home": match.get("homeScore"), "away": match.get("awayScore")})


def bet_result(match: dict, bet: Bet) -> str | None:
    score = match_score(match)
    if bet.play_type == "spf":
        return match_result(match)
    if not score:
        return None
    home, away = score
    if bet.play_type == "rqspf":
        adjusted_home = home + (bet.handicap or 0)
        return "home" if adjusted_home > away else "away" if adjusted_home < away else "draw"
    if bet.play_type == "bf":
        return f"{home}-{away}"
    if bet.play_type == "zjq":
        return str(min(home + away, 7))
    if bet.play_type == "bqc":
        half = score_pair(match.get("halfScore") or match.get("halftimeScore"))
        if not half:
            return None
        half_home, half_away = half
        first = "home" if half_home > half_away else "away" if half_home < half_away else "draw"
        second = "home" if home > away else "away" if home < away else "draw"
        return f"{first}/{second}"
    return None


def settle_all(db: Session) -> int:
    matches = {str(match.get("id")): match for match in load_matches(db)}
    bets = db.scalars(select(Bet).where(Bet.status == "pending")).all()
    settled = 0
    for bet in bets:
        result = bet_result(matches.get(str(bet.match_id), {}), bet)
        if not result:
            continue
        bet.result = result
        bet.status = "won" if bet.selection == result else "lost"
        bet.profit = round(bet.stake * (bet.odds - 1), 2) if bet.status == "won" else round(-bet.stake, 2)
        bet.settled_at = now()
        settled += 1
    db.commit()
    return settled


@app.post("/api/admin/settle")
def settle(x_admin_key: Annotated[str | None, Header()] = None, db: Session = Depends(db_session)) -> dict:
    if not x_admin_key or not hmac.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(status_code=403, detail="管理员密钥错误")
    return {"settled": settle_all(db)}


@app.get("/api/admin/settle")
def settle_get(x_admin_key: Annotated[str | None, Header()] = None, db: Session = Depends(db_session)) -> dict:
    return settle(x_admin_key, db)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(ROOT / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/admin.html", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(ROOT / "admin.html", headers={"Cache-Control": "no-cache"})


@app.get("/account.html", include_in_schema=False)
def account_page() -> FileResponse:
    return FileResponse(ROOT / "account.html", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(ROOT / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


app.mount("/data", StaticFiles(directory=str(ROOT / "data")), name="data")
