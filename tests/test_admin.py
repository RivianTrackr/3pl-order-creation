import re

import pytest
from fastapi.testclient import TestClient

from tplsync.admin.app import create_app
from tplsync.admin.auth import hash_password
from tplsync.config import Bootstrap, ConfigError, load_settings
from tplsync.crypto import generate_key
from tplsync.db import Database

PASSWORD = "correct horse battery"


@pytest.fixture
def boot(tmp_path):
    b = Bootstrap(db_path=str(tmp_path / "admin.db"), encryption_key=generate_key(),
                  admin_secret_key="test-secret-key-" * 3, admin_host="127.0.0.1", admin_port=0,
                  cookie_secure=False)
    db = Database(b.db_path)
    db.create_user("admin", hash_password(PASSWORD))
    db.close()
    return b


@pytest.fixture
def client(boot):
    return TestClient(create_app(boot))


def login(client, password=PASSWORD):
    return client.post("/login", data={"username": "admin", "password": password}, follow_redirects=False)


def csrf(client, path="/settings"):
    return re.search(r'name="_csrf" value="([^"]+)"', client.get(path).text).group(1)


def test_requires_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    assert client.get("/login").status_code == 200


def test_login_and_logout(client):
    assert login(client, "wrong password!!").status_code == 401
    assert login(client).status_code == 303
    assert client.get("/").status_code == 200
    token = csrf(client, "/")
    client.post("/logout", data={"_csrf": token})
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_is_throttled(client):
    for _ in range(5):
        login(client, "nope nope nope")
    assert login(client).status_code == 429


def test_post_without_csrf_is_rejected(client):
    login(client)
    resp = client.post("/settings", data={"SYNCORE_VENDOR_NAME": "Evil"})
    assert resp.status_code == 403


def test_settings_secret_is_encrypted_and_kept_when_blank(client, boot):
    login(client)
    token = csrf(client)
    form = {"_csrf": token, "SYNCORE_API_KEY": "syn-key-123", "SYNCORE_VENDOR_NAME": "Example Supplier LLC",
            "TPL_USER_LOGIN": "apiuser", "TPL_FACILITY_NAME": "Example Warehouse",
            "SENDGRID_API_KEY": "SG.x", "EMAIL_FROM": "ops@example.com",
            "ALERT_EMAIL_TO": "alerts@example.com", "PO_SETTLE_MINUTES": "10", "DRY_RUN": "on"}
    client.post("/settings", data=form)
    db = Database(boot.db_path)
    raw = db.settings_raw()
    assert raw["SYNCORE_API_KEY"] and "syn-key-123" not in raw["SYNCORE_API_KEY"]
    assert "syn-key-123" not in client.get("/settings").text

    # Saving again with the key blank keeps it; unchecking the box turns dry run off.
    client.post("/settings", data={**form, "SYNCORE_API_KEY": "", "SENDGRID_API_KEY": "", "DRY_RUN": None})
    settings = load_settings(db, boot.secret_box(), boot.db_path)
    assert settings.syncore_api_key == "syn-key-123"
    assert settings.settle_minutes == 10 and settings.dry_run is False
    assert settings.vendor_name == "Example Supplier LLC"


def test_a_job_status_cant_be_both_sent_and_ignored(client, boot):
    login(client)
    resp = client.post("/settings", data={"_csrf": csrf(client), "SYNCORE_JOB_STATUSES": "Submitted, Delivered",
                                          "SYNCORE_JOB_STATUSES_IGNORE": "Delivered, Completed"})
    assert "be both sent and ignored" in resp.text and "Delivered" in resp.text
    assert "SYNCORE_JOB_STATUSES_IGNORE" not in Database(boot.db_path).settings_raw()


def test_email_toggles_on_the_settings_page(client, boot):
    login(client)
    db = Database(boot.db_path)
    defaults = load_settings(db, boot.secret_box(), boot.db_path, require_complete=False).emails
    assert defaults["short_stock"] and defaults["errors"] and not defaults["client_paused"]
    page = client.get("/settings").text
    assert "PO for a paused client" in page and "Order left Open for short stock" in page
    client.post("/settings", data={"_csrf": csrf(client), "EMAIL_SHORT_STOCK": "on", "EMAIL_CLIENT_PAUSED": "on"})
    emails = load_settings(db, boot.secret_box(), boot.db_path, require_complete=False).emails
    assert emails["short_stock"] and emails["client_paused"] and not emails["errors"]


