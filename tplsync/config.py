"""Configuration.

Server bootstrap values (database path, encryption and session keys) come from
.env. Everything else - API keys, client credentials, shipping rules - lives in
the database and is managed through the admin UI.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import settings_schema
from .crypto import SecretBox
from .db import Database, client_missing, client_status

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


@dataclass
class Bootstrap:
    db_path: str
    encryption_key: str
    admin_secret_key: str
    admin_host: str
    admin_port: int
    cookie_secure: bool
    backup_dir: str = str(ROOT / "backups")

    def secret_box(self) -> SecretBox:
        return SecretBox(self.encryption_key)


def load_bootstrap(require_keys: bool = True) -> Bootstrap:
    if load_dotenv:
        load_dotenv(ROOT / ".env")
    boot = Bootstrap(
        db_path=os.environ.get("DB_PATH") or str(ROOT / "tplsync.db"),
        encryption_key=os.environ.get("TPLSYNC_ENCRYPTION_KEY", "").strip(),
        admin_secret_key=os.environ.get("ADMIN_SECRET_KEY", "").strip(),
        admin_host=os.environ.get("ADMIN_HOST", "127.0.0.1"),
        admin_port=int(os.environ.get("ADMIN_PORT", "8120")),
        cookie_secure=os.environ.get("ADMIN_COOKIE_SECURE", "true").lower() in ("1", "true", "yes"),
        backup_dir=os.environ.get("BACKUP_DIR") or str(ROOT / "backups"),
    )
    if require_keys:
        missing = [n for n, v in (("TPLSYNC_ENCRYPTION_KEY", boot.encryption_key),
                                  ("ADMIN_SECRET_KEY", boot.admin_secret_key)) if not v]
        if missing:
            raise ConfigError(f"Missing {', '.join(missing)} in .env - run `python -m tplsync gen-keys`")
    return boot


@dataclass
class TplClient:
    """3PL Central credentials for one Syncore client."""

    name: str
    customer_id: int
    client_id: str
    client_secret: str
    user_login: Optional[str] = None
    active: bool = True


@dataclass
class Settings:
    syncore_api_key: str
    syncore_base_url: str
    vendor_name: str
    sku_pattern: str

    tpl_base_url: str
    tpl_user_login: str
    tpl_facility_name: str
    tpl_facility_id: Optional[int]
    tpl_billing_code: str
    tpl_default_carrier: Optional[str]
    tpl_default_mode: Optional[str]

    sendgrid_api_key: str
    email_from: str
    alert_email_to: str

    db_path: str
    settle_minutes: int
    lookback_hours: int
    dry_run: bool
    auto_complete_open: bool = True
    daily_summary: bool = True
    summary_hour: int = 8
    summary_email_to: str = ""
    backup_keep_days: int = 14
    paused: bool = False
    display_timezone: str = "America/New_York"

    clients: Dict[str, TplClient] = field(default_factory=dict)
    shipping_map: Dict[str, dict] = field(default_factory=dict)


def client_credentials(row, box: SecretBox) -> "TplClient":
    return TplClient(name=row["name"], customer_id=int(row["customer_id"]), client_id=row["client_id"],
                     client_secret=box.decrypt(row["client_secret_enc"]), user_login=row["user_login"] or None,
                     active=bool(row["active"]))


def any_ready_client(db: Database, box: SecretBox) -> Optional["TplClient"]:
    """Credentials of an active, fully set-up client (for warehouse-wide lookups)."""
    row = next((r for r in db.list_clients() if client_status(r) == "ready"), None)
    return client_credentials(row, box) if row else None


def setting_values(db: Database, box: SecretBox) -> Dict[str, Optional[str]]:
    """Current value of every defined setting (secrets decrypted, defaults applied)."""
    raw = db.settings_raw()
    values = {}
    for d in settings_schema.SETTINGS:
        value = raw.get(d.key)
        if value and d.kind == "secret":
            value = box.decrypt(value)
        values[d.key] = value if value not in (None, "") else d.default
    return values


def missing_required(values: Dict[str, Optional[str]]) -> List[str]:
    section_names = {key: name for key, name, _ in settings_schema.SECTIONS}
    return [f"{section_names[d.section]}: {d.label}"
            for d in settings_schema.SETTINGS if d.required and not values.get(d.key)]


def shipping_overrides(db: Database) -> Dict[str, dict]:
    """Ship Via overrides keyed by normalised Ship Via text."""
    return {
        " ".join(r["ship_via"].split()).casefold(): {
            "carrier": r["carrier"], "mode": r["mode"], "scacCode": r["scac_code"],
            "account": r["account"], "billingCode": r["billing_code"],
        } for r in db.list_shipping_rules()
    }


def load_settings(db: Database, box: SecretBox, db_path: str, require_complete: bool = True) -> Settings:
    v = setting_values(db, box)
    if require_complete:
        missing = missing_required(v)
        if missing:
            raise ConfigError("Settings incomplete in the admin UI: " + ", ".join(missing))

    def as_int(key: str, fallback: Optional[int]) -> Optional[int]:
        try:
            return int(v[key]) if v.get(key) else fallback
        except ValueError:
            raise ConfigError(f"Setting {key} must be a whole number")

    clients = {}
    for row in db.list_clients():
        if row["syncore_group_id"] and not client_missing(row):
            clients[str(row["syncore_group_id"])] = client_credentials(row, box)

    shipping_map = shipping_overrides(db)

    return Settings(
        syncore_api_key=v["SYNCORE_API_KEY"] or "",
        syncore_base_url=v["SYNCORE_BASE_URL"],
        vendor_name=v["SYNCORE_VENDOR_NAME"] or "",
        sku_pattern=v["SKU_PATTERN"] or "",
        tpl_base_url=v["TPL_BASE_URL"],
        tpl_user_login=v["TPL_USER_LOGIN"] or "",
        tpl_facility_name=v["TPL_FACILITY_NAME"] or "",
        tpl_facility_id=as_int("TPL_FACILITY_ID", None),
        tpl_billing_code=v["TPL_BILLING_CODE"] or "Prepaid",
        tpl_default_carrier=v["TPL_DEFAULT_CARRIER"],
        tpl_default_mode=v["TPL_DEFAULT_MODE"],
        sendgrid_api_key=v["SENDGRID_API_KEY"] or "",
        email_from=v["EMAIL_FROM"] or "",
        alert_email_to=v["ALERT_EMAIL_TO"] or "",
        db_path=db_path,
        settle_minutes=as_int("PO_SETTLE_MINUTES", 15),
        lookback_hours=as_int("LOOKBACK_HOURS", 72),
        dry_run=(v["DRY_RUN"] or "false") == "true",
        auto_complete_open=(v["AUTO_COMPLETE_OPEN"] or "true") == "true",
        daily_summary=(v["DAILY_SUMMARY"] or "true") == "true",
        summary_hour=min(max(as_int("SUMMARY_HOUR", 8), 0), 23),
        summary_email_to=v["SUMMARY_EMAIL_TO"] or "",
        backup_keep_days=max(as_int("BACKUP_KEEP_DAYS", 14), 0),
        paused=(v["PAUSED"] or "false") == "true",
        display_timezone=v["DISPLAY_TIMEZONE"] or "America/New_York",
        clients=clients,
        shipping_map=shipping_map,
    )
