#!/usr/bin/env bash
set -euo pipefail

echo "[HydroCompress] Iniciando en panel.plugged.host (Pelican/Pterodactyl)..."

if [ -f "requirements.txt" ]; then
  python3 -m pip install --upgrade --quiet -r requirements.txt
fi

# Nota: bot.py descarga automáticamente ffmpeg y ffprobe estáticos en ./bin
# si el contenedor no los tiene preinstalados (sin requerir root).
exec python3 bot.py
