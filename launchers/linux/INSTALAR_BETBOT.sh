#!/bin/bash
cd "$(dirname "$0")"
echo "=== INSTALADOR DE BETBOT (solo primera vez) ==="
command -v python3 >/dev/null || { echo "[ERROR] instala python3 (>=3.11)"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' || {
  echo "[ERROR] Se requiere Python >= 3.11 (tienes $(python3 -V))"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -e . || { echo "[ERROR] instalacion fallida"; exit 1; }
echo "[OK] Instalado. Abre BetBot con ABRIR_BETBOT.sh (doble clic)."
