"""Plain-language wording for timeline events, for people who don't know how the tool works inside.

Each event gets a short title, what happened in everyday words, what to do next and who can fix it. The
raw message and data stay available under "Technical details" for support.
"""

import re
from typing import Dict, Optional

MAX_ATTEMPTS = 20   # matches processor.MAX_ATTEMPTS

# Routine steps on the way to an order: hidden unless "Everything" is picked (only when they went fine).
ROUTINE = ("po.loaded", "po.matched", "sku.matched", "stock.checked", "order.recheck", "order.preview",
           "alert.sent", "alert.muted", "po.waiting")

# Repeated by a clearer event ("Order left Open"), so only shown under "Everything".
REPEATS = ("stock.checked",)

# Who can fix a problem: label and icon.
FIXERS = {
    "syncore": ("Fix in Syncore", "fa-pen-to-square"),
    "tpl": ("Fix in 3PL Central", "fa-warehouse"),
    "warehouse": ("Waiting on the warehouse", "fa-boxes-stacked"),
    "auto": ("Fixes itself", "fa-rotate"),
    "support": ("Contact support", "fa-life-ring"),
}

# Problem categories, for the summary cards and the filter.
CATEGORIES = {
    "po_lines": ("Couldn't read the PO's items", "fa-list-check"),
    "shipping": ("Ship Via not recognized", "fa-truck"),
    "item_missing": ("Item not in 3PL Central", "fa-barcode"),
    "connection": ("3PL Central or Syncore didn't respond", "fa-plug-circle-exclamation"),
    "rejected": ("Rejected by 3PL Central or Syncore", "fa-circle-exclamation"),
    "syncore_data": ("Missing from Syncore", "fa-file-circle-question"),
    "short_stock": ("Not enough stock", "fa-box-open"),
    "changed": ("Changed after it was sent", "fa-pen"),
    "duplicate": ("Order already existed", "fa-clone"),
    "email": ("Alert email not sent", "fa-envelope"),
    "other": ("Unexpected problem", "fa-bug"),
}

_ATTEMPT = re.compile(r"^(?:Dry run error|Attempt \d+ failed \(will retry\)|Gave up after \d+ attempts"
                      r"|Waiting for client setup \(checked again every run\)):\s*")
_API = re.compile(r"^(?P<service>.+?) (?:GET|POST|PUT|PATCH|DELETE) \S+ -> HTTP (?P<status>\d+)")


def _sentence(text: str) -> str:
    text = (text or "").strip()
    return text[:1].upper() + text[1:] if text else text


