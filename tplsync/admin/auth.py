"""Passwords, signed session cookies, CSRF tokens, flash messages, login throttling."""

import hashlib
import hmac
import secrets
import time
from collections import defaultdict
from typing import Dict, List, Optional

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE = "tplsync_session"
FLASH_COOKIE = "tplsync_flash"
SESSION_MAX_AGE = 12 * 60 * 60  # idle timeout; refreshed on every request
MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# Used to spend equal time on unknown usernames, so they can't be probed by timing.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def verify_login(password: str, password_hash: Optional[str]) -> bool:
    if password_hash is None:
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, password_hash)


def password_problem(password: str) -> Optional[str]:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password.encode()) > 72:
        return "Password must be at most 72 bytes."
    return None


class Signer:
    def __init__(self, secret_key: str):
        self.secret_key = secret_key
        self._sessions = URLSafeTimedSerializer(secret_key, salt="session")
        self._flash = URLSafeTimedSerializer(secret_key, salt="flash")

    def session_token(self, user_id: int, session_version: int, nonce: Optional[str] = None) -> str:
        return self._sessions.dumps({"uid": user_id, "sv": session_version, "n": nonce or secrets.token_urlsafe(16)})

    def read_session(self, token: str) -> Optional[dict]:
        try:
            return self._sessions.loads(token, max_age=SESSION_MAX_AGE)
        except (BadSignature, SignatureExpired):
            return None

    def csrf_token(self, nonce: str) -> str:
        return hmac.new(self.secret_key.encode(), f"csrf:{nonce}".encode(), hashlib.sha256).hexdigest()

    def csrf_valid(self, nonce: str, token: Optional[str]) -> bool:
        return bool(token) and hmac.compare_digest(self.csrf_token(nonce), token)

    def flash_token(self, message: str, kind: str) -> str:
        return self._flash.dumps({"m": message, "k": kind})

    def read_flash(self, token: str) -> Optional[dict]:
        try:
            return self._flash.loads(token, max_age=120)
        except (BadSignature, SignatureExpired):
            return None


class LoginThrottle:
    """At most MAX failures per IP (and per username) in WINDOW seconds."""

    MAX = 5
    WINDOW = 15 * 60

    def __init__(self):
        self._failures: Dict[str, List[float]] = defaultdict(list)

    def _recent(self, key: str) -> List[float]:
        now = time.monotonic()
        self._failures[key] = [t for t in self._failures[key] if now - t < self.WINDOW]
        return self._failures[key]

    def blocked(self, ip: str, username: str) -> bool:
        return len(self._recent(f"ip:{ip}")) >= self.MAX or len(self._recent(f"u:{username.lower()}")) >= self.MAX

    def fail(self, ip: str, username: str) -> None:
        now = time.monotonic()
        self._failures[f"ip:{ip}"].append(now)
        self._failures[f"u:{username.lower()}"].append(now)

    def clear(self, ip: str, username: str) -> None:
        self._failures.pop(f"ip:{ip}", None)
        self._failures.pop(f"u:{username.lower()}", None)
