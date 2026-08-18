# Tether Isolator — Kod İncelemesi, Hata Analizi ve Geliştirme Planı

> **Bu dosyanın amacı:** Deneyimli bir yazılım mühendisinin projeyi baştan sona
> incelemesi sonucu çıkan bulgular. Başka bir yapay zeka (veya geliştirici) yalnızca
> bu dosyayı okuyarak listelenen düzeltme ve geliştirmeleri **uygulayabilir**.
> Her madde: *sorun / neden önemli / dosya-konum / önerilen çözüm / kabul kriteri*
> biçiminde yazıldı. Kod alıntıları yönlendirme amaçlıdır; uygulayan taraf ilgili
> satırları güncel dosyada teyit etmelidir.
>
> **İnceleme tarihi:** 2026-07-02 · **İncelenen sürüm:** 3.x (paket 3.0.0)
> **Kapsam:** `tether_isolator/*.py`, `webui/*`, `bin/*`, `install.sh`,
> `systemd/*`, `tests/*`, `docs/*`

---

## 0. Yönetici Özeti

Proje mimarisi **sağlam ve doğru fikir** üzerine kurulu: fiziksel arayüz taşıma
(Model B) ile yapısal izolasyon, sıfır harici Python bağımlılığı, modüler katmanlar,
60 birim testi ve iyi dokümantasyon. Ancak **üretim/kurumsal kullanım için üç kritik
açık** var ve bunlar en yüksek önceliktir:

1. **Güvenlik — Kimlik doğrulaması olmayan, root çalışan yerel API + CSRF/DNS-rebinding
   açığı.** Bir web sayfası veya yerel süreç, root daemon'a komut çalıştırtabilir.
   (Bkz. G-1, G-2)
2. **Doğruluk/Güvenlik — Kill-switch yok.** İzolasyonun asıl vaadi olan "sızmasın"
   garantisi, uplink/VPN koptuğunda kısmen delinebilir. (Bkz. G-6, H-3)
3. **Performans/Eşzamanlılık — Global kilit uzun operasyonlarda tutuluyor.** `start`
   sırasında panel (durum sorgusu) donuyor; ayrıca watchdog her 4 sn ping atarak
   ölçülü tether kotasını tüketiyor. (Bkz. P-1, P-2)

Aşağıdaki bölümler önceliklendirilmiştir: **🔴 kritik · 🟡 orta · 🟢 iyileştirme**.
Efor: **S (küçük, <yarım gün) · M (orta) · L (büyük)**.

---

## 1. HATALAR (Correctness Bugs)

### B-1 🟡 Sürüm numarası tutarsızlığı — S
- **Konum:** `tether_isolator/__init__.py:21` (`__version__ = "3.0.0"`),
  `tether_isolator/server.py:49` (`TetherIsolator/3.0`),
  `tether_isolator/server.py:94` (`"version": "3.0.0"`),
  `docs/STATUS.md:3` (`Sürüm: 3.1.0`).
- **Sorun:** Üç ayrı yerde farklı sürüm; `server.py` sabit string kullanıyor.
- **Çözüm:** Tek kaynak (`__init__.__version__`). `server.py` içindeki `_status_payload`
  ve `server_version`, `from . import __version__` ile beslensin. `STATUS.md` güncellensin.
- **Kabul kriteri:** `grep -r "3\.0\.0"` yalnızca `__init__.py`'de görünür; API `version`
  alanı `__version__`'ı döner.

### B-2 🟡 `__init__` docstring'i var olmayan `supervisor` modülüne atıf yapıyor — S
- **Konum:** `tether_isolator/__init__.py:16` ("supervisor — …").
- **Sorun:** Modül `manager.py` olarak yeniden adlandırılmış; docstring güncellenmemiş
  (dokümantasyon kayması).
- **Çözüm:** `supervisor` → `manager` (orkestratör + watchdog) olarak düzelt.

### B-3 🟡 `cmd_start` kilitsiz durum okuyor ve sonsuz döngüde bekliyor — S
- **Konum:** `tether_isolator/cli.py:178-184`.
- **Sorun:** `while m.state.phase not in ("idle",): time.sleep(1)` — `phase` normalde
  hiç `idle` olmaz (yalnızca `stop_session` yapar; CLI start akışında çağrılmaz), yani
  Ctrl+C'ye kadar sonsuz döner. Ayrıca `m.state.phase` kilit dışından okunuyor.
- **Çözüm:** Amaç "watchdog thread'i ön planda canlı tutmak"tır; bunu açıkça
  `m._wd_thread.join()` veya `signal.pause()` ile ifade et. Kilitsiz okuma yerine
  `m.snapshot()["phase"]` kullan ya da sadece sinyal bekle.

### B-4 🟡 Relay kapatılırken `ip_forward` sysctl'i eski haline döndürülmüyor — S
- **Konum:** `tether_isolator/relay.py:88` (enable → `ip_forward=1`),
  `relay.py:229-236` (disable → yalnızca kurallar + veth silinir).
- **Sorun:** Relay bir kez açılınca `net.ipv4.ip_forward=1` **kalıcı** olarak açık kalır;
  kapatınca kapatılmaz. Host genelinde yönlendirme açık bırakmak istenmeyen bir yan etki
  ve güvenlik açısından "eski hale dön" ilkesini bozar.
- **Çözüm:** enable'da mevcut değeri oku (`sysctl -n net.ipv4.ip_forward`), state'e sakla;
  disable'da yalnızca **biz açtıysak** eski değere geri al. Alternatif: relay'e özgü
  yönlendirmeyi sysctl yerine sadece nft/iptables FORWARD kurallarıyla sınırla ve global
  forwarding'e dokunma (mümkünse tercih edilir).
- **Kabul kriteri:** Kapalıyken `sysctl net.ipv4.ip_forward` relay öncesi değeriyle aynı.

### B-5 🟡 `_static` yol-öneki kontrolü kardeş dizin kaçışına açık — S
- **Konum:** `tether_isolator/server.py:107-111`.
- **Sorun:** `full.startswith(WEBUI_DIR)` — `WEBUI_DIR="/a/webui"` iken `/a/webui-x`
  öneki geçer. `normpath` `../` daraltsa da bu prefix kontrolü zayıf.
- **Çözüm:** `os.path.realpath(full)` alıp `os.path.commonpath([real, WEBUI_DIR]) == WEBUI_DIR`
  ile doğrula; ya da `WEBUI_DIR + os.sep` önekini kontrol et. Sembolik bağ kaçışını da kapatır.
- **Kabul kriteri:** `/style.css` çalışır; `/../etc/passwd`, `/..%2f..` ve sibling-dir
  denemeleri 404 döner. (Test eklenebilir.)