def test_settings_validation(client, boot):
    login(client)
    resp = client.post("/settings", data={"_csrf": csrf(client), "SKU_PATTERN": "INV(", "PO_SETTLE_MINUTES": "abc"})
    assert "not a valid regular expression" in resp.text and "whole number" in resp.text
    assert "SKU_PATTERN" not in Database(boot.db_path).settings_raw()


def test_incomplete_settings_raise(boot):
    db = Database(boot.db_path)
    with pytest.raises(ConfigError):
        load_settings(db, boot.secret_box(), boot.db_path)


def test_client_crud_keeps_secret(client, boot):
    login(client)
    token = csrf(client, "/clients/new")
    data = {"_csrf": token, "name": "Acme", "syncore_group_id": "1213", "customer_id": "77",
            "client_id": "cid-1", "client_secret": "s3cret", "active": "on"}
    resp = client.post("/clients/save", data=data, follow_redirects=False)
    assert resp.status_code == 303
    client_pk = int(resp.headers["location"].rsplit("/", 1)[1])
    assert "s3cret" not in client.get(f"/clients/{client_pk}").text

    client.post("/clients/save", data={**data, "id": client_pk, "client_secret": "", "name": "Acme Corp"})
    db = Database(boot.db_path)
    row = db.get_client(client_pk)
    assert row["name"] == "Acme Corp" and boot.secret_box().decrypt(row["client_secret_enc"]) == "s3cret"
    assert row["active"] == 1

    dup = client.post("/clients/save", data={**data, "client_id": "other", "customer_id": "78"})
    assert "Acme Corp already uses Syncore client group 1213" in dup.text
    assert len(db.list_clients()) == 1

    client.post(f"/clients/{client_pk}/delete", data={"_csrf": token})
    assert db.get_client(client_pk) is None


def test_clients_can_be_listed_before_setup_and_show_whats_missing(client, boot):
    login(client)
    token = csrf(client, "/clients")
    client.post("/clients/add-names", data={"_csrf": token, "names": "Example Book Company\nNorthwind Tools\n\nnorthwind tools"})
    db = Database(boot.db_path)
    assert sorted(r["name"] for r in db.list_clients()) == ["Example Book Company", "Northwind Tools"]

    page = client.get("/clients").text
    assert "Needs info" in page and "Missing: Syncore client group, 3PL Customer ID, Client ID, Client Secret" in page

    # 3PL Central sync fills in customer ids by name and adds new customers
    assert db.sync_tpl_customers([(500, "Example Book Company"), (70, "Vertex Labs")]) == \
        {"added": 1, "linked": 1, "updated": 0}
    # A Syncore PO links its client by name
    db.link_syncore_group("1000001", "Example Book Company, Inc.")
    example_client = next(r for r in db.list_clients() if r["name"] == "Example Book Company")
    assert (example_client["customer_id"], example_client["syncore_group_id"]) == (500, "1000001")

    # Active can't be turned on until everything is filled in
    client.post("/clients/save", data={"_csrf": token, "id": example_client["id"], "name": "Example Book Company",
                                       "syncore_group_id": "1000001", "customer_id": "500", "client_id": "abc",
                                       "active": "on"})
    assert db.get_client(example_client["id"])["active"] == 0
    client.post("/clients/save", data={"_csrf": token, "id": example_client["id"], "name": "Example Book Company",
                                       "syncore_group_id": "1000001", "customer_id": "500", "client_id": "abc",
                                       "client_secret": "shh", "active": "on"})
    assert db.get_client(example_client["id"])["active"] == 1
    assert "Active" in client.get("/clients?show=ready").text


