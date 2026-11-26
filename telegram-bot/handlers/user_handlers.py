"""Manejadores de usuario: inicio interactivo, guía organizada, recepción de videos/enlaces,
menús inline (AV1/H.265/H.264, FPS, Opus, Metadatos, Herramientas rápidas), Modo Lotes (/auto),
Muestra 15s, subtítulos externos, /mediainfo y /ss."""

from __future__ import annotations

import contextlib
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from hydrogram import Client, filters
from hydrogram.types import CallbackQuery, Message, User

from config import BotConfig
from core.database import Database
from core.ffmpeg_engine import CompressionOptions, parse_timestamp
from core.media_probe import MediaInfo
from core.queue_manager import ActionKind, ActiveSessionInfo, CompressionJob, QueueManager
from core.ui_helpers import (
    DIVIDER,
    build_advanced_keyboard,
    build_back_home_keyboard,
    build_extras_keyboard,
    build_job_cancel_keyboard,
    build_main_config_keyboard,
    build_presets_keyboard,
    build_start_keyboard,
    build_target_size_keyboard,
    build_tools_keyboard,
    build_tracks_keyboard,
    format_draft_summary,
    human_bytes,
    human_duration,
)
from core.user_session import UserSessionManager

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".ts", ".wmv"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}
URL_REGEX = re.compile(r"^https?://\S+$", re.IGNORECASE)
VALID_TOOL_KINDS: set[str] = {
    "mediainfo",
    "screenshots_grid",
    "screenshots_hd",
    "remux_mp4",
    "remux_mkv",
    "extract_opus",
    "extract_mp3",
}


@dataclass(slots=True)
class DraftSession:
    session_id: str
    user_id: int
    user_label: str
    chat_id: int
    config_message: Message
    filename: str
    file_size: int | None
    duration: float | None
    width: int | None
    height: int | None
    options: CompressionOptions
    source_message: Message | None = None
    source_url: str | None = None
    media: MediaInfo | None = None
    created_at: float = field(default_factory=time.time)


def _format_user_label(user: User | None) -> str:
    if not user:
        return "Usuario"
    if user.username:
        return f"@{user.username}"
    full = " ".join(p for p in (user.first_name, user.last_name) if p).strip()
    return full[:28] or f"ID {user.id}"


