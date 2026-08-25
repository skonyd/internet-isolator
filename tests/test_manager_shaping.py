"""manager.py: bant genişliği tavanının (Faz 4) oturum yaşam döngüsüne bağlanması.

Gerçek komut çalıştırmaz (`shaping.apply/remove` doğrudan sahtelenir) — burada
test edilen, MANAGER'IN doğru anlarda doğru arayüzle çağrı yapıp yapmadığı.
Komutların kendi doğruluğu tests/test_shaping.py'de.
"""
import unittest
from unittest import mock

from tether_isolator.config import Profile, Settings
from tether_isolator.engine import EngineError
from tether_isolator.manager import Manager, _LEVEL_CAPS_KBIT, _MEDIA_QUALITY_CAP_KBIT


def _prof(**kw):
    kw.setdefault("auto_reconnect", False)
    return Profile(**kw)


def _ds_prof(level="balanced", **extra):
    p = _prof(name="d", uplink="usb0")
    p.data_saver.enabled = True
    p.data_saver.level = level
    for k, v in extra.items():
        setattr(p.data_saver, k, v)
    return p


class TestEffectiveCaps(unittest.TestCase):
    def test_level_presets(self):
        m = Manager(Settings(), dry_run=True)
        for level, expected in _LEVEL_CAPS_KBIT.items():
            ds = _ds_prof(level=level).data_saver
            self.assertEqual(m._effective_caps(ds), expected)

    def test_explicit_cap_overrides_level(self):
        m = Manager(Settings(), dry_run=True)
        ds = _ds_prof(level="strict", cap_down_kbit=5000, cap_up_kbit=2500).data_saver
        self.assertEqual(m._effective_caps(ds), (5000, 2500))

    def test_partial_override_keeps_other_from_level(self):
        m = Manager(Settings(), dry_run=True)
        ds = _ds_prof(level="balanced", cap_down_kbit=9000).data_saver
        self.assertEqual(m._effective_caps(ds), (9000, 1000))


class TestMediaQualityCaps(unittest.TestCase):
    """Medya kademesi (360p/720p) bant genişliği tavanına dönüşür."""

    def _ds(self, **kw):
        p = _prof(name="d")
        for k, v in kw.items():
            setattr(p.data_saver, k, v)
        return p.data_saver

    def test_media_level_caps_apply_without_data_saver(self):
        """Genel veri tasarrufu KAPALIYKEN de kalite tavanı uygulanmalı."""
        m = Manager(Settings(), dry_run=True)
        self.assertEqual(m._effective_caps(self._ds(media_level="144p")), (400, 0))
        self.assertEqual(m._effective_caps(self._ds(media_level="360p")), (1000, 0))
        self.assertEqual(m._effective_caps(self._ds(media_level="720p")), (3000, 0))

    def test_quality_caps_are_strictly_increasing(self):
        """Sürgü soldan sağa gevşemeli: 144p < 360p < 720p."""
        caps = [_MEDIA_QUALITY_CAP_KBIT[k] for k in ("144p", "360p", "720p")]
        self.assertEqual(caps, sorted(caps))
        self.assertEqual(len(set(caps)), 3)

    def test_144p_beats_strict_data_saver(self):
        """144p (400) strict'ten (700) daha düşük → 144p kazanmalı."""
        m = Manager(Settings(), dry_run=True)
        ds = self._ds(media_level="144p", enabled=True, level="strict")
        self.assertEqual(m._effective_caps(ds)[0], 400)

    def test_off_and_blocked_add_no_cap(self):
        m = Manager(Settings(), dry_run=True)
        for lvl in ("off", "blocked"):
            self.assertEqual(m._effective_caps(self._ds(media_level=lvl)), (0, 0))

    def test_lower_of_media_and_data_saver_wins(self):
        """720p (3000) + strict (700) → 700; hiçbir ayarın vaadi çiğnenmez."""
        m = Manager(Settings(), dry_run=True)
        ds = self._ds(media_level="720p", enabled=True, level="strict")
        self.assertEqual(m._effective_caps(ds), (700, 300))

    def test_media_cap_wins_when_stricter_than_data_saver(self):
        m = Manager(Settings(), dry_run=True)
        ds = self._ds(media_level="360p", enabled=True, level="balanced")
        self.assertEqual(m._effective_caps(ds)[0], 1000)   # 1000 < 2000

    def test_media_cap_beats_manual_cap_when_lower(self):
        m = Manager(Settings(), dry_run=True)
        ds = self._ds(media_level="360p", enabled=True, cap_down_kbit=8000)
        self.assertEqual(m._effective_caps(ds)[0], 1000)

    def test_quality_level_triggers_shaping_apply(self):
        """Veri tasarrufu kapalı + 360p → yine de tc uygulanmalı."""
        m = Manager(Settings(), dry_run=False)
        m._active_profile = _prof(name="d", uplink="usb0")
        m._active_profile.data_saver.media_level = "360p"
        with mock.patch("tether_isolator.manager.shaping.apply",
                        return_value={"download_method": "cake", "upload_applied": False}) as ap:
            m._apply_shaping("usb0")
        ap.assert_called_once_with(m.s.namespace, "usb0", down_kbit=1000, up_kbit=0,
                                   dry_run=False)
        self.assertTrue(m.state.shaping_active)

    def test_blocked_level_alone_does_not_shape(self):
        """'blocked' tarayıcı seviyesinde çalışır; tc tavanı gerektirmez."""
        m = Manager(Settings(), dry_run=False)
        m._active_profile = _prof(name="d", uplink="usb0")
        m._active_profile.data_saver.media_level = "blocked"
        with mock.patch("tether_isolator.manager.shaping.apply") as ap, \
             mock.patch("tether_isolator.manager.shaping.remove") as rm:
            m._apply_shaping("usb0")
        ap.assert_not_called()
        rm.assert_called_once()


