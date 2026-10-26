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


# Lines that describe a product's variants or decoration rather than a product.
DETAIL_TYPES = {"color", "size", "productcomment", "decorationmethod", "decorationcomment"}


# Comment lines that describe a product rather than name it ("Item Value : $67.89", "Item Cost : $10.24").
_INFO_LINE = re.compile(r"^\s*(item\s+)?(value|cost|price)\s*:", re.IGNORECASE)


def _product_above(items: List[dict], index: int) -> Optional[dict]:
    """The nearest line above items[index] that is a product (not a SKU line, a colour/size/decoration line, a
    blank line or an "Item Value : $..." note)."""
    for line in reversed(items[:index]):
        if tagged_sku(line) or str(line.get("type") or "").replace(" ", "").casefold() in DETAIL_TYPES:
            continue
        text = str(line.get("description") or "").strip()
        if (not text or _INFO_LINE.match(text)) and _qty(line.get("quantity")) <= 0:
            continue
        return line
    return None


# On-demand items: an "OD" (or "ODJ") right after the style number, e.g. BAL513OD-3, WWD199OD-BLACK, WT072ODJ-45.
OD_SKU = re.compile(r"\dODJ?(?![A-Za-z])")
_TAGS = re.compile(r"<[^>]+>")


def clean_text(text) -> str:
    """Description text as a person would read it: no HTML tags or entities, single spaces."""
    import html
    return " ".join(html.unescape(_TAGS.sub(" ", str(text or ""))).split())


def _header_above(items: List[dict], index: int) -> Optional[dict]:
    """The nearest top-level heading line above items[index] (e.g. "ON DEMAND FROM JUST INK", "INVENTORY")."""
    return next((p for p in reversed(items[:index]) if p.get("parent_id") == 0 and not tagged_sku(p)), None)


def is_on_demand(items: List[dict], index: int, sku: str, exempt=frozenset()) -> bool:
    """Whether the SKU on items[index] is an on-demand item. INV SKUs and SKUs on the client's Allowed SKUs list
    are real inventory items; otherwise an OD SKU, or a SKU under an "ON DEMAND" heading, is on demand."""
    if "INV" in sku.upper() or sku.strip().casefold() in exempt:
        return False
    if OD_SKU.search(sku):
        return True
    header = _header_above(items, index)
    return bool(header) and "ondemand" in clean_text(header.get("description")).casefold().replace(" ", "")


def on_demand_items(po: dict, sku_pattern: str, allowed=frozenset(), exempt=frozenset()) -> List[dict]:
    """The PO's on-demand items with what the warehouse needs to know: product, colour/size and quantity.
    [{"sku", "product", "code", "variants": [(label, qty)], "qty"}]. exempt: the client's Allowed SKUs."""
    details: List[dict] = []
    order_lines(po, sku_pattern, allowed, on_demand=True, details=details)
    items = po.get("line_items") or []
    result = []
    for d in details:
        if not is_on_demand(items, d["index"], d["sku"], exempt):
            continue
        line = items[d["index"]]
        product = line if line.get("sku") and str(line.get("sku")).strip() == d["sku"] else d["product"] or line
        variants = []
        for v, qty in d["variants"]:
            if v is line or v is product:
                label = ""
            elif str(v.get("type") or "").casefold() in ("color", "size"):
                label = " / ".join(_variant_parts(v, items))
            else:
                label = clean_text(v.get("description"))      # e.g. a "Black" line after the SKU line
            variants.append((label, qty))
        code = str(product.get("sku") or "").strip() if product is not line else ""
        result.append({"sku": d["sku"], "product": clean_text(product.get("description")) if product is not line
                       or not tagged_sku(line) else "",
                       "code": code if code != d["sku"] else "", "variants": variants,
                       "qty": sum(q for _, q in variants)})
    return result


def on_demand_note(items: List[dict], generic_sku: str) -> str:
    """The Warehouse Instructions line for on-demand items."""
    parts = []
    for item in items:
        what = item["product"] or "Item"
        if item["code"]:
            what += f" ({item['code']})"
        sizes = ", ".join(f"{lab} x{qty:g}" if lab else f"x{qty:g}" for lab, qty in item["variants"]) or "x0"
        parts.append(f"{what} - {sizes} [PO SKU {item['sku']}]")
    return f"ON DEMAND (sent as {generic_sku}): " + "; ".join(parts)


