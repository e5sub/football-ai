from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("DATABASE_PATH", ROOT / "runtime" / "app.db"))
PORT = int(os.getenv("PORT", "8080"))
PBKDF2_ROUNDS = 240_000
DB_LOCK = threading.RLock()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    with DB_LOCK, connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                activation_code TEXT NOT NULL,
                activated_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS bets (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                match_id TEXT,
                event_name TEXT NOT NULL,
                selection TEXT NOT NULL,
                stake_cents INTEGER NOT NULL CHECK(stake_cents > 0),
                odds TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'won', 'lost', 'void')),
                profit_cents INTEGER NOT NULL DEFAULT 0,
                placed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                settled_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bets_user_placed ON bets(user_id, placed_at DESC);
            """
        )


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"{salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest_hex = encoded.split("$", 1)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ROUNDS)
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def json_body(handler: SimpleHTTPRequestHandler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        payload = json.loads(handler.rfile.read(length) or b"{}")
        return payload if isinstance(payload, dict) else {}
    except (ValueError, json.JSONDecodeError):
        raise ValueError("请求体必须是 JSON 对象")


def cents(value: object) -> int:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("金额格式无效")
    if amount <= 0:
        raise ValueError("金额必须大于 0")
    return int(amount * 100)


def odds_value(value: object) -> Decimal:
    try:
        odds = Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("赔率格式无效")
    if odds < Decimal("1.001") or odds > Decimal("1000"):
        raise ValueError("赔率必须在 1.001 到 1000 之间")
    return odds


def user_json(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "email": row["email"], "activated": bool(row["activated_at"]), "createdAt": row["created_at"]}


def bet_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "matchId": row["match_id"], "eventName": row["event_name"],
        "selection": row["selection"], "stake": row["stake_cents"] / 100,
        "odds": float(row["odds"]), "status": row["status"], "profit": row["profit_cents"] / 100,
        "placedAt": row["placed_at"], "settledAt": row["settled_at"],
    }


class AppHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", os.getenv("CORS_ORIGIN", "*"))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", os.getenv("CORS_ORIGIN", "*"))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.startswith("/api/"):
            self.api_get()
        else:
            super().do_GET()

    def do_POST(self) -> None:
        if self.path.startswith("/api/"):
            self.api_post()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        if self.path.startswith("/api/"):
            self.api_delete()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def auth_user(self) -> sqlite3.Row | None:
        header = self.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ").strip()
        if not token:
            return None
        with DB_LOCK, connect() as db:
            return db.execute(
                "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id WHERE sessions.token = ?",
                (token,),
            ).fetchone()

    def require_user(self) -> sqlite3.Row:
        user = self.auth_user()
        if not user:
            raise PermissionError("请先登录")
        if not user["activated_at"]:
            raise PermissionError("账号尚未激活")
        return user

    def api_get(self) -> None:
        try:
            if self.path == "/api/health":
                self.send_json({"status": "ok", "database": str(DB_PATH)})
                return
            user = self.require_user()
            if self.path == "/api/me":
                self.send_json({"user": user_json(user)})
                return
            if self.path == "/api/bets":
                with DB_LOCK, connect() as db:
                    rows = db.execute("SELECT * FROM bets WHERE user_id = ? ORDER BY placed_at DESC", (user["id"],)).fetchall()
                total_stake = sum(row["stake_cents"] for row in rows)
                total_profit = sum(row["profit_cents"] for row in rows)
                self.send_json({"bets": [bet_json(row) for row in rows], "summary": {"count": len(rows), "stake": total_stake / 100, "profit": total_profit / 100}})
                return
            self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except PermissionError as error:
            self.send_json({"error": str(error)}, HTTPStatus.UNAUTHORIZED)
        except Exception as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def api_post(self) -> None:
        try:
            payload = json_body(self)
            path = urlparse(self.path).path
            if path == "/api/auth/register":
                email = str(payload.get("email", "")).strip().lower()
                password = str(payload.get("password", ""))
                if "@" not in email or len(email) > 190:
                    raise ValueError("请输入有效邮箱")
                if len(password) < 8:
                    raise ValueError("密码至少 8 位")
                activation_code = secrets.token_urlsafe(6).upper()
                user_id = str(uuid.uuid4())
                with DB_LOCK, connect() as db:
                    db.execute("INSERT INTO users(id,email,password_hash,activation_code) VALUES(?,?,?,?)", (user_id, email, password_hash(password), activation_code))
                self.send_json({"message": "注册成功，请使用激活码激活账号", "activationCode": activation_code}, HTTPStatus.CREATED)
                return
            if path == "/api/auth/activate":
                email = str(payload.get("email", "")).strip().lower()
                code = str(payload.get("code", "")).strip().upper()
                with DB_LOCK, connect() as db:
                    cursor = db.execute("UPDATE users SET activated_at = ? WHERE email = ? AND activation_code = ?", (now_iso(), email, code))
                if cursor.rowcount != 1:
                    raise ValueError("邮箱或激活码不正确")
                self.send_json({"message": "账号已激活"})
                return
            if path == "/api/auth/login":
                email = str(payload.get("email", "")).strip().lower()
                with DB_LOCK, connect() as db:
                    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                    if not user or not password_matches(str(payload.get("password", "")), user["password_hash"]):
                        raise PermissionError("邮箱或密码错误")
                    if not user["activated_at"]:
                        raise PermissionError("账号尚未激活")
                    token = secrets.token_urlsafe(32)
                    db.execute("INSERT INTO sessions(token,user_id) VALUES(?,?)", (token, user["id"]))
                self.send_json({"token": token, "user": user_json(user)})
                return
            user = self.require_user()
            if path == "/api/bets":
                event_name = str(payload.get("eventName", "")).strip()
                selection = str(payload.get("selection", "")).strip()
                if not event_name or not selection:
                    raise ValueError("赛事和选择不能为空")
                stake = cents(payload.get("stake"))
                odds = odds_value(payload.get("odds"))
                bet_id = str(uuid.uuid4())
                with DB_LOCK, connect() as db:
                    row = db.execute("INSERT INTO bets(id,user_id,match_id,event_name,selection,stake_cents,odds) VALUES(?,?,?,?,?,?,?) RETURNING *", (bet_id, user["id"], payload.get("matchId"), event_name, selection, stake, str(odds))).fetchone()
                self.send_json({"bet": bet_json(row)}, HTTPStatus.CREATED)
                return
            if path.startswith("/api/bets/") and path.endswith("/settle"):
                bet_id = path.split("/")[3]
                status = str(payload.get("status", "")).lower()
                if status not in {"won", "lost", "void"}:
                    raise ValueError("结算状态必须是 won、lost 或 void")
                with DB_LOCK, connect() as db:
                    row = db.execute("SELECT * FROM bets WHERE id = ? AND user_id = ?", (bet_id, user["id"])).fetchone()
                    if not row:
                        raise ValueError("投注记录不存在")
                    if row["status"] != "pending":
                        raise ValueError("该记录已经结算")
                    profit = row["stake_cents"] * (Decimal(row["odds"]) - 1) if status == "won" else (Decimal(0) if status == "void" else -Decimal(row["stake_cents"]))
                    profit_cents = int(profit.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                    updated = db.execute("UPDATE bets SET status = ?, profit_cents = ?, settled_at = ? WHERE id = ? RETURNING *", (status, profit_cents, now_iso(), bet_id)).fetchone()
                self.send_json({"bet": bet_json(updated)})
                return
            self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except PermissionError as error:
            self.send_json({"error": str(error)}, HTTPStatus.UNAUTHORIZED)
        except sqlite3.IntegrityError:
            self.send_json({"error": "邮箱已注册或数据冲突"}, HTTPStatus.CONFLICT)
        except Exception as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def api_delete(self) -> None:
        try:
            user = self.require_user()
            path = urlparse(self.path).path
            if path.startswith("/api/bets/"):
                bet_id = path.split("/")[3]
                with DB_LOCK, connect() as db:
                    cursor = db.execute("DELETE FROM bets WHERE id = ? AND user_id = ? AND status = 'pending'", (bet_id, user["id"]))
                if cursor.rowcount != 1:
                    raise ValueError("只能删除自己的待结算记录")
                self.send_json({"message": "已删除"})
                return
            self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except PermissionError as error:
            self.send_json({"error": str(error)}, HTTPStatus.UNAUTHORIZED)
        except Exception as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


if __name__ == "__main__":
    init_db()
    print(f"Football command center listening on http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), AppHandler).serve_forever()