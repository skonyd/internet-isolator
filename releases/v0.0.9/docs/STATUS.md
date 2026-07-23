# Proje Durumu (STATUS.md)

**Sürüm:** 3.1.0 · **Tarih:** 2026-07-01 · **Aşama:** Faz 1–2 çalışır, WiFi/relay/VPN gerçek donanımda doğrulandı

## Özet
Çekirdek izolasyon, otomatik yeniden bağlanma, sadeleştirilmiş relay, izole
alanda OpenVPN istemcisi ve web paneli çalışır durumda. WiFi uplink, relay
(kurum ağı erişimi) ve VPN gerçek donanımda çalıştırılıp doğrulandı.

## Ne çalışıyor (gerçek donanımda doğrulandı — 2026-07-01)
- ✅ WiFi uplink (PHY taşıma + wpa_supplicant + DHCP): izole alan `192.168.1.141`
  ile internete çıkıyor; VPN sunucusuna (176.33.72.103:1194) erişiliyor.
- ✅ **Relay (tek mod)**: açılınca host'un ulaştığı ağlar izole alana aynalanıyor.
  İzole alandan kurum sunucusu `10.0.15.127` → ping/SSH(22)/HTTPS(443) ve kurum
  DNS erişildi. İnternet tether'de kalıyor (doğru izolasyon). Kurum ağı host'un
  `tun0`/`tun1` OpenVPN tünelleri üzerinden gidiyor; relay + NAT bunu köprülüyor.
- ✅ **Relay "Ek hedefler"**: kurum-only dış siteler (ör. `mail.havelsan.com.tr`)
  için host çıkışından geçiş. (Kullanıcı tarafında: host'un o siteye erişebildiği
  teyit edilmeli.)
- ✅ **İzole alanda OpenVPN** (garageliman): `--disable-dco` + `--data-ciphers`
  düzeltmeleriyle bağlanıyor; ns içinde `tun0` (10.67.11.x) açıldı.
- ✅ Oturum devralma (resume), orphan'sız durdurma, DNS (systemd-resolved) düzeltmesi.

## Ne çalışıyor (dry-run + testlerle doğrulandı)
- ✅ 60 birim/entegrasyon testi geçiyor (`python3 -m unittest discover -s tests`).
- ✅ Daemon + `/api/*` yaşam döngüsü, statik web sunumu, path-traversal koruması.

## Devam eden / planlanan
- ⏳ **A↔B köprüsü**: garageliman VPN ağı ↔ kurum LAN'ı laptop üzerinden (Spark'taki
  LLM agent → kurum sunucuları). ns forwarding + iki yönlü NAT; uzak uç dönüş
  rotalarına bağlı.
- ⏳ Kill-switch, trafik sayacı, sistem tepsisi, IPv6, `.deb`/`pipx` paketleme.

## Güvenlik notu
Paylaşılan kurum kimlik bilgileri (root/DB parolaları) **döndürülmeli**. Uygulama
SSH/sudo parolası girmez; bu adımları kullanıcı yapar.

## Sonraki adım
A↔B köprüsünü kurup Spark'tan kurum sunucusuna erişimi doğrulamak. Ayrıntı:
[`TASK.md`](TASK.md).
