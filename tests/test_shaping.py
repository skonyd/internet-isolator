"""shaping.py testleri: tc/ifb komut dizisi, ifb/cake yoksa geriye düşüş.

Gerçek komut çalıştırmaz (system.run FakeRun ile sahtelenir).
"""
import unittest
from unittest import mock

from tether_isolator import shaping
from tether_isolator.system import RunResult
from tests._util import FakeRun


class TestApplyHappyPath(unittest.TestCase):
    """ifb + cake her adımda başarılı olduğunda."""

    def test_download_and_upload_use_cake_and_tbf(self):
        fake = FakeRun(RunResult(0, "", ""))
        with mock.patch.object(shaping, "run", fake):
            result = shaping.apply("tether_zone", "usb0", down_kbit=2000, up_kbit=1000)
        self.assertEqual(result["download_method"], "cake")
        self.assertTrue(result["upload_applied"])
        # upload: doğrudan iface üzerinde tbf
        self.assertTrue(fake.ran("tc", "qdisc", "add", "dev", "usb0", "root", "tbf",
                                 "rate", "1000kbit"))
        # download: ifb + mirred + cake
        self.assertTrue(fake.ran("ip", "link", "add", "tisor-ifb0", "type", "ifb"))
        self.assertTrue(fake.ran("tc", "qdisc", "add", "dev", "usb0", "handle", "ffff:", "ingress"))
        self.assertTrue(fake.ran("action", "mirred", "egress", "redirect", "dev", "tisor-ifb0"))
        self.assertTrue(fake.ran("tc", "qdisc", "add", "dev", "tisor-ifb0", "root", "cake",
                                 "bandwidth", "2000kbit"))
        # her ikisi de namespace İÇİNDE çalıştırılmalı
        self.assertTrue(all(c[:4] == ["ip", "netns", "exec", "tether_zone"] for c in fake.calls))

    def test_apply_always_removes_before_reapplying(self):
        fake = FakeRun(RunResult(0, "", ""))
        with mock.patch.object(shaping, "run", fake):
            shaping.apply("ns", "usb0", down_kbit=1000, up_kbit=0)
        # remove() önce çağrılmış olmalı: qdisc del komutları en başta
        del_indices = [i for i, c in enumerate(fake.calls) if "del" in c]
        add_indices = [i for i, c in enumerate(fake.calls) if "add" in c]
        self.assertTrue(del_indices)
        self.assertLess(min(del_indices), min(add_indices))

    def test_only_upload_requested_no_ifb_calls(self):
        fake = FakeRun(RunResult(0, "", ""))
        with mock.patch.object(shaping, "run", fake):
            result = shaping.apply("ns", "usb0", down_kbit=0, up_kbit=500)
        self.assertEqual(result["download_method"], "")
        self.assertTrue(result["upload_applied"])
        self.assertFalse(fake.ran("type", "ifb"))

    def test_zero_caps_apply_nothing(self):
        fake = FakeRun(RunResult(0, "", ""))
        with mock.patch.object(shaping, "run", fake):
            result = shaping.apply("ns", "usb0", down_kbit=0, up_kbit=0)
        self.assertEqual(result["download_method"], "")
        self.assertFalse(result["upload_applied"])
        # yalnızca remove()'un best-effort silme komutları çalışmış olmalı
        self.assertFalse(fake.ran("tbf"))
        self.assertFalse(fake.ran("cake"))


