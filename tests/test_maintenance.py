from datetime import datetime, timedelta, timezone

from tplsync import maintenance
from tplsync.db import Database

from .test_processor import NOW, env, make_po  # noqa: F401 - env is a fixture

MORNING = datetime(2026, 9, 18, 13, 5, tzinfo=timezone.utc)   # 9:05 am in America/New_York


def test_summary_waits_for_the_hour_and_goes_out_once_a_day(env):  # noqa: F811
    proc, db, syncore, tpl, notifier = env([make_po()])
    settings = proc.s
    early = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)   # 5 am local
    assert not maintenance.summary_due(db, settings, early)
    assert maintenance.summary_due(db, settings, MORNING)

    maintenance.send_summary(db, settings, notifier, MORNING)
    assert not maintenance.summary_due(db, settings, MORNING + timedelta(hours=2))
    assert maintenance.summary_due(db, settings, MORNING + timedelta(days=1))


def test_summary_reports_the_day(env):  # noqa: F811
    proc, db, syncore, tpl, notifier = env([make_po(po_id=900, qty=10), make_po(po_id=901, supplier="Other Vendor")],
                                           available=3)
    proc.run(NOW)
    notifier.sent.clear()
    proc.s.admin_url = "https://3pl.example.com"
    maintenance.send_summary(db, proc.s, notifier, MORNING)

    subject, lines = notifier.sent[-1]
    text = "\n".join(lines)
    assert subject == "[3PL] Daily summary - Fri Sep 18 - all clear"
    assert "Orders created: 1" in text and "Left open for stock: 1" in text
    assert "POs skipped (other vendors, no matching SKUs, or finished jobs): 1" in text
    assert "WAITING FOR STOCK (1)" in text and "12345-3 | Client Inc | 3PL order 5000" in text
    assert "Dashboard: https://3pl.example.com/" in text
    html = notifier.html
    assert "Waiting for stock" in html and 'href="https://3pl.example.com/po/900"' in html
    assert "Open the dashboard" in html and "<script" not in html


def test_summary_says_when_nothing_needs_attention(env):  # noqa: F811
    proc, db, syncore, tpl, notifier = env([make_po()])
    proc.run(NOW)
    maintenance.send_summary(db, proc.s, notifier, MORNING)
    assert "Nothing is stuck" in "\n".join(notifier.sent[-1][1])
    assert "All clear" in notifier.html


def test_paused_and_dry_run_are_called_out(env):  # noqa: F811
    proc, db, syncore, tpl, notifier = env([make_po()])
    proc.s.paused = True
    maintenance.send_summary(db, proc.s, notifier, MORNING)
    assert "Processing is PAUSED" in "\n".join(notifier.sent[-1][1])
    assert "Processing is paused" in notifier.html


def test_on_hold_list_only_shows_active_clients(env):  # noqa: F811
    proc, db, syncore, tpl, notifier = env([make_po()])
    db.save_client(None, name="Not Testing Co", syncore_group_id="9999", source="auto")
    db.upsert(950, 77001, state="held", reference="77001-1", client_id="5001", client_name="Client Inc",
              skip_reason="Syncore job 77001 is WIP; it's sent once the job is Submitted")
    db.upsert(951, 77002, state="held", reference="77002-1", client_id="9999", client_name="Not Testing Co",
              skip_reason="Syncore job 77002 is WIP; it's sent once the job is Submitted")
    db.upsert(952, 77003, state="error", reference="77003-1", client_id="9999", last_error="paused", attempts=1)
    maintenance.send_summary(db, proc.s, notifier, MORNING)
    text = "\n".join(notifier.sent[-1][1])
    assert "77001-1 | Client Inc | Job 77001 is WIP" in text
    assert "77002-1" not in text and "77003-1" not in text
    assert "Not shown: 1 on hold for clients that aren't active." in text
    assert "77002-1" not in notifier.html and "1 more on hold for clients that aren" in notifier.html


def test_backup_copies_the_database_and_prunes_old_copies(tmp_path, env):  # noqa: F811
    proc, db, syncore, tpl, notifier = env([make_po()])
    proc.run(NOW)
    folder = tmp_path / "backups"

    old = folder / "tplsync-20260101-000000.db"
    folder.mkdir()
    old.write_text("old")
    target = maintenance.backup_database(proc.s.db_path, str(folder), keep_days=14, now=MORNING)

    assert target.exists() and not old.exists()
    assert oct(target.stat().st_mode)[-3:] == "600"
    copy = Database(str(target))
    assert copy.get(900)["order_id"] == 5000          # the copy is a working database


def test_daily_tasks_run_once_a_day(env, tmp_path):  # noqa: F811
    proc, db, syncore, tpl, notifier = env([make_po()])
    proc.run(NOW)
    notifier.sent.clear()
    folder = str(tmp_path / "b")

    maintenance.run_daily_tasks(db, proc.s, notifier, folder, MORNING)
    maintenance.run_daily_tasks(db, proc.s, notifier, folder, MORNING + timedelta(hours=1))
    assert len(list((tmp_path / "b").glob("*.db"))) == 1
    assert len(notifier.sent) == 1

    maintenance.run_daily_tasks(db, proc.s, notifier, folder, MORNING + timedelta(days=1))
    assert len(list((tmp_path / "b").glob("*.db"))) == 2
    assert len(notifier.sent) == 2
