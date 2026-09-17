"""Syncore PO -> 3PL Central order workflow.

Each PO moves through idempotent steps, recorded in SQLite so a failure part-way
through resumes on the next run without creating a duplicate order:

  1. wait until the PO hasn't changed for PO_SETTLE_MINUTES (lines are often
     added after the PO is first saved)
  2. filter: the PO's vendor matches the setting, it has inventory SKUs, and its client is set up
  3. create the 3PL order (or find an existing one with the same reference)
  4. stock check -> Complete, or leave Open and email an alert

Nothing is written back to Syncore: the transaction number is recorded here and
shown in the admin, on the PO's timeline and in the alerts.

Every step is written to the events table so the admin Logs page can show a
timeline per PO and per run.
"""

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional

from . import carriers as carrier_match
from . import clientmatch
from . import mapping
from . import skus
from .config import Settings, TplClient
from .db import Database, client_missing, utcnow
from .http import ApiError
from .notify import Notifier
from .syncore import SyncoreClient
from .tpl import TplCentralClient

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 20
CARRIER_CACHE_HOURS = 6


def order_items(order: dict) -> Dict[str, float]:
    """SKU -> quantity from a 3PL Central order."""
    embedded = (order.get("_embedded") or {}).get("http://api.3plCentral.com/rels/orders/item") \
        or order.get("orderItems") or []
    lines: Dict[str, float] = {}
    for item in embedded:
        sku = (item.get("itemIdentifier") or {}).get("sku")
        if sku:
            lines[sku] = lines.get(sku, 0.0) + float(item.get("qty") or 0)
    return lines


class SkipPO(Exception):
    """The PO doesn't belong in 3PL Central."""


class ClientNotReady(RuntimeError):
    """The PO's client isn't fully set up. Retried every run without using up attempts."""


