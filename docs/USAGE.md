# Kullanım Kılavuzu

## Gereksinimler

| Amaç | Paket |
|---|---|
| Çekirdek | Python 3.10+, `iproute2` (ip), `dhcpcd` veya `udhcpc` |
| WiFi uplink | `wpa_supplicant`, `iw` |
| Relay | `nftables` (yoksa `iptables`) |
| VPN (izole alanda) | `openvpn` (2.6+; `--disable-dco` desteği) |
| Dış IP gösterimi | `curl` veya `wget` |

Python tarafında harici bağımlılık yoktur.

## Kurulum

```bash
git clone <repo> tether-isolator && cd tether-isolator
chmod +x bin/tetherctl
# (opsiyonel) PATH'e ekleyin:
sudo ln -s "$PWD/bin/tetherctl" /usr/local/bin/tetherctl
```

### systemd servisi (opsiyonel, kalıcı daemon)

```bash
sudo cp -r . /opt/tether-isolator
sudo cp systemd/tether-isolatord.service /etc/systemd/system/
# Birim dosyasındaki PYTHONPATH yolunu /opt/tether-isolator olarak ayarlayın
sudo systemctl daemon-reload
sudo systemctl enable --now tether-isolatord
```

## Çift tıkla başlat (masaüstü uygulaması)

```bash
./install.sh
```

`install.sh` (root gerektirmez):
- Başlatıcıları çalıştırılabilir yapar.
- `~/.local/share/applications/tether-isolator.desktop` üretir → uygulama
  menüsünde "Tether Isolator" görünür.
- İkonu yerleştirir ve masaüstüne çift tıklanan bir kopya koyar.

Çift tıklayınca `bin/tether-isolator-app` çalışır: panel zaten açıksa tarayıcıyı
açar; değilse `pkexec` ile (grafik parola penceresi) daemon'u root olarak
başlatır, GUI ortamını (DISPLAY/XAUTHORITY) içeri taşır ve paneli açar.
Kapatmak için panelin sağ üstündeki **Çıkış** düğmesini kullanın.

Kaldırma:
```bash
rm ~/.local/share/applications/tether-isolator.desktop ~/Desktop/tether-isolator.desktop
```

## Web paneli (terminalden)

```bash
sudo tetherctl gui              # tarayıcıyı açar
sudo tetherctl gui --no-browser # yalnızca sunucu
```

Panelde:
1. **Profil** seçin (ör. `default`).
2. **Uplink** seçin (telefon USB tether veya WiFi arayüzü).
3. Başlatılacak **uygulamaları** işaretleyin.
4. **Başlat**'a basın. Durum kartı canlı olarak güncellenir.
5. Gerektiğinde **Relay** anahtarını açın: izole uygulamalar bu makinenin
   ulaştığı kurum ağlarına erişir; internet tether'de kalır. Kurum çıkışından
   erişilen ama tether'den engelli dış siteler için **Ek hedefler** alanına
   IP/CIDR/alan adı (ör. `mail.havelsan.com.tr`) yazın.
6. Gerektiğinde **VPN** kutusuna `.ovpn` yolu girip açın (izole alan içinde,
   tether üzerinden uzak ağa şifreli bağlantı). Relay ile aynı anda çalışır.
7. İşiniz bitince **Durdur** → her şey eski haline döner.

## Komut satırı

```bash
tetherctl interfaces                 # arayüzleri listele
tetherctl apps                       # yüklü uygulamaları listele
sudo tetherctl start --uplink usb0 --app google-chrome --app firefox
sudo tetherctl status                # (veya --json)
sudo tetherctl stop
sudo tetherctl interactive           # orijinal scriptteki soru-cevap akışı
```

### Test (donanım/yetki olmadan)

```bash
tetherctl --dry-run gui --no-browser
tetherctl --dry-run interfaces
```

`--dry-run`'da hiçbir `ip`/`dhcpcd` komutu gerçekten çalışmaz; mantık ve
arayüz güvenle denenebilir.

## Profiller ve kalıcılık

- Ayar dosyası: `~/.config/tether-isolator/config.json`
- Kalıcı uygulama verileri: `~/.local/share/tether-isolator/profiles/<profil>/`
  → tarayıcı oturumları/oturum açmaları silinmez.

Yeni profil oluşturmak için config dosyasını düzenleyebilir veya panelden
profil kaydedebilirsiniz (API: `POST /api/profile`).

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| "root gerekir" | `sudo` ile çalıştırın (veya systemd servisi). |
| Uplink listede yok | Telefonu USB tethering moduna alın; `tetherctl interfaces` ile kontrol edin. |
| İnternet "hayır" | Telefon hattının verisi açık mı? Panelde olay akışına bakın. |
| Panel açılmıyor | Port çakışması: `config.json`'da `http_port` değiştirin. |
| Uygulama açılmıyor | `DISPLAY` ortamı: `sudo` yerine systemd servisi veya `sudo -E` deneyin. |
| WiFi uplink bağlanmıyor | `wpa_supplicant`/`iw` kurulu mu? PHY taşıma gerçek donanım ister. |

## REST API (özet)

| Yöntem | Yol | Açıklama |
|---|---|---|
| GET | `/api/status` | tam durum (state + arayüzler + profiller) |
| GET | `/api/interfaces` | host arayüzleri |
| POST | `/api/start` | `{profile, uplink, apps[]}` |
| POST | `/api/stop` | oturumu durdur |
| POST | `/api/relay` | `{enabled: bool}` |
| POST | `/api/profile` | `{profile: {...}}` oluştur/güncelle |
