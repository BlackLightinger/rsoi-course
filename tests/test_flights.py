import jwt
from fastapi.testclient import TestClient

from services.common.security import verifier
from services.flights.app import app
from services.idp.app import API_AUDIENCE, OIDC_ISSUER, issue_access_token, private_key


async def decode_locally(token: str):
    return jwt.decode(
        token,
        private_key.public_key(),
        algorithms=["RS256"],
        audience=API_AUDIENCE,
        issuer=OIDC_ISSUER,
    )


def test_flights_require_token(monkeypatch) -> None:
    monkeypatch.setattr(verifier, "verify", decode_locally)
    with TestClient(app) as client:
        assert client.get("/flights").status_code == 401


def test_search_reserve_and_release(monkeypatch) -> None:
    monkeypatch.setattr(verifier, "verify", decode_locally)
    token = issue_access_token(
        "service:gateway",
        "gateway",
        "service",
        "flights:read flights:write",
    )
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        search = client.get("/flights", params={"passengers": 1}, headers=headers)
        assert search.status_code == 200
        flight = search.json()["items"][0]
        before = flight["seats_available"]

        reserved = client.post(
            f"/flights/{flight['id']}/reserve", json={"seats": 1}, headers=headers
        )
        assert reserved.status_code == 200
        assert reserved.json()["seats_available"] == before - 1

        released = client.post(
            f"/flights/{flight['id']}/release", json={"seats": 1}, headers=headers
        )
        assert released.status_code == 200


def test_search_uses_city_names(monkeypatch) -> None:
    monkeypatch.setattr(verifier, "verify", decode_locally)
    token = issue_access_token(
        "service:gateway",
        "gateway",
        "service",
        "flights:read",
    )
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        response = client.get(
            "/flights",
            params={"origin": "москва", "destination": "петербург"},
            headers=headers,
        )
    assert response.status_code == 200
    data = response.json()
    assert data["count"] >= 1
    assert {item["origin_city"] for item in data["items"]} == {"Москва"}
    assert {item["destination_city"] for item in data["items"]} == {"Санкт-Петербург"}


def test_city_suggestions_and_named_seat_reservation(monkeypatch) -> None:
    monkeypatch.setattr(verifier, "verify", decode_locally)
    token = issue_access_token(
        "service:gateway",
        "gateway",
        "service",
        "flights:read flights:write",
    )
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        cities = client.get("/cities", params={"query": "моск"}, headers=headers)
        assert cities.status_code == 200
        assert "Москва" in cities.json()["items"]

        flight = client.get("/flights", headers=headers).json()["items"][0]
        seats = client.get(f"/flights/{flight['id']}/seats", headers=headers)
        assert seats.status_code == 200
        seat_number = next(
            item["seat_number"] for item in seats.json()["items"] if item["status"] == "available"
        )

        reserved = client.post(
            f"/flights/{flight['id']}/reserve",
            json={"seats": 1, "seat_numbers": [seat_number]},
            headers=headers,
        )
        assert reserved.status_code == 200
        assert reserved.json()["seat_numbers"] == [seat_number]

        after_reserve = client.get(f"/flights/{flight['id']}/seats", headers=headers)
        reserved_seat = next(
            item for item in after_reserve.json()["items"] if item["seat_number"] == seat_number
        )
        assert reserved_seat["status"] == "reserved"

        released = client.post(
            f"/flights/{flight['id']}/release",
            json={"seats": 1, "seat_numbers": [seat_number]},
            headers=headers,
        )
        assert released.status_code == 200
