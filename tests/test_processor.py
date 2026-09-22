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
    def __init__(self, pos, client_id=1213):
        self.pos = {p["id"]: p for p in pos}
        self.client_id = client_id

    def search_purchase_orders(self, created_from, modified_from):
        return [{"id": p["id"], "job_number": p["job_number"], "last_modified_date": p["last_modified_date"]}
                for p in self.pos.values()]

    def get_purchase_order(self, job_id, po_id, fresh=False):
        return self.pos.get(po_id)

    def get_job(self, job_id):
        return {"id": job_id, "client": {"id": self.client_id, "business_name": "Client Inc", "name": "Pat Buyer"}}

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

    def list_customers(self, facility_id=None):
        return [(CLIENT.customer_id, CLIENT.name)]


class FakeNotifier:
    email_to = "alerts@example.com"

    def __init__(self):
        self.sent = []
        self.recorder = None

    def send(self, subject, lines, po_id=None):
        self.sent.append((subject, lines))


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


def test_missing_client_credentials_alerts_once(env):
    proc, db, syncore, tpl, notifier = env([make_po()], client_id=4444)
    proc.run(NOW)
    proc.run(NOW)
    assert tpl.created == []
    assert db.get(900)["state"] == "error" and db.get(900)["attempts"] == 0
    assert len(notifier.sent) == 1 and "Northwind" in "\n".join(notifier.sent[0][1])


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


def test_incomplete_client_waits_without_using_attempts(env):
    proc, db, syncore, tpl, notifier = env([make_po()], client_id=4444)
    for _ in range(MAX_ATTEMPTS + 3):
        proc.run(NOW)
    row = db.get(900)
    assert row["state"] == "error" and row["attempts"] == 0
    assert len(notifier.sent) == 1 and "once the client is finished" in "\n".join(notifier.sent[0][1])
    [client] = [c for c in db.list_clients() if c["syncore_group_id"] == "777"]
    assert client["name"] == "Northwind" and client["source"] == "syncore" and client["active"] == 0


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
