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
    maintenance.send_summary(db, proc.s, notifier, MORNING)

    subject, lines = notifier.sent[-1]
    text = "\n".join(lines)
    assert subject == "[3PL] Daily summary - 2026-09-18"
    assert "Orders created: 1" in text
    assert "Left open for stock: 1" in text
    assert "POs skipped (other vendors or no INV/OD SKUs): 1" in text
    assert "Waiting for stock (1):" in text and "12345-3 - 3PL order 5000" in text


def test_summary_says_when_nothing_needs_attention(env):  # noqa: F811
    proc, db, syncore, tpl, notifier = env([make_po()])
    proc.run(NOW)
    maintenance.send_summary(db, proc.s, notifier, MORNING)
    assert "Nothing needs attention." in "\n".join(notifier.sent[-1][1])


def test_paused_and_dry_run_are_called_out(env):  # noqa: F811
    proc, db, syncore, tpl, notifier = env([make_po()])
    proc.s.paused = True
    maintenance.send_summary(db, proc.s, notifier, MORNING)
    assert "processing is PAUSED" in "\n".join(notifier.sent[-1][1])


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