### B-6 🟢 `real_user()` doğrulanmamış `SUDO_USER` değerini döndürüyor — S
- **Konum:** `tether_isolator/system.py:76-90`.
- **Sorun:** `SUDO_USER` ortam değişkeni geçerli bir kullanıcı adı olmayabilir (ortam
  manipülasyonu); doğrudan döndürülüyor. `PKEXEC_UID` için `pwd.getpwuid` doğrulaması var
  ama `SUDO_USER` için yok.
- **Çözüm:** Dönüşten önce `pwd.getpwnam(val)` ile doğrula; başarısızsa sıradaki kaynağa geç.

### B-7 🟢 `_running_on_host` `pgrep -x` süreç adı 15 karakterle sınırlı — S
- **Konum:** `tether_isolator/apps.py:128-144`.
- **Sorun:** `pgrep -x` çekirdek `comm` alanını (maks. 15 karakter) tam eşler;
  `google-chrome-stable` gibi uzun adlar ya da tam-yol argv'ler yanlış eşleşebilir →
  "host'ta açık" kontrolü atlanabilir (izolasyon delinme riski, düşük olasılık).
- **Çözüm:** `pgrep -f` ile birlikte argv doğrulaması, ya da `list_namespace_pids`
  yaklaşımını host için de kullan (net inode karşılaştırması zaten güvenilir).

### B-8 🟢 `Settings.load` `http_port`/`watchdog_interval` doğrulaması yapmıyor — S
- **Konum:** `tether_isolator/config.py:112-119`.
- **Sorun:** Bozuk config'te `int(...)` `ValueError` fırlatır ve tüm daemon açılmaz;
  negatif/0 watchdog_interval `wait()` davranışını bozar.
- **Çözüm:** Değerleri sınırla (port 1-65535, watchdog ≥1) ve hatalı config'te varsayılana
  düşerek uyar (fail-safe).

---

## 2. GÜVENLİK (En Yüksek Öncelik)

