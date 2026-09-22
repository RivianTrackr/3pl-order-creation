"""Admin web UI: settings, 3PL clients, shipping rules, PO activity, users."""

import json
import os
import re
import select
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import settings_schema
from .. import carriers as carrier_match
from .. import clientmatch
from .. import maintenance
from ..config import (ROOT, Bootstrap, ConfigError, TplClient, any_ready_client, load_settings,
                      missing_required, setting_values, shipping_overrides)
from ..db import Database, client_missing, client_status, company_key, utcnow
from ..http import ApiError
from ..notify import Notifier
from ..syncore import SyncoreClient
from ..tpl import TplCentralClient
from .auth import (FLASH_COOKIE, SESSION_COOKIE, SESSION_MAX_AGE, LoginThrottle, Signer, hash_password,
                   password_problem, verify_login)

HERE = Path(__file__).resolve().parent
PUBLIC_PATHS = ("/login", "/static/", "/healthz")
STATE_LABELS = {
    "waiting": "Waiting", "processing": "Processing", "error": "Retrying", "failed": "Failed",
    "done": "Done", "skipped": "Skipped", "dismissed": "Dismissed",
}


def create_app(boot: Bootstrap) -> FastAPI:
    app = FastAPI(title="3PL Order Sync", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    signer = Signer(boot.admin_secret_key)
    throttle = LoginThrottle()
    box = boot.secret_box()

    # --- per-request helpers --------------------------------------------------

    def get_db():
        db = Database(boot.db_path)
        try:
            yield db
        finally:
            db.close()

    def _zone(name: Optional[str]) -> ZoneInfo:
        try:
            return ZoneInfo(name or "America/New_York")
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    def render(request: Request, db: Database, template: str, status_code: int = 200, **context):
        try:
            values = setting_values(db, box)
        except ValueError:
            values = {}
        tz = _zone(values.get("DISPLAY_TIMEZONE"))

        def localtime(value: Optional[str], seconds: bool = False) -> str:
            if not value:
                return ""
            try:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return str(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(tz).strftime("%-I:%M:%S %p" if seconds else "%b %-d, %Y %-I:%M %p")

        flash = None
        if request.cookies.get(FLASH_COOKIE):
            flash = signer.read_flash(request.cookies[FLASH_COOKIE])
        response = templates.TemplateResponse(request, template, {
            "user": getattr(request.state, "user", None),
            "csrf": getattr(request.state, "csrf", ""),
            "flash": flash,
            "localtime": localtime,
            "tz_name": getattr(tz, "key", "UTC"),
            "state_labels": STATE_LABELS,
            "vendor_name": values.get("SYNCORE_VENDOR_NAME") or "the configured vendor",
            "warehouse_name": values.get("TPL_FACILITY_NAME") or "the warehouse",
            "nav": template.split(".")[0].split("_")[0],
            **context,
        }, status_code=status_code)
        if flash:
            response.delete_cookie(FLASH_COOKIE, path="/")
        return response

    def redirect(url: str, message: Optional[str] = None, kind: str = "success") -> RedirectResponse:
        response = RedirectResponse(url, status_code=303)
        if message:
            response.set_cookie(FLASH_COOKIE, signer.flash_token(message, kind), httponly=True,
                                samesite="lax", secure=boot.cookie_secure, max_age=120, path="/")
        return response

    async def csrf_protect(request: Request):
        form = await request.form()
        nonce = getattr(request.state, "nonce", None)
        if not nonce or not signer.csrf_valid(nonce, form.get("_csrf")):
            raise HTTPException(status_code=403, detail="Your session expired. Reload the page and try again.")

    def username(request: Request) -> str:
        return request.state.user["username"]

    # --- middleware -------------------------------------------------------------

    @app.middleware("http")
    async def auth_and_headers(request: Request, call_next):
        path = request.url.path
        public = any(path == p or (p.endswith("/") and path.startswith(p)) for p in PUBLIC_PATHS)
        refreshed = None

        if not public:
            session = signer.read_session(request.cookies.get(SESSION_COOKIE, ""))
            user = None
            if session:
                db = Database(boot.db_path)
                try:
                    user = db.get_user(session["uid"])
                finally:
                    db.close()
            if not user or user["session_version"] != session.get("sv"):
                response = RedirectResponse("/login", status_code=303)
                response.delete_cookie(SESSION_COOKIE, path="/")
                return _secure_headers(response)
            request.state.user = dict(user)
            request.state.nonce = session["n"]
            request.state.csrf = signer.csrf_token(session["n"])
            refreshed = signer.session_token(user["id"], user["session_version"], session["n"])

        response = await call_next(request)
        if refreshed and not response.headers.get("x-logout"):
            response.set_cookie(SESSION_COOKIE, refreshed, httponly=True, samesite="lax",
                                secure=boot.cookie_secure, max_age=SESSION_MAX_AGE, path="/")
        return _secure_headers(response)

    def _secure_headers(response: Response) -> Response:
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' https://cdnjs.cloudflare.com; "
            "font-src https://cdnjs.cloudflare.com; img-src 'self' data:; script-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code in (403, 404):
            db = Database(boot.db_path)
            try:
                return render(request, db, "error.html", status_code=exc.status_code, message=exc.detail)
            finally:
                db.close()
        return HTMLResponse(str(exc.detail), status_code=exc.status_code)

    # --- auth -----------------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, db: Database = Depends(get_db)):
        session = signer.read_session(request.cookies.get(SESSION_COOKIE, ""))
        user = db.get_user(session["uid"]) if session else None
        if user and user["session_version"] == session.get("sv"):
            return RedirectResponse("/", status_code=303)
        return render(request, db, "login.html", no_users=not db.list_users())

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(request: Request, username_: str = Form(..., alias="username"), password: str = Form(...),
                     db: Database = Depends(get_db)):
        ip = request.client.host if request.client else "unknown"
        if throttle.blocked(ip, username_):
            return render(request, db, "login.html", status_code=429,
                          error="Too many failed attempts. Try again in 15 minutes.")
        user = db.get_user_by_name(username_.strip())
        if not verify_login(password, user["password_hash"] if user else None):
            throttle.fail(ip, username_)
            db.audit(username_[:60], "login.failed", ip)
            return render(request, db, "login.html", status_code=401, error="Incorrect username or password.")
        throttle.clear(ip, username_)
        db.touch_login(user["id"])
        db.audit(user["username"], "login", ip)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(SESSION_COOKIE, signer.session_token(user["id"], user["session_version"]),
                            httponly=True, samesite="lax", secure=boot.cookie_secure, max_age=SESSION_MAX_AGE,
                            path="/")
        return response

    @app.post("/logout", dependencies=[Depends(csrf_protect)])
    def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.headers["x-logout"] = "1"
        return response

    # --- dashboard ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, state: Optional[str] = None, db: Database = Depends(get_db)):
        values = setting_values(db, box)
        if state not in (None, "", "done", "problems", "waiting", "open_short", "skipped", "dismissed"):
            state = None
        checklist = [
            ("Settings complete", not missing_required(values), "/settings"),
            ("At least one client ready", any(client_status(r) == "ready" for r in db.list_clients()), "/clients"),
            ("Go-live date set", bool(db.get_meta("start_date")), None),
        ]
        return render(request, db, "dashboard.html",
                      rows=db.recent(100, state=state or None, include_skipped=state in ("skipped", "dismissed")),
                      counts=db.counts(), state=state or "", checklist=checklist,
                      setup_done=all(ok for _, ok, _ in checklist),
                      start_date=db.get_meta("start_date"),
                      last_started=db.get_meta("last_run_started"),
                      last_finished=db.get_meta("last_run_finished"),
                      last_error=db.get_meta("last_run_error"),
                      last_run=next(iter(db.list_runs(1, kind="scheduled")), None),
                      last_backup=db.get_meta("last_backup_at"),
                      paused=values.get("PAUSED") == "true", dry_run=values.get("DRY_RUN") == "true")

    @app.post("/po/{po_id}/retry", dependencies=[Depends(csrf_protect)])
    def retry_po(request: Request, po_id: int, db: Database = Depends(get_db)):
        if not db.get(po_id):
            raise HTTPException(404, "Purchase order not found.")
        db.reset(po_id)
        db.audit(username(request), "po.retry", str(po_id))
        from_detail = urlparse(request.headers.get("referer", "")).path == f"/po/{po_id}"
        return redirect(f"/po/{po_id}" if from_detail else "/", f"PO {po_id} will be processed on the next run.")

    @app.post("/po/{po_id}/dismiss", dependencies=[Depends(csrf_protect)])
    def dismiss_po(request: Request, po_id: int, db: Database = Depends(get_db)):
        row = db.get(po_id)
        if not row:
            raise HTTPException(404, "Purchase order not found.")
        db.dismiss(po_id, username(request))
        db.audit(username(request), "po.dismiss", f"{row['reference'] or po_id}")
        return redirect("/", f"{row['reference'] or f'PO {po_id}'} dismissed. It won't be processed; "
                             f"find it under Dismissed to bring it back.")

    @app.post("/go-live", dependencies=[Depends(csrf_protect)])
    def go_live(request: Request, db: Database = Depends(get_db)):
        if db.get_meta("start_date"):
            return redirect("/", "The go-live date is already set.", "info")
        from ..__main__ import start_date_now
        value = start_date_now(db)
        db.audit(username(request), "go_live", value)
        return redirect("/", "Go-live set. Only POs created from now on will be processed.")

    def _start_run(*args: str, command: str = "run") -> Optional[int]:
        """Start a `tplsync` command in the background and return its run id once it has one."""
        cmd = [sys.executable, "-m", "tplsync", command, *args]
        proc = subprocess.Popen(cmd, cwd=ROOT, env={**os.environ, "PYTHONUNBUFFERED": "1"},
                                stdout=subprocess.PIPE, stderr=None, text=True, start_new_session=True)
        ready, _, _ = select.select([proc.stdout], [], [], 20)
        line = proc.stdout.readline() if ready else ""
        proc.stdout.close()
        match = re.match(r"RUN_ID=(\d+)", line.strip())
        return int(match.group(1)) if match else None

    @app.post("/run-now", dependencies=[Depends(csrf_protect)])
    def run_now(request: Request, db: Database = Depends(get_db)):
        if not db.get_meta("start_date"):
            return redirect("/", "Set the go-live date before running.", "error")
        db.audit(username(request), "run.manual")
        run_id = _start_run("--trigger", "manual", "--user", username(request))
        if run_id is None:
            return redirect("/logs", "Another run is still in progress. Try again when it finishes.", "info")
        return redirect(f"/logs/runs/{run_id}", "Run started.")

    @app.post("/test-po", dependencies=[Depends(csrf_protect)])
    def test_po(request: Request, job: int = Form(...), po: int = Form(...), mode: str = Form("dry"),
                db: Database = Depends(get_db)):
        args = ["--job", str(job), "--po", str(po), "--trigger", "test", "--user", username(request)]
        if mode != "live":
            args.append("--dry-run")
        db.audit(username(request), "po.test" if mode != "live" else "po.process", f"job {job} po {po}")
        run_id = _start_run(*args)
        if run_id is None:
            return redirect("/", "A run is already in progress. Try again when it finishes.", "info")
        return redirect(f"/logs/runs/{run_id}",
                        "Processing the PO for real." if mode == "live" else "Preview started. Nothing will be changed.")

    # --- logs -----------------------------------------------------------------------

    PAGE_SIZE = 50

    def _parse_data(rows):
        items = []
        for row in rows:
            item = dict(row)
            if item.get("data"):
                try:
                    item["data_pretty"] = json.dumps(json.loads(item["data"]), indent=2)
                except ValueError:
                    item["data_pretty"] = item["data"]
            if item.get("summary"):
                try:
                    item["summary_obj"] = json.loads(item["summary"])
                except ValueError:
                    item["summary_obj"] = {}
            items.append(item)
        return items

    def _duration(run) -> str:
        if not run["finished_at"]:
            return ""
        seconds = int((datetime.fromisoformat(run["finished_at"]) - datetime.fromisoformat(run["started_at"])).total_seconds())
        return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(request: Request, tab: str = "runs", status: str = "", kind: str = "", level: str = "",
                  q: str = "", page: int = 1, show_quiet: bool = False, db: Database = Depends(get_db)):
        page = max(page, 1)
        offset = (page - 1) * PAGE_SIZE
        q = q.strip()[:100]
        if tab == "events":
            rows = _parse_data(db.list_events(PAGE_SIZE + 1, offset, level=level or None, q=q or None))
        elif tab == "emails":
            rows = [dict(r) for r in db.list_emails(PAGE_SIZE + 1, offset, status=status or None)]
        else:
            tab = "runs"
            rows = _parse_data(db.list_runs(PAGE_SIZE + 1, offset, status=status or None, kind=kind or None,
                                            hide_empty=not show_quiet))
            for r in rows:
                r["duration"] = _duration(r)
        return render(request, db, "logs.html", tab=tab, rows=rows[:PAGE_SIZE], has_next=len(rows) > PAGE_SIZE,
                      page=page, status=status, kind=kind, level=level, q=q, show_quiet=show_quiet,
                      retention=setting_values(db, box).get("LOG_RETENTION_DAYS"))

    @app.get("/logs/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int, level: str = "", db: Database = Depends(get_db)):
        run = db.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found. It may have been removed by log retention.")
        run_item = _parse_data([run])[0]
        run_item["duration"] = _duration(run)
        return render(request, db, "logs_run.html", run=run_item, level=level,
                      events=_parse_data(db.list_events(1000, run_id=run_id)),
                      emails=db.list_emails(200, run_id=run_id),
                      lines=db.log_lines(run_id, level.upper() or None))

    @app.get("/po/{po_id}", response_class=HTMLResponse)
    def po_detail(request: Request, po_id: int, db: Database = Depends(get_db)):
        record = db.get(po_id)
        events = _parse_data(db.list_events(1000, po_id=po_id))
        if not record and not events:
            raise HTTPException(404, "No record of that purchase order.")
        return render(request, db, "logs_po.html", po_id=po_id, record=record, events=events,
                      emails=db.list_emails(200, po_id=po_id),
                      reference=(record["reference"] if record else None)
                      or next((e["reference"] for e in reversed(events) if e["reference"]), None))

    # --- settings -------------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, db: Database = Depends(get_db)):
        raw = db.settings_raw()
        decrypt_error = None
        try:
            values = setting_values(db, box)
        except ValueError as exc:
            values, decrypt_error = {d.key: raw.get(d.key) for d in settings_schema.SETTINGS
                                     if d.kind != "secret"}, str(exc)
        return render(request, db, "settings.html", sections=settings_schema.SECTIONS, decrypt_error=decrypt_error,
                      defs=settings_schema.SETTINGS, values=values,
                      secret_set={d.key: bool(raw.get(d.key)) for d in settings_schema.SETTINGS},
                      missing=missing_required(values) if values else [])

    @app.post("/settings", dependencies=[Depends(csrf_protect)])
    async def settings_save(request: Request, db: Database = Depends(get_db)):
        form = await request.form()
        errors, changed = [], []
        current = db.settings_raw()
        for d in settings_schema.SETTINGS:
            if d.kind == "bool":
                value = "true" if form.get(d.key) == "on" else "false"
            else:
                value = str(form.get(d.key, "")).strip()
            if d.kind == "secret":
                if form.get(f"{d.key}__clear") == "on":
                    value_to_store = None
                elif not value:
                    continue  # blank means keep the saved secret
                else:
                    value_to_store = box.encrypt(value)
            else:
                if d.kind == "int" and value and not value.isdigit():
                    errors.append(f"{d.label} must be a whole number.")
                    continue
                if d.kind == "email" and value and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
                    errors.append(f"{d.label} must be an email address.")
                    continue
                if d.key == "SKU_PATTERN" and value:
                    try:
                        re.compile(value)
                    except re.error:
                        errors.append("SKU pattern is not a valid regular expression.")
                        continue
                if d.key == "DISPLAY_TIMEZONE" and value:
                    try:
                        ZoneInfo(value)
                    except (ZoneInfoNotFoundError, ValueError):
                        errors.append("Display time zone is not a valid IANA name (e.g. America/New_York).")
                        continue
                value_to_store = value or None
                if current.get(d.key) == value_to_store or (current.get(d.key) is None and value == (d.default or "")):
                    continue
            db.set_setting(d.key, value_to_store, username(request))
            changed.append(d.key)
        if changed:
            db.audit(username(request), "settings.update", ", ".join(changed))
        if errors:
            return redirect("/settings", " ".join(errors), "error")
        return redirect("/settings", "Settings saved." if changed else "No changes.", "success" if changed else "info")

    @app.post("/settings/test-syncore", dependencies=[Depends(csrf_protect)])
    def test_syncore(request: Request, db: Database = Depends(get_db)):
        values = setting_values(db, box)
        if not values.get("SYNCORE_API_KEY"):
            return redirect("/settings", "Save a Syncore API key first.", "error")
        try:
            message = SyncoreClient(values["SYNCORE_API_KEY"], values["SYNCORE_BASE_URL"]).test_connection()
            return redirect("/settings", message)
        except ApiError as exc:
            return redirect("/settings", f"Syncore connection failed (HTTP {exc.status}). Check the API key.", "error")

    @app.post("/settings/send-summary", dependencies=[Depends(csrf_protect)])
    def send_summary_now(request: Request, db: Database = Depends(get_db)):
        try:
            settings = load_settings(db, box, boot.db_path)
        except ConfigError as exc:
            return redirect("/settings", str(exc), "error")
        notifier = Notifier(settings.sendgrid_api_key, settings.email_from, settings.alert_email_to, settings.dry_run)
        if maintenance.send_summary(db, settings, notifier, datetime.now(timezone.utc)):
            db.audit(username(request), "summary.send")
            return redirect("/settings", f"Summary sent to {settings.summary_email_to or settings.alert_email_to}.")
        return redirect("/settings", "The summary couldn't be sent; check the Logs page.", "error")

    @app.post("/settings/test-email", dependencies=[Depends(csrf_protect)])
    def test_email(request: Request, db: Database = Depends(get_db)):
        values = setting_values(db, box)
        if not (values.get("SENDGRID_API_KEY") and values.get("EMAIL_FROM") and values.get("ALERT_EMAIL_TO")):
            return redirect("/settings", "Save the SendGrid key, from address and recipients first.", "error")
        try:
            Notifier(values["SENDGRID_API_KEY"], values["EMAIL_FROM"], values["ALERT_EMAIL_TO"]).send(
                "[3PL] Test alert", ["This is a test alert from the 3PL Order Sync admin.",
                                     f"Sent by {username(request)}."])
            db.audit(username(request), "email.test")
            return redirect("/settings", f"Test email sent to {values['ALERT_EMAIL_TO']}.")
        except ApiError as exc:
            return redirect("/settings", f"SendGrid rejected the email (HTTP {exc.status}): {exc.body[:200]}", "error")

    # --- clients --------------------------------------------------------------------

    def _client_view(row) -> dict:
        item = dict(row)
        item.pop("client_secret_enc", None)
        item["has_secret"] = bool(row["client_secret_enc"])
        item["missing"] = client_missing(row)
        item["status"] = client_status(row)
        return item

    @app.get("/clients", response_class=HTMLResponse)
    def clients_page(request: Request, show: str = "", db: Database = Depends(get_db)):
        items = [_client_view(r) for r in db.list_clients()]
        counts = {k: sum(1 for i in items if i["status"] == k) for k in ("ready", "incomplete", "paused")}
        if show in counts:
            items = [i for i in items if i["status"] == show]
        order = {"incomplete": 0, "paused": 1, "ready": 2}
        items.sort(key=lambda i: (order[i["status"]], (i["name"] or "").casefold()))
        suggestion_counts: dict = {}
        for sgg in db.suggestions():
            suggestion_counts[sgg["client_pk"]] = suggestion_counts.get(sgg["client_pk"], 0) + 1
        for i in items:
            i["suggestions"] = suggestion_counts.get(i["id"], 0)
        match_result = json.loads(db.get_meta("clients_match_result") or "null")
        return render(request, db, "clients.html", clients=items, counts=counts, show=show,
                      total=sum(counts.values()), synced_at=db.get_meta("tpl_customers_synced_at"),
                      can_sync=any_ready_client(db, box) is not None,
                      matched_at=db.get_meta("clients_matched_at"), match_result=match_result,
                      has_syncore_key=bool(setting_values(db, box).get("SYNCORE_API_KEY")))

    @app.post("/clients/match", dependencies=[Depends(csrf_protect)])
    def clients_match(request: Request, db: Database = Depends(get_db)):
        if not setting_values(db, box).get("SYNCORE_API_KEY"):
            return redirect("/clients", "Save the Syncore API key in Settings first.", "error")
        values = setting_values(db, box)
        try:
            groups = SyncoreClient(values["SYNCORE_API_KEY"], values["SYNCORE_BASE_URL"]).list_client_groups()
        except ApiError as exc:
            return redirect("/clients", f"Syncore didn't return its client groups (HTTP {exc.status}).", "error")
        counts = clientmatch.match_syncore_groups(db, groups)
        db.audit(username(request), "clients.match", json.dumps(counts))
        return redirect("/clients", f"Read {counts['groups']} Syncore client groups: {counts['matched']} clients linked, "
                                    f"{counts['suggested']} with close names to confirm, {counts['unmatched']} with no "
                                    f"similar group.", "success" if counts["matched"] else "info")

    @app.post("/clients/{client_pk}/use-syncore", dependencies=[Depends(csrf_protect)])
    def client_use_syncore(request: Request, client_pk: int, syncore_group_id: str = Form(...),
                           db: Database = Depends(get_db)):
        row = db.get_client(client_pk)
        choice = next((sgg for sgg in db.suggestions(client_pk) if sgg["syncore_group_id"] == syncore_group_id), None)
        if not row or choice is None:
            raise HTTPException(404, "Suggestion not found.")
        try:
            db.assign_syncore_group(client_pk, choice["syncore_group_id"], choice["business_name"])
        except ValueError as exc:
            return redirect(f"/clients/{client_pk}", str(exc), "error")
        db.audit(username(request), "client.use_syncore", f"{row['name']} -> {syncore_group_id}")
        return redirect(f"/clients/{client_pk}", f"Linked {row['name']} to the Syncore client group "
                                                 f"{choice['business_name']}.")

    @app.post("/clients/sync", dependencies=[Depends(csrf_protect)])
    def clients_sync(request: Request, db: Database = Depends(get_db)):
        values = setting_values(db, box)
        creds = any_ready_client(db, box)
        if creds is None:
            return redirect("/clients", "Finish and activate one client first; its login is used to read the "
                                        "customer list.", "error")
        facility = int(values["TPL_FACILITY_ID"]) if values.get("TPL_FACILITY_ID") else None
        try:
            customers = TplCentralClient(values["TPL_BASE_URL"], creds, values.get("TPL_USER_LOGIN") or "") \
                .list_customers(facility)
        except ApiError as exc:
            return redirect("/clients", f"3PL Central didn't return the customer list (HTTP {exc.status}): "
                                        f"{exc.body[:200]}", "error")
        counts = db.sync_tpl_customers(customers)
        db.set_meta("tpl_customers_synced_at", utcnow())
        db.audit(username(request), "clients.sync", f"{len(customers)} customers via {creds.name}: {counts}")
        return redirect("/clients", f"Checked with 3PL Central: {len(customers)} customer(s) visible to {creds.name}'s "
                                    f"login, {counts['added']} added, {counts['linked']} matched, "
                                    f"{counts['updated']} updated.")

    @app.post("/clients/add-names", dependencies=[Depends(csrf_protect)])
    def clients_add_names(request: Request, names: str = Form(""), db: Database = Depends(get_db)):
        added = db.add_client_names([n for n in re.split(r"[\r\n]+", names) if n.strip()][:500])
        db.audit(username(request), "clients.add_names", f"{added} added")
        return redirect("/clients", f"Added {added} client(s)." if added else "Those clients are already listed.",
                        "success" if added else "info")

    @app.get("/clients/new", response_class=HTMLResponse)
    def client_new(request: Request, db: Database = Depends(get_db)):
        return render(request, db, "clients_form.html", client=None, suggestions=[],
                      groups=clientmatch.cached_groups(db))

    @app.get("/clients/{client_pk}", response_class=HTMLResponse)
    def client_edit(request: Request, client_pk: int, db: Database = Depends(get_db)):
        row = db.get_client(client_pk)
        if not row:
            raise HTTPException(404, "Client not found.")
        return render(request, db, "clients_form.html", client=_client_view(row),
                      suggestions=db.suggestions(client_pk), groups=clientmatch.cached_groups(db))

    @app.post("/clients/save", dependencies=[Depends(csrf_protect)])
    def client_save(request: Request, db: Database = Depends(get_db),
                    id: Optional[int] = Form(None), name: str = Form(...), syncore_group_id: str = Form(""),
                    customer_id: str = Form(""), client_id: str = Form(""), client_secret: str = Form(""),
                    user_login: str = Form(""), active: Optional[str] = Form(None)):
        back = f"/clients/{id}" if id else "/clients/new"
        name, syncore_group_id, client_id = name.strip(), syncore_group_id.strip(), client_id.strip()
        customer_id = customer_id.strip()
        if not name:
            return redirect(back, "Client name is required.", "error")
        if syncore_group_id and not syncore_group_id.isdigit():
            return redirect(back, "Choose the Syncore client group from the list.", "error")
        if customer_id and not customer_id.isdigit():
            return redirect(back, "3PL Customer ID is a number.", "error")
        existing = db.get_client(id) if id else None
        if id and not existing:
            raise HTTPException(404, "Client not found.")
        fields = dict(name=name, syncore_group_id=syncore_group_id or None,
                      customer_id=int(customer_id) if customer_id else None, client_id=client_id or None,
                      user_login=user_login.strip() or None)
        if client_secret.strip():
            fields["client_secret_enc"] = box.encrypt(client_secret.strip())
        if not existing:
            fields["source"] = "manual"
        if fields["syncore_group_id"]:
            holder = next((r for r in db.list_clients()
                           if r["syncore_group_id"] == fields["syncore_group_id"] and r["id"] != id), None)
            if holder is not None and (holder["source"] != "syncore" or holder["customer_id"] or holder["client_id"]
                                       or holder["client_secret_enc"]):
                return redirect(back, f"{holder['name']} already uses Syncore client group {fields['syncore_group_id']}.",
                                "error")
        notes = []
        # Look up the 3PL Customer ID from the login when it isn't filled in.
        secret = client_secret.strip() or (box.decrypt(existing["client_secret_enc"])
                                           if existing and existing["client_secret_enc"] else "")
        if not fields["customer_id"] and fields["client_id"] and secret:
            values = setting_values(db, box)
            login = fields["user_login"] or values.get("TPL_USER_LOGIN") or ""
            if login:
                try:
                    found = clientmatch.lookup_customer(
                        values["TPL_BASE_URL"], fields["client_id"], secret, login,
                        int(values["TPL_FACILITY_ID"]) if values.get("TPL_FACILITY_ID") else None)
                except ApiError as exc:
                    found = []
                    notes.append(f"Couldn't look up the 3PL Customer ID (HTTP {exc.status}); check the Client ID "
                                 f"and Secret.")
                if len(found) > 1:
                    wanted = company_key(name)
                    found = [f for f in found if company_key(f[1]) == wanted] or found
                if len(found) == 1:
                    fields["customer_id"], fields["tpl_customer_name"] = found[0][0], found[0][1]
                    notes.append(f"Found 3PL Customer ID {found[0][0]} ({found[0][1]}).")
                elif len(found) > 1:
                    notes.append("This login sees several 3PL customers; enter the Customer ID.")
        complete = all([fields["syncore_group_id"], fields["customer_id"], fields["client_id"], secret])
        fields["active"] = 1 if (complete and active) else 0
        syncore_id = fields.pop("syncore_group_id")
        merged = None
        try:
            client_pk = db.save_client(id, **fields, **({} if syncore_id else {"syncore_group_id": None}))
            if syncore_id and (not existing or existing["syncore_group_id"] != syncore_id):
                group_name = next((g["name"] for g in clientmatch.cached_groups(db) if g["id"] == syncore_id), None)
                merged = db.assign_syncore_group(client_pk, syncore_id, group_name)
        except ValueError as exc:
            db.save_client(client_pk, active=0)
            return redirect(f"/clients/{client_pk}", str(exc), "error")
        except Exception as exc:  # sqlite3.IntegrityError
            if "UNIQUE" in str(exc):
                return redirect(back, "Another client already uses that 3PL Customer ID.", "error")
            raise
        db.audit(username(request), "client.save", f"{name} (Syncore {syncore_group_id or '-'})"
                                                   + (f", merged {merged}" if merged else ""))
        row = db.get_client(client_pk)
        if client_missing(row):
            message, kind = f"Saved {name}. Still needed to activate: {', '.join(client_missing(row))}.", "info"
        elif row["active"]:
            message, kind = f"Saved {name}. It's active, so its POs will go to 3PL Central.", "success"
        else:
            message, kind = f"Saved {name}. It's paused; turn on Active to start sending its POs.", "info"
        return redirect(f"/clients/{client_pk}", " ".join([message, *notes]), kind)

    @app.post("/clients/{client_pk}/delete", dependencies=[Depends(csrf_protect)])
    def client_delete(request: Request, client_pk: int, db: Database = Depends(get_db)):
        client = db.get_client(client_pk)
        if not client:
            raise HTTPException(404, "Client not found.")
        db.delete_client(client_pk)
        db.audit(username(request), "client.delete", f"{client['name']} (Syncore {client['syncore_group_id']})")
        return redirect("/clients", f"Deleted {client['name']}.")

    @app.post("/clients/{client_pk}/test", dependencies=[Depends(csrf_protect)])
    def client_test(request: Request, client_pk: int, db: Database = Depends(get_db)):
        row = db.get_client(client_pk)
        if not row:
            raise HTTPException(404, "Client not found.")
        if not (row["customer_id"] and row["client_id"] and row["client_secret_enc"]):
            return redirect(f"/clients/{client_pk}", "Enter the 3PL Customer ID, Client ID and Client Secret first.",
                            "error")
        values = setting_values(db, box)
        creds = TplClient(name=row["name"], customer_id=row["customer_id"], client_id=row["client_id"],
                          client_secret=box.decrypt(row["client_secret_enc"]), user_login=row["user_login"])
        if not (creds.user_login or values.get("TPL_USER_LOGIN")):
            return redirect(f"/clients/{client_pk}", "Set the 3PL Central user login in Settings first.", "error")
        try:
            message = TplCentralClient(values["TPL_BASE_URL"], creds, values.get("TPL_USER_LOGIN") or "").test_connection()
            return redirect(f"/clients/{client_pk}", message)
        except ApiError as exc:
            hint = "Check the Client ID, Client Secret and user login." if exc.status in (400, 401, 403) else ""
            return redirect(f"/clients/{client_pk}", f"3PL Central sign-in failed (HTTP {exc.status}). {hint}", "error")

    # --- Ship Via overrides & carrier list -------------------------------------------

    def _cached_carriers(db: Database):
        raw = db.get_meta("tpl_carriers")
        if not raw:
            return None, None
        data = json.loads(raw)
        return carrier_match.carriers_from_json(data["carriers"]), data["fetched_at"]

    @app.get("/shipping", response_class=HTMLResponse)
    def shipping_page(request: Request, edit: Optional[int] = None, check: str = "", db: Database = Depends(get_db)):
        values = setting_values(db, box)
        carriers, fetched_at = _cached_carriers(db)
        check = " ".join(check.split())[:100]
        check_result = check_error = None
        if check:
            if not carriers:
                check_error = "Load the carrier list from 3PL Central first."
            else:
                try:
                    check_result = carrier_match.resolve_routing(
                        check, shipping_overrides(db), carriers, values.get("TPL_BILLING_CODE") or "Prepaid",
                        values.get("TPL_DEFAULT_CARRIER"), values.get("TPL_DEFAULT_MODE"))
                except carrier_match.CarrierMatchError as exc:
                    check_error = str(exc)
        return render(request, db, "shipping.html", rules=db.list_shipping_rules(),
                      editing=db.get_shipping_rule(edit) if edit else None,
                      default_billing=values.get("TPL_BILLING_CODE"), carriers=carriers, fetched_at=fetched_at,
                      carriers_json=json.dumps(carrier_match.carriers_to_json(carriers or [])),
                      check=check,
                      check_base=carrier_match.split_account(check, carrier_match.service_words(carriers or []))[0]
                      if check else "",
                      check_result=check_result, check_error=check_error,
                      has_client=any_ready_client(db, box) is not None)

    @app.post("/shipping/refresh-carriers", dependencies=[Depends(csrf_protect)])
    def refresh_carriers(request: Request, db: Database = Depends(get_db)):
        values = setting_values(db, box)
        creds = any_ready_client(db, box)
        if creds is None:
            return redirect("/shipping", "Finish and activate one client first; its login is used to read the carrier list.",
                            "error")
        if not values.get("TPL_USER_LOGIN") and not creds.user_login:
            return redirect("/shipping", "Set the 3PL Central user login in Settings first.", "error")
        try:
            data = TplCentralClient(values["TPL_BASE_URL"], creds, values.get("TPL_USER_LOGIN") or "").get_carriers()
        except ApiError as exc:
            return redirect("/shipping", f"3PL Central didn't return the carrier list (HTTP {exc.status}): "
                                         f"{exc.body[:200]}", "error")
        carriers = carrier_match.parse_carrier_list(data)
        db.set_meta("tpl_carriers", json.dumps({"fetched_at": utcnow(),
                                                "carriers": carrier_match.carriers_to_json(carriers)}))
        db.audit(username(request), "carriers.refresh", f"{len(carriers)} carriers via {creds.name}")
        services = sum(len(c.services) for c in carriers)
        return redirect("/shipping", f"Loaded {len(carriers)} carriers and {services} services from 3PL Central.")

    @app.post("/shipping/save", dependencies=[Depends(csrf_protect)])
    def shipping_save(request: Request, db: Database = Depends(get_db), id: Optional[int] = Form(None),
                      ship_via: str = Form(...), carrier: str = Form(...), mode: str = Form(...),
                      account: str = Form(""), billing_code: str = Form("")):
        back = f"/shipping?edit={id}" if id else "/shipping"
        carriers, _ = _cached_carriers(db)
        if not carriers:
            return redirect("/shipping", "Load the carrier list from 3PL Central first.", "error")
        # A trailing account number is dropped: it's read from each PO. Service words ("UPS 3DAY") are kept.
        ship_via, typed_account = carrier_match.split_account(" ".join(ship_via.split()),
                                                              carrier_match.service_words(carriers))
        account = account.strip() or ""
        match = next((c for c in carriers if c.name == carrier), None)
        service = next((sv for sv in (match.services if match else []) if sv.code == mode), None)
        if not ship_via or match is None or service is None:
            return redirect(back, "Choose the Ship Via text, a carrier and one of its services.", "error")
        if billing_code and match.billing_codes and billing_code not in match.billing_codes:
            return redirect(back, f"{billing_code} isn't allowed for {match.name}.", "error")
        try:
            db.save_shipping_rule(id, ship_via=ship_via, carrier=match.name, mode=service.code,
                                  scac_code=match.scac or None, account=account.strip() or None,
                                  billing_code=billing_code or None)
        except Exception as exc:
            if "UNIQUE" in str(exc):
                return redirect(back, f"An override for “{ship_via}” already exists.", "error")
            raise
        db.audit(username(request), "shipping.save", f"{ship_via} -> {match.name} / {service.description} ({service.code})")
        note = (f" {typed_account} was left off the shorthand: the account number is read from each PO."
                if typed_account else "")
        return redirect("/shipping", f"“{ship_via}” now ships as {match.name} / {service.description}.{note}")

    @app.post("/shipping/{rule_id}/delete", dependencies=[Depends(csrf_protect)])
    def shipping_delete(request: Request, rule_id: int, db: Database = Depends(get_db)):
        rule = db.get_shipping_rule(rule_id)
        if not rule:
            raise HTTPException(404, "Override not found.")
        db.delete_shipping_rule(rule_id)
        db.audit(username(request), "shipping.delete", rule["ship_via"])
        return redirect("/shipping", f"Deleted override for “{rule['ship_via']}”.")

    # --- users ----------------------------------------------------------------------

    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request, db: Database = Depends(get_db)):
        return render(request, db, "users.html", users=db.list_users(), audit=db.audit_entries(100))

    @app.post("/users/create", dependencies=[Depends(csrf_protect)])
    def user_create(request: Request, db: Database = Depends(get_db), new_username: str = Form(...),
                    new_password: str = Form(...)):
        new_username = new_username.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,60}", new_username):
            return redirect("/users", "Usernames are 3-60 letters, numbers or . _ @ -", "error")
        problem = password_problem(new_password)
        if problem:
            return redirect("/users", problem, "error")
        if db.get_user_by_name(new_username):
            return redirect("/users", f"{new_username} already exists.", "error")
        db.create_user(new_username, hash_password(new_password))
        db.audit(username(request), "user.create", new_username)
        return redirect("/users", f"Created {new_username}.")

    @app.post("/users/{user_id}/delete", dependencies=[Depends(csrf_protect)])
    def user_delete(request: Request, user_id: int, db: Database = Depends(get_db)):
        target = db.get_user(user_id)
        if not target:
            raise HTTPException(404, "User not found.")
        if target["id"] == request.state.user["id"]:
            return redirect("/users", "You can't delete your own account.", "error")
        db.delete_user(user_id)
        db.audit(username(request), "user.delete", target["username"])
        return redirect("/users", f"Deleted {target['username']}.")

    @app.post("/account/password", dependencies=[Depends(csrf_protect)])
    def change_password(request: Request, db: Database = Depends(get_db), current_password: str = Form(...),
                        new_password: str = Form(...), confirm_password: str = Form(...)):
        me = db.get_user(request.state.user["id"])
        if not verify_login(current_password, me["password_hash"]):
            return redirect("/users", "Current password is incorrect.", "error")
        if new_password != confirm_password:
            return redirect("/users", "New passwords don't match.", "error")
        problem = password_problem(new_password)
        if problem:
            return redirect("/users", problem, "error")
        db.set_password(me["id"], hash_password(new_password))
        db.audit(me["username"], "user.password")
        response = redirect("/login", "Password changed. Sign in again.")
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.headers["x-logout"] = "1"
        return response

    return app
