#!/bin/bash
cd "$(dirname "$0")"
echo "Cierra BetBot y elimina el entorno (.venv). data/ y artifacts/ NO se borran."
read -p "Escribe SI para continuar: " CONF
[ "$CONF" = "SI" ] || { echo "Cancelado."; exit 0; }
[ -x .venv/bin/python ] && ./.venv/bin/python -m betbot.launcher --stop >/dev/null 2>&1
rm -rf .venv artifacts/betbot.lock
echo "[OK] Desinstalado."