### G-1 🔴 Root daemon API'sinde kimlik doğrulaması yok — M
- **Konum:** `tether_isolator/server.py:234-246` (`serve`, 127.0.0.1'e bağlanır),
  tüm `_api_post` uçları (`/api/start`, `/api/launch`, `/api/relay`, `/api/vpn`…).
- **Sorun:** Daemon **root** çalışır ve `127.0.0.1`'i dinler ama **hiçbir kimlik/token
  doğrulaması yoktur**. `127.0.0.1` çok-kullanıcılı makinede **her yerel kullanıcı**
  tarafından erişilebilir. `/api/launch` ve `/api/start`, gövdeden gelen `app` komutunu
  `shlex.split` → `runuser` ile çalıştırır (gerçek kullanıcı kimliğiyle); saldırgan yerel
  bir kullanıcı, root daemon'a ağ namespace'i manipülasyonu yaptırabilir ve masaüstü
  kullanıcısı adına program başlatabilir.
- **Çözüm (önerilen):**
  1. Daemon açılışında rastgele bir **oturum token'ı** üret, `0600` izinli bir dosyaya
     yaz (`/run/tether-isolator/token`, yalnızca root okur; masaüstü başlatıcısı token'ı
     okuyup UI'ye enjekte eder ya da tarayıcıya localStorage/URL-fragment ile aktarır).
  2. Her `/api/*` isteğinde `Authorization: Bearer <token>` (veya özel header) doğrula;
     eksik/yanlışsa `401`.
  3. Statik dosyalar (index/app.js/css) token gerektirmesin; yalnızca API uçları.
- **Alternatif (daha basit):** Daemon'u TCP yerine **Unix domain socket** (`0600`, sahibi
  root) üzerinden dinlet; tarayıcı erişimi için ince bir yerel köprü. Token yaklaşımı
  tarayıcı-uyumlu olduğu için tercih edilir.
- **Kabul kriteri:** Token'sız `curl -XPOST http://127.0.0.1:8787/api/stop` → `401`.

### G-2 🔴 CSRF / DNS-rebinding — kötü niyetli web sayfası API'yi tetikleyebilir — S/M
- **Konum:** `tether_isolator/server.py:67-74` (`_body`), `do_POST` (122-129).
- **Sorun:** `_body()` **Content-Type'ı kontrol etmez**, gövdeyi doğrudan `json.loads`
  eder. Bu yüzden bir web sayfası `fetch("http://127.0.0.1:8787/api/start", {method:"POST",
  body: JSON.stringify(...)})` çağrısını **`text/plain` içerik tipiyle** yapabilir — bu bir
  "simple request"tir, **CORS preflight tetiklemez**, yani tarayıcı isteği gönderir ve
  sunucu yan etkiyi (oturum başlat, uygulama çalıştır) uygular. Yanıtı okuyamasa da
  **eylem gerçekleşir**. `Host` başlığı da doğrulanmadığından DNS-rebinding ile de
  ulaşılabilir.
- **Çözüm (birlikte uygulanmalı):**
  1. **`Host` başlığı doğrulaması:** yalnızca `127.0.0.1[:port]` / `localhost` kabul et;
     aksi halde `403` (DNS-rebinding'i kapatır).
  2. **Content-Type zorunluluğu:** POST'larda `application/json` iste (basit-istek
     bypass'ını kapatır) **ve/veya** özel bir header (`X-Tisor-CSRF: 1`) zorunlu kıl —
     özel header simple-request olmadığından preflight'a zorlar, cross-site engellenir.
  3. G-1'deki token zaten en güçlü savunmadır; bu ikisi derinlemesine savunma.
- **Kabul kriteri:** `Host: evil.com` başlıklı istek `403`; `Content-Type` olmadan POST
  reddedilir; UI normal çalışır.

### G-3 🟡 `config.json` düz metin parola içeriyor ve dosya izni kısıtlı değil — S/M
- **Konum:** `tether_isolator/config.py:121-136` (`save`), profil alanları
  `wifi_password`, dolaylı olarak `.ovpn` yolları.
- **Sorun:** WiFi (ve gelecekte VPN) parolaları `config.json`'da **düz metin**. `save()`
  dosyayı `os.replace` ile yazıyor; **mod açıkça `0600` yapılmıyor**, umask'e bağlı
  (genelde `0644`) → diğer yerel kullanıcılar okuyabilir. `_chown_to_real_user` sahipliği
  veriyor ama izni daraltmıyor.
- **Çözüm:**
  1. Kısa vade: `config.json` ve `CONFIG_DIR` için `os.chmod(..., 0o600/0o700)`.
  2. Orta vade (TODO'da da var): parolayı keyring'e (`secretstorage`/libsecret, opsiyonel
     import) taşı; yoksa en azından ayrı `0600` dosya. `_profile_view` zaten maskeliyor —
     bunu diske de uygula.
- **Kabul kriteri:** `stat -c %a config.json` → `600`.

### G-4 🟡 Hata mesajları istemciye ham `str(e)` olarak sızıyor — S
- **Konum:** `tether_isolator/server.py:127-129` (`do_POST` genel `except`),
  çeşitli `EngineError` mesajları.
- **Sorun:** İç yol/komut ayrıntıları UI'ye dönüyor (bilgi sızıntısı, düşük risk ama
  kimlik doğrulaması eklenince önemi artar). Ayrıca `alert(e.message)` XSS değil ama
  ham içerik.
- **Çözüm:** İstemciye kullanıcı-dostu, genel mesaj; ayrıntı yalnızca sunucu loguna
  (`log.exception` zaten var). Hata kodları/kategorileri döndür.

### G-5 🟡 İstek gövdesi boyut sınırı yok (yerel DoS) — S
- **Konum:** `tether_isolator/server.py:67-74` (`_body` → `Content-Length` kadar okur).
- **Sorun:** Devasa `Content-Length` ile daemon belleği şişirilebilir (yerel DoS).
- **Çözüm:** Makul üst sınır (ör. 1 MB); aşınca `413`.

### G-6 🔴 Kill-switch yok — kopma anında sızıntı mümkün — S/M
- **Konum:** Kavramsal; `manager._tick` (`manager.py:418-464`), `engine`.
- **Sorun:** İzolasyonun asıl güvencesi, uplink düştüğünde/namespace bozulduğunda izole
  uygulamaların trafiğinin "sessizce başka yola düşmemesi"dir. Şu an ns içinde varsayılan
  rota kaybolduğunda paketler basitçe gitmez (iyi), ama **relay açıkken** host rotaları
  aynalı olduğundan ve VPN düştüğünde split-tunnel geri döndüğünde beklenmeyen çıkış
  olabilir. Ayrıca IPv6 (G-7) ayrı bir kaçış yoludur.
- **Çözüm:** ns içinde açık bir **fail-closed rota politikası**: varsayılan uplink yokken
  `ip route add blackhole default` (veya `unreachable`) — böylece "internet yok" durumunda
  paket sessizce host'a/yanlış yola düşmek yerine reddedilir. VPN "zorunlu" işaretliyse tün
  düşünce izole moda dön (bkz. H-3). Panelde kill-switch anahtarı.
- **Kabul kriteri:** Uplink çıkarıldığında izole app'ten dışarı hiçbir paket sızmaz
  (tcpdump ile host arayüzlerinde doğrulama senaryosu belgelenir).

### G-7 🟡 IPv6 sızıntısı kapatılmıyor — S
- **Konum:** `engine.ensure_namespace` / `move_uplink_in`.
- **Sorun:** IPv4 yapısal izole olsa da, namespace'te IPv6 açıksa SLAAC/RA ile IPv6
  bağlantısı ayrı bir çıkış oluşturabilir; relay/host tarafında beklenmeyen IPv6 yolu.
- **Çözüm:** Politika kararı ver: (a) ns içinde IPv6'yı kapat
  (`sysctl -w net.ipv6.conf.all.disable_ipv6=1` namespace bağlamında), ya da (b) IPv6'yı
  bilinçli yönet. Varsayılan güvenli seçim: kapat. `doctor`'a IPv6 durum kontrolü ekle.

### G-8 🟡 systemd birimi `CAP_SYS_ADMIN` + geniş yetkiyle çalışıyor, sıkılaştırma yok — S/M
- **Konum:** `systemd/tether-isolatord.service:6-18`.
- **Sorun:** `User=root`, `CAP_NET_ADMIN CAP_SYS_ADMIN`, `NoNewPrivileges=no`. Namespace
  işlemleri için gerekli ama servis hiç sıkılaştırılmamış (`ProtectSystem`,
  `ProtectHome`, `PrivateTmp`, `RestrictAddressFamilies` vb. yok).
- **Çözüm:** İşlevi bozmayan systemd sıkılaştırmaları ekle: `ProtectSystem=strict` +
  gerekli yollar için `ReadWritePaths=/run/tether-isolator /etc/netns`, `ProtectKernelLogs`,
  `RestrictRealtime`, `RestrictSUIDSGID`, `LockPersonality`. `pkexec` yolu için de dar bir
  polkit politikası yaz (TODO F). `Documentation` yolu (`/opt/...`) gerçek kuruluma göre
  düzelt.

### G-9 🟢 Panelde CSP (Content-Security-Policy) başlığı yok — S
- **Konum:** `server.py:_static` yanıt başlıkları; `webui/index.html`.
- **Sorun:** Yerel araç olsa da statik yanıtlarda CSP yok; ileride bir XSS vektörü açılırsa
  savunma katmanı eksik. `escapeHtml` var (iyi) ama derinlemesine savunma için CSP eklenmeli.
- **Çözüm:** Statik yanıtlara `Content-Security-Policy: default-src 'self'; script-src 'self';
  style-src 'self'` ekle. (Inline script yok; app.js ayrı dosya — CSP uyumlu.)

---

## 3. PERFORMANS & EŞZAMANLILIK

### P-1 🔴 Global kilit uzun operasyonlar boyunca tutuluyor → panel donuyor — M
- **Konum:** `manager.py` — `start_session:48-111`, `switch_uplink:342-389`,
  `connect_vpn:184-208`, hepsi `with self._lock:` içinde; `snapshot:511-513` **aynı kilidi**
  alır. `server.py` `ThreadingHTTPServer` olduğundan `/api/status` ayrı thread'de gelir.
- **Sorun:** `start_session`, DHCP (`dhcpcd -w`, `timeout=45`), WiFi association bekleme
  (`timeout=20`), VPN bağlanma (`wait_connected`, `timeout=30`) gibi **saniyelerce bloklayan**
  çağrıları kilit **altında** yapar. Bu sırada UI'nin her 2 sn'de attığı `/api/status` →
  `manager.snapshot()` **aynı kilidi bekler** → panel başlatma boyunca **tamamen donar**
  (kullanıcı ilerleme göremez, "takıldı mı?" hissi). Ayrıca `relay.enable` içindeki DNS
  çözümü (`_resolve_extra` → `getaddrinfo`) de kilit altında bloklar.
- **Çözüm:**
  1. **Durum okuması kilitsiz olsun:** `RuntimeState`'i atomik/kopya olarak yayınla; `snapshot`
     kısa bir kilit ya da immutable kopya döndürsün. Uzun işlemler kilidi "yazma anları"nda
     kısa tutsun.
  2. Uzun operasyonları (`start`/`switch`/`vpn`) **arka plan iş parçacığında** yürüt; API
     hemen `202 Accepted`/`{"ok":true,"async":true}` dönsün, ilerleme `events` akışından
     izlensin. UI zaten olay akışı gösteriyor.
  3. En azından: DHCP/VPN/DNS bekleme çağrılarını kilit **dışına** çıkar; kilit yalnızca
     `state` mutasyonunu korusun.
- **Kabul kriteri:** `start` sırasında `/api/status` <100 ms'de yanıt verir; panelde canlı
  ilerleme görünür.

### P-2 🔴 Watchdog her 4 sn 3 paket ICMP atıyor → ölçülü tether kotası tüketimi — S
- **Konum:** `engine.connectivity:187-194` (`ping -c 3 8.8.8.8`),
  `manager._tick:445` (her tikte `_refresh_network(light=True)` → `connectivity`).
- **Sorun:** `watchdog_interval=4` sn ve her tik `8.8.8.8`'e **3 ping** atıyor →
  ~64.800 ping/gün. Bu araç **telefon tether kotasını korumayı** önemsiyor (TODO'da trafik
  sayacı bu yüzden var); sürekli aktif prob bu amaca ters ve pil/veri tüketir.
- **Çözüm:**
  1. Sağlık kontrolünü **pasifleştir/aralıkla:** online iken ping aralığını uzat (ör. 30-60 sn),
     yalnızca `degraded`/`reconnecting`'te sıklaştır.
  2. Mümkünse **pasif sinyal** kullan: arayüz taşıyıcı durumu (`operstate`), rota varlığı,
     DHCP lease geçerliliği — ICMP yerine. ICMP yalnızca son doğrulama için.
  3. Ping'i tek pakete indir + daha uzun aralık; ölçülü uplink için "düşük veri modu" ayarı.
- **Kabul kriteri:** Boşta online oturumda dakikada atılan prob paketi belirgin azalır
  (ör. ≤2/dk), yeniden bağlanma yine <10 sn'de algılanır.

### P-3 🟡 `/api/status` her çağrıda `discover_installed` + `list_host_interfaces` çalıştırıyor — S
- **Konum:** `server.py:_status_payload:91-102`, `apps.discover_installed:40-49`
  (her çağrı ~10 `shutil.which`), `system.list_host_interfaces` (subprocess `ip link`).
- **Sorun:** 2 sn'lik polling'te her seferinde PATH taraması + subprocess. Kurulu uygulamalar
  neredeyse hiç değişmez.
- **Çözüm:** `discover_installed` sonucunu **önbelleğe al** (TTL ör. 30-60 sn ya da daemon
  ömrü boyunca sabit). Arayüz listesini de kısa TTL ile önbelleğe al ya da yalnızca
  `/api/interfaces` çağrısında yenile; `/api/status` daha hafif olsun.
- **Kabul kriteri:** Boşta polling'te süreç başına subprocess/PATH-tarama sayısı düşer.

### P-4 🟢 `public_ip()` her tam yenilemede harici HTTP çağrısı — S
- **Konum:** `engine.public_ip:230-240`, `manager._refresh_network:477-478` (non-light).
- **Sorun:** `curl api.ipify.org` harici bağımlılık ve gecikme; her reconnect'te çağrılır.
- **Çözüm:** Sonucu önbelleğe al (IP nadiren değişir); yalnızca uplink/rota değiştiğinde
  yenile. Zaman aşımını kısa tut (zaten 5 sn).

### P-5 🟢 `_chown_to_real_user` her `save()`'de tüm config ağacını yürüyor — S
- **Konum:** `config.py:145-161`.
- **Sorun:** Küçük ama gereksiz; her ayar kaydında `os.walk`.
- **Çözüm:** Yalnızca yazılan dosya + dizini chown et.

---

## 4. MİMARİ, BAKIM & KOD KALİTESİ

### M-1 🟡 `server.py` manager iç durumuna doğrudan erişiyor (kapsülleme sızıntısı) — S
- **Konum:** `server.py:181, 192, 256` → `m._active_profile` (private) kullanımı.
- **Çözüm:** `Manager`'a `active_profile` (property) ve `set_relay_extra_targets(...)` gibi
  açık API ekle; `server` private alanlara dokunmasın.

### M-2 🟡 chown mantığı iki yerde tekrarlanıyor — S
- **Konum:** `system.chown_to_user` ve `config._chown_to_real_user`.
- **Çözüm:** Tek yardımcıya indir (system'deki genel olanı kullan); config onu çağırsın.

### M-3 🟡 Statik/lint/CI yok — S
- **Konum:** Repo genel; `docs/TODO.md` G bölümü.
- **Çözüm:** `ruff` (lint) + `mypy` (stdlib-only kalarak) + GitHub Actions:
  `py_compile` + `python -m unittest discover -s tests` + `ruff check`. Her push'ta koşsun.
- **Kabul kriteri:** CI yeşil; regresyonları yakalar.

### M-4 🟡 HTTP katmanı (server.py) için test yok — M
- **Konum:** `tests/` (server testi yok; 60 test motor/manager/relay/config/vpn/apps/state).
- **Çözüm:** `_Handler`'ı sahte manager ile test et: `/api/status` şeması, path-traversal
  (B-5), Host doğrulaması (G-2), token (G-1). `http.client` ile entegrasyon testi.

### M-5 🟢 Karmaşık kabuk-string üretimi (`apps._spawn`) kırılgan — S/M
- **Konum:** `apps.py:96-125` — `sh -c` içinde resolv.conf bind preamble + `runuser` +
  `env` zinciri, string birleştirmeyle.
- **Not:** Mantık doğru ve iyi belgeli, ama string tabanlı kabuk üretimi bakımı zor ve
  hataya açık. `shlex.quote` kullanılmış (iyi).
- **Çözüm (opsiyonel):** Küçük bir yardımcı script/fonksiyona çıkar; birim testle preamble'ı
  doğrula. Alternatif: resolv.conf düzeltmesini Python tarafında mount ns kurarak yap.

### M-6 🟢 `RelayPolicy`'de kullanılmayan eski alanlar taşınıyor — S
- **Konum:** `config.py:47-51` (`scope`, `lan_targets`, `verify_hosts`, `allowed_domains`).
- **Not:** Geriye dönük uyumluluk için bilinçli tutulmuş (yorumda açık). Kabul edilebilir.
- **Çözüm (opsiyonel):** Yükleme sırasında bir kez migrasyon yapıp eski alanları sessizce
  düşür; şema sadeleşir.

---

## 5. YENİ ÖZELLİK / GELİŞTİRME ÖNERİLERİ

> Aşağıdakilerin bir kısmı `docs/TODO.md`'de zaten var; burada **öncelik + gerekçe +
> uygulama ipucu** ile pekiştiriliyor. Yeni öneriler ★ ile işaretlendi.

### H-1 🔴 Trafik / veri kullanım sayacı — S
- **Neden:** Ölçülü tether kotası için en yüksek günlük fayda. P-2 ile birlikte "veri dostu".
- **Uygulama:** İzole ns içinde `/proc/net/dev` (veya `ip -s link`) periyodik oku; uplink
  arayüzü için rx/tx byte deltası; `state`'e ekle; panelde canlı gösterge + oturum toplamı.
- **Kabul:** Panelde indirilen/yüklenen MB canlı görünür.

### H-2 🔴 Sistem tepsisi göstergesi (AppIndicator) — M
- **Neden:** Panel kapalıyken durum + hızlı başlat/durdur/uplink değiştir. Günlük kullanım.
- **Uygulama:** `gi`/AppIndicator (opsiyonel bağımlılık); daemon'a token'la konuşur (G-1).
- **Not:** Harici bağımlılık gerektirir; "stdlib-only" ilkesine opsiyonel eklenti olarak
  eklenebilir (import başarısızsa özellik kapalı).

### H-3 🟡 VPN watchdog + "zorunlu VPN" (fail-closed) — S
- **Neden:** Tün düşerse (uplink değişimi/uyku) izole app trafiği yanlış yoldan çıkmasın.
- **Uygulama:** `_tick`'e VPN canlılık kontrolü (`vpn.is_active`); düşerse yeniden bağlan;
  profil `vpn_required=True` ise VPN yokken kill-switch (G-6) devreye girsin.

### H-4 🟡 Uyku/uyanma (suspend/resume) dayanıklılığı — M
- **Neden:** Laptop uykudan dönünce uplink/DHCP/VPN/relay bozulur.
- **Uygulama:** systemd-logind `PrepareForSleep` D-Bus sinyalini dinle (ya da resume sonrası
  watchdog agresif reconcile); uplink/DHCP/VPN/relay durumunu otomatik onar.

### H-5 ✅ WiFi SSID tarama — S
- **Neden:** SSID elle yazılıyor; hataya açık.
- **Uygulama:** `system.wifi_scan()` (`iw dev <if> scan`, root) → SSID + sinyal listesi;
  `GET /api/wifi/scan?iface=` ucu; kayıtlı ağlar `Settings.wifi_networks`'te SSID→parola
  olarak kalıcı (profil bağımsız, Ubuntu ağ menüsü mantığı); UI'de tıklanabilir ağ listesi
  (`webui/index.html` + `app.js`, kayıtlı ağlar 🔑 rozetiyle, parola alanı yalnızca
  kayıtsız ağ seçilince görünür).

### H-6 🟡 Panelden profil oluştur/düzenle/sil (UI) — M
- **Neden:** API (`/api/profile`) hazır, UI formu yok. İçe/dışa aktarma ile taşınabilirlik.
- **Uygulama:** `webui`'de profil düzenleme modalı; JSON import/export.

### H-7 🟡 Bağlantı sağlık göstergesi (uçtan uca) — S
- **Neden:** "online" yetersiz; gateway ping + DNS çözümü + dış IP + VPN + relay hedefleri
  tek bakışta yeşil/sarı/kırmızı.
- **Uygulama:** `state`'e alt-sağlık alanları; UI'de rozetler. P-2 ile uyumlu (düşük veri).

### H-8 🟡 ★ "Logları/tanı paketi indir" — S
- **Neden:** Gerçek donanım sorun gidermesi için. TODO'da kısmen var.
- **Uygulama:** `/api/diag` → `doctor` çıktısı + son olaylar + rotalar + OpenVPN log kuyruğu
  (parolalar maskeli) tek JSON/zip. UI "İndir" düğmesi.

### H-9 🟢 A↔B köprüsü (garageliman VPN ↔ kurum LAN) — M/L
- **Neden:** Kullanıcının ana senaryosu (bkz. `docs/STATUS.md`, TODO B).
- **Uygulama:** Yeni `bridge.py`; **Yol A** (ns içinde `socat`/nftables DNAT ile seçili
  kurum servisini VPN arayüzünde yayınla) önerilir — uzak uç yapılandırması gerekmez.

### H-10 🟢 WireGuard desteği — M
- **Neden:** OpenVPN'in DCO/cipher/namespace dertleri yok; daha hızlı.
- **Uygulama:** `wg`/`wg-quick` namespace içinde; `vpn.py`'ye backend soyutlaması.

### H-11 🟢 i18n (EN) + tema anahtarı — S
- **Neden:** Arayüz yalnızca TR; kurumsal/uluslararası kullanım.
- **Uygulama:** UI string'lerini sözlüğe çıkar; TR/EN geçişi; karanlık/aydınlık tema.

### H-12 🟢 Çoklu eşzamanlı namespace — L
- **Neden:** Aynı anda birden çok izole oturum (farklı uplink+profil).
- **Uygulama:** Tek-namespace varsayımını kaldır; `Manager`'ı oturum-örneği yap. Büyük iş.

---

## 6. GEREKSİZ / SADELEŞTİRİLEBİLİR

- **`RelayPolicy` eski alanları** (M-6): geriye uyumluluk dışında işlevi yok; migrasyonla düşür.
- **`legacy/` bash scriptleri:** Referans olarak değerli; ama README/STATUS'ta "kaynak
  doğruluğu buradan" atfı sürdürülüyorsa dokümante et, değilse arşiv klasörü olduğunu netleştir.
- **Sürüm string tekrarı** (B-1): tek kaynağa indir.
- **`public_ip` harici çağrısı** (P-4): çoğu kullanımda gereksiz sıklıkta; önbellekle.
- **Not:** Çekirdek mimaride "gereksiz" bir katman yok; modüler ayrım yerinde. Asıl kazanım
  sadeleştirmeden çok **kilit/prob sıklığı** optimizasyonunda (P-1, P-2).

---

## 7. ÖNCELİKLENDİRİLMİŞ UYGULAMA SIRASI (Yol Haritası)

**Sprint 1 — Güvenlik & Doğruluk temeli (kritik):**
1. G-1 API token kimlik doğrulaması (M)
2. G-2 Host + Content-Type/CSRF doğrulaması (S)
3. G-6 Kill-switch (fail-closed rota) (S/M)
4. G-3 config.json `0600` + parola sıkılaştırma (S)
5. B-1/B-4/B-5 sürüm birleştirme + ip_forward geri alma + path-traversal düzeltme (S)

**Sprint 2 — Performans & Kullanılabilirlik:**
6. P-1 kilit granülerliği / async start (M)
7. P-2 watchdog prob sıklığı / düşük-veri modu (S)
8. P-3 discover_installed önbelleği (S)
9. H-1 trafik sayacı (S)
10. H-7 uçtan uca sağlık göstergesi (S)

**Sprint 3 — Dayanıklılık & Kapsam:**
11. H-3 VPN watchdog + zorunlu VPN (S)
12. H-4 suspend/resume dayanıklılığı (M)
13. G-7 IPv6 sızıntı kapatma (S)
14. M-3/M-4 CI + server testleri (S/M)
15. H-2 sistem tepsisi (M)

**Sprint UX — Ürün cilası & son kullanıcı avantajı (bkz. Bölüm 9):**
16. U-1 İlk çalıştırma sihirbazı (M) + U-4 akıllı uplink otomatik algılama (S)
17. U-3 "İzolasyon kanıtı" güven rozeti (S) — ürünün ayırt edici değeri
18. U-2 Hazır senaryolar / tek-tık şablonlar (S)
19. U-6 Görsel veri kullanım paneli (S, H-1 üstüne) + U-7 hız/kalite testi (S)
20. U-5 Masaüstü bildirimleri (S) + U-10 hatada rehberli kurtarma (S)
21. U-11 Boş/yükleniyor durumları + mikro-etkileşim + erişilebilirlik (S)

**Sonraki:** H-9 A↔B köprüsü, H-10 WireGuard, H-6 profil UI, H-11 i18n, H-12 çoklu namespace,
U-8 oturum zaman çizelgesi, U-9 duraklat/geçici izin, U-12 yedekle-geri yükle & profil paylaşımı,
U-13 komut paleti & kısayollar.

---

## 8. UYGULAYAN İÇİN NOTLAR (Test & Doğrulama)

- **dry-run her katmanda var:** Kod değişikliklerini önce `./bin/tetherctl --dry-run gui`
  ve `python3 -m unittest discover -s tests` ile doğrula (root/donanım gerekmez).
- **Gerçek donanım senaryoları** (TODO G-1): USB tether, WiFi, relay, VPN, uplink switch,
  resume, kill-switch — her biri için beklenen çıktı + doğrulama komutu belgelenmeli.
- **Sızıntı doğrulaması:** Güvenlik değişikliklerinden sonra host arayüzlerinde `tcpdump`
  ile izole app trafiğinin **görünmediğini** teyit et (yapısal izolasyonun ampirik kanıtı).
- **Geriye uyumluluk:** `config.json` şeması değişirse eski profilleri yükleyen migrasyon
  ekle; `Profile.from_dict` bilinmeyen alanları zaten süzüyor (iyi).

---

## 9. ÜRÜN & SON KULLANICI DENEYİMİ (UX) — "Profesyonel ürün" cilası

> Buraya kadarki bölümler **doğruluk, güvenlik ve performans** içindi. Bu bölüm, aynı
> sağlam çekirdeği **son kullanıcıya somut avantaj sağlayan, profesyonel bir ekibin
> çıkardığı hissi veren bir ürüne** dönüştürecek geliştirmeleri içerir. Hedef kitle çoğunlukla
> "ağ namespace'i" ne demek bilmeyen, sadece *"işim güvenli ve ayrı bir hattan çıksın,
> uğraşmadan çalışsın"* isteyen kullanıcıdır. Her madde: **Kullanıcı değeri → Deneyim →
> Uygulama ipucu (mevcut mimariden nasıl beslenir)** biçiminde.

### Ürün tasarım ilkeleri (uygulayan taraf bunları rehber alsın)
1. **Sıfır jargon, sonuç odaklı dil.** "namespace/veth/MASQUERADE" arka planda kalsın;
   kullanıcı "İşin ayrı hatta ve güvende ✅" görsün.
2. **Varsayılanlar doğru olsun (opinionated).** Kullanıcı hiçbir şey seçmeden mantıklı bir
   kurulum çalışmalı; ileri ayarlar "Gelişmiş" altında gizli.
3. **Her durum görünür ve güven verici.** Boşluk yok: yükleniyor, boş, hata — hepsinin
   tasarlanmış bir hâli var. Kullanıcı asla "takıldı mı?" diye tahmin etmesin.
4. **Kurtarma her zaman bir tık ötede.** Bir şey bozulduğunda kullanıcıya *ne olduğu* ve
   *tek düğmeyle nasıl düzeltileceği* söylensin (terminal/komut istemeden).
5. **Güveni kanıtla, iddia etme.** Ürünün ana vaadi "izolasyon"; bunu kullanıcıya
   **görünür kanıtla** göster (U-3). Farkı yaratan budur.

---

### U-1 🔴 İlk çalıştırma sihirbazı (Onboarding) — M
- **Kullanıcı değeri:** İlk açılışta boş bir kontrol paneliyle karşılaşmak yerine 3-4 adımlık
  rehberle saniyeler içinde ilk güvenli oturumu kurar. "Kurulumu beceremedim" terkini yok eder.
- **Deneyim:** Adım 1: *"Neyi izole etmek istiyorsun?"* (tarayıcı seç). Adım 2: *"Hangi hattan
  çıksın?"* (algılanan uplink'ler kart olarak; telefon takılıysa otomatik önerilir). Adım 3:
  *"Kurum ağına da erişmen gerekiyor mu?"* (relay aç/kapa, sade dille). Adım 4: *"Hazır!"* +
  tek düğme başlat. Sonraki açılışlarda sihirbaz atlanır; "Ayarlar > Sihirbazı tekrar çalıştır".
- **Uygulama ipucu:** Salt istemci-tarafı (webui). `config.json`'a `onboarded: true` bayrağı
  (yeni `Settings` alanı). Adımlar mevcut `/api/interfaces`, `/api/status`, `/api/start`
  uçlarını kullanır; yeni backend gerekmez.

### U-2 🔴 Hazır senaryolar / tek-tık şablonlar — S
- **Kullanıcı değeri:** Kullanıcının tekrar eden kurulumunu ezberlemesine gerek kalmaz; "işe
  git" gibi tek karta dokunup çalıştırır. Ürünü "araç" olmaktan çıkarıp "çözüm" yapar.
- **Deneyim:** Panelin üstünde büyük senaryo kartları: **"📱 Telefon tether + izole tarama"**,
  **"🏢 Kurum ağı + tether interneti (relay)"**, **"🔒 VPN üzerinden uzak ağ"**. Karta dokun →
  ilgili profil yüklenir, uplink seçilir, tek "Başlat".
- **Uygulama ipucu:** Şablon = önceden doldurulmuş `Profile`. `config.json`'da yerleşik
  şablonlar (salt-okunur) + kullanıcı "Bunu şablon yap" ile kendi profilinden şablon üretir.
  `docs/TODO.md D` bölümündeki fikrin ürünleştirilmiş hâli.

### U-3 🔴 "İzolasyon kanıtı" güven rozeti (Trust Badge) — S ⭐ AYIRT EDİCİ ÖZELLİK
- **Kullanıcı değeri:** Ürünün tüm satış vaadi "yapısal izolasyon"; bunu kullanıcıya **kanıt
  olarak** gösterirsen güven ve algılanan değer katlanır. Rakip "VPN" araçlarından farkı budur.
- **Deneyim:** Hero kartında yeşil bir rozet: **"✅ İzole — işin PC'nin hattından çıkmıyor"**.
  Üstüne gelince/dokununca sade kanıt: *"İzole dış IP: 203.0.113.42 · PC'nin IP'si: 88.x.x.x —
  farklı, yani ayrı hattasın."* İkisi eşitse **kırmızı uyarı**: "Dikkat: izolasyon doğrulanamadı".
- **Uygulama ipucu:** İzole alanın `public_ip`'i zaten var (`state.public_ip`). Host'un dış
  IP'sini de bir kez öğren (host bağlamında `curl`), ikisini karşılaştır. Eşitlik = kırmızı
  bayrak. Ampirik kanıt (U hedefi) Bölüm 8'deki tcpdump doğrulamasının kullanıcıya görünen hâli.

### U-4 🔴 Akıllı uplink otomatik algılama & öneri — S
- **Kullanıcı değeri:** Kullanıcı "usb0 mu wlan0 mı?" bilmek zorunda kalmaz; telefonu takınca
  ürün onu tanır ve önerir.
- **Deneyim:** Telefon USB tether takıldığında panel *"📱 Telefon bağlantısı algılandı — bununla
  başlat?"* diye canlı önerir. Arayüzler teknik ad yerine dostça etiketle: "Telefon (USB)",
  "WiFi", "Kablolu".
- **Uygulama ipucu:** `system._classify` zaten usb/wifi/ethernet ayırıyor. UI'de kind→dostça-ad
  eşlemesi + yeni cihaz belirince (interface listesi değişince) toast. `enx*/rndis` = telefon.

### U-5 🟡 Masaüstü bildirimleri (panel kapalıyken bile) — S
- **Kullanıcı değeri:** Kullanıcı panele bakmak zorunda kalmadan önemli olaylardan haberdar olur:
  "tether koptu", "yeniden bağlanıldı", "VPN düştü", "kota %80".
- **Deneyim:** Kritik olaylarda `notify-send` bildirimi (başlatıcıda zaten `note()` var).
  Bildirim tıklanınca panel açılır.
- **Uygulama ipucu:** Watchdog olayları (`state.events`) zaten seviyeli (`warn`/`error`).
  Daemon tarafında seçili olaylarda `notify-send` çağır (gerçek kullanıcı DBUS oturumuna;
  `apps._gui_env` DBUS adresini zaten biliyor). H-2 (sistem tepsisi) ile birleşir.

### U-6 🟡 Görsel veri kullanım paneli — S (H-1 üstüne kullanıcı katmanı)
- **Kullanıcı değeri:** "Bu ay tether'den ne kadar harcadım?" — ölçülü kota için birinci
  derece fayda; kullanıcı faturasını korur.
- **Deneyim:** Hero'da canlı sayaç (↓ indirilen / ↑ yüklenen) + oturum toplamı + basit çubuk.
  İsteğe bağlı aylık kota girilirse ilerleme çubuğu + %80'de uyarı (U-5).
- **Uygulama ipucu:** Veri kaynağı H-1 (`/proc/net/dev`). UI tarafı hafif SVG/CSS çubuk;
  harici grafik kütüphanesi gerekmez (stdlib/bağımsızlık ilkesi korunur).

### U-7 🟡 Bağlantı hız/kalite testi (izole alandan) — S
- **Kullanıcı değeri:** "İzole hattım yeterince hızlı mı?" sorusuna tek tıkla yanıt; kullanıcıyı
  terminale göndermez.
- **Deneyim:** "Hızı test et" düğmesi → izole alandan küçük bir indirme + ping/jitter → yeşil/
  sarı/kırmızı kalite göstergesi.
- **Uygulama ipucu:** İzole ns içinde küçük bir dosya indir (`curl` zaten kullanılıyor) + gecikme
  ölç. Sonuç `state`'e yaz; UI göster. Ölçülü uplink'te "az veri kullanır" notu.

### U-8 🟡 Oturum zaman çizelgesi / geçmiş — S
- **Kullanıcı değeri:** "Bugün kaç kez koptu, ne zaman relay açtım?" — güven ve şeffaflık;
  sorun giderirken kullanıcı kendi geçmişini görür.
- **Deneyim:** Olay akışının zenginleştirilmiş, ikonlu, filtrelenebilir (bilgi/uyarı/hata) hâli;
  "logları/tanı paketini indir" (H-8) ile bütünleşir.
- **Uygulama ipucu:** `state.events` zaten var (son 100). UI'de filtre çipleri + ikon + indir.

### U-9 🟡 "Duraklat" ve "geçici izin" — S
- **Kullanıcı değeri:** Kullanıcı her şeyi durdurup baştan kurmak zorunda kalmadan anlık esneklik
  kazanır: "5 dakikalığına kurum ağına izin ver, sonra otomatik kapansın".
- **Deneyim:** Relay/VPN yanında "geçici" seçeneği (süreli). Ayrıca izole oturumu tümüyle
  kapatmadan "duraklat" (uplink'i namespace'te tut, app'leri koru).
- **Uygulama ipucu:** Relay/VPN için `enable` çağrısına opsiyonel süre (`threading.Timer` ile
  otomatik `disable`). Mevcut `enable_relay`/`disable_relay` altyapısı yeterli.

### U-10 🟡 Hatada rehberli kurtarma (Guided Recovery) — S
- **Kullanıcı değeri:** Teknik hata mesajı yerine *ne olduğu + tek düğmeyle çözüm*; kullanıcı
  terminale düşmeden kendi kendine kurtulur. Destek yükünü azaltır.
- **Deneyim:** "WiFi'ye bağlanılamadı" → "SSID/parolayı kontrol et [Tekrar dene]". "DHCP alınamadı"
  → "[Yeniden bağlan]". "openvpn kurulu değil" → tam kurulum komutu + kopyala düğmesi.
- **Uygulama ipucu:** `doctor` mantığı zaten eksik araç → çözüm komutu üretiyor; bunu API'ye
  (`/api/doctor`) taşı ve hata durumlarına eyleme dönük "sonraki adım" kodu iliştir. G-4 (genel
  hata mesajı) ile birlikte: kullanıcıya kategori + öneri, loga ayrıntı.

### U-11 🟡 Boş/yükleniyor durumları, mikro-etkileşim, erişilebilirlik — S
- **Kullanıcı değeri:** Ürünün "bitmiş, cilalı" hissini veren detaylar; her ekran düşünülmüş.
- **Deneyim:** İskelet (skeleton) yükleniyor animasyonları, düğmelerde işlem-sırası spinner'ı,
  başarı için hafif onay animasyonu, boş listelerde açıklayıcı yönlendirme. `alert()` yerine
  şık toast/inline hata. Klavye erişimi + `aria-label` + kontrast (WCAG AA).
- **Uygulama ipucu:** Saf CSS/JS (mevcut yaklaşım). `alert("Hata: ...")` (app.js:270, 335)
  → tasarlanmış toast bileşeni. `busy` durumu zaten var; düğme spinner'ına bağla.

### U-12 🟢 Yedekle / geri yükle + profil paylaşımı — S
- **Kullanıcı değeri:** Kullanıcı kurulumunu kaybetmez, yeni makineye taşır, ekip arkadaşıyla
  paylaşır. "Kurumsal ürün" hissi.
- **Deneyim:** "Ayarları dışa aktar" (JSON indir) / "içe aktar". Profil için paylaşılabilir link
  ya da QR (parolalar hariç). Yeni cihazda içe aktar → hazır.
- **Uygulama ipucu:** `Settings.save/load` zaten JSON. `/api/config/export|import` uçları;
  parola alanlarını dışa aktarımdan çıkar (G-3 maskeleme ile uyumlu).

### U-13 🟢 Komut paleti & klavye kısayolları — S
- **Kullanıcı değeri:** Güç kullanıcıları için hız; profesyonel araç algısı (Cmd+K deneyimi).
- **Deneyim:** `Ctrl/Cmd+K` → "Başlat / Durdur / Relay aç / Uplink değiştir / VPN bağlan" arama.
  Kısayollar: Başlat, Durdur, Yeniden bağlan.
- **Uygulama ipucu:** Küçük istemci-tarafı komut listesi; mevcut düğme aksiyonlarını çağırır.

### U-14 🟢 Zengin uygulama kataloğu + simgeler — S
- **Kullanıcı değeri:** Uygulama seçimi metin çip yerine tanınır simgelerle; daha davetkâr.
- **Deneyim:** Bilinen uygulamalar (Chrome/Firefox/VS Code…) gerçek ikonlarıyla; "yüklü değil"
  olanlar için "kur" ipucu. Kullanıcı kendi komutunu da ekleyebilir ("Özel uygulama +").
- **Uygulama ipucu:** `apps.KNOWN_APPS`'i {ad, dostça-ad, ikon, kategori} meta ile zenginleştir.
  İkonlar tema ikonlarından ya da gömülü SVG.

### U-15 🟢 Tema (karanlık/aydınlık) + i18n (TR/EN) — S
- **Kullanıcı değeri:** Erişim ve konfor; uluslararası/kurumsal kullanım (H-11'in ürün yüzü).
- **Deneyim:** Sistem temasını izleyen + elle geçilebilen karanlık/aydınlık; dil anahtarı.
- **Uygulama ipucu:** CSS değişkenleri zaten var (`style.css`); `prefers-color-scheme` + toggle.
  UI string'lerini sözlüğe çıkar.

---

## 10. BU AVANTAJLARI MÜMKÜN KILAN MİMARİ (Neden hızlı ve sağlam ürünleşir)

Yukarıdaki ürün özelliklerinin çoğu **küçük bir efor** ile eklenebilir; çünkü mevcut mimari
bunları doğal olarak besliyor. Uygulayan tarafın bu kaldıraçları bilmesi önemli:

- **Olay-akışı modeli (`state.events`) hazır.** U-5 bildirimleri, U-8 zaman çizelgesi ve
  U-10 rehberli kurtarma; hepsi mevcut seviyeli olay akışının üzerine oturur — yeni altyapı yok.
- **Durum tek noktadan yayınlanıyor (`RuntimeState` → `/api/status`).** U-3 güven rozeti,
  U-6 veri sayacı, U-7 hız testi; sadece `state`'e alan ekleyip UI'de göstermek yeterli.
  (P-1 kilit iyileştirmesi bu canlı göstergeleri akıcı kılar — ürün hissinin ön koşulu.)
- **Profil soyutlaması (`Profile`) zaten kalıcı & serileştirilebilir.** U-1 onboarding,
  U-2 şablonlar, U-12 yedekle/paylaş; hepsi `Profile`/`Settings` JSON'u üzerinde çalışır.
- **Sıfır harici Python bağımlılığı + bağımsız web UI.** Ürün cilası (skeleton, toast, tema,
  grafik) **saf CSS/JS** ile eklenir; dağıtım tek repo, kurulum sürtünmesi düşük — profesyonel
  ürünün "indir-çalıştır" hissi korunur.
- **`dry-run` her katmanda.** Tüm UX akışları (sihirbaz, şablon, senaryolar) root/donanım
  olmadan uçtan uca prova edilebilir → hızlı, güvenli iterasyon; demo/ekran görüntüsü kolay.
- **`doctor` teşhis mantığı hazır.** U-10 rehberli kurtarma ve onboarding ön-kontrolleri
  bu mevcut "eksik araç → çözüm komutu" motorundan beslenir.
- **Yerel-öncelik & gizlilik.** Her şey `127.0.0.1` + kullanıcı makinesinde; U-3'teki "kanıt",
  U-6'daki sayaç gibi özellikler **kullanıcı verisini dışarı çıkarmadan** güven verir — bu,
  gizlilik-duyarlı kullanıcıya doğrudan pazarlanabilir bir ürün avantajıdır.

**Sonuç:** Çekirdek (Model B izolasyon) zaten profesyonel; eksik olan **son kullanıcı katmanı**.
Bölüm 9'daki U-1…U-4 + U-3 güven rozeti tek bir "Sprint UX" ile eklenirse ürün, teknik bir
araçtan *"tak-çalıştır, güvenini gösteren, cilalı bir gizlilik ürünü"ne* dönüşür — hem de mevcut
mimariyi hiç zorlamadan.

---

*Bu doküman canlıdır: her madde bağımsız uygulanabilir. Bir maddeyi tamamlayınca ilgili
`docs/TODO.md` / `docs/STATUS.md` girişini güncelle ve kabul kriterini işaretle.*
