"""Video/müzik yükleme engelleme (Faz 5, tarayıcı seviyesi).

İki farklı mekanizma, çünkü tarayıcılar farklı yetenekler sunuyor:
  * Firefox: MediaSource API'sini pref ile KAPATIR (alan adından bağımsız).
  * Chromium ailesi: MSE kapatılamadığı için tarayıcının KENDİ ad
    çözümleyicisi (`--host-resolver-rules`) medya CDN'lerini ölü adrese eşler.

Her ikisi de `data_saver.enabled`'dan bağımsız çalışır.
"""
import os
import shlex
import tempfile
import unittest
from unittest import mock

from tether_isolator import apps, system
from tether_isolator.config import Profile, Settings


def _prof(**kw):
    kw.setdefault("auto_reconnect", False)
    return Profile(**kw)


def _argv_for(program, **ds_kwargs):
    """apps.launch'ın ürettiği argv listesini döndürür (süreç başlatmadan)."""
    s = Settings()
    p = _prof(name="t")
    for k, v in ds_kwargs.items():
        setattr(p.data_saver, k, v)
    with mock.patch.object(apps, "_spawn", return_value=1) as spawn:
        apps.launch(s, p, program, dry_run=True)
    return spawn.call_args[0][2]


class TestChromiumHostResolverRules(unittest.TestCase):
    def _resolver_arg(self, argv):
        hits = [a for a in argv if a.startswith("--host-resolver-rules=")]
        return hits[0] if hits else ""

    def test_disabled_adds_no_resolver_rules(self):
        argv = _argv_for("google-chrome")   # media_level = "off"
        self.assertEqual(self._resolver_arg(argv), "")

    def test_enabled_adds_resolver_rules(self):
        argv = _argv_for("chromium", media_level="blocked")
        self.assertTrue(self._resolver_arg(argv))

    def test_rules_are_a_single_argv_token(self):
        """Kurallar boşluk içerir ('MAP host ip'); tek bir argümanda kalmalı."""
        argv = _argv_for("google-chrome", media_level="blocked")
        arg = self._resolver_arg(argv)
        self.assertIn("MAP googlevideo.com", arg)
        # Boşluklu değer yanlış tırnaklanırsa ayrı token'lara bölünür → 'MAP' tek
        # başına bir argv öğesi olarak GÖRÜNMEMELİ.
        self.assertNotIn("MAP", argv)

    def test_covers_wildcard_and_apex_for_every_host(self):
        argv = _argv_for("google-chrome", media_level="blocked")
        arg = self._resolver_arg(argv)
        for host in apps._MEDIA_BLOCK_HOSTS:
            self.assertIn(f"MAP {host} {apps._MEDIA_BLOCK_SINK}", arg)
            self.assertIn(f"MAP *.{host} {apps._MEDIA_BLOCK_SINK}", arg)

    def test_youtube_segment_host_pattern_is_covered(self):
        """YouTube istek başına rr3---sn-xxx.googlevideo.com üretir — joker şart."""
        arg = self._resolver_arg(_argv_for("google-chrome", media_level="blocked"))
        self.assertIn("MAP *.googlevideo.com", arg)

    def test_main_site_domains_not_blocked(self):
        """Site açılmaya devam etmeli — ana alan adları listede OLMAMALI."""
        arg = self._resolver_arg(_argv_for("google-chrome", media_level="blocked"))
        for site in ("youtube.com", "netflix.com", "spotify.com", "twitch.tv"):
            self.assertNotIn(f"MAP {site} ", arg)

    def test_thumbnails_not_blocked(self):
        """ytimg (küçük resim) engellenmez; aksi halde YouTube arayüzü bozulur."""
        arg = self._resolver_arg(_argv_for("google-chrome", media_level="blocked"))
        self.assertNotIn("ytimg", arg)

    def test_applies_to_whole_chromium_family(self):
        for prog in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "brave-browser", "opera"):
            argv = _argv_for(prog, media_level="blocked")
            self.assertTrue(self._resolver_arg(argv), f"{prog} için kural yok")

    def test_independent_of_data_saver_enabled(self):
        argv = _argv_for("brave-browser", media_level="blocked", enabled=False)
        self.assertTrue(self._resolver_arg(argv))
        self.assertNotIn("--autoplay-policy=user-gesture-required", argv)

    def test_combines_with_data_saver_flags(self):
        argv = _argv_for("chromium", media_level="blocked", enabled=True)
        joined = " ".join(argv)
        self.assertTrue(self._resolver_arg(argv))
        self.assertIn("--autoplay-policy=user-gesture-required", joined)

    def test_non_browser_app_unaffected(self):
        argv = _argv_for("code", media_level="blocked")
        self.assertEqual(self._resolver_arg(argv), "")

    def test_flag_is_shell_safe(self):
        """Bayrak shlex.split'ten geçtiği için kabuk-güvenli üretilmeli."""
        flag = apps._chromium_media_block_flag()
        self.assertEqual(len(shlex.split(flag)), 1)

    def test_quality_levels_do_not_hard_block(self):
        """360p/720p SERT engel değil — kaliteyi tavanla düşürür (bkz. manager)."""
        for lvl in ("360p", "720p"):
            argv = _argv_for("google-chrome", media_level=lvl)
            self.assertEqual(self._resolver_arg(argv), "",
                             f"{lvl} tarayıcıyı engellememeli")