def test_saving_a_syncore_id_merges_the_auto_added_placeholder(client, boot):
    login(client)
    token = csrf(client, "/clients")
    db = Database(boot.db_path)
    db.add_client_names(["Northwind"])
    placeholder = db.link_syncore_group("4444", "Riverside Promo LLC Store")   # name doesn't match: new row
    northwind = next(r for r in db.list_clients() if r["name"] == "Northwind")
    client.post("/clients/save", data={"_csrf": token, "id": northwind["id"], "name": "Northwind",
                                       "syncore_group_id": "4444"})
    rows = db.list_clients()
    assert [r["name"] for r in rows] == ["Northwind"] and rows[0]["syncore_group_name"] == "Riverside Promo LLC Store"
    assert db.get_client(placeholder["id"]) is None


def test_ship_via_overrides_use_the_carrier_list(client, boot):
    import json
    from tplsync import carriers as cm
    login(client)
    token = csrf(client, "/shipping")
    # No carrier list yet: overrides can't be saved
    resp = client.post("/shipping/save", data={"_csrf": token, "ship_via": "USPS Priority", "carrier": "USPS", "mode": "92"})
    assert "Load the carrier list" in resp.text

    db = Database(boot.db_path)
    carriers = cm.parse_carrier_list({"_embedded": {"http://api.3plCentral.com/rels/properties/carrier": [
        {"name": "USPS", "shipmentServices": [{"code": "92", "description": "Priority Mail"},
                                              {"code": "93", "description": "Priority Mail Express"}]}]}})
    db.set_meta("tpl_carriers", json.dumps({"fetched_at": "2026-09-17T16:00:00", "carriers": cm.carriers_to_json(carriers)}))

    page = client.get("/shipping?check=USPS+Priority+Mail").text
    assert "Priority Mail" in page and "<code>92</code>" in page
    assert "isn&#39;t one of the USPS services" in client.get("/shipping?check=USPS+Priority").text

    bad = client.post("/shipping/save", data={"_csrf": token, "ship_via": "USPS Priority", "carrier": "USPS", "mode": "999"})
    assert "Choose the Ship Via text" in bad.text
    client.post("/shipping/save", data={"_csrf": token, "ship_via": " USPS  Priority ", "carrier": "USPS", "mode": "92"})
    rules = db.list_shipping_rules()
    assert [(r["ship_via"], r["carrier"], r["mode"]) for r in rules] == [("USPS Priority", "USPS", "92")]
    assert "from an override" in client.get("/shipping?check=usps+priority").text


def test_password_change_signs_out_everywhere(client, boot):
    login(client)
    other = TestClient(create_app(boot))
    login(other)
    token = csrf(client, "/users")
    new = "an even better passphrase"
    client.post("/account/password", data={"_csrf": token, "current_password": PASSWORD,
                                           "new_password": new, "confirm_password": new})
    assert other.get("/", follow_redirects=False).status_code == 303
    assert login(client, new).status_code == 303


def test_security_headers(client):
    resp = client.get("/login")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


def test_syncore_group_matching_links_names_and_suggests_close_ones(boot):
    from tplsync import clientmatch
    db = Database(boot.db_path)
    db.add_client_names(["Example Book Company", "Acme Supply", "Park Quick", "Bridge Point", "Arcadia"])
    db.link_syncore_group("555", "Arcadia")        # already linked: left alone
    groups = [
        {"id": 5001, "name": "Example Book Company"},
        {"id": 24903, "name": "Acme Supply"},
        {"id": 31000, "name": "ParkQuick Store"},
        {"id": 31001, "name": "ParkQuick"},
        {"id": 555, "name": "Arcadia"},
        {"id": 9, "name": "Someone Else"},
    ]
    counts = clientmatch.match_syncore_groups(db, groups)
    assert counts == {"groups": 6, "matched": 3, "suggested": 0, "unmatched": 1}
    by_name = {r["name"]: r for r in db.list_clients()}
    assert by_name["Example Book Company"]["syncore_group_id"] == "5001"
    assert by_name["Acme Supply"]["syncore_group_name"] == "Acme Supply"
    assert by_name["Park Quick"]["syncore_group_id"] == "31001"

    db.add_client_names(["Summit Care"])
    counts = clientmatch.match_syncore_groups(db, groups + [{"id": 40, "name": "Summit Care Store"},
                                                            {"id": 41, "name": "Summit Care - Events"}])
    ta = next(r for r in db.list_clients() if r["name"] == "Summit Care")
    assert ta["syncore_group_id"] is None
    assert [s["business_name"] for s in db.suggestions(ta["id"])] == ["Summit Care Store", "Summit Care - Events"]

    client = TestClient(create_app(boot))
    login(client)
    page = client.get(f"/clients/{ta['id']}").text
    assert "Is this the Syncore client group?" in page and '<option value="5001"' in page
    token = csrf(client, f"/clients/{ta['id']}")
    client.post(f"/clients/{ta['id']}/use-syncore", data={"_csrf": token, "syncore_group_id": "40"})
    assert db.get_client(ta["id"])["syncore_group_name"] == "Summit Care Store"
    assert db.suggestions(ta["id"]) == []


