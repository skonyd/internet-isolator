# Görev Planı (TASK.md)

Durum işaretleri: ✅ tamam · 🔄 sürüyor · ⏳ planlandı

## Faz 1 — Çekirdek + Dayanıklılık + GUI  (MVP)
- ✅ Bash mantığının Python'a modüler taşınması (orijinal `ip netns` korunarak)
- ✅ Model B izolasyon motoru (`engine.py`)
- ✅ Profil/ayar yönetimi, kalıcı dizinler (`config.py`)
- ✅ Çalışma anı durumu (`state.py`)
- ✅ Orkestratör + **watchdog / otomatik yeniden bağlanma** (`manager.py`)
- ✅ İzole alanda uygulama başlatma, GUI ortamı, kalıcı profiller (`apps.py`)
- ✅ Yerel HTTP API + statik sunum (`server.py`)
- ✅ Apple-esinli web paneli (`webui/`)
- ✅ Scriptlenebilir CLI + etkileşimli mod (`cli.py`)
- ✅ `--dry-run` her katmanda
- ✅ systemd birimi, başlatıcı, dokümanlar
- ✅ Çift tıklanan masaüstü uygulaması (`.desktop` + pkexec başlatıcı +
  `install.sh` + arayüzde "Çıkış" düğmesi / `/api/quit`)
- ✅ Dry-run uçtan uca doğrulama (API + statik + güvenlik)

## Faz 2 — Relay & Profiller (kısmen Faz 1'de yapıldı)
- ✅ veth relay yan-kanalı (`relay.py`)
- ✅ Panelden relay aç/kapa
- ✅ **Canlı uygulama başlatma** (oturum açıkken çipe tıkla = şimdi çalıştır) `/api/launch`
- ✅ **Manuel yeniden bağlanma** düğmesi `/api/reconnect`
- ✅ **Mevcut sistem profilini kullanma** seçeneği (girişler korunur) + host'ta açıksa
  izolasyon delinmesin diye reddetme güvenliği (`apps.py: _running_on_host`)
- ✅ Root daemon'un oluşturduğu profil dizinlerini kullanıcıya devretme
  (`system.chown_to_user`) — tarayıcıların açılmama hatası giderildi
- ✅ **Relay doğrulaması**: `relay.enable` kurulumdan sonra ns-ucu veth'i
  (`tisor-ns`) izole alanın İÇİNDE arar; yoksa net hata verir. `is_active`
  hem host hem ns ucunu denetler. `manager.enable_relay` başarısızlıkta
  `relay_active`'i False bırakır + hatayı arayüze iletir. Watchdog her
  adımda relay durumunu gerçeğe göre uzlaştırır (kanal düşerse arayüz
  yansıtır). → "açık görünüp çalışmama" durumu giderildi.
- ✅ **Relay full-mod internet düzeltmesi**: full mod artık MASQUERADE'e ek
  olarak FORWARD ACCEPT kuralları da ekliyor (host FORWARD politikası DROP ise
  iletilen paketlerin düşmesini önler; nft+iptables). Ayrıca **güvenli geri-alma**:
  full mod gerçekten internet vermezse otomatik kapatılıp tether interneti geri
  yükleniyor ve net hata gösteriliyor → "relay açınca internet kopuyor ve kalıyor"
  durumu giderildi. iptables kuralları `disable`'da da temizleniyor.
- ✅ **Relay TEK MODA sadeleştirildi (2026-07-01)** — `host-only`/`lan`/`full`
  kapsam seçici, allowlist ve `verify_hosts` doğrulaması KALDIRILDI (kafa
  karıştırıyordu). Artık tek aç/kapa düğmesi: relay açılınca **host'un ulaştığı
  tüm ağlar (varsayılan rota HARİÇ)** izole alana aynalanır ve host üzerinden
  NAT'lanır → izole uygulamalar bu makinenin ethernet/tünel üzerinden eriştiği
  her yere (kurum sunucuları vb.) ulaşır. **İnternet değişmez, tether'de kalır.**
  Doğrulama pingi yerine gerçek kanıt: ns-ucu veth'in izole alanda varlığı.
  Panelde aynalanan ağlar listelenir. `RelayPolicy` eski alanları (scope,
  lan_targets, verify_hosts) yalnızca eski profil uyumluluğu için tutuluyor.
  → "relay açık görünüp istediğim ağlara erişemiyorum" ve "doğrulama IP anlamsız"
  şikâyetleri giderildi. Gerçek donanımda doğrulandı: izole alandan
  `10.0.15.127` ping/SSH:22/HTTPS:443 ve kurum DNS erişildi.
