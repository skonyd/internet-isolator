"""state.py testleri: olay günlüğü sınırı, to_dict, persist/read."""
import unittest

from tether_isolator.state import AppProcess, RuntimeState
from tether_isolator import state as st
from tests._util import IsolatedPaths


class TestRuntimeState(unittest.TestCase):
    def test_log_event_caps_at_100(self):
        s = RuntimeState()
        for i in range(150):
            s.log_event("info", f"olay {i}")
        self.assertEqual(len(s.events), 100)
        self.assertEqual(s.events[-1]["msg"], "olay 149")   # en yeni korunur
        self.assertEqual(s.events[0]["msg"], "olay 50")

    def test_to_dict_has_uptime_apps(self):
        s = RuntimeState()
        s.apps = [AppProcess("opera", 1, running=True),
                  AppProcess("firefox", 2, running=False)]
        d = s.to_dict()
        self.assertEqual(d["uptime_apps"], 1)
        self.assertEqual(len(d["apps"]), 2)

    def test_persist_and_read_roundtrip(self):
        with IsolatedPaths():
            s = RuntimeState(phase="online", uplink="wlan0", online=True)
            s.persist()
            data = st.RuntimeState.read()
            self.assertIsNotNone(data)
            self.assertEqual(data["phase"], "online")
            self.assertEqual(data["uplink"], "wlan0")
            self.assertTrue(data["online"])

    def test_read_returns_none_when_absent(self):
        with IsolatedPaths():
            self.assertIsNone(RuntimeState.read())


if __name__ == "__main__":
    unittest.main()
