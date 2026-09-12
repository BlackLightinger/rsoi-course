from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from services.common.config import API_AUDIENCE, DATA_DIR, OIDC_ISSUER, service_db
from services.common.database import Database
from services.common.events import install_observability

app = FastAPI(title="Flight Platform Identity Provider", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
events = install_observability(app, "idp")
db = Database(service_db("identity"))

KEY_ID = os.getenv("OIDC_KEY_ID", "flight-platform-key-1")
ACCESS_TOKEN_TTL = 600
AUTH_CODE_TTL = 180


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _load_or_create_key() -> rsa.RSAPrivateKey:
    configured = os.getenv("OIDC_PRIVATE_KEY_PATH")
    path = Path(configured) if configured else DATA_DIR / "oidc-private-key.pem"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


private_key = _load_or_create_key()
public_numbers = private_key.public_key().public_numbers()
public_jwk = {
    "kty": "RSA",
    "use": "sig",
    "alg": "RS256",
    "kid": KEY_ID,
    "n": _b64url(public_numbers.n.to_bytes((public_numbers.n.bit_length() + 7) // 8, "big")),
    "e": _b64url(public_numbers.e.to_bytes((public_numbers.e.bit_length() + 7) // 8, "big")),
}


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    result = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${result.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        _, salt_hex, expected = encoded.split("$", 2)
        actual = password_hash(password, bytes.fromhex(salt_hex)).split("$", 2)[2]
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def init_db() -> None:
    with db.transaction() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                full_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS authorization_codes (
                code_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                scope TEXT NOT NULL,
                nonce TEXT,
                code_challenge TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS refresh_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                client_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )
        existing = connection.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        if not existing:
            connection.execute(
                "INSERT INTO users(username,email,full_name,password_hash,role,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    "admin",
                    "admin@example.test",
                    "Администратор",
                    password_hash(os.getenv("ADMIN_PASSWORD", "admin123")),
                    "admin",
                    datetime.now(UTC).isoformat(),
                ),
            )


init_db()


def _web_redirects() -> set[str]:
    raw = os.getenv(
        "WEB_REDIRECT_URIS",
        "http://localhost:5173/callback,http://localhost/callback,"
        "http://127.0.0.1:5173/callback,http://127.0.0.1/callback",
    )
    redirects = {value.strip() for value in raw.split(",") if value.strip()}
    # Both loopback hostnames are used by Docker Desktop during local development.
    redirects.update(
        {
            "http://localhost:5173/callback",
            "http://localhost/callback",
            "http://127.0.0.1:5173/callback",
            "http://127.0.0.1/callback",
        }
    )
    return redirects


def client_config(client_id: str) -> dict[str, Any] | None:
    clients = {
        "flight-web": {
            "public": True,
            "redirect_uris": _web_redirects(),
            "scopes": {
                "openid",
                "profile",
                "email",
                "flights:read",
                "tickets:read",
                "tickets:write",
                "loyalty:read",
                "loyalty:write",
                "payments:read",
                "payments:write",
                "stats:read",
            },
        },
        "gateway": {
            "public": False,
            "secret": os.getenv("GATEWAY_CLIENT_SECRET", "gateway-dev-secret"),
            "redirect_uris": set(),
            "scopes": {
                "flights:read",
                "flights:write",
                "tickets:read",
                "tickets:write",
                "loyalty:read",
                "loyalty:write",
                "payments:read",
                "payments:write",
                "stats:read",
                "users:write",
            },
        },
    }
    return clients.get(client_id)


def scopes_for_user(requested: str, client: dict[str, Any], role: str) -> str:
    scopes = set(requested.split()) & client["scopes"]
    if role != "admin":
        scopes.discard("stats:read")
    return " ".join(sorted(scopes))


def issue_access_token(subject: str, username: str, role: str, scope: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": OIDC_ISSUER,
        "sub": subject,
        "aud": API_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(seconds=ACCESS_TOKEN_TTL),
        "jti": secrets.token_urlsafe(16),
        "preferred_username": username,
        "role": role,
        "scope": scope,
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": KEY_ID})


def issue_id_token(user: dict[str, Any], client_id: str, nonce: str | None) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": OIDC_ISSUER,
        "sub": f"user:{user['id']}",
        "aud": client_id,
        "iat": now,
        "exp": now + timedelta(seconds=ACCESS_TOKEN_TTL),
        "auth_time": int(time.time()),
        "preferred_username": user["username"],
        "email": user["email"],
        "name": user["full_name"],
        "role": user["role"],
    }
    if nonce:
        payload["nonce"] = nonce
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": KEY_ID})


class Registration(BaseModel):
    username: str = Field(min_length=3, max_length=40, pattern=r"^[a-zA-Z0-9_.-]+$")
    email: str = Field(min_length=5, max_length=200)
    full_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=8, max_length=128)


class ProfileUpdate(BaseModel):
    email: str = Field(min_length=5, max_length=200)
    full_name: str = Field(min_length=2, max_length=120)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "idp"}


@app.get("/.well-known/openid-configuration")
async def discovery() -> dict[str, Any]:
    return {
        "issuer": OIDC_ISSUER,
        "authorization_endpoint": f"{OIDC_ISSUER}/oauth2/authorize",
        "token_endpoint": f"{OIDC_ISSUER}/oauth2/token",
        "userinfo_endpoint": f"{OIDC_ISSUER}/oauth2/userinfo",
        "jwks_uri": f"{OIDC_ISSUER}/oauth2/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": [
            "openid",
            "profile",
            "email",
            "flights:read",
            "tickets:read",
            "tickets:write",
            "loyalty:read",
            "loyalty:write",
            "payments:read",
            "payments:write",
            "stats:read",
            "users:write",
        ],
        "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
    }


@app.get("/oauth2/jwks")
async def jwks() -> dict[str, list[dict[str, str]]]:
    return {"keys": [public_jwk]}


def authorize_error(redirect_uri: str, state: str, error: str, description: str):
    query = urlencode({"error": error, "error_description": description, "state": state})
    return RedirectResponse(f"{redirect_uri}?{query}", status_code=303)


@app.get("/oauth2/authorize", response_class=HTMLResponse)
async def authorize_form(
    client_id: str,
    redirect_uri: str,
    response_type: str,
    scope: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
    nonce: str = "",
) -> HTMLResponse:
    client = client_config(client_id)
    if not client or redirect_uri not in client["redirect_uris"]:
        raise HTTPException(400, "Неизвестный client_id или redirect_uri")
    if response_type != "code" or code_challenge_method != "S256" or not code_challenge:
        return authorize_error(redirect_uri, state, "invalid_request", "Требуется Authorization Code + PKCE S256")
    hidden = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": response_type,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "nonce": nonce,
    }
    hidden_html = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in hidden.items()
    )
    return HTMLResponse(
        f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width"><title>Вход — AeroFlow</title>
        <style>body{{font:16px system-ui;background:#07111f;color:#eef6ff;display:grid;place-items:center;min-height:100vh}}
        form{{background:#10233c;padding:32px;border-radius:18px;min-width:300px;box-shadow:0 20px 60px #0008}}
        label{{display:block;margin:14px 0 5px}}input{{box-sizing:border-box;width:100%;padding:11px;border:1px solid #38506d;border-radius:8px}}
        button{{width:100%;margin-top:22px;padding:12px;border:0;border-radius:8px;background:#48d6c9;font-weight:700}}</style></head>
        <body><form method="post" action="/oauth2/authorize"><h1>AeroFlow ID</h1>
        <p>Вход в систему бронирования</p>{hidden_html}
        <label>Логин</label><input name="username" autocomplete="username" required>
        <label>Пароль</label><input type="password" name="password" autocomplete="current-password" required>
        <button type="submit">Продолжить</button><small><p>Демо: admin / admin123</p>
        <p>Нет аккаунта? <a href="/?register=1" style="color:#48d6c9">Зарегистрироваться</a></p></small></form></body></html>"""
    )


@app.post("/oauth2/authorize")
async def authorize_submit(
    client_id: Annotated[str, Form()],
    redirect_uri: Annotated[str, Form()],
    response_type: Annotated[str, Form()],
    scope: Annotated[str, Form()],
    state: Annotated[str, Form()],
    code_challenge: Annotated[str, Form()],
    code_challenge_method: Annotated[str, Form()],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    nonce: Annotated[str, Form()] = "",
):
    client = client_config(client_id)
    if not client or redirect_uri not in client["redirect_uris"]:
        raise HTTPException(400, "Некорректный клиент")
    if response_type != "code" or code_challenge_method != "S256":
        return authorize_error(redirect_uri, state, "invalid_request", "Некорректный OAuth-запрос")
    user = db.fetchone("SELECT * FROM users WHERE username=?", (username,))
    if not user or not password_matches(password, user["password_hash"]):
        return authorize_error(redirect_uri, state, "access_denied", "Неверный логин или пароль")
    code = secrets.token_urlsafe(32)
    granted = scopes_for_user(scope, client, user["role"])
    db.execute(
        "INSERT INTO authorization_codes(code_hash,user_id,client_id,redirect_uri,scope,nonce,code_challenge,expires_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            hashlib.sha256(code.encode()).hexdigest(),
            user["id"],
            client_id,
            redirect_uri,
            granted,
            nonce,
            code_challenge,
            int(time.time()) + AUTH_CODE_TTL,
        ),
    )
    await events.publish("user_authenticated", subject=f"user:{user['id']}", client_id=client_id)
    return RedirectResponse(
        f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=303
    )


def token_response(user: dict[str, Any], client_id: str, scope: str, nonce: str | None = None):
    subject = f"user:{user['id']}"
    refresh_token = secrets.token_urlsafe(48)
    db.execute(
        "INSERT INTO refresh_tokens(token_hash,user_id,client_id,scope,expires_at) VALUES(?,?,?,?,?)",
        (
            hashlib.sha256(refresh_token.encode()).hexdigest(),
            user["id"],
            client_id,
            scope,
            int(time.time()) + 7 * 86400,
        ),
    )
    return {
        "access_token": issue_access_token(subject, user["username"], user["role"], scope),
        "id_token": issue_id_token(user, client_id, nonce),
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "scope": scope,
    }


@app.post("/oauth2/token")
async def token_endpoint(
    grant_type: Annotated[str, Form()],
    client_id: Annotated[str, Form()],
    client_secret: Annotated[str | None, Form()] = None,
    code: Annotated[str | None, Form()] = None,
    redirect_uri: Annotated[str | None, Form()] = None,
    code_verifier: Annotated[str | None, Form()] = None,
    refresh_token: Annotated[str | None, Form()] = None,
):
    client = client_config(client_id)
    if not client:
        raise HTTPException(401, "Неизвестный клиент")
    if not client["public"] and not secrets.compare_digest(
        client_secret or "", str(client.get("secret", ""))
    ):
        raise HTTPException(401, "Неверный секрет клиента")

    if grant_type == "client_credentials":
        if client["public"]:
            raise HTTPException(400, "Публичный клиент не может использовать client_credentials")
        scope = " ".join(sorted(client["scopes"]))
        return {
            "access_token": issue_access_token(f"service:{client_id}", client_id, "service", scope),
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "scope": scope,
        }

    if grant_type == "authorization_code":
        if not code or not redirect_uri or not code_verifier:
            raise HTTPException(400, "Не хватает параметров authorization_code")
        row = db.fetchone(
            "SELECT * FROM authorization_codes WHERE code_hash=?",
            (hashlib.sha256(code.encode()).hexdigest(),),
        )
        challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
        if (
            not row
            or row["consumed"]
            or row["expires_at"] < int(time.time())
            or row["client_id"] != client_id
            or row["redirect_uri"] != redirect_uri
            or not secrets.compare_digest(challenge, row["code_challenge"])
        ):
            raise HTTPException(400, "Недействительный код или PKCE verifier")
        db.execute("UPDATE authorization_codes SET consumed=1 WHERE code_hash=?", (row["code_hash"],))
        user = db.fetchone("SELECT * FROM users WHERE id=?", (row["user_id"],))
        assert user
        return token_response(user, client_id, row["scope"], row["nonce"])

    if grant_type == "refresh_token":
        if not refresh_token:
            raise HTTPException(400, "Не передан refresh_token")
        row = db.fetchone(
            "SELECT * FROM refresh_tokens WHERE token_hash=?",
            (hashlib.sha256(refresh_token.encode()).hexdigest(),),
        )
        if not row or row["revoked"] or row["expires_at"] < int(time.time()) or row["client_id"] != client_id:
            raise HTTPException(400, "Недействительный refresh_token")
        db.execute("UPDATE refresh_tokens SET revoked=1 WHERE token_hash=?", (row["token_hash"],))
        user = db.fetchone("SELECT * FROM users WHERE id=?", (row["user_id"],))
        assert user
        return token_response(user, client_id, row["scope"])

    raise HTTPException(400, "Неподдерживаемый grant_type")


def local_bearer(authorization: str = Header(default="")) -> dict[str, Any]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Требуется Bearer-токен")
    try:
        return jwt.decode(
            authorization.removeprefix("Bearer "),
            private_key.public_key(),
            algorithms=["RS256"],
            audience=API_AUDIENCE,
            issuer=OIDC_ISSUER,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "Недействительный токен") from exc


@app.get("/oauth2/userinfo")
async def userinfo(claims: dict[str, Any] = Depends(local_bearer)) -> dict[str, Any]:
    if not str(claims["sub"]).startswith("user:"):
        raise HTTPException(403, "UserInfo доступен только пользователям")
    user = db.fetchone("SELECT * FROM users WHERE id=?", (claims["sub"].split(":", 1)[1],))
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    return {
        "sub": claims["sub"],
        "preferred_username": user["username"],
        "email": user["email"],
        "name": user["full_name"],
        "role": user["role"],
    }


@app.post("/users", status_code=201)
async def register(
    payload: Registration, claims: dict[str, Any] = Depends(local_bearer)
) -> dict[str, str]:
    scopes = set(str(claims.get("scope", "")).split())
    if claims.get("role") != "service" or "users:write" not in scopes:
        raise HTTPException(403, "Регистрация доступна только авторизованному сервису")
    try:
        user_id = db.execute(
            "INSERT INTO users(username,email,full_name,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
            (
                payload.username,
                payload.email,
                payload.full_name,
                password_hash(payload.password),
                "user",
                datetime.now(UTC).isoformat(),
            ),
        )
    except Exception as exc:
        raise HTTPException(409, "Логин или email уже используется") from exc
    await events.publish("user_registered", subject=f"user:{user_id}")
    return {"id": f"user:{user_id}", "username": payload.username}


@app.patch("/users/me")
async def update_profile(
    payload: ProfileUpdate, claims: dict[str, Any] = Depends(local_bearer)
) -> dict[str, str]:
    if not str(claims["sub"]).startswith("user:"):
        raise HTTPException(403, "Доступно только пользователю")
    try:
        db.execute(
            "UPDATE users SET email=?, full_name=? WHERE id=?",
            (payload.email, payload.full_name, claims["sub"].split(":", 1)[1]),
        )
    except Exception as exc:
        raise HTTPException(409, "Email уже используется") from exc
    return {"status": "updated"}
