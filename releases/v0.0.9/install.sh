#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Tether Isolator — masaüstü kurulumu
#
#  Yaptıkları (hepsi kullanıcı düzeyinde, root GEREKTİRMEZ):
#    • başlatıcıları çalıştırılabilir yapar
#    • bir .desktop dosyası üretir (mutlak yollarla) → uygulama menüsünde görünür
#    • ikonu kullanıcı ikon temasına yerleştirir
#    • masaüstüne çift tıklanan bir kopya koyar (varsa Desktop dizini)
#
#  Sonuç: "Tether Isolator"ı uygulama menüsünden veya masaüstünden çift
#  tıklayarak başlatabilirsiniz. (Açılışta bir kez polkit parola penceresi çıkar.)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APP_BIN="$REPO_ROOT/bin/tether-isolator-app"
ICON_SRC="$REPO_ROOT/assets/tether-isolator.svg"

APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
DESKTOP_FILE="$APPS_DIR/tether-isolator.desktop"

echo "→ Başlatıcılar çalıştırılabilir yapılıyor"
chmod +x "$REPO_ROOT/bin/tetherctl" "$APP_BIN"

echo "→ İkon yerleştiriliyor: $ICON_DIR"
mkdir -p "$ICON_DIR"
cp "$ICON_SRC" "$ICON_DIR/tether-isolator.svg"

echo "→ .desktop dosyası yazılıyor: $DESKTOP_FILE"
mkdir -p "$APPS_DIR"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Tether Isolator
GenericName=İzole Ağ Yöneticisi
Comment=Uygulamaları izole bir ağ alanında çalıştırın
Exec="$APP_BIN"
Icon=tether-isolator
Terminal=false
Categories=Network;Security;
Keywords=vpn;namespace;izolasyon;tether;network;
StartupNotify=true
EOF
chmod +x "$DESKTOP_FILE"

# Masaüstü kopyası (varsa)
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESKTOP_DIR" ]; then
  cp "$DESKTOP_FILE" "$DESKTOP_DIR/tether-isolator.desktop"
  chmod +x "$DESKTOP_DIR/tether-isolator.desktop"
  # GNOME, masaüstü başlatıcılarına "güven" işareti ister:
  gio set "$DESKTOP_DIR/tether-isolator.desktop" metadata::trusted true 2>/dev/null || true
  echo "→ Masaüstüne kopyalandı: $DESKTOP_DIR"
fi

# Menü önbelleğini tazele (varsa)
update-desktop-database "$APPS_DIR" 2>/dev/null || true

echo
echo "✓ Kuruldu. Artık 'Tether Isolator'ı uygulama menüsünden veya"
echo "  masaüstünden çift tıklayarak başlatabilirsiniz."
echo "  Kaldırmak için: rm \"$DESKTOP_FILE\" \"$DESKTOP_DIR/tether-isolator.desktop\""