class TestApplyRemoveShaping(unittest.TestCase):
    def test_disabled_calls_remove_not_apply(self):
        m = Manager(Settings(), dry_run=False)
        m._active_profile = _prof(name="d")   # data_saver.enabled = False
        with mock.patch("tether_isolator.manager.shaping.apply") as ap, \
             mock.patch("tether_isolator.manager.shaping.remove") as rm:
            m._apply_shaping("usb0")
        ap.assert_not_called()
        rm.assert_called_once()
        self.assertFalse(m.state.shaping_active)

    def test_light_level_has_no_caps_calls_remove(self):
        m = Manager(Settings(), dry_run=False)
        m._active_profile = _ds_prof(level="light")
        with mock.patch("tether_isolator.manager.shaping.apply") as ap, \
             mock.patch("tether_isolator.manager.shaping.remove") as rm:
            m._apply_shaping("usb0")
        ap.assert_not_called()
        rm.assert_called_once()

    def test_balanced_level_applies_with_preset_caps(self):
        m = Manager(Settings(), dry_run=False)
        m._active_profile = _ds_prof(level="balanced")
        with mock.patch("tether_isolator.manager.shaping.apply",
                        return_value={"download_method": "cake", "upload_applied": True}) as ap:
            m._apply_shaping("usb0")
        ap.assert_called_once_with(m.s.namespace, "usb0", down_kbit=2000, up_kbit=1000,
                                   dry_run=False)
        self.assertTrue(m.state.shaping_active)
        self.assertEqual(m.state.shaping_down_kbit, 2000)
        self.assertEqual(m.state.shaping_up_kbit, 1000)
        self.assertEqual(m.state.shaping_method, "cake")

    def test_partial_apply_reflects_only_succeeded_side(self):
        m = Manager(Settings(), dry_run=False)
        m._active_profile = _ds_prof(level="balanced")
        with mock.patch("tether_isolator.manager.shaping.apply",
                        return_value={"download_method": "", "upload_applied": True}):
            m._apply_shaping("usb0")
        self.assertTrue(m.state.shaping_active)
        self.assertEqual(m.state.shaping_down_kbit, 0)
        self.assertEqual(m.state.shaping_up_kbit, 1000)

    def test_apply_exception_marks_inactive_and_logs_warning(self):
        m = Manager(Settings(), dry_run=False)
        m._active_profile = _ds_prof(level="balanced")
        with mock.patch("tether_isolator.manager.shaping.apply",
                        side_effect=RuntimeError("boom")):
            m._apply_shaping("usb0")
        self.assertFalse(m.state.shaping_active)
        self.assertTrue(any("uygulanamadı" in e["msg"] for e in m.state.events))

    def test_no_active_profile_is_noop(self):
        m = Manager(Settings(), dry_run=False)
        with mock.patch("tether_isolator.manager.shaping.apply") as ap, \
             mock.patch("tether_isolator.manager.shaping.remove") as rm:
            m._apply_shaping("usb0")
        ap.assert_not_called()
        rm.assert_not_called()

    def test_remove_resets_state_fields(self):
        m = Manager(Settings(), dry_run=False)
        m.state.shaping_active = True
        m.state.shaping_down_kbit = 2000
        m.state.shaping_up_kbit = 1000
        m.state.shaping_method = "cake"
        with mock.patch("tether_isolator.manager.shaping.remove"):
            m._remove_shaping("usb0")
        self.assertFalse(m.state.shaping_active)
        self.assertEqual(m.state.shaping_down_kbit, 0)
        self.assertEqual(m.state.shaping_method, "")


