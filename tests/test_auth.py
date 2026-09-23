import tempfile
import unittest
from pathlib import Path

from nupson.auth import AuthManager, hash_password, verify_password
from nupson.db import Database


class AuthTests(unittest.TestCase):
    def test_hash_and_verify(self):
        encoded = hash_password("a-long-test-password")
        self.assertTrue(verify_password("a-long-test-password", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))

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


if __name__ == "__main__":
    unittest.main()
