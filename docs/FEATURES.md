# Özellikler ve Nasıl Çalışır

Her özelliğin **ne yaptığı**, **nasıl yapıldığı** ve **ilgili kod** birlikte.

## 1. Yapısal izolasyon
- **Ne:** İzole uygulamalar yalnızca seçilen uplink'ten çıkar; host'un kablolu
  bağlantısını kullanamaz.
- **Nasıl:** Uplink arayüzü `ip link set <iface> netns tether_zone` ile
  namespace'e fiziksel taşınır. Ayrı yönlendirme tablosu + DNS.
- **Kod:** `engine.py: move_uplink_in`, `ensure_namespace`, `_write_resolv`.

## 2. Otomatik yeniden bağlanma (watchdog)
- **Ne:** Telefon çıkıp takıldığında bağlantı uygulamaları öldürmeden geri gelir.
- **Nasıl:** Arka plan thread'i periyodik olarak uplink'in namespace içinde olup
  olmadığını ve internet sağlığını kontrol eder; gerekirse arayüzü geri taşır,
  DHCP'yi yeniler. Uygulamalar namespace'e bağlı olduğu için yaşar.
- **Kod:** `manager.py: _watchdog_loop`, `_tick`, `_refresh_network`.

## 3. Denetimli relay (kurum ağı erişimi) — tek mod
- **Ne:** Gerektiğinde izole uygulamalara, bu makinenin ulaştığı LAN/kurum
  ağlarına erişim. İnternet değişmez (tether'de kalır).
- **Nasıl:** Geçici bir veth çifti; host'un varsayılan-dışı tüm rotaları izole
  alana aynalanır + nftables/iptables MASQUERADE. Opsiyonel "ek hedefler"
  (IP/CIDR/alan adı) kurum çıkışından geçirilir. Kapatınca veth + kurallar silinir.
- **Kod:** `relay.py: enable / disable / _host_lan_routes / _resolve_extra`.

## 3b. İzole alanda OpenVPN istemcisi
- **Ne:** İzole alanın kendi uplink'i (tether) üzerinden uzak ağa (ör. garageliman)
  şifreli bağlantı; namespace içinde `tun`. Relay ile aynı anda çalışır.
- **Nasıl:** `ip netns exec ... openvpn --disable-dco --data-ciphers ...`
  (namespace'te DCO takılması + eski sunucu cipher uyumu için).
- **Kod:** `vpn.py: connect / wait_connected / disconnect`.

## 4. Kalıcı profiller ve oturumlar
- **Ne:** Tarayıcı oturumları silinmez; birden çok kayıtlı yapılandırma.
- **Nasıl:** Her profil için `~/.local/share/tether-isolator/profiles/<ad>/<app>`
  kalıcı veri dizini; `--user-data-dir` / `--profile` ile bağlanır.
- **Kod:** `config.py: Profile.profile_data_dir`, `apps.py: _APP_FLAGS`.

## 5. USB tether + WiFi uplink
- **Ne:** İzole bağlantı tipi seçilebilir.
- **Nasıl:** USB/ethernet doğrudan taşınır. WiFi için PHY namespace'e taşınır
  (`iw phy ... set netns`) ve içeride `wpa_supplicant` çalışır.
- **Kod:** `engine.py: move_uplink_in`, `move_wifi_phy_in`, `wifi_associate`.

## 6. Apple-esinli web paneli
- **Ne:** Canlı durum, dış IP, olay akışı, tek tık kontroller.
- **Nasıl:** stdlib HTTP daemon + vanilla JS; 2 sn'de bir `/api/status` polling;
  sistem yazı tipi, blur kartlar, segment/anahtar bileşenleri, koyu mod.
- **Kod:** `server.py`, `webui/`.

## 7. Scriptlenebilir CLI + etkileşimli mod
- **Ne:** Otomasyon için alt komutlar; orijinal script gibi soru-cevap akışı.
- **Kod:** `cli.py`.

## 8. Güvenli yetki ayrımı
- **Ne:** Daemon root, uygulamalar kullanıcı; panel yalnızca localhost.
- **Kod:** `apps.py: launch (runuser)`, `server.py (127.0.0.1 bind)`.

## 9. dry-run
- **Ne:** Donanım/yetki olmadan tüm mantığı test et.
- **Kod:** Her modülde `dry_run` parametresi; `system.run(dry_run=...)`.

---

## Yol haritası (sonraki fazlar)
Bkz. [`TASK.md`](TASK.md). Öne çıkanlar: domain-bazlı relay beyaz listesi,
trafik/veri sayacı, kill-switch (uplink yokken relay'i de kes), profil
içe/dışa aktarma, sistem tepsisi göstergesi, IPv6, otomatik testler.