def test_customer_id_is_looked_up_from_the_login(client, boot, monkeypatch):
    from tplsync import clientmatch
    login(client)
    db = Database(boot.db_path)
    db.set_setting("TPL_USER_LOGIN", "apiuser")
    seen = {}

    def fake_lookup(base_url, client_id, client_secret, user_login, facility_id=None):
        seen.update(client_id=client_id, secret=client_secret, login=user_login)
        return [(500, "Example Book Company")]

    monkeypatch.setattr(clientmatch, "lookup_customer", fake_lookup)
    token = csrf(client, "/clients/new")
    resp = client.post("/clients/save", data={"_csrf": token, "name": "Example Book Company",
                                              "syncore_group_id": "5001", "client_id": "cid", "client_secret": "sec",
                                              "active": "on"})
    assert "Found 3PL Customer ID 500" in resp.text
    [row] = db.list_clients()
    assert (row["customer_id"], row["tpl_customer_name"], row["active"]) == (500, "Example Book Company", 1)
    assert seen == {"client_id": "cid", "secret": "sec", "login": "apiuser"}


def test_dismissed_po_is_hidden_and_never_processed(client, boot):
    login(client)
    db = Database(boot.db_path)
    db.upsert(501, 7001, state="error", reference="7001-1", last_error="bad data", attempts=3)
    db.upsert(502, 7002, state="done", reference="7002-1", order_id=9, completion="completed")
    token = csrf(client, "/")
    client.post("/po/501/dismiss", data={"_csrf": token})
    assert db.get(501)["state"] == "dismissed"
    page = client.get("/").text
    assert "7002-1" in page and "7001-1" not in page
    assert "7001-1" in client.get("/?state=dismissed").text
    client.post("/po/501/retry", data={"_csrf": token})          # Restore
    assert db.get(501)["state"] == "error"


def test_override_shorthand_drops_a_typed_account_number(client, boot):
    import json
    from tplsync import carriers as cm
    login(client)
    db = Database(boot.db_path)
    carriers = cm.parse_carrier_list({"_embedded": {"http://api.3plCentral.com/rels/properties/carrier": [
        {"name": "UPS", "shipmentServices": [{"code": "03", "description": "UPS Ground"}]}]}})
    db.set_meta("tpl_carriers", json.dumps({"fetched_at": "2026-09-22T16:00:00", "carriers": cm.carriers_to_json(carriers)}))
    token = csrf(client, "/shipping")
    resp = client.post("/shipping/save", data={"_csrf": token, "ship_via": "UPS GRND C713X7", "carrier": "UPS", "mode": "03"})
    assert "C713X7 was left off the shorthand" in resp.text
    assert [r["ship_via"] for r in db.list_shipping_rules()] == ["UPS GRND"]
    page = client.get("/shipping?check=UPS+GRND+9W2Y44").text
    assert "account <code>9W2Y44</code>" in page and 'value="UPS GRND"' in page


