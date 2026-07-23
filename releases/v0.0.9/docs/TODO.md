# Yapılacaklar & Geliştirme Fikirleri (TODO.md)

Bu dosya, `tether_isolator` için mantıklı, kullanışlı ve avantajlı geliştirmeleri
önceliklendirilmiş biçimde listeler. Tamamlananlar için bkz. [`TASK.md`](TASK.md).

**Öncelik:** 🔴 yüksek (net değer/az efor) · 🟡 orta · 🟢 nice-to-have
**Efor:** S (küçük) · M (orta) · L (büyük)

---

## A. Güvenlik & Sızıntı Önleme (en yüksek değer)

- 🔴 **Kill-switch (sızıntı önleme)** — S/M
  Uplink düşerse veya namespace bozulursa izole uygulamaların trafiği
  "varsayılan izin"e düşmesin. `default` rota yokken paketler `REJECT` edilsin
  (ns içinde blackhole/`unreachable` rota + host tarafında güvenlik). VPN/relay
  düşerse otomatik izole moda dönüş.
  *Neden:* İzolasyonun asıl güvencesi; kopma anında veri kaçağını engeller.
  *Kod:* `manager._tick`, `engine`.

- 🔴 **VPN split-tunnel doğrulaması + leak testi** — S
  VPN açıkken izole alanın gerçek çıkış IP'sini kontrol et (`curl ifconfig.co`);
  beklenen VPN/uplink IP'si değilse uyar. DNS leak testi (sorgu hangi resolvera
  gidiyor).
  *Neden:* "VPN açık ama trafik yanlış yerden çıkıyor" sessiz hatasını yakalar.

- 🟡 **WiFi/VPN parolalarını keyring'de sakla** — M
  Şu an `config.json`'da düz metin. `secretstorage`/`libsecret` (opsiyonel) veya
  en azından dosya izinlerini `600` + ayrı şifreli depo.
  *Neden:* Kurumsal ortamda düz metin parola risk.

- 🟡 **IPv6 sızıntı kapatma** — S
  Namespace'te IPv6 kapalı değilse, IPv4 izole edilse bile IPv6 host üzerinden
  sızabilir. ns içinde `disable_ipv6` veya bilinçli IPv6 uplink yönetimi.

- 🟢 **Firewall denetim modu** — M
  Relay açıkken izole alandan host'un HANGI ağlarına erişildiğini logla; beklenen
  dışı hedefe erişim denemesinde uyar (görünürlük).

---

## B. Ağ / Köprüleme Yetenekleri

