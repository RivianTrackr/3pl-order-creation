import pytest

from tplsync import mapping


def po(**overrides):
    base = {
        "id": 900, "number": 3, "job_number": 12345,
        "supplier": {"id": 1, "name": "Example Supplier LLC"},
        "ship_to": {"business_name": "Acme Co", "name": "Pat Doe", "address1": "1 Main St", "address2": "",
                    "city": "Austin", "state": "TX", "zip": "78701", "country": "US"},
        "ship_via": "UPS Ground",
        "critical_comments": "",
        "line_items": [],
    }
    base.update(overrides)
    return base


def test_reference_number():
    assert mapping.reference_number(po()) == "12345-3"


def test_vendor_match_ignores_case_and_spacing():
    assert mapping.is_vendor(po(supplier={"name": "example  Supplier LLC "}), "Example Supplier LLC")
    assert not mapping.is_vendor(po(supplier={"name": "Another Vendor Co"}), "Example Supplier LLC")
    assert not mapping.is_vendor(po(supplier=None), "Example Supplier LLC")


def test_order_lines_filters_skus_and_sums_duplicates():
    items = [
        {"line_id": 1, "parent_id": 0, "sku": "ABC-INV-01", "quantity": 10},
        {"line_id": 2, "parent_id": 0, "sku": "JI-OD-7", "quantity": 5},
        {"line_id": 3, "parent_id": 0, "sku": "PLAIN-99", "quantity": 4},
        {"line_id": 4, "parent_id": 0, "sku": "ABC-INV-01", "quantity": 2},
        {"line_id": 5, "parent_id": 0, "sku": "", "quantity": 1, "type": "Comment"},
        {"line_id": 6, "parent_id": 0, "sku": "XINV", "quantity": 0},
    ]
    assert dict(mapping.order_lines(po(line_items=items), "INV|OD")) == {"ABC-INV-01": 12, "JI-OD-7": 5}


def test_order_lines_does_not_double_count_parent_with_size_children():
    items = [
        {"line_id": 10, "parent_id": 0, "sku": "TEE-INV", "quantity": 30, "type": "Asi"},
        {"line_id": 11, "parent_id": 10, "sku": "TEE-INV-S", "quantity": 10, "type": "Size"},
        {"line_id": 12, "parent_id": 10, "sku": "TEE-INV-M", "quantity": 20, "type": "Size"},
    ]
    assert dict(mapping.order_lines(po(line_items=items), "INV|OD")) == {"TEE-INV-S": 10, "TEE-INV-M": 20}


def test_build_order_payload():
    routing = {"carrier": "UPS", "mode": "03", "billingCode": "Prepaid"}
    payload = mapping.build_order(
        po(shipping_and_instructions="Box by size"),
        {"client": {"business_name": "Client Inc"}, "store": {"name": "Client Store"}},
        {"A-INV": 3.0}, customer_id=77, facility_id=None, facility_name="Example Warehouse", routing=routing)

    assert payload["customerIdentifier"] == {"id": 77}
    assert payload["facilityIdentifier"] == {"name": "Example Warehouse"}
    assert payload["referenceNum"] == "12345-3"
    assert payload["deferNotification"] is True
    assert payload["billingCode"] == "Prepaid"
    assert payload["routingInfo"] == {"carrier": "UPS", "mode": "03"}
    assert payload["shipTo"]["companyName"] == "Acme Co"
    assert payload["orderItems"] == [{"itemIdentifier": {"sku": "A-INV"}, "qty": 3}]
    assert "Client Store" in payload["notes"] and "Box by size" in payload["notes"]
    assert "shippingNotes" not in payload   # Carrier Instructions stays empty in 3PL Manager


def test_find_shortages():
    order = {"_embedded": {"http://api.3plCentral.com/rels/orders/item": [
        {"itemIdentifier": {"sku": "A"}, "readOnly": {"fullyAllocated": True}},
        {"itemIdentifier": {"sku": "B"}, "readOnly": {"fullyAllocated": False}},
    ]}}
    summaries = [
        {"itemIdentifier": {"sku": "B"}, "available": 4, "facilityId": 1},
        {"itemIdentifier": {"sku": "B"}, "available": 100, "facilityId": 2},
        {"itemIdentifier": {"sku": "C"}, "available": 50, "facilityId": 1},
    ]
    lines = {"A": 999, "B": 5, "C": 50, "D": 1}
    assert mapping.find_shortages(lines, order, summaries, facility_id=1) == [
        {"sku": "B", "ordered": 5, "available": 4},
        {"sku": "D", "ordered": 1, "available": 0},
    ]


