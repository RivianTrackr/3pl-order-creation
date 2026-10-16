"""Daily housekeeping: a summary email and a database backup."""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import summary
from .config import Settings
from .db import Database, utcnow
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
    """The summary as plain-text lines (the email's text part)."""
    return summary.render_text(summary.collect(db, settings, now))


def send_summary(db: Database, settings: Settings, notifier: Notifier, now: datetime) -> bool:
    data = summary.collect(db, settings, now)
    recipients = settings.summary_email_to or settings.alert_email_to
    original, notifier.email_to = notifier.email_to, recipients
    try:
        notifier.send(summary.subject(data), summary.render_text(data), html_body=summary.render_html(data))
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
