"""SQLite storage: PO processing state, settings, clients, shipping rules, admin users."""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id          INTEGER PRIMARY KEY,
    job_id         INTEGER NOT NULL,
    reference      TEXT,
    client_id      TEXT,
    last_modified  TEXT,
    first_seen     TEXT NOT NULL,
    state          TEXT NOT NULL,   -- waiting | skipped | processing | error | failed | done
    skip_reason    TEXT,
    order_id       INTEGER,         -- 3PL Central transaction number
    completion     TEXT,            -- completed | open_short
    logged         INTEGER NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    alerted_error  TEXT,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,               -- encrypted for secret settings
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
CREATE TABLE IF NOT EXISTS clients (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL,
    syncore_group_id   TEXT UNIQUE,      -- Syncore client group (all of a company's contacts)
    syncore_group_name TEXT,
    customer_id        INTEGER UNIQUE,   -- 3PL Central customer id
    tpl_customer_name  TEXT,
    client_id          TEXT,
    client_secret_enc  TEXT,
    user_login         TEXT,
    active             INTEGER NOT NULL DEFAULT 0,
    source             TEXT,             -- 3pl | syncore | manual
    last_po_at         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS syncore_contacts (
    contact_id   TEXT PRIMARY KEY,      -- job.client.id
    group_id     TEXT,
    group_name   TEXT,
    business_name TEXT,
    fetched_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS client_suggestions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    client_pk          INTEGER NOT NULL,
    syncore_group_id  TEXT NOT NULL,
    business_name      TEXT,
    detail             TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS client_suggestions_client ON client_suggestions(client_pk);
CREATE TABLE IF NOT EXISTS shipping_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ship_via      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    carrier       TEXT NOT NULL,
    mode          TEXT,
    scac_code     TEXT,
    account       TEXT,
    billing_code  TEXT,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash    TEXT NOT NULL,
    session_version  INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    last_login_at    TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    kind          TEXT NOT NULL,     -- scheduled | manual | test
    triggered_by  TEXT,
    dry_run       INTEGER NOT NULL DEFAULT 0,
    target        TEXT,              -- "job 123 / PO 456" for single-PO runs
    status        TEXT NOT NULL,     -- running | ok | failed | interrupted
    summary       TEXT,              -- JSON counters
    error         TEXT
);
CREATE TABLE IF NOT EXISTS log_lines (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL,
    at        TEXT NOT NULL,
    level     TEXT NOT NULL,
    logger    TEXT,
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS log_lines_run ON log_lines(run_id, id);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    run_id     INTEGER,
    po_id      INTEGER,
    job_id     INTEGER,
    reference  TEXT,
    level      TEXT NOT NULL,        -- info | warning | error
    event      TEXT NOT NULL,        -- e.g. order.created
    message    TEXT NOT NULL,
    data       TEXT                  -- JSON
);
CREATE INDEX IF NOT EXISTS events_po ON events(po_id, id);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS emails (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    run_id      INTEGER,
    po_id       INTEGER,
    subject     TEXT NOT NULL,
    recipients  TEXT NOT NULL,
    body        TEXT NOT NULL,
    status      TEXT NOT NULL,       -- sent | failed | dry_run
    error       TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    username  TEXT,
    action    TEXT NOT NULL,
    detail    TEXT
);
"""

ACTIVE_STATES = ("waiting", "processing", "error")


CLIENT_REQUIREMENTS = (
    ("syncore_group_id", "Syncore client group"),
    ("customer_id", "3PL Customer ID"),
    ("client_id", "Client ID"),
    ("client_secret_enc", "Client Secret"),
)
_COMPANY_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "the"}


def company_key(name: Optional[str]) -> str:
    """Loose company-name key: "The Acme Co., Inc." and "acme" compare equal."""
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").casefold()) if w not in _COMPANY_SUFFIXES]
    return "".join(words)


def client_missing(row) -> List[str]:
    return [label for column, label in CLIENT_REQUIREMENTS if row[column] in (None, "")]


def client_status(row) -> str:
    """ready | paused | incomplete"""
    if client_missing(row):
        return "incomplete"
    return "ready" if row["active"] else "paused"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, timeout=15, check_same_thread=False)  # one connection per request, used sequentially
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate_clients()
        self.conn.executescript(SCHEMA)

    def _migrate_clients(self) -> None:
        """Clients are linked to Syncore client *groups* (every contact at a company shares one),
        and their other details are optional until the client is set up."""
        row = self.conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'clients'").fetchone()
        if row is None or "syncore_group_id" in row[0]:
            return
        old_columns = {r[1] for r in self.conn.execute("PRAGMA table_info(clients)")}
        keep = [c for c in ("id", "name", "customer_id", "tpl_customer_name", "client_id", "client_secret_enc",
                            "user_login", "active", "source", "last_po_at", "created_at", "updated_at")
                if c in old_columns]
        with self.conn:
            self.conn.execute("DROP TABLE IF EXISTS client_suggestions")
            self.conn.execute("ALTER TABLE clients RENAME TO clients_old")
            self.conn.executescript(SCHEMA)
            cols = ", ".join(keep)
            self.conn.execute(f"INSERT INTO clients({cols}) SELECT {cols} FROM clients_old")
            self.conn.execute("UPDATE clients SET source = COALESCE(source, 'manual')")
            self.conn.execute("DROP TABLE clients_old")

    def close(self) -> None:
        self.conn.close()

    # --- meta -------------------------------------------------------------

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: Optional[str]) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # --- purchase orders ----------------------------------------------------

    def get(self, po_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM purchase_orders WHERE po_id = ?", (po_id,)).fetchone()

    def upsert(self, po_id: int, job_id: int, **fields) -> None:
        fields["updated_at"] = utcnow()
        with self.conn:
            if self.get(po_id) is None:
                fields.setdefault("first_seen", utcnow())
                fields.setdefault("state", "waiting")
                cols = ["po_id", "job_id", *fields]
                self.conn.execute(
                    f"INSERT INTO purchase_orders({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                    (po_id, job_id, *fields.values()))
            else:
                sets = ", ".join(f"{k} = ?" for k in fields)
                self.conn.execute(f"UPDATE purchase_orders SET {sets} WHERE po_id = ?",
                                  (*fields.values(), po_id))

    def active(self) -> List[sqlite3.Row]:
        marks = ",".join("?" * len(ACTIVE_STATES))
        return self.conn.execute(
            f"SELECT * FROM purchase_orders WHERE state IN ({marks}) ORDER BY first_seen", ACTIVE_STATES
        ).fetchall()

    def recent(self, limit: int = 50, state: Optional[str] = None, include_skipped: bool = False) -> List[sqlite3.Row]:
        where, params = [], []
        if state == "open_short":
            where.append("completion = 'open_short'")
        elif state == "problems":
            where.append("state IN ('error', 'failed')")
        elif state == "done":
            where.append("state = 'done' AND completion = 'completed'")
        elif state:
            where.append("state = ?")
            params.append(state)
        elif not include_skipped:
            where.append("state != 'skipped'")
        sql = "SELECT * FROM purchase_orders"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self.conn.execute(sql + " ORDER BY updated_at DESC LIMIT ?", (*params, limit)).fetchall()

    def event_counts_since(self, since: str) -> Dict[str, int]:
        return {r["event"]: r["n"] for r in self.conn.execute(
            "SELECT event, COUNT(*) AS n FROM events WHERE at >= ? GROUP BY event", (since,))}

    def runs_since(self, since: str) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs WHERE started_at >= ? ORDER BY id", (since,)).fetchall()

    def open_short_orders(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM purchase_orders WHERE completion = 'open_short' AND order_id IS NOT NULL "
                                 "ORDER BY updated_at").fetchall()

    def counts(self) -> Dict[str, int]:
        row = self.conn.execute("""
            SELECT
              SUM(state = 'done' AND completion = 'completed') AS completed,
              SUM(completion = 'open_short') AS open_short,
              SUM(state IN ('error', 'failed')) AS problems,
              SUM(state IN ('waiting', 'processing')) AS pending
            FROM purchase_orders""").fetchone()
        return {k: row[k] or 0 for k in row.keys()}

    def reset(self, po_id: int) -> None:
        """Retry a failed PO. Keeps order_id so no duplicate order is created."""
        with self.conn:
            self.conn.execute("UPDATE purchase_orders SET state = 'error', attempts = 0, alerted_error = NULL "
                              "WHERE po_id = ?", (po_id,))

    # --- settings -----------------------------------------------------------

    def settings_raw(self) -> Dict[str, Optional[str]]:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value: Optional[str], username: Optional[str] = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                "updated_by = excluded.updated_by", (key, value, utcnow(), username))

    # --- clients --------------------------------------------------------------

    def list_clients(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clients ORDER BY name COLLATE NOCASE").fetchall()

    def get_client(self, client_pk: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clients WHERE id = ?", (client_pk,)).fetchone()

    def save_client(self, client_pk: Optional[int], **fields) -> int:
        fields["updated_at"] = utcnow()
        with self.conn:
            if client_pk is None:
                fields["created_at"] = fields["updated_at"]
                cur = self.conn.execute(
                    f"INSERT INTO clients({', '.join(fields)}) VALUES({', '.join('?' * len(fields))})",
                    tuple(fields.values()))
                return int(cur.lastrowid)
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(f"UPDATE clients SET {sets} WHERE id = ?", (*fields.values(), client_pk))
            return client_pk

    def delete_client(self, client_pk: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM clients WHERE id = ?", (client_pk,))
            self.conn.execute("DELETE FROM client_suggestions WHERE client_pk = ?", (client_pk,))

    def assign_syncore_group(self, client_pk: int, syncore_group_id: str, syncore_group_name: Optional[str]) -> Optional[str]:
        """Give a client its Syncore client group. A placeholder that was added automatically
        from a Syncore PO for the same ID is folded in. Returns the merged placeholder's
        name, or raises ValueError if a real client already uses the ID."""
        syncore_group_id = str(syncore_group_id)
        other = self.conn.execute("SELECT * FROM clients WHERE syncore_group_id = ? AND id != ?",
                                  (syncore_group_id, client_pk)).fetchone()
        fields = {"syncore_group_id": syncore_group_id}
        if syncore_group_name:
            fields["syncore_group_name"] = syncore_group_name
        merged = None
        if other is not None:
            if other["source"] != "syncore" or other["customer_id"] or other["client_id"] or other["client_secret_enc"]:
                raise ValueError(f"{other['name']} already uses Syncore client group {syncore_group_id}.")
            fields["syncore_group_name"] = other["syncore_group_name"] or syncore_group_name
            fields["last_po_at"] = other["last_po_at"]
            self.delete_client(other["id"])
            merged = other["name"]
        self.save_client(client_pk, **fields)
        self.set_suggestions(client_pk, [])
        return merged

    def set_suggestions(self, client_pk: int, suggestions: List[dict]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM client_suggestions WHERE client_pk = ?", (client_pk,))
            for s in suggestions:
                self.conn.execute(
                    "INSERT INTO client_suggestions(client_pk, syncore_group_id, business_name, detail, created_at) "
                    "VALUES(?, ?, ?, ?, ?)", (client_pk, str(s["id"]), s.get("business_name"), s.get("detail"), utcnow()))

    def suggestions(self, client_pk: Optional[int] = None) -> List[sqlite3.Row]:
        if client_pk is None:
            return self.conn.execute("SELECT * FROM client_suggestions ORDER BY client_pk, id").fetchall()
        return self.conn.execute("SELECT * FROM client_suggestions WHERE client_pk = ? ORDER BY id",
                                 (client_pk,)).fetchall()

    def _client_by_name(self, name: str, missing_column: str) -> Optional[sqlite3.Row]:
        """A single client whose name matches and that isn't linked on `missing_column` yet."""
        wanted = company_key(name)
        matches = [r for r in self.conn.execute(f"SELECT * FROM clients WHERE {missing_column} IS NULL")
                   if wanted and wanted in (company_key(r["name"]), company_key(r["tpl_customer_name"]),
                                            company_key(r["syncore_group_name"]))]
        return matches[0] if len(matches) == 1 else None

    def link_syncore_group(self, syncore_group_id: str, syncore_group_name: str) -> sqlite3.Row:
        """Record a Syncore client seen on a PO: update it, link it to a client with the same
        name, or add it as a new client that still needs setting up."""
        syncore_group_id = str(syncore_group_id)
        row = self.conn.execute("SELECT * FROM clients WHERE syncore_group_id = ?", (syncore_group_id,)).fetchone()
        if row is None:
            row = self._client_by_name(syncore_group_name, "syncore_group_id")
            if row is not None:
                self.save_client(row["id"], syncore_group_id=syncore_group_id, syncore_group_name=syncore_group_name,
                                 last_po_at=utcnow())
            else:
                self.save_client(None, name=syncore_group_name or f"Syncore client group {syncore_group_id}",
                                 syncore_group_id=syncore_group_id, syncore_group_name=syncore_group_name,
                                 source="syncore", active=0, last_po_at=utcnow())
        else:
            self.save_client(row["id"], syncore_group_name=syncore_group_name, last_po_at=utcnow())
        return self.conn.execute("SELECT * FROM clients WHERE syncore_group_id = ?", (syncore_group_id,)).fetchone()

    def sync_tpl_customers(self, customers: List[tuple]) -> Dict[str, int]:
        """Add or update clients from 3PL Central's (customer_id, name) list."""
        counts = {"added": 0, "linked": 0, "updated": 0}
        for customer_id, name in customers:
            row = self.conn.execute("SELECT * FROM clients WHERE customer_id = ?", (customer_id,)).fetchone()
            if row is not None:
                if row["tpl_customer_name"] != name:
                    self.save_client(row["id"], tpl_customer_name=name)
                    counts["updated"] += 1
                continue
            row = self._client_by_name(name, "customer_id")
            if row is not None:
                self.save_client(row["id"], customer_id=customer_id, tpl_customer_name=name)
                counts["linked"] += 1
            else:
                self.save_client(None, name=name, customer_id=customer_id, tpl_customer_name=name, source="3pl", active=0)
                counts["added"] += 1
        return counts

    def add_client_names(self, names: List[str]) -> int:
        added = 0
        for name in names:
            name = " ".join(name.split())[:120]
            if not name or any(company_key(name) in (company_key(r["name"]), company_key(r["tpl_customer_name"]),
                                                     company_key(r["syncore_group_name"])) for r in self.list_clients()):
                continue
            self.save_client(None, name=name, source="manual", active=0)
            added += 1
        return added

    def cached_contact(self, contact_id: str, max_age_days: int = 7) -> Optional[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%S")
        return self.conn.execute("SELECT * FROM syncore_contacts WHERE contact_id = ? AND fetched_at >= ?",
                                 (str(contact_id), cutoff)).fetchone()

    def cache_contact(self, contact_id: str, group_id: Optional[str], group_name: Optional[str],
                      business_name: Optional[str]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO syncore_contacts(contact_id, group_id, group_name, business_name, fetched_at) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(contact_id) DO UPDATE SET group_id = excluded.group_id, "
                "group_name = excluded.group_name, business_name = excluded.business_name, "
                "fetched_at = excluded.fetched_at", (str(contact_id), group_id, group_name, business_name, utcnow()))

    # --- shipping rules ---------------------------------------------------------

    def list_shipping_rules(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM shipping_rules ORDER BY ship_via COLLATE NOCASE").fetchall()

    def get_shipping_rule(self, rule_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM shipping_rules WHERE id = ?", (rule_id,)).fetchone()

    def save_shipping_rule(self, rule_id: Optional[int], **fields) -> None:
        fields["updated_at"] = utcnow()
        with self.conn:
            if rule_id is None:
                self.conn.execute(
                    f"INSERT INTO shipping_rules({', '.join(fields)}) VALUES({', '.join('?' * len(fields))})",
                    tuple(fields.values()))
            else:
                sets = ", ".join(f"{k} = ?" for k in fields)
                self.conn.execute(f"UPDATE shipping_rules SET {sets} WHERE id = ?", (*fields.values(), rule_id))

    def delete_shipping_rule(self, rule_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM shipping_rules WHERE id = ?", (rule_id,))

    # --- users --------------------------------------------------------------------

    def list_users(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()

    def get_user(self, user_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def get_user_by_name(self, username: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    def create_user(self, username: str, password_hash: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO users(username, password_hash, created_at) VALUES(?, ?, ?)",
                              (username, password_hash, utcnow()))

    def set_password(self, user_id: int, password_hash: str) -> None:
        """Also bumps session_version, signing out existing sessions."""
        with self.conn:
            self.conn.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 "
                              "WHERE id = ?", (password_hash, user_id))

    def touch_login(self, user_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow(), user_id))

    def delete_user(self, user_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    # --- audit ----------------------------------------------------------------------

    def audit(self, username: Optional[str], action: str, detail: str = "") -> None:
        with self.conn:
            self.conn.execute("INSERT INTO audit_log(at, username, action, detail) VALUES(?, ?, ?, ?)",
                              (utcnow(), username, action, detail))

    def audit_entries(self, limit: int = 100) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # --- runs & logs ------------------------------------------------------------------

    def start_run(self, kind: str, triggered_by: Optional[str], dry_run: bool, target: Optional[str]) -> int:
        with self.conn:
            # Order runs hold an exclusive lock, so any other "running" order run was killed mid-run.
            same_lock = "kind = 'clients'" if kind == "clients" else "kind != 'clients'"
            self.conn.execute("UPDATE runs SET status = 'interrupted', finished_at = COALESCE(finished_at, ?) "
                              f"WHERE status = 'running' AND {same_lock}", (utcnow(),))
            cur = self.conn.execute(
                "INSERT INTO runs(started_at, kind, triggered_by, dry_run, target, status) VALUES(?, ?, ?, ?, ?, 'running')",
                (utcnow(), kind, triggered_by, 1 if dry_run else 0, target))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: Optional[dict] = None, error: Optional[str] = None,
                   dry_run: Optional[bool] = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, summary = ?, error = ?, dry_run = COALESCE(?, dry_run) "
                "WHERE id = ?",
                (utcnow(), status, json.dumps(summary) if summary else None, error,
                 None if dry_run is None else int(dry_run), run_id))

    def get_run(self, run_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def list_runs(self, limit: int = 50, offset: int = 0, status: Optional[str] = None,
                  kind: Optional[str] = None, hide_empty: bool = False) -> List[sqlite3.Row]:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if hide_empty:
            where.append("(kind != 'scheduled' OR status != 'ok' OR EXISTS (SELECT 1 FROM events e WHERE e.run_id = runs.id))")
        sql = "SELECT * FROM runs" + (" WHERE " + " AND ".join(where) if where else "")
        return self.conn.execute(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()

    def add_log_line(self, run_id: int, level: str, logger: str, message: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO log_lines(run_id, at, level, logger, message) VALUES(?, ?, ?, ?, ?)",
                              (run_id, utcnow(), level, logger, message))

    def log_lines(self, run_id: int, min_level: Optional[str] = None) -> List[sqlite3.Row]:
        levels = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
        rows = self.conn.execute("SELECT * FROM log_lines WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        if min_level in levels:
            rows = [r for r in rows if levels.get(r["level"], 1) >= levels[min_level]]
        return rows

    def add_event(self, run_id: Optional[int], po_id: Optional[int], job_id: Optional[int], reference: Optional[str],
                  level: str, event: str, message: str, data=None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO events(at, run_id, po_id, job_id, reference, level, event, message, data) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (utcnow(), run_id, po_id, job_id, reference, level, event, message,
                 json.dumps(data, default=str) if data is not None else None))

    def list_events(self, limit: int = 100, offset: int = 0, run_id: Optional[int] = None, po_id: Optional[int] = None,
                    level: Optional[str] = None, q: Optional[str] = None) -> List[sqlite3.Row]:
        where, params = [], []
        for col, value in (("run_id", run_id), ("po_id", po_id), ("level", level)):
            if value not in (None, ""):
                where.append(f"{col} = ?")
                params.append(value)
        if q:
            where.append("(message LIKE ? OR reference LIKE ? OR event LIKE ? OR CAST(po_id AS TEXT) = ? "
                         "OR CAST(job_id AS TEXT) = ?)")
            params += [f"%{q}%", f"%{q}%", f"%{q}%", q, q]
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        order = "ASC" if run_id or po_id else "DESC"
        return self.conn.execute(sql + f" ORDER BY id {order} LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()

    def add_email(self, run_id: Optional[int], po_id: Optional[int], subject: str, recipients: str, body: str,
                  status: str, error: Optional[str] = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO emails(at, run_id, po_id, subject, recipients, body, status, error) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)", (utcnow(), run_id, po_id, subject, recipients, body, status, error))

    def list_emails(self, limit: int = 50, offset: int = 0, run_id: Optional[int] = None,
                    po_id: Optional[int] = None, status: Optional[str] = None) -> List[sqlite3.Row]:
        where, params = [], []
        for col, value in (("run_id", run_id), ("po_id", po_id), ("status", status)):
            if value not in (None, ""):
                where.append(f"{col} = ?")
                params.append(value)
        sql = "SELECT * FROM emails" + (" WHERE " + " AND ".join(where) if where else "")
        return self.conn.execute(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()

    def prune_logs(self, days: int) -> int:
        """Delete runs, log lines, events and emails older than `days`. PO state and the audit log are kept."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        with self.conn:
            old_runs = "SELECT id FROM runs WHERE started_at < ? AND status != 'running'"
            removed = self.conn.execute(f"DELETE FROM log_lines WHERE run_id IN ({old_runs})", (cutoff,)).rowcount
            self.conn.execute("DELETE FROM events WHERE at < ?", (cutoff,))
            self.conn.execute("DELETE FROM emails WHERE at < ?", (cutoff,))
            self.conn.execute("DELETE FROM runs WHERE started_at < ? AND status != 'running'", (cutoff,))
        return removed
