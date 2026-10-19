"""The daily summary email: what happened in the last 24 hours and what needs someone to look.

Only clients that are set up and active count as "being tested": POs on hold or with problems for
other clients are left out of the lists (and counted in a single line), so the email shows what the
team can act on.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import emails as E
from .config import Settings
from .db import Database, client_status


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


@dataclass
class Item:
    reference: str
    client: str
    detail: str
    po_id: int


@dataclass
class Summary:
    local_now: datetime
    tz_name: str
    created: int
    completed: int
    left_open: int
    skipped: int
    runs: int                      # scheduled runs in the last 24 hours
    failed_runs: int               # of those, how many failed
    problems: List[Item] = field(default_factory=list)
    open_orders: List[Item] = field(default_factory=list)
    held: List[Item] = field(default_factory=list)
    held_not_tested: int = 0
    problems_not_tested: int = 0
    not_set_up: List[str] = field(default_factory=list)
    paused: bool = False
    dry_run: bool = False
    admin_url: str = ""
    run_errors: List[Tuple[str, int]] = field(default_factory=list)   # (error, times), most common first
    changed: List[Item] = field(default_factory=list)    # sent POs edited in Syncore since, not yet reviewed

    @property
    def attention(self) -> int:
        """POs needing a look, plus one for failing runs (however many runs failed)."""
        return len(self.problems) + len(self.changed) + (1 if self.failed_runs else 0)


def _item(row, detail: str) -> Item:
    return Item(reference=row["reference"] or f"PO id {row['po_id']}", client=row["client_display"] or "Unknown client",
                detail=detail, po_id=row["po_id"])


def _hold_reason(reason: Optional[str]) -> str:
    """"Syncore job 77563 is WIP; it's sent once the job is Submitted" -> "Job 77563 is WIP"."""
    match = re.match(r"Syncore job (\d+) is ([^;]+);", reason or "")
    return f"Job {match.group(1)} is {match.group(2)}" if match else (reason or "")


def _capped(names: List[str], limit: int = 12) -> List[str]:
    return names if len(names) <= limit else names[:limit] + [f"and {len(names) - limit} more"]


def collect(db: Database, settings: Settings, now: datetime) -> Summary:
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    counts = db.event_counts_since(since)
    runs = [r for r in db.runs_since(since) if r["kind"] == "scheduled"]
    failed = [r for r in runs if r["status"] in ("failed", "interrupted")]
    errors = Counter((r["error"] or ("Interrupted before it finished" if r["status"] == "interrupted"
                                     else "No error recorded"))[:200] for r in failed)
    clients = db.list_clients()
    status = {str(r["syncore_group_id"]): client_status(r) for r in clients if r["syncore_group_id"]}

    def tested(row) -> Optional[bool]:
        """True for an active client, False for one that isn't set up or is paused, None if unknown."""
        return status.get(str(row["client_id"])) == "ready" if row["client_id"] else None

    problems = db.recent(50, state="problems")
    held = db.recent(500, state="held")
    tz = zone(settings.display_timezone)
    return Summary(
        local_now=now.astimezone(tz), tz_name=settings.display_timezone,
        created=counts.get("order.created", 0), completed=counts.get("order.completed", 0),
        left_open=counts.get("order.left_open", 0), skipped=counts.get("po.skipped", 0),
        runs=len(runs), failed_runs=len(failed), run_errors=errors.most_common(5),
        problems=[_item(r, (r["last_error"] or "")[:220]) for r in problems if tested(r) is not False],
        problems_not_tested=sum(1 for r in problems if tested(r) is False),
        open_orders=[_item(r, f"3PL order {r['order_id']}") for r in db.recent(100, state="open_short")],
        changed=[_item(r, f"3PL order {r['order_id']}: {r['change_summary'] or ''}"[:240])
                 for r in db.recent(100, state="changed")],
        held=[_item(r, _hold_reason(r["skip_reason"])) for r in held if tested(r) is not False],
        held_not_tested=sum(1 for r in held if tested(r) is False),
        not_set_up=_capped([r["name"] + (" (paused)" if client_status(r) == "paused" else "")
                            for r in clients if client_status(r) != "ready" and r["last_po_at"]]),
        paused=settings.paused, dry_run=settings.dry_run, admin_url=(settings.admin_url or "").rstrip("/"),
    )


def subject(s: Summary) -> str:
    day = s.local_now.strftime("%a %b %-d")
    status = "all clear" if not s.attention else f"{s.attention} need{'s' if s.attention == 1 else ''} attention"
    return f"[3PL] Daily summary - {day} - {status}"


def _headline(s: Summary) -> str:
    if s.paused:
        return "Processing is paused"
    if s.attention:
        return f"{s.attention} thing{'' if s.attention == 1 else 's'} need{'s' if s.attention == 1 else ''} attention"
    return "All clear: nothing needs attention"


# --- plain text --------------------------------------------------------------------------------

