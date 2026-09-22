import json

import pytest

from tplsync.carriers import CarrierMatchError, parse_carrier_list, resolve_routing

from .test_processor import NOW, env, make_po  # noqa: F401 - env is a fixture

# Shape from the 3PL Central docs (GET /properties/carriers); codes are illustrative.
CARRIERS = parse_carrier_list({
    "defaultBillingCodes": [{"code": "BillThirdParty"}, {"code": "FreightCollect"}, {"code": "Prepaid"}],
    "defaultShipmentServices": [{"code": "04", "description": "Ground"}, {"code": "09", "description": "Two-Day"}],
    "_embedded": {"http://api.3plCentral.com/rels/properties/carrier": [
        {"name": "USPS", "description": "United States Postal Service", "scacCode": "USPS", "deactivated": False,
         "disallowOnOrder": False, "billingCodes": [{"code": "Prepaid"}],
         "shipmentServices": [
             {"code": "92", "description": "USPS Priority Mail"},
             {"code": "93", "description": "USPS Priority Mail Express"},
             {"code": "94", "description": "USPS First-Class Package"},
             {"code": "95", "description": "USPS Parcel Select Ground"},
         ]},
        {"name": "UPS", "description": "UPS", "scacCode": "UPSN", "shipmentServices": [
            {"code": "03", "description": "UPS Ground"},
            {"code": "02", "description": "UPS 2nd Day Air"},
            {"code": "01", "description": "UPS Next Day Air"},
            {"code": "12", "description": "UPS 3 Day Select"},
        ]},
        {"name": "FedEx", "description": "FedEx", "scacCode": "FDEG", "shipmentServices": [
            {"code": "FEDEX_GROUND", "description": "FedEx Ground"},
            {"code": "FEDEX_2_DAY", "description": "FedEx 2Day"},
        ]},
        {"name": "Truckline", "description": "Old LTL", "deactivated": True, "shipmentServices": []},
        {"name": "Local Courier", "shipmentServices": []},
    ]},
})


def route(ship_via, overrides=None, billing="Prepaid", default_carrier=None, default_mode=None):
    return resolve_routing(ship_via, overrides or {}, CARRIERS, billing, default_carrier, default_mode)


def test_parse_drops_deactivated_and_uses_default_services():
    names = [c.name for c in CARRIERS]
    assert "Truckline" not in names
    courier = next(c for c in CARRIERS if c.name == "Local Courier")
    assert [s.code for s in courier.services] == ["04", "09"]
    assert courier.billing_codes == ["BillThirdParty", "FreightCollect", "Prepaid"]


@pytest.mark.parametrize("ship_via, carrier, code", [
    ("USPS Priority Mail", "USPS", "92"),
    ("usps  priority mail", "USPS", "92"),
    ("USPS Priority Mail Express", "USPS", "93"),
    ("U.S. Postal Service Priority Mail", "USPS", "92"),
    ("USPS - First-Class Package", "USPS", "94"),
    ("UPS Ground", "UPS", "03"),
    ("UPS 2 Day Air", "UPS", "02"),
    ("UPS Second Day Air".replace("Second", "2nd"), "UPS", "02"),
    ("Fed Ex 2 Day", "FedEx", "FEDEX_2_DAY"),
    ("FedEx Ground", "FedEx", "FEDEX_GROUND"),
    ("UPS 03", "UPS", "03"),
    ("Local Courier Two Day", "Local Courier", "09"),
])
def test_ship_via_matches_carrier_and_service_code(ship_via, carrier, code):
    r = route(ship_via)
    assert (r.carrier, r.mode, r.source) == (carrier, code, "po")
    assert r.as_payload()["mode"] == code


@pytest.mark.parametrize("ship_via, message", [
    ("USPS Priority", "isn't one of the USPS services"),          # partial names are never guessed
    ("USPS Media Mail", "isn't one of the USPS services"),
    ("USPS", "no service"),
    ("Pony Express Overnight", "doesn't start with a carrier"),
    ("UPSGround", "doesn't start with a carrier"),
])
def test_unconfident_matches_raise(ship_via, message):
    with pytest.raises(CarrierMatchError, match=message):
        route(ship_via)


def test_error_lists_available_services():
    with pytest.raises(CarrierMatchError) as exc:
        route("USPS Media Mail")
    assert "USPS Priority Mail (92)" in str(exc.value)


def test_override_is_validated():
    overrides = {"usps priority mail": {"carrier": "USPS", "mode": "93"}}
    r = route("USPS Priority Mail", overrides)
    assert (r.mode, r.source) == ("93", "override")
    with pytest.raises(CarrierMatchError, match="doesn't have"):
        route("USPS Priority Mail", {"usps priority mail": {"carrier": "USPS", "mode": "999"}})


