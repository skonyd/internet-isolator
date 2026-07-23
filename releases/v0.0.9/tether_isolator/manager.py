"""Orkestratör — tüm parçaları birleştirir ve oturum yaşam döngüsünü yönetir.

Sorumluluklar:
  * start_session / stop_session
  * relay aç/kapa
  * sürekli çalışan watchdog (dayanıklılık): uplink kopup geri geldiğinde
    uygulamaları ÖLDÜRMEDEN arayüzü tekrar namespace'e alıp DHCP'yi yeniler.

Watchdog'un temel fikri: uygulamalar namespace içindedir ve namespace
yaşamaya devam eder. Yalnızca fiziksel uplink gelip gider. Telefon çıkarılıp
takıldığında arayüz host'ta yeniden belirir; watchdog bunu görür ve sessizce
namespace'e geri taşır. Uygulamaların soketleri kopsa da süreçleri yaşar;
tarayıcılar kendiliğinden yeniden dener.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

from . import apps, system
from .config import Profile, Settings
from .engine import Engine, EngineError
from .relay import Relay
from .state import AppProcess, RuntimeState
from .vpn import VPN

log = logging.getLogger("tether.manager")


class Manager:
    def __init__(self, settings: Settings, *, dry_run: bool = False):
        self.s = settings
        self.dry = dry_run
        self.engine = Engine(settings, dry_run=dry_run)
        self.relay = Relay(settings, dry_run=dry_run)
        self.vpn = VPN(settings, dry_run=dry_run)
        self.state = RuntimeState(namespace=settings.namespace)
        self._lock = threading.RLock()
        self._wd_stop = threading.Event()
        self._wd_thread: threading.Thread | None = None
        self._active_iface: str | None = None
        self._active_profile: Profile | None = None

    # ---------------------------------------------------------------- start
    def start_session(self, profile: Profile, uplink: str) -> None:
        with self._lock:
            if self.state.phase in ("online", "starting", "degraded"):
                raise EngineError("Zaten etkin bir oturum var; önce durdurun.")
            self._active_iface = uplink
            self._active_profile = profile
            self.state = RuntimeState(
                phase="starting", profile=profile.name,
                namespace=self.s.namespace, uplink=uplink,
            )
            self._event("info", f"Oturum başlatılıyor: profil={profile.name} uplink={uplink}")

            try:
                self.engine.ensure_namespace(profile)

                # Uplink'i izole alana al — WiFi ise PHY taşıma + bağlanma,
                # USB/Ethernet ise doğrudan arayüz taşıma (ortak yol).
                self._bring_uplink_into_ns(uplink)

                self._event("info", "IP adresi alınıyor (DHCP)...")
                self.engine.start_dhcp(uplink)
            except Exception as e:
                # Yarım kalan kurulumu temizle ki phase 'starting'te takılıp
                # tekrar başlatmayı engellemesin (ör. WiFi association hatası).
                self._event("error", f"Başlatılamadı: {e}")
                try:
                    self.engine.teardown(uplink)
                except Exception as te:  # noqa: BLE001
                    log.warning("başarısız başlatma temizliğinde: %s", te)
                self.state = RuntimeState(phase="idle", namespace=self.s.namespace)
                self._active_iface = None
                self._active_profile = None
                self.state.persist()
                raise

            self._refresh_network()
            if profile.relay.enabled_by_default:
                self.enable_relay(profile)

            # Profilde VPN tanımlıysa izole alan içinde otomatik bağlan
            # (başarısızlık oturumu düşürmesin — uygulamalar yine açılsın).
            if profile.vpn_config:
                try:
                    self.connect_vpn(profile.vpn_config)
                except Exception as e:  # noqa: BLE001
                    self._event("warn", f"VPN otomatik bağlanamadı: {e}")

            # Uygulamaları başlat
            for prog in profile.apps:
                try:
                    pid = apps.launch(self.s, profile, prog, dry_run=self.dry)
                    self.state.apps.append(AppProcess(command=prog, pid=pid))
                    self._event("info", f"Uygulama başlatıldı: {prog} (pid={pid})")
                except Exception as e:  # tek uygulama patlarsa oturum ölmesin
                    self._event("error", f"{prog} başlatılamadı: {e}")

            self.state.phase = "online" if self.state.online else "degraded"
            self._event("ok" if self.state.online else "warn",
                        "Bağlantı kuruldu." if self.state.online
                        else "Uplink hazır ama internet doğrulanamadı.")
            self.state.persist()

            if profile.auto_reconnect:
                self._start_watchdog()

    # ----------------------------------------------------------------- stop
    def stop_session(self) -> None:
        with self._lock:
            self._event("info", "Oturum durduruluyor...")
            self.state.phase = "stopping"
            self._stop_watchdog()
            # Namespace silinmeden ÖNCE oturumun uygulamalarını sonlandır; aksi
            # halde uplinksiz ölü namespace'te öksüz (orphan) kalırlar.
            self._terminate_apps()
            try:
                self.vpn.disconnect()
            except Exception as e:  # noqa: BLE001
                log.warning("VPN kapatılırken: %s", e)
            try:
                self.relay.disable()
            except Exception as e:  # noqa: BLE001
                log.warning("relay kapatılırken: %s", e)
            self.engine.teardown(self._active_iface)
            self._event("ok", "Her şey eski haline döndü.")
            self.state = RuntimeState(phase="idle", namespace=self.s.namespace)
            self._active_iface = None
            self._active_profile = None
            self.state.persist()

    # ---------------------------------------------------------------- relay
    def enable_relay(self, profile: Profile | None = None,
                     extra_targets: list[str] | None = None) -> None:
        """Relay'i açar: izole alan, bu makinenin ulaştığı LAN ağlarına erişir.
        İnternet değişmez — tether/WiFi'de kalır. Tek düğme, kapsam/allowlist yok.
        extra_targets: host üzerinden geçirilecek ek IP/CIDR/alan adı listesi.
        """
        with self._lock:
            prof = profile or self._active_profile
            if not prof:
                raise EngineError("Etkin oturum yok.")
            if extra_targets is not None:
                prof.relay.extra_targets = list(extra_targets)
            try:
                routes = self.relay.enable(prof.relay)
            except Exception as e:
                # Kurulum gerçekten başarısız oldu (ör. bayat namespace);
                # körlemesine "açık" deme — durumu kapalı bırak, hatayı bildir.
                self.state.relay_active = False
                self.state.relay_targets = []
                self._event("error", f"Relay açılamadı: {e}")
                self.state.persist()
                raise

            self.state.relay_active = True
            self.state.relay_scope = "lan"
            self.state.relay_targets = list(routes)
            if routes:
                detail = f" → {', '.join(routes)}"
            else:
                detail = (" (uyarı: bu makine şu an LAN'a bağlı görünmüyor; "
                          "ethernet kablosu takılı mı?)")
            self._event("ok", f"Relay açıldı{detail}. İnternet tether'de.")
            self.state.persist()

    def disable_relay(self) -> None:
        with self._lock:
            self.relay.disable()
            self.state.relay_active = False
            self.state.relay_scope = ""
            self.state.relay_targets = []
            self._event("info", "Relay kapatıldı; tam izolasyon.")
            self.state.persist()

    # ---------------------------------------------------------------- VPN
    def connect_vpn(self, config_path: str | None = None) -> None:
        """İzole alan içinde OpenVPN başlatır (relay ile birlikte çalışabilir)."""
        with self._lock:
            if self.state.phase in ("idle", "stopping"):
                raise EngineError("Etkin oturum yok; önce başlatın.")
            prof = self._active_profile
            path = config_path or (prof.vpn_config if prof else "")
            if not path:
                raise EngineError("VPN yapılandırması (.ovpn) belirtilmedi.")
            if prof is not None and config_path:
                prof.vpn_config = config_path
            self._event("info", f"VPN bağlanıyor: {os.path.basename(path)}")
            try:
                self.vpn.connect(path)
            except Exception as e:
                self.state.vpn_active = False
                self._event("error", f"VPN bağlanamadı: {e}")
                self.state.persist()
                raise
            st = self.vpn.status()
            self.state.vpn_active = st["active"]
            self.state.vpn_name = os.path.basename(path)
            self.state.vpn_iface = st.get("iface", "")
            self.state.vpn_ip = st.get("ip", "")
            self._event("ok", f"VPN bağlandı ({self.state.vpn_iface} "
                              f"{self.state.vpn_ip}).")
            self.state.persist()

    def disconnect_vpn(self) -> None:
        with self._lock:
            self.vpn.disconnect()
            self.state.vpn_active = False
            self.state.vpn_name = ""
            self.state.vpn_iface = ""
            self.state.vpn_ip = ""
            self._event("info", "VPN kapatıldı.")
            self.state.persist()

    # ------------------------------------------------------------- devralma
    def adopt_if_running(self) -> bool:
        """Daemon açılışında mevcut sağlıklı oturumu DEVRAL (yeniden kurma).

        Namespace + uplink hâlâ duruyorsa (ör. daemon yeniden başlatıldı ama
        oturum teardown edilmedi), kaldığımız yerden devam ederiz: durumu
        önceki state'ten geri yükler, yaşayan uygulamaları tanır, watchdog'u
        yeniden başlatır. Böylece "kaldığın yerden devam" mümkün olur ve
        uygulamalar öksüz kalmaz.
        """
        if self.dry:
            return False
        with self._lock:
            if not system.namespace_exists(self.s.namespace):
                return False
            prev = RuntimeState.read() or {}
            uplink = prev.get("uplink")
            if not uplink or not system.interface_in_namespace(self.s.namespace, uplink):
                return False

            self._active_profile = self.s.profile(
                prev.get("profile") or self.s.active_profile)
            self._active_iface = uplink
            self.state = RuntimeState(
                phase="starting", profile=self._active_profile.name,
                namespace=self.s.namespace, uplink=uplink,
                reconnect_count=int(prev.get("reconnect_count", 0)),
            )
            # Önceki uygulama kayıtlarından hâlâ yaşayanları geri al.
            for a in prev.get("apps", []):
                if apps.pid_alive(a.get("pid", 0)):
                    self.state.apps.append(
                        AppProcess(command=a.get("command", "?"), pid=a["pid"]))
            self._event("info", f"Mevcut oturum devralındı (uplink={uplink}); "
                                f"kaldığın yerden devam.")
            self._refresh_network()
            self.state.relay_active = self.relay.is_active()
            if self.vpn.is_active():
                st = self.vpn.status()
                self.state.vpn_active = True
                self.state.vpn_iface = st.get("iface", "")
                self.state.vpn_ip = st.get("ip", "")
            self.state.phase = "online" if self.state.online else "degraded"
            self.state.persist()
            if self._active_profile.auto_reconnect:
                self._start_watchdog()
            return True

    # ---------------------------------------------------------- canlı işlemler
    def launch_app(self, program: str) -> int:
        """Etkin oturuma (namespace'e) yeni bir uygulama başlatır."""
        with self._lock:
            if self.state.phase in ("idle", "stopping"):
                raise EngineError("Etkin oturum yok; önce başlatın.")
            prof = self._active_profile
            assert prof
            pid = apps.launch(self.s, prof, program, dry_run=self.dry)
            self.state.apps.append(AppProcess(command=program, pid=pid))
            self._event("info", f"Uygulama başlatıldı: {program} (pid={pid})")
            self.state.persist()
            return pid

    def force_reconnect(self) -> None:
        """Elle yeniden bağlanma: arayüzü gerekiyorsa geri al, DHCP'yi yenile."""
        with self._lock:
            iface = self._active_iface
            if not iface:
                raise EngineError("Etkin oturum yok.")
            self.state.phase = "reconnecting"
            self._event("info", "Elle yeniden bağlanılıyor...")
            if not self.dry and not system.interface_in_namespace(self.s.namespace, iface):
                host_ifaces = {i.name for i in system.list_host_interfaces()}
                if iface in host_ifaces:
                    self._bring_uplink_into_ns(iface)
            self.engine.start_dhcp(iface)
            self.state.reconnect_count += 1
            self.state.last_reconnect = time.time()
            self._refresh_network()
            self.state.phase = "online" if self.state.online else "degraded"
            self._event("ok" if self.state.online else "warn",
                        "Yeniden bağlanıldı." if self.state.online
                        else "Bağlantı kurulamadı (uplink/veri yok).")
            self.state.persist()

    def _bring_uplink_into_ns(self, iface: str) -> None:
        """Uplink'i namespace'e alır — uplink tipine göre doğru yöntemle.

        WiFi'de generic 'ip link set netns' çalışmaz ("interface netns is
        immutable"); kablosuz PHY taşınmalı ve gerekiyorsa yeniden bağlanılmalı.
        Bu, hem ilk kurulum hem reconnect/watchdog için ortak yol.
        """
        prof = self._active_profile
        is_wifi = (prof is not None and prof.uplink_kind == "wifi") \
            or system._classify(iface) == "wifi"
        if is_wifi:
            self.engine.move_wifi_phy_in(iface)
            self.engine.move_uplink_in(iface)
            if prof and prof.wifi_ssid:
                self.engine.wifi_associate(iface, prof.wifi_ssid, prof.wifi_password)
                # DHCP'den ÖNCE association'ı bekle; yoksa taşıyıcı gelmeden
                # DHCP başarısız olur ("internet doğrulanamadı").
                if not self.engine.wait_wifi_associated(iface):
                    raise EngineError(
                        f"WiFi ağına bağlanılamadı: '{prof.wifi_ssid}'. "
                        f"SSID/parola yanlış olabilir, ağ menzil dışı olabilir "
                        f"ya da 5GHz/bant desteklenmiyor olabilir.")
            elif prof:
                raise EngineError(
                    "WiFi uplink seçildi ama SSID/parola girilmedi. Panelde "
                    "WiFi ağ adı ve parolasını girin.")
        else:
            self.engine.move_uplink_in(iface)

    def switch_uplink(self, new_iface: str, *, uplink_kind: str | None = None,
                      wifi_ssid: str | None = None,
                      wifi_password: str | None = None) -> None:
        """Canlı oturumda uplink'i değiştirir — namespace ve UYGULAMALAR korunur.

        Eski uplink host'a iade edilir, yenisi izole alana alınır, DHCP ile IP
        alınır. Uygulamalar yaşamaya devam eder; yalnızca dış bağlantı yeni
        uplink üzerinden yeniden kurulur (mevcut TCP oturumları sıfırlanabilir).
        """
        with self._lock:
            if self.state.phase in ("idle", "stopping"):
                raise EngineError("Etkin oturum yok; önce başlatın.")
            prof = self._active_profile
            old = self._active_iface
            if new_iface == old:
                raise EngineError("Yeni uplink mevcutla aynı.")
            self._event("info", f"Uplink değiştiriliyor: {old} → {new_iface}")

            # Profil bilgisini güncelle (yeni uplink tipi / WiFi kimlik bilgisi)
            if prof is not None:
                if uplink_kind:
                    prof.uplink_kind = uplink_kind
                if wifi_ssid is not None:
                    prof.wifi_ssid = wifi_ssid
                if wifi_password:
                    prof.wifi_password = wifi_password

            self.state.phase = "reconnecting"
            self.state.persist()

            # 1) Eski uplink'i host'a iade et (namespace'i SİLMEDEN)
            if old and not self.dry:
                try:
                    self.engine.stop_dhcp(old)
                    if system._classify(old) == "wifi":
                        self.engine.move_wifi_phy_out(old)
                    else:
                        self.engine.move_uplink_out(old)
                except Exception as e:  # noqa: BLE001
                    log.warning("eski uplink (%s) çıkarılırken: %s", old, e)

            # 2) Yeni uplink'i izole alana al  3) IP al
            self._active_iface = new_iface
            if prof is not None:
                prof.uplink = new_iface
            self.state.uplink = new_iface
            self._bring_uplink_into_ns(new_iface)
            self.engine.start_dhcp(new_iface)

            self.state.reconnect_count += 1
            self.state.last_reconnect = time.time()
            self._refresh_network()
            self.state.phase = "online" if self.state.online else "degraded"
            self._event("ok" if self.state.online else "warn",
                        f"Yeni uplink etkin: {new_iface}" if self.state.online
                        else f"{new_iface} hazır ama internet doğrulanamadı.")
            self.state.persist()

    # ------------------------------------------------------------- watchdog
    def _start_watchdog(self) -> None:
        if self._wd_thread and self._wd_thread.is_alive():
            return
        self._wd_stop.clear()
        self._wd_thread = threading.Thread(
            target=self._watchdog_loop, name="tether-watchdog", daemon=True)
        self._wd_thread.start()
        log.info("watchdog başlatıldı (her %ss)", self.s.watchdog_interval)

    def _stop_watchdog(self) -> None:
        self._wd_stop.set()
        if self._wd_thread:
            self._wd_thread.join(timeout=2)
            self._wd_thread = None

    def _watchdog_loop(self) -> None:
        while not self._wd_stop.wait(self.s.watchdog_interval):
            try:
                # Her adımda GÜNCEL uplink'i oku — canlı uplink değişiminde
                # (switch_uplink) watchdog otomatik olarak yeni uplink'i izler.
                iface = self._active_iface
                if iface:
                    self._tick(iface)
            except Exception as e:  # noqa: BLE001
                log.warning("watchdog tick hatası: %s", e)

    def _tick(self, iface: str) -> None:
        """Tek bir izleme adımı: uplink yerinde mi, internet var mı?"""
        with self._lock:
            if self.state.phase in ("idle", "stopping"):
                return
            present = self.dry or system.interface_in_namespace(self.s.namespace, iface)

            if not present:
                # Arayüz namespace'ten düştü. Host'ta yeniden belirdi mi?
                self.state.phase = "reconnecting"
                self.state.uplink_present = False
                host_ifaces = {i.name for i in system.list_host_interfaces()}
                if iface in host_ifaces:
                    self._event("warn", f"{iface} tekrar belirdi, namespace'e alınıyor...")
                    self._bring_uplink_into_ns(iface)
                    self.engine.start_dhcp(iface)
                    self.state.reconnect_count += 1
                    self.state.last_reconnect = time.time()
                    self._refresh_network()
                    if self.state.online:
                        self._event("ok", "Yeniden bağlanıldı.")
                else:
                    self._event("warn", f"{iface} bekleniyor (uplink yok).")
                self.state.persist()
                return

            # Arayüz yerinde — internet sağlığını kontrol et
            self._refresh_network(light=True)
            if not self.state.online:
                if self.state.phase != "degraded":
                    self._event("warn", "İnternet kesildi; DHCP yenileniyor...")
                self.engine.start_dhcp(iface)
                self._refresh_network()
                self.state.phase = "online" if self.state.online else "degraded"
            else:
                self.state.phase = "online"
            # Relay durumunu gerçeğe göre uzlaştır: "açık" diyorsak gerçekten
            # kanal duruyor mu? (Beklenmedik düşüşte arayüz doğruyu göstersin.)
            if self.state.relay_active and not self.dry and not self.relay.is_active():
                self.state.relay_active = False
                self.state.relay_scope = ""
                self.state.relay_targets = []
                self._event("warn", "Relay kanalı düştü; tam izolasyona dönüldü.")

            # Uygulama canlılığı
            self._refresh_apps()
            self.state.persist()

    # ------------------------------------------------------------- yardımcı
    def _refresh_network(self, *, light: bool = False) -> None:
        iface = self._active_iface
        if not iface:
            return
        self.state.uplink_present = self.dry or system.interface_in_namespace(
            self.s.namespace, iface)
        ip, gw = self.engine.uplink_ip(iface)
        self.state.ip_address, self.state.gateway = ip, gw
        self.state.online = self.engine.connectivity()
        self.state.last_check = time.time()
        if not light and self.state.online:
            self.state.public_ip = self.engine.public_ip()

    def _refresh_apps(self) -> None:
        if self.dry:
            return
        for a in self.state.apps:
            a.running = apps.pid_alive(a.pid)

    def _terminate_apps(self) -> None:
        """Oturumun başlattığı uygulamaları (süreç grubuyla) sonlandırır.

        Uygulamalar start_new_session=True ile başlatıldığından PID = grup
        lideridir; grubu sinyallemek tarayıcının alt süreçlerini de kapatır.
        Böylece 'Durdur' orphan bırakmaz.
        """
        if self.dry:
            return
        for a in self.state.apps:
            if not apps.pid_alive(a.pid):
                continue
            try:
                os.killpg(os.getpgid(a.pid), signal.SIGTERM)
            except OSError:
                try:
                    os.kill(a.pid, signal.SIGTERM)
                except OSError:
                    pass

    def _event(self, level: str, msg: str) -> None:
        log.info("[%s] %s", level, msg)
        self.state.log_event(level, msg)

    # --------------------------------------------------------------- durum
    def snapshot(self) -> dict:
        with self._lock:
            return self.state.to_dict()