def test_sku_in_comment_description_takes_quantity_from_product_line():
    # The layout Syncore POs use: the inventory SKU is a comment line under the product line
    items = [
        {"line_id": 183744628, "parent_id": 0, "type": "Comment", "sku": "BC3501CVC / Sanmar",
         "description": "Bella+Canvas Unisex Jersey Long-Sleeve T-Shirt - Heather Forest -  2XL", "quantity": 1},
        {"line_id": 183744629, "parent_id": 0, "type": "Comment", "sku": "",
         "description": "SKU: EBC006INVJ-2XL", "quantity": 0},
    ]
    assert dict(mapping.order_lines(po(line_items=items), "INV|OD")) == {"EBC006INVJ-2XL": 1}
    assert mapping.po_skus(po(line_items=items)) == ["BC3501CVC / Sanmar", "EBC006INVJ-2XL"]


def test_multiple_products_each_with_sku_line():
    items = [
        {"line_id": 1, "sku": "BC3501 / Sanmar", "description": "Tee - M", "quantity": 4},
        {"line_id": 2, "sku": "", "description": "SKU: EBC006INVJ-M", "quantity": 0},
        {"line_id": 3, "sku": "PC54 / Sanmar", "description": "Tee - L", "quantity": 2},
        {"line_id": 4, "sku": "", "description": "sku:  LBC007INVJ-L ", "quantity": 0},
        {"line_id": 5, "sku": "", "description": "SKU: PLAIN-12", "quantity": 0},
    ]
    assert dict(mapping.order_lines(po(line_items=items), "INV|OD")) == {"EBC006INVJ-M": 4, "LBC007INVJ-L": 2}


def test_ambiguous_sku_lines_raise():
    items = [
        {"line_id": 1, "sku": "BC3501 / Sanmar", "description": "Tee", "quantity": 6},
        {"line_id": 2, "sku": "", "description": "SKU: A-INV-M", "quantity": 0},
        {"line_id": 3, "sku": "", "description": "SKU: A-INV-L", "quantity": 0},
    ]
    import pytest
    with pytest.raises(mapping.LineMappingError):
        mapping.order_lines(po(line_items=items), "INV|OD")


def test_country_and_contact_from_critical_comments():
    p = po(critical_comments="pat.doe@example.com\r\n+15551234567")
    p["ship_to"]["country"] = "United States"
    payload = mapping.build_order(p, {}, {"A-INV": 1}, 77, None, "Example Warehouse", {})
    assert payload["shipTo"]["country"] == "US"
    assert payload["shipTo"]["emailAddress"] == "pat.doe@example.com"
    assert payload["shipTo"]["phoneNumber"] == "+15551234567"
    assert "\r" not in payload["notes"]
    assert mapping.contact_from_text("Call 555-123-4567 first")["phoneNumber"] == "555-123-4567"
    assert mapping.country_code("ca") == "CA" and mapping.country_code("") == "US"



def test_on_demand_groups_take_quantity_from_their_color_and_size_lines():
    # Shape of a real on-demand PO: header, product, SKU line, then Color/Size children of the header.
    items = [
        {"line_id": 1, "parent_id": 0, "type": "Comment", "sku": "", "description": "ON DEMAND FROM HANDSTANDS", "quantity": 0},
        {"line_id": 2, "parent_id": None, "type": "Comment", "sku": "70007 / Handstands", "description": "Mini Speaker", "quantity": 0},
        {"line_id": 3, "parent_id": None, "type": "Comment", "sku": "", "description": "SKU: WWD119OD-BLACK", "quantity": 0},
        {"line_id": 4, "parent_id": 1, "type": "Color", "sku": "", "description": "Black", "quantity": 1},
        {"line_id": 5, "parent_id": 0, "type": "Comment", "sku": "", "description": "ON DEMAND FROM PRINTFUL", "quantity": 0},
        {"line_id": 6, "parent_id": None, "type": "Comment", "sku": "18500 / Printful", "description": "Hoodie", "quantity": 0},
        {"line_id": 7, "parent_id": None, "type": "Comment", "sku": "", "description": "SKU: WWD265OD-M-SGRY", "quantity": 0},
        {"line_id": 8, "parent_id": 5, "type": "Color", "sku": "", "description": "Sport Grey", "quantity": 0},
        {"line_id": 9, "parent_id": 8, "type": "Size", "sku": "", "description": "M", "quantity": 2},
        {"line_id": 10, "parent_id": 0, "type": "Comment", "sku": "", "description": "ON DEMAND FROM STAHLS", "quantity": 0},
        {"line_id": 11, "parent_id": None, "type": "Comment", "sku": "BP20046", "description": "Bottle", "quantity": 0},
        {"line_id": 12, "parent_id": 10, "type": "Color", "sku": "", "description": "Clear", "quantity": 1},
    ]
    # The Stahls group has no SKU line, so it isn't an inventory item and is left out.
    assert dict(mapping.order_lines(po(line_items=items), "INV|OD")) == {"WWD119OD-BLACK": 1, "WWD265OD-M-SGRY": 2}


