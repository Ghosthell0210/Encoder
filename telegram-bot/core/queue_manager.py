"""Gestor de cola FIFO asíncrona, guardia de disco (30 GiB) y entrega híbrida.

Si el archivo comprimido pesa ≤ 1990 MiB → sube nativamente a Telegram con Hydrogram.
Si pesa > 1990 MiB → sube por streaming al proveedor de enlaces y ofrece re-compresión
en 1 toque a 1900 MB (2-Pass) para que quepa en Telegram.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from hydrogram import Client
from hydrogram.types import Message

from config import (
    TELEGRAM_MAX_UPLOAD_BYTES,
    TELEGRAM_PREMIUM_MAX_UPLOAD_BYTES,
    BotConfig,
)
from core.database import Database
from core.ffmpeg_engine import (
    CompressionOptions,
    EncodeProgress,
    JobCancelledError,
    compress_video,
    extract_thumbnail,
)
from core.fast_transfer import fast_download_media, fast_send_video_or_doc
from core.media_probe import MediaInfo, probe_media
from core.ui_helpers import (
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
    created_at: float = field(default_factory=time.time)


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
    expected_size: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    phase: str = "En cola"
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    active_proc: asyncio.subprocess.Process | None = None


def dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if item.is_file():
            with contextlib.suppress(OSError):
                total += item.stat().st_size
    return total


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

        self._queue: asyncio.Queue[CompressionJob] = asyncio.Queue()
        self._pending: list[CompressionJob] = []
        self._active: dict[str, CompressionJob] = {}
        self._all_jobs: dict[str, CompressionJob] = {}
        self._recompress_sources: dict[str, RecompressSource] = {}

        self._workers: list[asyncio.Task[None]] = []
        self._running = False
        self._concurrency_limit = config.max_concurrent_jobs
        self._active_sem_lock = asyncio.Lock()
        self._active_cond = asyncio.Condition()

    async def start(self) -> None:
        self._running = True
        saved_conc = int(await self.db.get_setting("max_concurrent_jobs", str(self.config.max_concurrent_jobs)))
        self._concurrency_limit = max(1, min(2, saved_conc))
        # Creamos 2 corrutinas trabajadoras controladas por condición de concurrencia dinámica
        for idx in range(2):
            self._workers.append(asyncio.create_task(self._worker_loop(idx + 1)))
        logger.info(
            "Cola iniciada (concurrencia=%s, hilos totales=%s).",
            self._concurrency_limit,
            self.config.max_cpu_threads,
        )

    async def stop(self) -> None:
        self._running = False
        await self.cancel_all()
        for task in self._workers:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._workers.clear()

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
        # 400% CPU: 1 trabajo -> 4 hilos | 2 trabajos -> 2 hilos c/u
        return max(1, self.config.max_cpu_threads // self._concurrency_limit)

    def get_recompress_source(self, token: str) -> RecompressSource | None:
        # Limpiar entradas con más de 24h
        now = time.time()
        expired = [k for k, v in self._recompress_sources.items() if now - v.created_at > 86400]
        for k in expired:
            self._recompress_sources.pop(k, None)
        return self._recompress_sources.get(token)

    def check_disk_budget(self, incoming_bytes: int) -> tuple[bool, str]:
        """Verifica espacio libre real del sistema y presupuesto de 30 GiB del contenedor."""
        usage = shutil.disk_usage(self.config.work_dir)
        reserve_bytes = int(self.config.disk_reserve_gb * (1024**3))
        max_bot_bytes = int(self.config.max_disk_gb * (1024**3))
        used_by_bot = dir_size_bytes(self.config.work_dir) + dir_size_bytes(self.config.links_dir)

        # Un trabajo necesita aprox. 2.2x el peso de entrada (original + salida)
        estimated_need = max(int(incoming_bytes * 2.2), 300 * 1024 * 1024)
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
        """Elimina subcarpetas temporales huérfanas que no correspondan a trabajos activos."""
        active_ids = set(self._active.keys())
        removed = 0
        if self.config.work_dir.exists():
            for child in self.config.work_dir.iterdir():
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

    async def _process_job(self, job: CompressionJob, worker_num: int) -> None:
        job.started_at = time.time()
        job_dir = self.config.work_dir / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        updater = ProgressUpdater(job.status_message, interval_sec=self.config.progress_interval_sec)
        cancel_kb = build_job_cancel_keyboard(job.job_id)

        try:
            # 0. Guardia de disco antes de descargar
            ok_disk, disk_msg = self.check_disk_budget(job.expected_size)
            if not ok_disk:
                await updater.update(f"⚠️ **Espacio insuficiente en disco**\n{disk_msg}", force=True)
                return

            # 1. Descarga de origen (Telegram MTProto o enlace HTTP directo)
            job.phase = "Descargando"
            input_path = await self._download_source(job, job_dir, updater, cancel_kb)

            if job.cancel_event.is_set():
                raise JobCancelledError("Trabajo cancelado.")

            input_size = input_path.stat().st_size

            # 2. Inspección completa con ffprobe
            job.phase = "Analizando"
            await updater.update(
                f"🔍 **Analizando metadatos con FFprobe...**\n"
                f"• Archivo: `{input_path.name[:60]}`\n"
                f"• Peso entrada: `{human_bytes(input_size)}`",
                reply_markup=cancel_kb,
                force=True,
            )
            media: MediaInfo = await probe_media(input_path)

            # 2b. Descargar subtítulo externo (.srt / .ass) si fue adjuntado
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

            # Duración efectiva del clip (muestra 15s, recorte Trim o video completo)
            if job.options.sample_duration and job.options.sample_duration > 0:
                effective_dur = min(job.options.sample_duration, media.duration)
            elif job.options.trim_start is not None or job.options.trim_end is not None:
                t_start = max(0.0, job.options.trim_start or 0.0)
                t_end = min(job.options.trim_end, media.duration) if job.options.trim_end else media.duration
                effective_dur = max(1.0, t_end - t_start)
            else:
                effective_dur = media.duration

            # 3. Compresión FFmpeg aprovechando los hilos configurados
            threads = self.threads_per_job()
            job.phase = "Muestra 15s" if job.options.sample_duration else "Comprimiendo"
            encode_start = time.monotonic()

            async def _on_encode_progress(prog: EncodeProgress) -> None:
                elapsed = max(1.0, time.monotonic() - encode_start)
                eta = (elapsed / max(prog.percent, 0.5)) * (100.0 - prog.percent) if prog.percent > 1 else 0
                text = (
                    f"⚙️ **{prog.pass_label}** (`{threads} hilos`)\n"
                    f"`{progress_bar(prog.percent)}`\n"
                    f"• **Velocidad:** `{prog.speed}` · **FPS:** `{prog.fps:.1f}`\n"
                    f"• **Procesado:** `{human_duration(prog.out_time_sec)} / {human_duration(effective_dur)}`\n"
                    f"• **Tamaño parcial:** `{human_bytes(prog.current_size_bytes)}` · **ETA:** `{human_duration(eta)}`\n"
                    f"• **Trabajo:** `{job.job_id}` (Worker #{worker_num})"
                )
                await updater.update(text, reply_markup=cancel_kb)

            def _reg_proc(proc: asyncio.subprocess.Process | None) -> None:
                job.active_proc = proc

            output_path = await compress_video(
                media=media,
                output_dir=job_dir / "out",
                opts=job.options,
                threads=threads,
                cancel_event=job.cancel_event,
                on_progress=_on_encode_progress,
                register_proc=_reg_proc,
                external_sub_path=external_sub_path,
            )

            output_size = output_path.stat().st_size
            out_media = await probe_media(output_path)
            elapsed_total = time.time() - (job.started_at or time.time())
            reduction_pct = ((input_size - output_size) / max(input_size, 1)) * 100.0

            # 3b. Si es Muestra rápida de 15s, enviar con proyección de tamaño final y sin sumar a estadísticas
            if job.options.sample_duration:
                est_full_bytes = output_size * (media.duration / max(out_media.duration, 1.0))
                bit_tag = " 10-bit" if (job.options.bit_depth == 10 and job.options.codec == "libx265") else ""
                sample_caption = safe_caption(
                    f"🎞 **Muestra rápida ({int(round(out_media.duration))}s) — Vista previa**\n"
                    f"• **Peso muestra:** `{human_bytes(output_size)}` · **Estimado video completo:** `~{human_bytes(est_full_bytes)}` (orig: `{human_bytes(input_size)}`)\n"
                    f"• **Calidad:** `{out_media.width}×{out_media.height}` · `{out_media.video_codec.upper()}{bit_tag}` · CRF `{job.options.crf}`\n"
                    f"• **Tiempo:** `{human_duration(elapsed_total)}` *(Tu panel de configuración sigue abierto arriba)*"
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
                    custom_caption=sample_caption,
                    record_stats=False,
                )
                return

            # 4. Decisión inteligente de entrega:
            #    • ≤ 1.99 GB → Bot directo en Telegram
            #    • 1.99 GB a 3.98 GB con cuenta Premium vinculada (/login) → Cuenta Premium
            #    • > límite de Telegram → Enlace de descarga directa (GoFile / Buzzheavier)
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
                )

        except JobCancelledError:
            await updater.update(f"🛑 **Trabajo `{job.job_id}` cancelado.** Archivos temporales eliminados.", force=True)
        except Exception as exc:
            logger.exception("Error procesando trabajo %s (fase=%s, archivo=%s)", job.job_id, job.phase, job.filename)
            logs_hint = "\n\n💡 *Usa `/logs` para ver el detalle completo en `data/bot.log`.*" if job.user_id == self.config.owner_id else ""
            await updater.update(
                f"❌ **Error en el trabajo `{job.job_id}`** (`{job.phase}`)\n`{str(exc)[:600]}`{logs_hint}",
                force=True,
            )
            # Alerta automática al dueño si falló el trabajo de otro usuario y las alertas están activas
            if job.user_id != self.config.owner_id:
                with contextlib.suppress(Exception):
                    if await self.db.get_setting("error_alerts", "1") == "1":
                        await self.client.send_message(
                            self.config.owner_id,
                            f"🚨 **Alerta de error en trabajo `{job.job_id}`**\n"
                            f"• **Usuario:** `{job.user_id}`\n"
                            f"• **Fase:** `{job.phase}`\n"
                            f"• **Archivo:** `{job.filename[:60]}`\n"
                            f"• **Detalle:** `{str(exc)[:400]}`\n\n"
                            "Usa `/logs errors` o `/logs file` para ver el registro completo.",
                        )
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

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
        cancel_kb,
    ) -> None:
        assert self.user_session.client is not None
        job.phase = "Subiendo con cuenta Premium (2–4 GB)"
        up_start = time.monotonic()

        async def _up_progress(current: int, total: int) -> None:
            if job.cancel_event.is_set():
                raise JobCancelledError("Subida cancelada.")
            elapsed = max(0.5, time.monotonic() - up_start)
            speed_bps = current / elapsed
            pct = (current / max(total, 1)) * 100.0
            eta = (total - current) / max(speed_bps, 1.0)
            await updater.update(
                f"⭐ **Subiendo video (2–4 GB) vía cuenta Telegram Premium...**\n"
                f"`{progress_bar(pct)}`\n"
                f"• **Subido:** `{human_bytes(current)}` / `{human_bytes(total)}`\n"
                f"• **Velocidad:** `{human_bytes(speed_bps)}/s` · **ETA:** `{human_duration(eta)}`",
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
        sign = "-" if reduction_pct >= 0 else "+"
        bit_tag = " 10-bit" if (job.options.bit_depth == 10 and job.options.codec == "libx265") else ""
        summary_msg = (
            f"✅ **Video comprimido (Entrega Premium >2 GB)**\n"
            f"• **Archivo:** `{output_path.name[:80]}`\n"
            f"• **Original:** `{human_bytes(input_size)}` → **Final:** `{human_bytes(output_size)}` (`{sign}{abs(reduction_pct):.1f}%`)\n"
            f"• **Calidad:** `{out_media.width}×{out_media.height}` · `{out_media.video_codec.upper()}{bit_tag}` · `{human_duration(out_media.duration)}`\n"
            f"• **Tiempo total:** `{human_duration(elapsed_total)}`"
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
                f"⭐ **Video de `{human_bytes(output_size)}` enviado a tus Mensajes Guardados**\n\n{summary_msg}",
                force=True,
            )

        await self.db.record_job_completion(input_size, output_size, delivered_via_link=False)

    async def _download_source(
        self,
        job: CompressionJob,
        job_dir: Path,
        updater: ProgressUpdater,
        cancel_kb,
    ) -> Path:
        dl_start = time.monotonic()

        async def _dl_progress(current: int, total: int) -> None:
            if job.cancel_event.is_set():
                raise JobCancelledError("Descarga cancelada.")
            elapsed = max(0.5, time.monotonic() - dl_start)
            speed_bps = current / elapsed
            pct = (current / total * 100.0) if total > 0 else 0.0
            eta = ((total - current) / max(speed_bps, 1.0)) if total > current else 0.0
            bar_str = progress_bar(pct) if total > 0 else f"Descargados {human_bytes(current)}"
            await updater.update(
                f"⬇️ **Descargando video...**\n"
                f"`{bar_str}`\n"
                f"• **Transferido:** `{human_bytes(current)}` / `{human_bytes(total) if total else 'Desc.'}`\n"
                f"• **Velocidad:** `{human_bytes(speed_bps)}/s` · **ETA:** `{human_duration(eta)}`",
                reply_markup=cancel_kb,
            )

        if job.source_url:
            max_allow = int((self.config.max_disk_gb - self.config.disk_reserve_gb) * (1024**3) * 0.48)
            return await download_from_url(
                url=job.source_url,
                dest_dir=job_dir / "in",
                max_allowed_bytes=max_allow,
                cancel_event=job.cancel_event,
                on_progress=_dl_progress,
            )

        assert job.source_message is not None
        dest_dir = job_dir / "in"
        dest_dir.mkdir(parents=True, exist_ok=True)
        return await fast_download_media(
            client=self.client,
            message=job.source_message,
            dest_path=dest_dir / job.filename,
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
            except Exception:
                pass

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
        cancel_kb,
        custom_caption: str | None = None,
        record_stats: bool = True,
    ) -> None:
        job.phase = "Subiendo a Telegram"
        up_start = time.monotonic()

        async def _up_progress(current: int, total: int) -> None:
            if job.cancel_event.is_set():
                raise JobCancelledError("Subida cancelada.")
            elapsed = max(0.5, time.monotonic() - up_start)
            speed_bps = current / elapsed
            pct = (current / max(total, 1)) * 100.0
            eta = (total - current) / max(speed_bps, 1.0)
            await updater.update(
                f"⬆️ **Subiendo video comprimido a Telegram...**\n"
                f"`{progress_bar(pct)}`\n"
                f"• **Subido:** `{human_bytes(current)}` / `{human_bytes(total)}`\n"
                f"• **Velocidad:** `{human_bytes(speed_bps)}/s` · **ETA:** `{human_duration(eta)}`",
                reply_markup=cancel_kb,
            )

        thumb_path = await self._prepare_thumbnail(
            job.user_id, output_path, out_media.duration, job_dir
        )

        # El video se envía limpio con su nombre original y su pie original (si tenía)
        video_caption = (
            ""
            if job.options.sample_duration
            else (
                safe_caption(job.source_message.caption)
                if job.source_message and job.source_message.caption
                else ""
            )
        )

        # La notificación con estadísticas va en un mensaje separado justo debajo del video
        if custom_caption is not None:
            summary_msg = custom_caption
        else:
            sign = "-" if reduction_pct >= 0 else "+"
            bit_tag = " 10-bit" if (job.options.bit_depth == 10 and job.options.codec == "libx265") else ""
            summary_msg = (
                f"✅ **Video comprimido**\n"
                f"• **Archivo:** `{output_path.name[:80]}`\n"
                f"• **Original:** `{human_bytes(input_size)}` → **Final:** `{human_bytes(output_size)}` (`{sign}{abs(reduction_pct):.1f}%`)\n"
                f"• **Calidad:** `{out_media.width}×{out_media.height}` · `{out_media.video_codec.upper()}{bit_tag}` · `{human_duration(out_media.duration)}`\n"
                f"• **Tiempo total:** `{human_duration(elapsed_total)}`"
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
        cancel_kb,
    ) -> None:
        job.phase = "Generando enlace (>2 GB)"
        preferred = await self.db.get_setting("large_file_uploader", self.config.large_file_uploader)
        up_start = time.monotonic()

        await updater.update(
            f"📦 **El video comprimido pesa `{human_bytes(output_size)}`**\n"
            f"Supera el límite de 2 GB de Telegram. Subiendo para generar enlace de descarga directa...",
            reply_markup=cancel_kb,
            force=True,
        )

        async def _link_progress(current: int, total: int) -> None:
            elapsed = max(0.5, time.monotonic() - up_start)
            speed_bps = current / elapsed
            pct = (current / max(total, 1)) * 100.0
            eta = (total - current) / max(speed_bps, 1.0)
            await updater.update(
                f"☁️ **Subiendo video >2 GB para enlace directo...**\n"
                f"`{progress_bar(pct)}`\n"
                f"• **Subido:** `{human_bytes(current)}` / `{human_bytes(total)}`\n"
                f"• **Velocidad:** `{human_bytes(speed_bps)}/s` · **ETA:** `{human_duration(eta)}`",
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

        # Guardar referencia para el botón rápido "Re-comprimir a 1.90 GB (para Telegram)"
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
        )

        sign = "-" if reduction_pct >= 0 else "+"
        text = (
            f"🔗 **Video comprimido listo (Entrega por Enlace >2 GB)**\n\n"
            f"• **Archivo:** `{output_path.name[:64]}`\n"
            f"• **Original:** `{human_bytes(input_size)}` → **Final:** `{human_bytes(output_size)}` (`{sign}{abs(reduction_pct):.1f}%`)\n"
            f"• **Video:** `{out_media.width}×{out_media.height}` · `{out_media.video_codec.upper()}` · `{human_duration(out_media.duration)}`\n"
            f"• **Proveedor:** `{result.provider}`\n"
            f"• **Tiempo total:** `{human_duration(elapsed_total)}`\n\n"
            f"📥 **Enlace directo:** {result.url}\n"
            f"ℹ️ *{result.expires_note}*\n\n"
            f"💡 *Si prefieres recibirlo dentro del chat de Telegram, toca el botón inferior para re-comprimirlo en 2-Pass a 1.90 GB.*"
        )

        await self.db.record_job_completion(input_size, output_size, delivered_via_link=True)
        await updater.update(
            text,
            reply_markup=build_large_file_link_keyboard(result.url, recomp_token),
            force=True,
        )
