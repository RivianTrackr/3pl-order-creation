"""Match a PO's SKU to the client's item in 3PL Central.

Syncore POs name a size in the SKU ("LBC006INVJ-2XL"), while 3PL Central items
use a numeric variant code with the size in the description
("LBC006INVJ-15570" = "... - XXL"). An exact SKU is used when it exists;
otherwise the item is found by base SKU plus size, and anything less than one
clear match is an error rather than a guess.
"""

import re
from typing import Dict, List, Optional, Tuple

SIZE_ALIASES = {
    "XXS": "XXS", "2XS": "XXS",
    "XS": "XS", "S": "S", "SM": "S", "SMALL": "S",
    "M": "M", "MED": "M", "MEDIUM": "M",
    "L": "L", "LG": "L", "LARGE": "L",
    "XL": "XL", "1XL": "XL", "XLARGE": "XL",
    "XXL": "XXL", "2XL": "XXL", "2X": "XXL",
    "XXXL": "XXXL", "3XL": "XXXL", "3X": "XXXL",
    "XXXXL": "XXXXL", "4XL": "XXXXL", "4X": "XXXXL",
    "XXXXXL": "XXXXXL", "5XL": "XXXXXL", "5X": "XXXXXL",
    "OS": "OSFA", "OSFA": "OSFA", "ONESIZE": "OSFA", "ONE SIZE": "OSFA",
}


class SkuMatchError(ValueError):
    pass


def normalise_size(text: Optional[str]) -> str:
    key = re.sub(r"[^A-Z0-9 ]", "", (text or "").upper()).strip()
    return SIZE_ALIASES.get(key, key)


def split_sku(sku: str) -> Tuple[str, str]:
    """"LBC006INVJ-2XL" -> ("LBC006INVJ", "XXL"); no size suffix -> (sku, "")."""
    if "-" not in sku:
        return sku, ""
    base, _, suffix = sku.rpartition("-")
    size = normalise_size(suffix)
    return (base, size) if size and size in SIZE_ALIASES.values() else (sku, "")


def item_size(description: Optional[str]) -> str:
    """The size at the end of an item description ("... - Heather Forest - XXL")."""
    parts = [p.strip() for p in (description or "").split(" - ") if p.strip()]
    return normalise_size(parts[-1]) if parts else ""


def resolve(sku: str, items: List[dict], client_name: str) -> str:
    """The 3PL Central SKU to order for a PO's SKU."""
    by_sku = {str(i.get("sku") or "").casefold(): i for i in items}
    if sku.casefold() in by_sku:
        return str(by_sku[sku.casefold()]["sku"])

    base, size = split_sku(sku)
    if not size:
        raise SkuMatchError(f"{sku} is not an item for {client_name} in 3PL Central.")

    candidates = [i for i in items
                  if str(i.get("sku") or "").casefold().startswith(base.casefold() + "-")]
    if not candidates:
        raise SkuMatchError(f"{sku} is not an item for {client_name} in 3PL Central, and neither is any "
                            f"{base}- variant.")
    matches = [i for i in candidates if item_size(i.get("description")) == size]
    if len(matches) == 1:
        return str(matches[0]["sku"])
    if matches:
        raise SkuMatchError(
            f"{sku} matches more than one {client_name} item in 3PL Central: "
            f"{', '.join(str(i.get('sku')) for i in matches[:6])}.")
    sizes = sorted({item_size(i.get("description")) for i in candidates if item_size(i.get("description"))})
    raise SkuMatchError(
        f"{client_name} has {base}- items in 3PL Central but none in size {size}"
        + (f" (sizes there: {', '.join(sizes)})" if sizes else "") + f", so {sku} could not be matched.")


def resolve_lines(lines: Dict[str, float], items: List[dict], client_name: str) -> Tuple[Dict[str, float], Dict[str, str]]:
    """PO SKUs -> 3PL SKUs, keeping quantities. Raises on the first SKU that can't be matched."""
    resolved: Dict[str, float] = {}
    renames: Dict[str, str] = {}
    problems = []
    for sku, qty in lines.items():
        try:
            target = resolve(sku, items, client_name)
        except SkuMatchError as exc:
            problems.append(str(exc))
            continue
        if target != sku:
            renames[sku] = target
        resolved[target] = resolved.get(target, 0.0) + qty
    if problems:
        raise SkuMatchError(" ".join(problems))
    return resolved, renames