def test_product_line_with_its_own_sizes_needs_one_variant_per_sku():
    items = [
        {"line_id": 1, "parent_id": 0, "type": "Comment", "sku": "EB200", "description": "Fleece Jacket", "quantity": 0},
        {"line_id": 2, "parent_id": None, "type": "Comment", "sku": "", "description": "SKU: WDT511OD-3", "quantity": 0},
        {"line_id": 3, "parent_id": 1, "type": "Color", "sku": "", "description": "Grey Steel", "quantity": 0},
        {"line_id": 4, "parent_id": 3, "type": "Size", "sku": "", "description": "M", "quantity": 1},
        {"line_id": 5, "parent_id": 3, "type": "Size", "sku": "", "description": "L", "quantity": 2},
        {"line_id": 6, "parent_id": None, "type": "Comment", "sku": "", "description": "Item Value : $79.99", "quantity": 0},
    ]
    import pytest
    with pytest.raises(mapping.LineMappingError, match="covers 2 colors/sizes"):
        mapping.order_lines(po(line_items=items), "INV|OD")
    items[4]["quantity"] = 0          # a single size: fine
    assert dict(mapping.order_lines(po(line_items=items), "INV|OD")) == {"WDT511OD-3": 1}


def test_allowed_skus_are_included_without_inv_or_od():
    from tplsync.mapping import order_lines
    po = {"line_items": [
        {"line_id": 1, "parent_id": 0, "sku": "CENT037", "quantity": 2},
        {"line_id": 2, "parent_id": 0, "sku": "boxnewhirekit1", "quantity": 1},
        {"line_id": 3, "parent_id": 0, "sku": "PLAIN-123", "quantity": 5},
        {"line_id": 4, "parent_id": 0, "sku": "SHIRT-INV", "quantity": 3},
    ]}
    assert dict(order_lines(po, "INV|OD")) == {"SHIRT-INV": 3}
    lines = order_lines(po, "INV|OD", frozenset({"cent037", "boxnewhirekit1"}))
    assert dict(lines) == {"CENT037": 2, "boxnewhirekit1": 1, "SHIRT-INV": 3}


def test_sku_lines_under_different_products_in_one_po():
    """PO 77828-3: an on-demand vest whose quantity is on the header's Navy/Large line, and an inventory polo
    whose quantity is on its own product line, each followed by its own SKU line."""
    from tplsync.mapping import order_lines
    po = {"line_items": [
        {"line_id": 1, "parent_id": 0, "type": "Comment", "sku": "", "quantity": 0, "description": "ON DEMAND FROM JUST INK"},
        {"line_id": 2, "parent_id": None, "type": "Comment", "sku": "TM18501", "quantity": 0,
         "description": "Men's Tyndall Poly Microfleece Vest"},
        {"line_id": 3, "parent_id": None, "type": "Comment", "sku": "", "quantity": 0, "description": "SKU: BAL513OD-3"},
        {"line_id": 4, "parent_id": 1, "type": "Color", "sku": "", "quantity": 0, "description": "Navy"},
        {"line_id": 5, "parent_id": 4, "type": "Size", "sku": "", "quantity": 1, "description": "Large"},
        {"line_id": 6, "parent_id": 0, "type": "Comment", "sku": "", "quantity": 0, "description": "INVENTORY\t"},
        {"line_id": 7, "parent_id": None, "type": "Comment", "sku": "ME0XK00E", "quantity": 1,
         "description": "Peter Millar Men's Performance Solid Jersey Polo -  LRG - Navy"},
        {"line_id": 8, "parent_id": None, "type": "Comment", "sku": "", "quantity": 0,
         "description": "SKU: BAL528INV-59838-24227"},
    ]}
    assert dict(order_lines(po, "INV|OD")) == {"BAL513OD-3": 1, "BAL528INV-59838-24227": 1}


