from tplsync.admin import explain


def _event(event, message, level="error", **data):
    return {"event": event, "level": level, "message": message, "data_obj": data}


def test_po_reading_error_says_to_fix_the_po_in_syncore():
    x = explain.explain(_event("po.error", "Attempt 2 failed (will retry): Found SKU X-INV but no product line with "
                                           "a quantity above it.", error_type="LineMappingError", attempt=2))
    assert x["title"] == "Couldn't read the items on the PO" and x["fixer"] == "syncore"
    assert x["what"].startswith("Found SKU X-INV")                 # the "Attempt 2 failed" prefix is gone
    assert x["retry"] == "Retrying automatically (attempt 2 of 20)" and x["category"] == "po_lines"


def test_outage_is_plain_and_fixes_itself():
    x = explain.explain(_event("po.error", "Attempt 1 failed (will retry): 3PL Central POST https://x/orders -> "
                                           "HTTP 503: <html>busy</html>", error_type="ApiError",
                               service="3PL Central", http_status=503, attempt=1))
    assert x["title"] == "3PL Central didn't respond" and x["fixer"] == "auto" and "https" not in x["what"]


def test_rejected_sign_in_goes_to_support():
    x = explain.explain(_event("po.error", "Attempt 1 failed (will retry): Syncore GET https://x -> HTTP 401: no",
                               error_type="ApiError", service="Syncore", http_status=401))
    assert x["fixer"] == "support" and x["category"] == "rejected"


def test_given_up_item_error_says_to_retry_by_hand():
    x = explain.explain(_event("po.failed", "Gave up after 20 attempts: A-INV is not an item for Acme in 3PL "
                                            "Central. No order was created. Add the item in 3PL Central, or fix the "
                                            "SKU on the Syncore PO.", error_type="RuntimeError", attempt=20))
    assert x["title"] == "An item isn't set up in 3PL Central" and x["fixer"] == "tpl"
    assert x["what"] == "A-INV is not an item for Acme in 3PL Central."
    assert "on its own" not in x["next"] and x["next"].endswith("click Retry on the PO's page.")
    assert x["retry"] == "Stopped retrying after 20 attempts"


def test_shipping_setup_problem_in_settings_goes_to_support():
    x = explain.explain(_event("po.error", "Attempt 1 failed (will retry): The PO has no Ship Via and no default "
                                           "carrier is set in Settings.", error_type="CarrierMatchError"))
    assert x["category"] == "shipping" and x["fixer"] == "support"


def test_unknown_error_is_a_bug_for_support():
    x = explain.explain(_event("po.error", "Attempt 1 failed (will retry): KeyError 'x'", error_type="KeyError"))
    assert x["category"] == "other" and x["fixer"] == "support"


def test_info_events_get_plain_titles_and_hints():
    held = explain.explain(_event("po.held", "On hold: Syncore job 1 is WIP; it's sent once the job is Submitted",
                                  level="info"))
    assert held["title"] == "On hold until the job is submitted" and held["next"].startswith("Nothing to do")
    skipped = explain.explain(_event("po.skipped", "Skipped: client Acme isn't set up for 3PL Central (missing x)",
                                     level="info"))
    assert skipped["what"].startswith("Client Acme") and "Process" in skipped["next"]
    assert explain.explain(_event("order.created", "Created 3PL order 5", level="info"))["title"] == \
        "Order created in 3PL Central"


def test_resolved_follows_the_po_status_now():
    error = _event("po.error", "Attempt 1 failed (will retry): x")
    assert explain.resolved(error, {"state": "done", "completion": "completed", "changed_at": None}) is True
    assert explain.resolved(error, {"state": "error", "completion": None, "changed_at": None}) is False
    short = _event("order.left_open", "left Open", level="warning")
    assert explain.resolved(short, {"state": "done", "completion": "open_short", "changed_at": None}) is False
    assert explain.resolved(error, None) is None