class TestLaunchTimeMediaLevelRecorded(unittest.TestCase):
    """Uygulamalar hangi medya kademesiyle açıldıysa o kaydedilmeli.

    Tarayıcı bayrakları yalnızca başlatma anında uygulanabildiğinden, panel
    "bu pencere eski ayarla açıldı" uyarısını bu alana bakarak gösterir.
    """

    def _mgr_with_apps(self, media_level):
        from tether_isolator.manager import Manager
        from tether_isolator.config import Settings as S
        m = Manager(S(), dry_run=True)
        p = _prof(name="d", uplink="usb0", apps=["opera"])
        p.data_saver.media_level = media_level
        m.start_session(p, "usb0")
        return m

    def test_start_session_records_level(self):
        m = self._mgr_with_apps("blocked")
        self.assertEqual(m.state.apps[0].media_level, "blocked")

    def test_records_off_when_unrestricted(self):
        m = self._mgr_with_apps("off")
        self.assertEqual(m.state.apps[0].media_level, "off")

    def test_changing_level_does_not_rewrite_running_apps(self):
        """ASIL NOKTA: sürgüyü değiştirmek çalışan örneğin kaydını DEĞİŞTİRMEZ."""
        m = self._mgr_with_apps("off")
        m.active_profile.data_saver.media_level = "blocked"
        self.assertEqual(m.state.apps[0].media_level, "off")   # hâlâ eski ayar
        self.assertNotEqual(m.state.apps[0].media_level,
                            m.active_profile.data_saver.media_level)

    def test_restart_apps_refreshes_recorded_level(self):
        """'Şimdi uygula' → yeniden başlatma kaydı güncel kademeye taşımalı."""
        m = self._mgr_with_apps("off")
        m.active_profile.data_saver.media_level = "blocked"
        m.restart_apps()
        self.assertTrue(m.state.apps)
        for a in m.state.apps:
            self.assertEqual(a.media_level, "blocked")

    def test_level_survives_state_roundtrip(self):
        """Daemon yeniden başlasa bile kayıt korunmalı (adopt yolu)."""
        from tether_isolator.state import AppProcess, RuntimeState
        st = RuntimeState(phase="online")
        st.apps = [AppProcess(command="opera", pid=1, media_level="blocked")]
        self.assertEqual(st.to_dict()["apps"][0]["media_level"], "blocked")


