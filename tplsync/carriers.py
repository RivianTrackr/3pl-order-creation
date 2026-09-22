"""Match a PO's Ship Via to the warehouse's carrier and service codes in 3PL Central.

3PL Central wants the carrier name and the *service code* exactly as configured
for the warehouse (GET /properties/carriers). This module only accepts a match
it is confident about; anything else raises CarrierMatchError so the order is
not created with a carrier or service the warehouse doesn't recognise.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Common ways carriers are written on POs, keyed by the usual 3PL Central name.
ALIASES: Dict[str, Tuple[str, ...]] = {
    # Only words that name the carrier: they're ignored when comparing service names, so
    # service words like "mail" or "express" must never appear here unless the carrier's
    # own name contains them.
    "usps": ("usps", "u.s.p.s.", "us postal service", "u.s. postal service", "united states postal service",
             "postal service"),
    "ups": ("ups", "u.p.s.", "united parcel service"),
    "fedex": ("fedex", "fed ex", "federal express"),
    "dhl": ("dhl", "dhl express"),
    "ontrac": ("ontrac", "on trac"),
}

# Word forms that mean the same thing in service names.
_PHRASES = (
    (r"\b(two|2nd|2)[\s-]?day\b", "2day"),
    (r"\b(three|3rd|3)[\s-]?day\b", "3day"),
    (r"\bnext[\s-]?day\b", "nextday"),
    (r"\bgnd\b", "ground"),
    (r"\bintl\b", "international"),
    (r"\bexp\b", "express"),
    (r"\bsat\b", "saturday"),
    (r"[®™]", ""),
    (r"&", " and "),
)


class CarrierMatchError(ValueError):
    pass


@dataclass
class Service:
    code: str
    description: str


@dataclass
class Carrier:
    name: str
    description: str = ""
    scac: str = ""
    services: List[Service] = field(default_factory=list)
    billing_codes: List[str] = field(default_factory=list)


@dataclass
class Routing:
    carrier: str
    mode: str
    service_description: str
    scac: str
    account: Optional[str]
    billing_code: str
    source: str          # po | override | default
    explanation: str

    def as_payload(self) -> Dict[str, str]:
        info = {"carrier": self.carrier, "mode": self.mode, "scacCode": self.scac, "account": self.account}
        return {k: v for k, v in info.items() if v}


def _alnum(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").casefold())


def _get(d: dict, key: str, default=None):
    return d.get(key, d.get(key[0].upper() + key[1:], default))


def parse_carrier_list(data: dict) -> List[Carrier]:
    """Normalise GET /properties/carriers. Deactivated carriers, and carriers not
    allowed on orders, are dropped. Carriers without their own services or billing
    codes use the defaults, as the API documents."""
    default_services = [Service(str(_get(s, "code", "")), str(_get(s, "description", "") or ""))
                        for s in _get(data, "defaultShipmentServices", []) or []]
    default_billing = [str(_get(b, "code", "")) for b in _get(data, "defaultBillingCodes", []) or []]
    embedded = _get(data, "_embedded", {}) or {}
    raw = embedded.get("http://api.3plCentral.com/rels/properties/carrier") \
        or next((v for k, v in embedded.items() if k.lower().endswith("/properties/carrier")), None) \
        or _get(data, "resourceList", []) or []
    carriers = []
    for c in raw:
        if _get(c, "deactivated") or _get(c, "disallowOnOrder"):
            continue
        services = [Service(str(_get(s, "code", "")), str(_get(s, "description", "") or ""))
                    for s in _get(c, "shipmentServices", []) or []]
        billing = [str(_get(b, "code", "")) for b in _get(c, "billingCodes", []) or []]
        carriers.append(Carrier(name=str(_get(c, "name", "")), description=str(_get(c, "description", "") or ""),
                                scac=str(_get(c, "scacCode", "") or ""),
                                services=services or list(default_services),
                                billing_codes=billing or list(default_billing)))
    return carriers


def carriers_to_json(carriers: List[Carrier]) -> List[dict]:
    return [{"name": c.name, "description": c.description, "scac": c.scac,
             "services": [{"code": s.code, "description": s.description} for s in c.services],
             "billing_codes": c.billing_codes} for c in carriers]


def carriers_from_json(items: List[dict]) -> List[Carrier]:
    return [Carrier(name=i["name"], description=i.get("description", ""), scac=i.get("scac", ""),
                    services=[Service(s["code"], s.get("description", "")) for s in i.get("services", [])],
                    billing_codes=i.get("billing_codes", [])) for i in items]


def _carrier_aliases(carrier: Carrier) -> List[str]:
    names = {carrier.name, carrier.description, carrier.scac}
    for key, aliases in ALIASES.items():
        if key in (_alnum(carrier.name), _alnum(carrier.description)):
            names.update(aliases)
    return sorted({" ".join(n.casefold().split()) for n in names if n and n.strip()}, key=len, reverse=True)


def find_carrier(ship_via: str, carriers: List[Carrier]) -> Tuple[Carrier, str]:
    """The carrier the Ship Via starts with, and the remaining service text."""
    text = " ".join(ship_via.split())
    lowered = text.casefold()
    matches = []
    for carrier in carriers:
        for alias in _carrier_aliases(carrier):
            if lowered == alias or re.match(re.escape(alias) + r"(?=[\s:/,-])", lowered):
                matches.append((len(alias), carrier, alias))
                break
    if not matches:
        names = ", ".join(c.name for c in carriers) or "none"
        raise CarrierMatchError(f"Ship Via {ship_via!r} doesn't start with a carrier set up in 3PL Central "
                                f"(carriers: {names}).")
    best = max(m[0] for m in matches)
    top = [m for m in matches if m[0] == best]
    if len(top) > 1:
        exact = [m for m in top if _alnum(m[1].name) == _alnum(m[2])]
        if len(exact) != 1:
            raise CarrierMatchError(f"Ship Via {ship_via!r} could be any of these 3PL Central carriers: "
                                    f"{', '.join(m[1].name for m in top)}. Add a Ship Via override.")
        top = exact
    _, carrier, alias = top[0]
    return carrier, text[len(alias):].strip(" :/,-")


def _service_tokens(text: str, carrier: Carrier) -> frozenset:
    value = text.casefold()
    for pattern, replacement in _PHRASES:
        value = re.sub(pattern, replacement, value)
    # Carrier words are dropped so "USPS Priority Mail" equals "Priority Mail". The carrier's
    # free-text description is not used here: it could contain service words.
    names = [carrier.name, carrier.scac]
    for key, aliases in ALIASES.items():
        if key in (_alnum(carrier.name), _alnum(carrier.description)):
            names.extend(aliases)
    carrier_words = set()
    for name in names:
        carrier_words.update(re.findall(r"[a-z0-9]+", (name or "").casefold().replace(".", "")))
    return frozenset(t for t in re.findall(r"[a-z0-9]+", value.replace(".", "")) if t not in carrier_words)


def find_service(service_text: str, carrier: Carrier, ship_via: str) -> Service:
    if not service_text:
        raise CarrierMatchError(f"Ship Via {ship_via!r} names the carrier {carrier.name} but no service.")
    by_code = [s for s in carrier.services if s.code.casefold() == service_text.casefold()]
    if len(by_code) == 1:
        return by_code[0]
    wanted = _service_tokens(service_text, carrier)
    matches = [s for s in carrier.services if wanted and _service_tokens(s.description, carrier) == wanted]
    if len(matches) == 1:
        return matches[0]
    available = "; ".join(f"{s.description} ({s.code})" for s in carrier.services[:40])
    more = f" and {len(carrier.services) - 40} more" if len(carrier.services) > 40 else ""
    if matches:
        raise CarrierMatchError(f"Ship Via {ship_via!r} matches several {carrier.name} services: "
                                f"{', '.join(f'{s.description} ({s.code})' for s in matches)}. Add a Ship Via override.")
    raise CarrierMatchError(f"{service_text!r} isn't one of the {carrier.name} services in 3PL Central, so no order "
                            f"was created. Available: {available}{more}. Add a Ship Via override if it goes by "
                            f"another name.")


def _check_billing(billing_code: str, carrier: Carrier) -> None:
    if carrier.billing_codes and billing_code not in carrier.billing_codes:
        raise CarrierMatchError(f"Billing code {billing_code!r} isn't allowed for {carrier.name} in 3PL Central "
                                f"(allowed: {', '.join(carrier.billing_codes)}).")


ACCOUNT_TOKEN = re.compile(r"[A-Za-z0-9]{4,20}")


def split_account(text: str) -> Tuple[str, Optional[str]]:
    """"UPS GRND C713X7" -> ("UPS GRND", "C713X7"). The team writes the shipping account number as the last
    word; it must be 4-20 letters/digits and contain a digit, so words like "AIR" aren't mistaken for one."""
    head, _, last = text.rpartition(" ")
    if head and ACCOUNT_TOKEN.fullmatch(last) and any(ch.isdigit() for ch in last):
        return head, last
    return text, None