def _problem(error_type: str, message: str, data: dict) -> Dict[str, Optional[str]]:
    """Title, category, fixer, plain message and next step for a failed attempt."""
    if error_type == "LineMappingError":
        return dict(category="po_lines", fixer="syncore", title="Couldn't read the items on the PO", what=message,
                    next="Open the PO in Syncore. Each “SKU:” line needs to sit right under its product "
                         "line, and that product line needs a quantity. Once the PO is saved it's tried again on "
                         "its own.")
    if error_type == "CarrierMatchError":
        in_admin = "Settings" in message or "override" in message
        return dict(category="shipping", fixer="support" if in_admin else "syncore",
                    title="The Ship Via wasn't recognized", what=message,
                    next=("This is a setup problem in this tool. Contact support." if in_admin else
                          "Check the PO's Ship Via in Syncore. It has to name a carrier and service the warehouse "
                          "uses, e.g. “UPS Ground” or “FedEx 2Day”. If it looks right, contact "
                          "support so it can be added."))
    if "is not an item for" in message or "isn't an item for" in message:
        return dict(category="item_missing", fixer="tpl", title="An item isn't set up in 3PL Central",
                    what=message.split(" No order was created.")[0],
                    next="Add the item for this client in 3PL Central, or correct the SKU on the Syncore PO. "
                         "No order was created; it's tried again on its own.")
    if error_type == "ApiError" or _API.match(message):
        match = _API.match(message)
        service = data.get("service") or (match.group("service") if match else "3PL Central or Syncore")
        status = int(data.get("http_status") or (match.group("status") if match else 0) or 0)
        if status == 0 or status >= 500 or status == 429:
            return dict(category="connection", fixer="auto", title=f"{service} didn't respond",
                        what=f"{service} was unreachable or busy" + (f" (error {status})." if status else "."),
                        next="This is usually a short outage and it's retried on its own. If it keeps happening "
                             "for more than an hour, contact support.")
        if status in (401, 403):
            return dict(category="rejected", fixer="support", title=f"{service} rejected the sign-in",
                        what=f"{service} didn't accept this tool's credentials (error {status}).",
                        next="The connection details need updating. Contact support.")
        return dict(category="rejected", fixer="support", title=f"{service} rejected the request",
                    what=f"{service} refused the order (error {status}). The technical details say why.",
                    next="Contact support and mention the PO reference.")
    if "not found on Syncore job" in message:
        return dict(category="syncore_data", fixer="syncore", title="The PO is no longer in Syncore",
                    what="The PO was deleted or moved to another job in Syncore.",
                    next="If that was on purpose, click Dismiss on the PO's page. Otherwise check the job in "
                         "Syncore.")
    if "job has no client" in message:
        return dict(category="syncore_data", fixer="syncore", title="The Syncore job has no client",
                    what=message, next="Set the client on the job in Syncore. It's tried again on its own.")
    if error_type in ("ClientNotReady",) or message.startswith("Waiting for client setup"):
        return dict(category="other", fixer="support", title="The client isn't ready", what=message,
                    next="The client needs to be finished or activated on the Clients page. Contact support.")
    return dict(category="other", fixer="support", title="Something unexpected went wrong", what=message,
                next="This is probably a bug in the tool. Contact support with the PO reference.")


def _skipped_hint(reason: str) -> Optional[str]:
    if "isn't set up for 3PL Central" in reason:
        return "The client isn't connected to 3PL Central yet. Once it is, click Process on the PO to send it."
    if reason.startswith("no SKUs match"):
        return ("None of its items are warehouse items, so there's nothing to send. If one should be, add it on "
                "the Allowed SKUs page.")
    if reason.startswith("vendor is"):
        return "It isn't for the warehouse's vendor, so it doesn't go to 3PL Central."
    if "went back to" in reason:
        return "If it should still go out, set the job back to Submitted in Syncore and click Process on the PO."
    return None


