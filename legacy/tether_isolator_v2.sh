#!/bin/bash

# --- AYARLAR ---
NAMESPACE="chrome_alani"
CHROME_TEMP="/tmp/chrome-usb-session"

# Renkler
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}--- Tethered App Isolator (v2) ---${NC}"

# Root yetkisi kontrolü
if [ "$EUID" -ne 0 ]; then 
  echo -e "${RED}Lütfen bu scripti 'sudo' ile çalıştırın.${NC}"
  echo "Örnek: sudo $0"
  exit 1
fi

# Gerçek kullanıcıyı bul
REAL_USER=$SUDO_USER
[ -z "$REAL_USER" ] && REAL_USER=$(whoami)

# --- TEMİZLİK FONKSİYONU ---
cleanup() {
    echo -e "\n${YELLOW}Sistem temizleniyor... Lütfen bekleyin.${NC}"
    ip netns exec $NAMESPACE dhcpcd -x $ARAYUZ > /dev/null 2>&1
    # Arayüzü geri getir
    ip netns exec $NAMESPACE ip link set $ARAYUZ netns 1 > /dev/null 2>&1
    ip netns del $NAMESPACE > /dev/null 2>&1
    rm -rf /etc/netns/$NAMESPACE
    echo -e "${GREEN}Her şey eski haline döndü. Güle güle!${NC}"
    exit 0
}

# Sinyalleri yakala (Ctrl+C, veya kill)
trap cleanup SIGINT SIGTERM

# --- 1. ARAYÜZ SEÇİMİ ---
echo -e "${YELLOW}Ağ arayüzleri taranıyor...${NC}"
INTERFACES=($(/sbin/ip -o link show 2>/dev/null | awk '{print $2}' | tr -d ':' | grep -v "lo" | grep -v "^\s*$"))