def test_override_keeps_a_service_name_that_ends_in_a_digit(client, boot):
    import json
    from tplsync import carriers as cm
    login(client)
    db = Database(boot.db_path)
    carriers = cm.parse_carrier_list({"_embedded": {"http://api.3plCentral.com/rels/properties/carrier": [
        {"name": "UPS", "shipmentServices": [{"code": "03", "description": "UPS Ground"},
                                             {"code": "12", "description": "UPS 3 Day Select"}]}]}})
    db.set_meta("tpl_carriers", json.dumps({"fetched_at": "2026-09-22T16:00:00", "carriers": cm.carriers_to_json(carriers)}))
    token = csrf(client, "/shipping")
    resp = client.post("/shipping/save", data={"_csrf": token, "ship_via": "UPS 3DAY", "carrier": "UPS", "mode": "12"})
    assert "left off the shorthand" not in resp.text
    assert [r["ship_via"] for r in db.list_shipping_rules()] == ["UPS 3DAY"]
    page = client.get("/shipping?check=UPS+3DAY+C713X7").text
    assert "UPS 3 Day Select" in page and "account <code>C713X7</code>" in page


def test_allowed_skus_import_add_and_remove(client, boot, tmp_path):
    import io
    from openpyxl import Workbook
    login(client)
    db = Database(boot.db_path)
    centurion = db.save_client(None, name="Centurion", source="manual")
    db.save_client(None, name="The Baldwin Group", source="manual")

    wb = Workbook()
    ws = wb.active
    ws.append(["3PL Client Name", "Product SKU #", "Product Description"])
    ws.append(["Centurion", "CENT037", "14 Grande Wall Clock - Black"])
    ws.append(["Centurion", "CENT037", "14 Grande Wall Clock - Black"])   # duplicate row
    ws.append(["The Baldwin Group/TBG", "boxnewhirekit1", "Box for New Hire Kit"])
    ws.append(["Harbor Global", "HARBOX", "Kits ship in a box"])
    buf = io.BytesIO()
    wb.save(buf)
    token = csrf(client, "/skus")
    resp = client.post("/skus/import", data={"_csrf": token},
                       files={"file": ("inventory_skus.xlsx", buf.getvalue(),
                                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert "2 SKU(s) added, 1 already listed" in resp.text and "Harbor Global (1)" in resp.text
    assert sorted(r["sku"] for r in db.list_allowed_skus()) == ["CENT037", "boxnewhirekit1"]
    assert db.allowed_skus_by_client()[centurion] == {"cent037"}

    client.post("/skus/add", data={"_csrf": token, "client_pk": centurion, "sku": "CENT058", "description": "Yeti"})
    page = client.get(f"/skus?client={centurion}").text
    assert "CENT058" in page and "boxnewhirekit1" not in page
    everything = client.get("/skus?client=")            # the "All clients" choice
    assert everything.status_code == 200 and "CENT058" in everything.text and "boxnewhirekit1" in everything.text

    sku_id = next(r["id"] for r in db.list_allowed_skus() if r["sku"] == "CENT058")
    client.post(f"/skus/{sku_id}/delete", data={"_csrf": token})
    assert "CENT058" not in [r["sku"] for r in db.list_allowed_skus()]


def test_allowed_skus_csv_and_bad_files(client, boot):
    login(client)
    db = Database(boot.db_path)
    db.save_client(None, name="Spot Hero", source="manual")
    token = csrf(client, "/skus")
    csv_body = "Client,SKU\nSpot Hero,SPHKIT005\n".encode()
    resp = client.post("/skus/import", data={"_csrf": token}, files={"file": ("list.csv", csv_body, "text/csv")})
    assert "1 SKU(s) added" in resp.text
    resp = client.post("/skus/import", data={"_csrf": token}, files={"file": ("list.pdf", b"%PDF", "application/pdf")})
    assert "Upload an .xlsx or .csv file" in resp.text


def test_dashboard_shows_client_and_searches(client, boot):
    login(client)
    db = Database(boot.db_path)
    db.save_client(None, name="Centurion", syncore_group_id="20011", source="manual")
    db.upsert(501, 77673, state="error", reference="77673-1", client_id="20011", client_name="Centurion Group",
              last_error="CENT017INV-27132 is not an item", attempts=3)
    db.upsert(502, 77700, state="held", reference="77700-1", client_name="Spot Hero",
              skip_reason="Syncore job 77700 is WIP")
    page = client.get("/").text
    assert "Centurion" in page and "Spot Hero" in page and "On hold" in page
    found = client.get("/?q=centurion").text
    assert "77673-1" in found and "77700-1" not in found and "1 result" in found
    assert "77700-1" in client.get("/?q=77700").text
    po_page = client.get("/po/501").text
    assert "Centurion" in po_page and "Dismiss" in po_page and "Retry" in po_page


def test_changed_after_send_shows_and_can_be_marked_reviewed(client, boot):
    login(client)
    db = Database(boot.db_path)
    db.upsert(601, 77900, state="done", reference="77900-1", order_id=202000, completion="completed",
              changed_at="2026-10-19T15:00:00", change_summary="SHIRT-INV quantity 10 -> 12", change_signature="x")
    assert "Changed in Syncore" in client.get("/").text            # badge on the dashboard
    assert "77900-1" in client.get("/?state=changed").text
    page = client.get("/po/601").text
    assert "SHIRT-INV quantity 10 -&gt; 12" in page and "Mark reviewed" in page
    client.post("/po/601/reviewed", data={"_csrf": csrf(client, "/po/601")})
    assert db.get(601)["changed_at"] is None and db.get(601)["change_signature"] == "x"
    assert "77900-1" not in client.get("/?state=changed").text


def test_shipped_po_shows_a_tracking_link(client, boot):
    login(client)
    db = Database(boot.db_path)
    db.upsert(602, 77901, state="done", reference="77901-1", order_id=202001, completion="completed",
              order_status="shipped", tracking_number="1Z0WY6070307889570", ship_carrier="UPS",
              shipped_at="2026-10-19T15:00:00")
    page = client.get("/").text
    assert "Shipped" in page and 'href="https://www.ups.com/track?tracknum=1Z0WY6070307889570"' in page
    assert "1Z0WY6070307889570" in client.get("/po/602").text


def _carriers(db):
    import json
    from datetime import datetime, timezone
    from tplsync import carriers as cm
    carriers = cm.parse_carrier_list({"_embedded": {"http://api.3plCentral.com/rels/properties/carrier": [
        {"name": "UPS", "shipmentServices": [{"code": "03", "description": "UPS Ground"}]}]}})
    db.set_meta("tpl_carriers", json.dumps({"fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                                            "carriers": cm.carriers_to_json(carriers)}))


def test_unmatched_ship_via_is_listed_with_a_fix(client, boot):
    login(client)
    db = Database(boot.db_path)
    _carriers(db)
    db.add_event(None, 701, 77950, "77950-1", "error", "shipping.unmatched", "no match",
                 {"ship_via": "UPS GRD C713X7", "overrides": []})
    page = client.get("/shipping").text
    assert "Ship Via that didn" in page and "UPS GRD C713X7" in page and "Create override for “UPS GRD”" in page
    db.save_shipping_rule(None, ship_via="UPS GRD", carrier="UPS", mode="03", scac_code=None, account=None,
                          billing_code=None)
    assert "Ship Via that didn" not in client.get("/shipping").text      # fixed: no longer listed


def test_sku_from_a_skipped_po_can_be_allowed_in_one_click(client, boot):
    login(client)
    db = Database(boot.db_path)
    centurion = db.save_client(None, name="Centurion", syncore_group_id="20011", source="manual")
    db.upsert(702, 77951, state="skipped", reference="77951-1", client_id="20011", client_name="Centurion",
              skip_reason="no SKUs match 'INV|OD' or the Allowed SKUs list")
    db.add_event(None, 702, 77951, "77951-1", "info", "po.skipped", "Skipped: no SKUs match",
                 {"skus_on_po": ["CENT037", "TM18501"]})
    page = client.get("/skus").text
    assert "Skipped because no SKU matched" in page and "CENT037" in page and "TM18501" in page
    client.post("/skus/allow-from-po", data={"_csrf": csrf(client, "/skus"), "po_id": 702, "sku": "CENT037"})
    assert db.allowed_skus_by_client()[centurion] == {"cent037"}
    assert db.get(702)["state"] == "error"                                 # will be processed next run
