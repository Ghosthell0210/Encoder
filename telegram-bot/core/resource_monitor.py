"""Monitor de consumo real de RAM, CPU y disco por video (bot + procesos FFmpeg).

Muestrea cada 1 segundo el árbol completo de procesos del contenedor (Python +
ffmpeg/ffprobe) y el espacio en disco usado por el trabajo, para medir picos
reales y dimensionar exactamente CPU %, RAM y disco en panel.plugged.host.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from dataclasses import dataclass
from pathlib import Path

import psutil


@dataclass(slots=True)
class JobResourceMetrics:
    peak_ram_bytes: int = 0
    avg_ram_bytes: int = 0
    peak_cpu_pct: float = 0.0
    avg_cpu_pct: float = 0.0
    peak_disk_bytes: int = 0
    samples: int = 0


def _dir_bytes(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            with contextlib.suppress(OSError):
                total += item.stat().st_size
    return total


def recommended_panel_specs(
    max_ram_bytes: int,
    max_cpu_pct: float,
    max_disk_bytes: int,
) -> tuple[float, int, float]:
    """Calcula especificaciones seguras sugeridas para Pelican/Pterodactyl.

    Devuelve (ram_gib, cpu_pct, disk_gib):
    - RAM: pico real × 1.5 + 256 MiB de colchón, redondeado al 0.5 GiB superior (mín 1.0 GiB).
    - CPU: múltiplo de 100% superior al pico real (mín 100%).
    - Disco: pico por video × 1.5 + 2 GiB de margen sistema/logs (mín 5.0 GiB).
    """
    ram_gib_raw = (max_ram_bytes * 1.5 + 256 * 1024 * 1024) / (1024**3)
    ram_gib = max(1.0, math.ceil(ram_gib_raw * 2.0) / 2.0)

    cpu_pct = max(100, int(math.ceil(max(max_cpu_pct, 50.0) / 100.0) * 100))

    disk_gib_raw = (max_disk_bytes * 1.5) / (1024**3) + 2.0
    disk_gib = max(5.0, float(math.ceil(disk_gib_raw)))
    return ram_gib, cpu_pct, disk_gib


class JobResourceMonitor:
    """Mide en segundo plano picos y promedios de RAM, CPU y disco de un trabajo."""

    def __init__(
        self,
        watch_dirs: list[Path],
        interval_sec: float = 1.0,
    ) -> None:
        self._watch_dirs = watch_dirs
        self._interval = max(0.5, interval_sec)
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._root_proc = psutil.Process()
        self._tracked_procs: dict[int, psutil.Process] = {}

        self._peak_ram = 0
        self._sum_ram = 0
        self._peak_cpu = 0.0
        self._sum_cpu = 0.0
        self._peak_disk = 0
        self._samples = 0

    def add_watch_path(self, path: Path) -> None:
        if path not in self._watch_dirs:
            self._watch_dirs.append(path)

    def start(self) -> None:
        # Inicializar referencia de CPU para el proceso principal
        with contextlib.suppress(psutil.Error):
            self._root_proc.cpu_percent(interval=None)
        self._task = asyncio.create_task(self._sample_loop())

    def _collect_once(self) -> None:
        current_procs: list[psutil.Process] = [self._root_proc]
        with contextlib.suppress(psutil.Error):
            current_procs.extend(self._root_proc.children(recursive=True))

        alive_pids: set[int] = set()
        total_ram = 0
        total_cpu = 0.0

        for proc in current_procs:
            pid = proc.pid
            alive_pids.add(pid)
            cached = self._tracked_procs.get(pid)
            if cached is None:
                # Primera vez que vemos este hijo (ej. ffmpeg recién lanzado): cebar cpu_percent
                with contextlib.suppress(psutil.Error):
                    proc.cpu_percent(interval=None)
                self._tracked_procs[pid] = proc
                cached = proc

            try:
                mem = cached.memory_info()
                total_ram += int(mem.rss)
                cpu_val = float(cached.cpu_percent(interval=None))
                total_cpu += cpu_val
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._tracked_procs.pop(pid, None)

        # Limpiar PIDs terminados
        for stale_pid in list(self._tracked_procs.keys()):
            if stale_pid not in alive_pids:
                self._tracked_procs.pop(stale_pid, None)

        # Espacio real en disco usado por las rutas de este trabajo (sin contar duplicados)
        seen_paths: set[Path] = set()
        total_disk = 0
        for p in self._watch_dirs:
            rp = p.resolve()
            if rp not in seen_paths:
                seen_paths.add(rp)
                total_disk += _dir_bytes(rp)

        self._samples += 1
        self._sum_ram += total_ram
        self._sum_cpu += total_cpu
        if total_ram > self._peak_ram:
            self._peak_ram = total_ram
        if total_cpu > self._peak_cpu:
            self._peak_cpu = total_cpu
        if total_disk > self._peak_disk:
            self._peak_disk = total_disk

    async def _sample_loop(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.to_thread(self._collect_once)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    async def stop(self) -> JobResourceMetrics:
        self._stop_event.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

        # Última lectura para capturar pico final en trabajos muy cortos
        if self._samples == 0:
            await asyncio.to_thread(self._collect_once)

        count = max(1, self._samples)
        return JobResourceMetrics(
            peak_ram_bytes=self._peak_ram,
            avg_ram_bytes=int(self._sum_ram / count),
            peak_cpu_pct=round(self._peak_cpu, 1),
            avg_cpu_pct=round(self._sum_cpu / count, 1),
            peak_disk_bytes=self._peak_disk,
            samples=self._samples,
        )
