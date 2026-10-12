"""
Local account management and authentication for ZEUS.

Entirely offline: accounts live in the SAME SQLite database as the rest of
ZEUS (`database/medha.db`), verified in-process. There is no auth server,
no REST endpoint, no cloud identity provider, and no default account - the
first account is created by the user during first-run setup.

Policy comes from config.ini [auth] so it can be tuned without editing code:

    require_login          true
    max_failed_attempts    5
    lockout_seconds        60
    min_password_length    8
    session_idle_minutes   0   (0 = no idle timeout)

Security properties this module is responsible for:
  * no plaintext password is ever stored, logged or returned;
  * an unknown username costs the same work as a known one, so the login
    screen cannot be used to enumerate accounts by timing;
  * repeated failures lock the ACCOUNT for a configurable period, and the
    lock always expires - a mistyped password can never brick the install;
  * every attempt is written to an audit table with no secret material.
"""

from __future__ import annotations

import configparser
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.logger import get_logger
from memory import database
import auth.password_manager as pm

logger = get_logger("auth")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.ini"

# Failed attempts against usernames that have no account: {name: (count, until)}.
# Process-local by design - see AuthManager._record_unknown_failure.
_UNKNOWN_FAILURES: dict = {}

DEFAULT_POLICY = {
    "require_login": "true",
    "max_failed_attempts": "5",
    "lockout_seconds": "60",
    "min_password_length": "8",
    "session_idle_minutes": "0",
    "allow_multiple_users": "true",
}

# A real hash of a random password, computed once. Verifying against it gives
# an unknown username the same cost as a known one.
_DUMMY_HASH: Optional[str] = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = pm.hash_password(pm.generate_token(16))
    return _DUMMY_HASH


@dataclass
class AuthResult:
    ok: bool
    user: Optional[dict] = None
    reason: str = ""
    attempts_left: Optional[int] = None
    locked_until: Optional[float] = None
    # Set by SessionManager when the password was right but a second factor
    # (currently only "face") must still pass before the session is usable.
    pending_factor: str = ""
    # How the session was proven: "password" or "face".
    method: str = "password"
    # A ready-made user-facing message (face sign-in explains itself).
    note: str = ""

    @property
    def message(self) -> str:
        """User-facing text. Deliberately vague about WHICH half was wrong."""
        if self.note and not self.ok:
            return self.note
        if self.ok and self.pending_factor == "face":
            return "Password accepted - now look at the camera."
        if self.ok:
            return "Authenticated"
        if self.locked_until:
            remaining = max(0, int(self.locked_until - time.time()))
            return f"Account locked. Try again in {remaining}s."
        if self.reason == "inactive":
            return "This account is disabled."
        if self.reason == "empty":
            return "Enter a user ID and password."
        if self.attempts_left is not None and self.attempts_left > 0:
            return f"Invalid credentials. {self.attempts_left} attempt(s) left."
        return "Invalid credentials."

    def __bool__(self) -> bool:
        """`if auth.authenticate(...)` must mean "it worked". Without this a
        failed result is still a truthy object and a caller written the obvious
        way would let the wrong password through."""
        return bool(self.ok)