- 🔴 **A↔B köprüsü** (garageliman VPN ağı ↔ kurum LAN'ı) — M/L
  Laptop router olarak iki ağı birbirine bağlasın: Spark'taki (garageliman) LLM
  agent → kurum sunucuların. İki seçenek:
  - **Yol A (port-forward, önerilen):** ns içinde `socat`/nftables DNAT ile
    seçili kurum servisini (ör. `10.0.15.127:8000`) VPN arayüzünde yayınla.
    garageliman sunucu yapılandırması gerekmez, basit ve kontrollü.
  - **Yol B (tam yönlendirme + çift yönlü NAT):** garageliman OpenVPN sunucusunda
    `iroute`/`ccd` dönüş rotaları gerekir.
  *Kod:* yeni `bridge.py`; `docs/STATUS.md`'de plan mevcut.

- 🟡 **Çoklu VPN / VPN seçici** — M
  Birden çok `.ovpn` profili; panelden hangisinin bağlanacağını seç, aynı anda
  birden fazla tün (garageliman + başka).

- 🟡 **WireGuard desteği** — M
  OpenVPN yanında WireGuard (`wg`) — daha hızlı, namespace'te daha az sorun,
  DCO/cipher dertleri yok.

- 🟡 **Relay hedeflerinde DNS-farkında allowlist (domains)** — L
  Alan-adı bazlı geçiş (nftables set + küçük DNS gözlemci). Şu an ek hedefler
  açılışta bir kez çözülüyor; IP değişen siteler için canlı güncelleme.

- 🟢 **Uplink bonding/failover** — L
  Birden çok uplink (USB + WiFi) aynı anda; biri düşerse diğerine sorunsuz geçiş
  (şu an manuel `switch_uplink` var).

---

## C. Gözlemlenebilirlik & Teşhis

- 🔴 **Trafik/veri kullanım sayacı** — S
  İzole alanda `/proc/net/dev` veya `nstat` ile uplink başına indirilen/yüklenen
  bayt; panelde canlı grafik. Telefon tether kotası için çok değerli.

- 🔴 **Panelde canlı log/olay akışı iyileştirme + indirme** — S
  OpenVPN log kuyruğu, watchdog olayları panelde filtrelenebilir; "logları indir"
  ile tanı paketi (`doctor` çıktısı + son olaylar + rotalar).

- 🟡 **Bağlantı sağlık göstergesi (uçtan uca)** — S
  Sadece "online" değil: gateway ping, DNS çözümü, dış IP, VPN durumu, relay
  hedeflerine erişilebilirlik — tek bakışta yeşil/sarı/kırmızı.

- 🟡 **`doctor` genişletme** — S
  DCO/cipher uyumu, IPv6 durumu, resolv.conf tuzağı, nft vs iptables, kernel
  `tun` modülü, forwarding sysctl — otomatik ön-uçuş kontrolü ve öneriler.

---

## D. Kullanılabilirlik & Arayüz

- 🔴 **Sistem tepsisi göstergesi (AppIndicator)** — M
  Panel kapalıyken bile durum (izole/relay/VPN), hızlı başlat/durdur, uplink
  değiştir. Günlük kullanımı ciddi kolaylaştırır.

- 🔴 **WiFi SSID tarama** — S
  Şu an SSID elle yazılıyor. `iw dev scan` ile menü; sinyal gücü + kayıtlı ağlar.

- 🟡 **Panelden profil oluştur/düzenle/sil** — M
  API hazır, UI formu eksik. İçe/dışa aktarma (JSON) ile makineler arası taşıma.

- 🟡 **"Hızlı senaryolar" / şablonlar** — S
  Tek tıkla hazır kurulumlar: "Telefon tether + kurum relay", "garageliman VPN +
  relay köprü", "sadece izole tarama". Kullanıcının tekrarlayan işini kaydeder.

- 🟢 **Sağ-tık terminal eklentisini genişlet** — S
  "Host interneti terminali" yanında "İzole alan terminali", "VPN durumunu göster",
  "kurum sunucusuna SSH" kısayolları.

- 🟢 **Karanlık/aydınlık tema + i18n** — S
  Arayüz şu an TR; EN çevirisi ve tema anahtarı.

---

## E. Dayanıklılık & Doğruluk

- 🟡 **VPN watchdog / otomatik yeniden bağlanma** — S
  Tün düşerse (uplink değişimi, uyku sonrası) otomatik yeniden bağlan; relay gibi
  reconcile edilsin.

- 🟡 **Uyku/uyanma (suspend/resume) dayanıklılığı** — M
  Laptop uykudan dönünce uplink/DHCP/VPN/relay durumunu otomatik onar.

- 🟡 **Çoklu eşzamanlı namespace** — L
  Birden çok izole oturum (`tether_zone2` ...); farklı uplink+profil kombinasyonu
  aynı anda. Tek-namespace varsayımını kaldırır.

- 🟢 **Durum tutarlılık testleri (property-based)** — M
  Rastgele başlat/durdur/switch/relay/vpn dizileriyle state makinesinin hiç
  "yarım" kalmadığını doğrula.

---

## F. Paketleme & Dağıtım

- 🟡 **`.deb` paketi + `pipx`** — M
  `install.sh` yerine düzgün paket; systemd birimi, polkit kuralı, bağımlılık
  bildirimi. Kurumsal dağıtım için gerekli.

- 🟡 **Polkit kuralı inceltme** — S
  `pkexec` ile tüm root yerine, yalnızca gereken ağ işlemlerine izin veren dar
  polkit politikası (en az ayrıcalık).

- 🟢 **Otomatik güncelleme / sürüm kontrolü** — S
  Panelde sürüm ve "güncelleme var" bildirimi.

---

## G. Test & Kalite (gerçek donanım)

- 🔴 **Gerçek donanım entegrasyon senaryoları (belgeli)** — M
  Elle koşulacak kontrol listesi: USB tether, WiFi, relay, VPN, uplink switch,
  resume, kill-switch. Her biri için beklenen çıktı + doğrulama komutu.

- 🟡 **CI (GitHub Actions vb.)** — S
  `unittest` + `py_compile` + lint (ruff) her push'ta. (Ağ testleri mock'lu.)

- 🟢 **Kod içi tip denetimi (mypy) + ruff** — S
  Stdlib-only kalırken statik analiz; regresyon önleme.

---

## Öneri: sıradaki 3 adım
1. 🔴 **Kill-switch** (A) — izolasyonun güvenlik boşluğunu kapatır.
2. 🔴 **Trafik sayacı** (C) — telefon tether kotası için günlük fayda.
3. 🔴 **A↔B köprüsü Yol A** (B) — senin garageliman↔kurum senaryonu tamamlar.
