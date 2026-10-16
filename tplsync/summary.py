"""The daily summary email: what happened in the last 24 hours and what needs someone to look.

Only clients that are set up and active count as "being tested": POs on hold or with problems for
other clients are left out of the lists (and counted in a single line), so the email shows what the
team can act on.
"""

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings
from .db import Database, client_status

PRIMARY = "#0f766e"


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
    runs: int
    failed_runs: int
    problems: List[Item] = field(default_factory=list)
    open_orders: List[Item] = field(default_factory=list)
    held: List[Item] = field(default_factory=list)
    held_not_tested: int = 0
    problems_not_tested: int = 0
    not_set_up: List[str] = field(default_factory=list)
    paused: bool = False
    dry_run: bool = False
    admin_url: str = ""

    @property
    def attention(self) -> int:
        return len(self.problems) + self.failed_runs


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
    runs = db.runs_since(since)
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
        runs=len(runs), failed_runs=sum(1 for r in runs if r["status"] in ("failed", "interrupted")),
        problems=[_item(r, (r["last_error"] or "")[:220]) for r in problems if tested(r) is not False],
        problems_not_tested=sum(1 for r in problems if tested(r) is False),
        open_orders=[_item(r, f"3PL order {r['order_id']}") for r in db.recent(100, state="open_short")],
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
    if s.failed_runs:
        lines.append(f"  {s.failed_runs} scheduled run(s) failed in the last 24 hours - see Logs.")
    section("WAITING FOR STOCK", s.open_orders)
    section("ON HOLD UNTIL THE JOB IS SUBMITTED", s.held,
            f"Not shown: {s.held_not_tested} on hold for clients that aren't active." if s.held_not_tested else "")
    if not (s.problems or s.open_orders or s.held or s.failed_runs):
        lines += ["", "Nothing is stuck: no problems, nothing waiting for stock and nothing on hold."]
    if s.not_set_up or s.problems_not_tested:
        lines += ["", "NOT ACTIVE (their POs are skipped or wait): " + (", ".join(s.not_set_up) or "-")]
    lines += ["", f"Runs: {s.runs}" + (f" ({s.failed_runs} failed)" if s.failed_runs else "")
              + f". POs skipped (other vendors, no matching SKUs, or finished jobs): {s.skipped}."]
    if s.admin_url:
        lines += ["", f"Dashboard: {s.admin_url}/"]
    return lines


# --- HTML --------------------------------------------------------------------------------------

def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _tile(label: str, value: int, color: str) -> str:
    return (f'<td class="tile" width="25%" style="padding:6px;"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="border:1px solid #e2e5ea;border-radius:10px;background:#ffffff;"><tr>'
            f'<td style="padding:12px 14px;"><div style="font-size:11px;font-weight:600;letter-spacing:.04em;'
            f'text-transform:uppercase;color:#6b7280;">{_e(label)}</div><div style="font-size:24px;font-weight:700;'
            f'color:{color};line-height:1.3;">{value}</div></td></tr></table></td>')


def _section(s: Summary, title: str, color: str, items: List[Item], detail_label: str, note: str = "") -> str:
    if not items and not note:
        return ""
    rows = []
    for i in items[:25]:
        ref = _e(i.reference)
        if s.admin_url:
            ref = f'<a href="{_e(s.admin_url)}/po/{i.po_id}" style="color:{PRIMARY};text-decoration:none;font-weight:600;">{ref}</a>'
        else:
            ref = f'<span style="font-weight:600;">{ref}</span>'
        rows.append(
            f'<tr><td style="padding:9px 12px;border-top:1px solid #eef0f4;font-family:Menlo,Consolas,monospace;'
            f'font-size:13px;white-space:nowrap;vertical-align:top;">{ref}</td>'
            f'<td style="padding:9px 12px;border-top:1px solid #eef0f4;font-size:13px;vertical-align:top;">{_e(i.client)}</td>'
            f'<td style="padding:9px 12px;border-top:1px solid #eef0f4;font-size:13px;color:#4b5563;vertical-align:top;">'
            f'{_e(i.detail)}</td></tr>')
    if len(items) > 25:
        rows.append(f'<tr><td colspan="3" style="padding:9px 12px;border-top:1px solid #eef0f4;font-size:13px;'
                    f'color:#6b7280;">…and {len(items) - 25} more on the dashboard</td></tr>')
    table = ""
    if rows:
        table = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
                 f'<tr><th align="left" style="padding:8px 12px;font-size:11px;letter-spacing:.04em;text-transform:uppercase;'
                 f'color:#6b7280;background:#f8f9fb;">Reference</th><th align="left" style="padding:8px 12px;font-size:11px;'
                 f'letter-spacing:.04em;text-transform:uppercase;color:#6b7280;background:#f8f9fb;">Client</th>'
                 f'<th align="left" style="padding:8px 12px;font-size:11px;letter-spacing:.04em;text-transform:uppercase;'
                 f'color:#6b7280;background:#f8f9fb;">{_e(detail_label)}</th></tr>{"".join(rows)}</table>')
    footer = (f'<div style="padding:10px 12px;font-size:12px;color:#6b7280;border-top:1px solid #eef0f4;">{_e(note)}</div>'
              if note else "")
    return (f'<tr><td style="padding:6px 0;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #e2e5ea;border-radius:10px;overflow:hidden;background:#ffffff;">'
            f'<tr><td style="padding:12px 14px;border-left:4px solid {color};font-size:15px;font-weight:700;color:#111827;">'
            f'{_e(title)} <span style="font-weight:600;color:#6b7280;">({len(items)})</span></td></tr>'
            f'<tr><td style="padding:0;">{table}{footer}</td></tr></table></td></tr>')


