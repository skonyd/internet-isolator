"""apps.py testleri: uygulama keşfi, komut/bayrak kurulumu, resolv.conf sarmalı."""
import os
import tempfile
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

    def test_launch_system_profile_still_isolates(self):
        """use_system_profile=True olsa bile --user-data-dir VERİLİR.

        Sistem profili paylaşılırsa Chromium, host'ta çalışan örneğe
        SingletonSocket üzerinden devredip çıkar; pencere host'ta açılır ve
        trafik host ağından gider. İzolasyon buna güvenemez.
        """
        s = Settings()
        p = Profile(name="t", use_system_profile=True)
        with mock.patch.object(apps, "_spawn", return_value=2) as spawn:
            apps.launch(s, p, "google-chrome", dry_run=True)
        joined = " ".join(spawn.call_args[0][2])
        self.assertIn("--user-data-dir=", joined)

    def test_launch_non_persistent_profile_still_isolates(self):
        """persistent_profile=False de bayraksız bırakmaz (sistem profiline düşmez)."""
        s = Settings()
        p = Profile(name="t", persistent_profile=False, use_system_profile=False)
        with mock.patch.object(apps, "_spawn", return_value=3) as spawn:
            apps.launch(s, p, "opera", dry_run=True)
        joined = " ".join(spawn.call_args[0][2])
        self.assertIn("--user-data-dir=", joined)

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


class TestImportProfile(unittest.TestCase):
    """Host'taki gerçek profili izole (kalıcı) profile aktarma (bkz. apps.import_profile)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host_src = os.path.join(self.tmp.name, "host", "opera")
        os.makedirs(os.path.join(self.host_src, "Default"))
        with open(os.path.join(self.host_src, "Default", "Login Data"), "w") as f:
            f.write("gercek-sifre-verisi")
        self.data_dir = os.path.join(self.tmp.name, "isolated")

    def _profile(self):
        p = Profile(name="t")
        p.profile_data_dir = lambda user: self.data_dir  # yalnızca test: dizini sabitle
        return p

    def test_import_copies_host_profile_into_isolated_dir(self):
        s = Settings()
        p = self._profile()
        with mock.patch.object(apps, "host_profile_source", return_value=self.host_src), \
             mock.patch.object(apps, "_any_running", return_value=False), \
             mock.patch.object(system, "chown_to_user"):
            dest = apps.import_profile(s, p, "opera", dry_run=False)
        self.assertEqual(dest, os.path.join(self.data_dir, "opera"))
        with open(os.path.join(dest, "Default", "Login Data")) as f:
            self.assertEqual(f.read(), "gercek-sifre-verisi")

    def test_import_backs_up_existing_isolated_profile(self):
        s = Settings()
        p = self._profile()
        existing = os.path.join(self.data_dir, "opera")
        os.makedirs(existing)
        with open(os.path.join(existing, "marker.txt"), "w") as f:
            f.write("eski-izole-veri")
        with mock.patch.object(apps, "host_profile_source", return_value=self.host_src), \
             mock.patch.object(apps, "_any_running", return_value=False), \
             mock.patch.object(system, "chown_to_user"):
            apps.import_profile(s, p, "opera", dry_run=False)
        backups = [d for d in os.listdir(self.data_dir) if d.startswith("opera.bak-")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.data_dir, backups[0], "marker.txt")) as f:
            self.assertEqual(f.read(), "eski-izole-veri")

    def test_import_refuses_if_app_running(self):
        s = Settings()
        p = self._profile()
        with mock.patch.object(apps, "_any_running", return_value=True):
            with self.assertRaises(apps.ImportProfileError):
                apps.import_profile(s, p, "opera", dry_run=False)

    def test_import_refuses_if_no_host_source(self):
        s = Settings()
        p = self._profile()
        with mock.patch.object(apps, "host_profile_source", return_value=""), \
             mock.patch.object(apps, "_any_running", return_value=False):
            with self.assertRaises(apps.ImportProfileError):
                apps.import_profile(s, p, "opera", dry_run=False)

    def test_import_skips_fifo_without_crashing(self):
        """Kaynak profilde bir FIFO (ör. Opera'nın oauc_pipe'ı) olsa bile kopyalama patlamaz."""
        os.mkfifo(os.path.join(self.host_src, "oauc_pipe"))
        s = Settings()
        p = self._profile()
        with mock.patch.object(apps, "host_profile_source", return_value=self.host_src), \
             mock.patch.object(apps, "_any_running", return_value=False), \
             mock.patch.object(system, "chown_to_user"):
            dest = apps.import_profile(s, p, "opera", dry_run=False)
        with open(os.path.join(dest, "Default", "Login Data")) as f:
            self.assertEqual(f.read(), "gercek-sifre-verisi")
        self.assertNotIn("oauc_pipe", os.listdir(dest))

    def test_import_unsupported_app_rejected(self):
        s = Settings()
        p = self._profile()
        with self.assertRaises(apps.ImportProfileError):
            apps.import_profile(s, p, "xterm", dry_run=False)


if __name__ == "__main__":
    unittest.main()
