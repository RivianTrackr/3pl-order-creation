"""Editable settings: one definition drives the loader, defaults and the admin form."""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    section: str
    kind: str = "text"          # text | secret | int | bool | email
    default: Optional[str] = None
    help: str = ""
    required: bool = False


SECTIONS: Tuple[Tuple[str, str, str], ...] = (
    ("syncore", "Syncore", "fa-solid fa-cloud"),
    ("tpl", "3PL Central", "fa-solid fa-warehouse"),
    ("alerts", "Alerts", "fa-solid fa-envelope"),
    ("behaviour", "Behaviour", "fa-solid fa-sliders"),
)

SETTINGS: Tuple[SettingDef, ...] = (
    SettingDef("SYNCORE_API_KEY", "API key", "syncore", "secret", required=True,
               help="Generated in Syncore under Settings > API."),
    SettingDef("SYNCORE_BASE_URL", "API base URL", "syncore", default="https://api.syncore.app/v2/orders"),
    SettingDef("SYNCORE_VENDOR_NAME", "Vendor name", "syncore", required=True,
               help="Only POs from this Syncore vendor are sent to 3PL Central (case-insensitive), e.g. "
                    "the supplier that holds your inventory."),
    SettingDef("SKU_PATTERN", "SKU pattern", "syncore", default="INV|OD", required=True,
               help="Regular expression. PO lines whose SKU matches are included (case-sensitive)."),

    SettingDef("TPL_BASE_URL", "API base URL", "tpl", default="https://secure-wms.com"),
    SettingDef("TPL_USER_LOGIN", "User login", "tpl", required=True,
               help="3PL Central user login (or numeric login ID) used for all clients unless a client overrides it."),
    SettingDef("TPL_FACILITY_NAME", "Warehouse name", "tpl", required=True,
               help="The warehouse in 3PL Central that orders are created for, exactly as it's named there."),
    SettingDef("TPL_FACILITY_ID", "Warehouse ID", "tpl", "int",
               help="Optional. When set, used instead of the warehouse name."),
    SettingDef("TPL_BILLING_CODE", "Default billing code", "tpl", default="Prepaid",
               help="Prepaid, FreightCollect or BillThirdParty. Syncore POs don't carry a billing type."),
    SettingDef("TPL_DEFAULT_CARRIER", "Carrier when Ship Via is blank", "tpl",
               help="Only used for POs with no Ship Via, e.g. USPS. Must be a carrier set up in 3PL Central."),
    SettingDef("TPL_DEFAULT_MODE", "Service when Ship Via is blank", "tpl",
               help="Service name or code for that carrier, e.g. Priority Mail."),

    SettingDef("SENDGRID_API_KEY", "SendGrid API key", "alerts", "secret", required=True),
    SettingDef("EMAIL_FROM", "From address", "alerts", "email", required=True,
               help="Must be a verified sender in SendGrid."),
    SettingDef("ALERT_EMAIL_TO", "Alert recipients", "alerts", required=True,
               help="Who gets alerts about orders that need attention. Comma-separated."),

    SettingDef("PAUSED", "Pause processing", "behaviour", "bool", default="false",
               help="Scheduled runs do nothing while paused."),
    SettingDef("DRY_RUN", "Dry run", "behaviour", "bool", default="false",
               help="Log what would be sent without creating orders, updating Syncore or emailing."),
    SettingDef("AUTO_COMPLETE_OPEN", "Complete open orders when stock arrives", "behaviour", "bool", default="true",
               help="Re-check orders left Open for short stock on every run and complete them once the warehouse "
                    "has enough inventory."),
    SettingDef("PO_SETTLE_MINUTES", "Settle time (minutes)", "behaviour", "int", default="15",
               help="Wait until a PO has been unchanged this long before sending it."),
    SettingDef("LOOKBACK_HOURS", "Lookback (hours)", "behaviour", "int", default="72",
               help="How far back each run searches for modified POs."),
    SettingDef("DAILY_SUMMARY", "Daily summary email", "alerts", "bool", default="true",
               help="One email a day listing what was created, what's waiting for stock and what needs attention."),
    SettingDef("SUMMARY_HOUR", "Send summary at (hour)", "alerts", "int", default="8",
               help="0-23 in the display time zone. The summary goes out on the first run after this hour."),
    SettingDef("SUMMARY_EMAIL_TO", "Summary recipients", "alerts",
               help="Comma-separated. Leave blank to use the alert recipients."),
    SettingDef("BACKUP_KEEP_DAYS", "Keep database backups (days)", "behaviour", "int", default="14",
               help="A copy of the database is made once a day. 0 turns backups off."),
    SettingDef("DISPLAY_TIMEZONE", "Display time zone", "behaviour", default="America/New_York"),
    SettingDef("LOG_RETENTION_DAYS", "Keep logs (days)", "behaviour", "int", default="90",
               help="Run logs, PO timelines and the email log older than this are deleted. PO status is kept."),
)

BY_KEY = {s.key: s for s in SETTINGS}
