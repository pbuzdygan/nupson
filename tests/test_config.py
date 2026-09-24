import tempfile
import unittest
from pathlib import Path

from nupson.config import (
    public_ups_config,
    read_nut_client_password,
    read_ups_secrets,
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
        self.assertIn("maxstartdelay = 10", rendered)
        self.assertIn("maxretry = 1", rendered)

    def test_renders_remote_nut_repeater_profile(self):
        config = validate_ups_config(
            {
                "connection_type": "remote_nut",
                "name": "rack-ups",
                "remote_host": "192.168.20.10",
                "remote_port": "3493",
                "remote_ups_name": "main-ups",
            }
        )
        rendered = render_nut_config(config)
        self.assertIn("driver = dummy-ups", rendered)
        self.assertIn("port = main-ups@192.168.20.10:3493", rendered)
        self.assertIn("repeater_disable_strict_start", rendered)

    def test_snmp_secrets_are_rendered_but_not_exposed(self):
        config = validate_ups_config(
            {
                "connection_type": "snmp",
                "name": "rack-ups",
                "snmp_host": "192.168.20.20",
                "snmp_version": "v3",
                "sec_name": "nupson",
                "sec_level": "authPriv",
                "auth_protocol": "SHA256",
                "auth_password": "authentication-secret",
                "priv_protocol": "AES",
                "priv_password": "privacy-secret",
            }
        )
        public = public_ups_config(config)
        self.assertNotIn("auth_password", public)
        self.assertNotIn("priv_password", public)
        self.assertTrue(public["auth_password_configured"])
        self.assertTrue(public_ups_config(public)["auth_password_configured"])
        rendered = render_nut_config(config)
        self.assertIn("driver = snmp-ups", rendered)
        self.assertIn("secLevel = authPriv", rendered)
        self.assertIn("authPassword = authentication-secret", rendered)
        self.assertIn("snmp_retries = 1", rendered)
        self.assertIn("snmp_timeout = 1", rendered)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_nut_files(path, config, "secondary")
            self.assertEqual(
                read_ups_secrets(path),
                {
                    "auth_password": "authentication-secret",
                    "priv_password": "privacy-secret",
                },
            )
            self.assertEqual((path / "ups.conf").stat().st_mode & 0o777, 0o600)

    def test_snmp_update_preserves_existing_secret(self):
        config = validate_ups_config(
            {
                "connection_type": "snmp",
                "name": "rack-ups",
                "snmp_host": "ups.example.test",
                "snmp_version": "v2c",
            },
            {"community": "existing-community"},
        )
        self.assertEqual(config["community"], "existing-community")

    def test_rejects_short_snmpv3_password(self):
        with self.assertRaisesRegex(ValueError, "at least 8"):
            validate_ups_config(
                {
                    "connection_type": "snmp",
                    "name": "rack-ups",
                    "snmp_host": "ups.example.test",
                    "snmp_version": "v3",
                    "sec_name": "nupson",
                    "sec_level": "authNoPriv",
                    "auth_password": "short",
                }
            )

    def test_rejects_configuration_injection(self):
        with self.assertRaises(ValueError):
            validate_ups_config({"name": "ups\n[evil]", "driver": "usbhid-ups", "port": "auto"})
        with self.assertRaises(ValueError):
            validate_ups_config({"name": "ups", "connection_type": "modbus"})
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
