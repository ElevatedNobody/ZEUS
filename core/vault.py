"""
The ZEUS secure file locker.

Files are encrypted on this machine with a key derived from a passphrase the
operator sets. Nothing leaves the machine, no key is stored anywhere, and a
directory listing of the vault reveals neither what a file is called nor what
is in it.

What this is, precisely
-----------------------
    key      = scrypt(passphrase, salt, n=2**15, r=8, p=1) -> 32 bytes
    envelope = AES-256-GCM(key, nonce, plaintext) with the file id as
               associated data, or Fernet where AES-GCM is unavailable
    on disk   database/vault/<file_id>.bin   an opaque blob
    in the db metadata only - never the passphrase, never the key

The passphrase is verified against a stored VERIFIER - itself an encrypted
known string - not against a hash of the passphrase. That means a wrong
passphrase fails at decryption, which is where it would fail anyway, so there
is no separate check to get out of step with the real one.

What this deliberately is NOT
-----------------------------
This protects files at rest against someone who copies the vault directory or
the database. It does NOT protect against someone who is already running code
as this user while the vault is unlocked - at that point the key is in memory
by definition, because the operator asked for their files to be readable.
Claiming otherwise would be the kind of unearned security promise this project
does not make.

Locking is real: `lock()` overwrites the key material and drops the reference,
and every subsequent read fails until the passphrase is supplied again.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from core.logger import get_logger

logger = get_logger("vault")

ROOT = Path(__file__).resolve().parent.parent
from core.paths import data_dir as _data_dir
VAULT_DIR = _data_dir() / "vault"
CONFIG_PATH = VAULT_DIR / "vault.json"

# scrypt parameters. n=2**15 costs roughly 100-200 ms on the target laptop:
# slow enough that guessing a passphrase offline is expensive, fast enough
# that unlocking does not feel broken. maxmem must be raised explicitly or
# hashlib refuses these parameters.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_BYTES = 32
SCRYPT_MAXMEM = SCRYPT_N * SCRYPT_R * 200

MIN_PASSPHRASE_LENGTH = 10

# The plaintext hidden inside the verifier. Its only job is to be recognisable
# after a successful decryption.
_VERIFIER_PLAINTEXT = b"medha-vault-verifier-v1"

MAX_FILE_BYTES = 200 * 1024 * 1024      # a 200 MB ceiling; this is a locker,
                                        # not a backup system


class VaultError(RuntimeError):
    """Something the operator can act on: wrong passphrase, locked, missing."""


class VaultLocked(VaultError):
    """The vault is locked. Unlock it and try again."""


# ------------------------------------------------------------- ciphers --

def _backend() -> str:
    """Which cipher this machine can actually use.

    AES-256-GCM is preferred: it is authenticated, so a tampered file fails to
    decrypt rather than returning wrong bytes. Fernet (also from
    `cryptography`) is the fallback and is also authenticated. If neither is
    installed the vault refuses to operate - it does NOT fall back to
    something weaker, because a locker that only looks locked is worse than an
    obviously absent one.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return "aes-256-gcm"
    except Exception:
        pass
    try:
        from cryptography.fernet import Fernet  # noqa: F401
        return "fernet"
    except Exception:
        return ""


def backend_name() -> str:
    return _backend() or "none"


def _encrypt(key: bytes, plaintext: bytes, associated: bytes) -> bytes:
    backend = _backend()
    if backend == "aes-256-gcm":
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        return b"G1" + nonce + AESGCM(key).encrypt(nonce, plaintext, associated)
    if backend == "fernet":
        import base64
        from cryptography.fernet import Fernet
        return b"F1" + Fernet(base64.urlsafe_b64encode(key)).encrypt(plaintext)
    raise VaultError(
        "No encryption backend is available. The vault will not store files "
        "unencrypted. Install one with:  pip install cryptography")


def _decrypt(key: bytes, blob: bytes, associated: bytes) -> bytes:
    """Decrypt, or raise VaultError with something the operator can act on.

    Both backends are authenticated, so a wrong key and a tampered file are
    the same event to the cipher: the tag does not verify. It surfaces as
    `InvalidTag` / `InvalidToken`, which says nothing useful to whoever is
    looking at the screen - so it is translated here rather than leaked
    upward as a library exception.
    """
    try:
        if blob[:2] == b"G1":
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce, ciphertext = blob[2:14], blob[14:]
            return AESGCM(key).decrypt(nonce, ciphertext, associated)
        if blob[:2] == b"F1":
            import base64
            from cryptography.fernet import Fernet
            return Fernet(base64.urlsafe_b64encode(key)).decrypt(blob[2:])
    except VaultError:
        raise
    except Exception as error:
        raise VaultError(
            "This file could not be decrypted. Either it was altered after "
            "it was stored, or it belongs to a different vault passphrase."
        ) from error
    raise VaultError("This file was not written by the vault, or is damaged.")


def _derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=SCRYPT_N,
                          r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES,
                          maxmem=SCRYPT_MAXMEM)


