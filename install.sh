#!/bin/sh
# Installs for the current user: ~/.local/bin/phosphor-snake (wrapper), the .desktop entry and the icon. No sudo.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
BIN=${XDG_BIN_HOME:-$HOME/.local/bin}; APPS=${XDG_DATA_HOME:-$HOME/.local/share}/applications; ICONS=${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps
mkdir -p "$BIN" "$APPS" "$ICONS"
python3 -c "import gi, cairo, PIL" 2>/dev/null || { echo "needs python3-gi (GTK3), python3-cairo and python3-pil:  sudo apt install python3-gi gir1.2-gtk-3.0 python3-cairo python3-pil"; exit 1; }
printf '#!/bin/sh\nexec setsid -f python3 "%s/phosphor_snake.py" "$@"\n' "$HERE" > "$BIN/phosphor-snake"
chmod +x "$BIN/phosphor-snake"
python3 "$HERE/phosphor_snake.py" --icon "$ICONS/phosphor-snake.png" >/dev/null
sed "s|^Exec=.*|Exec=$BIN/phosphor-snake|" "$HERE/phosphor-snake.desktop" > "$APPS/phosphor-snake.desktop"
update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -q "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true
echo "installed: $BIN/phosphor-snake  +  $APPS/phosphor-snake.desktop (find 'Phosphor Snake' in your app menu)"
