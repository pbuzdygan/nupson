import io
import unittest
import zipfile

from nupson.client_config import (
    render_client_files,
    validate_client_profile,
    zip_client_files,
)


class ClientConfigTests(unittest.TestCase):
    def setUp(self):
        self.profile = validate_client_profile(
            {
                "name": "Proxmox-01",
                "address": "192.168.68.102",
                "server_address": "192.168.68.5",
                "platform": "debian",
                "policy": "critical",
                "delay_seconds": 600,
                "final_delay_seconds": 5,
            }
        )
        self.settings = {
            "ups_name": "nutdev1",
            "client_shutdown_policy": "timer",
            "client_shutdown_delay_seconds": 900,
            "client_final_delay_seconds": 10,
        }

    def test_critical_policy_generates_network_client_files(self):
        files = render_client_files(self.profile, self.settings, "strong-password")
        self.assertEqual(files["nut.conf"], "MODE=netclient\n")
        self.assertIn(
            "MONITOR nutdev1@192.168.68.5:3493 1 upsmon strong-password secondary",
            files["upsmon.conf"],
        )
        self.assertNotIn("upssched.conf", files)

    def test_uses_configured_nut_client_username(self):
        self.settings["nut_client_username"] = "rack-client"
        files = render_client_files(self.profile, self.settings, "strong-password")
        self.assertIn(
            "MONITOR nutdev1@192.168.68.5:3493 1 rack-client strong-password secondary",
            files["upsmon.conf"],
        )

    def test_inherited_timer_generates_one_exec_flag_and_script(self):
        self.profile["policy"] = "inherit"
        files = render_client_files(self.profile, self.settings, "strong-password")
        self.assertIn("START-TIMER nupson-shutdown 900", files["upssched.conf"])
        self.assertEqual(files["upsmon.conf"].count("NOTIFYFLAG ONBATT"), 1)
        self.assertIn("SYSLOG+WALL+EXEC", files["upsmon.conf"])
        self.assertIn("RUN_AS_USER nut", files["upsmon.conf"])
        self.assertIn(
            "exec /usr/bin/sudo -n /usr/sbin/shutdown -h now",
            files["nupson-upssched"],
        )
        self.assertEqual(
            files["nupson-nut-shutdown.sudoers"].splitlines()[-1],
            "nut ALL=(root) NOPASSWD: /usr/sbin/shutdown -h now",
        )
        archive = zipfile.ZipFile(io.BytesIO(zip_client_files(files)))
        self.assertIn("nupson-upssched", archive.namelist())
        self.assertIn("nupson-nut-shutdown.sudoers", archive.namelist())
        self.assertEqual(
            archive.getinfo("nupson-upssched").external_attr >> 16,
            0o755,
        )
        self.assertEqual(
            archive.getinfo("nupson-nut-shutdown.sudoers").external_attr >> 16,
            0o440,
        )
        self.assertIn("visudo -cf", files["README.txt"])

    def test_critical_policy_does_not_generate_sudoers_rule(self):
        files = render_client_files(self.profile, self.settings, "strong-password")
        self.assertNotIn("nupson-nut-shutdown.sudoers", files)
        self.assertNotIn("RUN_AS_USER", files["upsmon.conf"])

    def test_profile_requires_exact_ip_address(self):
        values = dict(self.profile)
        values["address"] = "client.example.test"
        with self.assertRaisesRegex(ValueError, "IP address"):
            validate_client_profile(values)


if __name__ == "__main__":
    unittest.main()