def render_html(s: Summary) -> str:
    if s.paused:
        banner_bg, banner_fg, icon = "#fdf3e1", "#92400e", "&#9208;"
    elif s.attention:
        banner_bg, banner_fg, icon = "#fdecec", "#b91c1c", "&#9888;"
    else:
        banner_bg, banner_fg, icon = "#e8f6ed", "#15803d", "&#10003;"
    notes = []
    if s.paused:
        notes.append("Processing is paused in Settings, so no POs are being sent.")
    if s.dry_run:
        notes.append("Dry run is on, so nothing is being sent to 3PL Central.")
    if s.failed_runs:
        notes.append(f"{s.failed_runs} scheduled run{'s' if s.failed_runs != 1 else ''} failed in the last 24 hours. "
                     f"See Logs.")
    banner_notes = "".join(f'<div style="font-size:13px;margin-top:4px;">{_e(n)}</div>' for n in notes)

    sections = "".join([
        _section(s, "Needs attention", "#b91c1c", s.problems, "Problem"),
        _section(s, "Waiting for stock", "#b45309", s.open_orders, "3PL order"),
        _section(s, "On hold until the job is submitted", "#1e40af", s.held, "Why",
                 f"{s.held_not_tested} more on hold for clients that aren't active are not shown."
                 if s.held_not_tested else ""),
    ])
    if not sections:
        sections = ('<tr><td style="padding:6px 0;"><div style="border:1px solid #e2e5ea;border-radius:10px;'
                    'background:#ffffff;padding:18px;text-align:center;color:#4b5563;font-size:14px;">Nothing is stuck: '
                    'no problems, nothing waiting for stock and nothing on hold.</div></td></tr>')
    inactive = ""
    if s.not_set_up:
        inactive = (f'<tr><td style="padding:6px 4px 0;font-size:12px;color:#6b7280;">Not active, so their POs are '
                    f'skipped or wait: {_e(", ".join(s.not_set_up))}.</td></tr>')
    button = ""
    if s.admin_url:
        button = (f'<tr><td align="center" style="padding:18px 24px 4px;"><a href="{_e(s.admin_url)}/" '
                  f'style="display:inline-block;background:{PRIMARY};color:#ffffff;text-decoration:none;font-weight:600;'
                  f'font-size:14px;padding:11px 22px;border-radius:8px;">Open the dashboard</a></td></tr>')

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  @media only screen and (max-width: 520px) {{
    .tile {{ display: inline-block !important; width: 50% !important; box-sizing: border-box; }}
    .outer {{ padding: 12px 6px !important; }}
  }}
</style></head>
<body style="margin:0;padding:0;background:#f4f5f7;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;">
<tr><td class="outer" align="center" style="padding:24px 12px;">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:100%;max-width:640px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#111827;">
  <tr><td style="background:{PRIMARY};border-radius:12px 12px 0 0;padding:18px 24px;color:#ffffff;">
    <div style="font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;opacity:.85;">3PL Order Sync &middot; Daily summary</div>
    <div style="font-size:20px;font-weight:700;margin-top:2px;">{s.local_now:%A, %B %-d}</div>
    <div style="font-size:13px;opacity:.85;margin-top:2px;">Last 24 hours, to {s.local_now:%-I:%M %p} ({_e(s.tz_name)})</div>
  </td></tr>
  <tr><td style="background:#ffffff;padding:16px 24px 6px;">
    <div style="background:{banner_bg};color:{banner_fg};border-radius:10px;padding:12px 14px;">
      <div style="font-size:15px;font-weight:700;">{icon} {_e(_headline(s))}</div>{banner_notes}
    </div>
  </td></tr>
  <tr><td style="background:#ffffff;padding:6px 18px 12px;border-radius:0 0 12px 12px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      {_tile("Created", s.created, "#111827")}{_tile("Completed", s.completed, "#15803d")}{_tile("Left open", s.left_open, "#b45309")}{_tile("Attention", len(s.problems), "#b91c1c" if s.problems else "#111827")}
    </tr></table>
  </td></tr>
  <tr><td style="height:8px;"></td></tr>
  {sections}
  {inactive}
  {button}
  <tr><td style="padding:16px 24px;font-size:12px;color:#6b7280;text-align:center;">
    {s.runs} run{'s' if s.runs != 1 else ''} in the last 24 hours{f" ({s.failed_runs} failed)" if s.failed_runs else ""} &middot; {s.skipped} PO{'s' if s.skipped != 1 else ''} skipped (other vendors, no matching SKUs, or finished jobs)<br>
    Sent by 3PL Order Sync. Choose which emails go out under Settings &rarr; Emails.
  </td></tr>
</table>
</td></tr></table>
</body></html>"""