class AuthManager:
    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = Path(config_path or CONFIG_PATH)
        self.policy = self._load_policy()
        database.init_db()

    # ------------------------------------------------------------- policy --

    def _load_policy(self) -> dict:
        values = dict(DEFAULT_POLICY)
        if self.config_path.exists():
            parser = configparser.ConfigParser()
            try:
                parser.read(self.config_path, encoding="utf-8")
                if parser.has_section("auth"):
                    for key, value in parser.items("auth"):
                        values[key.strip().lower()] = value.strip()
            except (configparser.Error, OSError):
                pass   # a broken config must not lock the user out
        return values

    def _int(self, key: str, default: int) -> int:
        try:
            return int(str(self.policy.get(key, default)).strip())
        except (TypeError, ValueError):
            return default

    def _bool(self, key: str, default: bool) -> bool:
        raw = str(self.policy.get(key, "")).strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        return default

    @property
    def require_login(self) -> bool:
        return self._bool("require_login", True)

    @property
    def max_failed_attempts(self) -> int:
        return max(1, self._int("max_failed_attempts", 5))

    @property
    def lockout_seconds(self) -> int:
        return max(1, self._int("lockout_seconds", 60))

    @property
    def session_idle_seconds(self) -> int:
        return max(0, self._int("session_idle_minutes", 0)) * 60

    # ------------------------------------------------------------ accounts --

    @staticmethod
    def first_run_required() -> bool:
        """True when no account exists yet - the app must run setup first."""
        database.init_db()
        return database.count_users() == 0

    @staticmethod
    def user_count() -> int:
        database.init_db()
        return database.count_users()

    @staticmethod
    def list_users() -> list:
        return database.list_users()

    def create_account(self, username: str, password: str, display_name: str = "",
                       is_admin: Optional[bool] = None) -> dict:
        """Create a local account. Raises ValueError with an actionable message."""
        username = (username or "").strip()
        if not username:
            raise ValueError("User ID cannot be empty")
        if len(username) > 64:
            raise ValueError("User ID is too long")
        if any(ch in username for ch in "\t\n\r"):
            raise ValueError("User ID contains invalid characters")
        if database.get_user(username):
            raise ValueError(f"User '{username}' already exists")

        min_length = self._int("min_password_length", pm.MIN_PASSWORD_LENGTH)
        if not isinstance(password, str) or len(password) < min_length:
            raise ValueError(f"Password must be at least {min_length} characters")
        pm.validate_password(password)

        hashed = pm.hash_password(password)
        algorithm = pm.algorithm_of(hashed)
        if is_admin is None:
            is_admin = database.count_users() == 0   # the first account owns the box
        user_id = database.create_user(username, hashed, algorithm,
                                       display_name or username, bool(is_admin))
        database.log_auth_event("account_created", username,
                                f"algorithm={algorithm}")
        logger.info(f"Local account created: {username} (hash: {algorithm})")
        return database.get_user_by_id(user_id)

    def change_password(self, username: str, old_password: str, new_password: str) -> bool:
        user = database.get_user(username)
        if not user or not pm.verify_password(old_password, user["password_hash"]):
            database.log_auth_event("password_change_denied", username)
            return False
        min_length = self._int("min_password_length", pm.MIN_PASSWORD_LENGTH)
        if len(new_password) < min_length:
            raise ValueError(f"Password must be at least {min_length} characters")
        hashed = pm.hash_password(new_password)
        database.update_user_password(user["id"], hashed, pm.algorithm_of(hashed))
        database.log_auth_event("password_changed", username)
        logger.info(f"Password changed for {username}")
        return True

    def reset_lock(self, username: str) -> bool:
        user = database.get_user(username)
        if not user:
            return False
        database.set_user_lock(user["id"], None)
        database.record_login_success(user["id"])   # clears the failure counter
        database.log_auth_event("lock_cleared", username)
        return True

    # ------------------------------------------------------ authentication --

    def _record_unknown_failure(self, username: str) -> tuple:
        """Failure counter for usernames that have no account. Process-local and
        deliberately not persisted: it exists to make the reply indistinguishable
        from a real account's, and to slow password spraying within a session."""
        key = username.lower()
        now = time.time()
        failures, lock_until = _UNKNOWN_FAILURES.get(key, (0, None))
        if lock_until and lock_until > now:
            # Already locked: report the same shrinking countdown a real locked
            # account reports, and do not extend it.
            return failures, lock_until
        if lock_until:
            failures, lock_until = 0, None           # the lock always expires
        failures += 1
        if failures >= self.max_failed_attempts:
            lock_until = now + self.lockout_seconds
        if len(_UNKNOWN_FAILURES) > 512:             # bounded; never grows unbounded
            _UNKNOWN_FAILURES.clear()
        _UNKNOWN_FAILURES[key] = (failures, lock_until)
        return failures, lock_until

    def authenticate(self, username: str, password: str) -> AuthResult:
        """Verify local credentials. Never raises for bad input."""
        username = (username or "").strip()
        if not username or not password:
            database.log_auth_event("login_rejected", username, "empty field")
            return AuthResult(False, reason="empty")

        user = database.get_user(username)

        if user is None:
            # Same work as a real verification so timing does not reveal
            # whether the account exists...
            pm.verify_password(password, _dummy_hash())
            database.log_auth_event("login_failed", username, "unknown user")
            # ...and the same countdown, so the WORDING does not reveal it
            # either. A real account answers "3 attempt(s) left"; an invented
            # one must answer identically or the login screen becomes an
            # account-enumeration oracle. Counted in memory only - inventing
            # database rows for names that do not exist would be worse.
            failures, lock_until = self._record_unknown_failure(username)
            return AuthResult(False, reason="unknown",
                              attempts_left=max(0, self.max_failed_attempts - failures),
                              locked_until=lock_until)

        if not user["is_active"]:
            database.log_auth_event("login_failed", username, "inactive account")
            return AuthResult(False, user=None, reason="inactive")

        locked_until = user["locked_until"]
        if locked_until and locked_until > time.time():
            database.log_auth_event("login_blocked", username, "locked")
            return AuthResult(False, reason="locked", locked_until=locked_until)

        if pm.verify_password(password, user["password_hash"]):
            database.record_login_success(user["id"])
            # Opportunistic upgrade: if a stronger backend has since been
            # installed, re-hash now that we legitimately hold the password.
            if pm.needs_rehash(user["password_hash"]):
                try:
                    new_hash = pm.hash_password(password)
                    database.update_user_password(user["id"], new_hash,
                                                  pm.algorithm_of(new_hash))
                    logger.info(f"Password hash upgraded for {username} "
                                f"({pm.algorithm_of(new_hash)})")
                except Exception as e:
                    logger.warning(f"Password rehash skipped: {e}")
            database.log_auth_event("login_success", username)
            return AuthResult(True, user=database.get_user_by_id(user["id"]))

        # Wrong password.
        failures = database.record_login_failure(user["id"])
        attempts_left = max(0, self.max_failed_attempts - failures)
        lock_until = None
        if failures >= self.max_failed_attempts:
            lock_until = time.time() + self.lockout_seconds
            database.set_user_lock(user["id"], lock_until)
            database.log_auth_event("account_locked", username,
                                    f"{failures} failed attempts")
            logger.warning(f"Account '{username}' locked for "
                           f"{self.lockout_seconds}s after {failures} failures")
        else:
            database.log_auth_event("login_failed", username,
                                    f"attempt {failures}")
        return AuthResult(False, reason="bad_password", attempts_left=attempts_left,
                          locked_until=lock_until)

    # --------------------------------------------------------------- info --

    def status(self) -> dict:
        """Safe to display and log - no hashes, no passwords."""
        users = database.list_users()
        return {
            "require_login": self.require_login,
            "accounts": len(users),
            "first_run_required": len(users) == 0,
            "hashing": pm.describe_backend(),
            "max_failed_attempts": self.max_failed_attempts,
            "lockout_seconds": self.lockout_seconds,
            "session_idle_seconds": self.session_idle_seconds,
            "locked_accounts": [u["username"] for u in users
                                if u["locked_until"] and u["locked_until"] > time.time()],
            "storage": "local sqlite (database/medha.db)",
            "external_identity_provider": "NONE",
        }


auth_manager = AuthManager()
