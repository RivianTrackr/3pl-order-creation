"""Command line entry point.

  python -m tplsync gen-keys                               print new .env keys
  python -m tplsync create-user USERNAME                   add an admin login (prompts for password)
  python -m tplsync admin                                  start the admin web UI
  python -m tplsync init [--start 2026-09-17T15:00:00]     record go-live time (UTC)
  python -m tplsync run [--dry-run]                        poll Syncore and process POs
  python -m tplsync run --job 12345 --po 67890 [--dry-run] process one PO now
  python -m tplsync status                                 recent activity
  python -m tplsync reset PO_ID                            retry a failed PO
"""

import argparse
import fcntl
import getpass
import json
import logging
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import clientmatch
from . import maintenance
from .config import ConfigError, load_bootstrap, load_settings, setting_values
from .crypto import generate_key
from .db import Database, utcnow
from .notify import Notifier
from .processor import Processor
from .runlog import DatabaseLogHandler, RedactingFilter
from .syncore import SyncoreClient


def start_date_now(db: Database, start: str = None, force: bool = False) -> str:
    if db.get_meta("start_date") and not force:
        return db.get_meta("start_date")
    when = datetime.fromisoformat(start) if start else datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    db.set_meta("start_date", when.isoformat(timespec="seconds"))
    return db.get_meta("start_date")


