"""Gestor de cola FIFO asíncrona, guardia de disco, herramientas FFmpeg,
caché de sesión, telemetría /live y Auto-Log automático de incidencias.

Si el archivo resultante pesa ≤ 1990 MiB → sube nativamente a Telegram con Hydrogram.
Si pesa 2–4 GB y hay cuenta Premium vinculada → sube vía cuenta Premium.
Si pesa > 1990 MiB sin Premium → sube por streaming al proveedor de enlaces y ofrece
re-compresión en 1 toque a 1900 MB (2-Pass).
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from hydrogram import Client
from hydrogram.types import InputMediaPhoto, Message

from config import (
    TELEGRAM_MAX_UPLOAD_BYTES,
    TELEGRAM_PREMIUM_MAX_UPLOAD_BYTES,
    BotConfig,
)
from core.database import Database
from core.fast_transfer import fast_download_media, fast_send_video_or_doc
from core.ffmpeg_engine import (
    CompressionOptions,
    EncodeProgress,
    JobCancelledError,
    compress_video,
    extract_thumbnail,
)
from core.job_log_capture import JobIssueCollector, start_job_capture, stop_job_capture
from core.media_probe import MediaInfo, format_mediainfo_report, probe_media
from core.media_tools import (
    extract_audio,
    generate_hd_screenshots,
    generate_screenshot_grid,
    remux_video,
)
from core.resource_monitor import (
    JobResourceMetrics,
    JobResourceMonitor,
    fast_dir_bytes,
)
from core.ui_helpers import (
    DIVIDER,
    ProgressUpdater,
    build_job_cancel_keyboard,
    build_large_file_link_keyboard,
    human_bytes,
    human_duration,
    progress_bar,
    safe_caption,
)
from core.uploader import DirectLinkStore, download_from_url, upload_large_file
from core.user_session import UserSessionManager

logger = logging.getLogger(__name__)

ActionKind = Literal[
    "compress",
    "download_only",
    "mediainfo",
    "screenshots_grid",
    "screenshots_hd",
    "remux_mp4",
    "remux_mkv",
    "extract_opus",
    "extract_mp3",
]


@dataclass(slots=True)
class RecompressSource:
    token: str
    user_id: int
    chat_id: int
    source_chat_id: int | None
    source_message_id: int | None
    source_url: str | None
    filename: str
    base_options: CompressionOptions
    user_label: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class ActiveSessionInfo:
    session_id: str
    user_id: int
    user_label: str
    filename: str
    created_at: float


@dataclass(slots=True)
class CompressionJob:
    job_id: str
    user_id: int
    chat_id: int
    status_message: Message
    filename: str
    options: CompressionOptions
    source_message: Message | None = None
    source_url: str | None = None
    download_max_height: int | None = None
    expected_size: int = 0
    session_id: str | None = None
    keep_cached_source: bool = False
    user_label: str = ""
    action_kind: ActionKind = "compress"
    on_media_probed: Callable[[MediaInfo], Awaitable[None]] | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    phase: str = "En cola"
    progress_pct: float = 0.0
    progress_detail: str = ""
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    active_proc: asyncio.subprocess.Process | None = None

    @property
    def action_badge(self) -> str:
        mapping = {
            "download_only": "Descarga Directa",
            "mediainfo": "MediaInfo",
            "screenshots_grid": "Cuadrícula 3×3",
            "screenshots_hd": "4 Capturas HD",
            "remux_mp4": "Remux MP4",
            "remux_mkv": "Remux MKV",
            "extract_opus": "Audio Opus",
            "extract_mp3": "Audio MP3",
        }
        if self.action_kind in mapping:
            return mapping[self.action_kind]
        if self.options.sample_duration:
            return f"Muestra 15s · {self.options.codec_display_label()}"
        return self.options.codec_display_label()


def dir_size_bytes(path: Path) -> int:
    return fast_dir_bytes(path)


class QueueManager:
    def __init__(
        self,
        client: Client,
        config: BotConfig,
        db: Database,
        direct_store: DirectLinkStore,
        user_session: UserSessionManager,
    ) -> None:
        self.client = client
        self.config = config
        self.db = db
        self.direct_store = direct_store
        self.user_session = user_session

        self.bot_started_at: float = time.time()
        self._queue: asyncio.Queue[CompressionJob] = asyncio.Queue()
        self._pending: list[CompressionJob] = []
        self._active: dict[str, CompressionJob] = {}
        self._all_jobs: dict[str, CompressionJob] = {}
        self._recompress_sources: dict[str, RecompressSource] = {}
        self._session_cache: dict[str, Path] = {}
        self._session_cache_ts: dict[str, float] = {}
        self._cache_locks: dict[str, asyncio.Lock] = {}
        self._sessions_provider: Callable[[], list[ActiveSessionInfo]] | None = None

        self._workers: list[asyncio.Task[None]] = []
        self._maint_task: asyncio.Task[None] | None = None
        self._running = False
        self._concurrency_limit = config.max_concurrent_jobs
        self._active_cond = asyncio.Condition()

    def set_sessions_provider(
        self,
        provider: Callable[[], list[ActiveSessionInfo]],
    ) -> None:
        self._sessions_provider = provider

    def get_active_sessions_snapshot(self) -> list[ActiveSessionInfo]:
        if self._sessions_provider is None:
            return []
        try:
            return self._sessions_provider()
        except Exception:
            return []

    def get_active_job_pids(self) -> dict[str, int]:
        pids: dict[str, int] = {}
        for jid, job in self._active.items():
            proc = job.active_proc
            if proc is not None and proc.returncode is None and proc.pid:
                pids[jid] = proc.pid
        return pids

    async def start(self) -> None:
        self._running = True
        self.bot_started_at = time.time()
        saved_conc = int(
            await self.db.get_setting("max_concurrent_jobs", str(self.config.max_concurrent_jobs))
        )
        self._concurrency_limit = max(1, min(2, saved_conc))
        for idx in range(2):
            self._workers.append(asyncio.create_task(self._worker_loop(idx + 1)))
        self._maint_task = asyncio.create_task(self._maintenance_loop())
        logger.info(
            "Cola iniciada (concurrencia=%s, hilos totales=%s).",
            self._concurrency_limit,
            self.config.max_cpu_threads,
        )

    async def stop(self) -> None:
        self._running = False
        await self.cancel_all()
        if self._maint_task is not None:
            self._maint_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maint_task
            self._maint_task = None
        for task in self._workers:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._workers.clear()

    async def _maintenance_loop(self) -> None:
        """Limpieza periódica en segundo plano: cachés expirados (>1h) y GC ligero en reposo."""
        while self._running:
            try:
                await asyncio.sleep(600)
                now = time.time()
                open_sids = {s.session_id for s in self.get_active_sessions_snapshot()}
                expired_sids = [
                    sid
                    for sid, ts in list(self._session_cache_ts.items())
                    if (now - ts > 3600) and (sid not in open_sids) and not self._session_in_use(sid)
                ]
                for sid in expired_sids:
                    await asyncio.to_thread(self.release_session_cache, sid)
                if expired_sids:
                    logger.info("Mantenimiento: liberados %d cachés de sesión expirados.", len(expired_sids))

                # Limpiar tokens de re-compresión > 24h
                for tok in [k for k, v in list(self._recompress_sources.items()) if now - v.created_at > 86400]:
                    self._recompress_sources.pop(tok, None)

                if not self._active and not self._pending:
                    gc.collect()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Error menor en tarea de mantenimiento", exc_info=True)

    async def set_concurrency(self, limit: int) -> None:
        limit = max(1, min(2, limit))
        self._concurrency_limit = limit
        await self.db.set_setting("max_concurrent_jobs", limit)
        async with self._active_cond:
            self._active_cond.notify_all()

    @property
    def concurrency_limit(self) -> int:
        return self._concurrency_limit

    def threads_per_job(self) -> int:
        return max(1, self.config.max_cpu_threads // self._concurrency_limit)

    def get_recompress_source(self, token: str) -> RecompressSource | None:
        now = time.time()
        expired = [k for k, v in self._recompress_sources.items() if now - v.created_at > 86400]
        for k in expired:
            self._recompress_sources.pop(k, None)
        return self._recompress_sources.get(token)

    def _session_in_use(self, session_id: str, exclude_job_id: str | None = None) -> bool:
        for j in self._all_jobs.values():
            if j.session_id == session_id and j.job_id != exclude_job_id:
                return True
        return False

    def has_cached_source(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        cached = self._session_cache.get(session_id)
        return cached is not None and cached.exists() and cached.stat().st_size > 0

    def has_or_pending_cache(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        return self.has_cached_source(session_id) or self._session_in_use(session_id)

    def release_session_cache(self, session_id: str | None, exclude_job_id: str | None = None) -> None:
        if not session_id:
            return
        if self._session_in_use(session_id, exclude_job_id=exclude_job_id):
            return
        self._session_cache.pop(session_id, None)
        self._session_cache_ts.pop(session_id, None)
        self._cache_locks.pop(session_id, None)
        cache_dir = self.config.work_dir / "_cache" / session_id
        shutil.rmtree(cache_dir, ignore_errors=True)

    def check_disk_budget(
        self,
        incoming_bytes: int,
        already_cached: bool = False,
        light_tool: bool = False,
    ) -> tuple[bool, str]:
        """Verifica espacio libre real del sistema y presupuesto de disco del contenedor."""
        usage = shutil.disk_usage(self.config.work_dir)
        reserve_bytes = int(self.config.disk_reserve_gb * (1024**3))
        max_bot_bytes = int(self.config.max_disk_gb * (1024**3))
        used_by_bot = fast_dir_bytes(self.config.work_dir) + fast_dir_bytes(self.config.links_dir)

        if light_tool:
            factor = 0.2 if already_cached else 1.15
        else:
            factor = 1.15 if already_cached else 2.2

        estimated_need = max(int(incoming_bytes * factor), 150 * 1024 * 1024)
        available_in_budget = max_bot_bytes - used_by_bot - reserve_bytes
        available_os = usage.free - reserve_bytes
        effective_free = min(available_in_budget, available_os)

        if effective_free < estimated_need:
            return (
                False,
                f"Espacio temporal insuficiente (disponible: {human_bytes(max(0, effective_free))}, "
                f"necesario aprox.: {human_bytes(estimated_need)}). Espera a que terminen otros trabajos o ejecuta /purge.",
            )
        return True, ""

    async def enqueue(self, job: CompressionJob) -> int:
        self._pending.append(job)
        self._all_jobs[job.job_id] = job
        await self._queue.put(job)
        return len(self._pending)

    def get_queue_snapshot(self) -> tuple[list[CompressionJob], list[CompressionJob]]:
        return list(self._active.values()), list(self._pending)

    async def cancel_job(self, job_id: str, requester_id: int) -> tuple[bool, str]:
        job = self._all_jobs.get(job_id)
        if not job:
            return False, "Ese trabajo ya terminó o no existe."
        if requester_id != job.user_id and requester_id != self.config.owner_id:
            return False, "Solo el dueño del trabajo o el administrador puede cancelarlo."

        job.cancel_event.set()
        if job.active_proc and job.active_proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                job.active_proc.terminate()
        if job in self._pending:
            self._pending.remove(job)
            self._all_jobs.pop(job_id, None)
            with contextlib.suppress(Exception):
                await job.status_message.edit_text(f"🛑 Trabajo `{job_id}` cancelado antes de iniciar.")
        return True, f"Cancelación solicitada para `{job_id}`."

    async def cancel_all(self) -> int:
        count = 0
        for job in list(self._all_jobs.values()):
            job.cancel_event.set()
            if job.active_proc and job.active_proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    job.active_proc.terminate()
            count += 1
        self._pending.clear()
        return count

    def purge_inactive_temp(self) -> int:
        active_ids = set(self._active.keys())
        active_sessions = {j.session_id for j in self._all_jobs.values() if j.session_id}
        removed = 0
        if self.config.work_dir.exists():
            for child in self.config.work_dir.iterdir():
                if child.name == "_cache" and child.is_dir():
                    for sdir in child.iterdir():
                        if sdir.name not in active_sessions:
                            shutil.rmtree(sdir, ignore_errors=True)
                            self._session_cache.pop(sdir.name, None)
                            self._session_cache_ts.pop(sdir.name, None)
                            self._cache_locks.pop(sdir.name, None)
                            removed += 1
                    continue
                if child.name not in active_ids:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        with contextlib.suppress(OSError):
                            child.unlink()
                    removed += 1
        removed += self.direct_store.purge_all()
        return removed

    async def _worker_loop(self, worker_num: int) -> None:
        while self._running:
            job = await self._queue.get()
            try:
                if job.cancel_event.is_set():
                    self._all_jobs.pop(job.job_id, None)
                    continue

                async with self._active_cond:
                    while len(self._active) >= self._concurrency_limit:
                        await self._active_cond.wait()
                    if job in self._pending:
                        self._pending.remove(job)
                    self._active[job.job_id] = job

                await self._process_job(job, worker_num)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error inesperado en worker #%s", worker_num)
            finally:
                self._active.pop(job.job_id, None)
                self._all_jobs.pop(job.job_id, None)
                async with self._active_cond:
                    self._active_cond.notify_all()
                self._queue.task_done()

    async def _send_auto_log_to_owner(
        self,
        job: CompressionJob,
        collector: JobIssueCollector,
        outcome: str,
        fatal_error: str = "",
    ) -> None:
        """Envía automáticamente al dueño cualquier error o aviso no fatal capturado en el trabajo."""
        if not collector.has_issues and not fatal_error:
            return
        if fatal_error and collector.error_count == 0:
            collector.add("ERROR", "queue_manager", fatal_error)

        if await self.db.get_setting("error_alerts", "1") != "1":
            return

        metadata = {
            "Estado": outcome,
            "Usuario": f"{job.user_label or job.user_id} ({job.user_id})",
            "Acción": job.action_badge,
            "Archivo": job.filename[:100],
            "Última fase": job.phase,
            "Duración": human_duration(time.time() - (job.started_at or job.created_at)),
        }
        job_logs_dir = self.config.data_dir / "job_logs"
        report_path = await asyncio.to_thread(collector.save_report, job_logs_dir, metadata, 30)

        icon = "❌" if outcome == "Fallido" else "⚠️"
        header = (
            f"{icon} **Auto-Log · `{job.job_id}` ({outcome})**\n"
            f"`{DIVIDER}`\n"
            f"├ **Usuario:** `{job.user_label or job.user_id}` (`{job.user_id}`)\n"
            f"├ **Archivo:** `{job.filename[:52]}`\n"
            f"├ **Acción / Fase:** `{job.action_badge}` · `{job.phase}`\n"
            f"└ **Incidencias:** `{collector.error_count}` errores · `{collector.warning_count}` avisos"
        )

        snippet = "\n".join(collector.entries[-10:])
        with contextlib.suppress(Exception):
            if len(collector.entries) <= 8 and len(snippet) <= 850:
                safe_snip = snippet.replace("```", "'''")
                await self.client.send_message(
                    self.config.owner_id,
                    f"{header}\n\n```\n{safe_snip}\n```",
                )
            else:
                await self.client.send_document(
                    chat_id=self.config.owner_id,
                    document=str(report_path),
                    caption=safe_caption(header, 1000),
                )

    async def _process_job(self, job: CompressionJob, worker_num: int) -> None:
        job.started_at = time.time()
        job_dir = self.config.work_dir / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        updater = ProgressUpdater(job.status_message, interval_sec=self.config.progress_interval_sec)
        cancel_kb = build_job_cancel_keyboard(job.job_id)

        collector, log_token = start_job_capture(job.job_id)
        monitor = JobResourceMonitor(watch_dirs=[job_dir], interval_sec=1.0)
        monitor.start()
        metrics: JobResourceMetrics | None = None
        outcome = "Completado"
        fatal_error = ""
        input_size = 0

        def _reg_proc(proc: asyncio.subprocess.Process | None) -> None:
            job.active_proc = proc

        try:
            # 0. Guardia de disco no bloqueante
            already_cached = self.has_cached_source(job.session_id)
            light_tool = job.action_kind in {
                "download_only",
                "mediainfo",
                "screenshots_grid",
                "screenshots_hd",
                "extract_opus",
                "extract_mp3",
            }
            ok_disk, disk_msg = await asyncio.to_thread(
                self.check_disk_budget, job.expected_size, already_cached, light_tool
            )
            if not ok_disk:
                outcome = "Sin espacio"
                await updater.update(f"⚠️ **Espacio insuficiente en disco**\n{disk_msg}", force=True)
                return

            # 1. Descarga de origen (o reutilización desde caché local)
            job.phase = "Descargando"
            job.progress_pct = 0.0
            input_path = await self._download_source(job, job_dir, updater, cancel_kb)
            monitor.add_watch_path(input_path.parent)

            if job.cancel_event.is_set():
                raise JobCancelledError("Trabajo cancelado.")

            input_size = input_path.stat().st_size

            # 2. Inspección completa con ffprobe
            job.phase = "Analizando"
            job.progress_pct = 0.0
            await updater.update(
                f"🔍 **Analizando Video**\n"
                f"`{DIVIDER}`\n"
                f"├ **Archivo:** `{input_path.name[:56]}`\n"
                f"└ **Peso:** `{human_bytes(input_size)}`",
                reply_markup=cancel_kb,
                force=True,
            )
            media: MediaInfo = await probe_media(input_path)
            if job.on_media_probed is not None:
                with contextlib.suppress(Exception):
                    await job.on_media_probed(media)

            # ── Descarga directa sin recomprimir ──────────────────────────────
            if job.action_kind == "download_only":
                elapsed_total = time.time() - (job.started_at or time.time())
                metrics = await monitor.stop()
                dl_summary = (
                    f"⬇️ **Descarga Completada**\n"
                    f"`{DIVIDER}`\n"
                    f"├ **Archivo:** `{input_path.name[:72]}`\n"
                    f"├ **Peso:** `{human_bytes(input_size)}`\n"
                    f"├ **Video:** `{media.width}×{media.height}` · `{media.video_codec.upper()}` · `{human_duration(media.duration)}`\n"
                    f"├ **Consumo pico:** RAM `{human_bytes(metrics.peak_ram_bytes)}` · CPU `{metrics.peak_cpu_pct:.0f}%` · Disco `{human_bytes(metrics.peak_disk_bytes)}`\n"
                    f"└ **Tiempo total:** `{human_duration(elapsed_total)}`"
                )
                await self._deliver_output(
                    job=job,
                    job_dir=job_dir,
                    output_path=input_path,
                    input_size=input_size,
                    output_size=input_size,
                    out_media=media,
                    reduction_pct=0.0,
                    elapsed_total=elapsed_total,
                    updater=updater,
                    cancel_kb=cancel_kb,
                    metrics=metrics,
                    custom_summary=dl_summary,
                    record_stats=False,
                )
                outcome = "Completado con avisos" if collector.has_issues else "Completado"
                return

            # ── Despacho de Herramientas Rápidas ──────────────────────────────
            if job.action_kind == "mediainfo":
                metrics = await monitor.stop()
                card_md, plain_txt = format_mediainfo_report(media, human_bytes, human_duration, DIVIDER)
                if len(card_md) > 3800 or len(media.audio_tracks) > 8 or len(media.subtitle_tracks) > 8:
                    txt_path = job_dir / f"{media.path.stem[:60]}_mediainfo.txt"
                    txt_path.write_text(plain_txt, encoding="utf-8")
                    await updater.update(card_md[:3800], force=True)
                    await self.client.send_document(
                        chat_id=job.chat_id,
                        document=str(txt_path),
                        caption=f"📄 MediaInfo completo de `{media.path.name[:64]}`",
                    )
                else:
                    await updater.update(card_md, force=True)
                outcome = "Completado con avisos" if collector.has_issues else "Completado"
                return

            if job.action_kind == "screenshots_grid":
                job.phase = "Generando cuadrícula 3×3"
                await updater.update(
                    f"🖼 **Generando Cuadrícula 3×3**\n"
                    f"`{DIVIDER}`\n"
                    f"└ Extrayendo 9 fotogramas con marcas de tiempo...",
                    reply_markup=cancel_kb,
                    force=True,
                )
                grid_path = await generate_screenshot_grid(
                    media=media,
                    out_dir=job_dir / "ss",
                    cancel_event=job.cancel_event,
                    register_proc=_reg_proc,
                )
                metrics = await monitor.stop()
                elapsed = time.time() - (job.started_at or time.time())
                await self.client.send_photo(
                    chat_id=job.chat_id,
                    photo=str(grid_path),
                    caption=safe_caption(
                        f"🖼 **Cuadrícula 3×3**\n"
                        f"`{DIVIDER}`\n"
                        f"├ **Archivo:** `{media.path.name[:60]}`\n"
                        f"├ **Video:** `{media.width}×{media.height}` · `{media.video_codec.upper()}` · `{human_duration(media.duration)}`\n"
                        f"└ **Tiempo:** `{human_duration(elapsed)}`"
                    ),
                )
                with contextlib.suppress(Exception):
                    await job.status_message.delete()
                outcome = "Completado con avisos" if collector.has_issues else "Completado"
                return

            if job.action_kind == "screenshots_hd":
                job.phase = "Extrayendo 4 capturas HD"
                await updater.update(
                    f"📸 **Extrayendo 4 Capturas HD**\n"
                    f"`{DIVIDER}`\n"
                    f"└ Capturando fotogramas en calidad original...",
                    reply_markup=cancel_kb,
                    force=True,
                )
                shots = await generate_hd_screenshots(
                    media=media,
                    out_dir=job_dir / "ss_hd",
                    cancel_event=job.cancel_event,
                    register_proc=_reg_proc,
                    count=4,
                )
                metrics = await monitor.stop()
                media_group = [
                    InputMediaPhoto(
                        media=str(path),
                        caption=(f"📸 `{media.path.name[:52]}` · Capturas HD ({ts})" if idx == 0 else ""),
                    )
                    for idx, (path, ts) in enumerate(shots)
                ]
                await self.client.send_media_group(chat_id=job.chat_id, media=media_group)
                with contextlib.suppress(Exception):
                    await job.status_message.delete()
                outcome = "Completado con avisos" if collector.has_issues else "Completado"
                return

            if job.action_kind in {"extract_opus", "extract_mp3"}:
                fmt: Literal["opus", "mp3"] = "opus" if job.action_kind == "extract_opus" else "mp3"
                job.phase = f"Extrayendo audio ({fmt.upper()})"
                await updater.update(
                    f"🎵 **Extrayendo Audio ({fmt.upper()})**\n"
                    f"`{DIVIDER}`\n"
                    f"├ **Archivo:** `{media.path.name[:56]}`\n"
                    f"└ **Bitrate:** `{job.options.audio_bitrate_k}k`",
                    reply_markup=cancel_kb,
                    force=True,
                )
                audio_path = await extract_audio(
                    media=media,
                    out_dir=job_dir / "audio",
                    audio_format=fmt,
                    cancel_event=job.cancel_event,
                    register_proc=_reg_proc,
                    audio_mode=job.options.audio_mode,
                    bitrate_k=job.options.audio_bitrate_k,
                    custom_name=job.options.custom_output_name,
                )
                metrics = await monitor.stop()
                elapsed = time.time() - (job.started_at or time.time())
                audio_size = audio_path.stat().st_size
                await self.client.send_audio(
                    chat_id=job.chat_id,
                    audio=str(audio_path),
                    duration=int(round(media.duration)),
                    title=audio_path.stem[:64],
                    file_name=audio_path.name,
                )
                with contextlib.suppress(Exception):
                    await job.status_message.delete()
                await self.client.send_message(
                    job.chat_id,
                    f"🎵 **Audio Extraído ({fmt.upper()})**\n"
                    f"`{DIVIDER}`\n"
                    f"├ **Archivo:** `{audio_path.name[:68]}`\n"
                    f"├ **Peso:** `{human_bytes(audio_size)}` · **Duración:** `{human_duration(media.duration)}`\n"
                    f"├ **Consumo pico:** RAM `{human_bytes(metrics.peak_ram_bytes)}` · CPU `{metrics.peak_cpu_pct:.0f}%`\n"
                    f"└ **Tiempo:** `{human_duration(elapsed)}`",
                )
                outcome = "Completado con avisos" if collector.has_issues else "Completado"
                return

            if job.action_kind in {"remux_mp4", "remux_mkv"}:
                target_fmt: Literal["mp4", "mkv"] = "mp4" if job.action_kind == "remux_mp4" else "mkv"
                job.phase = f"Remux a {target_fmt.upper()}"
                await updater.update(
                    f"🔄 **Remux Rápido a {target_fmt.upper()}**\n"
                    f"`{DIVIDER}`\n"
                    f"├ **Archivo:** `{media.path.name[:56]}`\n"
                    f"└ Copiando flujo sin recomprimir video...",
                    reply_markup=cancel_kb,
                    force=True,
                )
                output_path, remux_note = await remux_video(
                    media=media,
                    out_dir=job_dir / "remux",
                    target_format=target_fmt,
                    cancel_event=job.cancel_event,
                    register_proc=_reg_proc,
                    custom_name=job.options.custom_output_name,
                    strip_metadata=job.options.strip_metadata,
                    meta_title=job.options.meta_title,
                    meta_artist=job.options.meta_artist,
                )
                output_size = output_path.stat().st_size
                out_media = await probe_media(output_path)
                elapsed_total = time.time() - (job.started_at or time.time())
                metrics = await monitor.stop()

                remux_summary = (
                    f"🔄 **Remux Completado ({target_fmt.upper()})**\n"
                    f"`{DIVIDER}`\n"
                    f"├ **Archivo:** `{output_path.name[:72]}`\n"
                    f"├ **Peso:** `{human_bytes(output_size)}` · **Video:** `{out_media.width}×{out_media.height}` · `{out_media.video_codec.upper()}`\n"
                    f"├ **Detalle:** `{remux_note}`\n"
                    f"├ **Consumo pico:** RAM `{human_bytes(metrics.peak_ram_bytes)}` · CPU `{metrics.peak_cpu_pct:.0f}%` · Disco `{human_bytes(metrics.peak_disk_bytes)}`\n"
                    f"└ **Tiempo total:** `{human_duration(elapsed_total)}`"
                )
                await self._deliver_output(
                    job=job,
                    job_dir=job_dir,
                    output_path=output_path,
                    input_size=input_size,
                    output_size=output_size,
                    out_media=out_media,
                    reduction_pct=0.0,
                    elapsed_total=elapsed_total,
                    updater=updater,
                    cancel_kb=cancel_kb,
                    metrics=metrics,
                    custom_summary=remux_summary,
                    record_stats=False,
                )
                outcome = "Completado con avisos" if collector.has_issues else "Completado"
                return

            # ── Flujo de Compresión (AV1 / H.265 / H.264 / Sample 15s) ───────
            external_sub_path: Path | None = None
            if job.options.external_sub_file_id:
                sub_ext = Path(job.options.external_sub_name or "sub.srt").suffix.lower()
                if sub_ext not in {".srt", ".ass", ".ssa", ".vtt"}:
                    sub_ext = ".srt"
                sub_dest = job_dir / f"ext_sub{sub_ext}"
                dl_sub = await self.client.download_media(
                    job.options.external_sub_file_id,
                    file_name=str(sub_dest),
                )
                if dl_sub and Path(dl_sub).exists():
                    external_sub_path = Path(dl_sub)

            if job.options.sample_duration and job.options.sample_duration > 0:
                effective_dur = min(job.options.sample_duration, media.duration)
            elif job.options.trim_start is not None or job.options.trim_end is not None:
                t_start = max(0.0, job.options.trim_start or 0.0)
                t_end = min(job.options.trim_end, media.duration) if job.options.trim_end else media.duration
                effective_dur = max(1.0, t_end - t_start)
            else:
                effective_dur = media.duration

            threads = self.threads_per_job()
            job.phase = "Muestra 15s" if job.options.sample_duration else "Codificando"
            job.progress_pct = 0.0
            encode_start = time.monotonic()

            async def _on_encode_progress(prog: EncodeProgress) -> None:
                job.progress_pct = prog.percent
                job.progress_detail = f"{prog.speed} · {prog.fps:.1f} fps"
                elapsed = max(1.0, time.monotonic() - encode_start)
                eta = (elapsed / max(prog.percent, 0.5)) * (100.0 - prog.percent) if prog.percent > 1 else 0
                text = (
                    f"⚙️ **{prog.pass_label}**\n"
                    f"`{progress_bar(prog.percent)}`\n"
                    f"├ **Velocidad:** `{prog.speed}` · **FPS:** `{prog.fps:.1f}`\n"
                    f"├ **Progreso:** `{human_duration(prog.out_time_sec)} / {human_duration(effective_dur)}`\n"
                    f"├ **Tamaño:** `{human_bytes(prog.current_size_bytes)}` · **ETA:** `{human_duration(eta)}`\n"
                    f"└ **Trabajo:** `{job.job_id}` · `{threads} hilos` (W#{worker_num})"
                )
                await updater.update(text, reply_markup=cancel_kb)

            encode_notes: list[str] = []
            output_path = await compress_video(
                media=media,
                output_dir=job_dir / "out",
                opts=job.options,
                threads=threads,
                cancel_event=job.cancel_event,
                on_progress=_on_encode_progress,
                register_proc=_reg_proc,
                external_sub_path=external_sub_path,
                notes_out=encode_notes,
            )

            output_size = output_path.stat().st_size
            out_media = await probe_media(output_path)
            elapsed_total = time.time() - (job.started_at or time.time())
            reduction_pct = ((input_size - output_size) / max(input_size, 1)) * 100.0

            metrics = await monitor.stop()
            logger.info(
                "Consumo trabajo %s (%s): RAM pico=%s (prom=%s) | CPU pico=%.1f%% (prom=%.1f%%) | Disco pico=%s",
                job.job_id,
                output_path.name,
                human_bytes(metrics.peak_ram_bytes),
                human_bytes(metrics.avg_ram_bytes),
                metrics.peak_cpu_pct,
                metrics.avg_cpu_pct,
                human_bytes(metrics.peak_disk_bytes),
            )

            codec_tag = job.options.codec_display_label()
            if not job.options.sample_duration:
                try:
                    await self.db.record_job_metrics(
                        job_id=job.job_id,
                        filename=output_path.name,
                        codec=codec_tag,
                        resolution=f"{out_media.width}x{out_media.height}",
                        input_bytes=input_size,
                        output_bytes=output_size,
                        duration_sec=out_media.duration,
                        elapsed_sec=elapsed_total,
                        peak_ram_bytes=metrics.peak_ram_bytes,
                        avg_ram_bytes=metrics.avg_ram_bytes,
                        peak_cpu_pct=metrics.peak_cpu_pct,
                        avg_cpu_pct=metrics.avg_cpu_pct,
                        peak_disk_bytes=metrics.peak_disk_bytes,
                    )
                except Exception:
                    logger.warning("No se pudieron guardar las métricas del trabajo %s", job.job_id, exc_info=True)

            if job.options.sample_duration:
                est_full_bytes = output_size * (media.duration / max(out_media.duration, 1.0))
                sample_summary = (
                    f"🎞 **Muestra Lista ({int(round(out_media.duration))}s)**\n"
                    f"`{DIVIDER}`\n"
                    f"├ **Peso muestra:** `{human_bytes(output_size)}`\n"
                    f"├ **Estimado completo:** `~{human_bytes(est_full_bytes)}` (orig: `{human_bytes(input_size)}`)\n"
                    f"├ **Calidad:** `{out_media.width}×{out_media.height}` · `{codec_tag}` · CRF `{job.options.crf}`\n"
                    f"├ **Consumo pico:** RAM `{human_bytes(metrics.peak_ram_bytes)}` · CPU `{metrics.peak_cpu_pct:.0f}%` · Disco `{human_bytes(metrics.peak_disk_bytes)}`\n"
                    f"└ **Tiempo:** `{human_duration(elapsed_total)}`\n\n"
                    "*(Tu panel de configuración sigue abierto arriba.)*"
                )
                await self._deliver_via_telegram(
                    job=job,
                    job_dir=job_dir,
                    output_path=output_path,
                    input_size=input_size,
                    output_size=output_size,
                    out_media=out_media,
                    reduction_pct=reduction_pct,
                    elapsed_total=elapsed_total,
                    updater=updater,
                    cancel_kb=cancel_kb,
                    custom_caption=sample_summary,
                    record_stats=False,
                    metrics=metrics,
                )
                outcome = "Completado con avisos" if collector.has_issues else "Completado"
                return

            await self._deliver_output(
                job=job,
                job_dir=job_dir,
                output_path=output_path,
                input_size=input_size,
                output_size=output_size,
                out_media=out_media,
                reduction_pct=reduction_pct,
                elapsed_total=elapsed_total,
                updater=updater,
                cancel_kb=cancel_kb,
                metrics=metrics,
                encode_notes=encode_notes,
            )
            outcome = "Completado con avisos" if collector.has_issues else "Completado"

        except JobCancelledError:
            outcome = "Cancelado"
            await updater.update(f"🛑 **Trabajo `{job.job_id}` cancelado.** Archivos temporales limpiados.", force=True)
        except Exception as exc:
            outcome = "Fallido"
            fatal_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Error procesando trabajo %s (fase=%s, archivo=%s)", job.job_id, job.phase, job.filename)
            logs_hint = "\n\n💡 *El Auto-Log se guardó y envió al dueño; también puedes usar `/logs`.*" if job.user_id == self.config.owner_id else ""
            await updater.update(
                f"❌ **Error en Trabajo `{job.job_id}`**\n"
                f"`{DIVIDER}`\n"
                f"├ **Fase:** `{job.phase}`\n"
                f"└ **Detalle:** `{str(exc)[:550]}`{logs_hint}",
                force=True,
            )
        finally:
            if metrics is None:
                with contextlib.suppress(Exception):
                    await monitor.stop()
            stop_job_capture(job.job_id, log_token)
            if outcome != "Cancelado":
                await self._send_auto_log_to_owner(job, collector, outcome, fatal_error)
            await asyncio.to_thread(shutil.rmtree, job_dir, True)
            if job.session_id and not job.keep_cached_source:
                await asyncio.to_thread(self.release_session_cache, job.session_id, job.job_id)
            if input_size >= 200 * 1024 * 1024 or job.options.codec == "libsvtav1":
                gc.collect()

    async def _deliver_output(
        self,
        job: CompressionJob,
        job_dir: Path,
        output_path: Path,
        input_size: int,
        output_size: int,
        out_media: MediaInfo,
        reduction_pct: float,
        elapsed_total: float,
        updater: ProgressUpdater,
        cancel_kb: Any,
        metrics: JobResourceMetrics | None = None,
        custom_summary: str | None = None,
        record_stats: bool = True,
        encode_notes: list[str] | None = None,
    ) -> None:
        dump_id_raw = await self.db.get_setting("dump_chat_id", str(self.config.dump_chat_id or 0))
        dump_chat_id = int(dump_id_raw) if dump_id_raw.lstrip("-").isdigit() else 0
        can_use_premium = (
            output_size <= TELEGRAM_PREMIUM_MAX_UPLOAD_BYTES
            and self.user_session.is_connected
            and self.user_session.is_premium
            and (dump_chat_id != 0 or job.user_id == self.config.owner_id)
        )

        if output_size <= TELEGRAM_MAX_UPLOAD_BYTES:
            await self._deliver_via_telegram(
                job=job,
                job_dir=job_dir,
                output_path=output_path,
                input_size=input_size,
                output_size=output_size,
                out_media=out_media,
                reduction_pct=reduction_pct,
                elapsed_total=elapsed_total,
                updater=updater,
                cancel_kb=cancel_kb,
                custom_caption=custom_summary,
                record_stats=record_stats,
                metrics=metrics,
                encode_notes=encode_notes,
            )
        elif can_use_premium:
            await self._deliver_via_premium_account(
                job=job,
                job_dir=job_dir,
                output_path=output_path,
                input_size=input_size,
                output_size=output_size,
                out_media=out_media,
                reduction_pct=reduction_pct,
                elapsed_total=elapsed_total,
                dump_chat_id=dump_chat_id,
                updater=updater,
                cancel_kb=cancel_kb,
                custom_caption=custom_summary,
                record_stats=record_stats,
                metrics=metrics,
                encode_notes=encode_notes,
            )
        else:
            await self._deliver_via_link(
                job=job,
                output_path=output_path,
                input_size=input_size,
                output_size=output_size,
                out_media=out_media,
                reduction_pct=reduction_pct,
                elapsed_total=elapsed_total,
                updater=updater,
                cancel_kb=cancel_kb,
                metrics=metrics,
                encode_notes=encode_notes,
            )

    async def _deliver_via_premium_account(
        self,
        job: CompressionJob,
        job_dir: Path,
        output_path: Path,
        input_size: int,
        output_size: int,
        out_media: MediaInfo,
        reduction_pct: float,
        elapsed_total: float,
        dump_chat_id: int,
        updater: ProgressUpdater,
        cancel_kb: Any,
        custom_caption: str | None = None,
        record_stats: bool = True,
        metrics: JobResourceMetrics | None = None,
        encode_notes: list[str] | None = None,
    ) -> None:
        assert self.user_session.client is not None
        job.phase = "Subiendo (Premium)"
        job.progress_pct = 0.0
        up_start = time.monotonic()

        async def _up_progress(current: int, total: int) -> None:
            if job.cancel_event.is_set():
                raise JobCancelledError("Subida cancelada.")
            elapsed = max(0.5, time.monotonic() - up_start)
            speed_bps = current / elapsed
            pct = (current / max(total, 1)) * 100.0
            job.progress_pct = pct
            job.progress_detail = f"{human_bytes(speed_bps)}/s"
            eta = (total - current) / max(speed_bps, 1.0)
            await updater.update(
                f"⭐ **Subiendo (Premium 2–4 GB)**\n"
                f"`{progress_bar(pct)}`\n"
                f"├ **Subido:** `{human_bytes(current)}` / `{human_bytes(total)}`\n"
                f"└ **Velocidad:** `{human_bytes(speed_bps)}/s` · **ETA:** `{human_duration(eta)}`",
                reply_markup=cancel_kb,
            )

        thumb_path = await self._prepare_thumbnail(
            job.user_id, output_path, out_media.duration, job_dir
        )
        orig_caption = (
            safe_caption(job.source_message.caption)
            if job.source_message and job.source_message.caption
            else ""
        )
        if custom_caption is not None:
            summary_msg = custom_caption
        else:
            sign = "-" if reduction_pct >= 0 else "+"
            notes_line = (
                f"├ **Motor Pro:** `{' · '.join(encode_notes)}`\n"
                if encode_notes
                else ""
            )
            usage_line = (
                f"├ **Consumo pico:** RAM `{human_bytes(metrics.peak_ram_bytes)}` · CPU `{metrics.peak_cpu_pct:.0f}%` · Disco `{human_bytes(metrics.peak_disk_bytes)}`\n"
                if metrics is not None
                else ""
            )
            summary_msg = (
                f"✅ **Compresión Completada (Premium)**\n"
                f"`{DIVIDER}`\n"
                f"├ **Archivo:** `{output_path.name[:72]}`\n"
                f"├ **Peso:** `{human_bytes(input_size)}` → `{human_bytes(output_size)}` (`{sign}{abs(reduction_pct):.1f}%`)\n"
                f"├ **Video:** `{out_media.width}×{out_media.height}` · `{job.options.codec_display_label()}` · `{human_duration(out_media.duration)}`\n"
                f"{notes_line}"
                f"{usage_line}"
                f"└ **Tiempo total:** `{human_duration(elapsed_total)}`"
            )

        target_peer: int | str = dump_chat_id if dump_chat_id != 0 else "me"
        send_as_doc = job.options.send_as_document or output_path.suffix.lower() == ".mkv"

        sent_msg = await fast_send_video_or_doc(
            client=self.user_session.client,
            chat_id=target_peer,
            file_path=output_path,
            duration=int(round(out_media.duration)),
            width=out_media.width,
            height=out_media.height,
            thumb_path=thumb_path,
            caption=orig_caption,
            send_as_doc=send_as_doc,
            cancel_event=job.cancel_event,
            on_progress=_up_progress,
            workers=self.config.fast_transfer_workers,
        )

        if dump_chat_id != 0:
            await self.client.copy_message(
                chat_id=job.chat_id,
                from_chat_id=dump_chat_id,
                message_id=sent_msg.id,
            )
            with contextlib.suppress(Exception):
                await job.status_message.delete()
            await self.client.send_message(job.chat_id, summary_msg)
        else:
            await updater.update(
                f"⭐ **Enviado a Mensajes Guardados**\n\n{summary_msg}",
                force=True,
            )

        if record_stats:
            await self.db.record_job_completion(input_size, output_size, delivered_via_link=False)

    async def _download_source(
        self,
        job: CompressionJob,
        job_dir: Path,
        updater: ProgressUpdater,
        cancel_kb: Any,
    ) -> Path:
        if job.session_id:
            lock = self._cache_locks.setdefault(job.session_id, asyncio.Lock())
            async with lock:
                cached = self._session_cache.get(job.session_id)
                if cached is not None and cached.exists() and cached.stat().st_size > 0:
                    self._session_cache_ts[job.session_id] = time.time()
                    logger.info("Reutilizando video desde caché de sesión %s: %s", job.session_id, cached.name)
                    await updater.update(
                        f"⚡ **Video Listo en Caché Local**\n"
                        f"`{DIVIDER}`\n"
                        f"├ **Archivo:** `{cached.name[:56]}`\n"
                        f"└ Sin re-descargar · iniciando `{job.action_badge}`...",
                        reply_markup=cancel_kb,
                        force=True,
                    )
                    return cached

                cache_dir = self.config.work_dir / "_cache" / job.session_id
                cache_dir.mkdir(parents=True, exist_ok=True)
                downloaded = await self._perform_download(job, cache_dir, updater, cancel_kb)
                self._session_cache[job.session_id] = downloaded
                self._session_cache_ts[job.session_id] = time.time()
                return downloaded

        return await self._perform_download(job, job_dir / "in", updater, cancel_kb)

    async def _perform_download(
        self,
        job: CompressionJob,
        dest_dir: Path,
        updater: ProgressUpdater,
        cancel_kb: Any,
    ) -> Path:
        dl_start = time.monotonic()

        async def _dl_progress(current: int, total: int) -> None:
            if job.cancel_event.is_set():
                raise JobCancelledError("Descarga cancelada.")
            elapsed = max(0.5, time.monotonic() - dl_start)
            speed_bps = current / elapsed
            pct = (current / total * 100.0) if total > 0 else 0.0
            job.progress_pct = pct
            job.progress_detail = f"{human_bytes(speed_bps)}/s"
            eta = ((total - current) / max(speed_bps, 1.0)) if total > current else 0.0
            bar_str = progress_bar(pct) if total > 0 else f"Descargados {human_bytes(current)}"
            await updater.update(
                f"⬇️ **Descargando Video**\n"
                f"`{bar_str}`\n"
                f"├ **Progreso:** `{human_bytes(current)}` / `{human_bytes(total) if total else 'Desc.'}`\n"
                f"└ **Velocidad:** `{human_bytes(speed_bps)}/s` · **ETA:** `{human_duration(eta)}`",
                reply_markup=cancel_kb,
            )

        if job.source_url:
            max_allow = int((self.config.max_disk_gb - self.config.disk_reserve_gb) * (1024**3) * 0.48)
            return await download_from_url(
                url=job.source_url,
                dest_dir=dest_dir,
                max_allowed_bytes=max_allow,
                cancel_event=job.cancel_event,
                on_progress=_dl_progress,
                max_height=job.download_max_height,
            )

        assert job.source_message is not None
        dest_dir.mkdir(parents=True, exist_ok=True)
        return await fast_download_media(
            client=self.client,
            message=job.source_message,
            dest_path=dest_dir / Path(job.filename).name,
            cancel_event=job.cancel_event,
            on_progress=_dl_progress,
            workers=self.config.fast_transfer_workers,
        )

    async def _prepare_thumbnail(
        self,
        user_id: int,
        video_path: Path,
        duration: float,
        job_dir: Path,
    ) -> str | None:
        custom_thumb_id = await self.db.get_custom_thumb(user_id)
        if custom_thumb_id:
            custom_path = job_dir / "custom_thumb.jpg"
            try:
                dl = await self.client.download_media(custom_thumb_id, file_name=str(custom_path))
                if dl and Path(dl).exists():
                    return str(dl)
            except Exception as exc:
                logger.warning("No se pudo descargar la miniatura personalizada: %s", exc)

        auto_thumb = await extract_thumbnail(video_path, job_dir / "auto_thumb.jpg", duration)
        return str(auto_thumb) if auto_thumb else None

    async def _deliver_via_telegram(
        self,
        job: CompressionJob,
        job_dir: Path,
        output_path: Path,
        input_size: int,
        output_size: int,
        out_media: MediaInfo,
        reduction_pct: float,
        elapsed_total: float,
        updater: ProgressUpdater,
        cancel_kb: Any,
        custom_caption: str | None = None,
        record_stats: bool = True,
        metrics: JobResourceMetrics | None = None,
        encode_notes: list[str] | None = None,
    ) -> None:
        job.phase = "Subiendo a Telegram"
        job.progress_pct = 0.0
        up_start = time.monotonic()

        async def _up_progress(current: int, total: int) -> None:
            if job.cancel_event.is_set():
                raise JobCancelledError("Subida cancelada.")
            elapsed = max(0.5, time.monotonic() - up_start)
            speed_bps = current / elapsed
            pct = (current / max(total, 1)) * 100.0
            job.progress_pct = pct
            job.progress_detail = f"{human_bytes(speed_bps)}/s"
            eta = (total - current) / max(speed_bps, 1.0)
            await updater.update(
                f"⬆️ **Subiendo a Telegram**\n"
                f"`{progress_bar(pct)}`\n"
                f"├ **Subido:** `{human_bytes(current)}` / `{human_bytes(total)}`\n"
                f"└ **Velocidad:** `{human_bytes(speed_bps)}/s` · **ETA:** `{human_duration(eta)}`",
                reply_markup=cancel_kb,
            )

        thumb_path = await self._prepare_thumbnail(
            job.user_id, output_path, out_media.duration, job_dir
        )

        video_caption = (
            ""
            if job.options.sample_duration
            else (
                safe_caption(job.source_message.caption)
                if job.source_message and job.source_message.caption
                else ""
            )
        )

        if custom_caption is not None:
            summary_msg = custom_caption
        else:
            sign = "-" if reduction_pct >= 0 else "+"
            notes_line = (
                f"├ **Motor Pro:** `{' · '.join(encode_notes)}`\n"
                if encode_notes
                else ""
            )
            usage_line = (
                f"├ **Consumo pico:** RAM `{human_bytes(metrics.peak_ram_bytes)}` · CPU `{metrics.peak_cpu_pct:.0f}%` · Disco `{human_bytes(metrics.peak_disk_bytes)}`\n"
                if metrics is not None
                else ""
            )
            summary_msg = (
                f"✅ **Compresión Completada**\n"
                f"`{DIVIDER}`\n"
                f"├ **Archivo:** `{output_path.name[:72]}`\n"
                f"├ **Peso:** `{human_bytes(input_size)}` → `{human_bytes(output_size)}` (`{sign}{abs(reduction_pct):.1f}%`)\n"
                f"├ **Video:** `{out_media.width}×{out_media.height}` · `{job.options.codec_display_label()}` · `{human_duration(out_media.duration)}`\n"
                f"{notes_line}"
                f"{usage_line}"
                f"└ **Tiempo total:** `{human_duration(elapsed_total)}`"
            )

        send_as_doc = job.options.send_as_document or output_path.suffix.lower() == ".mkv"
        await fast_send_video_or_doc(
            client=self.client,
            chat_id=job.chat_id,
            file_path=output_path,
            duration=int(round(out_media.duration)),
            width=out_media.width,
            height=out_media.height,
            thumb_path=thumb_path,
            caption=video_caption,
            send_as_doc=send_as_doc,
            cancel_event=job.cancel_event,
            on_progress=_up_progress,
            workers=self.config.fast_transfer_workers,
        )

        if record_stats:
            await self.db.record_job_completion(input_size, output_size, delivered_via_link=False)
        with contextlib.suppress(Exception):
            await job.status_message.delete()
        with contextlib.suppress(Exception):
            await self.client.send_message(job.chat_id, summary_msg)

    async def _deliver_via_link(
        self,
        job: CompressionJob,
        output_path: Path,
        input_size: int,
        output_size: int,
        out_media: MediaInfo,
        reduction_pct: float,
        elapsed_total: float,
        updater: ProgressUpdater,
        cancel_kb: Any,
        metrics: JobResourceMetrics | None = None,
        encode_notes: list[str] | None = None,
    ) -> None:
        job.phase = "Subiendo enlace (>2 GB)"
        job.progress_pct = 0.0
        preferred = await self.db.get_setting("large_file_uploader", self.config.large_file_uploader)
        up_start = time.monotonic()

        await updater.update(
            f"☁️ **Subiendo Video (>2 GB)**\n"
            f"`{DIVIDER}`\n"
            f"├ **Peso:** `{human_bytes(output_size)}`\n"
            f"└ Generando enlace de descarga directa...",
            reply_markup=cancel_kb,
            force=True,
        )

        async def _link_progress(current: int, total: int) -> None:
            elapsed = max(0.5, time.monotonic() - up_start)
            speed_bps = current / elapsed
            pct = (current / max(total, 1)) * 100.0
            job.progress_pct = pct
            job.progress_detail = f"{human_bytes(speed_bps)}/s"
            eta = (total - current) / max(speed_bps, 1.0)
            await updater.update(
                f"☁️ **Subiendo Enlace Directo (>2 GB)**\n"
                f"`{progress_bar(pct)}`\n"
                f"├ **Subido:** `{human_bytes(current)}` / `{human_bytes(total)}`\n"
                f"└ **Velocidad:** `{human_bytes(speed_bps)}/s` · **ETA:** `{human_duration(eta)}`",
                reply_markup=cancel_kb,
            )

        result = await upload_large_file(
            file_path=output_path,
            preferred_provider=preferred,
            config=self.config,
            direct_store=self.direct_store,
            cancel_event=job.cancel_event,
            on_progress=_link_progress,
        )

        recomp_token = secrets.token_hex(6)
        recomp_opts = job.options.clone()
        recomp_opts.target_mb = 1900.0
        recomp_opts.sample_duration = None
        self._recompress_sources[recomp_token] = RecompressSource(
            token=recomp_token,
            user_id=job.user_id,
            chat_id=job.chat_id,
            source_chat_id=job.source_message.chat.id if job.source_message else None,
            source_message_id=job.source_message.id if job.source_message else None,
            source_url=job.source_url,
            filename=job.filename,
            base_options=recomp_opts,
            user_label=job.user_label,
        )

        sign = "-" if reduction_pct >= 0 else "+"
        notes_line = (
            f"├ **Motor Pro:** `{' · '.join(encode_notes)}`\n"
            if encode_notes
            else ""
        )
        usage_line = (
            f"├ **Consumo pico:** RAM `{human_bytes(metrics.peak_ram_bytes)}` · CPU `{metrics.peak_cpu_pct:.0f}%` · Disco `{human_bytes(metrics.peak_disk_bytes)}`\n"
            if metrics is not None
            else ""
        )
        text = (
            f"🔗 **Video Listo (Enlace >2 GB)**\n"
            f"`{DIVIDER}`\n"
            f"├ **Archivo:** `{output_path.name[:64]}`\n"
            f"├ **Peso:** `{human_bytes(input_size)}` → `{human_bytes(output_size)}` (`{sign}{abs(reduction_pct):.1f}%`)\n"
            f"├ **Video:** `{out_media.width}×{out_media.height}` · `{job.options.codec_display_label()}` · `{human_duration(out_media.duration)}`\n"
            f"├ **Proveedor:** `{result.provider}`\n"
            f"{notes_line}"
            f"{usage_line}"
            f"└ **Tiempo total:** `{human_duration(elapsed_total)}`\n\n"
            f"📥 **Enlace:** {result.url}\n"
            f"ℹ️ *{result.expires_note}*"
        )

        await self.db.record_job_completion(input_size, output_size, delivered_via_link=True)
        await updater.update(
            text,
            reply_markup=build_large_file_link_keyboard(result.url, recomp_token),
            force=True,
        )
