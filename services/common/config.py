import os
from pathlib import Path


def env(name: str, default: str) -> str:
    return os.getenv(name, default)


OIDC_INTERNAL_URL = env("OIDC_INTERNAL_URL", "http://localhost:8001")
OIDC_ISSUER = env("OIDC_ISSUER", "http://localhost:8001")
API_AUDIENCE = env("API_AUDIENCE", "flight-platform-api")
KAFKA_BOOTSTRAP_SERVERS = env("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_EVENTS_TOPIC = env("KAFKA_EVENTS_TOPIC", "service-events")
KAFKA_ENABLED = env("KAFKA_ENABLED", "true").lower() in {"1", "true", "yes"}
DATA_DIR = Path(env("DATA_DIR", "./data"))


def service_db(name: str) -> Path:
    path = DATA_DIR / f"{name}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
