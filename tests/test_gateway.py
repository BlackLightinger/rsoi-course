import json

import httpx
from fastapi.testclient import TestClient

from services.common.security import verifier
from services.common.service_client import UpstreamUnavailable
from services.gateway import app as gateway_module


def test_partner_api_is_limited_and_requires_api_key(monkeypatch) -> None:
    async def fake_request(*args, **kwargs):
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "items": [
                        {
                            "id": 1,
                            "flight_number": "AF101",
                            "origin_code": "SVO",
                            "origin_city": "Москва",
                            "destination_code": "LED",
                            "destination_city": "Санкт-Петербург",
                            "departure_at": "2026-06-20T06:00:00+00:00",
                            "arrival_at": "2026-06-20T07:30:00+00:00",
                            "price_rub": 6800,
                            "seats_available": 20,
                            "aircraft": "Airbus A320",
                        }
                    ],
                    "count": 1,
                }
            ).encode(),
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "http://flights/flights"),
        )

    monkeypatch.setattr(gateway_module.client, "request", fake_request)
    with TestClient(gateway_module.app) as client:
        assert client.get("/partner/v1/flights").status_code == 422
        response = client.get(
            "/partner/v1/flights", headers={"X-API-Key": "partner-demo-key"}
        )
    assert response.status_code == 200
    assert "seats_available" not in response.json()["items"][0]
    assert "aircraft" not in response.json()["items"][0]
    assert response.json()["items"][0]["origin_city"] == "Москва"


def test_dashboard_degrades_when_loyalty_is_unavailable(monkeypatch) -> None:
    async def fake_verify(token: str):
        return {
            "sub": "user:42",
            "preferred_username": "smoke",
            "role": "user",
            "scope": "tickets:read loyalty:read payments:read",
        }

    async def fake_request(method, url, **kwargs):
        if "/loyalty/" in url:
            raise UpstreamUnavailable("loyalty down")
        body = {"items": [], "count": 0}
        return httpx.Response(
            200,
            json=body,
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(verifier, "verify", fake_verify)
    monkeypatch.setattr(gateway_module.client, "request", fake_request)
    with TestClient(gateway_module.app) as client:
        response = client.get(
            "/api/dashboard", headers={"Authorization": "Bearer test-token"}
        )
    assert response.status_code == 200
    assert response.json()["bookings"] == {"items": [], "count": 0}
    assert response.json()["loyalty"] is None
    assert any("loyalty" in item for item in response.json()["warnings"])


def test_city_suggestions_degrade_when_flights_is_unavailable(monkeypatch) -> None:
    async def fake_request(method, url, **kwargs):
        raise UpstreamUnavailable("flights down")

    published: list[tuple[str, dict]] = []

    async def fake_publish(action: str, **details):
        published.append((action, details))

    monkeypatch.setattr(gateway_module.client, "request", fake_request)
    monkeypatch.setattr(gateway_module.events, "publish", fake_publish)

    with TestClient(gateway_module.app) as client:
        response = client.get("/api/cities", params={"query": "моск"})

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["degraded"] is True
    assert any(action == "city_suggestions_degraded" for action, _ in published)
