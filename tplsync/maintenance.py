"""Daily housekeeping: a summary email and a database backup."""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings
from .db import Database, client_missing, client_status, utcnow
from .notify import Notifier

log = logging.getLogger(__name__)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_day(settings: Settings, now: datetime) -> str:
    return now.astimezone(_zone(settings.display_timezone)).strftime("%Y-%m-%d")


def summary_due(db: Database, settings: Settings, now: datetime) -> bool:
    local = now.astimezone(_zone(settings.display_timezone))
    return local.hour >= settings.summary_hour and db.get_meta("summary_sent_on") != local.strftime("%Y-%m-%d")


def build_summary(db: Database, settings: Settings, now: datetime) -> List[str]:
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    counts = db.event_counts_since(since)
    runs = db.runs_since(since)
    failed_runs = [r for r in runs if r["status"] in ("failed", "interrupted")]
    open_orders = db.open_short_orders()
    problems = db.recent(50, state="problems")
    waiting_clients = [r for r in db.list_clients() if client_status(r) != "ready" and r["last_po_at"]]

    lines = [
        f"3PL Order Sync summary for the 24 hours to {now.astimezone(_zone(settings.display_timezone)):%b %-d, %Y %-I:%M %p} "
        f"({settings.display_timezone}).",
        "",
        f"Orders created: {counts.get('order.created', 0)}",
        f"Orders completed: {counts.get('order.completed', 0)}",
        f"Left open for stock: {counts.get('order.left_open', 0)}",
        f"POs skipped (other vendors or no INV/OD SKUs): {counts.get('po.skipped', 0)}",
        f"Runs: {len(runs)}" + (f", of which {len(failed_runs)} failed" if failed_runs else ""),
    ]
    if settings.paused:
        lines += ["", "NOTE: processing is PAUSED in the admin settings."]
    if settings.dry_run:
        lines += ["", "NOTE: dry run is ON, so nothing is being sent to 3PL Central."]

    if open_orders:
        lines += ["", f"Waiting for stock ({len(open_orders)}):"]
        lines += [f"  {r['reference'] or r['po_id']} - 3PL order {r['order_id']}" for r in open_orders[:25]]
    if problems:
        lines += ["", f"Needs attention ({len(problems)}):"]
        lines += [f"  {r['reference'] or r['po_id']}: {(r['last_error'] or '')[:160]}" for r in problems[:25]]
    if waiting_clients:
        lines += ["", "Clients sending POs that aren't set up (their POs are skipped):"]
        lines += [f"  {r['name']}: needs {', '.join(client_missing(r))}" for r in waiting_clients[:25]]
    if not (open_orders or problems or waiting_clients):
        lines += ["", "Nothing needs attention."]
    return lines


def send_summary(db: Database, settings: Settings, notifier: Notifier, now: datetime) -> bool:
    lines = build_summary(db, settings, now)
    recipients = settings.summary_email_to or settings.alert_email_to
    original, notifier.email_to = notifier.email_to, recipients
    try:
        notifier.send(f"[3PL] Daily summary - {local_day(settings, now)}", lines)
    except Exception:  # noqa: BLE001 - a failed summary must not fail the run
        log.exception("Could not send the daily summary")
        return False
    finally:
        notifier.email_to = original
    db.set_meta("summary_sent_on", local_day(settings, now))
    log.info("Daily summary emailed to %s", recipients)
    return True


def backup_database(db_path: str, backup_dir: str, keep_days: int, now: Optional[datetime] = None) -> Path:
    """Copy the database with SQLite's backup API (safe while it's in use) and drop old copies."""
    now = now or datetime.now(timezone.utc)
    folder = Path(backup_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"tplsync-{now.strftime('%Y%m%d-%H%M%S')}.db"
    source = sqlite3.connect(db_path)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
    target.chmod(0o600)
    cutoff = now - timedelta(days=keep_days)
    for old in folder.glob("tplsync-*.db"):
        try:
            stamp = datetime.strptime(old.name[8:23], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if stamp < cutoff and old != target:
            old.unlink()
    return target


def run_daily_tasks(db: Database, settings: Settings, notifier: Notifier, backup_dir: str,
                    now: Optional[datetime] = None) -> None:
    """Called at the end of a scheduled run: back up once a day, then email the summary."""
    now = now or datetime.now(timezone.utc)
    today = local_day(settings, now)
    if settings.backup_keep_days > 0 and db.get_meta("backup_made_on") != today:
        try:
            target = backup_database(settings.db_path, backup_dir, settings.backup_keep_days, now)
            db.set_meta("backup_made_on", today)
            db.set_meta("last_backup_at", utcnow())
            db.set_meta("last_backup_path", str(target))
            log.info("Backed up the database to %s", target)
        except Exception:  # noqa: BLE001
            log.exception("Could not back up the database")
    if settings.daily_summary and summary_due(db, settings, now):
        send_summary(db, settings, notifier, now)
