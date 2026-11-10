import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "bridge"))
import pc_bridge


def result(text="", code=0):
    return subprocess.CompletedProcess([], code, text, "")


def usb(serial="selected-phone", state="device", model="Test Phone"):
    return {"serial": serial, "state": state, "model": model, "connection": "usb"}


class PhoneBackend:
    """Phone starts USB-only, with wireless debugging switched off after boot."""
    endpoint = "192.0.2.42:43000"

    def __init__(self):
        self.enabled = False
        self.sdk = "33"
        self.wifi = "inet 192.0.2.42/24"
        self.deny_enable = False
        self.pairing_valid = True
        self.connected = False
        self.writes = []
        self.connections = []

    def adb(self, *args, **kwargs):
        if args[:1] == ("-s",):
            serial, command = args[1], args[2:]
            if serial not in ("selected-phone", self.endpoint):
                raise AssertionError(f"Unexpected target {serial}")
            if command == ("shell", "getprop", "ro.build.version.sdk"):
                return result(self.sdk)
            if command == ("shell", "getprop", "ro.serialno"):
                return result("selected-phone")
            if command == ("shell", "ip", "-4", "addr", "show", "wlan0"):
                return result(self.wifi)
            if command == ("shell", "settings", "get", "global", "adb_wifi_enabled"):
                return result("1" if self.enabled else "0")
            if command == ("shell", "settings", "put", "global", "adb_wifi_enabled", "1"):
                self.writes.append(serial)
                if not self.deny_enable:
                    self.enabled = True
                return result()  # Even a zero exit code must be verified by reading back.
        if args == ("devices", "-l"):
            rows = "List of devices attached\nselected-phone device model:Test_Phone\n"
            if self.connected:
                rows += f"{self.endpoint} device model:Test_Phone\n"
            return result(rows)
        if args == ("mdns", "services"):
            return result(f"selected _adb-tls-connect._tcp {self.endpoint}\n" if self.enabled else "")
        if args[:1] == ("connect",):
            self.connections.append(args[1])
            if not self.pairing_valid:
                return result("failed to authenticate", code=1)
            if args[1] == self.endpoint:
                self.connected = True
                return result("connected")
            return result("connection refused", code=1)
        raise AssertionError(f"Unexpected ADB command {args}")


class WirelessRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(pc_bridge, "PHONE_CONFIG_FILE", self.root / "phone.json"))
        self.enterContext(patch.object(pc_bridge, "LAST_WIRELESS_ENDPOINT_FILE", self.root / "endpoint.txt"))
        self.enterContext(patch.object(pc_bridge, "_phone_state", {"wireless_recovery": None}))
        self.enterContext(patch.object(pc_bridge, "_phone_identity_cache", {}))
        self.enterContext(patch.object(pc_bridge, "_phone_transport_serial", None))
        self.enterContext(patch.object(pc_bridge, "log"))
        self.backend = PhoneBackend()
        self.adb = self.enterContext(patch.object(pc_bridge, "adb", side_effect=self.backend.adb))
        self.config = pc_bridge.load_phone_config()
        self.config["preferred_hardware_serial"] = "selected-phone"

    def recovery(self):
        return pc_bridge.get_phone()["wireless_recovery"]

    def test_disabled_setting_is_enabled_over_authorized_usb(self):
        pc_bridge.restore_wireless_debugging(self.config, [usb()])
        self.assertTrue(self.backend.enabled)
        self.assertEqual(self.backend.writes, ["selected-phone"])
        self.assertEqual(self.recovery()["state"], "waiting_connection")

    def test_enabled_setting_is_not_written_again(self):
        self.backend.enabled = True
        pc_bridge.restore_wireless_debugging(self.config, [usb()])
        self.assertEqual(self.backend.writes, [])

    def test_explicit_usb_mode_and_opt_out_do_not_touch_phone_settings(self):
        for settings in ({"mode": "usb"}, {"auto_restore_wireless_debugging": False}, {"prefer_wireless": False}):
            with self.subTest(settings=settings):
                pc_bridge.restore_wireless_debugging({**self.config, **settings}, [usb()])
                self.assertEqual(self.recovery()["state"], "disabled")
        self.adb.assert_not_called()

    def test_unavailable_unauthorized_or_wrong_phone_is_not_modified(self):
        for devices in ([], [usb(state="unauthorized")], [usb(state="offline")], [usb(serial="other-phone")]):
            with self.subTest(devices=devices):
                pc_bridge.restore_wireless_debugging(self.config, devices)
                self.assertEqual(self.recovery()["state"], "usb_required")
        self.adb.assert_not_called()

    def test_two_matching_usb_phones_require_selection(self):
        self.config["preferred_hardware_serial"] = ""
        self.config["preferred_model"] = "Test Phone"
        pc_bridge.restore_wireless_debugging(self.config, [usb(), usb(serial="other-phone")])
        self.assertEqual(self.recovery()["state"], "ambiguous_phone")
        self.adb.assert_not_called()

    def test_missing_wifi_does_not_enable_debugging(self):
        self.backend.wifi = ""
        pc_bridge.restore_wireless_debugging(self.config, [usb()])
        self.assertEqual(self.recovery()["state"], "waiting_wifi")
        self.assertEqual(self.backend.writes, [])

    def test_legacy_android_does_not_enable_unencrypted_tcp_debugging(self):
        self.backend.sdk = "29"
        pc_bridge.restore_wireless_debugging(self.config, [usb()])
        self.assertEqual(self.recovery()["state"], "manual_setup_required")
        self.assertEqual(self.backend.writes, [])

    def test_setting_write_is_verified_and_failure_keeps_usb_usable(self):
        self.backend.deny_enable = True
        pc_bridge.connect_configured_wireless(self.config, devices=[usb()])
        self.assertFalse(self.backend.enabled)
        self.assertEqual(self.recovery()["state"], "failed")
        self.assertIn("permission", self.recovery()["error"])
        self.assertIsNotNone(pc_bridge.select_phone_device([usb()], self.config)[0])

    def test_discovered_address_is_tried_before_obsolete_saved_address(self):
        pc_bridge.LAST_WIRELESS_ENDPOINT_FILE.write_text("198.51.100.9:37000", encoding="ascii")
        pc_bridge.connect_configured_wireless(self.config, devices=[usb()])
        self.assertEqual(self.backend.connections[0], self.backend.endpoint)

    def test_offline_endpoint_is_retried_by_watcher(self):
        self.backend.enabled = True
        offline = {**usb(), "serial": self.backend.endpoint, "connection": "wireless", "state": "offline"}
        self.run_watcher([usb(), offline])
        self.assertTrue(self.backend.connected)

    def test_missing_pairing_is_reported_instead_of_claiming_recovery(self):
        self.backend.pairing_valid = False
        pc_bridge.connect_configured_wireless(self.config, devices=[usb()])
        self.assertEqual(self.recovery()["state"], "pairing_required")
        self.assertFalse(self.backend.connected)

    def test_opt_out_and_invalid_config_values_are_respected(self):
        for value in (False, "false", 1, None):
            with self.subTest(value=value):
                pc_bridge.PHONE_CONFIG_FILE.write_text(json.dumps({"auto_restore_wireless_debugging": value}), encoding="utf-8")
                self.assertFalse(pc_bridge.load_phone_config()["auto_restore_wireless_debugging"])

    def run_watcher(self, initial_devices=None):
        pc_bridge.PHONE_CONFIG_FILE.write_text(json.dumps(self.config), encoding="utf-8")
        if initial_devices is not None:
            native_list = pc_bridge.list_adb_devices
            first = [True]

            def devices():
                if first[0]:
                    first[0] = False
                    return initial_devices, None
                return native_list()
            self.enterContext(patch.object(pc_bridge, "list_adb_devices", side_effect=devices))
        self.enterContext(patch.object(pc_bridge, "_stop", Mock(is_set=Mock(return_value=False), wait=Mock(return_value=True))))
        self.enterContext(patch.object(pc_bridge, "_phone_seen", False))
        self.enterContext(patch.object(pc_bridge, "_phone_seen_transport", None))
        self.enterContext(patch.object(pc_bridge, "_phone_resume_requested", threading.Event()))
        self.enterContext(patch.object(pc_bridge, "wait_for_active_session", return_value=True))
        self.enterContext(patch.object(pc_bridge, "is_session_locked", return_value=False))
        self.enterContext(patch.object(pc_bridge, "start_phone_screen_guardian"))
        launch = self.enterContext(patch.object(pc_bridge, "setup_phone", return_value="Dashboard PWA"))
        pc_bridge.phone_watcher()
        return launch

    def test_watcher_restores_after_boot_selects_wireless_opens_pwa_and_saves_new_endpoint(self):
        launch = self.run_watcher()
        self.assertTrue(self.backend.enabled)
        self.assertTrue(self.backend.connected)
        self.assertEqual(pc_bridge.get_phone()["transport"], "wireless")
        self.assertEqual(self.recovery(), {"state": "connected", "error": None})
        launch.assert_called_once_with(self.backend.endpoint, True)
        self.assertEqual(pc_bridge.LAST_WIRELESS_ENDPOINT_FILE.read_text(encoding="ascii"), self.backend.endpoint)


if __name__ == "__main__":
    unittest.main()
