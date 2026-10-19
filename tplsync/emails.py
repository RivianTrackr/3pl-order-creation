"""HTML for every email the platform sends, in one design: a coloured header, a white card with a
status banner and key facts, then content cards and a button back to the admin.

Email clients ignore stylesheets and modern CSS, so everything is tables with inline styles. Each
email also has a plain-text part, built by the caller.
"""

import html
from typing import Iterable, List, Optional, Sequence, Tuple

PRIMARY = "#0f766e"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "Menlo,Consolas,'Courier New',monospace"
MUTED = "#6b7280"
BORDER = "#e2e5ea"
LINE = "#eef0f4"

# tone -> (background, text colour, symbol)
TONES = {
    "error": ("#fdecec", "#b91c1c", "&#9888;"),
    "warning": ("#fdf3e1", "#92400e", "&#9888;"),
    "success": ("#e8f6ed", "#15803d", "&#10003;"),
    "info": ("#eaf1fd", "#1e40af", "&#9432;"),
    "paused": ("#fdf3e1", "#92400e", "&#9208;"),
}
ACCENTS = {"error": "#b91c1c", "warning": "#b45309", "success": "#15803d", "info": "#1e40af", "muted": "#9ca3af"}


def e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def link(url: Optional[str], text: str, mono: bool = False) -> str:
    style = f"font-weight:600;{'font-family:' + MONO + ';' if mono else ''}"
    if not url:
        return f'<span style="{style}">{e(text)}</span>'
    return f'<a href="{e(url)}" style="color:{PRIMARY};text-decoration:none;{style}">{e(text)}</a>'


def banner(tone: str, headline: str, notes: Iterable[str] = ()) -> str:
    bg, fg, symbol = TONES[tone]
    extra = "".join(f'<div style="font-size:13px;margin-top:4px;">{e(n)}</div>' for n in notes if n)
    return (f'<div style="background:{bg};color:{fg};border-radius:10px;padding:12px 14px;">'
            f'<div style="font-size:15px;font-weight:700;">{symbol} {e(headline)}</div>{extra}</div>')


def facts(rows: Sequence[Tuple[str, str]]) -> str:
    """Label / value pairs. Values are HTML (escape text with e())."""
    cells = "".join(
        f'<tr><td style="padding:7px 0;font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;'
        f'color:{MUTED};width:38%;vertical-align:top;">{e(label)}</td>'
        f'<td style="padding:7px 0;font-size:14px;color:#111827;vertical-align:top;">{value}</td></tr>'
        for label, value in rows)
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{cells}</table>'


def table(columns: Sequence[str], rows: Sequence[Sequence[str]], mono_first: bool = True) -> str:
    """A simple table. Cells are HTML."""
    head = "".join(
        f'<th align="left" style="padding:8px 12px;font-size:11px;letter-spacing:.04em;text-transform:uppercase;'
        f'color:{MUTED};background:#f8f9fb;">{e(c)}</th>' for c in columns)
    body = []
    for row in rows:
        tds = []
        for i, cell in enumerate(row):
            first = i == 0 and mono_first
            tds.append(f'<td style="padding:9px 12px;border-top:1px solid {LINE};font-size:13px;vertical-align:top;'
                       f'{"white-space:nowrap;font-family:" + MONO + ";" if first else "color:#374151;"}">{cell}</td>')
        body.append(f'<tr>{"".join(tds)}</tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
            f'<tr>{head}</tr>{"".join(body)}</table>')


def card(title: str, content: str, accent: str = "muted", count: Optional[int] = None, note: str = "",
         padded: bool = False) -> str:
    """A content card below the header. content is HTML."""
    counter = f' <span style="font-weight:600;color:{MUTED};">({count})</span>' if count is not None else ""
    inner = f'<div style="padding:12px 14px;">{content}</div>' if padded else content
    footer = (f'<div style="padding:10px 12px;font-size:12px;color:{MUTED};border-top:1px solid {LINE};">{e(note)}</div>'
              if note else "")
    return (f'<tr><td style="padding:6px 0;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid {BORDER};border-radius:10px;overflow:hidden;background:#ffffff;">'
            f'<tr><td style="padding:12px 14px;border-left:4px solid {ACCENTS.get(accent, accent)};font-size:15px;'
            f'font-weight:700;color:#111827;">{e(title)}{counter}</td></tr>'
            f'<tr><td style="padding:0;">{inner}{footer}</td></tr></table></td></tr>')


