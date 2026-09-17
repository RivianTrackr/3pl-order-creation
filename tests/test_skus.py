import pytest

from tplsync import skus

# The real shape of one client's items in 3PL Central: a numeric variant code per size.
ITEMS = [
    {"sku": "LBC002INV", "description": "Ultra-Slim Power Bank Charger - UL Certified - Silver"},
    {"sku": "LBC006INVJ-15565", "description": "Bella+Canvas Unisex Jersey Long-Sleeve T-Shirt - Heather Forest - XS"},
    {"sku": "LBC006INVJ-15567", "description": "Bella+Canvas Unisex Jersey Long-Sleeve T-Shirt - Heather Forest - M"},
    {"sku": "LBC006INVJ-15570", "description": "Bella+Canvas Unisex Jersey Long-Sleeve T-Shirt - Heather Forest - XXL"},
]


@pytest.mark.parametrize("po_sku, expected", [
    ("LBC002INV", "LBC002INV"),              # exact match wins
    ("lbc002inv", "LBC002INV"),              # case doesn't matter
    ("LBC006INVJ-2XL", "LBC006INVJ-15570"),  # 2XL is XXL
    ("LBC006INVJ-XXL", "LBC006INVJ-15570"),
    ("LBC006INVJ-M", "LBC006INVJ-15567"),
    ("LBC006INVJ-Medium", "LBC006INVJ-15567"),
])
def test_po_skus_match_the_right_size(po_sku, expected):
    assert skus.resolve(po_sku, ITEMS, "Example Books") == expected


def test_missing_size_is_reported_with_the_sizes_that_exist():
    with pytest.raises(skus.SkuMatchError, match="none in size L"):
        skus.resolve("LBC006INVJ-L", ITEMS, "Example Books")
    try:
        skus.resolve("LBC006INVJ-L", ITEMS, "Example Books")
    except skus.SkuMatchError as exc:
        assert "sizes there: M, XS, XXL" in str(exc)


def test_unknown_sku_is_reported():
    with pytest.raises(skus.SkuMatchError, match="not an item for Example Books"):
        skus.resolve("NOSUCH-INV", ITEMS, "Example Books")
    with pytest.raises(skus.SkuMatchError, match="neither is any OTHER- variant"):
        skus.resolve("OTHER-2XL", ITEMS, "Example Books")


def test_ambiguous_match_is_refused():
    items = ITEMS + [{"sku": "LBC006INVJ-15599",
                      "description": "Bella+Canvas Unisex Jersey Long-Sleeve T-Shirt - Heather Forest - XXL"}]
    with pytest.raises(skus.SkuMatchError, match="matches more than one"):
        skus.resolve("LBC006INVJ-2XL", items, "Example Books")


def test_resolve_lines_keeps_quantities_and_reports_renames():
    lines, renames = skus.resolve_lines({"LBC006INVJ-2XL": 3, "LBC002INV": 1}, ITEMS, "Example Books")
    assert lines == {"LBC006INVJ-15570": 3, "LBC002INV": 1}
    assert renames == {"LBC006INVJ-2XL": "LBC006INVJ-15570"}


def test_resolve_lines_reports_every_problem_sku():
    with pytest.raises(skus.SkuMatchError) as exc:
        skus.resolve_lines({"LBC006INVJ-L": 1, "NOSUCH-INV": 2}, ITEMS, "Example Books")
    assert "LBC006INVJ-L" in str(exc.value) and "NOSUCH-INV" in str(exc.value)
