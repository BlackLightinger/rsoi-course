from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from services.common.events import install_observability
from services.common.security import AuthContext, require_auth, verifier


def test_unhandled_errors_are_json_and_audited() -> None:
    app = FastAPI()
    publisher = install_observability(app, "sample")
    published: list[tuple[str, dict]] = []

    async def fake_publish(action: str, **details):
        published.append((action, details))

    async def noop() -> None:
        return None

    publisher.publish = fake_publish  # type: ignore[method-assign]
    publisher.start = noop  # type: ignore[method-assign]
    publisher.stop = noop  # type: ignore[method-assign]

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"X-Request-ID": "req-1"})

    assert response.status_code == 500
    assert response.json()["detail"] == "Внутренняя ошибка сервиса"
    assert response.json()["request_id"] == "req-1"
    assert any(action == "unhandled_error" for action, _ in published)
    assert any(
        action == "http_request" and details["status"] == 500
        for action, details in published
    )


def test_audit_event_includes_authenticated_subject(monkeypatch) -> None:
    app = FastAPI()
    publisher = install_observability(app, "sample")
    published: list[tuple[str, dict]] = []

    async def fake_publish(action: str, **details):
        published.append((action, details))

    async def noop() -> None:
        return None

    async def fake_verify(_: str):
        return {
            "sub": "user:7",
            "preferred_username": "demo",
            "role": "user",
            "scope": "tickets:read",
        }

    publisher.publish = fake_publish  # type: ignore[method-assign]
    publisher.start = noop  # type: ignore[method-assign]
    publisher.stop = noop  # type: ignore[method-assign]
    monkeypatch.setattr(verifier, "verify", fake_verify)

    @app.get("/protected")
    async def protected(auth: AuthContext = Depends(require_auth("tickets:read"))):
        return {"sub": auth.subject}

    with TestClient(app) as client:
        response = client.get("/protected", headers={"Authorization": "Bearer test"})

    assert response.status_code == 200
    assert response.json() == {"sub": "user:7"}
    assert any(
        action == "http_request"
        and details["authenticated"] is True
        and details["subject"] == "user:7"
        and details["role"] == "user"
        for action, details in published
    )
