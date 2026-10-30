"""Cosmo: каркас защищённого мессенджера на FastAPI + SQLite.
Запуск: uvicorn server:app --host 0.0.0.0 --port 8000
"""
import hashlib, hmac, json, re, secrets, sqlite3, time
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).parent
DB = sqlite3.connect(BASE / "cosmo.db", check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  salt BLOB NOT NULL, pwhash BLOB NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, sender INTEGER NOT NULL, recipient INTEGER NOT NULL,
  body TEXT NOT NULL,
  ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_msg ON messages(sender, recipient, ts);
""")

USERNAME = re.compile(r"^[a-z0-9_]{3,24}$")
SESSION_TTL = 7 * 24 * 3600

app = FastAPI(docs_url=None, redoc_url=None)


# ---------- безопасность ----------
@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.update({
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; connect-src 'self' ws: wss:; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    })
    return resp


def hash_pw(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_fails: dict[str, list[float]] = defaultdict(list)


def check_rate(key: str, limit: int = 5, window: int = 300):
    now = time.time()
    _fails[key] = [t for t in _fails[key] if now - t < window]
    if len(_fails[key]) >= limit:
        raise HTTPException(429, "Слишком много попыток. Подождите несколько минут")


def user_by_token(token: str | None):
    if not token:
        return None
    row = DB.execute(
        "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires > ?", (token_hash(token), time.time())).fetchone()
    return row


def require_user(authorization: str | None):
    user = user_by_token((authorization or "").removeprefix("Bearer "))
    if not user:
        raise HTTPException(401, "Нужно войти")
    return user


def new_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    DB.execute("INSERT INTO sessions VALUES(?,?,?)", (token_hash(token), user_id, time.time() + SESSION_TTL))
    DB.commit()
    return token


# ---------- API ----------
class Creds(BaseModel):
    username: str = Field(min_length=3, max_length=24)
    password: str = Field(min_length=8, max_length=128)


@app.post("/api/register")
def register(c: Creds, request: Request):
    name = c.username.lower()
    if not USERNAME.match(name):
        raise HTTPException(400, "Имя: 3–24 символа, латиница, цифры и _")
    check_rate("reg:" + request.client.host, 5, 3600)
    _fails["reg:" + request.client.host].append(time.time())
    salt = secrets.token_bytes(16)
    try:
        cur = DB.execute("INSERT INTO users(username,salt,pwhash,created) VALUES(?,?,?,?)",
                         (name, salt, hash_pw(c.password, salt), time.time()))
        DB.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Это имя уже занято")
    return {"token": new_session(cur.lastrowid), "username": name}


@app.post("/api/login")
def login(c: Creds, request: Request):
    key = f"login:{request.client.host}:{c.username.lower()}"
    check_rate(key)
    row = DB.execute("SELECT * FROM users WHERE username = ?", (c.username.lower(),)).fetchone()
    salt = row["salt"] if row else b"\0" * 16
    ok = hmac.compare_digest(hash_pw(c.password, salt), row["pwhash"] if row else b"")
    if not (row and ok):
        _fails[key].append(time.time())
        raise HTTPException(401, "Неверное имя или пароль")
    return {"token": new_session(row["id"]), "username": row["username"]}


@app.post("/api/logout")
def logout(authorization: str | None = Header(None)):
    DB.execute("DELETE FROM sessions WHERE token_hash = ?",
               (token_hash((authorization or "").removeprefix("Bearer ")),))
    DB.commit()
    return {"ok": True}


@app.get("/api/users")
def search_users(q: str = "", authorization: str | None = Header(None)):
    me = require_user(authorization)
    q = re.sub(r"[^a-z0-9_]", "", q.lower())[:24]
    rows = DB.execute("SELECT username FROM users WHERE username LIKE ? AND id != ? LIMIT 20",
                      (q + "%", me["id"])).fetchall()
    return [r["username"] for r in rows]


@app.get("/api/messages")
def history(with_: str, authorization: str | None = Header(None)):
    me = require_user(authorization)
    peer = DB.execute("SELECT id FROM users WHERE username = ?", (with_.lower(),)).fetchone()
    if not peer:
        raise HTTPException(404, "Пользователь не найден")
    rows = DB.execute(
        "SELECT sender, body, ts FROM messages WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?) "
        "ORDER BY ts DESC LIMIT 100", (me["id"], peer["id"], peer["id"], me["id"])).fetchall()
    return [{"mine": r["sender"] == me["id"], "body": r["body"], "ts": r["ts"]} for r in reversed(rows)]


# ---------- WebSocket ----------
online: dict[int, set[WebSocket]] = defaultdict(set)


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    try:
        first = json.loads(await sock.receive_text())
        me = user_by_token(first.get("token"))
        if not me:
            await sock.close(4401)
            return
        online[me["id"]].add(sock)
        last = 0.0
        while True:
            data = json.loads(await sock.receive_text())
            body = str(data.get("body", ""))[:4000]
            if not body.strip() or time.time() - last < 0.2:
                continue
            last = time.time()
            peer = DB.execute("SELECT id, username FROM users WHERE username = ?",
                              (str(data.get("to", "")).lower(),)).fetchone()
            if not peer:
                continue
            ts = time.time()
            DB.execute("INSERT INTO messages(sender,recipient,body,ts) VALUES(?,?,?,?)",
                       (me["id"], peer["id"], body, ts))
            DB.commit()
            out = {"from": me["username"], "to": peer["username"], "body": body, "ts": ts}
            for uid in (peer["id"], me["id"]):
                for s in list(online[uid]):
                    try:
                        await s.send_text(json.dumps(out))
                    except Exception:
                        online[uid].discard(s)
    except (WebSocketDisconnect, ValueError):
        pass
    finally:
        for conns in online.values():
            conns.discard(sock)


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
