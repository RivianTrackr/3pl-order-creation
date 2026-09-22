"""Pure functions that translate Syncore data into 3PL Central requests."""

import re
from collections import OrderedDict
from typing import Dict, List, Optional


def normalize_name(name: Optional[str]) -> str:
    return " ".join((name or "").split()).casefold()


def is_vendor(po: dict, vendor_name: str) -> bool:
    return normalize_name((po.get("supplier") or {}).get("name")) == normalize_name(vendor_name)


def reference_number(po: dict) -> str:
    """Job number plus the PO's sequence number on the job, e.g. 12345-3."""
    return f"{po['job_number']}-{po['number']}"


def _qty(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class LineMappingError(ValueError):
    """The PO's lines can't be turned into 3PL order lines without a human looking."""


SKU_TAG = re.compile(r"^\s*SKU\s*[:#]\s*(\S+)", re.IGNORECASE)


def tagged_sku(line: dict) -> Optional[str]:
    """The inventory SKU from a "SKU: ABC123" description, if the line has one."""
    match = SKU_TAG.match(str(line.get("description") or ""))
    return match.group(1).strip() if match else None


def order_lines(po: dict, sku_pattern: str) -> "OrderedDict[str, float]":
    """SKU -> quantity for the PO lines that belong in 3PL Central.

    Two layouts are supported:

    * The inventory SKU is in a comment line's description ("SKU: EBC006INVJ-2XL").
      The SKU line has no quantity, so it takes its group's: the nearest top-level
      line above it, whose quantity is its own or sits on its Color/Size lines
      (on-demand POs put an "ON DEMAND FROM ..." header there).
    * The inventory SKU is in the line's own SKU field. A matching parent whose
      matching children carry their own quantities (e.g. sizes) is skipped so it
      isn't counted twice.

    Repeated SKUs are summed.
    """
    rx = re.compile(sku_pattern)
    items = po.get("line_items") or []
    lines: "OrderedDict[str, float]" = OrderedDict()

    def add(sku: str, qty: float) -> None:
        lines[sku] = lines.get(sku, 0.0) + qty

    # Layout 1: "SKU: ..." description lines.
    children: Dict[object, List[dict]] = {}
    for li in items:
        if li.get("parent_id"):
            children.setdefault(li["parent_id"], []).append(li)

    def leaves(line_id) -> List[dict]:
        """The Color/Size descendants that actually carry a quantity."""
        found = []
        for child in children.get(line_id, []):
            below = leaves(child.get("line_id"))
            found += below if below else ([child] if _qty(child.get("quantity")) > 0 else [])
        return found

    def leaf_quantity(line_id) -> float:
        return sum(_qty(leaf.get("quantity")) for leaf in leaves(line_id))

    claimed = set()   # group/product line ids whose quantity was used by a SKU line
    for index, li in enumerate(items):
        sku = tagged_sku(li)
        if not sku or not rx.search(sku):
            continue
        qty = _qty(li.get("quantity"))
        if qty <= 0:
            # The SKU belongs to the nearest group above it: a top-level line whose quantity is either
            # its own or spread over its Color/Size lines ("ON DEMAND FROM ..." headers work this way).
            group = next((p for p in reversed(items[:index])
                          if p.get("parent_id") == 0 and not tagged_sku(p)
                          and (leaf_quantity(p.get("line_id")) > 0 or _qty(p.get("quantity")) > 0)), None)
            if group is None:
                group = next((p for p in reversed(items[:index])
                              if _qty(p.get("quantity")) > 0 and not tagged_sku(p)), None)
            if group is None:
                raise LineMappingError(f"Found SKU {sku} but no product line with a quantity above it.")
            if group.get("line_id") in claimed:
                raise LineMappingError(
                    f"Several SKU lines belong to {group.get('description')!r}, so the quantity for {sku} is "
                    f"ambiguous. Give each SKU line its own quantity.")
            claimed.add(group.get("line_id"))
            variants = leaves(group.get("line_id"))
            if len(variants) > 1:
                raise LineMappingError(
                    f"SKU {sku} covers {len(variants)} colors/sizes "
                    f"({', '.join(str(v.get('description')) for v in variants)}), and each is a different "
                    f"warehouse item. Give each variant its own SKU line.")
            qty = leaf_quantity(group.get("line_id")) or _qty(group.get("quantity"))
        add(sku, qty)

    # Layout 2: SKU field on the line itself.
    matching = [li for li in items
                if li.get("sku") and rx.search(str(li["sku"])) and _qty(li.get("quantity")) > 0
                and li.get("line_id") not in claimed]
    parents_with_children = {li.get("parent_id") for li in matching if li.get("parent_id")}
    for li in matching:
        if li.get("line_id") not in parents_with_children:
            add(str(li["sku"]).strip(), _qty(li.get("quantity")))
    return lines


def po_skus(po: dict) -> List[str]:
    """Every SKU on the PO, from SKU fields and "SKU:" descriptions (for logging)."""
    found = []
    for li in po.get("line_items") or []:
        for value in (li.get("sku"), tagged_sku(li)):
            if value and str(value).strip() not in found:
                found.append(str(value).strip())
    return found


def _clip(value: Optional[str], limit: int) -> str:
    return (value or "").strip()[:limit]


COUNTRY_CODES = {
    "united states": "US", "united states of america": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US", "us": "US",
    "canada": "CA", "mexico": "MX", "puerto rico": "PR", "united kingdom": "GB", "great britain": "GB",
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\w])(\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})(?![\w])")


