"""3PL Central (Extensiv 3PL Warehouse Manager) REST API client."""

import base64
import time
from typing import List, Optional, Tuple

import requests

from .config import TplClient
from .http import ApiError, request

TOKEN_TTL_SECONDS = 45 * 60  # docs say tokens last 30-60 min; refresh early


class TplCentralClient:
    def __init__(self, base_url: str, creds: TplClient, default_user_login: str):
        self.base_url = base_url.rstrip("/")
        self.creds = creds
        self.user_login = creds.user_login or default_user_login
        self.session = requests.Session()
        self._token: Optional[str] = None
        self._token_at = 0.0

    # --- auth -------------------------------------------------------------

    def _authenticate(self) -> None:
        basic = base64.b64encode(f"{self.creds.client_id}:{self.creds.client_secret}".encode()).decode()
        body = {"grant_type": "client_credentials"}
        if str(self.user_login).isdigit():
            body["user_login_id"] = int(self.user_login)
        else:
            body["user_login"] = self.user_login
        resp = request(self.session, "3PL Central", "POST", f"{self.base_url}/AuthServer/api/Token",
                       json=body, headers={
                           "Authorization": f"Basic {basic}",
                           "Content-Type": "application/json; charset=utf-8",
                           "Accept": "application/json",
                       })
        self._token = resp.json()["access_token"]
        self._token_at = time.monotonic()

    def _call(self, method: str, path: str, **kwargs) -> requests.Response:
        if not self._token or time.monotonic() - self._token_at > TOKEN_TTL_SECONDS:
            self._authenticate()
        headers = {"Accept": "application/hal+json", "Content-Type": "application/hal+json; charset=utf-8"}
        headers.update(kwargs.pop("headers", {}))
        for attempt in (1, 2):
            headers["Authorization"] = f"Bearer {self._token}"
            try:
                return request(self.session, "3PL Central", method, f"{self.base_url}{path}",
                               headers=headers, **kwargs)
            except ApiError as exc:
                if exc.status == 401 and attempt == 1:
                    self._authenticate()
                    continue
                raise
        raise AssertionError("unreachable")

    def test_connection(self) -> str:
        """Authenticate and look up the customer. Returns a short description."""
        self._authenticate()
        try:
            data = self._call("GET", f"/customers/{self.creds.customer_id}").json()
        except ApiError as exc:
            return (f"Signed in, but customer {self.creds.customer_id} could not be read "
                    f"(HTTP {exc.status}). Check the Customer ID.")
        company = data.get("companyInfo") or data.get("CompanyInfo") or {}
        name = company.get("companyName") or company.get("CompanyName") or data.get("name") or ""
        return f"Signed in. Customer {self.creds.customer_id}" + (f" is {name}." if name else " found.")

    def list_customers(self, facility_id: Optional[int] = None) -> List[Tuple[int, str]]:
        """(customer id, name) for every customer this login can see."""
        customers, page = [], 1
        while True:
            params = {"pgsiz": 100, "pgnum": page}
            if facility_id:
                params["facilityid"] = facility_id
            data = self._call("GET", "/customers", params=params).json()
            embedded = data.get("_embedded") or {}
            batch = next((v for k, v in embedded.items() if k.lower().endswith("/customers/customer")), None) \
                or data.get("ResourceList") or data.get("resourceList") or []
            for c in batch:
                ro = c.get("readOnly") or c.get("ReadOnly") or {}
                company = c.get("companyInfo") or c.get("CompanyInfo") or {}
                cid = ro.get("customerId") or ro.get("CustomerId") or c.get("customerId") or c.get("id")
                name = company.get("companyName") or company.get("CompanyName") or c.get("name") or ""
                if cid and name and not (ro.get("deactivated") or c.get("deactivated")):
                    customers.append((int(cid), name.strip()))
            total = data.get("totalResults") or data.get("TotalResults") or 0
            if not batch or len(customers) >= total or len(batch) < 100:
                return customers
            page += 1

    def get_carriers(self) -> dict:
        """Carriers, their service codes and billing codes configured in 3PL Central."""
        return self._call("GET", "/properties/carriers").json()

    # --- orders -----------------------------------------------------------

    def _orders_matching(self, rql: str) -> List[dict]:
        resp = self._call("GET", "/orders", params={"pgsiz": 100, "rql": rql})
        data = resp.json()
        return (data.get("_embedded") or {}).get("http://api.3plCentral.com/rels/orders/order") \
            or data.get("ResourceList") or []

    def find_existing_order(self, reference: str) -> Optional[Tuple[int, str]]:
        """An order this customer already has with that reference or PO number.
        Returns (order id, which field matched)."""
        for field, rql in (("reference number", f"referenceNum=={reference}"), ("PO number", f"poNum=={reference}")):
            try:
                orders = self._orders_matching(rql)
            except ApiError as exc:
                if field == "PO number" and exc.status == 400:
                    continue   # not every warehouse allows searching on poNum
                raise
            for order in orders:
                ro = order.get("readOnly") or order.get("ReadOnly") or {}
                customer = ro.get("customerIdentifier") or ro.get("CustomerIdentifier") or {}
                status = ro.get("status", ro.get("Status"))
                if (int(customer.get("id") or customer.get("Id") or 0) == self.creds.customer_id
                        and status != 2):  # 2 = canceled
                    return int(ro.get("orderId") or ro.get("OrderId")), field
        return None

    def list_items(self) -> List[dict]:
        """Every item set up for this customer (sku, description)."""
        items, page = [], 1
        while True:
            data = self._call("GET", f"/customers/{self.creds.customer_id}/items",
                              params={"pgsiz": 100, "pgnum": page}).json()
            batch = (data.get("_embedded") or {}).get("http://api.3plCentral.com/rels/customers/item") \
                or data.get("ResourceList") or []
            items += [i for i in batch if not (i.get("readOnly") or {}).get("deactivated")]
            if len(batch) < 100:
                return items
            page += 1

    def find_item(self, sku: str) -> Optional[dict]:
        """The customer's item with that SKU, if it exists in 3PL Central."""
        resp = self._call("GET", f"/customers/{self.creds.customer_id}/items",
                          params={"pgsiz": 10, "rql": f"sku=={sku}"})
        data = resp.json()
        items = (data.get("_embedded") or {}).get("http://api.3plCentral.com/rels/customers/item") \
            or data.get("ResourceList") or []
        for item in items:
            if str(item.get("sku") or item.get("Sku") or "").casefold() == sku.casefold():
                return item
        return None

    def create_order(self, payload: dict) -> int:
        resp = self._call("POST", "/orders", json=payload)
        ro = resp.json().get("readOnly") or {}
        return int(ro["orderId"])

    def get_order(self, order_id: int) -> Tuple[dict, str]:
        resp = self._call("GET", f"/orders/{order_id}",
                          params={"detail": "OrderItems", "itemdetail": "Allocations"})
        return resp.json(), resp.headers.get("ETag", "")

    def complete_order(self, order_id: int) -> None:
        _, etag = self.get_order(order_id)
        self._call("POST", f"/orders/{order_id}/completer", headers={"If-Match": etag})

    def stock_for_order(self, order_id: int) -> List[dict]:
        resp = self._call("GET", "/inventory/stocksummariesfororder", params={"orderid": order_id})
        data = resp.json()
        return data.get("summaries") or data.get("Summaries") or []
