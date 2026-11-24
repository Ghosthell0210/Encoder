"""Captura automática de advertencias y errores por trabajo (Auto-Log).

Utiliza un ContextVar de asyncio para aislar cada trabajo concurrente y recopilar
cualquier WARNING/ERROR del logger raíz + avisos no fatales de FFmpeg durante el
ciclo de vida del trabajo, aunque el video finalice con éxito.
"""

from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CURRENT_JOB_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hydrocompress_current_job_id",
    default=None,
)

# Avisos benignos muy frecuentes de FFmpeg que no representan errores reales
_BENIGN_FFMPEG_PATTERNS = (
    "deprecated pixel format used",
    "last message repeated",
    "guestimating channel layout",
    "guessing channel layout",
    "swscaler",
    "non-monotonous dts",
    "past duration",
    "side data",
)


def is_actionable_ffmpeg_line(line: str) -> bool:
    low = line.strip().lower()
    if not low:
        return False
    for pat in _BENIGN_FFMPEG_PATTERNS:
        if pat in low:
            return False
    # Capturar advertencias/errores reales
    if any(
        token in low
        for token in (
            "error",
            "failed",
            "invalid",
            "warning",
            "could not",
            "cannot",
            "unable",
            "corrupt",
            "discarding",
            "missing",
            "unsupported",
            "overflow",
            "dropped",
            "no such",
            "permission denied",
            "out of memory",
            "killed",
        )
    ):
        return True
    return False


@dataclass(slots=True)
class JobIssueCollector:
    job_id: str
    max_entries: int = 120
    entries: list[str] = field(default_factory=list)
    warning_count: int = 0
    error_count: int = 0
    started_at: float = field(default_factory=time.time)

    def add(self, level: str, source: str, message: str) -> None:
        lvl = level.upper()
        if lvl in {"ERROR", "CRITICAL"}:
            self.error_count += 1
        else:
            self.warning_count += 1

        if len(self.entries) >= self.max_entries:
            return

        ts = datetime.now().strftime("%H:%M:%S")
        clean_msg = " ".join(message.strip().splitlines())[:420]
        self.entries.append(f"[{ts}] {lvl:<7} | {source} | {clean_msg}")

    def add_ffmpeg_line(self, line: str) -> None:
        if not is_actionable_ffmpeg_line(line):
            return
        low = line.lower()
        lvl = "ERROR" if any(k in low for k in ("error", "failed", "cannot", "corrupt", "killed")) else "WARNING"
        self.add(lvl, "ffmpeg", line)

    @property
    def has_issues(self) -> bool:
        return bool(self.entries)

    def build_report_text(self, metadata: dict[str, str]) -> str:
        lines = [
            f"=== HydroCompress Auto-Log | Trabajo {self.job_id} ===",
            f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        for k, v in metadata.items():
            lines.append(f"{k}: {v}")
        lines.append(
            f"Incidencias capturadas: {self.error_count} errores · {self.warning_count} avisos"
        )
        lines.append("=" * 60)
        lines.extend(self.entries)
        if len(self.entries) >= self.max_entries:
            lines.append(f"... (limitado a las primeras {self.max_entries} incidencias)")
        return "\n".join(lines) + "\n"

    def save_report(
        self,
        job_logs_dir: Path,
        metadata: dict[str, str],
        max_files: int = 30,
    ) -> Path:
        job_logs_dir.mkdir(parents=True, exist_ok=True)
        report_path = job_logs_dir / f"job_{self.job_id}.log"
        report_path.write_text(self.build_report_text(metadata), encoding="utf-8")

        # Rotación ligera: conservar solo los últimos N archivos de log por trabajo
        try:
            files = sorted(
                job_logs_dir.glob("job_*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for stale in files[max_files:]:
                stale.unlink(missing_ok=True)
        except OSError:
            pass
        return report_path


_ACTIVE_COLLECTORS: dict[str, JobIssueCollector] = {}
_HANDLER_INSTALLED = False


class _JobContextLogHandler(logging.Handler):
    """Captura registros WARNING+ del logger raíz para el trabajo en curso."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        job_id = CURRENT_JOB_ID.get()
        if not job_id:
            return
        collector = _ACTIVE_COLLECTORS.get(job_id)
        if collector is None:
            return
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                msg = f"{msg} ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
            collector.add(record.levelname, record.name, msg)
        except Exception:
            pass


def install_job_log_handler() -> None:
    global _HANDLER_INSTALLED
    if _HANDLER_INSTALLED:
        return
    handler = _JobContextLogHandler()
    handler.setLevel(logging.WARNING)
    logging.getLogger().addHandler(handler)
    _HANDLER_INSTALLED = True


def start_job_capture(job_id: str) -> tuple[JobIssueCollector, contextvars.Token[str | None]]:
    install_job_log_handler()
    collector = JobIssueCollector(job_id=job_id)
    _ACTIVE_COLLECTORS[job_id] = collector
    token = CURRENT_JOB_ID.set(job_id)
    return collector, token


def stop_job_capture(job_id: str, token: contextvars.Token[str | None]) -> None:
    try:
        CURRENT_JOB_ID.reset(token)
    except ValueError:
        pass
    _ACTIVE_COLLECTORS.pop(job_id, None)


def record_current_job_ffmpeg_line(line: str) -> None:
    """Permite que ffmpeg_engine y media_tools registren avisos no fatales de stderr."""
    job_id = CURRENT_JOB_ID.get()
    if not job_id:
        return
    collector = _ACTIVE_COLLECTORS.get(job_id)
    if collector is not None:
        collector.add_ffmpeg_line(line)