def render_text(s: Summary) -> List[str]:
    lines = [
        "3PL ORDER SYNC - DAILY SUMMARY",
        f"{s.local_now:%A, %B %-d} - last 24 hours to {s.local_now:%-I:%M %p} ({s.tz_name})",
        "",
        _headline(s).upper(),
    ]
    if s.paused:
        lines.append("Processing is PAUSED in Settings, so no POs are being sent.")
    if s.dry_run:
        lines.append("Dry run is ON, so nothing is being sent to 3PL Central.")
    lines += [
        "",
        f"Orders created: {s.created}    Completed: {s.completed}    Left open for stock: {s.left_open}",
    ]

    def section(title: str, items: List[Item], more: str = "") -> None:
        if not items and not more:
            return
        lines.extend(["", f"{title} ({len(items)})"])
        for i in items[:25]:
            lines.append(f"  {i.reference} | {i.client} | {i.detail}")
        if len(items) > 25:
            lines.append(f"  ...and {len(items) - 25} more on the dashboard")
        if more:
            lines.append(f"  {more}")

    section("NEEDS ATTENTION", s.problems)
    section("CHANGED IN SYNCORE AFTER IT WAS SENT (check the 3PL order)", s.changed)
    if s.failed_runs:
        lines.extend(["", f"RUNS FAILING: {s.failed_runs} of {s.runs} scheduled runs failed in the last 24 hours"])
        lines.extend(f"  {times}x  {error}" for error, times in s.run_errors)
    section("WAITING FOR STOCK", s.open_orders)
    section("ON HOLD UNTIL THE JOB IS SUBMITTED", s.held,
            f"Not shown: {s.held_not_tested} on hold for clients that aren't active." if s.held_not_tested else "")
    if not (s.problems or s.changed or s.open_orders or s.held or s.failed_runs):
        lines += ["", "Nothing is stuck: no problems, nothing waiting for stock and nothing on hold."]
    if s.not_set_up or s.problems_not_tested:
        lines += ["", "NOT ACTIVE (their POs are skipped or wait): " + (", ".join(s.not_set_up) or "-")]
    lines += ["", f"Scheduled runs: {s.runs}" + (f" ({s.failed_runs} failed)" if s.failed_runs else "")
              + f". POs skipped (other vendors, no matching SKUs, or finished jobs): {s.skipped}."]
    if s.admin_url:
        lines += ["", f"Dashboard: {s.admin_url}/"]
    return lines


# --- HTML --------------------------------------------------------------------------------------

def render_html(s: Summary) -> str:
    tone = "paused" if s.paused else ("error" if s.attention else "success")
    notes = []
    if s.paused:
        notes.append("Processing is paused in Settings, so no POs are being sent.")
    if s.dry_run:
        notes.append("Dry run is on, so nothing is being sent to 3PL Central.")
    if s.failed_runs:
        notes.append(f"Scheduled runs are failing: {s.failed_runs} of {s.runs} in the last 24 hours.")
    head = [
        E.banner(tone, _headline(s), notes),
        E.tiles([("Created", s.created, "#111827"), ("Completed", s.completed, "#15803d"),
                 ("Left open", s.left_open, "#b45309"),
                 ("Attention", len(s.problems), "#b91c1c" if s.problems else "#111827")]),
    ]

    def po_rows(items: List[Item]) -> List[List[str]]:
        rows = [[E.link(E.po_url(s.admin_url, i.po_id), i.reference, mono=True), E.e(i.client), E.e(i.detail)]
                for i in items[:25]]
        if len(items) > 25:
            rows.append(["", "", E.e(f"…and {len(items) - 25} more on the dashboard")])
        return rows

    body = []
    if s.problems:
        body.append(E.card("Needs attention", E.table(["Reference", "Client", "Problem"], po_rows(s.problems)),
                           "error", len(s.problems)))
    if s.changed:
        body.append(E.card("Changed in Syncore after it was sent",
                           E.table(["Reference", "Client", "What changed"], po_rows(s.changed)), "warning",
                           len(s.changed), "The 3PL orders weren't changed. Check them with the warehouse, then mark "
                                           "each change as reviewed on its PO page."))
    if s.failed_runs:
        logs = f"{s.admin_url}/logs?tab=runs&status=failed" if s.admin_url else None
        rows = [[E.e(f"{times}×"), E.e(error)] for error, times in s.run_errors]
        more = (f'<div style="padding:0 12px 12px;font-size:13px;">{E.link(logs, "See the failed runs in Logs")}</div>'
                if logs else "")
        body.append(E.card("Runs failing", E.table(["Times", "Error"], rows) + more, "error", s.failed_runs,
                           "Each failed run is retried 5 minutes later. The same error over and over usually means a "
                           "setting or a connection needs fixing."))
    if s.open_orders:
        body.append(E.card("Waiting for stock", E.table(["Reference", "Client", "3PL order"], po_rows(s.open_orders)),
                           "warning", len(s.open_orders)))
    if s.held or s.held_not_tested:
        note = (f"{s.held_not_tested} more on hold for clients that aren't active are not shown."
                if s.held_not_tested else "")
        content = E.table(["Reference", "Client", "Why"], po_rows(s.held)) if s.held else ""
        body.append(E.card("On hold until the job is submitted", content, "info", len(s.held) or None, note))
    if not (s.problems or s.changed or s.failed_runs or s.open_orders or s.held):
        body.append(E.card("Nothing is stuck", E.message("No problems, nothing waiting for stock and nothing on hold "
                                                          "for active clients."), "success"))
    if s.not_set_up:
        body.append(E.note_row(f"Not active, so their POs are skipped or wait: {', '.join(s.not_set_up)}."))
    body.append(E.button(f"{s.admin_url}/" if s.admin_url else None, "Open the dashboard"))
    footer = (f"{s.runs} scheduled run{'s' if s.runs != 1 else ''} in the last 24 hours"
              f"{f' ({s.failed_runs} failed)' if s.failed_runs else ''} &middot; {s.skipped} "
              f"PO{'s' if s.skipped != 1 else ''} skipped (other vendors, no matching SKUs, or finished jobs)")
    return E.page("Daily summary", f"{s.local_now:%A, %B %-d}",
                  f"Last 24 hours, to {s.local_now:%-I:%M %p} ({s.tz_name})", head, body, footer)