- ✅ **Relay "Ek hedefler" (`extra_targets`)**: kurum çıkışından erişilen ama
  tether'den engelli DIŞ adresler (ör. `mail.havelsan.com.tr`) için. Bunlar
  LAN rotalarında görünmez çünkü host'un **varsayılan (kurum ethernet)**
  rotasından çıkarlar. Panelde "Ek hedefler" alanına IP/CIDR/alan adı yazılınca
  (alan adları açılışta A kaydına çözülür) izole alanda host'a yönlendirilir,
  host kendi kurum çıkışından NAT'lar. Böylece internet tether'de kalırken
  seçili kurum-only siteler kurum ağından geçer. Profile kaydedilir.
- ✅ **İzole alan içinde OpenVPN istemcisi** (`vpn.py`, `/api/vpn`, profil
  `vpn_config`): `.ovpn` izole namespace İÇİNDE çalışır — taşıyıcı olarak izole
  alanın uplink'i (tether), namespace içinde tun. Split-tunnel'da internet
  tether'de kalır, uzak ağa (ör. garageliman) VPN üzerinden erişilir. **Relay ile
  aynı anda açık olabilir** (farklı arayüz/rota) → izole terminator hem VPN ağına
  hem relay LAN'ına erişir. Bağlanma "Initialization Sequence Completed" ile
  doğrulanır; başarısızsa OpenVPN log kuyruğu hatayla döner. stop_session VPN'i
  kapatır; profilde tanımlıysa başlatmada otomatik bağlanır. Panelde VPN kutusu.
- ✅ **VPN namespace düzeltmeleri (2026-07-01)** — gerçek `.ovpn` (garageliman)
  ile iki kök neden bulunup giderildi:
  1. **DCO takılması**: OpenVPN 2.6 Data Channel Offload kernel modülü ağ
     namespace'i içinde asılı kalıyordu (versiyon satırından sonra ilerlemiyordu).
     Çözüm: `--disable-dco` (destekliyse) → userspace tun yolu.
  2. **Cipher uyuşmazlığı**: eski sunucu `AES-128-CBC` dayatıyor, OpenVPN 2.6
     varsayılanı sadece GCM kabul edip tüneli reddediyordu ("failed to negotiate
     cipher... Failed to open tun"). Çözüm: `--data-ciphers` listesine modern GCM/
     CHACHA yanında legacy `AES-256-CBC:AES-128-CBC` eklendi.
  Gerçek donanımda doğrulandı: izole alanda `tun0` (10.67.11.x) açıldı,
  garageliman VPN ağı erişildi. VPN taşıyıcısı WiFi/tether'dir (PC ethernet'i değil).
- ⏳ **A↔B köprüsü** (VPN ağı ↔ relay LAN, laptop router) — ns forwarding + iki
  yönlü NAT; uzak uç dönüş rotalarına bağlı, gerçek ortamda test edilecek.
- ⏳ Domain-bazlı relay beyaz listesi (`scope=domains`) — nftables set + dnsmasq
- ⏳ Panelden profil oluşturma/düzenleme arayüzü (API hazır, UI formu eksik)
- ⏳ Profil içe/dışa aktarma

## Faz 3 — WiFi & İleri Özellikler
- 🔄 WiFi uplink (PHY taşıma + wpa_supplicant) — kod var, **gerçek donanım testi**
  - ✅ **Gereksinim:** WiFi için `iw` kurulu olmalı (`sudo apt install iw`).
    Eksikse artık eyleme dönük net hata veriliyor.
  - ✅ Reconnect/watchdog artık **WiFi-farkında** (`_bring_uplink_into_ns`):
    kablosuzda generic `ip link set netns` yerine PHY taşıma + yeniden
    bağlanma kullanılıyor → "interface netns is immutable" hatası giderildi.
  - ✅ `move_wifi_phy_in` dry-run'da donanım yoklamıyor (önizleme tutarlılığı).
  - ✅ **PHY taşıma ölü-PID hatası giderildi**: eski kod `sh -c 'echo $$'` ile
    geçici PID alıp `iw phy set netns <pid>` çağırıyordu ama o süreç ölmüş
    oluyordu (`iw ... 253`). Artık `iw phy <phy> set netns name <ns>` (modern
    iw, isimle namespace) kullanılıyor; `_ns_pid` kaldırıldı.
- ✅ **Panelden WiFi parola girişi**: WiFi uplink seçilince SSID + parola
  alanları çıkar; `/api/start` ve `/api/switch-uplink` bunları alır, profile
  kaydeder (parola maskeli gösterilir). → "WiFi bağlanmıyor" kök nedeni (boş
  SSID/parola) giderildi.
- ✅ **Süreklilik — canlı uplink değişimi** (`manager.switch_uplink`,
  `/api/switch-uplink`): oturum açıkken farklı uplink'e geçince namespace ve
  **uygulamalar kapanmaz**; eski uplink host'a iade edilir (WiFi PHY dahil),
  yenisi izole alana alınıp DHCP ile IP alır, internet yeni uplink üzerinden
  devam eder. Watchdog güncel uplink'i dinamik izler. Panelde "↪ Bu uplink'e
  geç" düğmesi.