class TestApplyFallbacks(unittest.TestCase):
    def test_ifb_link_add_fails_falls_back_to_ingress_police(self):
        fake = FakeRun(RunResult(0, "", ""))
        fake.add_rule(lambda c: "ifb" in c and "add" in c and "link" in c,
                     RunResult(1, "", "İşlev desteklenmiyor"))
        with mock.patch.object(shaping, "run", fake):
            result = shaping.apply("ns", "usb0", down_kbit=700, up_kbit=0)
        self.assertEqual(result["download_method"], "police")
        self.assertTrue(fake.ran("police", "rate", "700kbit", "drop"))
        # ifb hiç kurulamadığı için cake/tbf denenmemeli
        self.assertFalse(fake.ran("cake"))

    def test_mirred_filter_fails_cleans_up_and_falls_back(self):
        fake = FakeRun(RunResult(0, "", ""))
        fake.add_rule(lambda c: "mirred" in c, RunResult(2, "", "filter reddedildi"))
        with mock.patch.object(shaping, "run", fake):
            result = shaping.apply("ns", "usb0", down_kbit=700, up_kbit=0)
        self.assertEqual(result["download_method"], "police")
        # ifb temizliği: qdisc del + link del çağrılmış olmalı (mirred'den sonra)
        self.assertTrue(fake.ran("tc", "qdisc", "del", "dev", "tisor-ifb0", "root"))
        self.assertTrue(fake.ran("ip", "link", "del", "tisor-ifb0"))

    def test_cake_unavailable_falls_back_to_tbf_on_ifb(self):
        fake = FakeRun(RunResult(0, "", ""))
        fake.add_rule(lambda c: "cake" in c, RunResult(1, "", "unknown qdisc"))
        with mock.patch.object(shaping, "run", fake):
            result = shaping.apply("ns", "usb0", down_kbit=2000, up_kbit=0)
        self.assertEqual(result["download_method"], "tbf")
        self.assertTrue(fake.ran("tc", "qdisc", "add", "dev", "tisor-ifb0", "root", "tbf",
                                 "rate", "2000kbit"))

    def test_cake_and_tbf_both_fail_falls_back_to_police(self):
        fake = FakeRun(RunResult(0, "", ""))
        fake.add_rule(lambda c: "cake" in c or ("tbf" in c and "tisor-ifb0" in c),
                     RunResult(1, "", "boom"))
        with mock.patch.object(shaping, "run", fake):
            result = shaping.apply("ns", "usb0", down_kbit=2000, up_kbit=0)
        self.assertEqual(result["download_method"], "police")

    def test_upload_failure_reported_but_download_still_applied(self):
        fake = FakeRun(RunResult(0, "", ""))
        fake.add_rule(lambda c: "1:" in c and "tbf" in c, RunResult(1, "", "boom"))
        with mock.patch.object(shaping, "run", fake):
            result = shaping.apply("ns", "usb0", down_kbit=1000, up_kbit=500)
        self.assertFalse(result["upload_applied"])
        self.assertEqual(result["download_method"], "cake")


class TestDryRun(unittest.TestCase):
    def test_apply_dry_run_executes_nothing(self):
        fake = FakeRun(RunResult(0, "", ""))
        with mock.patch.object(shaping, "run", fake):
            result = shaping.apply("ns", "usb0", down_kbit=1000, up_kbit=500, dry_run=True)
        self.assertEqual(fake.calls, [])
        self.assertEqual(result["download_method"], "cake")
        self.assertTrue(result["upload_applied"])

    def test_remove_dry_run_executes_nothing(self):
        fake = FakeRun(RunResult(0, "", ""))
        with mock.patch.object(shaping, "run", fake):
            shaping.remove("ns", "usb0", dry_run=True)
        self.assertEqual(fake.calls, [])


class TestRemove(unittest.TestCase):
    def test_remove_strips_iface_and_ifb(self):
        fake = FakeRun(RunResult(0, "", ""))
        with mock.patch.object(shaping, "run", fake):
            shaping.remove("ns", "usb0")
        self.assertTrue(fake.ran("tc", "qdisc", "del", "dev", "usb0", "root"))
        self.assertTrue(fake.ran("tc", "qdisc", "del", "dev", "usb0", "ingress"))
        self.assertTrue(fake.ran("tc", "qdisc", "del", "dev", "tisor-ifb0", "root"))
        self.assertTrue(fake.ran("ip", "link", "del", "tisor-ifb0"))


class TestIsActive(unittest.TestCase):
    def test_true_when_tbf_present(self):
        fake = FakeRun(RunResult(0, "qdisc tbf 1: root refcnt 2 rate 1000Kbit", ""))
        with mock.patch.object(shaping, "run", fake):
            self.assertTrue(shaping.is_active("ns", "usb0"))

    def test_true_when_ingress_present(self):
        fake = FakeRun(RunResult(0, "qdisc ingress ffff: parent ffff:fff1 ----", ""))
        with mock.patch.object(shaping, "run", fake):
            self.assertTrue(shaping.is_active("ns", "usb0"))

    def test_false_when_default_qdisc_only(self):
        fake = FakeRun(RunResult(0, "qdisc noqueue 0: root refcnt 2", ""))
        with mock.patch.object(shaping, "run", fake):
            self.assertFalse(shaping.is_active("ns", "usb0"))

    def test_false_when_command_fails(self):
        fake = FakeRun(RunResult(1, "", "Cannot find device"))
        with mock.patch.object(shaping, "run", fake):
            self.assertFalse(shaping.is_active("ns", "usb0"))


if __name__ == "__main__":
    unittest.main()
