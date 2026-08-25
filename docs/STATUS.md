# Tether Isolator — Durum

**Sürüm:** 1.2.0 (önceki yayınlanmış etiket: v1.1.0) · **Son güncelleme:** 2026-08-25

> Not: v1.1.0 etiketindeki kodda `__version__` hâlâ `0.1.0` yazıyordu — etiket ile
> kod sürümü uyumsuzdu. Bu sürümde ikisi **hizalandı**: etiket `v1.2.0`,
> `__version__` `1.2.0`. Yayınlanmış v1.1.0 etiketine dokunulmadı.

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

## Sprint Veri Tasarrufu (Faz 1+2) ✅ — ölçüm + toggle iskeleti (tamamlandı)
- [x] Kalıcı kullanım günlüğü (`usage.py`, gün bazlı kova, `usage.json`, 0600 + chown, 90 gün saklama)
- [x] Oturum trafiği: anlık hız (B/s), `session_started_at`, `usage_today/month_rx/tx`
- [x] `DataSaverPolicy` (profil bazlı): enabled/level/frugal_probes/quota_mb/quota_action
- [x] Cimri prob politikası: veri tasarrufu açıkken watchdog aralığı seyrelir (10/20 sn),
      dış IP sorgusu TTL'li önbelleğe alınır (120/300/900 sn) — kapalıyken davranış değişmez
- [x] Hız testi indirme boyutu veri tasarrufunda küçülür (10 MB → 5/2/1 MB)
- [x] Aylık kota: %80 uyarı, %100'de `warn` ya da `killswitch` (mevcut kill-switch'i kullanır)
- [x] API: `POST /api/data-saver`, `GET /api/usage`; CLI: `tetherctl usage`
- [x] Panel: "Veri Tasarrufu" kutusu (anahtar, seviye çipleri, oturum/bugün/ay, kota çubuğu)
- [x] Bug fix: hero trafik kutusu idle'da da görünüyordu (`!currentPhase !== "idle"` mantık hatası)
- [x] 29 yeni test (usage/datasaver/server), toplam 110 test geçiyor

## Sprint Veri Tasarrufu (Faz 3) ✅ — tarayıcı bayrakları + Firefox user.js (tamamlandı)
- [x] Chromium ailesi (chrome/chromium/brave/opera): veri tasarrufu açıkken
      `--autoplay-policy`, `--disable-background-networking`, `--disable-component-update`,
      `--disable-sync`, `--no-pings`, `--disk-cache-size=1GB`, ilgisiz feature'ları kapatma;
      katı seviyede ayrıca `--blink-settings=imagesEnabled=false`
- [x] Firefox: izole profile işaretli bloklu `user.js` yazılır/güncellenir/kaldırılır
      (prefetch/predictor/sync/güncelleme kapatma, 1 GB disk cache, katı seviyede görsel engeli);
      kullanıcının kendi `user.js` satırları markörler dışında korunur
- [x] VS Code/terminator gibi tarayıcı olmayan uygulamalar etkilenmez
- [x] `POST /api/apps/restart-all` + panelde "Açık uygulamaları yeniden başlat" düğmesi —
      bayraklar yalnızca YENİ açılan örnekte etkili olduğundan, açık tarayıcılara da yansıtmak için
- [x] 13 yeni test (Chromium bayrak matrisi, Firefox user.js splice/idempotency, restart_apps)

## Sprint Veri Tasarrufu (Faz 4) ✅ — bant genişliği tavanı (tamamlandı)
- [x] `shaping.py`: tamamen namespace-içi `tc`/IFB — upload doğrudan uplink'te `tbf`;
      download IFB'ye `mirred` yönlendirilip `cake` (yoksa `tbf`) ile şekillendirilir;
      IFB hiç kurulamazsa kaba ama çalışan ingress `police...drop`'a düşer
- [x] Seviye ön ayarları: hafif=sınırsız, dengeli=2000/1000 kbit, katı=700/300 kbit;
      `cap_down_kbit`/`cap_up_kbit` girilirse seviyeyi geçersiz kılar
