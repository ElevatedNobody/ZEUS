"""
ZEUS local authentication.

Mandatory login for a fully offline desktop application:

    auth/password_manager.py   Argon2id / bcrypt / scrypt hashing (no plaintext)
    auth/auth_manager.py       accounts, policy, lockout, audit
    auth/session_manager.py    the active session, lock/unlock/logout,
                               and which user's memory is active

There is no authentication server, no REST endpoint, no cloud identity
provider and no default account. Accounts live in the same local SQLite
database as the rest of ZEUS, and the first one is created by the user
during first-run setup.

Imports are lazy so that `import auth` costs nothing before the login
screen is actually needed.
"""

__all__ = [
    "AuthManager", "AuthResult", "auth_manager",
    "SessionManager", "Session", "session_manager",
    "password_manager",
]


def __getattr__(name):
    # importlib.import_module, not "from . import x": a `from` import asks the
    # package for the attribute first, which re-enters this very function and
    # recurses until the stack blows up.
    import importlib

    if name in ("AuthManager", "AuthResult", "auth_manager"):
        return getattr(importlib.import_module("auth.auth_manager"), name)
    if name in ("SessionManager", "Session", "session_manager"):
        return getattr(importlib.import_module("auth.session_manager"), name)
    if name == "password_manager":
        return importlib.import_module("auth.password_manager")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
