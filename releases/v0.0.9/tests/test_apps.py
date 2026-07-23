"""apps.py testleri: uygulama keşfi, komut/bayrak kurulumu, resolv.conf sarmalı."""
import unittest
from unittest import mock

from tether_isolator import apps, system
from tether_isolator.config import Profile, Settings


class TestApps(unittest.TestCase):
    def test_discover_installed_uses_have(self):
        with mock.patch.object(system, "have",
                               side_effect=lambda a: a in ("opera", "terminator")):
            found = apps.discover_installed()
        self.assertIn("opera", found)
        self.assertIn("terminator", found)
        self.assertNotIn("firefox", found)

    def test_launch_persistent_profile_adds_user_data_dir(self):
        s = Settings()
        p = Profile(name="t", persistent_profile=True, use_system_profile=False)
        with mock.patch.object(apps, "_spawn", return_value=4242) as spawn:
            pid = apps.launch(s, p, "google-chrome", dry_run=True)
        self.assertEqual(pid, 4242)
        argv = spawn.call_args[0][2]           # (settings, user, argv)
        joined = " ".join(argv)
        self.assertTrue(argv[0].endswith("google-chrome"))
        self.assertIn("--user-data-dir=", joined)   # izole/kalıcı profil
        self.assertIn("--no-first-run", joined)

    def test_launch_terminator_flag(self):
        s = Settings()
        p = Profile(name="t")
        with mock.patch.object(apps, "_spawn", return_value=1) as spawn:
            apps.launch(s, p, "terminator", dry_run=True)
        argv = spawn.call_args[0][2]
        self.assertEqual(argv[0], "terminator")
        self.assertIn("-u", argv)

    def test_launch_system_profile_no_userdatadir(self):
        """use_system_profile=True iken --user-data-dir VERİLMEZ (girişler korunur)."""
        s = Settings()
        p = Profile(name="t", use_system_profile=True)
        with mock.patch.object(apps, "_running_on_host", return_value=False), \
             mock.patch.object(apps, "_spawn", return_value=2) as spawn:
            apps.launch(s, p, "google-chrome", dry_run=False)
        joined = " ".join(spawn.call_args[0][2])
        self.assertNotIn("--user-data-dir=", joined)

    def test_launch_system_profile_refuses_if_open_on_host(self):
        s = Settings()
        p = Profile(name="t", use_system_profile=True)
        with mock.patch.object(apps, "_running_on_host", return_value=True):
            with self.assertRaises(RuntimeError):
                apps.launch(s, p, "opera", dry_run=False)

    def test_spawn_wraps_with_resolv_bind(self):
        """_spawn, DNS symlink sorununu aşmak için resolv.conf bind sarmalı üretir."""
        s = Settings()
        fake_proc = mock.Mock()
        fake_proc.pid = 999
        with mock.patch("subprocess.Popen", return_value=fake_proc) as popen, \
             mock.patch.object(system, "user_home", return_value="/home/x"):
            pid = apps._spawn(s, "user", ["opera"], dry_run=False)
        self.assertEqual(pid, 999)
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[:5], ["ip", "netns", "exec", s.namespace, "sh"])
        inner = cmd[-1]
        self.assertIn("resolv.conf", inner)
        self.assertIn("mount --bind", inner)
        self.assertIn("runuser", inner)
        self.assertIn("opera", inner)


if __name__ == "__main__":
    unittest.main()
