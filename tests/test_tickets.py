import jwt
from fastapi.testclient import TestClient

from services.common.security import verifier
from services.idp.app import API_AUDIENCE, OIDC_ISSUER, issue_access_token, private_key
from services.tickets.app import app


async def decode_locally(token: str):
    return jwt.decode(
        token,
        private_key.public_key(),
        algorithms=["RS256"],
        audience=API_AUDIENCE,
        issuer=OIDC_ISSUER,
    )


def test_online_check_in_requires_confirmed_booking(monkeypatch) -> None:
    monkeypatch.setattr(verifier, "verify", decode_locally)
    token = issue_access_token(
        "user:42",
        "demo",
        "user",
        "tickets:read tickets:write",
    )
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        created = client.post(
            "/bookings",
            headers=headers,
            json={
                "flight_id": 1,
                "flight_number": "AF101",
                "origin_code": "SVO",
                "destination_code": "LED",
                "departure_at": "2026-06-20T06:00:00+00:00",
                "passengers": 1,
                "passenger_name": "Иван Петров",
                "passenger_email": "ivan@example.test",
                "passenger_phone": "+79000000000",
                "passenger_document": "4012 345678",
                "passenger_birth_date": "1990-01-01",
                "seat_numbers": ["1A"],
                "baggage_type": "checked",
                "amount_rub": 9300,
            },
        )
        assert created.status_code == 201
        booking_id = created.json()["id"]
        assert created.json()["seat_numbers"] == "1A"

        early = client.post(f"/bookings/{booking_id}/check-in", headers=headers)
        assert early.status_code == 409

        confirmed = client.patch(
            f"/bookings/{booking_id}/status",
            headers=headers,
            json={"status": "confirmed"},
        )
        assert confirmed.status_code == 200

        checked_in = client.post(f"/bookings/{booking_id}/check-in", headers=headers)
        assert checked_in.status_code == 200
        assert checked_in.json()["checked_in_at"]
