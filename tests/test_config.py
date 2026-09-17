"""config.py testleri: profil serileştirme + ayar yükle/kaydet."""
import unittest

from tether_isolator.config import Profile, Settings
from tests._util import IsolatedPaths


class TestProfile(unittest.TestCase):
    def test_from_dict_roundtrip(self):
        src = {
            "name": "iş", "uplink": "wlan0", "uplink_kind": "wifi",
            "apps": ["opera"], "wifi_ssid": "Ev", "wifi_password": "gizli",
        }
        p = Profile.from_dict(src)
        self.assertEqual(p.name, "iş")
        self.assertEqual(p.uplink_kind, "wifi")
        self.assertEqual(p.apps, ["opera"])

    def test_from_dict_ignores_unknown_keys(self):
        p = Profile.from_dict({"name": "x", "bogus_key": 123})
        self.assertEqual(p.name, "x")
        self.assertFalse(hasattr(p, "bogus_key"))

    def test_profile_data_dir_is_per_profile(self):
        p = Profile(name="kişisel")
        d = p.profile_data_dir("user")
        self.assertIn("profiles", d)
        self.assertTrue(d.endswith("kişisel"))


class TestSettings(unittest.TestCase):
    def test_default_has_default_profile(self):
        with IsolatedPaths():
            s = Settings.load()   # dosya yok → varsayılan
            self.assertIn("default", s.profiles)
            self.assertEqual(s.active_profile, "default")

    def test_save_load_roundtrip(self):
        with IsolatedPaths():
            s = Settings()
            s.profiles["default"] = Profile(name="default", uplink="usb0",
                                            apps=["terminator"])
            s.profiles["iş"] = Profile(name="iş", uplink_kind="wifi",
                                       wifi_ssid="Ofis")
            s.active_profile = "iş"
            s.save()

            s2 = Settings.load()
            self.assertEqual(set(s2.profiles), {"default", "iş"})
            self.assertEqual(s2.active_profile, "iş")
            self.assertEqual(s2.profiles["default"].apps, ["terminator"])
            self.assertEqual(s2.profiles["iş"].wifi_ssid, "Ofis")

    def test_profile_creates_missing(self):
        with IsolatedPaths():
            s = Settings()
            p = s.profile("yeni")
            self.assertEqual(p.name, "yeni")
            self.assertIn("yeni", s.profiles)


if __name__ == "__main__":
    unittest.main()
