"""Configuración central del bot de compresión de videos para Telegram.

Calibrado por defecto para panel.plugged.host:
  - 400% CPU (4 hilos FFmpeg)
  - 10 GiB RAM asignada
  - 30 GiB Disco asignado (con reserva de seguridad de 2 GiB)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Límite seguro de subida MTProto para bots (2000 MiB exactos en Telegram;
# usamos 1990 MiB para evitar rechazos por redondeo de partes de 512 KiB).
TELEGRAM_MAX_UPLOAD_BYTES: int = 1990 * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(slots=True)
class BotConfig:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int

    # Presupuesto de recursos del contenedor (panel.plugged.host)
    max_cpu_threads: int = 4
    max_ram_gb: float = 10.0
    max_disk_gb: float = 30.0
    disk_reserve_gb: float = 2.0
    max_concurrent_jobs: int = 1

    # Acceso inicial (private = solo dueño + lista blanca | public = abierto)
    default_access_mode: str = "private"

    # Entrega automática cuando el archivo comprimido supera ~1.99 GB
    large_file_uploader: str = "gofile"  # gofile | buzzheavier | direct_http
    gofile_token: str = ""
    public_download_base_url: str = ""
    direct_link_host: str = "0.0.0.0"
    direct_link_port: int = 8080
    direct_link_ttl_hours: int = 6

    # Rutas locales persistentes y temporales
    base_dir: Path = Path(__file__).resolve().parent
    data_dir: Path = Path("./data")
    work_dir: Path = Path("./temp_work")
    links_dir: Path = Path("./temp_links")
    db_path: Path = Path("./data/bot.db")

    # Intervalo de actualización de mensajes en Telegram (evita FloodWait)
    progress_interval_sec: float = 3.5

    @classmethod
    def from_env(cls) -> BotConfig:
        base_dir = Path(__file__).resolve().parent
        data_dir = Path(_env_str("DATA_DIR", str(base_dir / "data"))).resolve()
        work_dir = Path(_env_str("WORK_DIR", str(base_dir / "temp_work"))).resolve()
        links_dir = Path(_env_str("LINKS_DIR", str(base_dir / "temp_links"))).resolve()
        db_path = data_dir / "bot.db"

        data_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        links_dir.mkdir(parents=True, exist_ok=True)

        api_id = _env_int("API_ID", 0)
        api_hash = _env_str("API_HASH")
        bot_token = _env_str("BOT_TOKEN")
        owner_id = _env_int("OWNER_ID", 0)

        missing = []
        if api_id <= 0:
            missing.append("API_ID")
        if not api_hash:
            missing.append("API_HASH")
        if not bot_token:
            missing.append("BOT_TOKEN")
        if owner_id <= 0:
            missing.append("OWNER_ID")

        if missing:
            raise RuntimeError(
                f"Faltan variables de entorno obligatorias: {', '.join(missing)}. "
                "Revisa tu archivo .env o las variables en panel.plugged.host."
            )

        uploader = _env_str("LARGE_FILE_UPLOADER", "gofile").lower()
        if uploader not in {"gofile", "buzzheavier", "direct_http"}:
            uploader = "gofile"

        access_mode = _env_str("ACCESS_MODE", "private").lower()
        if access_mode not in {"private", "public"}:
            access_mode = "private"

        max_threads = max(1, min(16, _env_int("MAX_CPU_THREADS", 4)))
        max_concurrent = max(1, min(2, _env_int("MAX_CONCURRENT_JOBS", 1)))

        return cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            owner_id=owner_id,
            max_cpu_threads=max_threads,
            max_ram_gb=max(1.0, _env_float("MAX_RAM_GB", 10.0)),
            max_disk_gb=max(5.0, _env_float("MAX_DISK_GB", 30.0)),
            disk_reserve_gb=max(0.5, _env_float("DISK_RESERVE_GB", 2.0)),
            max_concurrent_jobs=max_concurrent,
            default_access_mode=access_mode,
            large_file_uploader=uploader,
            gofile_token=_env_str("GOFILE_TOKEN"),
            public_download_base_url=_env_str("PUBLIC_DOWNLOAD_BASE_URL").rstrip("/"),
            direct_link_host=_env_str("DIRECT_LINK_HOST", "0.0.0.0"),
            direct_link_port=_env_int("DIRECT_LINK_PORT", 8080),
            direct_link_ttl_hours=max(1, _env_int("DIRECT_LINK_TTL_HOURS", 6)),
            base_dir=base_dir,
            data_dir=data_dir,
            work_dir=work_dir,
            links_dir=links_dir,
            db_path=db_path,
            progress_interval_sec=max(2.0, _env_float("PROGRESS_INTERVAL_SEC", 3.5)),
        )
