"""Configuración central del bot de compresión de videos para Telegram.

Calibrado por defecto para panel.plugged.host (Egg Python Generic Pelican/Pterodactyl):
  - Auto-detección de CPU y RAM desde cgroups del contenedor (por defecto 500% CPU / 5 hilos y 4 GiB RAM)
  - 30 GiB Disco asignado (con reserva de seguridad de 2 GiB)
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_THIS_DIR = Path(__file__).resolve().parent
for _candidate_env in (Path.cwd() / ".env", _THIS_DIR.parent / ".env", _THIS_DIR / ".env"):
    if _candidate_env.exists():
        load_dotenv(_candidate_env, override=False)
load_dotenv(override=False)

# Límite seguro de subida MTProto para bots (2000 MiB exactos en Telegram;
# usamos 1990 MiB para evitar rechazos por redondeo de partes de 512 KiB).
TELEGRAM_MAX_UPLOAD_BYTES: int = 1990 * 1024 * 1024

# Límite seguro para cuentas Telegram Premium vinculadas vía /login (4000 MiB).
TELEGRAM_PREMIUM_MAX_UPLOAD_BYTES: int = 3980 * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw or raw.lower() == "auto":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw or raw.lower() == "auto":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on", "si", "sí"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _read_text_safe(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _detect_cgroup_cpu_threads() -> int | None:
    """Detecta la cuota real de vCPUs del contenedor Docker/Pelican (cgroup v2 y v1)."""
    detected: int | None = None

    # cgroup v2: /sys/fs/cgroup/cpu.max -> "<quota> <period>" o "max <period>"
    cpu_max = _read_text_safe("/sys/fs/cgroup/cpu.max")
    if cpu_max:
        parts = cpu_max.split()
        if len(parts) >= 2 and parts[0] != "max":
            try:
                quota = int(parts[0])
                period = int(parts[1])
                if quota > 0 and period > 0:
                    detected = max(1, int(round(quota / period)))
            except ValueError:
                pass

    # cgroup v1: cpu.cfs_quota_us / cpu.cfs_period_us
    if detected is None:
        for base in ("/sys/fs/cgroup/cpu", "/sys/fs/cgroup/cpu,cpuacct"):
            q_txt = _read_text_safe(f"{base}/cpu.cfs_quota_us")
            p_txt = _read_text_safe(f"{base}/cpu.cfs_period_us")
            if q_txt and p_txt:
                try:
                    quota = int(q_txt)
                    period = int(p_txt)
                    if quota > 0 and period > 0:
                        detected = max(1, int(round(quota / period)))
                        break
                except ValueError:
                    pass

    if detected is None:
        return None

    host_cpus = os.cpu_count()
    if host_cpus and host_cpus > 0:
        detected = min(detected, host_cpus)
    return max(1, min(16, detected))


def _detect_cgroup_ram_gb() -> float | None:
    """Detecta el límite real de RAM (GiB) del contenedor Docker/Pelican (cgroup v2, v1 o SERVER_MEMORY)."""
    # Límite máximo razonable (< 32 TiB) para descartar centinelas sin límite (~9e18)
    max_sane_bytes = 1 << 45

    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        raw = _read_text_safe(path)
        if not raw or raw == "max":
            continue
        try:
            limit_bytes = int(raw)
        except ValueError:
            continue
        if 128 * 1024 * 1024 <= limit_bytes < max_sane_bytes:
            return round(limit_bytes / (1024**3), 2)

    # Variable estándar de Pelican/Pterodactyl (en MiB, ej. 4096)
    srv_mem = os.getenv("SERVER_MEMORY", "").strip()
    if srv_mem:
        try:
            mib = float(srv_mem)
            if mib >= 128:
                return round(mib / 1024.0, 2)
        except ValueError:
            pass

    return None


def _dir_bytes_simple(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _detect_disk_gb(work_dir: Path, links_dir: Path) -> float | None:
    """Detecta el espacio real disponible en disco (GiB) para el presupuesto del bot."""
    try:
        usage = shutil.disk_usage(work_dir)
    except OSError:
        return None
    if usage.total <= 0:
        return None
    used_by_bot = _dir_bytes_simple(work_dir) + _dir_bytes_simple(links_dir)
    avail_bytes = int(usage.free) + used_by_bot
    if avail_bytes < 2 * (1024**3):
        return None
    return round(avail_bytes / (1024**3), 1)


def _interactive_setup_env(base_dir: Path) -> None:
    """Si faltan credenciales y hay consola interactiva (Pelican/Pterodactyl), las pide y guarda .env."""
    env_path = Path.cwd() / ".env" if Path.cwd().exists() else base_dir / ".env"
    print("\n═══ Configuración inicial de HydroCompress Bot ═══")
    print("Faltan credenciales de Telegram. Introdúcelas ahora para crear .env automáticamente:\n")
    api_id = input("1) API_ID (de https://my.telegram.org): ").strip()
    api_hash = input("2) API_HASH (de https://my.telegram.org): ").strip()
    bot_token = input("3) BOT_TOKEN (de @BotFather): ").strip()
    owner_id = input("4) OWNER_ID (tu ID numérico de @userinfobot): ").strip()

    content = "\n".join(
        [
            f"API_ID={api_id}",
            f"API_HASH={api_hash}",
            f"BOT_TOKEN={bot_token}",
            f"OWNER_ID={owner_id}",
            "AUTO_DETECT_RESOURCES=true",
            "MAX_CPU_THREADS=5",
            "MAX_RAM_GB=4",
            "MAX_DISK_GB=auto",
            "DISK_RESERVE_GB=2.0",
            "MAX_CONCURRENT_JOBS=1",
            "ACCESS_MODE=private",
            "LARGE_FILE_UPLOADER=gofile",
            "PROGRESS_INTERVAL_SEC=3.5",
            "",
        ]
    )
    env_path.write_text(content, encoding="utf-8")
    load_dotenv(env_path, override=True)
    print(f"\n✅ Archivo .env guardado en {env_path}\n")


@dataclass(slots=True)
class BotConfig:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int

    # Cuenta de Telegram opcional (para subir hasta 4 GB con Premium o gestionar sesión)
    user_session_string: str = ""
    dump_chat_id: int = 0

    # Presupuesto de recursos del contenedor (panel.plugged.host)
    max_cpu_threads: int = 5
    max_ram_gb: float = 4.0
    max_disk_gb: float = 30.0
    disk_reserve_gb: float = 2.0
    max_concurrent_jobs: int = 1
    fast_transfer_workers: int = 6
    cpu_auto_detected: bool = False
    ram_auto_detected: bool = False
    disk_auto_detected: bool = False

    # Acceso inicial (private = solo dueño + lista blanca | public = abierto)
    default_access_mode: str = "private"

    # Entrega automática cuando el archivo comprimido supera el límite de Telegram
    large_file_uploader: str = "gofile"  # gofile | buzzheavier | direct_http
    gofile_token: str = ""
    public_download_base_url: str = ""
    direct_link_host: str = "0.0.0.0"
    direct_link_port: int = 8080
    direct_link_ttl_hours: int = 6

    # Rutas locales persistentes y temporales
    base_dir: Path = Path(__file__).resolve().parent
    bin_dir: Path = Path("./bin")
    data_dir: Path = Path("./data")
    work_dir: Path = Path("./temp_work")
    links_dir: Path = Path("./temp_links")
    db_path: Path = Path("./data/bot.db")

    # Intervalo de actualización de mensajes en Telegram (evita FloodWait)
    progress_interval_sec: float = 3.5

    @classmethod
    def from_env(cls) -> BotConfig:
        base_dir = Path(__file__).resolve().parent
        bin_dir = (base_dir / "bin").resolve()
        data_dir = Path(_env_str("DATA_DIR", str(base_dir / "data"))).resolve()
        work_dir = Path(_env_str("WORK_DIR", str(base_dir / "temp_work"))).resolve()
        links_dir = Path(_env_str("LINKS_DIR", str(base_dir / "temp_links"))).resolve()
        db_path = data_dir / "bot.db"

        bin_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        links_dir.mkdir(parents=True, exist_ok=True)

        api_id = _env_int("API_ID", 0)
        api_hash = _env_str("API_HASH")
        bot_token = _env_str("BOT_TOKEN")
        owner_id = _env_int("OWNER_ID", 0)

        if (api_id <= 0 or not api_hash or not bot_token or owner_id <= 0) and not sys.stdin.closed:
            try:
                _interactive_setup_env(base_dir)
                api_id = _env_int("API_ID", 0)
                api_hash = _env_str("API_HASH")
                bot_token = _env_str("BOT_TOKEN")
                owner_id = _env_int("OWNER_ID", 0)
            except EOFError:
                pass

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
                "Crea el archivo .env en el Administrador de Archivos de panel.plugged.host "
                "(puedes generarlo desde la pestaña 'Configurar .env' antes de descargar el ZIP)."
            )

        uploader = _env_str("LARGE_FILE_UPLOADER", "gofile").lower()
        if uploader not in {"gofile", "buzzheavier", "direct_http"}:
            uploader = "gofile"

        access_mode = _env_str("ACCESS_MODE", "private").lower()
        if access_mode not in {"private", "public"}:
            access_mode = "private"

        auto_detect = _env_bool("AUTO_DETECT_RESOURCES", True)
        detected_threads = _detect_cgroup_cpu_threads() if auto_detect else None
        detected_ram = _detect_cgroup_ram_gb() if auto_detect else None

        if detected_threads is not None:
            max_threads = detected_threads
            cpu_auto = True
        else:
            max_threads = max(1, min(16, _env_int("MAX_CPU_THREADS", 5)))
            cpu_auto = False

        if detected_ram is not None:
            max_ram_gb = max(0.5, detected_ram)
            ram_auto = True
        else:
            max_ram_gb = max(0.5, _env_float("MAX_RAM_GB", 4.0))
            ram_auto = False

        raw_disk_env = os.getenv("MAX_DISK_GB", "").strip().lower()
        explicit_disk = bool(raw_disk_env) and raw_disk_env not in {"auto", "30", "30.0"}
        detected_disk = _detect_disk_gb(work_dir, links_dir) if (auto_detect and not explicit_disk) else None
        if detected_disk is not None:
            max_disk_gb = max(5.0, detected_disk)
            disk_auto = True
        else:
            max_disk_gb = max(5.0, _env_float("MAX_DISK_GB", 30.0))
            disk_auto = False

        max_concurrent = max(1, min(2, _env_int("MAX_CONCURRENT_JOBS", 1)))

        return cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            owner_id=owner_id,
            user_session_string=_env_str("USER_SESSION_STRING"),
            dump_chat_id=_env_int("DUMP_CHAT_ID", 0),
            max_cpu_threads=max_threads,
            max_ram_gb=max_ram_gb,
            max_disk_gb=max_disk_gb,
            disk_reserve_gb=max(0.5, _env_float("DISK_RESERVE_GB", 2.0)),
            max_concurrent_jobs=max_concurrent,
            fast_transfer_workers=max(2, min(12, _env_int("FAST_TRANSFER_WORKERS", 6))),
            cpu_auto_detected=cpu_auto,
            ram_auto_detected=ram_auto,
            disk_auto_detected=disk_auto,
            default_access_mode=access_mode,
            large_file_uploader=uploader,
            gofile_token=_env_str("GOFILE_TOKEN"),
            public_download_base_url=_env_str("PUBLIC_DOWNLOAD_BASE_URL").rstrip("/"),
            direct_link_host=_env_str("DIRECT_LINK_HOST", "0.0.0.0"),
            direct_link_port=_env_int("DIRECT_LINK_PORT", 8080),
            direct_link_ttl_hours=max(1, _env_int("DIRECT_LINK_TTL_HOURS", 6)),
            base_dir=base_dir,
            bin_dir=bin_dir,
            data_dir=data_dir,
            work_dir=work_dir,
            links_dir=links_dir,
            db_path=db_path,
            progress_interval_sec=max(2.0, _env_float("PROGRESS_INTERVAL_SEC", 3.5)),
        )
