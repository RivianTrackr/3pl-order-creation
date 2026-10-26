import json
from datetime import datetime, timedelta, timezone

import pytest

from tplsync.config import Settings, TplClient
from tplsync.db import Database
from tplsync.http import ApiError
from tplsync.processor import MAX_ATTEMPTS, Processor

START = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
CARRIER_LIST = {
    "defaultBillingCodes": [{"code": "Prepaid"}, {"code": "FreightCollect"}, {"code": "BillThirdParty"}],
    "defaultShipmentServices": [{"code": "04", "description": "Ground"}],
    "_embedded": {"http://api.3plCentral.com/rels/properties/carrier": [
        {"name": "UPS", "description": "United Parcel Service", "scacCode": "UPSN", "deactivated": False,
         "shipmentServices": [{"code": "03", "description": "UPS Ground"}, {"code": "02", "description": "UPS 2nd Day Air"}],
         "billingCodes": []},
    ]},
}
CLIENT = TplClient(name="Client Inc", customer_id=77, client_id="cid", client_secret="secret")


def make_po(po_id=900, supplier="Example Supplier LLC", sku="SHIRT-INV", qty=10, comments=""):
    return {
        "id": po_id, "number": 3, "job_number": 12345, "last_modified_date": "2026-09-17 12:05:00",
        "supplier": {"name": supplier}, "critical_comments": comments, "ship_via": "UPS Ground",
        "ship_to": {"business_name": "Acme", "name": "Pat", "address1": "1 Main", "city": "Austin",
                    "state": "TX", "zip": "78701", "country": "US"},
        "line_items": [{"line_id": 1, "parent_id": 0, "sku": sku, "quantity": qty}],
    }


class FakeSyncore:
    def __init__(self, pos, client_id=1213, job_status="Submitted"):
        self.pos = {p["id"]: p for p in pos}
        self.client_id = client_id
        self.job_status = job_status

    def search_purchase_orders(self, created_from, modified_from):
        return [{"id": p["id"], "job_number": p["job_number"], "last_modified_date": p["last_modified_date"]}
                for p in self.pos.values()]

    def get_purchase_order(self, job_id, po_id, fresh=False):
        return self.pos.get(po_id)

    def get_job(self, job_id):
        return {"id": job_id, "status": self.job_status,
                "client": {"id": self.client_id, "business_name": "Client Inc", "name": "Pat Buyer"}}

    def get_contact(self, contact_id):
        # Every contact at the company shares client group 5001; 4444 is in an unconfigured group.
        group = {"id": 5001, "name": "Client Inc"} if str(contact_id) != "4444" else {"id": 777, "name": "Northwind"}
        return {"id": int(contact_id), "business_name": group["name"], "client_group": group}

    def list_client_groups(self):
        return []



class FakeTpl:
    def __init__(self, available=100):
        self.available = available
        self.created = []
        self.completed = []
        self.existing = {}
        self.fail_create = None
        self.fail_complete = False
        self.items = [{"sku": "SHIRT-INV", "description": "Tee - Navy"},
                      {"sku": "JI-OD-7", "description": "Notebook"},
                      {"sku": "TEE-INV-15570", "description": "Tee - Heather - XXL"}]
        self.creds = CLIENT

    def find_existing_order(self, ref):
        order_id = self.existing.get(ref)
        return (order_id, "reference number") if order_id else None

    def list_items(self):
        return self.items

    def create_order(self, payload):
        if self.fail_create:
            raise self.fail_create
        order_id = 5000 + len(self.created)
        self.created.append(payload)
        self.existing[payload["referenceNum"]] = order_id
        return order_id

    def get_order(self, order_id):
        payload = next((p for p in self.created if self.existing.get(p["referenceNum"]) == order_id), None)
        items = [{"itemIdentifier": i["itemIdentifier"], "qty": i["qty"], "readOnly": {"fullyAllocated": False}}
                 for i in (payload or {}).get("orderItems", [])]
        return {"readOnly": {"orderId": order_id},
                "_embedded": {"http://api.3plCentral.com/rels/orders/item": items}}, 'W/"1"'

    def stock_for_order(self, order_id):
        return [{"itemIdentifier": {"sku": i["sku"]}, "available": self.available, "facilityId": 1}
                for i in self.items]

    def complete_order(self, order_id):
        if self.fail_complete:
            self.fail_complete = False
            raise ApiError("3PL Central", "POST", f"/orders/{order_id}/completer", 500, "server error")
        self.completed.append(order_id)

    def get_carriers(self):
        return CARRIER_LIST

    shipments: dict = {}

    def shipment(self, order_id):
        return self.shipments.get(order_id, {"status": "open", "tracking": None, "carrier": None, "shipped_at": None})

    def list_customers(self, facility_id=None):
        return [(CLIENT.customer_id, CLIENT.name)]


