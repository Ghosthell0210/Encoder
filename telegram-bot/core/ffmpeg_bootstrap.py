"""Instalador automático rootless de FFmpeg y FFprobe para Pelican / Pterodactyl.

El Egg 'Python Generic' (yolks:python_3.11) corre sin permisos root y con
frecuencia no trae ffmpeg preinstalado. Este módulo verifica si existe en PATH
o en ./bin/; si falta, descarga un compilado estático Linux (con libx264,
libx265 y libass) y extrae únicamente ffmpeg y ffprobe en ./bin/.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import stat
import tarfile
import urllib.request
from pathlib import Path

logger = logging.getLogger("hydrocompress.ffmpeg_bootstrap")

# Compilados estáticos GPL con libx264, libx265, libass, aac y ffprobe
STATIC_URLS: dict[str, list[str]] = {
    "x86_64": [
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz",
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    ],
    "aarch64": [
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linuxarm64-gpl.tar.xz",
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz",
    ],
}


def _prepend_bin_to_path(bin_dir: Path) -> None:
    bin_str = str(bin_dir.resolve())
    current_path = os.environ.get("PATH", "")
    parts = current_path.split(os.pathsep) if current_path else []
    if bin_str not in parts:
        os.environ["PATH"] = f"{bin_str}{os.pathsep}{current_path}" if current_path else bin_str


def _arch_key() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "x86_64"
    if machine in {"aarch64", "arm64"}:
        return "aarch64"
    raise RuntimeError(f"Arquitectura Linux no soportada para descarga automática: {machine}")


def _download_with_log(url: str, dest: Path) -> None:
    logger.info("Descargando FFmpeg estático desde: %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "HydroCompress-Bot/1.0"})
    with urllib.request.urlopen(req, timeout=180) as resp, dest.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        last_pct = -10
        while True:
            chunk = resp.read(512 * 1024)
            if not chunk:
                break
            out.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                pct = int((downloaded / total) * 100)
                if pct >= last_pct + 20:
                    logger.info("Descarga FFmpeg: %d%% (%.1f MiB)", pct, downloaded / (1024 * 1024))
                    last_pct = pct


def _extract_binaries(archive_path: Path, bin_dir: Path) -> bool:
    bin_dir.mkdir(parents=True, exist_ok=True)
    found = set()
    with tarfile.open(archive_path, mode="r:*") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            base = Path(member.name).name
            if base in {"ffmpeg", "ffprobe"}:
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                target = bin_dir / base
                with extracted, target.open("wb") as out:
                    shutil.copyfileobj(extracted, out)
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                found.add(base)
                if found == {"ffmpeg", "ffprobe"}:
                    break
    return found == {"ffmpeg", "ffprobe"}


def ensure_ffmpeg(bin_dir: Path) -> tuple[str, str]:
    """Garantiza que ffmpeg y ffprobe estén disponibles en PATH sin requerir root."""
    _prepend_bin_to_path(bin_dir)

    ff = shutil.which("ffmpeg")
    fp = shutil.which("ffprobe")
    if ff and fp:
        logger.info("FFmpeg listo en: %s", ff)
        return ff, fp

    if platform.system().lower() != "linux":
        raise RuntimeError(
            "FFmpeg/FFprobe no están en PATH. Instálalos manualmente en tu sistema operativo."
        )

    arch = _arch_key()
    bin_dir.mkdir(parents=True, exist_ok=True)
    archive_path = bin_dir / "ffmpeg_static.tar.xz"

    last_error: Exception | None = None
    for url in STATIC_URLS[arch]:
        try:
            _download_with_log(url, archive_path)
            logger.info("Extrayendo ffmpeg y ffprobe en %s ...", bin_dir)
            if _extract_binaries(archive_path, bin_dir):
                break
            raise RuntimeError("El archivo descargado no contenía ffmpeg y ffprobe.")
        except Exception as exc:
            last_error = exc
            logger.warning("Fallo fuente %s: %s", url, exc)
        finally:
            if archive_path.exists():
                archive_path.unlink(missing_ok=True)
    else:
        raise RuntimeError(
            f"No se pudo instalar FFmpeg automáticamente ({last_error}). "
            "Verifica la salida a internet del contenedor o usa una imagen con FFmpeg."
        )

    _prepend_bin_to_path(bin_dir)
    ff = shutil.which("ffmpeg")
    fp = shutil.which("ffprobe")
    if not ff or not fp:
        raise RuntimeError("Error verificando ffmpeg/ffprobe tras la extracción.")

    logger.info("FFmpeg estático instalado automáticamente sin root en: %s", bin_dir)
    return ff, fp
