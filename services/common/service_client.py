from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from services.common.config import OIDC_INTERNAL_URL


class UpstreamUnavailable(RuntimeError):
    pass


@dataclass
class TokenCache:
    token: str = ""
    expires_at: float = 0.0


class ServiceClient:
    """HTTP client that authenticates every service-to-service request."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.cache = TokenCache()
        self._lock = asyncio.Lock()

    async def service_token(self) -> str:
        async with self._lock:
            if self.cache.token and self.cache.expires_at > time.monotonic() + 15:
                return self.cache.token
            try:
                async with httpx.AsyncClient(timeout=4.0) as client:
                    response = await client.post(
                        f"{OIDC_INTERNAL_URL}/oauth2/token",
                        data={
                            "grant_type": "client_credentials",
                            "client_id": self.client_id,
                            "client_secret": self.client_secret,
                        },
                    )
                    response.raise_for_status()
            except httpx.HTTPError as exc:
                raise UpstreamUnavailable("Identity Provider недоступен") from exc
            payload = response.json()
            self.cache = TokenCache(
                token=payload["access_token"],
                expires_at=time.monotonic() + int(payload.get("expires_in", 300)),
            )
            return self.cache.token

    async def request(
        self,
        method: str,
        url: str,
        *,
        user_token: str | None = None,
        timeout: float = 5.0,
        **kwargs: Any,
    ) -> httpx.Response:
        token = user_token or await self.service_token()
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"Сервис {url} недоступен") from exc

