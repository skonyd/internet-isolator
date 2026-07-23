"""Tether Isolator — uygulamaları izole bir ağ alanında çalıştıran araç.

Çekirdek mantık: seçilen bir uplink arayüzü (USB tether / WiFi) bir Linux
network namespace'inin içine *fiziksel olarak* taşınır. Böylece o alandaki
uygulamalar yalnızca o bağlantıdan çıkar; host'un kablolu bağlantısıyla
ortak hiçbir kernel yığını paylaşmadıkları için yapısal (sızdırmaz) izolasyon
sağlanır.

Modüller:
    system      — kabuk komutu çalıştırma, arayüz listeleme, yetki kontrolü
    config      — profil/ayar yönetimi (JSON)
    state       — çalışma anı durum bilgisi
    engine      — namespace kurulum/yıkım, arayüz taşıma, DHCP, durum
    relay       — isteğe bağlı veth yan-kanalı (host erişimi)
    apps        — izole alanda uygulama başlatma (kalıcı profiller)
    manager     — orkestratör + watchdog (dayanıklılık)
    server      — yerel HTTP API + web arayüzü
    cli         — komut satırı arabirimi
"""

__version__ = "0.1.0"
__app_name__ = "Tether Isolator"
