import os
from pathlib import Path

os.environ["KAFKA_ENABLED"] = "false"
os.environ["DATA_DIR"] = str(Path("work/test-data").absolute())
os.environ["ADMIN_PASSWORD"] = "admin123"
os.environ["GATEWAY_CLIENT_SECRET"] = "gateway-dev-secret"

