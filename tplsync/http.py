"""Shared HTTP session with timeouts and retry/backoff for safe requests."""

import json
import logging
import time
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

TIMEOUT = 45
RETRY_STATUSES = {429, 500, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, service: str, method: str, url: str, status: int, body: str):
        self.service = service
        self.status = status
        self.body = body
        self.error_code = _error_code(body)
        super().__init__(f"{service} {method} {url} -> HTTP {status}: {body[:1000]}")


def _error_code(body: str):
    try:
        data = json.loads(body)
        return data.get("ErrorCode") or data.get("errorCode")
    except (ValueError, AttributeError):
        return None


def request(session: requests.Session, service: str, method: str, url: str,
            retries: int = 3, **kwargs) -> requests.Response:
    """Send a request. GETs are retried on transient errors; writes are not
    (the processor's idempotency checks cover re-running a failed write)."""
    kwargs.setdefault("timeout", TIMEOUT)
    attempts = retries if method.upper() == "GET" else 1
    shown = url + ("?" + urlencode(kwargs["params"]) if kwargs.get("params") else "")
    if method.upper() != "GET" and kwargs.get("json") is not None:
        log.info("%s %s %s request body: %s", service, method, shown, json.dumps(kwargs["json"])[:8000])
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            resp = session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            if attempt == attempts:
                raise ApiError(service, method, url, 0, str(exc)) from exc
            log.warning("%s %s %s failed (%s), retrying", service, method, shown, exc)
        else:
            elapsed = int((time.monotonic() - started) * 1000)
            log.log(logging.INFO if resp.status_code < 400 else logging.WARNING,
                    "%s %s %s -> HTTP %s (%d ms)", service, method, shown, resp.status_code, elapsed)
            if resp.status_code < 400:
                if method.upper() != "GET" and resp.content:
                    log.info("%s %s response body: %s", service, method, resp.text[:4000])
                return resp
            if resp.status_code not in RETRY_STATUSES or attempt == attempts:
                raise ApiError(service, method, url, resp.status_code, resp.text)
            log.warning("Retrying %s %s", service, shown)
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")