if [ ${#INTERFACES[@]} -eq 0 ]; then
    echo -e "${RED}HATA: Hiçbir ağ arayüzü bulunamadı!${NC}"
    echo -e "${YELLOW}--- Hata Ayıklama Bilgisi ---${NC}"
    /sbin/ip -o link show
    echo -e "${YELLOW}-----------------------------${NC}"
    exit 1
fi

echo "Lütfen kullanmak istediğiniz USB/Telefon arayüzünü seçin:"
for i in "${!INTERFACES[@]}"; do
    echo "$((i+1))) ${INTERFACES[$i]}"
done

read -p "Seçiminiz (1-${#INTERFACES[@]}): " INT_CHOICE
ARAYUZ=${INTERFACES[$((INT_CHOICE-1))]}

if [ -z "$ARAYUZ" ]; then
    echo -e "${RED}Geçersiz seçim!${NC}"
    exit 1
fi

# --- 2. UYGULAMA SEÇİMİ ---
echo -e "${YELLOW}Yüklü uygulamalar taranıyor...${NC}"
# terminator ve vscode eklendi
APP_LIST=("google-chrome" "google-chrome-stable" "firefox" "opera" "brave-browser" "chromium-browser" "terminator" "code")
AVAILABLE_APPS=()

for b in "${APP_LIST[@]}"; do
    if command -v "$b" > /dev/null 2>&1; then
        AVAILABLE_APPS+=("$b")
    fi
done

if [ ${#AVAILABLE_APPS[@]} -eq 0 ]; then
    echo -e "${RED}HATA: Desteklenen bir uygulama bulunamadı!${NC}"
    exit 1
fi

echo "Lütfen başlatmak istediğiniz uygulamaları seçin (Birden fazla seçmek için aralarında boşluk bırakın, örn: 1 3 4):"
for i in "${!AVAILABLE_APPS[@]}"; do
    echo "$((i+1))) ${AVAILABLE_APPS[$i]}"
done

read -p "Seçiminiz: " APP_CHOICES

SELECTED_APPS=()
for choice in $APP_CHOICES; do
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#AVAILABLE_APPS[@]}" ]; then
        SELECTED_APPS+=("${AVAILABLE_APPS[$((choice-1))]}")
    else
        echo -e "${RED}Geçersiz seçim atlandı: $choice${NC}"
    fi
done

if [ ${#SELECTED_APPS[@]} -eq 0 ]; then
    echo -e "${RED}Geçersiz seçim! Hiçbir geçerli uygulama seçilmedi.${NC}"
    exit 1
fi

# --- 3. ESKİ KALINTI TEMİZLİĞİ ---
if ip netns list | grep -q "$NAMESPACE"; then
    echo "Eski alan temizleniyor..."
    ip netns exec $NAMESPACE dhcpcd -x $ARAYUZ > /dev/null 2>&1
    ip netns del $NAMESPACE > /dev/null 2>&1
fi

# --- 4. KURULUM ---
echo -e "${YELLOW}Sanal oda oluşturuluyor ($ARAYUZ -> $NAMESPACE)...${NC}"
ip netns add $NAMESPACE
ip netns exec $NAMESPACE ip link set dev lo up

# DNS Ayarı (Google DNS)
mkdir -p /etc/netns/$NAMESPACE
echo "nameserver 8.8.8.8" > /etc/netns/$NAMESPACE/resolv.conf
echo "nameserver 1.1.1.1" >> /etc/netns/$NAMESPACE/resolv.conf

# --- 5. TAŞIMA VE BAĞLANTI ---
echo "USB interneti odaya taşınıyor..."
ip link set $ARAYUZ netns $NAMESPACE
ip netns exec $NAMESPACE ip link set dev $ARAYUZ up

echo "IP adresi alınıyor (dhcpcd kullanılıyor)..."
# -1 parametresi kaldırıldı, böylece dhcpcd arka planda çalışarak kira süresini (lease) yenilemeye devam eder
ip netns exec $NAMESPACE dhcpcd -w $ARAYUZ

# Bağlantı testi
if ip netns exec $NAMESPACE ping -c 1 8.8.8.8 > /dev/null 2>&1; then
    echo -e "${GREEN}Bağlantı Başarılı!${NC}"
else
    echo -e "${RED}UYARI: İnternet bağlantısı kurulamadı ama uygulamalar yine de açılacak.${NC}"
fi

# --- 6. ÇALIŞTIRMA ---
echo -e "${GREEN}Seçilen uygulamalar başlatılıyor...${NC}"

# Chrome tabanlılar için parametreler
CHROME_FLAGS="--user-data-dir=$CHROME_TEMP --no-first-run"

for APP in "${SELECTED_APPS[@]}"; do
    case "$APP" in
        google-chrome*|chromium*|brave*)
            ip netns exec $NAMESPACE sudo -u $REAL_USER "$APP" $CHROME_FLAGS &
            ;;
        firefox)
            FIREFOX_TEMP="/tmp/firefox-usb-session"
            mkdir -p "$FIREFOX_TEMP"
            ip netns exec $NAMESPACE sudo -u $REAL_USER "$APP" --profile "$FIREFOX_TEMP" --no-remote &
            ;;
        code)
            # VS Code electron tabanlıdır, var olan ana instance'a bağlanmaması için ayrı klasör:
            VSCODE_TEMP="/tmp/vscode-usb-session"
            ip netns exec $NAMESPACE sudo -u $REAL_USER "$APP" --user-data-dir="$VSCODE_TEMP" &
            ;;

        terminator)
            # -u (--no-dbus) bayrağı, ana sistemde çalışan terminator varsa ona bağlanmak 
            # yerine tamamen yeni ve bağımsız (bu ağa özel) bir pencere açmasını sağlar.
            ip netns exec $NAMESPACE sudo -u $REAL_USER "$APP" -u &
            ;;
        *)
            ip netns exec $NAMESPACE sudo -u $REAL_USER "$APP" &
            ;;
    esac
done

# --- 7. BEKLEME VE KAPANIŞ ---
echo -e "${YELLOW}Tüm uygulamaların kapanması bekleniyor... İşiniz bittiğinde terminali kapatabilir veya Ctrl+C yapabilirsiniz.${NC}"
wait

# Her şey normal şekilde kapandığında temizlik yap
cleanup
