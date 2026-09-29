from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time

from .db import Database

# OWASP-equivalent scrypt profile (N=2^15, r=8, p=3) needs 32 MiB per hash.
# The former N=2^17, p=1 profile needed 128 MiB, which pushed 512 MB hosts
# such as the Raspberry Pi Zero 2 W into swap during account setup.
SCRYPT_COST = 2**15
SCRYPT_BLOCK_SIZE = 8
SCRYPT_PARALLELISM = 3
SCRYPT_MAX_MEMORY = 256 * 1024 * 1024
LEGACY_SCRYPT_BLOCK_SIZE = 8
LEGACY_SCRYPT_PARALLELISM = 1

# Every request runs in its own thread. Serialise key derivation so that
# concurrent login attempts cannot multiply memory use.
_kdf_lock = threading.Lock()


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    with _kdf_lock:
        return hashlib.scrypt(
            password.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=SCRYPT_MAX_MEMORY
        )


def _decode(encoded: str) -> tuple[int, int, int, bytes, bytes]:
    parts = encoded.split("$")
    if parts[0] != "scrypt":
        raise ValueError("Unsupported password hash")
    if len(parts) == 4:
        _, cost, salt_text, digest_text = parts
        r, p = LEGACY_SCRYPT_BLOCK_SIZE, LEGACY_SCRYPT_PARALLELISM
    elif len(parts) == 6:
        _, cost, r_text, p_text, salt_text, digest_text = parts
        r, p = int(r_text), int(p_text)
    else:
        raise ValueError("Malformed password hash")
    return int(cost), r, p, base64.b64decode(salt_text), base64.b64decode(digest_text)


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Password must contain at least 10 characters")
    salt = os.urandom(16)
    digest = _scrypt(password, salt, SCRYPT_COST, SCRYPT_BLOCK_SIZE, SCRYPT_PARALLELISM)
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_COST),
            str(SCRYPT_BLOCK_SIZE),
            str(SCRYPT_PARALLELISM),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        n, r, p, salt, expected = _decode(encoded)
        return hmac.compare_digest(_scrypt(password, salt, n, r, p), expected)
    except (ValueError, TypeError):
        return False


def needs_rehash(encoded: str) -> bool:
    try:
        n, r, p, _salt, _digest = _decode(encoded)
    except (ValueError, TypeError):
        return True
    return (n, r, p) != (SCRYPT_COST, SCRYPT_BLOCK_SIZE, SCRYPT_PARALLELISM)


class AuthManager:
    def __init__(self, database: Database, lifetime: int = 12 * 3600):
        self.database = database
        self.lifetime = lifetime

    @property
    def setup_required(self) -> bool:
        return self.database.user_count() == 0

    def setup(self, username: str, password: str) -> str:
        username = username.strip()
        if not self.setup_required:
            raise ValueError("Administrator already exists")
        if not username or len(username) > 64:
            raise ValueError("Invalid username")
        self.database.create_user(username, hash_password(password))
        return self.create_session(username)

    def login(self, username: str, password: str) -> str | None:
        encoded = self.database.password_hash(username)
        if not encoded or not verify_password(password, encoded):
            return None
        if needs_rehash(encoded):
            self.database.upgrade_password_hash(username, hash_password(password))
        return self.create_session(username)

    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        self.database.create_session(
            self._token_hash(token), username, int(time.time()) + self.lifetime
        )
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        return self.database.session_valid(self._token_hash(token))

    def logout(self, token: str | None) -> None:
        if token:
            self.database.delete_session(self._token_hash(token))

    def change_password(
        self, token: str | None, current_password: str, new_password: str
    ) -> str | None:
        if not token:
            return None
        username = self.database.session_username(self._token_hash(token))
        encoded = self.database.password_hash(username) if username else None
        if not username or not encoded or not verify_password(current_password, encoded):
            return None
        if verify_password(new_password, encoded):
            raise ValueError("New password must be different from the current password")
        self.database.update_password(username, hash_password(new_password))
        return self.create_session(username)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