class Processor:
    def __init__(self, settings: Settings, db: Database, syncore: SyncoreClient, notifier: Notifier,
                 tpl_factory: Optional[Callable[[TplClient], TplCentralClient]] = None,
                 run_id: Optional[int] = None):
        self.s = settings
        self.db = db
        self.syncore = syncore
        self.notifier = notifier
        self.run_id = run_id
        self.stats: Counter = Counter()
        self._tpl_factory = tpl_factory or (
            lambda creds: TplCentralClient(settings.tpl_base_url, creds, settings.tpl_user_login))
        self._tpl_clients: Dict[str, TplCentralClient] = {}
        self._carriers = None
        self._items: Dict[int, list] = {}
        if getattr(notifier, "recorder", None) is None:
            notifier.recorder = self._record_email

    def tpl(self, creds: TplClient) -> TplCentralClient:
        if creds.client_id not in self._tpl_clients:
            self._tpl_clients[creds.client_id] = self._tpl_factory(creds)
        return self._tpl_clients[creds.client_id]

    def carrier_list(self, tpl: TplCentralClient):
        """The warehouse's carriers, cached in the database for CARRIER_CACHE_HOURS."""
        if self._carriers is None:
            cached = self.db.get_meta("tpl_carriers")
            if cached:
                data = json.loads(cached)
                fetched = datetime.fromisoformat(data["fetched_at"]).replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - fetched < timedelta(hours=CARRIER_CACHE_HOURS):
                    self._carriers = carrier_match.carriers_from_json(data["carriers"])
            if self._carriers is None:
                self._carriers = carrier_match.parse_carrier_list(tpl.get_carriers())
                self.db.set_meta("tpl_carriers", json.dumps(
                    {"fetched_at": utcnow(), "carriers": carrier_match.carriers_to_json(self._carriers)}))
                log.info("Loaded %d carrier(s) from 3PL Central", len(self._carriers))
        return self._carriers

    CUSTOMER_SYNC_HOURS = 24

    def client_group(self, contact_id: str):
        """(group id, group name) for a Syncore contact, cached for a week."""
        cached = self.db.cached_contact(contact_id)
        if cached is None:
            contact = self.syncore.get_contact(contact_id)
            group = contact.get("client_group") or {}
            self.db.cache_contact(contact_id, str(group["id"]) if group.get("id") else None, group.get("name"),
                                  contact.get("business_name"))
            cached = self.db.cached_contact(contact_id)
        return cached["group_id"], cached["group_name"]

    def match_groups_if_due(self) -> None:
        """Link clients to Syncore client groups by name once a day. Never fails a run."""
        last = self.db.get_meta("clients_matched_at")
        if last and datetime.now(timezone.utc) - datetime.fromisoformat(last).replace(tzinfo=timezone.utc) \
                < timedelta(hours=self.CUSTOMER_SYNC_HOURS):
            return
        if not any(not r["syncore_group_id"] for r in self.db.list_clients()):
            return
        try:
            counts = clientmatch.match_syncore_groups(self.db, self.syncore.list_client_groups())
            log.info("Matched clients to Syncore client groups: %s", counts)
        except Exception:  # noqa: BLE001
            log.exception("Could not match Syncore client groups")

    def sync_customers_if_due(self) -> None:
        """Pull 3PL Central's customer list into Clients once a day. Never fails a run."""
        last = self.db.get_meta("tpl_customers_synced_at")
        if last and datetime.now(timezone.utc) - datetime.fromisoformat(last).replace(tzinfo=timezone.utc) \
                < timedelta(hours=self.CUSTOMER_SYNC_HOURS):
            return
        creds = next((c for c in self.s.clients.values() if c.active), None)
        if creds is None:
            return
        try:
            counts = self.db.sync_tpl_customers(self.tpl(creds).list_customers(self.s.tpl_facility_id))
            self.db.set_meta("tpl_customers_synced_at", utcnow())
            log.info("Synced 3PL Central customers: %s", counts)
        except Exception:  # noqa: BLE001
            log.exception("Could not sync 3PL Central customers")

    # --- event log ----------------------------------------------------------

    def event(self, po_id: Optional[int], job_id: Optional[int], ref: Optional[str], level: str, event: str,
              message: str, data=None) -> None:
        getattr(log, {"info": "info", "warning": "warning", "error": "error"}[level])(
            "PO %s%s: %s", po_id, f" ({ref})" if ref else "", message)
        try:
            self.db.add_event(self.run_id, po_id, job_id, ref, level, event, message, data)
        except Exception:  # noqa: BLE001 - never let logging break processing
            log.exception("Could not write event %s", event)

    def _record_email(self, subject, recipients, body, status, error, po_id) -> None:
        self.db.add_email(self.run_id, po_id, subject, recipients, body, status, error)

    # --- polling ------------------------------------------------------------

    def run(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        if self.s.paused:
            log.info("Processing is paused in settings; nothing to do")
            self.stats["paused"] = 1
            return
        self.sync_customers_if_due()
        self.match_groups_if_due()
        start_raw = self.db.get_meta("start_date")
        if not start_raw:
            raise RuntimeError("No go-live date set. Click “Go live now” on the dashboard first.")
        start = datetime.fromisoformat(start_raw)
        modified_from = max(start, now - timedelta(hours=self.s.lookback_hours))
        log.info("Searching Syncore for POs created since %s and modified since %s%s", start.isoformat(),
                 modified_from.isoformat(), " (dry run)" if self.s.dry_run else "")

        seen = set()
        for brief in self.syncore.search_purchase_orders(start.replace(tzinfo=None),
                                                         modified_from.replace(tzinfo=None)):
            po_id, job_id = int(brief["id"]), int(brief["job_number"])
            seen.add(po_id)
            self.handle(po_id, job_id, brief.get("last_modified_date"), now)
        log.info("Syncore returned %d PO(s)", len(seen))
        self.stats["found"] = len(seen)

        if self.s.auto_complete_open:
            self.complete_open_orders()

        # Resume anything still in flight that fell out of the search window.
        for row in self.db.active():
            if row["po_id"] not in seen:
                self.handle(row["po_id"], row["job_id"], row["last_modified"], now)

    def complete_open_orders(self) -> None:
        """Orders left Open for short stock: complete them once the warehouse has the inventory."""
        for row in self.db.open_short_orders():
            creds = self.s.clients.get(str(row["client_id"] or ""))
            if creds is None or not creds.active or not row["order_id"]:
                continue
            ref, order_id = row["reference"], row["order_id"]
            try:
                tpl = self.tpl(creds)
                order, _ = tpl.get_order(order_id)
                lines = order_items(order)
                shortages = mapping.find_shortages(lines, order, tpl.stock_for_order(order_id),
                                                   self.s.tpl_facility_id)
                if shortages:
                    self.stats["still_open"] += 1
                    continue
                if self.s.dry_run:
                    self.event(row["po_id"], row["job_id"], ref, "info", "order.recheck",
                               f"Dry run: 3PL order {order_id} has stock now and would be completed",
                               {"ordered": lines})
                    continue
                try:
                    tpl.complete_order(order_id)
                except ApiError as exc:
                    if exc.error_code != "AlreadyCompleted":
                        raise
                self.db.upsert(row["po_id"], row["job_id"], completion="completed")
                self.stats["completed_late"] += 1
                self.event(row["po_id"], row["job_id"], ref, "info", "order.completed",
                           f"Stock arrived: 3PL order {order_id} was Open and is now completed", {"ordered": lines})
                self._send_alert(row["po_id"], row["job_id"], ref,
                                 f"[3PL] Order {ref} completed - stock arrived",
                                 [f"3PL Central order {order_id} ({ref}) was waiting for inventory and has now been "
                                  f"completed automatically.", "", f"Client: {creds.name}"])
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                self.event(row["po_id"], row["job_id"], ref, "error", "order.recheck_failed",
                           f"Could not re-check 3PL order {order_id}: {exc}")

    def client_items(self, tpl: TplCentralClient) -> list:
        """The client's items in 3PL Central, fetched once per run."""
        if tpl.creds.customer_id not in self._items:
            self._items[tpl.creds.customer_id] = tpl.list_items()
            log.info("Customer %s has %d item(s) in 3PL Central", tpl.creds.customer_id,
                     len(self._items[tpl.creds.customer_id]))
        return self._items[tpl.creds.customer_id]

    def handle(self, po_id: int, job_id: int, last_modified: Optional[str], now: datetime,
               force: bool = False) -> None:
        rec = self.db.get(po_id)
        if rec is not None and rec["state"] in ("done", "failed") and not force:
            return

        if not force and not self.s.dry_run and not self._settled(rec, po_id, job_id, last_modified, now):
            return

        try:
            self._process(po_id, job_id, last_modified)
        except SkipPO as exc:
            self.stats["skipped"] += 1
            if not self.s.dry_run:
                self.db.upsert(po_id, job_id, state="skipped", skip_reason=str(exc.args[0]),
                               last_modified=last_modified)
            self.event(po_id, job_id, exc.args[1] if len(exc.args) > 1 else None, "info", "po.skipped",
                       f"Skipped: {exc.args[0]}", exc.args[2] if len(exc.args) > 2 else None)
        except Exception as exc:  # noqa: BLE001 - every failure is recorded and alerted
            self._record_error(po_id, job_id, exc)

    def _settled(self, rec, po_id: int, job_id: int, last_modified: Optional[str], now: datetime) -> bool:
        in_progress = rec is not None and rec["state"] in ("processing", "error")
        if in_progress:
            return True
        if rec is None or rec["last_modified"] != last_modified:
            # New, or edited since we last looked (including previously skipped POs).
            self.db.upsert(po_id, job_id, state="waiting", last_modified=last_modified, first_seen=utcnow(),
                           skip_reason=None)
            if self.s.settle_minutes > 0:
                self.stats["waiting"] += 1
                self.event(po_id, job_id, None, "info", "po.waiting",
                           f"{'New' if rec is None else 'Changed'} PO in Syncore (last modified {last_modified}); "
                           f"waiting {self.s.settle_minutes} min for edits to finish")
                return False
            return True
        if rec["state"] == "skipped":
            return False
        first_seen = datetime.fromisoformat(rec["first_seen"]).replace(tzinfo=timezone.utc)
        if now - first_seen >= timedelta(minutes=self.s.settle_minutes):
            return True
        self.stats["waiting"] += 1
        return False

    # --- the workflow ---------------------------------------------------------

    def _process(self, po_id: int, job_id: int, last_modified: Optional[str]) -> None:
        dry = self.s.dry_run
        rec = self.db.get(po_id)
        self.stats["processed"] += 1

        po = self.syncore.get_purchase_order(job_id, po_id, fresh=True)
        if po is None:
            raise RuntimeError(f"PO {po_id} not found on Syncore job {job_id}")
        ref = mapping.reference_number(po)
        supplier = (po.get("supplier") or {}).get("name")
        try:
            lines = mapping.order_lines(po, self.s.sku_pattern)
        except mapping.LineMappingError:
            if not mapping.is_vendor(po, self.s.vendor_name):
                raise SkipPO(f"vendor is {supplier!r}, not {self.s.vendor_name!r}", ref)
            raise
        self.event(po_id, job_id, ref, "info", "po.loaded",
                   f"Loaded PO from Syncore: vendor {supplier!r}, {len(po.get('line_items') or [])} line(s), "
                   f"{len(lines)} matching SKU(s)", {
                       "supplier": po.get("supplier"), "ship_via": po.get("ship_via"), "ship_to": po.get("ship_to"),
                       "critical_comments": po.get("critical_comments"), "matched_lines": lines,
                       "line_items": [{k: li.get(k) for k in ("line_id", "parent_id", "type", "sku", "description",
                                                              "quantity")} for li in po.get("line_items") or []],
                   })

        order_id = rec["order_id"] if rec else None
        if order_id is None:
            if not mapping.is_vendor(po, self.s.vendor_name):
                raise SkipPO(f"vendor is {supplier!r}, not {self.s.vendor_name!r}", ref)
        if not lines and order_id is None:
            raise SkipPO(f"no SKUs match {self.s.sku_pattern!r}", ref, {"skus_on_po": mapping.po_skus(po)})

        job = self.syncore.get_job(job_id)
        client = job.get("client") or {}
        client_id = str(client.get("id") or "")
        client_label = client.get("business_name") or client.get("name") or "unknown"
        if not client_id:
            raise RuntimeError("The Syncore job has no client.")
        group_id, group_name = self.client_group(client_id)
        if not group_id:
            raise ClientNotReady(
                f"Syncore client {client_id} ({client_label}) isn't in a client group, so it can't be matched to a "
                f"3PL Central client. Add the contact to its company's client group in Syncore.")
        row = self.db.link_syncore_group(group_id, group_name)
        creds = self.s.clients.get(group_id)
        if creds is None:
            missing = [m for m in client_missing(row) if m != "Syncore client group"]
            self.event(po_id, job_id, ref, "warning", "client.incomplete",
                       f"Client {row['name']} (Syncore group {group_name}) isn't set up yet: missing "
                       f"{', '.join(missing)}", {"client_pk": row["id"], "missing": missing,
                                                 "syncore_client": client_id, "syncore_group": group_id})
            raise ClientNotReady(
                f"{row['name']} (Syncore client group {group_name}) needs {', '.join(missing)} before its orders "
                f"can go to 3PL Central. Finish it on the Clients page.")
        if not creds.active:
            raise ClientNotReady(f"{creds.name} is paused on the Clients page.")
        tpl = self.tpl(creds)

        po_lines = dict(lines)
        try:
            lines, renames = skus.resolve_lines(lines, self.client_items(tpl), creds.name)
        except skus.SkuMatchError as exc:
            self.event(po_id, job_id, ref, "error", "sku.unmatched", str(exc), {"ordered": po_lines})
            raise RuntimeError(f"{exc} No order was created. Add the item in 3PL Central, or fix the SKU on the "
                               f"Syncore PO.")
        if renames:
            self.event(po_id, job_id, ref, "info", "sku.matched",
                       "PO SKUs matched to 3PL Central items by size: "
                       + ", ".join(f"{a} -> {b}" for a, b in renames.items()), {"matched": renames})

        try:
            routing = carrier_match.resolve_routing(
                po.get("ship_via"), self.s.shipping_map, self.carrier_list(tpl), self.s.tpl_billing_code,
                self.s.tpl_default_carrier, self.s.tpl_default_mode)
        except carrier_match.CarrierMatchError as exc:
            self.event(po_id, job_id, ref, "error", "shipping.unmatched", str(exc), {
                "ship_via": po.get("ship_via"),
                "overrides": sorted(self.s.shipping_map),
            })
            raise
        payload = mapping.build_order(po, job, lines, creds.customer_id, self.s.tpl_facility_id,
                                      self.s.tpl_facility_name, {**routing.as_payload(), "billingCode": routing.billing_code})
        self.event(po_id, job_id, ref, "info", "po.matched",
                   f"Syncore client {client_id} ({client_label}, group {group_name}) uses 3PL client {creds.name} "
                   f"(customer {creds.customer_id}); {routing.explanation}",
                   {"job": {k: job.get(k) for k in ("id", "status", "job_class", "client", "store")},
                    "routing": payload.get("routingInfo"), "service": routing.service_description,
                    "routing_source": routing.source, "billing_code": payload.get("billingCode")})

        if dry:
            existing = tpl.find_existing_order(ref)
            self.stats["previewed"] += 1
            self.event(po_id, job_id, ref, "info", "order.preview",
                       (f"Dry run: 3PL order {existing[0]} already exists with this {existing[1]}"
                        if existing else "Dry run: this order would be created in 3PL Central"),
                       {"existing_order_id": existing[0] if existing else None, "payload": payload})
            return

        self.db.upsert(po_id, job_id, state="processing", reference=ref, client_id=group_id,
                       last_modified=last_modified)

        # Step 3: create (idempotent via reference number lookup)
        if order_id is None:
            existing = tpl.find_existing_order(ref)
            order_id = existing[0] if existing else None
            if existing:
                self.event(po_id, job_id, ref, "warning", "order.found_existing",
                           f"3PL order {existing[0]} already has {ref} as its {existing[1]}, so no second order was "
                           f"created", {"order_id": existing[0], "matched_on": existing[1]})
            else:
                order_id = tpl.create_order(payload)
                self.stats["created"] += 1
                self.event(po_id, job_id, ref, "info", "order.created",
                           f"Created 3PL order {order_id} for {creds.name}", {"order_id": order_id, "payload": payload})
            self.db.upsert(po_id, job_id, order_id=order_id)

        # Step 4: complete, or leave open when stock is short
        completion = rec["completion"] if rec else None
        if completion is None:
            order, _ = tpl.get_order(order_id)
            summaries = tpl.stock_for_order(order_id)
            shortages = mapping.find_shortages(lines, order, summaries, self.s.tpl_facility_id)
            self.event(po_id, job_id, ref, "warning" if shortages else "info", "stock.checked",
                       (f"Stock check: {len(shortages)} of {len(lines)} SKU(s) short" if shortages
                        else f"Stock check: all {len(lines)} SKU(s) available"),
                       {"ordered": lines, "shortages": shortages, "stock_summaries": summaries,
                        "order_fully_allocated": (order.get("readOnly") or {}).get("fullyAllocated")})
            if shortages:
                completion = "open_short"
                self.db.upsert(po_id, job_id, completion=completion)
                self.stats["left_open"] += 1
                self.event(po_id, job_id, ref, "warning", "order.left_open",
                           f"3PL order {order_id} left Open because of short inventory", {"shortages": shortages})
                self._alert_shortage(po_id, po, job, creds, order_id, shortages)
            else:
                try:
                    tpl.complete_order(order_id)
                    message = f"3PL order {order_id} completed"
                except ApiError as exc:
                    if exc.error_code != "AlreadyCompleted":
                        raise
                    message = f"3PL order {order_id} was already completed"
                completion = "completed"
                self.db.upsert(po_id, job_id, completion=completion)
                self.stats["completed"] += 1
                self.event(po_id, job_id, ref, "info", "order.completed", message, {"order_id": order_id})

        self.db.upsert(po_id, job_id, state="done", last_error=None)
        self.event(po_id, job_id, ref, "info", "po.done",
                   f"Finished: 3PL transaction {order_id}, {'Open (short stock)' if completion == 'open_short' else 'Completed'}")

    # --- alerts ---------------------------------------------------------------

    def _send_alert(self, po_id: int, job_id: Optional[int], ref: Optional[str], subject: str, lines: list) -> bool:
        try:
            self.notifier.send(subject, lines, po_id=po_id)
        except Exception as exc:  # noqa: BLE001
            self.event(po_id, job_id, ref, "error", "alert.failed", f"Alert email could not be sent: {exc}")
            return False
        self.event(po_id, job_id, ref, "info", "alert.sent",
                   f"{'Dry run: alert not sent' if self.s.dry_run else 'Alert emailed'} to {self.notifier.email_to}: {subject}")
        return True

    def _alert_shortage(self, po_id: int, po: dict, job: dict, creds: TplClient, order_id: int, shortages: list) -> None:
        ref = mapping.reference_number(po)
        lines = [
            f"3PL Central order {order_id} ({ref}) was created but left OPEN because there is not "
            f"enough inventory. Complete it in 3PL Central once stock is available.",
            "",
            f"Client: {creds.name or (job.get('client') or {}).get('business_name', '')}",
            f"Syncore job {po['job_number']}, PO {ref}",
            f"Ship to: {(po.get('ship_to') or {}).get('business_name') or (po.get('ship_to') or {}).get('name', '')}",
            "",
            "Short items:",
        ]
        lines += [f"  {s['sku']}: ordered {s['ordered']:g}, available {s['available']:g}" for s in shortages]
        self._send_alert(po_id, po["job_number"], ref, f"[3PL] Order {ref} left Open - insufficient inventory", lines)

    def _record_error(self, po_id: int, job_id: int, exc: Exception) -> None:
        log.exception("PO %s (job %s) failed", po_id, job_id)
        self.stats["errors"] += 1
        rec = self.db.get(po_id)
        ref = rec["reference"] if rec else None
        message = str(exc)[:2000]
        details = {"error_type": type(exc).__name__}
        if isinstance(exc, ApiError):
            details.update(service=exc.service, http_status=exc.status, error_code=exc.error_code,
                           response=exc.body[:4000])

        if self.s.dry_run:
            self.event(po_id, job_id, ref, "error", "po.error", f"Dry run error: {message}", details)
            return

        attempts = (rec["attempts"] if rec else 0) + (0 if isinstance(exc, ClientNotReady) else 1)
        state = "failed" if attempts >= MAX_ATTEMPTS else "error"
        self.db.upsert(po_id, job_id, state=state, attempts=attempts, last_error=message)
        details["attempt"] = attempts
        if isinstance(exc, ClientNotReady):
            text = f"Waiting for client setup (checked again every run): {message}"
        elif state == "failed":
            text = f"Gave up after {attempts} attempts: {message}"
        else:
            text = f"Attempt {attempts} failed (will retry): {message}"
        self.event(po_id, job_id, ref, "error", "po.failed" if state == "failed" else "po.error", text, details)

        signature = type(exc).__name__ + ":" + message[:300]
        if rec is not None and rec["alerted_error"] == signature and state != "failed":
            return  # already told them about this exact problem
        order_id = rec["order_id"] if rec else None
        lines = [
            f"The 3PL Central order for Syncore job {job_id} ({ref or f'PO id {po_id}'}) could not be finished.",
            f"3PL transaction: {order_id}" if order_id else "No 3PL order has been created yet.",
            "",
            f"Error: {message}",
            "",
            ("It has failed repeatedly and will no longer be retried automatically. "
             "After fixing the cause, click Retry on the admin dashboard.") if state == "failed"
            else ("It will go through automatically once the client is finished on the Clients page."
                  if isinstance(exc, ClientNotReady) else "It will be retried automatically on the next run."),
        ]
        if self._send_alert(po_id, job_id, ref, f"[3PL] Problem with order {ref or f'PO id {po_id}'}", lines):
            self.db.upsert(po_id, job_id, alerted_error=signature)
