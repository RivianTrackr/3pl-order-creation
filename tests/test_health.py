from datetime import datetime, timezone

from tplsync import health

from .test_processor import env, make_po  # noqa: F401 - env is a fixture

ERROR = "Syncore GET https://api.syncore.app/v2/orders/jobs/purchaseorders -> HTTP 401: "
NOW = datetime(2026, 10, 18, 21, 30, tzinfo=timezone.utc)


def _run(db, status, error=None):
    run = db.start_run("scheduled", None, False, None)
    db.finish_run(run, status, None if error else {"found": 0}, error)


def test_three_failed_runs_in_a_row_send_one_alert_then_one_recovery(env):  # noqa: F811
    proc, db, syncore, tpl, notifier = env([make_po()])
    proc.s.admin_url = "https://3pl.example.com"
    for _ in range(2):
        _run(db, "failed", ERROR)
        health.after_run(db, proc.s, notifier, ok=False, now=NOW)
    assert notifier.sent == []                                  # a blip or two isn't worth an email

    _run(db, "failed", ERROR)
    health.after_run(db, proc.s, notifier, ok=False, now=NOW)
    subject, lines = notifier.sent[-1]
    assert subject == "[3PL] Runs are failing: Syncore is rejecting the API key"
    assert any("Settings → Syncore → API key" in line for line in lines)
    assert "Syncore is rejecting the API key" in notifier.html and "See the failed runs" in notifier.html

    for _ in range(5):                                          # no repeats while it stays broken
        _run(db, "failed", ERROR)
        health.after_run(db, proc.s, notifier, ok=False, now=NOW)
    assert len(notifier.sent) == 1

    _run(db, "ok")
    health.after_run(db, proc.s, notifier, ok=True, now=NOW)
    subject, lines = notifier.sent[-1]
    assert subject == "[3PL] Runs are working again" and len(notifier.sent) == 2
    assert any("Failed runs: at least 8" in line for line in lines)
    assert "Scheduled runs are working again" in notifier.html

    _run(db, "ok")                                              # back to normal: nothing more
    health.after_run(db, proc.s, notifier, ok=True, now=NOW)
    assert len(notifier.sent) == 2 and db.get_meta("outage_since") is None


def test_turned_off_in_settings(env):  # noqa: F811
    proc, db, syncore, tpl, notifier = env([make_po()])
    proc.s.emails["run_failures"] = False
    for _ in range(4):
        _run(db, "failed", ERROR)
        health.after_run(db, proc.s, notifier, ok=False, now=NOW)
    _run(db, "ok")
    health.after_run(db, proc.s, notifier, ok=True, now=NOW)
    assert notifier.sent == []


def test_test_and_manual_runs_dont_count(env):  # noqa: F811
    proc, db, syncore, tpl, notifier = env([make_po()])
    _run(db, "failed", ERROR)
    _run(db, "failed", ERROR)
    db.finish_run(db.start_run("manual", "pat", False, None), "failed", None, ERROR)
    health.after_run(db, proc.s, notifier, ok=False, now=NOW)
    assert notifier.sent == []


def test_errors_are_explained_in_plain_words():
    assert health.explain(ERROR)[0] == "Syncore is rejecting the API key"
    assert health.explain("3PL Central POST https://secure-wms.com/AuthServer/api/Token -> HTTP 401: x")[0] \
        == "3PL Central is rejecting the login"
    assert "limiting requests" in health.explain("Syncore GET x -> HTTP 429: slow down")[0]
    assert "having problems (HTTP 503)" in health.explain("Syncore GET x -> HTTP 503: down")[0]
    assert health.explain("Settings incomplete in the admin UI: Syncore: API key")[0] == "Settings are incomplete"
    assert health.explain("something odd")[0] == "Scheduled runs are failing"


def test_a_real_run_that_syncore_rejects_triggers_the_alert(tmp_path, monkeypatch):
    from tplsync import __main__ as cli
    from tplsync.crypto import SecretBox, generate_key
    from tplsync.db import Database
    from tplsync.http import ApiError
    from tplsync.notify import Notifier

    key = generate_key()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TPLSYNC_ENCRYPTION_KEY", key)
    monkeypatch.setenv("ADMIN_SECRET_KEY", "x" * 40)
    box = SecretBox(key)
    db = Database(str(tmp_path / "t.db"))
    for k, v in {"SYNCORE_VENDOR_NAME": "Example Supplier LLC", "TPL_USER_LOGIN": "api",
                 "TPL_FACILITY_NAME": "Warehouse", "EMAIL_FROM": "a@b.c", "ALERT_EMAIL_TO": "ops@b.c"}.items():
        db.set_setting(k, v, "test")
    db.set_setting("SYNCORE_API_KEY", box.encrypt("old-key"), "test")
    db.set_setting("SENDGRID_API_KEY", box.encrypt("SG.x"), "test")
    db.set_meta("start_date", "2026-10-18T00:00:00+00:00")

    class RejectingSyncore:
        def __init__(self, *a, **kw):
            pass

        def search_purchase_orders(self, *a, **kw):
            raise ApiError("Syncore", "GET", "https://api.syncore.app/v2/orders/jobs/purchaseorders", 401, "")

        def list_client_groups(self):
            return []

    sent = []
    monkeypatch.setattr(cli, "SyncoreClient", RejectingSyncore)
    monkeypatch.setattr(cli.fcntl, "flock", lambda *a: None)   # each real run is its own process
    monkeypatch.setattr(Notifier, "send", lambda self, subject, lines, po_id=None, html_body=None:
                        sent.append(subject))
    pings = []
    monkeypatch.setattr(cli.health, "ping", lambda url: pings.append(url))
    db.set_setting("HEARTBEAT_URL", "https://hc-ping.com/abc", "test")
    for _ in range(3):
        assert cli.main(["run"]) == 1
    assert sent == ["[3PL] Runs are failing: Syncore is rejecting the API key"]
    assert pings == ["https://hc-ping.com/abc"] * 3          # the uptime check hears from every run


def test_ping_never_raises(monkeypatch):
    import requests

    def boom(*a, **kw):
        raise requests.ConnectionError("offline")
    monkeypatch.setattr(requests, "get", boom)
    health.ping("https://hc-ping.com/abc")      # no exception
    health.ping(None)
