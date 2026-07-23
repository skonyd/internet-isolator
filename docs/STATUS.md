# Tether Isolator — Durum

**Sürüm:** 0.1.0 (önceki: 0.0.9) · **Son güncelleme:** 2026-07-02

## Sprint 1 ✅ — Güvenlik & Doğruluk temeli (tamamlandı)
- [x] G-1 API token kimlik doğrulaması (Bearer token, 0600 dosya)
- [x] G-2 Host başlığı + Content-Type doğrulaması (CSRF/DNS-rebinding koruması)
- [x] G-6 Kill-switch (fail-closed blackhole rota)
- [x] G-3 config.json 0600 izni
- [x] G-4 Hata mesajlarında bilgi sızıntısı önleme
- [x] G-5 İstek gövdesi boyut sınırı (1 MB)
- [x] G-7 IPv6 sızıntı kapatma (namespace içinde disable_ipv6)
- [x] G-8 systemd sıkılaştırmaları (ProtectSystem, PrivateTmp, vb.)
- [x] G-9 CSP başlığı (Content-Security-Policy)
- [x] B-1 Sürüm tek kaynak (`__init__.__version__`)
- [x] B-2 `__init__` docstring düzeltmesi (supervisor → manager)
- [x] B-3 `cmd_start` sonsuz döngü → signal.pause / watchdog join
- [x] B-4 ip_forward eski haline döndürme
- [x] B-5 Path-traversal koruması (realpath + commonpath)
- [x] B-6 SUDO_USER doğrulaması (pwd.getpwnam)
- [x] B-7 pgrep -x → pgrep -f (15 karakter sınırı)
- [x] B-8 http_port/watchdog_interval doğrulaması

## Sprint 2 ✅ — Performans & Kullanılabilirlik (tamamlandı)
- [x] P-1 Kilit granülerliği (snapshot immutable kopya, trafik sayacı kilit dışı)
- [x] P-2 Watchdog prob sıklığı (light mod: online iken ping atılmaz)
- [x] P-3 discover_installed + interface önbelleği (TTL)
- [x] H-1 Trafik sayacı (/proc/net/dev üzerinden)
- [x] H-7 Uçtan uca sağlık göstergesi (gateway/DNS/external/VPN/relay)

## Sprint 3 ✅ — Dayanıklılık & Kapsam (tamamlandı)
- [x] H-3 VPN watchdog + vpn_required kill-switch
- [x] H-4 Suspend/resume dayanıklılığı (watchdog agresif reconcile)
- [x] M-3/M-4 CI + server testleri (75 test, hepsi geçiyor)
- [x] M-1 `_active_profile` → property (kapsülleme)
- [x] M-2 chown mantığı tek yardımcıya indir (system.chown_to_user)
- [x] M-5 `_spawn` belgeli ve testli (string tabanlı ama güvenli)

## Sprint UX ✅ — Ürün cilası (tamamlandı)
- [x] U-1 İlk çalıştırma sihirbazı (5 adımlı onboarding)
- [x] U-2 Hazır senaryolar/tek-tık şablonlar (profil seçimi)
- [x] U-3 İzolasyon kanıtı güven rozeti (Trust Badge)
- [x] U-4 Akıllı uplink otomatik algılama (dostça adlandırma)
- [x] U-5 Masaüstü bildirimleri (UI toast bildirimleri)
- [x] U-6 Görsel veri kullanım paneli (trafik sayacı)
- [x] U-7 Bağlantı hız/kalite testi
- [x] U-8 Oturum zaman çizelgesi/olay filtresi
- [x] U-9 Duraklat/geçici izin (switch-uplink ile)
- [x] U-10 Hatada rehberli kurtarma (toast + hata mesajları)
- [x] U-11 Boş/yükleniyor durumları, toast, skeleton
- [x] U-12 Yedekle/geri yükle (config.json JSON)
- [x] U-13 Komut paleti (Ctrl+K)
- [x] U-14 Zengin uygulama kataloğu + simgeler + dostça arayüz adları
- [x] U-15 Tema (karanlık/aydınlık/auto) + elle geçiş

## Kalan / Gelecek
- [ ] H-9 A↔B köprüsü (garageliman VPN ↔ kurum LAN) — M/L
- [ ] H-10 WireGuard desteği — M
- [ ] H-6 Panelden profil oluştur/düzenle/sil (UI) — M
- [ ] H-11 i18n (EN) — S
- [ ] H-12 Çoklu eşzamanlı namespace — L
- [ ] U-12 Yedekle-geri yükle & profil paylaşımı (API eklenecek) — S
- [ ] U-2 Hazır senaryolar / şablon kartları (config'de built-in) — S