def match_clients(boot, db: Database, user) -> int:
    lock = open(Path(boot.db_path).with_suffix(".clients.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.warning("Client matching is already running; exiting")
        return 0
    run_id = db.start_run("clients", user, False, "Match Syncore client groups")
    print(f"RUN_ID={run_id}", flush=True)
    root = logging.getLogger()
    handler = DatabaseLogHandler(boot.db_path, run_id)
    root.addHandler(handler)
    try:
        values = setting_values(db, boot.secret_box())
        if not values.get("SYNCORE_API_KEY"):
            raise ConfigError("Save the Syncore API key in Settings first.")
        syncore = SyncoreClient(values["SYNCORE_API_KEY"], values["SYNCORE_BASE_URL"])
        counts = clientmatch.match_syncore_groups(db, syncore.list_client_groups())
        logging.info("Done: %s", counts)
    except Exception as exc:  # noqa: BLE001
        logging.exception("Client matching failed")
        root.removeHandler(handler)
        handler.close()
        db.finish_run(run_id, "failed", None, str(exc)[:2000])
        return 1
    root.removeHandler(handler)
    handler.close()
    db.finish_run(run_id, "ok", counts)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tplsync", description="Syncore PO -> 3PL Central orders")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("gen-keys", help="print TPLSYNC_ENCRYPTION_KEY and ADMIN_SECRET_KEY lines for .env")
    p_user = sub.add_parser("create-user", help="create an admin login or reset its password")
    p_user.add_argument("username")
    sub.add_parser("admin", help="run the admin web UI")

    p_init = sub.add_parser("init", help="record the go-live time; only POs created after it are processed")
    p_init.add_argument("--start", help="UTC ISO timestamp (default: now)")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing start date")

    p_run = sub.add_parser("run", help="poll Syncore and process purchase orders")
    p_run.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    p_run.add_argument("--job", type=int, help="process one PO immediately (with --po)")
    p_run.add_argument("--po", type=int, help="Syncore purchase order id")
    p_run.add_argument("--trigger", choices=("scheduled", "manual", "test"), default=None,
                       help="how the run was started (shown on the Logs page)")
    p_run.add_argument("--user", help="admin user who started the run")

    p_match = sub.add_parser("match-clients", help="fill in Syncore client groups by matching names")
    p_match.add_argument("--user", help="admin user who started it")

    sub.add_parser("backup", help="back up the database now")
    sub.add_parser("summary", help="send the daily summary email now")
    sub.add_parser("status", help="show recent purchase orders")
    p_reset = sub.add_parser("reset", help="retry a failed purchase order")
    p_reset.add_argument("po_id", type=int)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter())

    if args.command == "gen-keys":
        print(f"TPLSYNC_ENCRYPTION_KEY={generate_key()}")
        print(f"ADMIN_SECRET_KEY={secrets.token_urlsafe(48)}")
        return 0

    try:
        boot = load_bootstrap()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    db = Database(boot.db_path)

    if args.command == "admin":
        import uvicorn

        from .admin.app import create_app
        uvicorn.run(create_app(boot), host=boot.admin_host, port=boot.admin_port,
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1")
        return 0

    if args.command == "create-user":
        from .admin.auth import hash_password, password_problem
        password = getpass.getpass("Password: ")
        problem = password_problem(password)
        if problem:
            print(problem, file=sys.stderr)
            return 2
        if password != getpass.getpass("Repeat password: "):
            print("Passwords don't match", file=sys.stderr)
            return 2
        user = db.get_user_by_name(args.username)
        if user:
            db.set_password(user["id"], hash_password(password))
            print(f"Password updated for {user['username']}")
        else:
            db.create_user(args.username, hash_password(password))
            print(f"Created admin user {args.username}")
        db.audit("cli", "user.save", args.username)
        return 0

    if args.command == "init":
        existing = db.get_meta("start_date")
        value = start_date_now(db, args.start, args.force)
        print(f"{'Already initialised' if existing and not args.force else 'Start date set'}: {value}")
        return 0

    if args.command == "status":
        print(f"Start date: {db.get_meta('start_date')}")
        for r in db.recent():
            print(f"{r['updated_at']}  PO {r['po_id']:>8}  {r['reference'] or '':<12} {r['state']:<10} "
                  f"txn={r['order_id'] or '-':<8} {r['completion'] or '':<11} {(r['last_error'] or '')[:80]}")
        return 0

    if args.command == "reset":
        db.reset(args.po_id)
        print(f"PO {args.po_id} will be retried on the next run")
        return 0

    if args.command in ("backup", "summary"):
        settings = load_settings(db, boot.secret_box(), boot.db_path, require_complete=args.command == "summary")
        if args.command == "backup":
            target = maintenance.backup_database(boot.db_path, boot.backup_dir, settings.backup_keep_days or 14)
            db.set_meta("last_backup_at", utcnow())
            db.set_meta("last_backup_path", str(target))
            print(f"Backed up to {target}")
            return 0
        notifier = Notifier(settings.sendgrid_api_key, settings.email_from, settings.alert_email_to, settings.dry_run)
        sent = maintenance.send_summary(db, settings, notifier, datetime.now(timezone.utc))
        print("Summary sent." if sent else "Summary could not be sent; see the log.")
        return 0 if sent else 1

    if args.command == "match-clients":
        return match_clients(boot, db, args.user)

    # run -------------------------------------------------------------------
    if bool(args.job) != bool(args.po):
        parser.error("--job and --po must be used together")

    lock = open(Path(boot.db_path).with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.warning("Another run is still in progress; exiting")
        return 0

    single = bool(args.po)
    kind = args.trigger or ("test" if single else "scheduled")
    target = f"job {args.job} / PO {args.po}" if single else None
    run_id = db.start_run(kind, args.user, args.dry_run, target)
    print(f"RUN_ID={run_id}", flush=True)

    root = logging.getLogger()
    handler = DatabaseLogHandler(boot.db_path, run_id)
    root.addHandler(handler)
    processor = None
    if not single:
        db.set_meta("last_run_started", utcnow())
    try:
        settings = load_settings(db, boot.secret_box(), boot.db_path)
        if args.dry_run:
            settings.dry_run = True
        processor = Processor(
            settings, db,
            SyncoreClient(settings.syncore_api_key, settings.syncore_base_url),
            Notifier(settings.sendgrid_api_key, settings.email_from, settings.alert_email_to, settings.dry_run),
            run_id=run_id,
        )
        now = datetime.now(timezone.utc)
        if single:
            processor.handle(args.po, args.job, None, now, force=True)
        else:
            processor.run(now)
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, (ConfigError, RuntimeError)):
            logging.error("Run failed: %s", exc)
        else:
            logging.exception("Run failed")
        root.removeHandler(handler)
        handler.close()
        db.finish_run(run_id, "failed", dict(processor.stats) if processor else None, str(exc)[:2000],
                      dry_run=processor.s.dry_run if processor else None)
        if not single:
            db.set_meta("last_run_finished", utcnow())
            db.set_meta("last_run_error", str(exc)[:1000])
        return 1

    root.removeHandler(handler)
    handler.close()
    stats = dict(processor.stats)
    status = "failed" if stats.get("errors") and single else "ok"
    db.finish_run(run_id, status, stats, dry_run=processor.s.dry_run)
    if not single:
        db.set_meta("last_run_finished", utcnow())
        db.set_meta("last_run_error", None)
        maintenance.run_daily_tasks(db, processor.s, processor.notifier, boot.backup_dir)
    try:
        db.prune_logs(int(setting_values(db, boot.secret_box()).get("LOG_RETENTION_DAYS") or 90))
    except Exception:  # noqa: BLE001
        logging.exception("Could not prune old logs")
    return 0

if __name__ == "__main__":
    sys.exit(main())