def country_code(country: Optional[str]) -> str:
    value = (country or "").strip()
    if not value:
        return "US"
    return COUNTRY_CODES.get(value.casefold(), value.upper() if len(value) == 2 else value)


def contact_from_text(text: Optional[str]) -> Dict[str, str]:
    """Email address and phone number mentioned in free text such as Critical Comments."""
    text = text or ""
    email = EMAIL.search(text)
    phone = PHONE.search(EMAIL.sub(" ", text))
    return {"emailAddress": email.group(0) if email else "", "phoneNumber": phone.group(1).strip() if phone else ""}


def build_order(po: dict, job: dict, lines: Dict[str, float], customer_id: int,
                facility_id: Optional[int], facility_name: str, routing: dict) -> dict:
    ship_to = po.get("ship_to") or {}
    ref = reference_number(po)
    client = job.get("client") or {}
    billing_code = routing.pop("billingCode", "Prepaid")

    notes = [f"Syncore Job {po['job_number']} / PO {ref}"]
    if client.get("business_name"):
        notes.append(f"Client: {client['business_name']}")
    if (job.get("store") or {}).get("name"):
        notes.append(f"Store: {job['store']['name']}")
    for field in ("shipping_and_instructions", "critical_comments"):
        if po.get(field):
            notes.append(" ".join(po[field].split()))

    shipping_notes = [s for s in (
        po.get("ship_via") and f"Ship Via: {po['ship_via']}",
        po.get("in_hand_date") and f"In hand: {po['in_hand_date']}",
        po.get("fob") and f"FOB: {po['fob']}",
        po.get("shipping_and_instructions") and " ".join(po["shipping_and_instructions"].split()),
    ) if s]

    payload = {
        "customerIdentifier": {"id": customer_id},
        "facilityIdentifier": {"id": facility_id} if facility_id else {"name": facility_name},
        "referenceNum": ref,
        "poNum": ref,
        "deferNotification": True,  # create incomplete; we complete after the stock check
        "billingCode": billing_code,
        "notes": _clip(" | ".join(notes), 1000),
        "shippingNotes": _clip(" | ".join(shipping_notes), 1000),
        "routingInfo": routing,
        "shipTo": {
            "companyName": _clip(ship_to.get("business_name") or ship_to.get("name"), 100),
            "name": _clip(ship_to.get("name") or ship_to.get("business_name"), 100),
            "address1": _clip(ship_to.get("address1"), 100),
            "address2": _clip(ship_to.get("address2"), 100),
            "city": _clip(ship_to.get("city"), 50),
            "state": _clip(ship_to.get("state"), 50),
            "zip": _clip(ship_to.get("zip"), 20),
            "country": country_code(ship_to.get("country")),
            **{k: _clip(v, 100) for k, v in contact_from_text(po.get("critical_comments")).items() if v},
        },
        "orderItems": [
            {"itemIdentifier": {"sku": sku}, "qty": int(qty) if float(qty).is_integer() else qty}
            for sku, qty in lines.items()
        ],
    }
    if not payload["routingInfo"]:
        del payload["routingInfo"]
    return payload


def _get(d: dict, key: str, default=None):
    """3PL Central responses mix camelCase and PascalCase."""
    if key in d:
        return d[key]
    return d.get(key[0].upper() + key[1:], default)


def find_shortages(lines: Dict[str, float], order: dict, summaries: List[dict],
                   facility_id: Optional[int]) -> List[dict]:
    """Lines the warehouse can't fill. A line already fully allocated is fine;
    otherwise available stock at the facility must cover the quantity."""
    allocated = set()
    embedded = _get(order, "_embedded", {}) or {}
    for item in embedded.get("http://api.3plCentral.com/rels/orders/item", []) or _get(order, "orderItems", []) or []:
        ro = _get(item, "readOnly", {}) or {}
        if _get(ro, "fullyAllocated"):
            allocated.add(_get(_get(item, "itemIdentifier", {}) or {}, "sku"))

    available: Dict[str, float] = {}
    for s in summaries:
        if facility_id and _get(s, "facilityId") not in (None, facility_id):
            continue
        sku = _get(_get(s, "itemIdentifier", {}) or {}, "sku")
        available[sku] = available.get(sku, 0.0) + float(_get(s, "available", 0) or 0)

    shortages = []
    for sku, qty in lines.items():
        if sku in allocated:
            continue
        have = available.get(sku, 0.0)
        if have < qty:
            shortages.append({"sku": sku, "ordered": qty, "available": have})
    return shortages
