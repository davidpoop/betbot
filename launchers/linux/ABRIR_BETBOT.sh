#!/bin/bash
# Doble clic desde el gestor de archivos: abre BetBot sin terminal.
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python"
mkdir -p "$ROOT/artifacts/logs"
if [ ! -x "$PY" ]; then
  notify-send "BetBot" "Ejecuta primero INSTALAR_BETBOT.sh" 2>/dev/null || \
    echo "Ejecuta primero INSTALAR_BETBOT.sh"
  exit 1
fi
cd "$ROOT"
nohup "$PY" -m betbot.launcher >> "$ROOT/artifacts/logs/launcher_app.log" 2>&1 &
