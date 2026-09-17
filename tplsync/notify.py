"""Alert emails via SendGrid."""

import html
import logging
from typing import Callable, Optional

import requests

from .http import request

log = logging.getLogger(__name__)

# recorder(subject, recipients, body, status, error, po_id) - status is sent | failed | dry_run
Recorder = Callable[[str, str, str, str, Optional[str], Optional[int]], None]


class Notifier:
    def __init__(self, api_key: str, email_from: str, email_to: str, dry_run: bool = False,
                 recorder: Optional[Recorder] = None):
        self.api_key = api_key
        self.email_from = email_from
        self.email_to = email_to
        self.dry_run = dry_run
        self.recorder = recorder
        self.session = requests.Session()

    def _record(self, subject: str, text: str, status: str, error: Optional[str], po_id: Optional[int]) -> None:
        if self.recorder:
            try:
                self.recorder(subject, self.email_to, text, status, error, po_id)
            except Exception:  # noqa: BLE001
                log.exception("Could not record email in the log")

    def send(self, subject: str, lines: list, po_id: Optional[int] = None) -> None:
        text = "\n".join(lines)
        if self.dry_run:
            log.info("[dry-run] would email %s: %s\n%s", self.email_to, subject, text)
            self._record(subject, text, "dry_run", None, po_id)
            return
        body_html = "<br>".join(html.escape(line) for line in lines)
        try:
            request(self.session, "SendGrid", "POST", "https://api.sendgrid.com/v3/mail/send",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "from": {"email": self.email_from},
                        "personalizations": [{"to": [{"email": addr.strip()} for addr in self.email_to.split(",")]}],
                        "subject": subject,
                        "content": [
                            {"type": "text/plain", "value": text},
                            {"type": "text/html", "value": f"<div style=\"font-family:sans-serif\">{body_html}</div>"},
                        ],
                    })
        except Exception as exc:
            self._record(subject, text, "failed", str(exc)[:1000], po_id)
            raise
        self._record(subject, text, "sent", None, po_id)
        log.info("Alert emailed to %s: %s", self.email_to, subject)