def message(text: str) -> str:
    """A block of preformatted text, e.g. an error message."""
    return (f'<div style="padding:12px 14px;font-size:13px;line-height:1.5;color:#374151;white-space:pre-wrap;'
            f'word-break:break-word;">{e(text)}</div>')


def note_row(text: str) -> str:
    return f'<tr><td style="padding:6px 4px 0;font-size:12px;color:{MUTED};">{e(text)}</td></tr>'


def button(url: Optional[str], text: str) -> str:
    if not url:
        return ""
    return (f'<tr><td align="center" style="padding:18px 0 4px;"><a href="{e(url)}" style="display:inline-block;'
            f'background:{PRIMARY};color:#ffffff;text-decoration:none;font-weight:600;font-size:14px;padding:11px 22px;'
            f'border-radius:8px;">{e(text)}</a></td></tr>')


def page(kicker: str, title: str, subtitle: str, head: List[str], body: List[str], footer: str = "") -> str:
    """The whole email. head: HTML blocks inside the white header card (banner, facts, tiles);
    body: <tr> rows from card() / button() / note_row()."""
    head_html = "".join(f'<tr><td style="padding:8px 0;">{block}</td></tr>' for block in head if block)
    foot = (footer + "<br>" if footer else "") + "Sent by 3PL Order Sync. Choose which emails go out under Settings &rarr; Emails."
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
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:100%;max-width:640px;font-family:{FONT};color:#111827;">
  <tr><td style="background:{PRIMARY};border-radius:12px 12px 0 0;padding:18px 24px;color:#ffffff;">
    <div style="font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;opacity:.85;">3PL Order Sync &middot; {e(kicker)}</div>
    <div style="font-size:20px;font-weight:700;margin-top:2px;">{e(title)}</div>
    {f'<div style="font-size:13px;opacity:.85;margin-top:2px;">{e(subtitle)}</div>' if subtitle else ''}
  </td></tr>
  <tr><td style="background:#ffffff;padding:10px 24px 14px;border-radius:0 0 12px 12px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{head_html}</table>
  </td></tr>
  <tr><td style="height:8px;"></td></tr>
  {''.join(body)}
  <tr><td style="padding:16px 24px;font-size:12px;color:{MUTED};text-align:center;">{foot}</td></tr>