def test_billing_code_must_be_allowed():
    with pytest.raises(CarrierMatchError, match="isn't allowed for USPS"):
        route("USPS Priority Mail", billing="FreightCollect")
    assert route("UPS Ground", billing="FreightCollect").billing_code == "FreightCollect"


def test_blank_ship_via_uses_validated_defaults():
    assert route("", default_carrier="UPS", default_mode="Ground").mode == "03"
    with pytest.raises(CarrierMatchError, match="no default carrier"):
        route("")
    with pytest.raises(CarrierMatchError, match="isn't set up"):
        route(None, default_carrier="Pony Express", default_mode="Ground")


def test_processor_refuses_unmatched_ship_via_and_caches_carriers(env):  # noqa: F811
    po = make_po()
    po["ship_via"] = "UPS Carrier Pigeon"
    proc, db, syncore, tpl, notifier = env([po])
    proc.run(NOW)
    assert tpl.created == []
    assert db.get(900)["state"] == "error" and "isn't one of the UPS services" in db.get(900)["last_error"]
    assert "shipping.unmatched" in [e["event"] for e in db.list_events(po_id=900)]
    assert len(notifier.sent) == 1
    assert json.loads(db.get_meta("tpl_carriers"))["carriers"][0]["name"] == "UPS"


def test_processor_sends_service_code(env):  # noqa: F811
    proc, db, syncore, tpl, _ = env([make_po()])
    proc.run(NOW)
    assert tpl.created[0]["routingInfo"] == {"carrier": "UPS", "mode": "03", "scacCode": "UPSN"}


def test_carrier_description_words_are_not_ignored_in_service_names():
    carriers = parse_carrier_list({"_embedded": {"http://api.3plCentral.com/rels/properties/carrier": [
        {"name": "UPSMI", "description": "UPS Mail Innovations", "shipmentServices": [
            {"code": "1", "description": "Mail Innovations Expedited"}, {"code": "2", "description": "Expedited"}]},
    ]}})
    r = resolve_routing("UPSMI Expedited", {}, carriers, "Prepaid", None, None)
    assert r.mode == "2"


def test_account_number_at_the_end_of_an_override_shorthand():
    overrides = {"ups grnd": {"carrier": "UPS", "mode": "03", "billingCode": "BillThirdParty"}}
    r = route("UPS GRND C713X7", overrides, billing="Prepaid")
    assert (r.carrier, r.mode, r.account, r.billing_code, r.source) == ("UPS", "03", "C713X7", "BillThirdParty", "override")
    assert r.as_payload()["account"] == "C713X7"
    assert "account C713X7" in r.explanation
    # The shorthand alone still works, with the override's saved account (none here)
    assert route("UPS GRND", overrides).account is None
    # A saved account on the override is replaced by the one on the PO
    overrides["ups grnd"]["account"] = "DEFAULT1"
    assert route("ups  grnd 9W2Y44", overrides).account == "9W2Y44"
    assert route("UPS GRND", overrides).account == "DEFAULT1"


def test_account_number_after_a_service_name_without_an_override():
    r = route("UPS Ground C713X7")
    assert (r.carrier, r.mode, r.account) == ("UPS", "03", "C713X7")
    assert route("UPS Ground").account is None


def test_words_are_not_mistaken_for_account_numbers():
    from tplsync.carriers import service_words, split_account
    words = service_words(CARRIERS)
    assert split_account("UPS GRND C713X7", words) == ("UPS GRND", "C713X7")
    assert split_account("UPS NEXT DAY AIR", words) == ("UPS NEXT DAY AIR", None)   # no digit
    assert split_account("UPS GRND 12", words) == ("UPS GRND 12", None)             # too short
    assert split_account("UPS", words) == ("UPS", None)
    # Words that name a service in 3PL Central are never read as an account number
    assert split_account("UPS 3DAY", words) == ("UPS 3DAY", None)
    assert split_account("FEDEX 2DAY", words) == ("FEDEX 2DAY", None)
    # A service name containing a digit still matches as written first
    assert route("UPS 2nd Day Air").account is None


def test_service_names_with_digits_keep_their_last_word():
    # "3DAY" is the service, not an account, so the shorthand to override is the whole "UPS 3DAY"
    with pytest.raises(CarrierMatchError, match="Add a Ship Via override for 'UPS 3DAY'"):
        route("UPS 3DAY C713X7")
    overrides = {"ups 3day": {"carrier": "UPS", "mode": "12", "scacCode": "UPSN",
                              "account": None, "billingCode": None}}
    assert route("UPS 3DAY", overrides).mode == "12"
    r = route("UPS 3DAY 9W2Y44", overrides)
    assert (r.mode, r.account) == ("12", "9W2Y44")


def test_unmatched_shorthand_with_account_says_what_to_add():
    import pytest
    with pytest.raises(CarrierMatchError, match="Add a Ship Via override for 'UPS GRND'"):
        route("UPS GRND C713X7")
