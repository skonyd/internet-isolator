# 🛡️ Tether Isolator

🇬🇧 [English version](README.en.md) | 🇹🇷 Türkçe (bu sayfa)

Belirli uygulamaları, **host'un kablolu bağlantısından yapısal olarak izole**
bir ağ alanında, yalnızca seçtiğiniz bir uplink (telefon USB tether / WiFi)
üzerinden çalıştıran araç. Apple-esinli bir web paneli ve otomatik yeniden
bağlanma ile birlikte gelir.

> Bu proje, `tether_isolator.sh` / `tether_isolator_v2.sh` bash scriptlerinin
> profesyonel, modüler ve test edilebilir bir Python yeniden yazımıdır.
> Orijinal scriptler `legacy/` altında korunmuştur ve çekirdek `ip netns`
> mantığı birebir aktarılmıştır.

---

## Neden?

Bir uygulamanın (ör. bir tarayıcı oturumu, bir otomasyon aracı) **kesinlikle**
PC'nin kablolu bağlantısını kullanmamasını, bunun yerine tamamen ayrı bir
internet yolundan (telefon hattı) çıkmasını istediğiniz durumlar vardır.

İki yol vardır:

| | İzolasyon | Sızma riski | Host'a etki |
|---|---|---|---|
| **veth + NAT** | Politikayla (firewall/rota) | Var (yanlış kural sızdırır) | Düşük |
| **Fiziksel taşıma (bu proje)** | **Yapısal (kernel seviyesi)** | **Yok** | Arayüz host'tan çıkar |

Bu proje **fiziksel taşımayı** (Model B) kullanır: uplink arayüzü tamamen
namespace'in içine taşınır; host'un `eth0`'ı ile ortak hiçbir kernel yığını
kalmaz. Sonuç: kurala bağlı değil, **garanti edilmiş** izolasyon.
Ayrıntı: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Öne çıkan özellikler

- 🔒 **Yapısal izolasyon** — uplink fiziksel olarak namespace içinde.
- ♻️ **Otomatik yeniden bağlanma** — telefon çıkıp takıldığında uygulamaları
  **öldürmeden** arayüzü geri alır, DHCP'yi yeniler (watchdog).
- 💾 **Kalıcı profiller** — tarayıcı oturumları silinmez; "iş"/"kişisel" gibi
  birden çok kayıtlı yapılandırma.
- 🖥️ **Apple-esinli web paneli** — canlı durum, dış IP, olay akışı.
- 🧩 **USB tether + WiFi** uplink desteği.
- ⌨️ **Scriptlenebilir CLI** + etkileşimli mod + systemd servisi.
- 🧪 **`--dry-run`** — root/donanım olmadan tüm mantığı test edin.

---

## Çift tıkla başlat (masaüstü uygulaması)

```bash
./install.sh        # root GEREKMEZ; menüye + masaüstüne kısayol ekler
```

Ardından **"Tether Isolator"**ı uygulama menüsünden veya masaüstünden çift
tıklayarak açın. İlk açılışta bir kez polkit (pkexec) parola penceresi çıkar;
sonra panel tarayıcıda açılır. Kapatmak için paneldeki **Çıkış** düğmesi.

## Hızlı başlangıç (terminal)

```bash
# Web panelini başlat (root gerekir — namespace işlemleri için)
sudo ./bin/tetherctl gui

# Yetki/donanım olmadan denemek için:
./bin/tetherctl --dry-run gui

# Komut satırından doğrudan:
sudo ./bin/tetherctl start --uplink usb0 --app google-chrome
sudo ./bin/tetherctl status
sudo ./bin/tetherctl stop
```

Panel `http://127.0.0.1:8787` adresinde açılır.

Kurulum, kullanım ve sorun giderme: [`docs/USAGE.md`](docs/USAGE.md).

---

## Proje yapısı

```
tether_isolator/      Python paketi (çekirdek motor + API + CLI)
  ├── system.py       OS yardımcıları (komut çalıştırma, arayüz keşfi)
  ├── config.py       profil/ayar yönetimi
  ├── state.py        çalışma anı durumu
  ├── engine.py       namespace yaşam döngüsü (Model B)
  ├── apps.py         izole alanda uygulama başlatma
  ├── manager.py      orkestratör + watchdog (dayanıklılık)
  ├── server.py       yerel HTTP API + web sunumu
  └── cli.py          komut satırı arabirimi
webui/                Apple-esinli web arayüzü (HTML/CSS/JS, bağımlılıksız)
systemd/              servis birimi
bin/tetherctl         başlatıcı
docs/                 mimari, özellikler, görev planı, durum, yapılacaklar (TODO)
legacy/               orijinal bash scriptleri (korunmuş)
```

Gereksinimler: Python 3.10+, `iproute2`, `dhcpcd` (veya `udhcpc`).
WiFi uplink için: `wpa_supplicant`, `iw`. Python tarafında **harici bağımlılık
yoktur** (yalnızca standart kütüphane).

## Lisans
Kişisel/araştırma kullanımı. Sorumluluk kullanıcıya aittir.
