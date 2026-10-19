"""Alert straight away when scheduled runs keep failing, and again when they recover.

A single failed run is usually a blip and the next run, 5 minutes later, retries everything. Three in
a row means something needs fixing (a rejected API key, a service that's down), so one email goes out
then, and one more when a run succeeds again. Nothing is repeated in between.
"""

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import emails
from .db import Database, utcnow

log = logging.getLogger(__name__)

STREAK = 3          # consecutive failed scheduled runs before alerting
FAILED = ("failed", "interrupted")


def ping(url: Optional[str]) -> None:
    """Tell the outside uptime check that a scheduled run happened. Never raises."""
    if not url:
        return
    try:
        import requests
        requests.get(url, timeout=10)
    except Exception:  # noqa: BLE001 - a missed ping only means the check may email, which is the point
        log.warning("Could not reach the uptime check URL")


def explain(error: str) -> Tuple[str, str]:
    """(headline, what to do) for a run error, in plain words."""
    text = error or ""
    service = "Syncore" if text.startswith("Syncore") else ("3PL Central" if text.startswith("3PL Central")
                                                            else ("SendGrid" if text.startswith("SendGrid") else None))
    status = re.search(r"HTTP (\d{3})", text)
    code = int(status.group(1)) if status else None
    if service == "Syncore" and code in (401, 403):
        return ("Syncore is rejecting the API key",
                "Check Settings → Syncore → API key. If the key was regenerated in Syncore, paste the new one and save.")
    if service == "3PL Central" and code in (401, 403):
        return ("3PL Central is rejecting the login",
                "Check the client's Client ID and Secret on the Clients page and the user login in Settings.")
    if code == 429:
        return (f"{service or 'An API'} is limiting requests (too many calls)",
                "This usually clears up by itself. If it keeps happening, tell Claude so runs can be spaced out.")
    if code and code >= 500:
        return (f"{service or 'An API'} is having problems (HTTP {code})",
                "Usually temporary on their side. Runs keep retrying every 5 minutes.")
    if "timed out" in text.lower() or "timeout" in text.lower() or "connection" in text.lower():
        return (f"{service or 'An API'} isn't responding",
                "Usually temporary. If it lasts, check the server's internet connection.")
    if "Settings incomplete" in text:
        return "Settings are incomplete", "Open Settings and fill in the fields marked as needed."
    if "go-live" in text.lower():
        return "The go-live date isn't set", "Click “Go live now” on the dashboard."
    if "database is locked" in text:
        return "The database was busy", "Usually temporary. If it keeps happening, restart the service."
    return "Scheduled runs are failing", "Open the failed runs in Logs to see the full error."


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _local(value: str, tz_name: str) -> str:
    dt = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    return dt.astimezone(_zone(tz_name)).strftime("%b %-d, %-I:%M %p")


def _streak(db: Database) -> List:
    """The current run of consecutive failed scheduled runs, newest first."""
    streak = []
    for run in db.list_runs(500, kind="scheduled"):
        if run["status"] == "running":
            continue
        if run["status"] not in FAILED:
            break
        streak.append(run)
    return streak


def _duration(since: str, until: datetime) -> str:
    minutes = max(int((until - datetime.fromisoformat(since).replace(tzinfo=timezone.utc)).total_seconds() // 60), 1)
    hours, mins = divmod(minutes, 60)
    return f"{hours} h {mins} min" if hours else f"{mins} min"


def after_run(db: Database, settings, notifier, ok: bool, now: Optional[datetime] = None) -> None:
    """Call after each scheduled run has been recorded. Never raises."""
    try:
        _after_run(db, settings, notifier, ok, now or datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 - an alerting problem must not fail the run
        log.exception("Could not check whether scheduled runs are failing")


def _send(settings, notifier, subject: str, lines: List[str], html_body: str) -> bool:
    if settings is None or notifier is None or not settings.emails.get("run_failures", True):
        return False
    try:
        notifier.send(subject, lines, html_body=html_body)
        return True
    except Exception:  # noqa: BLE001
        log.exception("Could not send the run-health email")
        return False


def _after_run(db: Database, settings, notifier, ok: bool, now: datetime) -> None:
    tz = settings.display_timezone if settings else "UTC"
    admin = (settings.admin_url if settings else "") or ""
    if ok:
        since, alerted = db.get_meta("outage_since"), db.get_meta("outage_alerted_at")
        if alerted and since:
            failed = int(db.get_meta("outage_failed_runs") or 0)
            headline = db.get_meta("outage_headline") or "Scheduled runs were failing"
            took = _duration(since, now)
            lines = [f"Scheduled runs are working again after {took}.", "",
                     f"Problem: {headline}", f"Failing since: {_local(since, tz)}",
                     f"Failed runs: at least {failed}", "",
                     "Nothing was missed: each run looks back over the last 72 hours, so POs from the gap "
                     "have been picked up."]
            _send(settings, notifier, "[3PL] Runs are working again", lines,
                  emails.runs_recovered_email(admin_url=admin, headline=headline, since=_local(since, tz),
                                              duration=took, failed=failed))
            log.info("Runs recovered after %s; recovery email sent", took)
        for key in ("outage_since", "outage_alerted_at", "outage_failed_runs", "outage_headline"):
            db.set_meta(key, None)
        return

    streak = _streak(db)
    if len(streak) < STREAK:
        return
    since = streak[-1]["started_at"]
    db.set_meta("outage_since", since)
    db.set_meta("outage_failed_runs", str(len(streak)))
    if db.get_meta("outage_alerted_at"):
        return                                    # already told them; wait for the recovery
    error = streak[0]["error"] or "No error recorded"
    headline, fix = explain(error)
    db.set_meta("outage_headline", headline)
    lines = [f"{headline}.", "", f"The last {len(streak)} scheduled runs failed, starting {_local(since, tz)}. "
             "No new POs are being sent until this is fixed; nothing is lost, and runs keep retrying every 5 minutes.",
             "", f"What to do: {fix}", "", f"Error: {error[:600]}"]
    if admin:
        lines += ["", f"Failed runs: {admin.rstrip('/')}/logs?tab=runs&status=failed"]
    if _send(settings, notifier, f"[3PL] Runs are failing: {headline}", lines,
             emails.runs_failing_email(admin_url=admin, headline=headline, fix=fix, error=error,
                                       since=_local(since, tz), failed=len(streak))):
        db.set_meta("outage_alerted_at", utcnow())
        log.warning("%d scheduled runs failed in a row; alert sent", len(streak))