- ✅ **DNS (systemd-resolved + netns) düzeltmesi**: host'ta `/etc/resolv.conf`
  çoğunlukla `127.0.0.53` stub'ına symlink'tir; bu adres izole ns'te erişilemez
  ve `ip netns exec` otomatik bind'i symlink yüzünden tutmaz → uygulamalarda
  **isim çözümleme kopuktu** (curl/ping-by-name; tarayıcılar DoH ile maskeliyordu;
  uplink değişince "internet gelmiyor" olarak görünüyordu). Artık uygulama
  başlatılırken (root, özel mount ns) kendi resolv.conf'umuz symlink'in ÇÖZÜLMÜŞ
  hedefine bind ediliyor → uygulamalar `8.8.8.8/1.1.1.1` görür, host etkilenmez.
- ✅ **WiFi association bekleme**: `wpa_supplicant -B` asenkron; eskiden hemen
  DHCP çalışıyordu → taşıyıcı gelmeden başarısız oluyordu. Artık DHCP'den ÖNCE
  `wait_wifi_associated` ile bağlantı beklenir; başarısızsa (yanlış parola/menzil)
  net hata verilir. `wpa_supplicant` `-D nl80211` ile başlatılır + eski örnek
  temizlenir.
- ✅ **Başlatma dayanıklılığı**: `start_session` yarıda hata alırsa (ör. WiFi
  association) namespace temizlenir ve durum `idle`'a döner (eskiden `starting`'te
  takılıp tekrar başlatmayı engelliyordu). Tek bir uygulama açılmazsa oturum ölmez.
- ✅ **Oturum devralma (resume) — "kaldığın yerden devam"** (`adopt_if_running`):
  Daemon açılışında namespace + uplink hâlâ ayaktaysa oturumu **yeniden kurmadan
  devralır** (state'ten geri yükler, yaşayan uygulamaları tanır, watchdog'u
  başlatır). **Çıkış artık teardown yapmaz** — panel kapansa da izole oturum +
  uygulamalar yaşar; tam durdurma yalnızca "Durdur" ile. → Daemon yeniden
  başlatılınca uygulamalar öksüz kalmaz, internet kopmaz. "önceden başlatılmış
  uygulama yeniden internete erişemiyor" ve orphan birikmesi giderildi.
- ✅ **Orphan'sız durdurma**: `stop_session` artık namespace'i silmeden ÖNCE
  oturumun uygulamalarını (süreç grubuyla) sonlandırır → 'Durdur' geride uplinksiz
  öksüz süreç bırakmaz. (Orphan'ın son kaynağı da kapatıldı.)
- ⏳ Panelden WiFi SSID **tarama** (şimdilik SSID elle yazılıyor)

## Faz 4 — Kalite & Dağıtım  (kısmen başlandı)
- ✅ **Birim/entegrasyon testleri** (`tests/`, stdlib `unittest`, bağımlılıksız):
  60 test — config serileştirme, state/olay günlüğü, apps komut/bayrak + resolv
  sarmalı, engine/relay komut kurulumu (tek-mod relay + ek hedef çözümleme),
  VPN komut kurulumu (`--disable-dco` + `--data-ciphers`), manager yaşam döngüsü +
  canlı uplink değişimi + relay/VPN + başlatma hata temizliği + oturum devralma.
  Çalıştır: `python3 -m unittest discover -s tests`
- ✅ **`tetherctl doctor`**: ortam sağlık kontrolü — eksik araçları (ip, dhcpcd,
  iw, wpa_supplicant, nft/iptables) ve bilinen tuzakları (resolv.conf symlink)
  raporlar.
- ⏳ Kill-switch: uplink yokken relay otomatik kapansın (sızıntı önleme)
- ⏳ Trafik/veri kullanım sayacı (ns içi `nstat`/`/proc/net/dev`)
- ⏳ Sistem tepsisi göstergesi (AppIndicator)
- ⏳ IPv6 desteği ve sızıntı testi

## Faz 4 — Kalite & Dağıtım (devam)
- ✅ Birim/entegrasyon testleri (yukarıda) + `doctor` sağlık kontrolü
- ⏳ Gerçek donanımla entegrasyon testi senaryoları
- ⏳ `.deb` / `pipx` paketleme
- ⏳ Parolaların keyring'de saklanması (şu an WiFi parolası düz metin)
- ⏳ Çoklu eşzamanlı namespace (birden çok izole oturum)

## Bilinen sınırlamalar / notlar
- WiFi PHY taşıma ve relay full-mod **gerçek donanımda** doğrulanmalı; bu
  ortamda yalnızca dry-run ile mantık doğrulandı.
- WiFi parolası şu an `config.json`'da düz metin (Faz 4'te keyring).
- Tek namespace varsayımı (`tether_zone`); çoklu oturum Faz 4.
