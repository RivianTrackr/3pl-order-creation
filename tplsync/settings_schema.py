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
    ("emails", "Emails", "fa-solid fa-bell"),
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
               help="Regular expression. PO lines whose SKU matches are included (case-sensitive). Older SKUs "
                    "without INV or OD go on the Allowed SKUs page."),
    SettingDef("SYNCORE_JOB_STATUSES", "Job statuses to send", "syncore", default="Submitted",
               help="A PO is only sent once its Syncore job is in one of these statuses (comma-separated). "
                    "In any other status (e.g. Pending or WIP) it's held and checked again every run."),
    SettingDef("SYNCORE_JOB_STATUSES_IGNORE", "Job statuses to ignore", "syncore", default="Delivered, Completed",
               help="Jobs in these statuses are already done: their POs are skipped and not checked again "
                    "(comma-separated)."),

    SettingDef("TPL_BASE_URL", "API base URL", "tpl", default="https://secure-wms.com"),
    SettingDef("TPL_USER_LOGIN", "User login", "tpl", required=True,
               help="A user in the warehouse's 3PL Central that orders are created as: the login name you'd type "
                    "to sign in to 3PL Manager, or its numeric user login ID. It needs permission to create orders. "
                    "Used for all clients unless a client overrides it."),
    SettingDef("TPL_FACILITY_NAME", "Warehouse name", "tpl", required=True,
               help="The warehouse in 3PL Central that orders are created for, exactly as it's named there."),
    SettingDef("TPL_FACILITY_ID", "Warehouse ID", "tpl", "int",
               help="Optional. When set, used instead of the warehouse name."),
    SettingDef("TPL_BILLING_CODE", "Default billing code", "tpl", default="Prepaid",
               help="Prepaid, FreightCollect or BillThirdParty. Syncore POs don't carry a billing type. Carriers "
                    "that name these differently get their own equivalent, e.g. BillThirdParty is sent to a FedEx "
                    "that uses \"third party\" as \"third party\"."),
    SettingDef("TPL_DEFAULT_CARRIER", "Carrier when Ship Via is blank", "tpl",
               help="Only used for POs with no Ship Via, e.g. USPS. Must be a carrier set up in 3PL Central."),
    SettingDef("TPL_DEFAULT_MODE", "Service when Ship Via is blank", "tpl",
               help="Service name or code for that carrier, e.g. Priority Mail."),

    SettingDef("SENDGRID_API_KEY", "SendGrid API key", "alerts", "secret", required=True),
    SettingDef("EMAIL_FROM", "From address", "alerts", "email", required=True,
               help="Must be a verified sender in SendGrid."),
    SettingDef("ADMIN_URL", "Admin address", "alerts",
               help="This admin's web address, e.g. https://3pl.ipromo.com. Emails link to it."),
    SettingDef("ALERT_EMAIL_TO", "Alert recipients", "alerts", required=True,
               help="Who gets alerts about orders that need attention. Comma-separated."),

    SettingDef("PAUSED", "Pause processing", "behaviour", "bool", default="false",
               help="Scheduled runs do nothing while paused."),
    SettingDef("DRY_RUN", "Dry run", "behaviour", "bool", default="false",
               help="Log what would be sent without creating orders in 3PL Central or sending emails."),
    SettingDef("AUTO_COMPLETE_OPEN", "Complete open orders when stock arrives", "behaviour", "bool", default="true",
               help="Re-check orders left Open for short stock on every run and complete them once the warehouse "
                    "has enough inventory."),
    SettingDef("PO_SETTLE_MINUTES", "Settle time (minutes)", "behaviour", "int", default="15",
               help="Wait until a PO has been unchanged this long before sending it."),
    SettingDef("LOOKBACK_HOURS", "Lookback (hours)", "behaviour", "int", default="72",
               help="How far back each run searches for modified POs."),
    # Which alert emails go out. A muted alert is still recorded in the PO's timeline.
    SettingDef("EMAIL_SHORT_STOCK", "Order left Open for short stock", "emails", "bool", default="true",
               help="An order was created but not completed because the warehouse doesn't have enough inventory."),
    SettingDef("EMAIL_STOCK_ARRIVED", "Open order completed after stock arrived", "emails", "bool", default="true",
               help="An order left Open was completed automatically once the inventory came in."),
    SettingDef("EMAIL_ERRORS", "Problems that will be retried", "emails", "bool", default="true",
               help="A PO couldn't be sent yet, e.g. an unmatched SKU or Ship Via, or 3PL Central or Syncore "
                    "returned an error. Sent once per distinct problem."),
    SettingDef("EMAIL_FAILED", "POs that stopped retrying", "emails", "bool", default="true",
               help="A PO failed 20 times and needs Retry on the dashboard once the cause is fixed."),
    SettingDef("EMAIL_CLIENT_PAUSED", "PO for a paused client", "emails", "bool", default="false",
               help="A PO arrived for a client that's paused on the Clients page. It's sent automatically once "
                    "the client is activated."),
    SettingDef("EMAIL_RUN_FAILURES", "Runs failing, and back to normal", "emails", "bool", default="true",
               help="Scheduled runs failed 3 times in a row (e.g. an API key was rejected), so no POs are being sent. "
                    "One email when it starts and one when runs work again."),
    SettingDef("DAILY_SUMMARY", "Daily summary email", "emails", "bool", default="true",
               help="One email a day listing what was created, what's waiting for stock and what needs attention."),
    SettingDef("SUMMARY_HOUR", "Send summary at (hour)", "emails", "int", default="8",
               help="0-23 in the display time zone. The summary goes out on the first run after this hour."),
    SettingDef("SUMMARY_EMAIL_TO", "Summary recipients", "emails",
               help="Comma-separated. Leave blank to use the alert recipients."),
    SettingDef("BACKUP_KEEP_DAYS", "Keep database backups (days)", "behaviour", "int", default="14",
               help="A copy of the database is made once a day. 0 turns backups off."),
    SettingDef("DISPLAY_TIMEZONE", "Display time zone", "behaviour", default="America/New_York"),
    SettingDef("LOG_RETENTION_DAYS", "Keep logs (days)", "behaviour", "int", default="90",
               help="Run logs, PO timelines and the email log older than this are deleted. PO status is kept."),
)

BY_KEY = {s.key: s for s in SETTINGS}
