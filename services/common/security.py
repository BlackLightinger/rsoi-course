from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
import jwt
from fastapi import HTTPException, Request, status
from jwt.algorithms import RSAAlgorithm

from services.common.config import API_AUDIENCE, OIDC_INTERNAL_URL, OIDC_ISSUER


@dataclass(frozen=True)
class AuthContext:
    subject: str
    username: str
    role: str
    scopes: frozenset[str]
    token: str
    claims: dict[str, Any]


class JWTVerifier:
    def __init__(self) -> None:
        self._keys: dict[str, Any] = {}
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _refresh_keys(self) -> None:
        async with self._lock:
            if self._keys and self._expires_at > time.monotonic():
                return
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{OIDC_INTERNAL_URL}/oauth2/jwks")
                response.raise_for_status()
            self._keys = {
                item["kid"]: RSAAlgorithm.from_jwk(item)
                for item in response.json().get("keys", [])
            }
            self._expires_at = time.monotonic() + 300

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("kid") not in self._keys or self._expires_at <= time.monotonic():
                await self._refresh_keys()
            key = self._keys[header["kid"]]
            return jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=API_AUDIENCE,
                issuer=OIDC_ISSUER,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except (KeyError, jwt.PyJWTError, httpx.HTTPError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Недействительный или просроченный токен",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc


verifier = JWTVerifier()


def require_auth(
    *required_scopes: str, roles: set[str] | None = None
) -> Callable[[Request], Any]:
    async def dependency(request: Request) -> AuthContext:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Требуется Bearer-токен",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = header.removeprefix("Bearer ").strip()
        claims = await verifier.verify(token)
        scopes = frozenset(str(claims.get("scope", "")).split())
        missing = set(required_scopes) - scopes
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Недостаточно прав: {', '.join(sorted(missing))}",
            )
        role = str(claims.get("role", "user"))
        if roles and role not in roles:
            raise HTTPException(status_code=403, detail="Недостаточная роль")
        context = AuthContext(
            subject=str(claims["sub"]),
            username=str(claims.get("preferred_username", claims["sub"])),
            role=role,
            scopes=scopes,
            token=token,
            claims=claims,
        )
        request.state.auth_context = context
        return context

    return dependency