- [x] Yaşam döngüsü entegrasyonu (`manager.py`): oturum başlarken, uplink switch'te
      (ESKİ arayüz ns'ten çıkmadan ÖNCE tavan kaldırılır — aksi halde qdisc host'a
      sürüklenebilir), watchdog yeniden bağlanmasında, `force_reconnect`'te ve
      daemon yeniden başlayıp oturumu devralırken (`adopt_if_running`) uygulanır/kaldırılır
  - watchdog ayrıca her tick'te `shaping.is_active()` ile gerçek durumu doğrular;
    kurallar sessizce düşerse (ör. WiFi yeniden ilişkilendirme) otomatik yeniden uygular
- [x] `POST /api/data-saver` artık `cap_down_kbit`/`cap_up_kbit` kabul ediyor; oturum
      aktifse `manager.reassert_shaping()` ile ANINDA canlı arayüze uygulanıyor
- [x] Panel: "Bant genişliği tavanı" alanı (opsiyonel ↓/↑ kbit girişi + "Uygulanan: ..." durum metni)
- [x] 33 yeni test (`shaping.py` komut dizisi/geriye-düşüş matrisi, manager yaşam döngüsü
      kablolaması, server cap alanları) — toplam 161 test geçiyor

## Sprint Veri Tasarrufu (Faz 5) ✅ — video/müzik yükleme engeli (tamamlandı, v3)
Hedef: toggle açıkken YouTube vb. **açılabilsin, yalnızca video/ses oynatılamasın**.
Tamamen tarayıcı seviyesinde — ayrı bir DNS servisi, port 53 bind'i ya da
`resolv.conf` değişikliği YOK.

### Elenen yaklaşımlar (ampirik olarak doğrulandı, kayıt için)
- **DNS proxy (v1)**: çalışıyordu ama sistem DNS'ini ele geçiriyordu; kullanıcı
  tarayıcı seviyesinde bir çözüm istedi → kaldırıldı.
- **Chromium uzantısı (v2)**: `--load-extension` Chrome 137+ tarafından güvenlik
  gerekçesiyle KALDIRILDI. Bayrak hata vermeden SESSİZCE yok sayılıyor → uzantı
  hiç yüklenmiyordu, toggle açık görünürken videolar oynamaya devam ediyordu.
- **`--disable-blink-features=MediaSource`**: Firefox'un MSE kapatmasının Chromium
  karşılığı olurdu; ölçüldü, ETKİSİZ. (Mekanizmanın kendisi çalışıyor —
  `Notifications` ile denendiğinde `typeof Notification === "undefined"` oluyor —
  ama `MediaSource` devre dışı bırakılabilir bir Blink runtime özelliği değil.)

### Arayüz: 5 kademeli sürgü (v4)
Tek aç/kapa anahtarı yerine `Sınırsız → 144p → 360p → 720p → Kapalı` sürgüsü
(`data_saver.media_level`). Eski boolean `block_media` alanı hem config
yüklerken hem API'de kabul edilmeye devam eder (`true` → `blocked`).

- [x] **144p / 360p / 720p**: bant genişliği tavanıyla (Faz 4 altyapısı) uygulanır —
      400 / 1000 / 3000 kbit. Tarayıcıya "şu çözünürlüğü oynat" diyen bir arayüz
      OLMADIĞINDAN kalite dolaylı olarak düşürülür: uyarlanabilir oynatıcılar
      ölçtükleri hıza göre rendition'ı kendileri seçer. Genel veri tasarrufu
      KAPALIYKEN de çalışır; veri tasarrufu tavanıyla birlikteyse **daha düşük
      olan** kazanır (720p + strict → 700 kbit; 144p + strict → 400 kbit).
      144p tavanı bilinçli olarak video bitrate'inin (~150 kbit) üstünde tutuldu —
      daha aşağısı sayfaların kendisini de kullanılamaz yapardı.
      *Dürüst sınırlama:* tavan tüm izole trafiği etkiler (yalnızca videoyu
      değil) ve gerçek çözünürlük siteye göre bir kademe oynayabilir.
- [x] **Kapalı**: aşağıdaki sert engel (tarayıcı seviyesi, yalnızca yeni açılan
      örnekte etkili). 360p/720p tarayıcıya hiç dokunmaz — anında uygulanır.