def explain(e: dict) -> Dict[str, Optional[str]]:
    """Plain-language view of one event row (a dict with event, level, message and the parsed data)."""
    event, level, message = e.get("event") or "", e.get("level") or "info", e.get("message") or ""
    data = e.get("data_obj") or {}
    out: Dict[str, Optional[str]] = dict(title=None, what=message, next=None, fixer=None, category=None,
                                         retry=None, tone=None)

    if event in ("po.error", "po.failed", "sku.unmatched"):
        clean = _ATTEMPT.sub("", message)
        out.update(_problem(data.get("error_type") or "", clean, data))
        if event == "po.failed":
            out["retry"] = f"Stopped retrying after {data.get('attempt') or MAX_ATTEMPTS} attempts"
            kept = [part for part in re.split(r"(?<=\.) ", out["next"] or "") if "on its own" not in part]
            out["next"] = " ".join(kept + ["Then click Retry on the PO's page."])
        elif message.startswith("Attempt"):
            out["retry"] = f"Retrying automatically (attempt {data.get('attempt') or 1} of {MAX_ATTEMPTS})"
        elif message.startswith("Dry run"):
            out["retry"] = "Dry run"
        if event == "sku.unmatched":
            out["retry"] = None
        return out

    simple = {
        "order.created": "Order created in 3PL Central",
        "order.completed": "Order completed in 3PL Central",
        "po.done": "Finished",
        "order.shipped": "Shipped",
        "po.loaded": "Read the PO from Syncore",
        "po.matched": "Matched to a 3PL Central client",
        "sku.matched": "Matched SKUs to 3PL Central items",
        "stock.checked": "Checked stock",
        "order.preview": "Dry run preview",
        "order.recheck": "Re-checked stock",
        "alert.sent": "Alert email sent",
        "alert.muted": "Alert not emailed (turned off in Settings)",
        "po.waiting": "Waiting for edits to finish",
        "po.on_demand": "On-demand items",
        "po.retry": "Retry requested",
        "po.process": "Sent for processing",
        "po.dismiss": "Dismissed",
        "po.test": "PO test",
        "po.change_reviewed": "Change marked as reviewed",
        "run.manual": "Run started by hand",
    }
    if event in simple:
        out["title"] = simple[event]
    if event == "po.skipped":
        reason = re.sub(r"^Skipped:\s*", "", message)
        out.update(title="Skipped", what=_sentence(reason), next=_skipped_hint(reason))
    elif event == "po.held":
        reason = re.sub(r"^On hold:\s*", "", message)
        paused = "paused" in reason
        out.update(title="On hold: client paused" if paused else "On hold until the job is submitted",
                   what=_sentence(reason),
                   next=("Nothing to do. It goes out on its own once the client is turned back on." if paused else
                         "Nothing to do. It goes out on its own once the job is Submitted in Syncore."))
    elif event == "order.left_open":
        out.update(title="Order left Open: not enough stock", category="short_stock", fixer="warehouse",
                   next="It's completed on its own once the warehouse has the stock. Nothing to do unless the "
                        "stock isn't coming.")
    elif event == "stock.checked" and level == "warning":
        out.update(title="Some items are short", category="short_stock", fixer="warehouse")
    elif event == "order.found_existing":
        out.update(title="Order already existed in 3PL Central", category="duplicate", fixer="support",
                   next="No second order was made. If that existing order isn't for this PO, contact support.")
    elif event == "order.cancelled":
        out.update(title="Order cancelled in 3PL Central")
    elif event == "po.changed_after_send":
        out.update(title="PO changed in Syncore after it was sent", category="changed", fixer="tpl",
                   next="The 3PL Central order was not updated. Compare them and update the order in 3PL Central "
                        "by hand if needed, then mark the change as reviewed on the PO's page.")
    elif event == "alert.failed":
        out.update(title="Alert email couldn't be sent", category="email", fixer="support",
                   next="The problem is still shown here. If emails keep failing, contact support.")
    elif event == "order.recheck_failed":
        out.update(title="Couldn't re-check stock", category="connection", fixer="auto",
                   next="It's tried again on the next run.")
    elif event.startswith("client."):
        out["title"] = "Client settings changed"
    elif event.startswith("po.note"):
        out["title"] = "Note " + ("deleted" if event.endswith("delete") else "added")
    if out["title"] is None:
        out["title"] = {"error": "Problem", "warning": "Needs a look"}.get(level, "Update")
    if level in ("error", "warning") and out["category"] is None:
        out["category"] = "other"
    return out


def resolved(e: dict, po) -> Optional[bool]:
    """Whether a problem is behind us, judged by the PO's status now. None when that doesn't apply."""
    if po is None or e.get("level") not in ("error", "warning"):
        return None
    event = e.get("event") or ""
    if event in ("order.left_open", "stock.checked"):
        return po["completion"] != "open_short"
    if event == "po.changed_after_send":
        return not po["changed_at"]
    if e.get("level") == "error" and event not in ("alert.failed", "order.recheck_failed"):
        return po["state"] not in ("error", "failed", "waiting", "processing")
    return None


def fixer_label(key: Optional[str]):
    return FIXERS.get(key or "")


def category_label(key: Optional[str]):
    return CATEGORIES.get(key or "", CATEGORIES["other"])
