"""
Local password hashing for ZEUS.

Chooses the strongest hashing backend available on the machine, in this
order, and records which one was used inside the stored string:

    1. Argon2id      (argon2-cffi)   - preferred, memory-hard, PHC winner
    2. bcrypt        (bcrypt)        - established fallback
    3. scrypt        (hashlib, stdlib) - memory-hard, ALWAYS available

The third option matters: ZEUS must work on a disconnected machine with
nothing but the standard library installed. scrypt has been in `hashlib`
since Python 3.6 and is a genuine memory-hard KDF, so there is never a
reason to fall back to a bare SHA hash - and this module cannot produce
one.

Stored format (a single self-describing string, like PHC):

    argon2id  ->  $argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>   (from argon2-cffi)
    bcrypt    ->  bcrypt$<bcrypt hash>
    scrypt    ->  scrypt$n=16384,r=8,p=1$<salt hex>$<hash hex>

Because the algorithm and its parameters travel with the hash, a database
written on a machine with argon2-cffi still verifies on one without it
(it will simply report that it cannot verify that algorithm, rather than
silently failing open), and `needs_rehash()` lets an account be upgraded
to a stronger algorithm the next time the user logs in successfully.

Nothing here logs, prints or returns a password.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional, Tuple

# Backend detection is done once, at import, without importing anything heavy.
try:  # pragma: no cover - depends on the machine
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHash, VerifyMismatchError
    _ARGON2 = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4,
                             hash_len=32, salt_len=16)
except Exception:  # ImportError, or a broken build
    _ARGON2 = None

try:  # pragma: no cover - depends on the machine
    import bcrypt as _bcrypt
except Exception:
    _bcrypt = None

# scrypt parameters: n=2^14, r=8, p=1 is the widely used interactive-login
# setting (~16 MB, tens of milliseconds). Raising n raises both cost and
# memory for an attacker.
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

MIN_PASSWORD_LENGTH = 8


class PasswordError(ValueError):
    """Raised for an unusable password (too short, empty, wrong type)."""


def available_backends() -> list:
    backends = []
    if _ARGON2 is not None:
        backends.append("argon2id")
    if _bcrypt is not None:
        backends.append("bcrypt")
    backends.append("scrypt")   # stdlib - always present
    return backends


def preferred_backend() -> str:
    return available_backends()[0]


def describe_backend() -> str:
    """Human-readable, safe to log - contains no secret material."""
    chosen = preferred_backend()
    detail = {
        "argon2id": "Argon2id (argon2-cffi), m=64MB t=3 p=4",
        "bcrypt": "bcrypt, cost 12",
        "scrypt": f"scrypt (stdlib), n={_SCRYPT_N} r={_SCRYPT_R} p={_SCRYPT_P}",
    }[chosen]
    return f"{detail}; available: {', '.join(available_backends())}"


def validate_password(password: str) -> None:
    """Raise PasswordError if the password is unusable. Deliberately not a
    complexity maze - length is what actually helps offline."""
    if not isinstance(password, str):
        raise PasswordError("Password must be text")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if password.strip() == "":
        raise PasswordError("Password cannot be blank")


def hash_password(password: str) -> str:
    """Hash a password with the strongest available backend.

    The returned string carries the algorithm and parameters, so it is
    self-describing and forward compatible.
    """
    validate_password(password)

    if _ARGON2 is not None:
        return _ARGON2.hash(password)

    if _bcrypt is not None:
        return "bcrypt$" + _bcrypt.hashpw(
            password.encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode("ascii")

    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                            r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
                            maxmem=_SCRYPT_N * _SCRYPT_R * 200)
    return (f"scrypt$n={_SCRYPT_N},r={_SCRYPT_R},p={_SCRYPT_P}$"
            f"{salt.hex()}${digest.hex()}")


def verify_password(password: str, stored: str) -> bool:
    """Constant-time-where-it-matters verification. Never raises for a wrong
    password - returns False. Returns False for a malformed record too,
    which is the safe direction (fail closed)."""
    if not isinstance(password, str) or not isinstance(stored, str) or not stored:
        return False

    try:
        if stored.startswith("$argon2"):
            if _ARGON2 is None:
                return False   # cannot verify -> fail closed, never fail open
            try:
                _ARGON2.verify(stored, password)
                return True
            except Exception:
                return False

        if stored.startswith("bcrypt$"):
            if _bcrypt is None:
                return False
            return _bcrypt.checkpw(password.encode("utf-8"),
                                   stored[len("bcrypt$"):].encode("ascii"))

        if stored.startswith("scrypt$"):
            _, params, salt_hex, digest_hex = stored.split("$", 3)
            values = dict(part.split("=", 1) for part in params.split(","))
            digest = hashlib.scrypt(
                password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                n=int(values["n"]), r=int(values["r"]), p=int(values["p"]),
                dklen=len(digest_hex) // 2,
                maxmem=int(values["n"]) * int(values["r"]) * 200)
            return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False
    return False


def needs_rehash(stored: str) -> bool:
    """True when the record should be re-hashed after a successful login -
    either because a stronger backend is now installed, or because the
    parameters of the current one have been raised."""
    if not stored:
        return True
    best = preferred_backend()
    if stored.startswith("$argon2"):
        if best != "argon2id":
            return False           # argon2 is already the strongest we know
        try:
            return _ARGON2.check_needs_rehash(stored)
        except Exception:
            return False
    if stored.startswith("bcrypt$"):
        return best == "argon2id"
    if stored.startswith("scrypt$"):
        return best in ("argon2id", "bcrypt")
    return True


def algorithm_of(stored: str) -> str:
    """Which algorithm produced this record - for reports and diagnostics."""
    if not stored:
        return "none"
    if stored.startswith("$argon2id"):
        return "argon2id"
    if stored.startswith("$argon2"):
        return stored.split("$")[1]
    if stored.startswith("bcrypt$"):
        return "bcrypt"
    if stored.startswith("scrypt$"):
        return "scrypt"
    return "unknown"


def generate_token(length: int = 32) -> str:
    """Cryptographically secure token for session identifiers."""
    return secrets.token_urlsafe(length)
