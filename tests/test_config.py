import tempfile
import unittest
from pathlib import Path

from nupson.config import (
    read_nut_client_password,
    remove_nut_files,
    render_nut_config,
    validate_nut_username,
    validate_ups_config,
    write_nut_files,
)


class ConfigTests(unittest.TestCase):
    def test_render_valid_usb_configuration(self):
        rendered = render_nut_config(
            {"name": "rack-ups", "driver": "usbhid-ups", "port": "auto", "serial": "ABC123"}
        )
        self.assertIn("[rack-ups]", rendered)
        self.assertIn("driver = usbhid-ups", rendered)
        self.assertIn("serial = ABC123", rendered)

    def test_rejects_configuration_injection(self):
        with self.assertRaises(ValueError):
            validate_ups_config({"name": "ups\n[evil]", "driver": "usbhid-ups", "port": "auto"})
        with self.assertRaises(ValueError):
            validate_ups_config({"name": "ups", "driver": "bad driver", "port": "auto"})
        with self.assertRaises(ValueError):
            validate_nut_username("client]\n[admin")

    def test_writes_complete_secure_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            credentials = write_nut_files(
                path, {"name": "ups", "driver": "usbhid-ups", "port": "auto"}, "secondary", "read"
            )
            self.assertEqual(credentials["upsmon_password"], "secondary")
            self.assertIn("upsmon secondary", (path / "upsd.users").read_text())
            self.assertIn("upsmon primary", (path / "upsd.users").read_text())
            self.assertIn('SHUTDOWNCMD "/bin/true"', (path / "upsmon.conf").read_text())
            self.assertEqual((path / "upsd.users").stat().st_mode & 0o777, 0o600)
            self.assertEqual(read_nut_client_password(path), "secondary")

    def test_writes_and_reads_custom_client_username(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_nut_files(
                path,
                {"name": "ups", "driver": "usbhid-ups", "port": "auto"},
                "secondary-password",
                client_username="rack-client",
            )
            users = (path / "upsd.users").read_text()
            self.assertIn("[rack-client]", users)
            self.assertIn("[nupson-primary]", users)
            self.assertEqual(read_nut_client_password(path, "rack-client"), "secondary-password")

    def test_removes_generated_nut_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_nut_files(
                path, {"name": "ups", "driver": "usbhid-ups", "port": "auto"}, "secondary"
            )
            unrelated = path / "keep.txt"
            unrelated.write_text("keep")

            remove_nut_files(path)

            self.assertFalse((path / "ups.conf").exists())
            self.assertFalse((path / "upsd.users").exists())
            self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
