from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from .db import Database


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Password must contain at least 10 characters")
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return (
        "scrypt$16384$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, cost, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "scrypt":
            return False
        salt = base64.b64decode(salt_text)
        expected = base64.b64decode(digest_text)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=int(cost), r=8, p=1, dklen=32)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


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

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
