import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from nupson.auth import AuthManager, hash_password, needs_rehash, verify_password
from nupson.db import Database


class AuthTests(unittest.TestCase):
    def test_hash_and_verify(self):
        encoded = hash_password("a-long-test-password")
        self.assertTrue(verify_password("a-long-test-password", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))

    def test_hash_stays_within_small_host_memory_budget(self):
        _, cost, block_size, parallelism, *_ = hash_password("a-long-test-password").split("$")
        self.assertLessEqual(128 * int(block_size) * int(cost), 32 * 1024 * 1024)
        self.assertGreater(int(parallelism), 1)

    def test_login_upgrades_legacy_hash_without_revoking_sessions(self):
        salt = b"0123456789abcdef"
        digest = hashlib.scrypt(
            b"a-long-test-password", salt=salt, n=2**17, r=8, p=1, dklen=32, maxmem=256 << 20
        )
        salt_text = base64.b64encode(salt).decode()
        legacy = f"scrypt${2**17}${salt_text}${base64.b64encode(digest).decode()}"
        self.assertTrue(verify_password("a-long-test-password", legacy))
        self.assertTrue(needs_rehash(legacy))
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db")
            database.create_user("admin", legacy)
            auth = AuthManager(database)
            first_token = auth.login("admin", "a-long-test-password")

            second_token = auth.login("admin", "a-long-test-password")

            self.assertFalse(needs_rehash(database.password_hash("admin")))
            self.assertTrue(auth.valid(first_token))
            self.assertTrue(auth.valid(second_token))

    def test_setup_is_one_time(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = AuthManager(Database(Path(directory) / "test.db"))
            token = auth.setup("admin", "a-long-test-password")
            self.assertTrue(auth.valid(token))
            with self.assertRaises(ValueError):
                auth.setup("other", "another-long-password")

    def test_session_survives_auth_manager_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db")
            token = AuthManager(database).setup("admin", "a-long-test-password")

            restarted_auth = AuthManager(Database(database.path))

            self.assertTrue(restarted_auth.valid(token))
            restarted_auth.logout(token)
            self.assertFalse(restarted_auth.valid(token))

    def test_change_password_revokes_previous_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = AuthManager(Database(Path(directory) / "test.db"))
            first_token = auth.setup("admin", "a-long-test-password")
            second_token = auth.login("admin", "a-long-test-password")

            replacement_token = auth.change_password(
                first_token, "a-long-test-password", "a-new-long-test-password"
            )

            self.assertIsNotNone(replacement_token)
            self.assertFalse(auth.valid(first_token))
            self.assertFalse(auth.valid(second_token))
            self.assertTrue(auth.valid(replacement_token))
            self.assertIsNone(auth.login("admin", "a-long-test-password"))
            self.assertIsNotNone(auth.login("admin", "a-new-long-test-password"))

    def test_change_password_rejects_incorrect_current_password(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = AuthManager(Database(Path(directory) / "test.db"))
            token = auth.setup("admin", "a-long-test-password")

            replacement_token = auth.change_password(
                token, "incorrect-password", "a-new-long-test-password"
            )

            self.assertIsNone(replacement_token)
            self.assertTrue(auth.valid(token))
            self.assertIsNotNone(auth.login("admin", "a-long-test-password"))


if __name__ == "__main__":
    unittest.main()
