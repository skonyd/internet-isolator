# Mimari

## 1. İzolasyon modeli (Model B — fiziksel taşıma)

Tether Isolator, izolasyonu **politikayla değil yapısal olarak** sağlar.

```
                    ┌─────────────────────── HOST (ana namespace) ───────────────────────┐
                    │                                                                     │
   internet ◀── eth0 (kablolu)  ── host yönlendirme tablosu ── (kullanıcının normal ağı)  │
                    │                                                                     │
                    │   ┌──────────── tether_zone (network namespace) ───────────┐        │
                    │   │                                                         │        │
   internet ◀───────┼───┤ usb0 (FİZİKSEL olarak buraya taşındı)                   │        │
   (telefon)        │   │   • kendi IP'si (DHCP)                                  │        │
                    │   │   • kendi default route'u                               │        │
                    │   │   • kendi resolv.conf'u (/etc/netns/tether_zone)        │        │
                    │   │                                                         │        │
                    │   │   [ Chrome ] [ Firefox ] ...  ← yalnızca usb0'ı görür   │        │
                    │   └─────────────────────────────────────────────────────────┘       │
                    └─────────────────────────────────────────────────────────────────────┘
```

**Anahtar nokta:** `usb0` arayüzü `ip link set usb0 netns tether_zone` ile
host'tan **tamamen çıkarılır**. Namespace'in kendi yönlendirme tablosu, kendi
DNS'i ve kendi fiziksel kapısı (usb0) vardır. Host'un `eth0`'ı bambaşka bir
kernel ağ yığınındadır. İkisi arasında köprü/rota/NAT **yoktur** → trafiğin
kablolu bağlantıya sızması fiziksel olarak imkânsızdır.

Karşılaştırma — `veth + NAT` (Model A) yaklaşımında paketler host'un IP
yığınından geçer; egress'i doğru arayüze sabitlemek için policy routing +
firewall gerekir ve bir yapılandırma hatası sızıntıya yol açabilir. Bu proje
bilinçli olarak Model B'yi seçer.

## 2. Bileşenler ve sorumluluklar

```
┌──────────┐   HTTP/JSON    ┌────────────┐   çağrı    ┌──────────────────────────┐
│  Web UI  │ ◀────────────▶ │  server.py │ ◀────────▶ │       manager.py         │
│ (tarayıcı)│   (polling)   │ (daemon)   │            │  (orkestratör+watchdog)  │
└──────────┘                └────────────┘            └────────┬─────────────────┘
                                                               │
                          ┌────────────────────┬───────────────┼───────────────┐
                          ▼                    ▼               ▼               ▼
                     engine.py            relay.py          apps.py        state.py
                  (ip netns / dhcp)   (veth yan-kanal)   (uygulama        (runtime
                                                          başlatma)        durum)
```

- **engine.py** — namespace oluştur/sil, arayüz taşı, DHCP, WiFi, durum okuma.
  Tüm `ip netns` komutları burada; orijinal bash mantığının birebir karşılığı.
- **relay.py** — isteğe bağlı veth çiftiyle host erişimi (aşağıya bakın).
- **apps.py** — `ip netns exec ... runuser -u <kullanıcı>` ile uygulamayı
  gerçek kullanıcı kimliğiyle ve GUI ortamıyla başlatır; kalıcı profil dizini.
- **manager.py** — oturum yaşam döngüsü + **watchdog** (dayanıklılık çekirdeği).
- **server.py** — yalnızca `127.0.0.1`'e bağlanan stdlib HTTP daemon; JSON API
  + statik web sunumu.
- **state.py** — bellek içi + `/run/tether-isolator/state.json` durum kopyası.

## 3. Yetki modeli

Namespace işlemleri root gerektirir. Bu nedenle **daemon root olarak** çalışır
(systemd servisi veya `sudo`). Ancak:

