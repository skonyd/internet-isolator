#!/bin/bash

# --- AYARLAR ---
NAMESPACE="chrome_alani"
CHROME_TEMP="/tmp/chrome-usb-session"

# Renkler
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}--- Tethered Browser Isolator (v2.2) ---${NC}"

# Root yetkisi kontrolü
if [ "$EUID" -ne 0 ]; then 
  echo -e "${RED}Lütfen bu scripti 'sudo' ile çalıştırın.${NC}"
  echo "Örnek: sudo $0"
  exit 1
fi

# Gerçek kullanıcıyı bul
REAL_USER=$SUDO_USER
[ -z "$REAL_USER" ] && REAL_USER=$(whoami)

# --- 1. ARAYÜZ SEÇİMİ ---
echo -e "${YELLOW}Ağ arayüzleri taranıyor...${NC}"
INTERFACES=($(ip -o link show | awk -F': ' '{print $2}' | grep -v "lo"))

if [ ${#INTERFACES[@]} -eq 0 ]; then
    echo -e "${RED}HATA: Hiçbir ağ arayüzü bulunamadı!${NC}"
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

# --- 2. TARAYICI SEÇİMİ ---
echo -e "${YELLOW}Yüklü tarayıcılar taranıyor...${NC}"
BROWSER_LIST=("google-chrome" "google-chrome-stable" "firefox" "opera" "brave-browser" "chromium-browser")
AVAILABLE_BROWSERS=()

for b in "${BROWSER_LIST[@]}"; do
    if command -v "$b" > /dev/null 2>&1; then
        AVAILABLE_BROWSERS+=("$b")
    fi
done

if [ ${#AVAILABLE_BROWSERS[@]} -eq 0 ]; then
    echo -e "${RED}HATA: Desteklenen bir tarayıcı bulunamadı!${NC}"
    exit 1
fi

echo "Lütfen başlatmak istediğiniz tarayıcıyı seçin:"
for i in "${!AVAILABLE_BROWSERS[@]}"; do
    echo "$((i+1))) ${AVAILABLE_BROWSERS[$i]}"
done

read -p "Seçiminiz (1-${#AVAILABLE_BROWSERS[@]}): " BR_CHOICE
SELECTED_BROWSER=${AVAILABLE_BROWSERS[$((BR_CHOICE-1))]}

if [ -z "$SELECTED_BROWSER" ]; then
    echo -e "${RED}Geçersiz seçim!${NC}"
    exit 1
fi

# --- 3. TEMİZLİK ---
if ip netns list | grep -q "$NAMESPACE"; then
    echo "Eski alan temizleniyor..."
    ip netns exec $NAMESPACE dhcpcd -x $ARAYUZ > /dev/null 2>&1
    ip netns del $NAMESPACE
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
ip netns exec $NAMESPACE dhcpcd -1 -w $ARAYUZ

# Bağlantı testi
if ip netns exec $NAMESPACE ping -c 1 8.8.8.8 > /dev/null 2>&1; then
    echo -e "${GREEN}Bağlantı Başarılı!${NC}"
else
    echo -e "${RED}UYARI: İnternet bağlantısı kurulamadı ama tarayıcı yine de açılacak.${NC}"
fi

# --- 6. ÇALIŞTIRMA ---
echo -e "${GREEN}$SELECTED_BROWSER başlatılıyor...${NC}"

# Chrome tabanlılar için parametreler
CHROME_FLAGS="--user-data-dir=$CHROME_TEMP --no-first-run"

case "$SELECTED_BROWSER" in
    google-chrome*|chromium*|brave*)
        ip netns exec $NAMESPACE sudo -u $REAL_USER "$SELECTED_BROWSER" $CHROME_FLAGS
        ;;
    firefox)
        FIREFOX_TEMP="/tmp/firefox-usb-session"
        mkdir -p "$FIREFOX_TEMP"
        ip netns exec $NAMESPACE sudo -u $REAL_USER "$SELECTED_BROWSER" --profile "$FIREFOX_TEMP" --no-remote
        ;;
    *)
        ip netns exec $NAMESPACE sudo -u $REAL_USER "$SELECTED_BROWSER"
        ;;
esac

# --- 7. KAPANIŞ ---
echo -e "${YELLOW}Tarayıcı kapatıldı. Sistem temizleniyor...${NC}"
ip netns exec $NAMESPACE dhcpcd -x $ARAYUZ > /dev/null 2>&1

# Arayüzü geri getir
ip netns exec $NAMESPACE ip link set $ARAYUZ netns 1

ip netns del $NAMESPACE
rm -rf /etc/netns/$NAMESPACE

echo -e "${GREEN}Her şey eski haline döndü. Güle güle!${NC}"
