"""Monitor de consumo real de RAM, CPU y disco por video + telemetría instantánea /live.

Muestrea el árbol completo de procesos del contenedor (Python + ffmpeg/ffprobe)
y el espacio en disco usando os.scandir rápido, en escala Pelican/Pterodactyl
(100% = 1 vCPU).
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import shutil
import time
from dataclasses import dataclass, field
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


@dataclass(slots=True)
class LiveProcUsage:
    cpu_pct: float = 0.0
    ram_bytes: int = 0


@dataclass(slots=True)
class LiveSnapshot:
    bot_tree_cpu_pct: float
    host_cpu_pct: float
    bot_tree_ram_bytes: int
    host_ram_used_bytes: int
    host_ram_total_bytes: int
    temp_disk_bytes: int
    cache_disk_bytes: int
    cache_count: int
    disk_free_bytes: int
    uptime_sec: float
    per_job: dict[str, LiveProcUsage] = field(default_factory=dict)


def fast_dir_bytes(path: Path | None) -> int:
    """Calcula el tamaño de una carpeta/archivo usando os.scandir (bajo overhead)."""
    if path is None:
        return 0
    try:
        st = path.stat()
    except OSError:
        return 0
    if not os.path.isdir(path):
        return int(st.st_size)

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


def _sample_live_sync(
    work_dir: Path,
    links_dir: Path,
    bot_start_time: float,
    job_pids: dict[str, int],
) -> LiveSnapshot:
    root = psutil.Process()
    procs: list[psutil.Process] = [root]
    with contextlib.suppress(psutil.Error):
        procs.extend(root.children(recursive=True))

    proc_by_pid: dict[int, psutil.Process] = {p.pid: p for p in procs}
    # Cebar contadores de CPU
    psutil.cpu_percent(interval=None)
    for p in procs:
        with contextlib.suppress(psutil.Error):
            p.cpu_percent(interval=None)

    time.sleep(0.25)

    host_cpu = float(psutil.cpu_percent(interval=None))
    total_cpu = 0.0
    total_ram = 0
    pid_metrics: dict[int, LiveProcUsage] = {}

    max_sane_cpu = float(max(1, os.cpu_count() or 16) * 100)
    for pid, p in proc_by_pid.items():
        try:
            cpu_v = min(max_sane_cpu, max(0.0, float(p.cpu_percent(interval=None))))
            ram_v = int(p.memory_info().rss)
            total_cpu += cpu_v
            total_ram += ram_v
            pid_metrics[pid] = LiveProcUsage(cpu_pct=round(cpu_v, 1), ram_bytes=ram_v)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    total_cpu = min(max_sane_cpu, total_cpu)

    per_job: dict[str, LiveProcUsage] = {}
    for jid, pid in job_pids.items():
        if pid in pid_metrics:
            per_job[jid] = pid_metrics[pid]

    vm = psutil.virtual_memory()
    disk = shutil.disk_usage(work_dir)
    temp_bytes = fast_dir_bytes(work_dir) + fast_dir_bytes(links_dir)
    cache_root = work_dir / "_cache"
    cache_bytes = fast_dir_bytes(cache_root)
    cache_count = 0
    if cache_root.exists():
        with contextlib.suppress(OSError):
            cache_count = sum(1 for _ in os.scandir(cache_root))

    return LiveSnapshot(
        bot_tree_cpu_pct=round(total_cpu, 1),
        host_cpu_pct=round(host_cpu, 1),
        bot_tree_ram_bytes=total_ram,
        host_ram_used_bytes=int(vm.used),
        host_ram_total_bytes=int(vm.total),
        temp_disk_bytes=temp_bytes,
        cache_disk_bytes=cache_bytes,
        cache_count=cache_count,
        disk_free_bytes=int(disk.free),
        uptime_sec=max(0.0, time.time() - bot_start_time),
        per_job=per_job,
    )


async def sample_live_resources(
    work_dir: Path,
    links_dir: Path,
    bot_start_time: float,
    job_pids: dict[str, int],
) -> LiveSnapshot:
    """Toma una fotografía no bloqueante de CPU, RAM, disco y procesos FFmpeg para /live."""
    return await asyncio.to_thread(
        _sample_live_sync,
        work_dir,
        links_dir,
        bot_start_time,
        dict(job_pids),
    )


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
        self._last_cpu_ts: dict[int, float] = {}
        self._max_sane_cpu = float(max(1, os.cpu_count() or 16) * 100)

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
        now = time.monotonic()
        with contextlib.suppress(psutil.Error):
            self._root_proc.cpu_percent(interval=None)
        self._tracked_procs[self._root_proc.pid] = self._root_proc
        self._last_cpu_ts[self._root_proc.pid] = now
        self._task = asyncio.create_task(self._sample_loop())

    def _collect_once(self) -> None:
        now = time.monotonic()
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
                # Primera vez que vemos este PID (ej. FFmpeg recién creado):
                # solo cebamos cpu_percent y medimos RAM; la CPU se leerá en el siguiente tick
                # para evitar picos artificiales (>1000%) por medir dos veces en microsegundos.
                try:
                    proc.cpu_percent(interval=None)
                    total_ram += int(proc.memory_info().rss)
                    self._tracked_procs[pid] = proc
                    self._last_cpu_ts[pid] = now
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
                continue

            try:
                mem = cached.memory_info()
                total_ram += int(mem.rss)
                last_ts = self._last_cpu_ts.get(pid, 0.0)
                if now - last_ts >= 0.25:
                    cpu_val = float(cached.cpu_percent(interval=None))
                    self._last_cpu_ts[pid] = now
                    if 0.0 <= cpu_val <= self._max_sane_cpu:
                        total_cpu += cpu_val
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._tracked_procs.pop(pid, None)
                self._last_cpu_ts.pop(pid, None)

        for stale_pid in list(self._tracked_procs.keys()):
            if stale_pid not in alive_pids:
                self._tracked_procs.pop(stale_pid, None)
                self._last_cpu_ts.pop(stale_pid, None)

        total_cpu = min(self._max_sane_cpu, total_cpu)

        seen_paths: set[Path] = set()
        total_disk = 0
        for p in self._watch_dirs:
            rp = p.resolve()
            if rp not in seen_paths:
                seen_paths.add(rp)
                total_disk += fast_dir_bytes(rp)

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