class TestSessionLifecycleWiring(unittest.TestCase):
    """start/stop/switch/reconnect'in doğru anlarda apply/remove çağırdığını doğrular."""

    def test_start_session_applies_shaping_after_uplink_join(self):
        m = Manager(Settings(), dry_run=True)
        with mock.patch.object(m, "_apply_shaping") as ap:
            m.start_session(_ds_prof(level="balanced"), "usb0")
        ap.assert_called_once_with("usb0")

    def test_stop_session_removes_shaping_before_teardown(self):
        m = Manager(Settings(), dry_run=True)
        m.start_session(_ds_prof(level="balanced"), "usb0")
        calls = []
        with mock.patch.object(m, "_remove_shaping", side_effect=lambda i: calls.append("remove")), \
             mock.patch.object(m.engine, "teardown", side_effect=lambda i: calls.append("teardown")):
            m.stop_session()
        self.assertEqual(calls, ["remove", "teardown"])

    def test_switch_uplink_removes_old_before_leaving_and_applies_new(self):
        m = Manager(Settings(), dry_run=False)
        prof = _ds_prof(level="balanced", uplink="usb0")
        m._active_profile = prof
        m._active_iface = "usb0"
        m.state.phase = "online"
        order = []
        with mock.patch.object(m, "_remove_shaping",
                               side_effect=lambda i: order.append(("remove", i))), \
             mock.patch.object(m, "_apply_shaping",
                               side_effect=lambda i: order.append(("apply", i))), \
             mock.patch.object(m.engine, "stop_dhcp"), \
             mock.patch.object(m.engine, "move_uplink_out"), \
             mock.patch.object(m, "_bring_uplink_into_ns"), \
             mock.patch.object(m.engine, "start_dhcp"), \
             mock.patch.object(m, "_refresh_network"), \
             mock.patch("tether_isolator.manager.system._classify", return_value="usb"):
            m.switch_uplink("wlan0")
        self.assertEqual(order, [("remove", "usb0"), ("apply", "wlan0")])

    def test_force_reconnect_reapplies_shaping(self):
        m = Manager(Settings(), dry_run=True)
        m.start_session(_ds_prof(level="balanced"), "usb0")
        with mock.patch.object(m, "_apply_shaping") as ap:
            m.force_reconnect()
        ap.assert_called_once_with("usb0")

    def test_restart_apps_without_session_still_raises_engineerror(self):
        # Sağlamlık: bant genişliği entegrasyonu diğer korumaları bozmamış.
        m = Manager(Settings(), dry_run=True)
        with self.assertRaises(EngineError):
            m.switch_uplink("wlan0")


class TestReassertShaping(unittest.TestCase):
    def test_noop_when_idle(self):
        m = Manager(Settings(), dry_run=True)
        with mock.patch.object(m, "_apply_shaping") as ap:
            m.reassert_shaping()
        ap.assert_not_called()

    def test_applies_when_session_active(self):
        m = Manager(Settings(), dry_run=True)
        m._active_iface = "usb0"
        m.state.phase = "online"
        with mock.patch.object(m, "_apply_shaping") as ap:
            m.reassert_shaping()
        ap.assert_called_once_with("usb0")


if __name__ == "__main__":
    unittest.main()
