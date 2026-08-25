"""Veri tasarrufu (Faz 3): Chromium bayrakları + Firefox user.js, restart_apps."""
import os
import tempfile
import unittest
from unittest import mock

from tether_isolator import apps, system
from tether_isolator.config import Profile, Settings
from tether_isolator.engine import EngineError
from tether_isolator.manager import Manager
from tether_isolator.state import AppProcess


def _prof(**kw):
    kw.setdefault("auto_reconnect", False)
    return Profile(**kw)


class TestChromiumFlags(unittest.TestCase):
    def test_disabled_adds_no_flags(self):
        s = Settings()
        p = _prof(name="t")   # data_saver.enabled = False (varsayılan)
        with mock.patch.object(apps, "_spawn", return_value=1) as spawn:
            apps.launch(s, p, "google-chrome", dry_run=True)
        joined = " ".join(spawn.call_args[0][2])
        self.assertNotIn("autoplay-policy", joined)

    def test_balanced_adds_base_flags_not_strict(self):
        s = Settings()
        p = _prof(name="t")
        p.data_saver.enabled = True
        p.data_saver.level = "balanced"
        with mock.patch.object(apps, "_spawn", return_value=1) as spawn:
            apps.launch(s, p, "chromium", dry_run=True)
        joined = " ".join(spawn.call_args[0][2])
        self.assertIn("--autoplay-policy=user-gesture-required", joined)
        self.assertIn("--disable-background-networking", joined)
        self.assertIn("--disable-sync", joined)
        self.assertNotIn("blink-settings", joined)

    def test_strict_adds_image_blocking_too(self):
        s = Settings()
        p = _prof(name="t")
        p.data_saver.enabled = True
        p.data_saver.level = "strict"
        with mock.patch.object(apps, "_spawn", return_value=1) as spawn:
            apps.launch(s, p, "brave-browser", dry_run=True)
        joined = " ".join(spawn.call_args[0][2])
        self.assertIn("--blink-settings=imagesEnabled=false", joined)

    def test_non_browser_app_unaffected(self):
        s = Settings()
        p = _prof(name="t")
        p.data_saver.enabled = True
        p.data_saver.level = "strict"
        with mock.patch.object(apps, "_spawn", return_value=1) as spawn:
            apps.launch(s, p, "code", dry_run=True)
        joined = " ".join(spawn.call_args[0][2])
        self.assertNotIn("autoplay-policy", joined)
        self.assertNotIn("blink-settings", joined)


class TestFirefoxUserJs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = os.path.join(self.tmp.name, "isolated", "firefox")

    def _profile(self, **ds_kwargs):
        p = _prof(name="t")
        p.profile_data_dir = lambda user: os.path.dirname(self.data_dir)
        for k, v in ds_kwargs.items():
            setattr(p.data_saver, k, v)
        return p

    def _launch(self, p):
        s = Settings()
        with mock.patch.object(apps, "_spawn", return_value=1), \
             mock.patch.object(system, "chown_to_user"):
            apps.launch(s, p, "firefox", dry_run=False)

    def test_enabled_writes_user_js_with_markers(self):
        self._launch(self._profile(enabled=True, level="balanced"))
        path = os.path.join(self.data_dir, "user.js")
        with open(path) as f:
            content = f.read()
        self.assertIn(apps._FIREFOX_MARKER_START, content)
        self.assertIn(apps._FIREFOX_MARKER_END, content)
        self.assertIn('user_pref("network.prefetch-next", false);', content)
        self.assertNotIn("permissions.default.image", content)

    def test_strict_adds_image_blocking(self):
        self._launch(self._profile(enabled=True, level="strict"))
        with open(os.path.join(self.data_dir, "user.js")) as f:
            content = f.read()
        self.assertIn('user_pref("permissions.default.image", 2);', content)

    def test_disabled_writes_nothing_when_no_prior_file(self):
        self._launch(self._profile(enabled=False))
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "user.js")))

    def test_toggling_off_removes_previous_block(self):
        self._launch(self._profile(enabled=True, level="strict"))
        self._launch(self._profile(enabled=False))
        path = os.path.join(self.data_dir, "user.js")
        # Blok kaldırılınca dosyada başka içerik kalmadıysa dosya da silinir.
        self.assertFalse(os.path.exists(path))

    def test_preserves_user_own_lines_outside_markers(self):
        os.makedirs(self.data_dir, exist_ok=True)
        with open(os.path.join(self.data_dir, "user.js"), "w") as f:
            f.write('user_pref("my.custom.pref", true);\n')
        self._launch(self._profile(enabled=True, level="balanced"))
        with open(os.path.join(self.data_dir, "user.js")) as f:
            content = f.read()
        self.assertIn('user_pref("my.custom.pref", true);', content)
        self.assertIn(apps._FIREFOX_MARKER_START, content)

        self._launch(self._profile(enabled=False))
        with open(os.path.join(self.data_dir, "user.js")) as f:
            content = f.read()
        self.assertIn('user_pref("my.custom.pref", true);', content)
        self.assertNotIn(apps._FIREFOX_MARKER_START, content)

    def test_reapplying_same_level_is_idempotent(self):
        self._launch(self._profile(enabled=True, level="balanced"))
        self._launch(self._profile(enabled=True, level="balanced"))
        path = os.path.join(self.data_dir, "user.js")
        with open(path) as f:
            content = f.read()
        self.assertEqual(content.count(apps._FIREFOX_MARKER_START), 1)


class TestRestartApps(unittest.TestCase):
    def test_restart_apps_relaunches_same_list(self):
        m = Manager(Settings(), dry_run=True)
        m.start_session(_prof(name="d", uplink="usb0", apps=["terminator", "opera"]), "usb0")
        old_app_objs = list(m.state.apps)
        started = m.restart_apps()
        self.assertEqual(sorted(started), ["opera", "terminator"])
        self.assertEqual(len(m.state.apps), 2)
        self.assertTrue(all(isinstance(a, AppProcess) for a in m.state.apps))
        # Eski AppProcess kayıtları atılıp TAMAMEN yeni nesnelerle değiştirilmeli.
        self.assertTrue(all(a is not old for a in m.state.apps for old in old_app_objs))

    def test_restart_apps_without_session_rejected(self):
        m = Manager(Settings(), dry_run=True)
        with self.assertRaises(EngineError):
            m.restart_apps()

    def test_restart_apps_one_failure_does_not_abort_others(self):
        m = Manager(Settings(), dry_run=True)
        m.start_session(_prof(name="d", uplink="usb0", apps=["opera", "terminator"]), "usb0")
        calls = {"n": 0}

        def flaky_launch(settings, profile, program, *, dry_run=False):
            calls["n"] += 1
            if program == "opera":
                raise RuntimeError("boom")
            return 123
        with mock.patch("tether_isolator.apps.launch", side_effect=flaky_launch):
            started = m.restart_apps()
        self.assertEqual(started, ["terminator"])
        self.assertEqual(len(m.state.apps), 1)


if __name__ == "__main__":
    unittest.main()