class FakeNotifier:
    email_to = "alerts@example.com"

    def __init__(self):
        self.sent = []
        self.recorder = None

    def send(self, subject, lines, po_id=None, html_body=None):
        self.sent.append((subject, lines))
        self.html = html_body


@pytest.fixture
def env(tmp_path):
    def build(pos, settle=0, dry_run=False, available=100, client_id=1213):
        settings = Settings(
            syncore_api_key="k", syncore_base_url="u", vendor_name="Example Supplier LLC",
            sku_pattern="INV|OD", tpl_base_url="u", tpl_user_login="api", tpl_facility_name="Example Warehouse",
            tpl_facility_id=None, tpl_billing_code="Prepaid", tpl_default_carrier=None, tpl_default_mode=None,
            sendgrid_api_key="s", email_from="a@b.c", alert_email_to="alerts@example.com",
            db_path=str(tmp_path / "t.db"), settle_minutes=settle, lookback_hours=72, dry_run=dry_run,
            clients={"5001": CLIENT},
        )
        db = Database(settings.db_path)
        db.set_meta("start_date", START.isoformat())
        db.save_client(None, name=CLIENT.name, syncore_group_id="5001", syncore_group_name="Client Inc",
                       customer_id=CLIENT.customer_id, client_id=CLIENT.client_id, client_secret_enc="encrypted",
                       active=1, source="manual")
        syncore, tpl, notifier = FakeSyncore(pos, client_id), FakeTpl(available), FakeNotifier()
        proc = Processor(settings, db, syncore, notifier, tpl_factory=lambda creds: tpl)
        return proc, db, syncore, tpl, notifier
    return build


NOW = START + timedelta(hours=1)


def test_happy_path_creates_completes_and_logs(env):
    proc, db, syncore, tpl, notifier = env([make_po(comments="Call first")])
    proc.run(NOW)

    assert len(tpl.created) == 1 and tpl.created[0]["referenceNum"] == "12345-3"
    assert tpl.completed == [5000]
    assert notifier.sent == []
    row = db.get(900)
    assert (row["state"], row["order_id"], row["completion"]) == ("done", 5000, "completed")

    proc.run(NOW)  # second run is a no-op
    assert len(tpl.created) == 1 and tpl.completed == [5000]


def test_short_inventory_leaves_open_and_alerts(env):
    proc, db, syncore, tpl, notifier = env([make_po(qty=10)], available=3)
    proc.run(NOW)

    assert tpl.completed == []
    assert db.get(900)["completion"] == "open_short"
    assert len(notifier.sent) == 1
    subject, lines = notifier.sent[0]
    assert "left Open" in subject and any("SHIRT-INV: ordered 10, available 3" in l for l in lines)


def test_other_vendor_and_non_inventory_skus_are_skipped(env):
    proc, db, syncore, tpl, notifier = env([
        make_po(po_id=1, supplier="Another Vendor Co"),
        make_po(po_id=2, sku="PLAIN-123"),
    ])
    proc.run(NOW)
    assert tpl.created == [] and notifier.sent == []
    assert db.get(1)["state"] == "skipped" and db.get(2)["state"] == "skipped"


def test_skipped_po_is_reevaluated_after_edit(env):
    po = make_po(sku="PLAIN-123")
    proc, db, syncore, tpl, _ = env([po])
    proc.run(NOW)
    assert db.get(900)["state"] == "skipped"

    po["line_items"][0]["sku"] = "SHIRT-INV"
    po["last_modified_date"] = "2026-09-17 12:30:00"
    proc.run(NOW)
    assert len(tpl.created) == 1