def _from_override(text: str, o: dict, carriers_by_name: Dict[str, Carrier], billing_code: str,
                   account: Optional[str]) -> Routing:
    carrier = carriers_by_name.get(_alnum(o.get("carrier")))
    if carrier is None:
        raise CarrierMatchError(f"The Ship Via override for {text!r} uses carrier {o.get('carrier')!r}, "
                                f"which isn't set up in 3PL Central any more.")
    service = next((s for s in carrier.services if s.code == o.get("mode")), None)
    if service is None:
        raise CarrierMatchError(f"The Ship Via override for {text!r} uses service code {o.get('mode')!r}, "
                                f"which {carrier.name} doesn't have in 3PL Central.")
    billing = o.get("billingCode") or billing_code
    _check_billing(billing, carrier)
    account = account or o.get("account")
    return Routing(carrier.name, service.code, service.description, o.get("scacCode") or carrier.scac,
                   account, billing, "override",
                   f"Ship Via {text!r} -> {carrier.name} / {service.description} ({service.code}) from an override"
                   + (f", account {account}" if account else ""))


def resolve_routing(ship_via: Optional[str], overrides: Dict[str, dict], carriers: List[Carrier],
                    billing_code: str, default_carrier: Optional[str], default_mode: Optional[str]) -> Routing:
    """Carrier, service code, account and billing for a PO, validated against 3PL Central.

    A shipping account number written as the last word of the Ship Via ("UPS GRND C713X7") is used as the
    order's account, whether the rest matches an override or a carrier and service directly."""
    if not carriers:
        raise CarrierMatchError("3PL Central returned no carriers, so shipping can't be checked.")
    text = " ".join((ship_via or "").split())
    by_name = {_alnum(c.name): c for c in carriers}
    base, account = split_account(text)

    if text.casefold() in overrides:
        return _from_override(text, overrides[text.casefold()], by_name, billing_code, None)
    if account and base.casefold() in overrides:
        return _from_override(text, overrides[base.casefold()], by_name, billing_code, account)

    if text:
        try:
            carrier, service_text = find_carrier(text, carriers)
            service = find_service(service_text, carrier, text)
            account = None
        except CarrierMatchError:
            if not account:
                raise
            try:
                carrier, service_text = find_carrier(base, carriers)
                service = find_service(service_text, carrier, base)
            except CarrierMatchError:
                raise CarrierMatchError(
                    f"Ship Via {text!r} doesn't match a 3PL Central service, with or without {account!r} as the "
                    f"account number. Add a Ship Via override for {base!r}.") from None
        _check_billing(billing_code, carrier)
        return Routing(carrier.name, service.code, service.description, carrier.scac, account, billing_code, "po",
                       f"Ship Via {text!r} -> {carrier.name} / {service.description} ({service.code})"
                       + (f", account {account}" if account else ""))

    if not default_carrier:
        raise CarrierMatchError("The PO has no Ship Via and no default carrier is set in Settings.")
    carrier = by_name.get(_alnum(default_carrier))
    if carrier is None:
        raise CarrierMatchError(f"The default carrier {default_carrier!r} in Settings isn't set up in 3PL Central.")
    service = find_service(default_mode or "", carrier, f"{default_carrier} {default_mode or ''}".strip())
    _check_billing(billing_code, carrier)
    return Routing(carrier.name, service.code, service.description, carrier.scac, None, billing_code, "default",
                   f"PO has no Ship Via -> default {carrier.name} / {service.description} ({service.code})")
