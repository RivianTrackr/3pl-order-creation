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