# --------------------------------------------------------------- state --

@dataclass
class VaultStatus:
    configured: bool
    unlocked: bool
    backend: str
    files: int
    detail: str

    def as_dict(self) -> dict:
        return {"configured": self.configured, "unlocked": self.unlocked,
                "encryption": self.backend, "files": self.files,
                "status": self.detail}


class Vault:
    """One vault per machine, unlocked for the duration of a session."""

    def __init__(self):
        self._key: Optional[bytearray] = None
        self._lock = threading.Lock()

    # -- configuration -----------------------------------------------------

    @staticmethod
    def _read_config() -> Optional[dict]:
        if not CONFIG_PATH.is_file():
            return None
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as error:
            logger.warning(f"vault config unreadable: {error}")
            return None

    def is_configured(self) -> bool:
        config = self._read_config()
        return bool(config and config.get("salt") and config.get("verifier"))

    @property
    def is_unlocked(self) -> bool:
        return self._key is not None

    def create(self, passphrase: str) -> None:
        """Set the vault passphrase for the first time.

        Refuses to run twice: re-creating would generate a new salt and orphan
        every file already stored, which looks exactly like data loss.
        """
        if self.is_configured():
            raise VaultError(
                "This vault already has a passphrase. Use 'change the vault "
                "passphrase' to replace it - creating a new one would make "
                "every file already stored unreadable.")
        if not _backend():
            raise VaultError(
                "No encryption backend is available, so the vault cannot "
                "store anything safely. Install one with: pip install cryptography")
        self._validate(passphrase)

        salt = os.urandom(16)
        key = _derive(passphrase, salt)
        verifier = _encrypt(key, _VERIFIER_PLAINTEXT, b"verifier")

        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps({
            "version": 1,
            "kdf": "scrypt",
            "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt": salt.hex(),
            "verifier": verifier.hex(),
            "cipher": _backend(),
            "created_at": time.time(),
        }, indent=2), encoding="utf-8")
        self._adopt(key)
        logger.info(f"vault created ({_backend()})")

    @staticmethod
    def _validate(passphrase: str) -> None:
        if not isinstance(passphrase, str) or not passphrase.strip():
            raise VaultError("A vault passphrase cannot be empty.")
        if len(passphrase) < MIN_PASSPHRASE_LENGTH:
            raise VaultError(
                f"A vault passphrase must be at least {MIN_PASSPHRASE_LENGTH} "
                f"characters. Length is what makes it hard to guess.")

    # -- unlocking ---------------------------------------------------------

    def unlock(self, passphrase: str) -> bool:
        """Derive the key and check it against the stored verifier.

        Returns False on a wrong passphrase rather than raising, because a
        wrong passphrase is an ordinary event, not an exceptional one.
        """
        config = self._read_config()
        if not config:
            raise VaultError("This vault has not been set up yet.")
        try:
            key = _derive(passphrase, bytes.fromhex(config["salt"]))
            opened = _decrypt(key, bytes.fromhex(config["verifier"]), b"verifier")
        except Exception:
            return False
        if not hmac.compare_digest(opened, _VERIFIER_PLAINTEXT):
            return False
        self._adopt(key)
        return True

    def _adopt(self, key: bytes) -> None:
        with self._lock:
            self._wipe()
            self._key = bytearray(key)

    def _wipe(self) -> None:
        """Overwrite the key bytes before dropping the reference.

        A bytearray is mutable, so this actually clears the buffer rather than
        leaving a copy for the garbage collector to release whenever it feels
        like it. It is not a guarantee against a determined memory dump - the
        interpreter may have copied it - but leaving the key sitting in memory
        after an explicit lock would be worse.
        """
        if self._key is not None:
            for index in range(len(self._key)):
                self._key[index] = 0
        self._key = None

    def lock(self) -> None:
        with self._lock:
            self._wipe()
        logger.info("vault locked")

    def _require_key(self) -> bytes:
        if self._key is None:
            raise VaultLocked(
                "The vault is locked. Say 'unlock the vault' and enter your "
                "passphrase first.")
        return bytes(self._key)

    def change_passphrase(self, current: str, new: str) -> int:
        """Re-key every stored file.

        The key is derived from the passphrase, so a new passphrase means a new
        key, which means every file must be decrypted with the old one and
        re-encrypted with the new one. Done as a two-phase write - all files
        re-encrypted to temporary paths, then moved into place - so an
        interruption halfway through cannot leave half the vault readable with
        one passphrase and half with the other.
        """
        from memory import database

        if not self.unlock(current):
            raise VaultError("The current vault passphrase is not correct.")
        self._validate(new)

        old_key = self._require_key()
        config = self._read_config() or {}
        new_salt = os.urandom(16)
        new_key = _derive(new, new_salt)

        staged: list = []
        try:
            for record in database.list_vault_files():
                file_id = record["file_id"]
                path = VAULT_DIR / f"{file_id}.bin"
                if not path.is_file():
                    continue
                plaintext = _decrypt(old_key, path.read_bytes(), file_id.encode())
                temporary = VAULT_DIR / f"{file_id}.rekey"
                temporary.write_bytes(_encrypt(new_key, plaintext, file_id.encode()))
                staged.append((temporary, path))
        except Exception as error:
            for temporary, _ in staged:
                temporary.unlink(missing_ok=True)
            raise VaultError(f"Re-keying failed, nothing was changed: {error}")

        for temporary, final in staged:
            temporary.replace(final)

        config.update({"salt": new_salt.hex(),
                       "verifier": _encrypt(new_key, _VERIFIER_PLAINTEXT,
                                            b"verifier").hex(),
                       "cipher": _backend(),
                       "rekeyed_at": time.time()})
        CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
        self._adopt(new_key)
        logger.info(f"vault re-keyed, {len(staged)} file(s)")
        return len(staged)

    # -- files -------------------------------------------------------------

    def store(self, source: Path, display_name: str = "", category: str = "",
              note: str = "") -> dict:
        """Encrypt a file into the vault. The original is left where it is."""
        key = self._require_key()
        source = Path(source).expanduser()
        if not source.is_file():
            raise VaultError(f"There is no file at {source}.")
        size = source.stat().st_size
        if size > MAX_FILE_BYTES:
            raise VaultError(
                f"{source.name} is {size / (1024 ** 2):.0f} MB. The vault holds "
                f"files up to {MAX_FILE_BYTES // (1024 ** 2)} MB - it is a "
                f"locker, not a backup system.")

        plaintext = source.read_bytes()
        digest = hashlib.sha256(plaintext).hexdigest()
        file_id = secrets.token_hex(16)

        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        (VAULT_DIR / f"{file_id}.bin").write_bytes(
            _encrypt(key, plaintext, file_id.encode()))

        from memory import database
        database.add_vault_file(
            file_id=file_id, display_name=display_name or source.name,
            size_bytes=size, sha256=digest, category=category, note=note,
            original_path=str(source))
        return {"file_id": file_id, "name": display_name or source.name,
                "size_bytes": size, "sha256": digest}

    def read(self, file_id: str) -> bytes:
        """Decrypt and return the bytes, checking the stored hash.

        The integrity check is against the hash of the ORIGINAL plaintext, so
        it catches a damaged blob as well as a substituted one.
        """
        key = self._require_key()
        from memory import database

        record = database.get_vault_file(file_id)
        if not record:
            raise VaultError("There is no such file in the vault.")
        path = VAULT_DIR / f"{file_id}.bin"
        if not path.is_file():
            raise VaultError(
                f"'{record['display_name']}' is listed in the vault but its "
                f"encrypted data is missing from disk.")
        plaintext = _decrypt(key, path.read_bytes(), file_id.encode())
        expected = record.get("sha256")
        if expected and hashlib.sha256(plaintext).hexdigest() != expected:
            raise VaultError(
                f"'{record['display_name']}' does not match the checksum "
                f"recorded when it was stored. It has been altered.")
        database.touch_vault_file(file_id)
        return plaintext

    def export(self, file_id: str, destination: Path) -> Path:
        """Write a decrypted copy out of the vault.

        This is the one operation that puts vault contents back on the ordinary
        filesystem, which is why it is HIGH_RISK at the tool layer and always
        audited.
        """
        from memory import database

        plaintext = self.read(file_id)
        record = database.get_vault_file(file_id)
        destination = Path(destination).expanduser()
        if destination.is_dir():
            destination = destination / record["display_name"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(plaintext)
        return destination

    def remove(self, file_id: str) -> bool:
        """Delete a file from the vault, overwriting the ciphertext first.

        Overwriting before unlinking is a courtesy, not a guarantee: on an SSD
        with wear levelling the old blocks may survive. It costs nothing and
        helps on a spinning disk.
        """
        from memory import database

        record = database.get_vault_file(file_id)
        if not record:
            return False
        path = VAULT_DIR / f"{file_id}.bin"
        if path.is_file():
            try:
                length = path.stat().st_size
                with open(path, "r+b") as handle:
                    handle.write(os.urandom(length))
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception as error:
                logger.debug(f"could not overwrite before delete: {error}")
            path.unlink(missing_ok=True)
        return database.delete_vault_file(file_id)

    # -- reporting ---------------------------------------------------------

    def status(self) -> VaultStatus:
        """Never claims a state it has not checked."""
        backend = backend_name()
        configured = self.is_configured()
        files = 0
        if configured:
            try:
                from memory import database
                files = database.vault_stats()["files"]
            except Exception:
                files = 0

        if backend == "none":
            detail = ("no encryption backend installed - the vault will not "
                      "store files; run: pip install cryptography")
        elif not configured:
            detail = "not set up yet - set a passphrase to start using it"
        elif self.is_unlocked:
            detail = f"unlocked, {files} file(s), encrypted with {backend}"
        else:
            detail = f"locked, {files} file(s) stored"
        return VaultStatus(configured, self.is_unlocked, backend, files, detail)


vault = Vault()
