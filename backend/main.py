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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Response, status
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
CSRF_COOKIE = "football_ai_csrf"
DATA_UPDATE_LOCK = threading.Lock()
DATA_UPDATE_STATUS_LOCK = threading.Lock()
DATA_UPDATE_STATUS = {"running": False, "message": "", "output": ""}


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


def current_user(authorization: Annotated[str | None, Header()] = None, db: Session = Depends(db_session)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    token = authorization.split(" ", 1)[1].strip()
    session_token = db.scalar(select(SessionToken).where(SessionToken.token_hash == digest(token)))
    if not session_token or as_utc(session_token.expires_at) < now():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")
    user = db.get(User, session_token.user_id)
    if not user or not user_has_access(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号尚未激活")
    session_token.expires_at = now() + timedelta(days=SESSION_DAYS)
    db.commit()
    return user


def current_admin(authorization: Annotated[str | None, Header()] = None, db: Session = Depends(db_session)) -> AdminSession | SessionToken:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="需要管理员登录")
    token = authorization.split(" ", 1)[1].strip()
    session = db.scalar(select(AdminSession).where(AdminSession.token_hash == digest(token)))
    if session:
        if as_utc(session.expires_at) < now():
            raise HTTPException(status_code=401, detail="管理员会话已过期")
        user = db.get(User, session.user_id) if session.user_id else None
        admin_session = session
    else:
        user_session = db.scalar(select(SessionToken).where(SessionToken.token_hash == digest(token)))
        if not user_session or as_utc(user_session.expires_at) < now():
            raise HTTPException(status_code=401, detail="管理员会话已过期")
        user = db.get(User, user_session.user_id)
        admin_session = user_session
    if not user or not user.is_admin or not user_has_access(user):
        raise HTTPException(status_code=403, detail="没有管理员权限")
    admin_session.expires_at = now() + timedelta(days=SESSION_DAYS)
    db.commit()
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
            return candidate.get("current", candidate)
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
    response.set_cookie(CSRF_COOKIE, token, httponly=False, secure=os.getenv("COOKIE_SECURE", "0") == "1", samesite="strict", max_age=86400)
    return {"csrf_token": token}


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
def login(payload: LoginRequest, db: Session = Depends(db_session)) -> dict:
    email = str(payload.email).lower()
    user = db.scalar(select(User).where(User.email == email))
    if not user or not check_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    if not user_has_access(user):
        raise HTTPException(status_code=403, detail="账号使用权已过期，请使用激活码或联系管理员")
    raw_token = secrets.token_urlsafe(32)
    db.add(SessionToken(user_id=user.id, token_hash=digest(raw_token), expires_at=now() + timedelta(days=SESSION_DAYS)))
    db.commit()
    return {"token": raw_token, "user": user_payload(user)}


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)) -> dict:
    return user_payload(user)


@app.post("/api/admin/login", dependencies=[Depends(csrf_protect)])
def admin_login(payload: AdminLoginRequest, db: Session = Depends(db_session)) -> dict:
    user = db.scalar(select(User).where(User.email == str(payload.email).lower()))
    if not user or not user.is_admin or not user_has_access(user) or not check_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="管理员账号或密码错误")
    raw_token = secrets.token_urlsafe(32)
    db.add(AdminSession(user_id=user.id, token_hash=digest(raw_token), expires_at=now() + timedelta(days=SESSION_DAYS)))
    db.commit()
    return {"token": raw_token, "expires_in_days": SESSION_DAYS, "user": user_payload(user)}


@app.post("/api/admin/password", dependencies=[Depends(csrf_protect)])
def reset_admin_password(payload: AdminPasswordResetRequest, session: AdminSession = Depends(current_admin), db: Session = Depends(db_session)) -> dict:
    user = db.get(User, session.user_id)
    user.password_hash = hash_password(payload.new_password)
    db.query(AdminSession).filter(AdminSession.user_id == user.id).delete()
    db.commit()
    return {"message": "管理员密码已重置，请使用新密码重新登录"}


@app.post("/api/admin/update-data", dependencies=[Depends(csrf_protect)])
def admin_update_data(_: AdminSession = Depends(current_admin)) -> dict:
    with DATA_UPDATE_STATUS_LOCK:
        if DATA_UPDATE_STATUS["running"]:
            raise HTTPException(status_code=409, detail="赛事数据更新正在进行中，请稍后查看")
        DATA_UPDATE_STATUS.update(running=True, message="赛事数据更新已在后台启动", output="")
    threading.Thread(target=run_data_update, daemon=True).start()
    return {"status": "started", "message": "赛事数据更新已在后台启动，请稍后查看结果"}


def run_data_update() -> None:
    if not DATA_UPDATE_LOCK.acquire(blocking=False):
        with DATA_UPDATE_STATUS_LOCK:
            DATA_UPDATE_STATUS.update(running=False, message="赛事数据更新正在进行中，请稍后查看", output="")
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
        output = (result.stdout or result.stderr or "").strip()
        with DATA_UPDATE_STATUS_LOCK:
            DATA_UPDATE_STATUS.update(
                running=False,
                message="赛事数据更新完成" if result.returncode == 0 else "赛事数据更新失败",
                output=output[-4000:],
            )
    except subprocess.TimeoutExpired:
        with DATA_UPDATE_STATUS_LOCK:
            DATA_UPDATE_STATUS.update(running=False, message="赛事数据更新超过 30 分钟，任务已终止", output="")
    except Exception as exc:
        with DATA_UPDATE_STATUS_LOCK:
            DATA_UPDATE_STATUS.update(running=False, message=f"赛事数据更新失败：{exc}", output="")
    finally:
        DATA_UPDATE_LOCK.release()


@app.get("/api/admin/update-data/status")
def admin_update_data_status(_: AdminSession = Depends(current_admin)) -> dict:
    with DATA_UPDATE_STATUS_LOCK:
        return dict(DATA_UPDATE_STATUS)


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


PLAY_SELECTIONS = {
    "spf": {"home", "draw", "away"},
    "rqspf": {"home", "draw", "away"},
    "bf": set(),
    "zjq": {str(value) for value in range(0, 8)},
    "bqc": {"home/home", "home/draw", "home/away", "draw/home", "draw/draw", "draw/away", "away/home", "away/draw", "away/away"},
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
    return FileResponse(ROOT / "index.html")


@app.get("/admin.html", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(ROOT / "admin.html")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(ROOT / "sw.js", media_type="application/javascript")


app.mount("/data", StaticFiles(directory=str(ROOT / "data")), name="data")