# --- on-demand items (shapes taken from real Baker POs) -------------------------------------------

def _po(*rows):
    keys = ("line_id", "parent_id", "type", "sku", "quantity", "description")
    return {"line_items": [dict(zip(keys, r)) for r in rows]}


PO_77828_3 = _po(
    (1, 0, "Comment", "", 0, "ON DEMAND FROM JUST INK"),
    (2, None, "Comment", "TM18501", 0, "Men's Tyndall Poly Microfleece Vest"),
    (3, None, "Comment", "", 0, "SKU: BAL513OD-3"),
    (4, 1, "Color", "", 0, "Navy"),
    (5, 4, "Size", "", 1, "Large"),
    (6, 0, "Comment", "", 0, "INVENTORY"),
    (7, None, "Comment", "ME0XK00E", 1, "Peter Millar Men's Performance Solid Jersey Polo -  LRG - Navy"),
    (8, None, "Comment", "", 0, "SKU: BAL528INV-59838-24227"),
)


def test_on_demand_items_get_product_colour_and_size():
    from tplsync.mapping import on_demand_items, on_demand_note
    items = on_demand_items(PO_77828_3, "INV|OD")
    assert [i["sku"] for i in items] == ["BAL513OD-3"]                 # the INV polo stays an inventory item
    assert items[0]["product"] == "Men's Tyndall Poly Microfleece Vest" and items[0]["code"] == "TM18501"
    assert items[0]["variants"] == [("Navy / Large", 1.0)]
    assert on_demand_note(items, "ONDEMAND123") == ("ON DEMAND (sent as ONDEMAND123): Men's Tyndall Poly Microfleece "
                                                    "Vest (TM18501) - Navy / Large x1 [PO SKU BAL513OD-3]")


def test_on_demand_sku_lines_that_name_their_size():
    """77632-5: two sock sizes under one Printify heading, each with its own SKU line, Item Value notes between."""
    from tplsync.mapping import on_demand_items, order_lines
    po = _po(
        (1, 0, "Comment", "", 0, "ON DEMAND FROM Printify ****"),
        (2, None, "Comment", "SP50029 / Spoke Cust", 0, "Socks"),
        (3, 1, "Color", "", 0, "White"),
        (4, 3, "Size", "", 1, "Small"),
        (5, None, "Comment", "", 0, "SKU: WDT037OD-SMALL-WHITE"),
        (6, None, "Comment", "", 0, "Item Cost : $10.24"),
        (7, 3, "Size", "", 2, "Large"),
        (8, None, "Comment", "", 0, "SKU: WDT037OD-LARGE-WHITE"),
    )
    assert dict(order_lines(po, "INV|OD", on_demand=True)) == {"WDT037OD-SMALL-WHITE": 1, "WDT037OD-LARGE-WHITE": 2}
    items = on_demand_items(po, "INV|OD")
    assert [(i["product"], i["variants"]) for i in items] == [("Socks", [("White / Small", 1.0)]),
                                                              ("Socks", [("White / Large", 2.0)])]


def test_on_demand_quantity_on_the_line_after_the_sku():
    """77476-3: 'SKU: WWD109OD-BLACK' followed by a 'Black' line carrying the quantity."""
    from tplsync.mapping import on_demand_items, order_lines
    po = _po(
        (1, None, "Comment", "97030 / OrigAudio", 0, "ON DEMAND FROM OrigAudio ****"),
        (2, None, "Comment", "", 0, "Slater Tote Bag"),
        (3, None, "Comment", "", 0, "SKU: WWD109OD-BLACK"),
        (4, None, "Comment", "", 1, "Black"),
    )
    with pytest.raises(mapping.LineMappingError):
        order_lines(po, "INV|OD")                                       # the old reading couldn't place it
    assert dict(order_lines(po, "INV|OD", on_demand=True)) == {"WWD109OD-BLACK": 1}
    assert on_demand_items(po, "INV|OD")[0]["variants"] == [("Black", 1.0)]