- [x] **"Bu pencere eski ayarla açıldı" uyarısı**: Tarayıcı bayrakları/user.js
      yalnızca BAŞLATMA anında uygulanabilir; kullanıcı sürgüyü çalışan bir
      tarayıcı varken değiştirirse o örnek eski ayarda kalır ("Kapalı dedim ama
      video hâlâ oynuyor" şikâyetinin kaynağı). Artık her `AppProcess`
      başlatıldığı kademeyi (`media_level`) kaydediyor; panel bunu güncel ayarla
      karşılaştırıp sarı bir uyarı + **"⟳ Şimdi uygula"** düğmesi gösteriyor
      (tek tıkla `restart_apps`). Uyarı yalnızca tarayıcı tarafını ilgilendiren
      geçişlerde (Kapalı ↔ diğerleri) çıkar; 144p/360p/720p arası geçişler bant
      genişliği tavanı olduğundan sessizce ve anında uygulanır.

### Uygulanan çözüm (sert engel = "Kapalı" kademesi)
- [x] **Firefox**: `media.mediasource.enabled=false` — MSE'yi API SEVİYESİNDE
      kapatır. YouTube/Netflix/Twitch/Spotify web player dâhil neredeyse tüm
      uyarlanabilir oynatıcı MSE'ye bağımlı; API yoksa segment indirme hiç
      başlamaz → **alan adı listesi gerekmez**. Ayrıca
      `media.mp4/webm/ogg/wave/av1.enabled=false` düz `<video src>` dosyalarını kapatır.
- [x] **Chromium ailesi** (Chrome/Opera/Brave/Chromium): `--host-resolver-rules`
      ile tarayıcının KENDİ ad çözümleyicisi medya CDN'lerini `127.0.0.1`'e eşler.
      İzole alanda 443'te dinleyen bir şey olmadığından `ERR_CONNECTION_REFUSED`
      ANINDA döner (blackhole IP'de olduğu gibi uzun zaman aşımı yok — ölçüldü: ~1 sn).
      Joker (`MAP *.googlevideo.com`) YouTube'un istek başına ürettiği
      `rr3---sn-4g5ednek.googlevideo.com` gibi adları da yakalar (gerçek Chrome 146
      ile uçtan uca doğrulandı).
- [x] Ana site alan adları (youtube.com, netflix.com, spotify.com, twitch.tv) ve
      küçük resim CDN'i (ytimg) listede DEĞİL → arayüz/önizlemeler normal çalışır
- [x] Genel veri tasarrufu anahtarından BAĞIMSIZ toggle (`data_saver.block_media`);
      diğer tarayıcı bayrakları/user.js gibi yalnızca YENİ açılan uygulamada
      etkili — "Açık uygulamaları yeniden başlat" düğmesiyle mevcutlara da uygulanır
- [x] Firefox `user.js`'te veri tasarrufu bloğuyla AYNI dosyada, ayrı işaretçi
      çiftiyle bir arada durur — biri kapatılınca diğeri silinmez (test edildi)
- [x] `POST /api/data-saver` `block_media` alanı + panelde bağımsız toggle
- [x] 33 test (bayrak üretimi/kabuk-güvenliği, joker kapsama, ana sitelerin
      engellenmediği, Firefox user.js yaz/kaldır/birlikte-var-olma, kademe→tavan
      eşlemesi, daha-düşük-kazanır kuralı, eski `block_media` göçü) — toplam 195 test geçiyor
- Bilinen sınırlama: **Chromium tarafı** hâlâ elle derlenmiş bir CDN listesine
  dayanıyor (Chromium'da MSE kapatılamadığı için başka yol yok); liste TAM
  değildir. Disney+/Prime Video gibi paylaşılan genel bulut CDN'i kullanan
  servisler kasıtlı olarak dışarıda (aksi halde ilgisiz siteler de kırılırdı).
  **Firefox tarafında bu sınırlama yok** — alan adından bağımsız çalışır.

## Yeniden Başlat = Tam Sıfırlama ✅ (davranışın netleştirilmesi)
Kısa bir süre "oturumu koru" davranışı denendi, ardından kullanıcı isteğiyle
**tam sıfırlamaya geri dönüldü**. Üç eylemin sorumlulukları:

| Eylem | Daemon | İzole oturum + uygulamalar |
|---|---|---|
| **Çıkış** | kapanır | **yaşar** (sonraki açılışta `adopt_if_running` devralır) |
| **Durdur** | çalışır | yıkılır |
| **Yeniden Başlat** | tazelenir (execv) | **yıkılır** — temiz başlangıç |

- [x] `/api/restart`: `stop_session()` + sahipsiz wpa_supplicant/dhcpcd temizliği
      + kopya daemon temizliği, ardından `execv`
- [x] 6 test (`tests/test_server_restart.py`): restart oturumu durduruyor,
      yardımcı süreçleri temizliyor, teardown patlasa bile ilerliyor;
      Durdur yıkıyor ama execv istemiyor; Çıkış oturumu koruyor
- **Önemli yan fayda:** `execv` diskteki GÜNCEL kodu yükler. Python modülleri
  süreç başlarken import edilir; panel HTML/JS'i her istekte diskten okunduğu
  için arayüz güncel görünse bile **Python tarafı eski kalabilir**. Kaynak
  değiştiğinde yeni mantığın etkin olması için "Yeniden Başlat" (ya da daemon'ı
  tamamen kapatıp açmak) gerekir.

## Yavaş DNS düzeltmesi ✅ (telefon hotspot'u — sahada bulundu)
**Belirti:** "İnternet bağlantısında sorun yok ama tarayıcılar internete
erişemiyor." Ping çalışıyor, `online: true`, ama sayfalar açılmıyor/çok yavaş
ve `public_ip` boş kalıyor.

**Kök neden:** glibc, A ve AAAA sorgularını VARSAYILAN olarak aynı kaynak
porttan paralel gönderir. Telefon hotspot'ları (iPhone Personal Hotspot,
`172.20.10.0/28`) ikinci yanıtı sık sık düşürür → resolver tam timeout (5 sn)
bekler. Bağlantı "çalışır" ama HER isim çözümlemesi ~5 sn sürer; tarayıcı bir
sayfa için onlarca çözümleme yaptığından internet yokmuş gibi görünür.

Aynı hatta ölçüm (izole alan içinden, 5 taze alan adı, önbeleksiz):

| Ayar | Toplam | Sorgu başına |
|---|---|---|
| Varsayılan | 26,8 sn | ~5,4 sn |
| **`single-request-reopen`** | **0,87 sn** | **~0,17 sn** |
| `single-request` | 0,95 sn | ~0,19 sn |

- [x] `engine._write_resolv` artık `options single-request-reopen timeout:2
      attempts:2` yazıyor (ayrı soket → çakışma yok; timeout emniyet ağı)
- [x] 5 regresyon testi (`TestResolvConf`) — seçenek düşerse test kırılır

## IPv6 kapatma (G-7) sessiz başarısızlığı ✅
Aynı teşhis sırasında bulundu: `net.ipv6.conf.all.disable_ipv6` sahada **0**
çıktı, yani G-7'nin kapattığını iddia ettiği IPv6 aslında AÇIKTI.

İki kusur vardı:
1. `sysctl` yalnızca `all`/`default` üzerinde ve uplink namespace'e ALINMADAN
   ÖNCE uygulanıyordu; sonradan taşınan arayüz kendi değerini koruyabiliyor.
2. Komutlar `check=False` ile çalışıp sonuç hiç okunmuyor, `_ipv6_disabled`
   koşulsuz `True` yapılıyordu → başarısızlık sessizce yutuluyordu.

- [x] Uplink içeri girdikten SONRA ve arayüze özel de uygulanıyor
      (start/switch/reconnect/watchdog yollarının hepsinde)
- [x] Değer geri okunup doğrulanıyor; kapanmadıysa panelde uyarı olayı üretiliyor

## Kalan / Gelecek — Veri Tasarrufu

## Kalan / Gelecek
- [ ] H-9 A↔B köprüsü (garageliman VPN ↔ kurum LAN) — M/L
- [ ] H-10 WireGuard desteği — M
- [ ] H-6 Panelden profil oluştur/düzenle/sil (UI) — M
- [ ] H-11 i18n (EN) — S
- [ ] H-12 Çoklu eşzamanlı namespace — L
- [ ] U-12 Yedekle-geri yükle & profil paylaşımı (API eklenecek) — S
- [ ] U-2 Hazır senaryolar / şablon kartları (config'de built-in) — S
