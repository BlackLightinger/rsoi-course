import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import jwt
from fastapi.testclient import TestClient

from services.idp.app import API_AUDIENCE, OIDC_ISSUER, app, issue_access_token, private_key


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def test_oidc_discovery_and_client_credentials() -> None:
    with TestClient(app) as client:
        discovery = client.get("/.well-known/openid-configuration")
        assert discovery.status_code == 200
        assert "S256" in discovery.json()["code_challenge_methods_supported"]

        response = client.post(
            "/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "gateway",
                "client_secret": "gateway-dev-secret",
            },
        )
        assert response.status_code == 200
        claims = jwt.decode(
            response.json()["access_token"],
            private_key.public_key(),
            algorithms=["RS256"],
            audience=API_AUDIENCE,
            issuer=OIDC_ISSUER,
        )
        assert claims["sub"] == "service:gateway"
        assert "flights:read" in claims["scope"]


def test_authorization_code_pkce_is_one_time() -> None:
    verifier = secrets.token_urlsafe(48)
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    params = {
        "client_id": "flight-web",
        "redirect_uri": "http://localhost:5173/callback",
        "response_type": "code",
        "scope": "openid profile email flights:read tickets:read",
        "state": "expected-state",
        "nonce": "expected-nonce",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    with TestClient(app) as client:
        assert client.get("/oauth2/authorize", params=params).status_code == 200
        response = client.post(
            "/oauth2/authorize",
            data={**params, "username": "admin", "password": "admin123"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        callback = urlparse(response.headers["location"])
        query = parse_qs(callback.query)
        assert query["state"] == ["expected-state"]

        token_data = {
            "grant_type": "authorization_code",
            "client_id": "flight-web",
            "code": query["code"][0],
            "redirect_uri": params["redirect_uri"],
            "code_verifier": verifier,
        }
        token = client.post("/oauth2/token", data=token_data)
        assert token.status_code == 200
        id_claims = jwt.decode(
            token.json()["id_token"],
            private_key.public_key(),
            algorithms=["RS256"],
            audience="flight-web",
            issuer=OIDC_ISSUER,
        )
        assert id_claims["nonce"] == "expected-nonce"
        assert client.post("/oauth2/token", data=token_data).status_code == 400


def test_authorize_rejects_unregistered_redirect() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/oauth2/authorize",
            params={
                "client_id": "flight-web",
                "redirect_uri": "https://evil.example/callback",
                "response_type": "code",
                "scope": "openid",
                "state": "x",
                "code_challenge": "x",
            },
        )
    assert response.status_code == 400


def test_authorize_accepts_docker_loopback_redirect() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/oauth2/authorize",
            params={
                "client_id": "flight-web",
                "redirect_uri": "http://127.0.0.1/callback",
                "response_type": "code",
                "scope": "openid",
                "state": "x",
                "code_challenge": "valid-challenge",
            },
        )
    assert response.status_code == 200


def test_registration_creates_user_for_oidc_login() -> None:
    username = f"test_{secrets.token_hex(6)}"
    password = "StrongPass123"
    verifier = secrets.token_urlsafe(48)
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    params = {
        "client_id": "flight-web",
        "redirect_uri": "http://localhost:5173/callback",
        "response_type": "code",
        "scope": "openid profile email",
        "state": "registration-state",
        "nonce": "registration-nonce",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    with TestClient(app) as client:
        assert client.post(
            "/users",
            json={
                "username": username,
                "email": f"{username}@example.test",
                "full_name": "Test User",
                "password": password,
            },
        ).status_code == 401
        service_token = issue_access_token(
            "service:gateway",
            "gateway",
            "service",
            "users:write",
        )
        created = client.post(
            "/users",
            headers={"Authorization": f"Bearer {service_token}"},
            json={
                "username": username,
                "email": f"{username}@example.test",
                "full_name": "Test User",
                "password": password,
            },
        )
        assert created.status_code == 201
        assert created.json()["username"] == username

        response = client.post(
            "/oauth2/authorize",
            data={**params, "username": username, "password": password},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert parse_qs(urlparse(response.headers["location"]).query)["state"] == ["registration-state"]
