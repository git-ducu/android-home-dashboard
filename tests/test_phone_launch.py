import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "bridge"))
import pc_bridge


def result(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


class PhoneForegroundTests(unittest.TestCase):
    def test_regular_chrome_tab_is_not_a_pwa(self):
        with patch.object(pc_bridge, "device_adb", return_value=result(
            "mCurrentFocus=Window{abc u0 com.android.chrome/org.chromium.chrome.browser.ChromeTabbedActivity}"
        )):
            self.assertFalse(pc_bridge.wait_for_phone_browser("phone", "com.android.chrome", timeout=0, pwa=True))

    def test_webapp_activities_are_pwa_windows(self):
        for activity in ("WebappActivity", "WebappActivity0", "WebApkActivity", "SameTaskWebApkActivity"):
            with self.subTest(activity=activity), patch.object(pc_bridge, "device_adb", return_value=result(
                f"mCurrentFocus=Window{{abc u0 com.android.chrome/org.chromium.chrome.browser.webapps.{activity}}}"
            )):
                self.assertTrue(pc_bridge.wait_for_phone_browser("phone", "com.android.chrome", timeout=0, pwa=True))

    def test_store_and_other_chrome_like_packages_are_not_the_browser(self):
        for package in ("com.android.vending", "com.android.chrome.fake"):
            with self.subTest(package=package), patch.object(
                pc_bridge, "device_adb", return_value=result(f"  mCurrentFocus=Window{{abc u0 {package}/Main}}")
            ):
                self.assertFalse(pc_bridge.wait_for_phone_browser("phone", "com.android.chrome", timeout=0))

    def test_waits_for_browser_after_launch_transition(self):
        screens = [result("mCurrentFocus=Window{abc u0 com.android.vending/Main}"),
                   result("mCurrentFocus=Window{abc u0 com.android.chrome/Main}")]
        with patch.object(pc_bridge, "device_adb", side_effect=screens), patch.object(pc_bridge.time, "sleep"):
            self.assertTrue(pc_bridge.wait_for_phone_browser("phone", "com.android.chrome"))

    def test_failed_window_query_is_not_success(self):
        with patch.object(pc_bridge, "device_adb", return_value=result(
            "mCurrentFocus=Window{abc u0 com.android.chrome/Main}", code=1
        )):
            self.assertFalse(pc_bridge.wait_for_phone_browser("phone", "com.android.chrome", timeout=0))


class PhoneLaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patches = {
            "PWA_PACKAGE_FILE": Path(temporary.name) / "pwa.txt",
            "_phone_pwa_package": None,
            "is_session_locked": lambda: False,
            "device_adb_ok": lambda *args, **kwargs: True,
            "dashboard_is_foreground": lambda *args: False,
        }
        for name, value in patches.items():
            self.enterContext(patch.object(pc_bridge, name, value))
        self.enterContext(patch.object(pc_bridge, "log"))
        self.pwa = self.enterContext(patch.object(pc_bridge, "installed_pwa_package", return_value="org.chromium.webapk.dashboard"))
        self.packages = self.enterContext(patch.object(
            pc_bridge, "package_exists", side_effect=lambda serial, package: package == "com.android.chrome"
        ))
        self.adb = self.enterContext(patch.object(pc_bridge, "device_adb", return_value=result("Status: ok")))
        self.foreground = self.enterContext(patch.object(pc_bridge, "wait_for_phone_browser"))

    def test_pwa_redirect_to_store_falls_back_to_direct_dashboard_url(self):
        self.foreground.side_effect = [False, True]
        self.assertEqual(pc_bridge.open_phone_ui("phone"), "Chrome")
        launches = [call.args for call in self.adb.call_args_list if "start" in call.args]
        self.assertEqual(len(launches), 1)
        self.assertIn("http://127.0.0.1:8765", launches[0])
        self.assertEqual(launches[0][-1], "com.android.chrome")

    def test_healthy_pwa_does_not_open_an_extra_browser_tab(self):
        self.foreground.return_value = True
        self.assertEqual(pc_bridge.open_phone_ui("phone"), "Dashboard PWA")
        self.assertFalse(any("start" in call.args for call in self.adb.call_args_list))

    def test_zero_exit_code_without_browser_is_reported_as_failure(self):
        self.foreground.return_value = False
        self.adb.return_value = result("org.chromium.webapk.dashboard/Main")
        with self.assertRaisesRegex(RuntimeError, "did not reach the foreground"):
            pc_bridge.open_phone_ui("phone")

    def test_samsung_browser_can_recover_when_chrome_does_not_open(self):
        self.pwa.return_value = None
        self.packages.side_effect = None
        self.packages.return_value = True
        self.foreground.side_effect = [False, True]
        self.assertEqual(pc_bridge.open_phone_ui("phone"), "Samsung Internet")

    def test_default_browser_is_targeted_without_returning_to_webapk(self):
        self.pwa.return_value = None
        self.packages.side_effect = None
        self.packages.return_value = False
        self.adb.return_value = result("org.mozilla.firefox/org.mozilla.fenix.IntentReceiverActivity")
        self.foreground.return_value = True
        self.assertEqual(pc_bridge.open_phone_ui("phone"), "Default browser")
        launch = next(call for call in self.adb.call_args_list if "start" in call.args)
        self.assertEqual(launch.args[-1], "org.mozilla.firefox")


if __name__ == "__main__":
    unittest.main()