def test_settle_period_waits_for_po_to_stop_changing(env):
    proc, db, syncore, tpl, _ = env([make_po()], settle=15)
    proc.run(NOW)
    assert tpl.created == [] and db.get(900)["state"] == "waiting"

    db.upsert(900, 12345, first_seen=(NOW - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S"))
    proc.run(NOW)
    assert len(tpl.created) == 1


def test_po_from_a_client_that_isnt_set_up_is_skipped(env):
    proc, db, syncore, tpl, notifier = env([make_po()], client_id=4444)
    proc.run(NOW)
    proc.run(NOW)
    assert tpl.created == [] and notifier.sent == []            # skipped quietly, no alert
    row = db.get(900)
    assert row["state"] == "skipped" and "isn't set up for 3PL Central" in row["skip_reason"]
    [client] = [c for c in db.list_clients() if c["syncore_group_id"] == "777"]
    assert client["name"] == "Northwind" and client["source"] == "syncore"   # listed for setup

def test_failure_after_create_resumes_without_duplicate(env):
    proc, db, syncore, tpl, notifier = env([make_po()])
    tpl.fail_complete = True
    proc.run(NOW)
    assert db.get(900)["state"] == "error" and db.get(900)["order_id"] == 5000
    assert tpl.completed == []

    proc.run(NOW)
    assert len(tpl.created) == 1 and tpl.completed == [5000]     # no second order
    assert db.get(900)["state"] == "done"


def test_existing_3pl_order_with_same_reference_is_reused(env):
    proc, db, syncore, tpl, _ = env([make_po()])
    tpl.existing["12345-3"] = 4242
    proc.run(NOW)
    assert tpl.created == [] and tpl.completed == [4242]


def test_gives_up_after_max_attempts(env):
    proc, db, syncore, tpl, notifier = env([make_po()])
    tpl.fail_create = ApiError("3PL Central", "POST", "/orders", 400, '{"ErrorCode":"DoesNotExist"}')
    for _ in range(MAX_ATTEMPTS + 2):
        proc.run(NOW)
    assert db.get(900)["state"] == "failed"
    assert len(notifier.sent) == 2  # first error, then the "giving up" notice


def test_dry_run_changes_nothing(env):
    proc, db, syncore, tpl, notifier = env([make_po()], dry_run=True, settle=15)
    proc.run(NOW)
    assert tpl.created == [] and notifier.sent == []
    assert db.get(900) is None


def test_skipped_po_can_be_processed_once_the_client_is_ready(env):
    proc, db, syncore, tpl, notifier = env([make_po()], client_id=4444)
    proc.run(NOW)
    assert db.get(900)["state"] == "skipped"
    syncore.client_id = 1213                   # now resolves to a set-up client
    db.reset(900)                              # "Process" on the dashboard
    proc.run(NOW)
    assert len(tpl.created) == 1 and db.get(900)["state"] == "done"

def test_any_contact_in_the_client_group_uses_the_same_client(env):
    first, second = make_po(po_id=900), make_po(po_id=901)
    second["number"] = 4
    proc, db, syncore, tpl, _ = env([first, second])
    syncore.client_id = 1000001          # store billing contact
    proc.handle(900, 12345, None, NOW, force=True)
    syncore.client_id = 1000002          # an employee at the same company
    proc.handle(901, 12345, None, NOW, force=True)
    assert [p["referenceNum"] for p in tpl.created] == ["12345-3", "12345-4"]
    assert db.cached_contact("1000002")["group_id"] == "5001"


def test_open_order_is_completed_once_stock_arrives(env):
    proc, db, syncore, tpl, notifier = env([make_po(qty=10)], available=3)
    proc.run(NOW)
    assert db.get(900)["completion"] == "open_short" and tpl.completed == []

    tpl.available = 50                       # the warehouse received stock
    proc.run(NOW)
    assert tpl.completed == [5000]
    assert db.get(900)["completion"] == "completed"
    assert "order.completed" in [e["event"] for e in db.list_events(po_id=900)]
    assert "completed - stock arrived" in notifier.sent[-1][0]

    proc.run(NOW)                            # nothing more to do
    assert tpl.completed == [5000]


def test_open_order_is_left_alone_while_stock_is_short(env):
    proc, db, syncore, tpl, notifier = env([make_po(qty=10)], available=3)
    proc.run(NOW)
    proc.run(NOW)
    assert tpl.completed == [] and db.get(900)["completion"] == "open_short"
    assert len(notifier.sent) == 1           # the shortage alert isn't repeated


def test_sku_that_is_not_an_item_in_3pl_stops_the_order(env):
    proc, db, syncore, tpl, notifier = env([make_po(sku="MISSING-INV")])
    proc.run(NOW)
    assert tpl.created == []
    assert "not an item for Client Inc" in db.get(900)["last_error"]
    assert "sku.unmatched" in [e["event"] for e in db.list_events(po_id=900)]
    assert len(notifier.sent) == 1

    tpl.items.append({"sku": "MISSING-INV", "description": "New item"})   # warehouse adds the item
    proc.run(NOW)
    assert len(tpl.created) == 1 and db.get(900)["state"] == "done"


def test_existing_order_with_the_same_po_number_is_not_duplicated(env):
    proc, db, syncore, tpl, _ = env([make_po()])
    tpl.existing["12345-3"] = 4242
    proc.run(NOW)
    assert tpl.created == []
    [found] = [e for e in db.list_events(po_id=900) if e["event"] == "order.found_existing"]
    assert "no second order was created" in found["message"]


def test_po_sku_with_a_size_is_matched_to_the_3pl_item(env):
    proc, db, syncore, tpl, _ = env([make_po(sku="TEE-INV-2XL", qty=4)])
    proc.run(NOW)
    assert tpl.created[0]["orderItems"] == [{"itemIdentifier": {"sku": "TEE-INV-15570"}, "qty": 4}]
    [matched] = [e for e in db.list_events(po_id=900) if e["event"] == "sku.matched"]
    assert "TEE-INV-2XL -> TEE-INV-15570" in matched["message"]


def test_unknown_user_login_is_explained(env):
    from tplsync.tpl import UserLoginRejected
    proc, db, syncore, tpl, notifier = env([make_po()])
    tpl.fail_create = UserLoginRejected("someone@example.com")
    proc.run(NOW)
    error = db.get(900)["last_error"]
    assert "isn't a user in this warehouse's 3PL Central" in error and "someone@example.com" in error
    assert "User login" in "\n".join(notifier.sent[0][1])


def test_dismissed_po_is_not_picked_up_again(env):
    proc, db, syncore, tpl, _ = env([make_po()])
    db.upsert(900, 12345, state="dismissed", last_modified="2026-09-17 12:05:00")
    proc.run(NOW)
    assert tpl.created == [] and db.get(900)["state"] == "dismissed"


def test_po_waits_until_its_job_is_submitted(env):
    proc, db, syncore, tpl, notifier = env([make_po()])
    syncore.job_status = "Pending"
    proc.run(NOW)
    db.upsert(900, 12345, first_seen=(NOW - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S"))
    calls, po_reads = [], []
    real_get_job, real_get_po = syncore.get_job, syncore.get_purchase_order
    syncore.get_job = lambda job_id: calls.append(job_id) or real_get_job(job_id)
    syncore.get_purchase_order = lambda *a, **kw: po_reads.append(a) or real_get_po(*a, **kw)
    proc.run(NOW)   # still Pending: checked again, but no new timeline entry and nothing sent
    assert calls == [12345] and po_reads == []        # only the job is looked at, not the whole PO
    assert tpl.created == [] and notifier.sent == []
    row = db.get(900)
    assert row["state"] == "held" and "Pending" in row["skip_reason"]
    events = [e["event"] for e in db.list_events(po_id=900)]
    assert events.count("po.held") == 1 and events.count("po.loaded") == 1

    # The job is submitted; the PO itself didn't change and has dropped out of the search window.
    syncore.job_status = "Submitted"
    syncore.search_purchase_orders = lambda created_from, modified_from: []
    proc.run(NOW)
    assert len(tpl.created) == 1 and db.get(900)["state"] == "done"


def test_po_on_a_delivered_job_is_skipped_not_held(env):
    proc, db, syncore, tpl, notifier = env([make_po()])
    syncore.job_status = "Delivered"
    proc.run(NOW)
    row = db.get(900)
    assert tpl.created == [] and notifier.sent == []
    assert row["state"] == "skipped" and "already done" in row["skip_reason"]


def test_held_po_is_dropped_when_its_job_goes_straight_to_delivered(env):
    proc, db, syncore, tpl, _ = env([make_po(po_id=901)])
    syncore.job_status = "WIP"
    proc.run(NOW)
    assert db.get(901)["state"] == "held"
    db.upsert(901, 12345, first_seen=(NOW - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S"))
    syncore.job_status = "Delivered"
    proc.run(NOW)
    assert db.get(901)["state"] == "skipped" and tpl.created == []


def test_job_statuses_setting_controls_what_is_sent(env):
    proc, db, syncore, tpl, _ = env([make_po()])
    proc.s.job_statuses = ("submitted",)
    syncore.job_status = "WIP"
    proc.run(NOW)
    assert tpl.created == [] and db.get(900)["state"] == "held"
    assert "sent once the job is Submitted" in db.get(900)["skip_reason"]


def test_alert_emails_can_be_turned_off(env):
    from tplsync.processor import ClientNotReady
    proc, db, syncore, tpl, notifier = env([make_po(qty=10)], available=3)
    proc.s.emails["short_stock"] = False
    proc.run(NOW)
    assert db.get(900)["completion"] == "open_short" and notifier.sent == []
    assert any(e["event"] == "alert.muted" for e in db.list_events(po_id=900))

    # A paused client is not emailed by default, and isn't emailed later if the setting is turned back on.
    proc._record_error(901, 12345, ClientNotReady("Client Inc is paused on the Clients page."))
    assert notifier.sent == []
    proc.s.emails["client_paused"] = True
    proc._record_error(901, 12345, ClientNotReady("Client Inc is paused on the Clients page."))
    assert notifier.sent == []
    proc._record_error(901, 12345, RuntimeError("3PL Central is down"))
    assert len(notifier.sent) == 1


def test_po_is_ignored_when_its_job_goes_back_from_submitted_to_wip(env):
    po = make_po(sku="MISSING-INV")
    proc, db, syncore, tpl, notifier = env([po])
    syncore.job_status = "Submitted"
    proc.run(NOW)                       # seen while Submitted, but the SKU stops it
    assert db.get(900)["state"] == "error" and db.get(900)["job_submitted_at"]

    syncore.job_status = "WIP"          # the team pulled the job back
    po["line_items"][0]["sku"] = "SHIRT-INV"
    proc.run(NOW)
    row = db.get(900)
    assert row["state"] == "skipped" and "went back to WIP after being Submitted" in row["skip_reason"]
    assert tpl.created == []

    syncore.job_status = "Submitted"    # skipped POs aren't re-checked, so nothing is sent
    proc.run(NOW)
    assert tpl.created == []


def test_wip_job_that_was_never_submitted_is_still_held(env):
    proc, db, syncore, tpl, _ = env([make_po()])
    syncore.job_status = "WIP"
    proc.run(NOW)
    assert db.get(900)["state"] == "held" and not db.get(900)["job_submitted_at"]


def test_allowed_sku_is_sent_for_its_own_client_only(env):
    proc, db, syncore, tpl, _ = env([make_po(sku="CENT037", qty=2)])
    tpl.items.append({"sku": "CENT037", "description": "14 Grande Wall Clock - Black"})
    proc.run(NOW)
    assert db.get(900)["state"] == "skipped"          # not allowed yet

    client_pk = db.list_clients()[0]["id"]
    other = db.save_client(None, name="Other Co", source="manual")
    db.add_allowed_sku(other, "CENT037", None, "test")
    syncore.pos[900]["last_modified_date"] = "2026-09-17 12:30:00"
    proc.run(NOW)
    row = db.get(900)
    assert row["state"] == "skipped" and "Client Inc's Allowed SKUs" in row["skip_reason"]

    db.add_allowed_sku(client_pk, "cent037", "Wall clock", "test")
    syncore.pos[900]["last_modified_date"] = "2026-09-17 12:45:00"
    proc.run(NOW)
    assert db.get(900)["state"] == "done"
    assert tpl.created[0]["orderItems"] == [{"itemIdentifier": {"sku": "CENT037"}, "qty": 2}]


def test_held_and_skipped_pos_record_their_reference_and_client(env):
    proc, db, syncore, tpl, _ = env([make_po(po_id=901), make_po(po_id=902, sku="PLAIN-123")])
    syncore.job_status = "WIP"
    proc.run(NOW)
    held, skipped = db.get(901), db.get(902)
    assert held["state"] == "held" and held["reference"] == "12345-3"
    assert held["client_id"] == "5001" and held["client_name"] == "Client Inc"
    assert skipped["state"] == "skipped" and skipped["reference"] == "12345-3"


def test_alert_emails_use_the_html_design(env):
    proc, db, syncore, tpl, notifier = env([make_po(sku="MISSING-INV")])
    proc.s.admin_url = "https://3pl.example.com"
    proc.run(NOW)
    subject, lines = notifier.sent[-1]
    assert subject == "[3PL] Problem with order 12345-3"
    assert "View this PO: https://3pl.example.com/po/900" in lines
    html = notifier.html
    assert "Problem with an order" in html and "Client Inc" in html and "MISSING-INV" in html
    assert 'href="https://3pl.example.com/po/900"' in html


def test_shortage_alert_lists_the_short_items(env):
    proc, db, syncore, tpl, notifier = env([make_po(qty=10)], available=3)
    proc.run(NOW)
    html = notifier.html
    assert "Order left open" in html and "Short items" in html and "SHIRT-INV" in html and ">10<" in html


def test_po_with_unreadable_lines_still_names_its_client(env):
    po = make_po()
    po["line_items"] = [
        {"line_id": 1, "parent_id": 0, "type": "Product", "sku": "", "description": "ON DEMAND", "quantity": 4},
        {"line_id": 2, "parent_id": 0, "type": "Comment", "sku": "", "description": "SKU: A-INV", "quantity": 0},
        {"line_id": 3, "parent_id": 0, "type": "Comment", "sku": "", "description": "SKU: B-INV", "quantity": 0},
    ]
    proc, db, syncore, tpl, notifier = env([po])
    proc.run(NOW)
    row = db.po_view(900)
    assert row["state"] == "error" and row["client_display"] == "Client Inc"


def _edit(po, minute):
    po["last_modified_date"] = f"2026-09-17 13:{minute:02d}:00"


def test_po_edited_after_it_was_sent_alerts_once(env):
    po = make_po()
    proc, db, syncore, tpl, notifier = env([po])
    proc.run(NOW)
    assert db.get(900)["state"] == "done" and json.loads(db.get(900)["sent_snapshot"])["lines"] == {"SHIRT-INV": 10.0}
    notifier.sent.clear()

    _edit(po, 1)                                         # accounting-style edit: nothing that was sent changed
    proc.run(NOW)
    assert notifier.sent == [] and db.get(900)["changed_at"] is None

    po["line_items"][0]["quantity"] = 12                 # quantity changed after sending
    po["ship_via"] = "UPS 2nd Day Air"
    _edit(po, 2)
    proc.run(NOW)
    subject, lines = notifier.sent[-1]
    assert subject == "[3PL] 12345-3 changed in Syncore after it was sent"
    assert "  - SHIRT-INV quantity 10 -> 12" in lines
    assert any("Ship Via changed" in line for line in lines)
    assert "What changed" in notifier.html
    row = db.get(900)
    assert row["changed_at"] and "SHIRT-INV quantity 10 -> 12" in row["change_summary"]
    assert [r["po_id"] for r in db.recent(10, state="changed")] == [900]
    assert len(tpl.created) == 1                          # the 3PL order isn't touched

    _edit(po, 3)                                         # edited again, same differences: no second email
    proc.run(NOW)
    assert len(notifier.sent) == 1


def test_po_deleted_after_it_was_sent_is_noticed_by_the_daily_check(env):
    proc, db, syncore, tpl, notifier = env([make_po()])
    proc.run(NOW)
    notifier.sent.clear()
    del syncore.pos[900]                                 # deleted in Syncore: it no longer shows up in searches
    db.set_meta("sent_check_on", None)
    proc.run(NOW)
    assert "no longer exists in Syncore" in db.get(900)["change_summary"]
    assert notifier.sent[-1][0] == "[3PL] 12345-3 changed in Syncore after it was sent"


def test_po_sent_before_snapshots_takes_a_quiet_baseline(env):
    po = make_po()
    proc, db, syncore, tpl, notifier = env([po])
    proc.run(NOW)
    db.upsert(900, 12345, sent_snapshot=None)            # as if sent before this feature existed
    notifier.sent.clear()
    po["line_items"][0]["quantity"] = 12
    _edit(po, 5)
    proc.run(NOW)
    assert notifier.sent == [] and json.loads(db.get(900)["sent_snapshot"])["lines"] == {"SHIRT-INV": 12.0}


def test_tracking_number_is_picked_up_when_the_warehouse_ships(env):
    proc, db, syncore, tpl, notifier = env([make_po()])
    proc.run(NOW)                                        # order 5000 created; not shipped yet
    assert db.get(900)["order_status"] is None and db.get(900)["ship_checked_at"]

    tpl.shipments = {5000: {"status": "shipped", "tracking": "1Z0WY6070307889570", "carrier": "UPS",
                            "shipped_at": "2026-09-17T15:00:00"}}
    proc.run(NOW)                                        # checked less than an hour ago: not asked again yet
    assert db.get(900)["order_status"] is None

    proc.run(NOW + timedelta(hours=2))
    row = db.get(900)
    assert (row["order_status"], row["tracking_number"], row["ship_carrier"]) == ("shipped", "1Z0WY6070307889570", "UPS")
    assert any(e["event"] == "order.shipped" for e in db.list_events(po_id=900))


def test_order_cancelled_in_3pl_central_is_recorded(env):
    proc, db, syncore, tpl, notifier = env([make_po()])
    proc.run(NOW)
    tpl.shipments = {5000: {"status": "cancelled", "tracking": None, "carrier": "UPS", "shipped_at": None}}
    proc.run(NOW + timedelta(hours=2))
    assert db.get(900)["order_status"] == "cancelled"


def test_po_for_a_paused_client_is_on_hold_not_an_error(env):
    from dataclasses import replace
    proc, db, syncore, tpl, notifier = env([make_po()])
    proc.s.clients["5001"] = replace(CLIENT, active=False)
    proc.run(NOW)
    row = db.get(900)
    assert row["state"] == "held" and "paused on the Clients page" in row["skip_reason"] and row["last_error"] is None
    assert row["attempts"] == 0 and notifier.sent == []              # no error, no email (off by default)
    assert 900 not in [r["po_id"] for r in db.recent(50)]             # not under Active
    assert 900 not in [r["po_id"] for r in db.recent(50, state="problems")]

    db.upsert(900, 12345, first_seen=(NOW - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S"))
    calls = []
    real_get_job = syncore.get_job
    syncore.get_job = lambda job_id: calls.append(job_id) or real_get_job(job_id)
    proc.run(NOW)                                                     # still paused: nothing asked of Syncore
    assert calls == [] and tpl.created == []

    proc.s.clients["5001"] = CLIENT                                   # activated
    proc.run(NOW)
    assert db.get(900)["state"] == "done" and len(tpl.created) == 1


def test_on_demand_items_go_out_as_the_generic_sku_with_details_in_the_notes(env):
    import dataclasses
    po = make_po()
    po["line_items"] = [
        {"line_id": 1, "parent_id": 0, "description": "ON DEMAND FROM JUST INK", "quantity": 0},
        {"line_id": 2, "parent_id": None, "sku": "TM18501", "description": "Men's Tyndall Vest", "quantity": 0},
        {"line_id": 3, "parent_id": None, "description": "SKU: BAL513OD-3", "quantity": 0},
        {"line_id": 4, "parent_id": 1, "type": "Color", "description": "Navy", "quantity": 0},
        {"line_id": 5, "parent_id": 4, "type": "Size", "description": "Large", "quantity": 2},
        {"line_id": 6, "parent_id": 0, "description": "INVENTORY", "quantity": 0},
        {"line_id": 7, "parent_id": None, "sku": "ME0XK00E", "description": "Polo", "quantity": 1},
        {"line_id": 8, "parent_id": None, "description": "SKU: SHIRT-INV", "quantity": 0},
    ]
    proc, db, syncore, tpl, notifier = env([po])
    proc.s = dataclasses.replace(proc.s, on_demand_sku="ONDEMAND123")
    tpl.items.append({"sku": "ONDEMAND123", "description": "On Demand Item"})
    stock = tpl.stock_for_order
    tpl.stock_for_order = lambda oid: [s if s["itemIdentifier"]["sku"] != "ONDEMAND123" else {**s, "available": 0}
                                       for s in stock(oid)]          # never stocked: not a shortage
    proc.run(NOW)

    order = tpl.created[0]
    assert order["orderItems"] == [{"itemIdentifier": {"sku": "SHIRT-INV"}, "qty": 1},
                                   {"itemIdentifier": {"sku": "ONDEMAND123"}, "qty": 2}]
    assert "ON DEMAND (sent as ONDEMAND123): Men's Tyndall Vest (TM18501) - Navy / Large x2 [PO SKU BAL513OD-3]" \
        in order["notes"]
    assert not order.get("shippingNotes")
    assert tpl.completed == [5000] and notifier.sent == []
    assert any(e["event"] == "po.on_demand" for e in db.list_events(po_id=900))