- Daemon yalnızca `127.0.0.1`'i dinler (dışarıdan erişilemez).
- Başlatılan **uygulamalar root değil**, `runuser` ile gerçek kullanıcıya
  düşürülür; GUI ortam değişkenleri (DISPLAY/XAUTHORITY/WAYLAND) korunur.
- Config dosyaları yazıldıktan sonra kullanıcıya `chown` edilir.

## 4. Dayanıklılık (watchdog) — "kopunca uygulama ölmesin"

`manager.py` içindeki watchdog thread'i `watchdog_interval` saniyede bir:

1. Uplink hâlâ namespace içinde mi? **Hayır** ise → faz `reconnecting`.
   Arayüz host'ta yeniden belirdiyse (telefon yeniden takıldı) sessizce
   namespace'e geri taşınır ve DHCP yenilenir. **Uygulamalara dokunulmaz.**
2. Uplink yerinde ama internet yok mu? → DHCP yenilenir (lease/IP tazelenir).
3. Uygulama süreçleri hâlâ canlı mı? (PID kontrolü)

Neden uygulamalar hayatta kalır: uygulamalar **namespace'e** bağlıdır,
fiziksel arayüze değil. Arayüz gidip gelse de namespace yaşar; uygulamaların
soketleri kopabilir ama süreçleri yaşamaya devam eder (tarayıcılar otomatik
yeniden dener). `dhcpcd -w` arka planda kalıp kira süresini yeniler.

## 5. Relay (denetimli host erişimi)

İzolasyon her zaman açıktır. Relay, **opt-in** ve anlık bir kapıdır
(`relay.py`):

```
[tether_zone]  tisor-ns ◀══ veth ══▶ tisor-host  [host]
```

**Tek mod (sadeleştirildi, 2026-07-01):** relay açılınca host'un **ulaştığı tüm
ağlar (varsayılan/internet rotası HARİÇ)** izole alana aynalanır ve host IP
forwarding + `nftables`/`iptables` MASQUERADE ile host üzerinden geçirilir:

- İzole uygulamalar, bu makinenin ethernet/tünel üzerinden eriştiği her yere
  (kurum LAN'ı, kurum OpenVPN tünelleri `tun0`/`tun1` arkasındaki alt ağlar)
  ulaşır.
- **Varsayılan rotaya dokunulmaz → internet izole alanın kendi uplink'inde
  (tether/WiFi) kalır.** İzolasyonun özü korunur.
- **Ek hedefler** (`extra_targets`): kurum çıkışından erişilen ama tether'den
  engelli dış adresler (IP/CIDR/alan adı; ör. `mail.havelsan.com.tr`) da host
  üzerinden geçirilebilir. Alan adları açılışta A kaydına çözülür.
- **kapalı**: veth çifti + NAT kuralları tamamen silinir → yeniden tam izolasyon.

Kanalın kurulduğunun kanıtı: ns-ucu veth'in (`tisor-ns`) izole alan içinde
varlığı (`_ns_veth_present`). Eski `host-only`/`lan`/`full` kapsam modeli ve
`verify_hosts` doğrulaması kaldırıldı (kafa karıştırıyordu). Bu, eski
`host_relay/` dosya-kuyruğu köprüsünün modern, gerçek-ağ tabanlı halefidir.

## 6. Veri akışı (durum)

UI her 2 sn'de `/api/status` çeker. Daemon, watchdog her tikte güncellenen
bellek-içi `RuntimeState`'i döndürür. CLI `status` komutu, daemon olmasa bile
diskteki son kopyayı okur.

## 7. Bilinçli tasarım kararları

- **Harici Python bağımlılığı yok** — her yerde, ek kurulum olmadan çalışır.
- **stdlib HTTP + polling** (WebSocket yerine) — basitlik ve sıfır bağımlılık.
- **JSON config** — insan-okur, sürümlenebilir.
- **dry-run** her katmanda — donanımsız/yetkisiz test ve CI için.
