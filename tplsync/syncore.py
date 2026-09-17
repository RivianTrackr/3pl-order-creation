"""Syncore v2 Orders API client (https://docs.syncore.app)."""

from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional

import requests

from .http import request


class SyncoreClient:
    def __init__(self, api_key: str, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": api_key, "Accept": "application/json"})
        self._job_po_cache: Dict[int, List[dict]] = {}

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = request(self.session, "Syncore", "GET", f"{self.base_url}{path}", params=params)
        return resp.json() if resp.content else {}

    def search_purchase_orders(self, created_from: datetime, modified_from: datetime) -> Iterator[dict]:
        """Brief PO records created on/after created_from and modified on/after modified_from."""
        page = 1
        while True:
            data = self._get("/jobs/purchaseorders", {
                "date_from": created_from.strftime("%Y-%m-%dT%H:%M:%S"),
                "last_modified_date_from": modified_from.strftime("%Y-%m-%dT%H:%M:%S"),
                "page": page,
                "count": 100,
            })
            pos = data.get("purchaseorders") or []
            if isinstance(pos, dict):  # the spec is inconsistent about object vs array
                pos = [pos]
            yield from pos
            if len(pos) < 100 or not (data.get("links") or {}).get("next"):
                return
            page += 1

    def test_connection(self) -> str:
        since = datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
        data = self._get("/jobs/purchaseorders", {"date_from": since.strftime("%Y-%m-%dT%H:%M:%S"), "count": 1})
        total = data.get("total_results")
        return "Connected to Syncore." + (f" {total} PO(s) created today." if isinstance(total, int) and total >= 0 else "")

    @property
    def crm_url(self) -> str:
        return self.base_url.rsplit("/", 1)[0] + "/crm"

    def get_contact(self, contact_id) -> dict:
        resp = request(self.session, "Syncore", "GET", f"{self.crm_url}/contacts/{contact_id}")
        return resp.json() if resp.content else {}

    def list_client_groups(self) -> List[dict]:
        resp = request(self.session, "Syncore", "GET", f"{self.crm_url}/client-groups")
        data = resp.json() if resp.content else []
        if isinstance(data, dict):
            data = data.get("client_groups") or data.get("clientGroups") or []
        return [g for g in data if g.get("id") and g.get("name")]

    def get_job(self, job_id: int) -> dict:
        return self._get(f"/jobs/{job_id}")

    def get_job_purchase_orders(self, job_id: int) -> List[dict]:
        if job_id not in self._job_po_cache:
            pos, page = [], 1
            while True:
                data = self._get(f"/jobs/{job_id}/purchaseorders", {"page": page, "count": 10})
                batch = data.get("purchaseorders") or []
                if isinstance(batch, dict):
                    batch = [batch]
                pos.extend(batch)
                if len(batch) < 10 or not (data.get("links") or {}).get("next"):
                    break
                page += 1
            self._job_po_cache[job_id] = pos
        return self._job_po_cache[job_id]

    def get_purchase_order(self, job_id: int, po_id: int, fresh: bool = False) -> Optional[dict]:
        if fresh:
            self._job_po_cache.pop(job_id, None)
        for po in self.get_job_purchase_orders(job_id):
            if int(po.get("id", 0)) == int(po_id):
                return po
        return None

    def update_critical_comments(self, job_id: int, po_id: int, comments: str) -> None:
        request(self.session, "Syncore", "PUT",
                f"{self.base_url}/jobs/{job_id}/purchaseorders/{po_id}",
                json={"critical_comments": comments})
        self._job_po_cache.pop(job_id, None)