def order_lines(po: dict, sku_pattern: str, allowed=frozenset(), on_demand: bool = False,
                details: Optional[List[dict]] = None) -> "OrderedDict[str, float]":
    """SKU -> quantity for the PO lines that belong in 3PL Central.

    Two layouts are supported:

    * The inventory SKU is in a comment line's description ("SKU: EBC006INVJ-2XL").
      The SKU line has no quantity, so it takes it from the product line right above
      it when that line has its own quantity, or else from its group: the nearest
      top-level line above it, whose quantity is its own or sits on its Color/Size
      lines (on-demand POs put an "ON DEMAND FROM ..." header there).
    * The inventory SKU is in the line's own SKU field. A matching parent whose
      matching children carry their own quantities (e.g. sizes) is skipped so it
      isn't counted twice.

    With on_demand, on-demand SKU lines (see is_on_demand) are read more leniently, since they all go out as
    one generic item: a SKU naming one of its group's colours/sizes takes just that one, several colours/sizes
    under one SKU are added up, and a quantity on the line right after the SKU line is accepted.

    A SKU belongs if it matches sku_pattern or is in allowed (casefolded SKUs from the Allowed SKUs page).
    Repeated SKUs are summed. details, if given, gets one entry per SKU line read:
    {"sku", "index", "product", "variants": [(line, qty)]}.
    """
    pattern = re.compile(sku_pattern)

    def wanted(sku: str) -> bool:
        return bool(pattern.search(sku)) or sku.strip().casefold() in allowed

    items = po.get("line_items") or []
    lines: "OrderedDict[str, float]" = OrderedDict()

    def add(sku: str, qty: float) -> None:
        lines[sku] = lines.get(sku, 0.0) + qty

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

    claimed = set()   # line ids whose quantity was used by a SKU line (groups, products, single colours/sizes)

    # Layout 1: "SKU: ..." description lines.
    for index, li in enumerate(items):
        sku = tagged_sku(li)
        if not sku or not wanted(sku):
            continue
        od = on_demand and is_on_demand(items, index, sku, allowed)
        product = _product_above(items, index)
        variants: List[tuple] = []
        if _qty(li.get("quantity")) > 0:
            variants = [(li, _qty(li.get("quantity")))]
        elif product is not None and _qty(product.get("quantity")) > 0 \
                and not leaves(product.get("line_id")) and product.get("line_id") not in claimed:
            # The product line right above the SKU line carries the quantity itself
            # (e.g. "Peter Millar Polo" qty 1, then "SKU: BAL528INV-59838-24227").
            claimed.add(product.get("line_id"))
            variants = [(product, _qty(product.get("quantity")))]
        else:
            # The SKU belongs to the nearest group above it: a top-level line whose quantity is either
            # its own or spread over its Color/Size lines ("ON DEMAND FROM ..." headers work this way).
            group = next((p for p in reversed(items[:index])
                          if p.get("parent_id") == 0 and not tagged_sku(p)
                          and (leaf_quantity(p.get("line_id")) > 0 or _qty(p.get("quantity")) > 0)), None)
            if group is None:
                group = next((p for p in reversed(items[:index])
                              if _qty(p.get("quantity")) > 0 and not tagged_sku(p)), None)
            if group is None and od:
                # Some on-demand POs put the colour and quantity on the line right after the SKU line.
                nxt = items[index + 1] if index + 1 < len(items) else None
                if nxt is not None and not tagged_sku(nxt) and nxt.get("parent_id") != 0 \
                        and _qty(nxt.get("quantity")) > 0 and not children.get(nxt.get("line_id")) \
                        and nxt.get("line_id") not in claimed:
                    claimed.add(nxt.get("line_id"))
                    variants = [(nxt, _qty(nxt.get("quantity")))]
            if not variants:
                if group is None:
                    raise LineMappingError(f"Found SKU {sku} but no product line with a quantity above it.")
                group_leaves = leaves(group.get("line_id"))
                if od and len(group_leaves) > 1:
                    leaf = _matching_leaf(sku, [v for v in group_leaves if v.get("line_id") not in claimed], items)
                    if leaf is not None:          # e.g. WDT037OD-LARGE-WHITE takes the White / Large line
                        claimed.add(leaf.get("line_id"))
                        variants = [(leaf, _qty(leaf.get("quantity")))]
            if not variants:
                if group.get("line_id") in claimed or any(v.get("line_id") in claimed for v in group_leaves):
                    raise LineMappingError(
                        f"Several SKU lines belong to {group.get('description')!r}, so the quantity for {sku} is "
                        f"ambiguous. Give each SKU line its own quantity.")
                if len(group_leaves) > 1 and not od:
                    raise LineMappingError(
                        f"SKU {sku} covers {len(group_leaves)} colors/sizes "
                        f"({', '.join(str(v.get('description')) for v in group_leaves)}), and each is a different "
                        f"warehouse item. Give each variant its own SKU line.")
                claimed.add(group.get("line_id"))
                variants = ([(v, _qty(v.get("quantity"))) for v in group_leaves]
                            or [(group, _qty(group.get("quantity")))])
        add(sku, sum(q for _, q in variants))
        if details is not None:
            details.append({"sku": sku, "index": index, "product": product, "variants": variants})

    # Layout 2: SKU field on the line itself.
    matching = [li for li in items
                if li.get("sku") and wanted(str(li["sku"])) and _qty(li.get("quantity")) > 0
                and li.get("line_id") not in claimed]
    parents_with_children = {li.get("parent_id") for li in matching if li.get("parent_id")}
    for li in matching:
        if li.get("line_id") not in parents_with_children:
            sku = str(li["sku"]).strip()
            add(sku, _qty(li.get("quantity")))
            if details is not None:
                found = leaves(li.get("line_id"))
                details.append({"sku": sku, "index": items.index(li), "product": li,
                                "variants": [(v, _qty(v.get("quantity"))) for v in found]
                                or [(li, _qty(li.get("quantity")))]})
    return lines


