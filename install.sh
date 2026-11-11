#!/usr/bin/env bash
# Установка Nightfall в домашний каталог пользователя.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/nightfall"
BIN="${HOME}/.local/bin"
WITH_AUTOSTART=0
WITH_SERVICE=0

for arg in "$@"; do
  case "$arg" in
    --autostart) WITH_AUTOSTART=1 ;;
    --service)   WITH_SERVICE=1 ;;
    -h|--help)
      echo "использование: ./install.sh [--autostart] [--service]"
      exit 0 ;;
    *) echo "неизвестный флаг: $arg"; exit 1 ;;
  esac
done

python3 - <<'PY' || { echo "Нужен Python 3.9+"; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY

if ! python3 -c "import tkinter" 2>/dev/null; then
  echo "Внимание: tkinter не найден — будет доступен только режим --headless."
  echo "  Debian/Ubuntu: sudo apt install python3-tk"
fi

echo "→ Устанавливаю в $DEST"
mkdir -p "$DEST" "$BIN"
rm -rf "$DEST/nightfall"
cp -r "$SRC/nightfall" "$DEST/"
cp -r "$SRC/assets" "$DEST/"   # иконка для трея
cp "$SRC/nightfall.py" "$DEST/"
chmod +x "$DEST/nightfall.py"

cat > "$BIN/nightfall" <<LAUNCHER
#!/usr/bin/env bash
exec python3 "$DEST/nightfall.py" "\$@"
LAUNCHER
chmod +x "$BIN/nightfall"
echo "→ Команда: $BIN/nightfall"

if [ "$WITH_AUTOSTART" = "1" ]; then
  AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
  mkdir -p "$AUTOSTART_DIR"
  sed "s|%h/.local/share/nightfall/nightfall.py|$DEST/nightfall.py|" \
      "$SRC/packaging/nightfall.desktop" > "$AUTOSTART_DIR/nightfall.desktop"
  echo "→ Автозапуск (иконка в трее, без окна): $AUTOSTART_DIR/nightfall.desktop"
fi

if [ "$WITH_SERVICE" = "1" ]; then
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$UNIT_DIR"
  sed "s|%h/.local/share/nightfall/nightfall.py|$DEST/nightfall.py|" \
      "$SRC/packaging/nightfall.service" > "$UNIT_DIR/nightfall.service"
  systemctl --user daemon-reload || true
  echo "→ Сервис установлен. Включить: systemctl --user enable --now nightfall.service"
fi

echo
echo "Готово."
echo "  nightfall                 # окно (служба поднимется сама)"
echo "  nightfall --service       # только служба, без окна"
echo "  nightfall --status        # что запланировано"
echo "  nightfall --postpone 15   # отложить выключение"
echo "Сейчас режим имитации — реальное выключение включается в настройках или флагом --real."
