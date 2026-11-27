"""Sistema de logging con archivo rotativo (data/bot.log) y lectura rápida de errores."""

from __future__ import annotations

import logging
import re
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

_HYDROGRAM_WAIT_RE = re.compile(r"Waiting for (\d+) seconds? before continuing", re.IGNORECASE)


class _HydrogramWaitFilter(logging.Filter):
    """Oculta micro-esperas rutinarias (<=10s) de Hydrogram durante subidas paralelas."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR:
            return True
        if not record.name.startswith("hydrogram"):
            return True
        msg = record.getMessage()
        m = _HYDROGRAM_WAIT_RE.search(msg)
        if m:
            try:
                if int(m.group(1)) <= 10:
                    return False
            except ValueError:
                pass
        return True


def setup_logging(log_file: Path) -> Path:
    """Configura salida simultánea a consola y archivo rotativo (5 MiB × 3 respaldos)."""
    log_file.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Limpiar handlers previos en caso de recarga
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    wait_filter = _HydrogramWaitFilter()

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(wait_filter)

    file_handler = RotatingFileHandler(
        filename=str(log_file),
        maxBytes=5 * 1024 * 1024,  # 5 MiB por archivo
        backupCount=3,  # Máximo 20 MiB en disco
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(wait_filter)

    root.addHandler(stream_handler)
    root.addHandler(file_handler)

    # Reducir ruido innecesario de librerías HTTP/SQLite
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    return log_file


def read_last_log_lines(log_file: Path, lines: int = 35, errors_only: bool = False) -> str:
    """Lee las últimas líneas de bot.log sin cargar todo el archivo en memoria."""
    if not log_file.exists():
        return "Todavía no existe ningún archivo de log."

    max_lines = max(5, min(150, lines))
    tail: deque[str] = deque(maxlen=max_lines)

    with log_file.open("r", encoding="utf-8", errors="replace") as fp:
        for raw in fp:
            line = raw.rstrip("\n")
            if errors_only and not any(
                tag in line for tag in ("| ERROR", "| WARNING", "| CRITICAL", "Traceback", "Error:")
            ):
                continue
            tail.append(line)

    if not tail:
        return "No hay registros de error recientes." if errors_only else "El archivo de log está vacío."
    return "\n".join(tail)