class TestLegacyBlockMediaMigration(unittest.TestCase):
    """Eski profillerdeki boolean `block_media` yeni kademeye eşlenmeli."""

    def test_legacy_true_becomes_blocked(self):
        p = Profile.from_dict({"name": "x", "data_saver": {"block_media": True}})
        self.assertEqual(p.data_saver.media_level, "blocked")

    def test_legacy_false_becomes_off(self):
        p = Profile.from_dict({"name": "x", "data_saver": {"block_media": False}})
        self.assertEqual(p.data_saver.media_level, "off")

    def test_new_field_wins_over_legacy(self):
        p = Profile.from_dict({"name": "x", "data_saver": {
            "block_media": True, "media_level": "360p"}})
        self.assertEqual(p.data_saver.media_level, "360p")

    def test_absent_defaults_to_off(self):
        p = Profile.from_dict({"name": "x", "data_saver": {}})
        self.assertEqual(p.data_saver.media_level, "off")


class TestFirefoxMediaBlock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = os.path.join(self.tmp.name, "isolated", "firefox")

    def _profile(self, *, media_level="blocked", data_saver_enabled=False):
        p = _prof(name="t")
        p.profile_data_dir = lambda user: os.path.dirname(self.data_dir)
        p.data_saver.media_level = media_level
        p.data_saver.enabled = data_saver_enabled
        return p

    def _launch(self, p):
        s = Settings()
        with mock.patch.object(apps, "_spawn", return_value=1), \
             mock.patch.object(system, "chown_to_user"):
            apps.launch(s, p, "firefox", dry_run=False)

    def _user_js(self):
        with open(os.path.join(self.data_dir, "user.js")) as f:
            return f.read()

    def test_enabled_disables_mediasource_and_formats(self):
        self._launch(self._profile(media_level="blocked"))
        content = self._user_js()
        self.assertIn(apps._FIREFOX_MEDIA_MARKER_START, content)
        self.assertIn('user_pref("media.mediasource.enabled", false);', content)
        self.assertIn('user_pref("media.mp4.enabled", false);', content)

    def test_firefox_does_not_get_resolver_flag(self):
        """Firefox `--host-resolver-rules` desteklemez; ona pref yolu uygulanır."""
        argv = _argv_for("firefox", media_level="blocked")
        self.assertFalse([a for a in argv if a.startswith("--host-resolver-rules")])

    def test_independent_of_data_saver_enabled(self):
        self._launch(self._profile(media_level="blocked", data_saver_enabled=False))
        content = self._user_js()
        self.assertIn(apps._FIREFOX_MEDIA_MARKER_START, content)
        self.assertNotIn(apps._FIREFOX_MARKER_START, content)

    def test_disabled_writes_nothing_when_no_prior_file(self):
        self._launch(self._profile(media_level="off"))
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "user.js")))

    def test_toggling_off_removes_block(self):
        self._launch(self._profile(media_level="blocked"))
        self._launch(self._profile(media_level="off"))
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "user.js")))

    def test_coexists_with_data_saver_block_without_clobbering(self):
        """Aynı user.js'te iki ayrı özellik bloğu bir arada, birbirini SİLMEDEN durmalı."""
        self._launch(self._profile(media_level="blocked", data_saver_enabled=True))
        content = self._user_js()
        self.assertIn(apps._FIREFOX_MARKER_START, content)
        self.assertIn(apps._FIREFOX_MEDIA_MARKER_START, content)

        self._launch(self._profile(media_level="off", data_saver_enabled=True))
        content = self._user_js()
        self.assertIn(apps._FIREFOX_MARKER_START, content)
        self.assertNotIn(apps._FIREFOX_MEDIA_MARKER_START, content)

    def test_preserves_user_own_lines(self):
        os.makedirs(self.data_dir, exist_ok=True)
        with open(os.path.join(self.data_dir, "user.js"), "w") as f:
            f.write('user_pref("my.custom.pref", true);\n')
        self._launch(self._profile(media_level="blocked"))
        content = self._user_js()
        self.assertIn('user_pref("my.custom.pref", true);', content)
        self.assertIn(apps._FIREFOX_MEDIA_MARKER_START, content)


if __name__ == "__main__":
    unittest.main()