def _variant_parts(line: dict, items: List[dict]) -> List[str]:
    """Colour / size texts for a Color/Size line, from the top of its branch down."""
    by_id = {li.get("line_id"): li for li in items}
    parts, current = [], line
    while current is not None:
        if str(current.get("type") or "").casefold() in ("color", "size"):
            parts.append(clean_text(current.get("description")))
        current = by_id.get(current.get("parent_id"))
    return [p for p in reversed(parts) if p]


def _matching_leaf(sku: str, leaves: List[dict], items: List[dict]) -> Optional[dict]:
    """The one colour/size line a SKU names, e.g. WDT037OD-LARGE-WHITE -> the White / Large line."""
    from .skus import normalise_size
    words = {w for w in re.split(r"[^a-z0-9]+", sku.casefold()) if w}
    named = [leaf for leaf in leaves
             if (tokens := {t for part in _variant_parts(leaf, items) for t in re.findall(r"[a-z0-9]+", part.casefold())})
             and tokens <= words]
    if len(named) == 1:
        return named[0]
    sizes = {normalise_size(w) for w in words}
    by_size = [leaf for leaf in leaves
               if str(leaf.get("type") or "").casefold() == "size" and normalise_size(leaf.get("description")) in sizes]
    return by_size[0] if len(by_size) == 1 else None


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
                facility_id: Optional[int], facility_name: str, routing: dict,
                extra_notes: Optional[List[str]] = None) -> dict:
    ship_to = po.get("ship_to") or {}
    ref = reference_number(po)
    client = job.get("client") or {}
    billing_code = routing.pop("billingCode", "Prepaid")

    notes = [f"Syncore Job {po['job_number']} / PO {ref}", *(extra_notes or [])]
    if client.get("business_name"):
        notes.append(f"Client: {client['business_name']}")
    if (job.get("store") or {}).get("name"):
        notes.append(f"Store: {job['store']['name']}")
    for field in ("shipping_and_instructions", "critical_comments"):
        if po.get(field):
            notes.append(" ".join(po[field].split()))

    payload = {
        "customerIdentifier": {"id": customer_id},
        "facilityIdentifier": {"id": facility_id} if facility_id else {"name": facility_name},
        "referenceNum": ref,
        "poNum": ref,
        "deferNotification": True,  # create incomplete; we complete after the stock check
        "billingCode": billing_code,
        # notes = Warehouse Instructions in 3PL Manager. shippingNotes (Carrier Instructions) is
        # deliberately left off: the PO's instructions belong in one place.
        "notes": _clip(" | ".join(notes), 1000),
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


def _address(ship_to: Optional[dict]) -> str:
    t = ship_to or {}
    parts = [t.get(k) for k in ("business_name", "name", "address1", "address2", "city", "state", "zip")]
    return " ".join(" ".join(str(x).split()) for x in parts if x)


def snapshot(po: dict, lines: Dict[str, float]) -> dict:
    """What was sent to 3PL Central for a PO, to notice later edits in Syncore."""
    return {"lines": {sku: float(qty) for sku, qty in lines.items()},
            "ship_via": " ".join(str(po.get("ship_via") or "").split()),
            "ship_to": _address(po.get("ship_to"))}


def snapshot_changes(sent: dict, now: dict) -> List[str]:
    """Plain-English differences between what was sent and the PO as it is now."""
    changes = []
    before, after = sent.get("lines") or {}, now.get("lines") or {}
    for sku in sorted(set(before) | set(after)):
        a, b = before.get(sku), after.get(sku)
        if a is None:
            changes.append(f"{sku} added (qty {b:g})")
        elif b is None:
            changes.append(f"{sku} removed (was qty {a:g})")
        elif float(a) != float(b):
            changes.append(f"{sku} quantity {a:g} -> {b:g}")
    for key, label in (("ship_via", "Ship Via"), ("ship_to", "Ship-to address")):
        if (sent.get(key) or "").casefold() != (now.get(key) or "").casefold():
            changes.append(f"{label} changed from \"{sent.get(key) or '-'}\" to \"{now.get(key) or '-'}\"")
    return changes
