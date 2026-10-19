"""What's deployed, what's on GitHub, and asking for an update.

The admin never updates itself: it writes a request file that a root-owned systemd service watches
(deploy/tplsync-update.path), and that service runs the fixed update script. Status and log are read
back from a folder only root can write.
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

import requests

from .config import ROOT

STATE_DIR = Path(os.environ.get("UPDATE_STATE_DIR", "/var/lib/tplsync-update"))
TRIGGER = ROOT / "update" / "requested"
VERSION_FILE = ROOT / "VERSION"
_cache: dict = {}


def installed() -> bool:
    """Whether setup.sh has set up the Update button on this server."""
    return VERSION_FILE.exists() and TRIGGER.parent.is_dir()


def deployed() -> Optional[dict]:
    try:
        return json.loads(VERSION_FILE.read_text())
    except (OSError, ValueError):
        return None


def status() -> dict:
    """{'state': 'idle'|'requested'|'running'|'ok'|'failed', 'at': ..., 'log': ...}"""
    try:
        info = json.loads((STATE_DIR / "status.json").read_text())
    except (OSError, ValueError):
        info = {"state": "idle", "at": None}
    if TRIGGER.exists() and info.get("state") != "running":
        info = {"state": "requested", "at": None}
    log_file = STATE_DIR / ("update.log.tmp" if info.get("state") == "running" else "update.log")
    try:
        info["log"] = log_file.read_text()[-6000:]
    except OSError:
        info["log"] = ""
    return info


def _github(path: str, ttl: int = 60):
    hit = _cache.get(path)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    resp = requests.get(f"https://api.github.com{path}", timeout=8,
                        headers={"Accept": "application/vnd.github+json", "User-Agent": "tplsync-admin"})
    resp.raise_for_status()
    data = resp.json()
    _cache[path] = (time.monotonic(), data)
    return data


def latest(repo: str, deployed_sha: Optional[str]) -> dict:
    """The newest commit on main, whether its CI passed, and what's new since the deployed commit."""
    commit = _github(f"/repos/{repo}/commits/main")
    sha = commit["sha"]
    runs = _github(f"/repos/{repo}/commits/{sha}/check-runs").get("check_runs", [])
    if not runs:
        ci = "none"
    elif any(r.get("status") != "completed" for r in runs):
        ci = "running"
    else:
        ci = "passed" if all(r.get("conclusion") in ("success", "skipped", "neutral") for r in runs) else "failed"
    changes = []
    if deployed_sha and deployed_sha != sha:
        try:
            compare = _github(f"/repos/{repo}/compare/{deployed_sha}...{sha}")
            changes = [{"sha": c["sha"][:7], "title": c["commit"]["message"].splitlines()[0],
                        "date": c["commit"]["committer"]["date"]} for c in reversed(compare.get("commits", []))]
        except requests.RequestException:
            changes = []
    return {"sha": sha, "title": commit["commit"]["message"].splitlines()[0],
            "date": commit["commit"]["committer"]["date"], "ci": ci, "changes": changes,
            "url": f"https://github.com/{repo}/commit/{sha}"}


def request_update() -> None:
    TRIGGER.write_text("requested by the admin\n")
