#!/usr/bin/env bash
set -euo pipefail

echo "[HydroCompress] Verificando entorno en panel.plugged.host..."

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "[ERROR] No se encontró ffmpeg/ffprobe en el contenedor."
  echo "Instala ffmpeg en la imagen Docker/Egg o usa el Dockerfile incluido."
  exit 1
fi

echo "[HydroCompress] FFmpeg detectado: $(ffmpeg -version | head -n 1)"

if [ -f "requirements.txt" ]; then
  python3 -m pip install --upgrade --quiet -r requirements.txt
fi

exec python3 bot.py
