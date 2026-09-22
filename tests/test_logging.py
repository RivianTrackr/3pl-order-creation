import json
import logging
import re

from fastapi.testclient import TestClient

from tplsync import __main__ as cli
from tplsync.admin.app import create_app
from tplsync.admin.auth import hash_password
from tplsync.config import Bootstrap
from tplsync.crypto import generate_key
from tplsync.db import Database
from tplsync.notify import Notifier
from tplsync.runlog import DatabaseLogHandler, redact

from .test_processor import NOW, env, make_po  # noqa: F401 - env is a fixture


def events(db, po_id=900):
    return [e["event"] for e in db.list_events(po_id=po_id)]


def test_happy_path_records_a_timeline(env):  # noqa: F811
    proc, db, *_ = env([make_po()])
    proc.run_id = db.start_run("scheduled", None, False, None)
    proc.run(NOW)
    assert events(db) == ["po.loaded", "po.matched", "order.created", "stock.checked", "order.completed", "po.done"]
    created = next(e for e in db.list_events(po_id=900) if e["event"] == "order.created")
    assert created["run_id"] == proc.run_id and created["reference"] == "12345-3"
    assert json.loads(created["data"])["payload"]["referenceNum"] == "12345-3"
    assert proc.stats["created"] == 1 and proc.stats["completed"] == 1


def test_shortage_records_warning_and_email(env):  # noqa: F811
    proc, db, syncore, tpl, _ = env([make_po(qty=10)], available=3)
    proc.notifier = Notifier("key", "from@example.com", "alerts@example.com", dry_run=True,
                             recorder=proc._record_email)
    proc.run(NOW)
    assert "order.left_open" in events(db) and "alert.sent" in events(db)
    [email] = db.list_emails(po_id=900)
    assert email["status"] == "dry_run" and "SHIRT-INV: ordered 10, available 3" in email["body"]


def test_skips_and_errors_are_logged(env):  # noqa: F811
    proc, db, *_ = env([make_po(po_id=1, supplier="Another Vendor Co"), make_po(po_id=2)], client_id=4444)
    proc.run(NOW)
    [skip] = [e for e in db.list_events(po_id=1) if e["event"] == "po.skipped"]
    assert "Another Vendor Co" in skip["message"]
    [skipped] = [e for e in db.list_events(po_id=2) if e["event"] == "po.skipped"]
    assert "Northwind isn't set up for 3PL Central" in skipped["message"]


def test_dry_run_logs_preview_without_changing_state(env):  # noqa: F811
    proc, db, syncore, tpl, notifier = env([make_po()], dry_run=True)
    proc.run(NOW)
    assert db.get(900) is None and tpl.created == []
    preview = next(e for e in db.list_events(po_id=900) if e["event"] == "order.preview")
    assert json.loads(preview["data"])["payload"]["orderItems"] == [{"itemIdentifier": {"sku": "SHIRT-INV"}, "qty": 10}]


def test_redaction():
    text = ('Authorization: Bearer abc.def-123 and Basic dXNlcjpwYXNz, {"client_secret": "shh", '
            '"x-api-key": "k1"} SG.aaaaaaaaaaaa.bbbbbbbbbbbbbbbb')
    out = redact(text)
    for secret in ("abc.def-123", "dXNlcjpwYXNz", "shh", "k1", "aaaaaaaaaaaa"):
        assert secret not in out


def test_log_handler_writes_lines(tmp_path):
    db = Database(str(tmp_path / "l.db"))
    run_id = db.start_run("manual", "me", False, None)
    logger = logging.getLogger("tplsync.test")
    handler = DatabaseLogHandler(str(tmp_path / "l.db"), run_id)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info("hello %s", "world")
    logger.warning("token Bearer secret123")
    logger.removeHandler(handler)
    handler.close()
    lines = db.log_lines(run_id)
    assert [(l["level"], l["message"]) for l in lines] == [("INFO", "hello world"), ("WARNING", "token Bearer ***")]
    assert [l["level"] for l in db.log_lines(run_id, "WARNING")] == ["WARNING"]


def test_stale_running_runs_are_marked_interrupted_and_pruned(tmp_path):
    db = Database(str(tmp_path / "p.db"))
    stale = db.start_run("scheduled", None, False, None)
    db.start_run("scheduled", None, False, None)
    assert db.get_run(stale)["status"] == "interrupted"
    db.conn.execute("UPDATE runs SET started_at = '2020-01-01T00:00:00' WHERE id = ?", (stale,))
    db.add_log_line(stale, "INFO", "x", "old")
    db.prune_logs(90)
    assert db.get_run(stale) is None and db.log_lines(stale) == []


def test_cli_run_records_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("TPLSYNC_ENCRYPTION_KEY", generate_key())
    monkeypatch.setenv("ADMIN_SECRET_KEY", "x" * 40)
    assert cli.main(["run", "--trigger", "manual", "--user", "pat"]) == 1
    db = Database(str(tmp_path / "cli.db"))
    [run] = db.list_runs()
    assert (run["status"], run["kind"], run["triggered_by"]) == ("failed", "manual", "pat")
    assert "Settings incomplete" in run["error"]
    assert any("Settings incomplete" in l["message"] for l in db.log_lines(run["id"]))


def test_admin_log_pages(tmp_path):
    boot = Bootstrap(db_path=str(tmp_path / "a.db"), encryption_key=generate_key(), admin_secret_key="s" * 48,
                     admin_host="127.0.0.1", admin_port=0, cookie_secure=False)
    db = Database(boot.db_path)
    db.create_user("admin", hash_password("correct horse battery"))
    run_id = db.start_run("test", "admin", True, "job 12345 / PO 900")
    db.add_event(run_id, 900, 12345, "12345-3", "info", "order.preview", "Dry run: would be created",
                 {"payload": {"referenceNum": "12345-3"}})
    db.add_email(run_id, 900, "[3PL] Order 12345-3 left Open", "ops@example.com", "Short items: X", "dry_run")
    db.add_log_line(run_id, "INFO", "tplsync.http", "3PL Central GET /orders -> HTTP 200 (80 ms)")
    db.finish_run(run_id, "ok", {"previewed": 1})

    client = TestClient(create_app(boot))
    client.post("/login", data={"username": "admin", "password": "correct horse battery"})
    runs = client.get("/logs").text
    assert f"/logs/runs/{run_id}" in runs and "1 previewed" in runs
    detail = client.get(f"/logs/runs/{run_id}").text
    assert "Dry run: would be created" in detail and "HTTP 200 (80 ms)" in detail and "left Open" in detail
    assert "12345-3" in client.get("/logs?tab=events&q=12345-3").text
    assert "Short items: X" in client.get("/logs?tab=emails").text
    po = client.get("/po/900").text
    assert "Timeline" in po and "order.preview" in po
    assert client.get("/po/424242").status_code == 404
    assert re.search(r'href="/logs"', client.get("/").text)


def test_console_output_is_redacted_too(caplog):
    logger = logging.getLogger("tplsync.test.console")
    logger.addFilter(redact_filter := __import__("tplsync.runlog", fromlist=["RedactingFilter"]).RedactingFilter())
    with caplog.at_level(logging.INFO):
        logger.info('signed in: {"access_token":"eyJabc.def","token_type":"Bearer"}')
    assert "eyJabc.def" not in caplog.text and "***" in caplog.text
    logger.removeFilter(redact_filter)
