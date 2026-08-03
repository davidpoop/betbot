#!/bin/bash
# Instalador de BetBot para macOS (solo primera vez; ventana visible a proposito).
cd "$(dirname "$0")"
echo "============================================"
echo "  INSTALADOR DE BETBOT (solo primera vez)"
echo "============================================"
if ! command -v python3 >/dev/null; then
  echo "[ERROR] No hay Python. Instala Python 3.11+ desde https://www.python.org/downloads/macos/"
  read -p "Pulsa Enter para salir"; exit 1
fi
python3 - <<'PYEOF' || { echo "[ERROR] Se requiere Python >= 3.11 (tienes $(python3 -V))"; read -p "Enter para salir"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PYEOF
echo "Python detectado: $(python3 -V)"
[ -d .venv ] || { echo "Creando entorno virtual..."; python3 -m venv .venv || exit 1; }
echo "Instalando BetBot y dependencias (requiere Internet la primera vez)..."
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -e . || { echo "[ERROR] instalacion fallida"; read -p "Enter"; exit 1; }
xattr -dr com.apple.quarantine BetBot.app 2>/dev/null
chmod +x BetBot.app/Contents/MacOS/BetBot
echo
echo "[OK] Instalacion completada. Abre BetBot con doble clic en BetBot.app"
echo "     (la primera vez: clic derecho > Abrir, por ser una app sin firmar)."
read -p "Pulsa Enter para terminar"
