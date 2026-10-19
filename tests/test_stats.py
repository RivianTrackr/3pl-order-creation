from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tplsync import stats
from tplsync.db import Database

NOW = datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc)


def test_nice_axis_top():
    assert [stats.nice_max(v) for v in (0, 1, 2, 3, 7, 12, 37, 140)] == [1, 1, 2, 4, 10, 20, 50, 200]


def test_trends_count_orders_per_local_day_and_time_to_send(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    tz = ZoneInfo("America/Chicago")
    db.upsert(1, 10, state="done", job_submitted_at="2026-10-19T14:00:00")
    db.upsert(2, 11, state="done", job_submitted_at="2026-10-18T10:00:00")
    for po_id, at in ((1, "2026-10-19T14:20:00"), (2, "2026-10-18T12:00:00"), (3, "2026-10-19T04:00:00")):
        db.conn.execute("INSERT INTO events(at, po_id, level, event, message) VALUES(?, ?, 'info', 'order.created', 'x')",
                        (at, po_id))
    db.upsert(1, 10, order_status="shipped", shipped_at="2026-10-19T09:00:00")
    db.upsert(9, 19, state="done", order_status="shipped", shipped_at="2026-08-01T09:00:00")   # outside the 30 days
    db.conn.commit()
    t = stats.trends(db, tz, NOW)
    by_day = {b["label"]: b["count"] for b in t["bars"]}
    assert len(t["bars"]) == 30 and t["total"] == 3 and t["shipped"] == 1
    assert by_day["Oct 19"] == 1 and by_day["Oct 18"] == 2      # 04:00 UTC on the 19th is the 18th in Chicago
    assert t["typical_wait"] == "1.2 h" and t["waits_measured"] == 2   # median of 20 min and 2 h
    geometry = stats.chart_geometry(t["bars"], t["y_max"])
    assert len(geometry["columns"]) == 30 and all(c["hit_w"] > 0 for c in geometry["columns"])
    assert sum(1 for c in geometry["columns"] if c["path"]) == 2