def _is_video_document(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    mime = (doc.mime_type or "").lower()
    if mime.startswith("video/"):
        return True
    name = (doc.file_name or "").lower()
    return Path(name).suffix in VIDEO_EXTENSIONS


def _is_subtitle_document(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    name = (doc.file_name or "").lower()
    return Path(name).suffix in SUBTITLE_EXTENSIONS


def _extract_video_meta(message: Message) -> tuple[str, int | None, float | None, int | None, int | None] | None:
    if message.video:
        v = message.video
        return (
            v.file_name or f"video_{message.id}.mp4",
            v.file_size,
            float(v.duration) if v.duration else None,
            v.width,
            v.height,
        )
    if message.document and _is_video_document(message):
        d = message.document
        return (
            d.file_name or f"video_{message.id}.mkv",
            d.file_size,
            None,
            None,
            None,
        )
    return None


def _format_saved_preset_line(opts: CompressionOptions) -> str:
    codec = opts.codec_display_label()
    res = f"{opts.max_height}p" if opts.max_height else "Orig"
    fps = f" {opts.target_fps}fps" if opts.target_fps else ""
    if opts.target_mb:
        mode = f"2-Pass {opts.target_mb:.0f} MB"
    elif opts.codec == "libsvtav1":
        mode = f"CRF {opts.crf}"
    else:
        mode = f"CRF {opts.crf} ({opts.speed})"
    acodec = "Copy" if opts.audio_codec == "copy" else ("Opus" if opts.audio_codec == "libopus" else "AAC")
    return f"`{codec}` · `{res}{fps}` · `{mode}` · `{acodec} {opts.audio_bitrate_k}k`"


def register_user_handlers(
    app: Client,
    config: BotConfig,
    db: Database,
    queue_mgr: QueueManager,
    user_session: UserSessionManager,
) -> None:
    sessions: dict[str, DraftSession] = {}
    # user_id -> (session_id, mode)
    awaiting_input: dict[int, tuple[str, str]] = {}

    def _cleanup_old_sessions() -> None:
        now = time.time()
        expired = [sid for sid, s in sessions.items() if now - s.created_at > 3600]
        for sid in expired:
            sessions.pop(sid, None)
            queue_mgr.release_session_cache(sid)

    def _export_open_sessions() -> list[ActiveSessionInfo]:
        _cleanup_old_sessions()
        return [
            ActiveSessionInfo(
                session_id=s.session_id,
                user_id=s.user_id,
                user_label=s.user_label,
                filename=s.filename,
                created_at=s.created_at,
            )
            for s in sessions.values()
        ]

    queue_mgr.set_sessions_provider(_export_open_sessions)

    async def _check_access(message: Message) -> bool:
        user = message.from_user
        if not user:
            return False
        if await db.is_authorized(user.id, config.owner_id):
            await db.ensure_user_profile(user.id, user.username or "")
            return True
        await message.reply_text(
            "🔒 **Acceso Restringido**\n"
            f"`{DIVIDER}`\n"
            "Este bot funciona en modo lista blanca.\n"
            f"└ **Tu ID:** `{user.id}`\n\n"
            "Compártelo con el administrador para recibir acceso."
        )
        return False

    async def _load_user_options(user_id: int) -> tuple[CompressionOptions, bool]:
        user_rec = await db.get_user(user_id)
        saved_dict = await db.get_default_options(user_id)
        opts = CompressionOptions.from_saved_dict(
            saved_dict,
            watermark_text=user_rec.watermark_text if user_rec else "",
            watermark_pos=user_rec.watermark_pos if user_rec else "br",
            send_as_document=user_rec.send_as_document if user_rec else False,
        )
        auto_mode = bool(user_rec.auto_mode) if user_rec else False
        return opts, auto_mode

    async def _render_home_text(user_id: int) -> tuple[str, bool]:
        opts, auto_mode = await _load_user_options(user_id)
        active, pending = queue_mgr.get_queue_snapshot()
        auto_tag = "ACTIVADO" if auto_mode else "Manual"
        text = (
            "🎬 **HydroCompress Bot**\n"
            f"`{DIVIDER}`\n"
            "Envíame cualquier **video**, **archivo** (hasta 2 GB) o **enlace** (`YouTube`, `TikTok`, `Instagram`, `X` o descarga directa) para comprimirlo en **AV1 / H.265 / H.264** o usar herramientas rápidas.\n\n"
            "⚙️ **Tu Perfil**\n"
            f"├ **Modo:** `{auto_tag}`\n"
            f"├ **Ajuste base:** {_format_saved_preset_line(opts)}\n"
            f"├ **Marca de agua:** `{opts.watermark_text or 'Ninguna'}`\n"
            f"└ **Cola actual:** `{len(active)}/{queue_mgr.concurrency_limit}` activos · `{len(pending)}` en espera\n\n"
            "💡 *Usa el menú `/` abajo o los botones rápidos.*"
        )
        return text, auto_mode

    def _render_help_text(is_owner: bool) -> str:
        lines = [
            "📖 **Guía de Comandos**",
            f"`{DIVIDER}`",
            "",
            "🎬 **Compresión y Herramientas**",
            "├ `/start` — Menú principal interactivo",
            "├ `/auto` — Activar/desactivar **Modo Lotes** automático",
            "├ `/mediainfo` — Ficha técnica completa (responde a un video)",
            "├ `/ss` — Cuadrícula 3×3 de capturas (responde a un video)",
            "├ `/queue` — Ver cola y trabajos activos",
            "└ `/stats` — Total comprimido y espacio ahorrado",
            "",
            "🎨 **Personalización**",
            "├ `/setthumb` — Fijar miniatura (responde a una foto)",
            "├ `/delthumb` — Volver a miniatura automática",
            "├ `/setwatermark <txt>` — Fijar marca de agua",
            "├ `/delwatermark` — Quitar marca de agua",
            "└ `/id` — Mostrar tu ID numérico",
        ]
        if is_owner:
            lines.extend(
                [
                    "",
                    "🛡 **Administración (Dueño)**",
                    "├ `/live` — Ver quién usa el bot, CPU y RAM en vivo",
                    "├ `/admin` — Panel del servidor, Auto-Log y recursos",
                    "├ `/usage [reset]` — Consumo real de RAM, CPU y disco por video",
                    "├ `/logs [n|errors|file]` — Ver o bajar `bot.log`",
                    "├ `/update` — Actualizar desde GitHub y reiniciar",
                    "├ `/users` · `/adduser <id>` · `/deluser <id>`",
                    "├ `/purge` · `/cancelall` — Limpiar / detener cola",
                    "└ `/login` · `/logout` · `/setdump <id>`",
                ]
            )
        lines.extend(
            [
                "",
                "💡 *En cada video tienes **AV1 (Rápido / Equilibrado / Máx)**, **Audio Opus**, **FPS**, **Metadatos**, **Remux MKV↔MP4**, **Extraer Audio**, **Screenshots** y **Muestra 15s** con caché compartido.*",
            ]
        )
        return "\n".join(lines)

    def _render_queue_text() -> str:
        active, pending = queue_mgr.get_queue_snapshot()
        lines = [
            "📋 **Estado de la Cola**",
            f"`{DIVIDER}`",
            f"├ **Capacidad:** `{len(active)}/{queue_mgr.concurrency_limit}` activos · `{queue_mgr.threads_per_job()}` hilos/trabajo",
            f"└ **En espera:** `{len(pending)}`",
        ]
        if active:
            lines.append("")
            lines.append("⚙️ **En ejecución**")
            for idx, j in enumerate(active):
                branch = "└" if idx == len(active) - 1 else "├"
                elapsed = human_duration(time.time() - (j.started_at or j.created_at))
                pct = f" {j.progress_pct:.0f}%" if j.progress_pct > 0 else ""
                lines.append(
                    f"{branch} `{j.job_id}` · `{j.action_badge}` · `{j.filename[:30]}` ({j.phase}{pct}, {elapsed})"
                )
        else:
            lines.append("\n• Sin trabajos activos en este momento.")

        if pending:
            lines.append("")
            lines.append(f"⏳ **Siguiente en cola ({len(pending)})**")
            shown = pending[:10]
            for idx, j in enumerate(shown, start=1):
                branch = "└" if idx == len(shown) else "├"
                lines.append(f"{branch} `{idx}.` `{j.job_id}` · `{j.action_badge}` · `{j.filename[:32]}`")
        return "\n".join(lines)

    async def _render_stats_text() -> str:
        st = await db.get_stats()
        pct = (st.total_saved_bytes / max(st.total_input_bytes, 1)) * 100.0
        return (
            "📊 **Estadísticas Globales**\n"
            f"`{DIVIDER}`\n"
            f"├ **Videos comprimidos:** `{st.total_jobs}`\n"
            f"├ **Entregas >2 GB (enlace):** `{st.total_link_deliveries}`\n"
            f"├ **Entrada procesada:** `{human_bytes(st.total_input_bytes)}`\n"
            f"├ **Salida generada:** `{human_bytes(st.total_output_bytes)}`\n"
            f"└ **Espacio ahorrado:** `{human_bytes(st.total_saved_bytes)}` (`-{pct:.1f}%`)"
        )

    async def _enqueue_auto_job(
        message: Message,
        user_id: int,
        user_label: str,
        filename: str,
        file_size: int | None,
        opts: CompressionOptions,
        source_message: Message | None = None,
        source_url: str | None = None,
    ) -> None:
        job_id = secrets.token_hex(4)
        status_msg = await message.reply_text(
            f"⚡ **Modo Lotes** · Encolando `{job_id}`...",
            quote=True,
        )
        job = CompressionJob(
            job_id=job_id,
            user_id=user_id,
            user_label=user_label,
            chat_id=message.chat.id,
            status_message=status_msg,
            filename=filename,
            options=opts,
            source_message=source_message,
            source_url=source_url,
            expected_size=file_size or (500 * 1024 * 1024),
        )
        pos = await queue_mgr.enqueue(job)
        with contextlib.suppress(Exception):
            await status_msg.edit_text(
                f"⚡ **Modo Lotes · En cola (#{pos})**\n"
                f"`{DIVIDER}`\n"
                f"├ **Archivo:** `{filename[:56]}`\n"
                f"├ **Ajuste:** {_format_saved_preset_line(opts)}\n"
                f"└ **Trabajo:** `{job_id}` · `{queue_mgr.threads_per_job()}` hilos\n\n"
                "*(Usa `/auto` para volver al modo manual)*",
                reply_markup=build_job_cancel_keyboard(job_id),
            )

    # ── Comandos informativos, perfil y accesos rápidos (/mediainfo, /ss) ────

    @app.on_message(filters.private & filters.command("start"))
    async def cmd_start(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        user_id = message.from_user.id
        is_owner = user_id == config.owner_id
        text, auto_mode = await _render_home_text(user_id)
        await message.reply_text(
            text,
            reply_markup=build_start_keyboard(is_owner=is_owner, auto_mode=auto_mode),
        )

    @app.on_message(filters.private & filters.command("help"))
    async def cmd_help(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        is_owner = bool(message.from_user and message.from_user.id == config.owner_id)
        await message.reply_text(
            _render_help_text(is_owner),
            reply_markup=build_back_home_keyboard(),
        )

    @app.on_message(filters.private & filters.command("id"))
    async def cmd_id(_client: Client, message: Message) -> None:
        if not message.from_user:
            return
        await message.reply_text(f"🆔 **Tu ID de Telegram:** `{message.from_user.id}`")

    @app.on_message(filters.private & filters.command("mediainfo"))
    async def cmd_mediainfo(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        reply = message.reply_to_message
        meta = _extract_video_meta(reply) if reply else None
        if not reply or not meta:
            await message.reply_text(
                "🔎 Responde con `/mediainfo` a cualquier **video o archivo de video** para ver su ficha técnica completa."
            )
            return
        filename, file_size, _, _, _ = meta
        job_id = secrets.token_hex(4)
        status = await message.reply_text(
            f"🔎 **MediaInfo · En cola**\n`{DIVIDER}`\n├ **Archivo:** `{filename[:56]}`\n└ **Trabajo:** `{job_id}`",
            reply_markup=build_job_cancel_keyboard(job_id),
            quote=True,
        )
        job = CompressionJob(
            job_id=job_id,
            user_id=message.from_user.id,
            user_label=_format_user_label(message.from_user),
            chat_id=message.chat.id,
            status_message=status,
            filename=filename,
            options=CompressionOptions(),
            source_message=reply,
            expected_size=file_size or (200 * 1024 * 1024),
            action_kind="mediainfo",
        )
        await queue_mgr.enqueue(job)

    @app.on_message(filters.private & filters.command("ss"))
    async def cmd_screenshots(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        reply = message.reply_to_message
        meta = _extract_video_meta(reply) if reply else None
        if not reply or not meta:
            await message.reply_text(
                "🖼 Responde con `/ss` a cualquier **video o archivo de video** para generar su cuadrícula 3×3 de capturas."
            )
            return
        filename, file_size, _, _, _ = meta
        job_id = secrets.token_hex(4)
        status = await message.reply_text(
            f"🖼 **Cuadrícula 3×3 · En cola**\n`{DIVIDER}`\n├ **Archivo:** `{filename[:56]}`\n└ **Trabajo:** `{job_id}`",
            reply_markup=build_job_cancel_keyboard(job_id),
            quote=True,
        )
        job = CompressionJob(
            job_id=job_id,
            user_id=message.from_user.id,
            user_label=_format_user_label(message.from_user),
            chat_id=message.chat.id,
            status_message=status,
            filename=filename,
            options=CompressionOptions(),
            source_message=reply,
            expected_size=file_size or (200 * 1024 * 1024),
            action_kind="screenshots_grid",
        )
        await queue_mgr.enqueue(job)

    @app.on_message(filters.private & filters.command("auto"))
    async def cmd_auto(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        user_id = message.from_user.id
        opts, currently_on = await _load_user_options(user_id)
        new_state = not currently_on
        await db.set_auto_mode(user_id, new_state)
        if new_state:
            await message.reply_text(
                "⚡ **Modo Lotes ACTIVADO**\n"
                f"`{DIVIDER}`\n"
                "Reenvíame varios videos seguidos y entrarán directo a la cola sin pedir confirmación.\n"
                f"├ **Ajuste:** {_format_saved_preset_line(opts)}\n"
                f"└ **Marca de agua:** `{opts.watermark_text or 'Ninguna'}`\n\n"
                "💡 *Para cambiar tu ajuste favorito, apaga `/auto`, configura un video y pulsa `💾 Guardar Auto`.*"
            )
        else:
            await message.reply_text(
                "🎛 **Modo Lotes DESACTIVADO**\n"
                f"`{DIVIDER}`\n"
                "Cada video volverá a abrir el panel de configuración manual."
            )

    @app.on_message(filters.private & filters.command("setwatermark"))
    async def cmd_setwatermark(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.reply_text(
                "🏷 **Marca de Agua**\n"
                f"`{DIVIDER}`\n"
                "Uso: `/setwatermark @TuCanal`\n"
                "Usa `/delwatermark` para quitarla."
            )
            return
        wm_text = parts[1].strip()[:48]
        await db.set_watermark(message.from_user.id, wm_text)
        await message.reply_text(
            f"✅ **Marca de agua guardada:** `{wm_text}`\n"
            "*(Puedes cambiar la esquina en `✂️ Trim / Marca / Meta` al configurar un video.)*"
        )

    @app.on_message(filters.private & filters.command("delwatermark"))
    async def cmd_delwatermark(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        await db.set_watermark(message.from_user.id, "")
        await message.reply_text("🗑 Marca de agua eliminada.")

    @app.on_message(filters.private & filters.command("setthumb"))
    async def cmd_setthumb(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        reply = message.reply_to_message
        if not reply or not reply.photo:
            await message.reply_text(
                "🖼 Responde con `/setthumb` a una **foto** para fijarla como tu miniatura personalizada."
            )
            return
        await db.set_custom_thumb(message.from_user.id, reply.photo.file_id)
        await message.reply_text("✅ Miniatura personalizada guardada para tus próximos videos.")

    @app.on_message(filters.private & filters.command("delthumb"))
    async def cmd_delthumb(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        await db.set_custom_thumb(message.from_user.id, None)
        await message.reply_text("🗑 Miniatura personalizada eliminada (volverá a generarse automáticamente).")

    @app.on_message(filters.private & filters.command("queue"))
    async def cmd_queue(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        await message.reply_text(_render_queue_text())

    @app.on_message(filters.private & filters.command("stats"))
    async def cmd_stats(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        await message.reply_text(await _render_stats_text())

    # ── Callbacks del Menú Principal (/start) ─────────────────────────────────

    @app.on_callback_query(filters.regex(r"^home:(main|queue|stats|help|toggle_auto)$"))
    async def cb_home(_client: Client, query: CallbackQuery) -> None:
        user_id = query.from_user.id
        if not await db.is_authorized(user_id, config.owner_id):
            await query.answer("Acceso restringido.", show_alert=True)
            return

        action = query.data.split(":", 1)[1]
        is_owner = user_id == config.owner_id

        if action == "toggle_auto":
            _, cur = await _load_user_options(user_id)
            await db.set_auto_mode(user_id, not cur)
            await query.answer(f"Modo Lotes: {'ON' if not cur else 'OFF'}")
            text, auto_mode = await _render_home_text(user_id)
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    text,
                    reply_markup=build_start_keyboard(is_owner=is_owner, auto_mode=auto_mode),
                )
            return

        if action == "main":
            await query.answer()
            text, auto_mode = await _render_home_text(user_id)
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    text,
                    reply_markup=build_start_keyboard(is_owner=is_owner, auto_mode=auto_mode),
                )
            return

        if action == "queue":
            await query.answer()
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _render_queue_text(),
                    reply_markup=build_back_home_keyboard(),
                )
            return

        if action == "stats":
            await query.answer()
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    await _render_stats_text(),
                    reply_markup=build_back_home_keyboard(),
                )
            return

        if action == "help":
            await query.answer()
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    _render_help_text(is_owner),
                    reply_markup=build_back_home_keyboard(),
                )

    # ── Recepción de Video, Documento de Video o Subtítulo Externo (.srt/.ass) ─

    @app.on_message(filters.private & (filters.video | filters.document))
    async def on_video_message(_client: Client, message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        user_label = _format_user_label(message.from_user)

        # 0. ¿Estaba esperando un archivo de subtítulo externo (.srt / .ass / .vtt)?
        pending_state = awaiting_input.get(user_id)
        if pending_state and pending_state[1] == "attach_sub" and message.document:
            if not _is_subtitle_document(message):
                await message.reply_text(
                    "❌ Formato no compatible. Envía un archivo `.srt`, `.ass`, `.ssa` o `.vtt`."
                )
                return
            sid, _ = awaiting_input.pop(user_id)
            session = sessions.get(sid)
            if not session:
                await message.reply_text("⚠️ Esa sesión expiró. Reenvía el video para configurarlo de nuevo.")
                return
            sub_name = message.document.file_name or "subtitulo.srt"
            session.options.external_sub_file_id = message.document.file_id
            session.options.external_sub_name = sub_name
            if session.options.subtitle_mode == "none":
                session.options.subtitle_mode = "soft"
            await _refresh_session_message(session, view="tracks")
            await message.reply_text(
                f"✅ Subtítulo externo `{sub_name[:48]}` adjuntado (`{session.options.subtitle_mode}`).",
                quote=True,
            )
            return

        meta = _extract_video_meta(message)
        if not meta:
            return
        if not await _check_access(message):
            return

        _cleanup_old_sessions()
        opts, auto_mode = await _load_user_options(user_id)
        filename, file_size, duration, width, height = meta

        if auto_mode:
            await _enqueue_auto_job(
                message=message,
                user_id=user_id,
                user_label=user_label,
                filename=filename,
                file_size=file_size,
                opts=opts,
                source_message=message,
            )
            return

        sid = secrets.token_hex(5)
        summary = format_draft_summary(
            filename=filename,
            file_size=file_size,
            duration=duration,
            width=width,
            height=height,
            opts=opts,
        )
        cfg_msg = await message.reply_text(
            summary,
            reply_markup=build_main_config_keyboard(sid, opts),
            quote=True,
        )
        sessions[sid] = DraftSession(
            session_id=sid,
            user_id=user_id,
            user_label=user_label,
            chat_id=message.chat.id,
            config_message=cfg_msg,
            filename=filename,
            file_size=file_size,
            duration=duration,
            width=width,
            height=height,
            options=opts,
            source_message=message,
        )

    # ── Recepción de texto: inputs interactivos o enlace directo HTTP(S) ──────

    @app.on_message(
        filters.private
        & filters.text
        & ~filters.command(
            [
                "start",
                "help",
                "id",
                "auto",
                "mediainfo",
                "ss",
                "setwatermark",
                "delwatermark",
                "queue",
                "stats",
                "setthumb",
                "delthumb",
                "live",
                "admin",
                "usage",
                "logs",
                "login",
                "cancellogin",
                "logout",
                "setdump",
                "adduser",
                "deluser",
                "users",
                "purge",
                "cancelall",
                "update",
            ]
        )
    )
    async def on_text_message(_client: Client, message: Message) -> None:
        if not message.from_user or not message.text:
            return
        user_id = message.from_user.id
        user_label = _format_user_label(message.from_user)
        text = message.text.strip()

        # 0. Asistente seguro /login
        if user_id == config.owner_id and user_session.login_in_progress:
            with contextlib.suppress(Exception):
                await message.delete()
            stage = user_session.current_stage
            try:
                if stage == "phone":
                    reply_txt = await user_session.submit_phone(text)
                    await message.reply_text(reply_txt)
                elif stage == "code":
                    done, reply_txt, session_str = await user_session.submit_code(text)
                    if done and session_str:
                        await db.set_setting("user_session_string", session_str)
                    await message.reply_text(reply_txt)
                elif stage == "password":
                    reply_txt, session_str = await user_session.submit_password(text)
                    await db.set_setting("user_session_string", session_str)
                    await message.reply_text(reply_txt)
            except Exception as exc:
                await message.reply_text(f"⚠️ {exc}")
            return

        # 1. Inputs interactivos del panel inline
        if user_id in awaiting_input:
            sid, mode = awaiting_input.pop(user_id)
            session = sessions.get(sid)
            if not session:
                await message.reply_text("⚠️ Esa sesión expiró. Reenvía el video para configurarlo de nuevo.")
                return

            if text.lower() in {"cancelar", "cancel"}:
                with contextlib.suppress(Exception):
                    await message.delete()
                return

            if mode == "custom_mb":
                try:
                    mb_val = float(text.replace(",", ".").replace("mb", "").replace("MB", "").strip())
                    if mb_val < 2 or mb_val > 10240:
                        raise ValueError
                except ValueError:
                    awaiting_input[user_id] = (sid, mode)
                    await message.reply_text("❌ Escribe un número entre `2` y `10240` (ej: `45` o `150`).")
                    return
                session.options.target_mb = round(mb_val, 1)
                with contextlib.suppress(Exception):
                    await message.delete()
                await _refresh_session_message(session, view="main")
                return

            if mode == "rename":
                cleaned = text.strip()
                if cleaned.lower() in {"-", "reset", "original"}:
                    session.options.custom_output_name = ""
                else:
                    stem = Path(cleaned).stem if Path(cleaned).suffix.lower() in VIDEO_EXTENSIONS else cleaned
                    session.options.custom_output_name = stem[:120].strip()
                with contextlib.suppress(Exception):
                    await message.delete()
                await _refresh_session_message(session, view="main")
                return

            if mode == "trim":
                try:
                    if "-" in text:
                        left, right = text.split("-", 1)
                        start_s = parse_timestamp(left) if left.strip() else 0.0
                        r_clean = right.strip().lower()
                        end_s = None if r_clean in {"", "fin", "end"} else parse_timestamp(r_clean)
                    else:
                        start_s = parse_timestamp(text)
                        end_s = None
                    if end_s is not None and end_s <= start_s:
                        raise ValueError("El tiempo final debe ser mayor al inicial.")
                except ValueError as exc:
                    awaiting_input[user_id] = (sid, mode)
                    await message.reply_text(
                        f"❌ Formato inválido ({exc}).\n"
                        "Ejemplos: `00:30 - 05:00`, `01:20 - fin` o `15 - 90`."
                    )
                    return
                session.options.trim_start = start_s
                session.options.trim_end = end_s
                with contextlib.suppress(Exception):
                    await message.delete()
                await _refresh_session_message(session, view="extras")
                return

            if mode == "watermark":
                wm = "" if text.lower() in {"-", "ninguna", "borrar"} else text[:48]
                session.options.watermark_text = wm
                await db.set_watermark(user_id, wm, session.options.watermark_pos)
                with contextlib.suppress(Exception):
                    await message.delete()
                await _refresh_session_message(session, view="extras")
                return

            if mode == "meta_title":
                session.options.meta_title = "" if text.lower() in {"-", "borrar", "ninguno"} else text[:96]
                with contextlib.suppress(Exception):
                    await message.delete()
                await _refresh_session_message(session, view="extras")
                return

            if mode == "meta_artist":
                session.options.meta_artist = "" if text.lower() in {"-", "borrar", "ninguno"} else text[:96]
                with contextlib.suppress(Exception):
                    await message.delete()
                await _refresh_session_message(session, view="extras")
                return

        # 2. Enlace directo HTTP(S)
        if URL_REGEX.match(text):
            if not await _check_access(message):
                return
            _cleanup_old_sessions()
            parsed = urlparse(text)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return

            raw_name = Path(parsed.path).name or "video_enlace.mp4"
            opts, auto_mode = await _load_user_options(user_id)

            if auto_mode:
                await _enqueue_auto_job(
                    message=message,
                    user_id=user_id,
                    user_label=user_label,
                    filename=raw_name,
                    file_size=None,
                    opts=opts,
                    source_url=text,
                )
                return

            sid = secrets.token_hex(5)
            summary = format_draft_summary(
                filename=raw_name,
                file_size=None,
                duration=None,
                width=None,
                height=None,
                opts=opts,
                source_is_url=True,
            )
            cfg_msg = await message.reply_text(
                summary,
                reply_markup=build_main_config_keyboard(sid, opts),
                quote=True,
            )
            sessions[sid] = DraftSession(
                session_id=sid,
                user_id=user_id,
                user_label=user_label,
                chat_id=message.chat.id,
                config_message=cfg_msg,
                filename=raw_name,
                file_size=None,
                duration=None,
                width=None,
                height=None,
                options=opts,
                source_url=text,
            )

    async def _refresh_session_message(session: DraftSession, view: str = "main") -> None:
        summary = format_draft_summary(
            filename=session.filename,
            file_size=session.file_size,
            duration=session.duration,
            width=session.width,
            height=session.height,
            opts=session.options,
            media=session.media,
            source_is_url=bool(session.source_url),
        )
        if view == "presets":
            kb = build_presets_keyboard(session.session_id, session.options.preset_key)
        elif view == "advanced":
            kb = build_advanced_keyboard(session.session_id, session.options)
        elif view == "target":
            kb = build_target_size_keyboard(session.session_id, session.options)
        elif view == "tracks":
            kb = build_tracks_keyboard(session.session_id, session.options, session.media)
        elif view == "extras":
            kb = build_extras_keyboard(session.session_id, session.options)
        elif view == "tools":
            kb = build_tools_keyboard(session.session_id)
        else:
            kb = build_main_config_keyboard(session.session_id, session.options)

        with contextlib.suppress(Exception):
            await session.config_message.edit_text(summary, reply_markup=kb)

    # ── Callbacks de configuración de video ───────────────────────────────────

    @app.on_callback_query(filters.regex(r"^cfg:"))
    async def cb_config(_client: Client, query: CallbackQuery) -> None:
        parts = query.data.split(":")
        if len(parts) < 3:
            await query.answer()
            return

        sid = parts[1]
        action = parts[2]
        session = sessions.get(sid)
        if not session:
            await query.answer("Esta sesión expiró. Reenvía el video.", show_alert=True)
            return
        if query.from_user.id != session.user_id and query.from_user.id != config.owner_id:
            await query.answer("No puedes modificar la sesión de otro usuario.", show_alert=True)
            return

        if action == "noop":
            await query.answer()
            return

        if action in {"main", "presets", "advanced", "target", "tracks", "extras", "tools"}:
            await query.answer()
            await _refresh_session_message(session, view=action)
            return

        if action == "set_preset" and len(parts) >= 4:
            session.options.apply_preset(parts[3])
            await query.answer("Preset aplicado")
            await _refresh_session_message(session, view="main")
            return

        if action == "codec" and len(parts) >= 4:
            if parts[3] in {"libx265", "libx264", "libsvtav1"}:
                prev_codec = session.options.codec
                session.options.codec = parts[3]  # type: ignore[assignment]
                if parts[3] == "libx264":
                    session.options.bit_depth = 8
                    if prev_codec == "libsvtav1":
                        session.options.crf = 24
                elif parts[3] == "libsvtav1":
                    session.options.bit_depth = 10
                    if prev_codec != "libsvtav1":
                        session.options.crf = 30
                elif parts[3] == "libx265" and prev_codec == "libsvtav1":
                    session.options.crf = 26
            await query.answer(f"Códec: {session.options.codec_display_label()}")
            await _refresh_session_message(session, view="advanced")
            return

        if action == "toggle_10bit":
            if session.options.codec == "libx264":
                session.options.bit_depth = 8
                await query.answer("H.264 usa 8-bit. Elige H.265 o AV1 para 10-bit.", show_alert=True)
                return
            if session.options.bit_depth == 10:
                session.options.bit_depth = 8
                await query.answer("Profundidad: 8-bit")
            else:
                session.options.bit_depth = 10
                await query.answer("Profundidad: 10-bit")
            await _refresh_session_message(session, view="advanced")
            return

        if action == "res" and len(parts) >= 4:
            session.options.max_height = None if parts[3] == "orig" else int(parts[3])
            await query.answer(f"Resolución: {parts[3]}")
            await _refresh_session_message(session, view="advanced")
            return

        if action == "fps" and len(parts) >= 4:
            session.options.target_fps = None if parts[3] == "orig" else int(parts[3])
            await query.answer(f"FPS: {parts[3]}")
            await _refresh_session_message(session, view="advanced")
            return

        if action == "speed" and len(parts) >= 4:
            if parts[3] in {"ultrafast", "veryfast", "fast", "medium"}:
                session.options.speed = parts[3]  # type: ignore[assignment]
            await query.answer(f"Velocidad: {parts[3]}")
            await _refresh_session_message(session, view="advanced")
            return

        if action == "av1p" and len(parts) >= 4:
            with contextlib.suppress(ValueError):
                session.options.av1_preset = max(0, min(13, int(parts[3])))
            await query.answer(f"SVT-AV1 Preset: P{session.options.av1_preset}")
            await _refresh_session_message(session, view="advanced")
            return

        if action == "crf" and len(parts) >= 4:
            delta = int(parts[3])
            max_crf = 50 if session.options.codec == "libsvtav1" else 38
            session.options.crf = max(16, min(max_crf, session.options.crf + delta))
            session.options.target_mb = None
            await query.answer(f"CRF: {session.options.crf}")
            await _refresh_session_message(session, view="advanced")
            return

        if action == "toggle_grain":
            session.options.av1_film_grain = not session.options.av1_film_grain
            await query.answer(
                f"Grano AV1: {'ON' if session.options.av1_film_grain else 'OFF'}"
            )
            await _refresh_session_message(session, view="advanced")
            return

        if action == "toggle_crop":
            session.options.auto_crop = not session.options.auto_crop
            await query.answer(
                f"Auto-Crop barras negras: {'ON' if session.options.auto_crop else 'OFF'}"
            )
            await _refresh_session_message(session, view="advanced")
            return

        if action == "cycle_hdr":
            order = ("auto", "on", "off")
            cur = session.options.hdr_to_sdr
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
            session.options.hdr_to_sdr = nxt  # type: ignore[assignment]
            labels = {
                "auto": "HDR→SDR: Auto (si detecta HDR)",
                "on": "HDR→SDR: Forzado ON",
                "off": "HDR→SDR: OFF (conservar HDR)",
            }
            await query.answer(labels[nxt])
            await _refresh_session_message(session, view="advanced")
            return

        if action == "set_mb" and len(parts) >= 4:
            session.options.target_mb = float(parts[3])
            await query.answer(f"Objetivo 2-Pass: {parts[3]} MB")
            await _refresh_session_message(session, view="main")
            return

        if action == "clear_mb":
            session.options.target_mb = None
            await query.answer("Modo CRF restaurado")
            await _refresh_session_message(session, view="main")
            return

        if action == "custom_mb":
            awaiting_input[session.user_id] = (sid, "custom_mb")
            await query.answer("Envía ahora el tamaño en MB (ej: 75)", show_alert=True)
            return

        if action == "rename":
            awaiting_input[session.user_id] = (sid, "rename")
            await query.answer(
                "Escribe el nuevo nombre del archivo (sin extensión), o '-' para usar el original.",
                show_alert=True,
            )
            return

        if action == "attach_sub":
            awaiting_input[session.user_id] = (sid, "attach_sub")
            await query.answer(
                "Envía ahora el archivo de subtítulos (.srt, .ass o .vtt) como documento en este chat.",
                show_alert=True,
            )
            return

        if action == "clear_ext_sub":
            session.options.external_sub_file_id = None
            session.options.external_sub_name = None
            await query.answer("Subtítulo externo quitado")
            await _refresh_session_message(session, view="tracks")
            return

        if action == "set_trim":
            awaiting_input[session.user_id] = (sid, "trim")
            await query.answer(
                "Envía el rango de recorte: ej. 00:30 - 05:00 o 01:20 - fin",
                show_alert=True,
            )
            return

        if action == "clear_trim":
            session.options.trim_start = None
            session.options.trim_end = None
            await query.answer("Recorte eliminado")
            await _refresh_session_message(session, view="extras")
            return

        if action == "set_wm":
            awaiting_input[session.user_id] = (sid, "watermark")
            await query.answer(
                "Escribe el texto de tu marca de agua (ej: @TuCanal) o '-' para quitarla.",
                show_alert=True,
            )
            return

        if action == "clear_wm":
            session.options.watermark_text = ""
            await db.set_watermark(session.user_id, "")
            await query.answer("Marca de agua quitada")
            await _refresh_session_message(session, view="extras")
            return

        if action == "wmpos" and len(parts) >= 4:
            if parts[3] in {"tl", "tr", "bl", "br"}:
                session.options.watermark_pos = parts[3]  # type: ignore[assignment]
                await db.set_watermark(session.user_id, session.options.watermark_text, parts[3])
            await query.answer("Posición actualizada")
            await _refresh_session_message(session, view="extras")
            return

        if action == "set_meta_title":
            awaiting_input[session.user_id] = (sid, "meta_title")
            await query.answer("Escribe el Título de metadatos o '-' para borrarlo.", show_alert=True)
            return

        if action == "set_meta_artist":
            awaiting_input[session.user_id] = (sid, "meta_artist")
            await query.answer("Escribe el Autor/Canal de metadatos o '-' para borrarlo.", show_alert=True)
            return

        if action == "toggle_strip_meta":
            session.options.strip_metadata = not session.options.strip_metadata
            await query.answer(
                f"Limpiar metadatos: {'ON' if session.options.strip_metadata else 'OFF'}"
            )
            await _refresh_session_message(session, view="extras")
            return

        if action == "toggle_deband":
            session.options.deband = not session.options.deband
            await query.answer(f"Deband: {'ON' if session.options.deband else 'OFF'}")
            await _refresh_session_message(session, view="extras")
            return

        if action == "toggle_deint":
            session.options.deinterlace = not session.options.deinterlace
            await query.answer(f"Desentrelazar (bwdif): {'ON' if session.options.deinterlace else 'OFF'}")
            await _refresh_session_message(session, view="extras")
            return

        if action == "toggle_loudnorm":
            session.options.loudnorm = not session.options.loudnorm
            await query.answer(f"Nivelar Audio (loudnorm): {'ON' if session.options.loudnorm else 'OFF'}")
            await _refresh_session_message(session, view="extras")
            return

        if action == "audio" and len(parts) >= 4:
            val = ":".join(parts[3:])
            session.options.audio_mode = val
            await query.answer("Audio actualizado")
            await _refresh_session_message(session, view="tracks")
            return

        if action == "acodec" and len(parts) >= 4:
            if parts[3] in {"aac", "libopus", "copy"}:
                session.options.audio_codec = parts[3]  # type: ignore[assignment]
            await query.answer(f"Códec audio: {parts[3]}")
            await _refresh_session_message(session, view="tracks")
            return

        if action == "sub" and len(parts) >= 4:
            if parts[3] in {"none", "soft", "hard"}:
                session.options.subtitle_mode = parts[3]  # type: ignore[assignment]
            await query.answer(f"Subtítulos: {parts[3]}")
            await _refresh_session_message(session, view="tracks")
            return

        if action == "subtrack" and len(parts) >= 4:
            session.options.subtitle_track_idx = int(parts[3])
            if session.options.subtitle_mode == "none":
                session.options.subtitle_mode = "soft"
            await query.answer(f"Pista de subtítulos #{int(parts[3]) + 1}")
            await _refresh_session_message(session, view="tracks")
            return

        if action == "abr" and len(parts) >= 4:
            session.options.audio_bitrate_k = int(parts[3])
            await query.answer(f"Audio: {parts[3]} kbps")
            await _refresh_session_message(session, view="tracks")
            return

        if action == "toggle_doc":
            session.options.send_as_document = not session.options.send_as_document
            await db.set_send_as_document(session.user_id, session.options.send_as_document)
            await query.answer("Formato de envío actualizado")
            await _refresh_session_message(session, view="main")
            return

        if action == "save_auto":
            await db.save_default_options(session.user_id, session.options.to_saved_dict())
            await query.answer(
                "Ajuste guardado como favorito. Activa /auto para comprimir en lote automáticamente.",
                show_alert=True,
            )
            return

        if action == "tool" and len(parts) >= 4 and parts[3] in VALID_TOOL_KINDS:
            tool_kind: ActionKind = parts[3]  # type: ignore[assignment]
            job_id = secrets.token_hex(4)
            cached_note = (
                "⚡ *Video en caché: sin re-descargar*"
                if queue_mgr.has_cached_source(session.session_id)
                else "💾 *Se guardará en caché temporal para no re-descargarlo si luego comprimes*"
            )
            tool_titles = {
                "mediainfo": "🔎 MediaInfo Técnico",
                "screenshots_grid": "🖼 Cuadrícula 3×3",
                "screenshots_hd": "📸 4 Capturas HD",
                "remux_mp4": "🔄 Remux a MP4",
                "remux_mkv": "🔄 Remux a MKV",
                "extract_opus": "🎵 Extraer Audio Opus",
                "extract_mp3": "🎵 Extraer Audio MP3",
            }
            tool_status = await session.config_message.reply_text(
                f"{tool_titles[tool_kind]} · **En cola**\n"
                f"`{DIVIDER}`\n"
                f"├ **Archivo:** `{session.filename[:56]}`\n"
                f"└ **Trabajo:** `{job_id}`\n\n"
                f"{cached_note}",
                reply_markup=build_job_cancel_keyboard(job_id),
            )

            async def _on_probed(probed: MediaInfo) -> None:
                if sid in sessions:
                    sessions[sid].media = probed
                    sessions[sid].duration = probed.duration
                    sessions[sid].width = probed.width
                    sessions[sid].height = probed.height
                    await _refresh_session_message(sessions[sid], view="tools")

            job = CompressionJob(
                job_id=job_id,
                user_id=session.user_id,
                user_label=session.user_label,
                chat_id=session.chat_id,
                status_message=tool_status,
                filename=session.filename,
                options=session.options.clone(),
                source_message=session.source_message,
                source_url=session.source_url,
                expected_size=session.file_size or (500 * 1024 * 1024),
                session_id=session.session_id,
                keep_cached_source=True,
                action_kind=tool_kind,
                on_media_probed=_on_probed,
            )
            pos = await queue_mgr.enqueue(job)
            await query.answer(f"{tool_titles[tool_kind]} encolado (#{pos})")
            return

        if action == "sample":
            sample_opts = session.options.clone()
            sample_opts.sample_duration = 15.0
            sample_opts.target_mb = None
            job_id = secrets.token_hex(4)
            cached_note = (
                "⚡ *Video en caché: sin re-descargar*"
                if queue_mgr.has_cached_source(session.session_id)
                else "💾 *Se guardará en caché temporal para no re-descargarlo al comprimir*"
            )
            sample_status = await session.config_message.reply_text(
                f"🎞 **Muestra 15s · En cola**\n"
                f"`{DIVIDER}`\n"
                f"├ **Archivo:** `{session.filename[:56]}`\n"
                f"└ **Trabajo:** `{job_id}`\n\n"
                f"{cached_note}",
                reply_markup=build_job_cancel_keyboard(job_id),
            )
            job = CompressionJob(
                job_id=job_id,
                user_id=session.user_id,
                user_label=session.user_label,
                chat_id=session.chat_id,
                status_message=sample_status,
                filename=session.filename,
                options=sample_opts,
                source_message=session.source_message,
                source_url=session.source_url,
                expected_size=session.file_size or (500 * 1024 * 1024),
                session_id=session.session_id,
                keep_cached_source=True,
            )
            pos = await queue_mgr.enqueue(job)
            await query.answer(f"Muestra 15s encolada (#{pos})")
            return

        if action == "discard":
            sessions.pop(sid, None)
            awaiting_input.pop(session.user_id, None)
            queue_mgr.release_session_cache(sid)
            await query.answer("Descartado")
            with contextlib.suppress(Exception):
                await session.config_message.edit_text("✖ Operación descartada.")
            return

        if action == "start":
            sessions.pop(sid, None)
            awaiting_input.pop(session.user_id, None)
            job_id = secrets.token_hex(4)
            has_cache = queue_mgr.has_or_pending_cache(session.session_id)

            job = CompressionJob(
                job_id=job_id,
                user_id=session.user_id,
                user_label=session.user_label,
                chat_id=session.chat_id,
                status_message=session.config_message,
                filename=session.filename,
                options=session.options,
                source_message=session.source_message,
                source_url=session.source_url,
                expected_size=session.file_size or (500 * 1024 * 1024),
                session_id=session.session_id if has_cache else None,
                keep_cached_source=False,
            )
            pos = await queue_mgr.enqueue(job)
            await query.answer(f"Encolado (#{pos})")
            cache_line = "\n⚡ *Usará el video ya descargado en caché*" if has_cache else ""
            await session.config_message.edit_text(
                f"⏳ **En Cola · Posición #{pos}**\n"
                f"`{DIVIDER}`\n"
                f"├ **Archivo:** `{session.filename[:56]}`\n"
                f"├ **Códec:** `{session.options.codec_display_label()}` · `{queue_mgr.threads_per_job()}` hilos\n"
                f"└ **Trabajo:** `{job_id}`{cache_line}",
                reply_markup=build_job_cancel_keyboard(job_id),
            )

    # ── Cancelación de trabajos activos / en cola ─────────────────────────────

    @app.on_callback_query(filters.regex(r"^job:cancel:"))
    async def cb_cancel_job(_client: Client, query: CallbackQuery) -> None:
        job_id = query.data.split(":", 2)[2]
        ok, msg = await queue_mgr.cancel_job(job_id, query.from_user.id)
        await query.answer(msg, show_alert=not ok)

    # ── Botón rápido "Re-comprimir a 1.90 GB (para Telegram)" ─────────────────

    @app.on_callback_query(filters.regex(r"^recomp:"))
    async def cb_recompress_telegram(client: Client, query: CallbackQuery) -> None:
        token = query.data.split(":", 1)[1]
        src = queue_mgr.get_recompress_source(token)
        if not src:
            await query.answer(
                "Ese acceso expiró. Reenvía el video y elige 1.9 GB en Tamaño Exacto.",
                show_alert=True,
            )
            return
        if query.from_user.id != src.user_id and query.from_user.id != config.owner_id:
            await query.answer("Solo el usuario que inició el trabajo puede re-comprimirlo.", show_alert=True)
            return

        source_msg: Message | None = None
        if src.source_chat_id and src.source_message_id:
            with contextlib.suppress(Exception):
                source_msg = await client.get_messages(src.source_chat_id, src.source_message_id)

        if not source_msg and not src.source_url:
            await query.answer("No se encontró el mensaje original para re-comprimir.", show_alert=True)
            return

        job_id = secrets.token_hex(4)
        status_msg = await query.message.reply_text(
            f"⏳ **Re-compresión 2-Pass (1.90 GB)**\n"
            f"`{DIVIDER}`\n"
            f"├ **Archivo:** `{src.filename[:56]}`\n"
            f"└ **Trabajo:** `{job_id}`",
            reply_markup=build_job_cancel_keyboard(job_id),
        )
        job = CompressionJob(
            job_id=job_id,
            user_id=src.user_id,
            user_label=src.user_label or _format_user_label(query.from_user),
            chat_id=src.chat_id,
            status_message=status_msg,
            filename=src.filename,
            options=src.base_options,
            source_message=source_msg,
            source_url=src.source_url,
            expected_size=2 * 1024 * 1024 * 1024,
        )
        pos = await queue_mgr.enqueue(job)
        await query.answer(f"Re-compresión encolada (#{pos})")
