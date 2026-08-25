"""usage.py testleri: kalıcı veri kullanım günlüğü (gerçek kullanıcı dizinine dokunmaz)."""
import os
import tempfile
import time
import unittest
from unittest import mock

from tether_isolator import usage


class TestUsage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "usage.json")
        self._patch = mock.patch.object(usage, "_path", return_value=self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_add_creates_today_bucket(self):
        usage.add(100, 50)
        data = usage.load()
        day = time.strftime("%Y-%m-%d")
        self.assertEqual(data[day], {"rx": 100, "tx": 50})

    def test_add_accumulates_same_day(self):
        usage.add(100, 50)
        usage.add(200, 20)
        rx, tx = usage.today_bytes()
        self.assertEqual((rx, tx), (300, 70))

    def test_add_noop_for_zero_delta_does_not_create_file(self):
        usage.add(0, 0)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(usage.load(), {})

    def test_add_ignores_negative_delta(self):
        usage.add(-5, -5)
        self.assertFalse(os.path.exists(self.path))

    def test_month_bytes_aggregates_multiple_days(self):
        month = time.strftime("%Y-%m")
        data = {
            f"{month}-01": {"rx": 10, "tx": 1},
            f"{month}-02": {"rx": 20, "tx": 2},
            "1999-01-01": {"rx": 999, "tx": 999},  # farklı ay: sayılmamalı
        }
        rx, tx = usage.month_bytes(data)
        self.assertEqual((rx, tx), (30, 3))

    def test_today_bytes_missing_day_returns_zero(self):
        rx, tx = usage.today_bytes({})
        self.assertEqual((rx, tx), (0, 0))

    def test_add_prunes_entries_older_than_retention(self):
        old_day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 200 * 86400))
        with open(self.path, "w") as f:
            import json
            json.dump({old_day: {"rx": 1, "tx": 1}}, f)
        usage.add(10, 10)
        data = usage.load()
        self.assertNotIn(old_day, data)

    def test_add_prunes_malformed_keys(self):
        with open(self.path, "w") as f:
            import json
            json.dump({"not-a-date": {"rx": 1, "tx": 1}}, f)
        usage.add(10, 10)
        data = usage.load()
        self.assertNotIn("not-a-date", data)

    def test_save_is_atomic_and_0600(self):
        usage.add(10, 10)
        self.assertTrue(os.path.exists(self.path))
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertFalse(os.path.exists(self.path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