def test_several_sizes_under_one_on_demand_sku_are_added_up():
    from tplsync.mapping import on_demand_items, order_lines
    po = _po(
        (1, 0, "Comment", "", 0, "<b>***ONDEMAND FROM JIT***</b>"),
        (2, None, "Comment", "", 0, "Vest"),
        (3, None, "Comment", "", 0, "SKU: ABC123OD-1"),
        (4, 1, "Color", "", 0, "Navy"),
        (5, 4, "Size", "", 2, "L"),
        (6, 4, "Size", "", 3, "XL"),
    )
    with pytest.raises(mapping.LineMappingError, match="covers 2 colors/sizes"):
        order_lines(po, "INV|OD")
    assert dict(order_lines(po, "INV|OD", on_demand=True)) == {"ABC123OD-1": 5}
    assert on_demand_items(po, "INV|OD")[0]["variants"] == [("Navy / L", 2.0), ("Navy / XL", 3.0)]


def test_what_counts_as_on_demand():
    from tplsync.mapping import is_on_demand
    items = [{"line_id": 1, "parent_id": 0, "description": "<b>***ON DEMAND FROM HPG***</b>"},
             {"line_id": 2, "parent_id": None, "description": "SKU: X"}]
    assert is_on_demand(items, 1, "WT072ODJ-45")                         # OD/ODJ after the style number
    assert is_on_demand(items, 1, "JI-NOTEBOOK")                         # under an ON DEMAND heading
    assert not is_on_demand(items, 1, "PHU563INV")                       # INV is always inventory
    assert not is_on_demand(items, 1, "WDT004OD-BLACK", exempt={"wdt004od-black"})   # an Allowed SKU
    plain = [{"line_id": 1, "parent_id": 0, "description": "INVENTORY"}, {"line_id": 2, "parent_id": None}]
    assert not is_on_demand(plain, 1, "JI-OD-7") and not is_on_demand(plain, 1, "GOODS-1")


def test_sku_typed_on_its_own_line_with_colour_and_size_as_text():
    """77993-1: every line is a plain comment; the OD SKU is a bare description, colour and size are typed."""
    from tplsync.mapping import on_demand_items, on_demand_note, order_lines, po_skus
    po = _po(
        (1, None, "Comment", "iprobox10x8x3", 2, "Mailing Box, B-Flute 10 x 8 x 3 - D - 240 sq in Full color"),
        (2, None, "Comment", "", 0, " "),
        (3, None, "Comment", "", 0, "ON DEMAND SKU:"),
        (4, None, "Comment", "", 1, "Eddie Bauer Tshirt"),
        (5, None, "Comment", "", 0, "IPROX123OD"),
        (6, None, "Comment", "", 0, "Color: White"),
        (7, None, "Comment", "", 0, "Size: XL"),
    )
    assert po_skus(po) == ["iprobox10x8x3", "IPROX123OD"]
    assert dict(order_lines(po, "INV|OD")) == {"IPROX123OD": 1}
    assert on_demand_note(on_demand_items(po, "INV|OD"), "ONDEMAND123") == \
        "ON DEMAND (sent as ONDEMAND123): Eddie Bauer Tshirt - White / XL x1 [PO SKU IPROX123OD]"


def test_inventory_sku_typed_under_each_product():
    """77607-1 (Allvue): product line with the quantity, then its INV SKU as a bare description."""
    from tplsync.mapping import order_lines
    po = _po(
        (1, 0, "Comment", "TM23804 / Snugz USA", 1, "Destin 16oz Double Wall Tumbler w Straw - Black"),
        (2, None, "Comment", "", 0, "ALLVUE0031INV-47741"),
        (3, 0, "Comment", "PED-676L / Journal Books", 2, "Pedova Journal - Large - Black"),
        (4, None, "Comment", "", 0, "ALLVUE0032INV-47741"),
    )
    assert dict(order_lines(po, "INV|OD")) == {"ALLVUE0031INV-47741": 1, "ALLVUE0032INV-47741": 2}


def test_words_that_are_not_skus():
    from tplsync.mapping import tagged_sku
    assert tagged_sku({"description": "INVENTORY"}) is None                      # a heading, no digits
    assert tagged_sku({"description": "Check in under SKU:BKM522INV-56538"}) is None   # receiving note
    assert tagged_sku({"description": "ABC123INV", "quantity": 4}) is None       # a product line with a quantity
    assert tagged_sku({"description": "ABC123INV", "sku": "X1"}) is None         # has its own SKU field
    assert tagged_sku({"description": "Size: XL"}) is None and tagged_sku({"description": "2XL"}) is None
    assert tagged_sku({"description": " SKU: ABC123OD-3 "}) == "ABC123OD-3"