</table>
</td></tr></table>
</body></html>"""


def tiles(items: Sequence[Tuple[str, int, str]]) -> str:
    """Count tiles: (label, value, colour)."""
    cells = "".join(
        f'<td class="tile" width="{100 // len(items)}%" style="padding:6px;"><table role="presentation" width="100%" '
        f'cellpadding="0" cellspacing="0" style="border:1px solid {BORDER};border-radius:10px;background:#ffffff;"><tr>'
        f'<td style="padding:12px 14px;"><div style="font-size:11px;font-weight:600;letter-spacing:.04em;'
        f'text-transform:uppercase;color:{MUTED};">{e(label)}</div><div style="font-size:24px;font-weight:700;'
        f'color:{colour};line-height:1.3;">{value}</div></td></tr></table></td>'
        for label, value, colour in items)
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table>'


def po_url(admin_url: str, po_id: Optional[int]) -> Optional[str]:
    return f"{admin_url.rstrip('/')}/po/{po_id}" if admin_url and po_id else None


# --- the platform's alert emails ---------------------------------------------------------------

def _po_title(reference: Optional[str], po_id: int) -> str:
    return reference or f"PO id {po_id}"


def _subtitle(client: Optional[str], job_id: Optional[int]) -> str:
    parts = [p for p in (client, f"Syncore job {job_id}" if job_id else None) if p]
    return " · ".join(parts)


def problem_email(*, admin_url: str, po_id: int, job_id: Optional[int], reference: Optional[str],
                  client: Optional[str], error: str, order_id: Optional[int], attempts: int,
                  gave_up: bool, client_paused: bool) -> str:
    if client_paused:
        tone, headline = "paused", "Waiting for this client to be activated"
        next_step = "It goes through automatically once the client is activated on the Clients page."
    elif gave_up:
        tone, headline = "error", f"Stopped retrying after {attempts} attempts"
        next_step = "Fix the cause, then click Retry on the PO's page."
    else:
        tone, headline = "error", "This PO couldn't be sent to 3PL Central yet"
        next_step = "It's tried again every run. Fix the cause and it goes through on its own."
    head = [
        banner(tone, headline),
        facts([
            ("3PL order", e(order_id) if order_id else "Not created yet"),
            ("Client", e(client or "Unknown")),
            ("Attempts", e(attempts)),
            ("Next step", e(next_step)),
        ]),
    ]
    body = [card("What went wrong", message(error), "warning" if client_paused else "error"),
            button(po_url(admin_url, po_id), "View this PO")]
    return page("Problem with an order", _po_title(reference, po_id), _subtitle(client, job_id), head, body)


def shortage_email(*, admin_url: str, po_id: int, job_id: Optional[int], reference: str, client: str,
                   order_id: int, ship_to: str, shortages: Sequence[dict]) -> str:
    head = [
        banner("warning", "Order created but left Open: not enough stock",
               ["It's completed automatically once the warehouse has the inventory."]),
        facts([("3PL order", e(order_id)), ("Client", e(client)), ("Ship to", e(ship_to or "—"))]),
    ]
    rows = [[e(s["sku"]), e(f"{s['ordered']:g}"), e(f"{s['available']:g}")] for s in shortages]
    body = [card("Short items", table(["SKU", "Ordered", "Available"], rows), "warning", len(shortages)),
            button(po_url(admin_url, po_id), "View this PO")]
    return page("Order left open", reference, _subtitle(client, job_id), head, body)


def stock_arrived_email(*, admin_url: str, po_id: int, job_id: Optional[int], reference: str, client: str,
                        order_id: int) -> str:
    head = [
        banner("success", "Stock arrived: the order is completed",
               ["It had been left Open for lack of inventory and was completed automatically."]),
        facts([("3PL order", e(order_id)), ("Client", e(client))]),
    ]
    return page("Stock arrived", reference, _subtitle(client, job_id), head,
                [button(po_url(admin_url, po_id), "View this PO")])


def test_email(*, admin_url: str, recipients: str) -> str:
    head = [banner("success", "Email is working", ["SendGrid accepted this message, so alerts will reach you."]),
            facts([("Sent to", e(recipients))])]
    return page("Test email", "Alerts are set up", "", head,
                [button(f"{admin_url.rstrip('/')}/settings" if admin_url else None, "Open Settings")])


def runs_failing_email(*, admin_url: str, headline: str, fix: str, error: str, since: str, failed: int) -> str:
    logs = f"{admin_url.rstrip('/')}/logs?tab=runs&status=failed" if admin_url else None
    head = [
        banner("error", headline, ["No new POs are being sent until this is fixed. Nothing is lost: runs keep "
                                   "retrying every 5 minutes and catch up once it works."]),
        facts([("Failing since", e(since)), ("Failed runs", e(f"{failed} in a row")), ("What to do", e(fix))]),
    ]
    body = [card("Error", message(error[:1500]), "error"), button(logs, "See the failed runs")]
    return page("Runs are failing", headline, f"Since {since}", head, body,
                "You'll get one more email when runs are working again.")


def runs_recovered_email(*, admin_url: str, headline: str, since: str, duration: str, failed: int) -> str:
    head = [
        banner("success", "Scheduled runs are working again",
               ["Nothing was missed: each run looks back over the last 72 hours, so POs from the gap have been "
                "picked up."]),
        facts([("Problem was", e(headline)), ("Failing since", e(since)), ("Lasted", e(duration)),
               ("Failed runs", e(f"at least {failed}"))]),
    ]
    return page("Runs recovered", "Back to normal", f"After {duration}", head,
                [button(f"{admin_url.rstrip('/')}/" if admin_url else None, "Open the dashboard")])
