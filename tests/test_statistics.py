import json
from datetime import UTC, datetime, timedelta

from services.common.database import Database
from services.statistics import app as statistics_module


def test_report_rows_treats_only_numeric_http_statuses_as_errors(tmp_path, monkeypatch) -> None:
    test_db = Database(tmp_path / "statistics.db")
    monkeypatch.setattr(statistics_module, "db", test_db)
    statistics_module.init_db()

    now = datetime.now(UTC)
    for index in range(3):
        event = {
            "event_id": f"domain-{index}",
            "occurred_at": now.isoformat(),
            "service": "tickets",
            "action": "booking_status_changed",
            "status": "confirmed",
        }
        test_db.execute(
            "INSERT INTO events(event_id,occurred_at,service,action,status,subject,payload) VALUES(?,?,?,?,?,?,?)",
            (
                event["event_id"],
                event["occurred_at"],
                event["service"],
                event["action"],
                event["status"],
                None,
                json.dumps(event),
            ),
        )

    rows = statistics_module.report_rows(
        (now - timedelta(minutes=1)).isoformat(),
        (now + timedelta(minutes=1)).isoformat(),
    )

    assert rows == [
        {
            "service": "tickets",
            "action": "booking_status_changed",
            "event_count": 3,
            "error_count": 0,
            "error_rate": 0.0,
        }
    ]


def test_event_http_status_ignores_domain_status_strings() -> None:
    assert statistics_module.event_http_status({"status": 500}) == 500
    assert statistics_module.event_http_status({"status": "confirmed"}) is None
    assert statistics_module.event_http_status({"status": True}) is None
