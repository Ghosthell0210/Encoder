"""Manejadores de usuario: inicio interactivo, guía organizada, recepción de videos/enlaces,
menús inline (AV1/H.265/H.264, FPS, Opus, Metadatos, Herramientas rápidas), Modo Lotes (/auto),
Muestra 15s, subtítulos externos, /mediainfo y /ss."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from hydrogram import Client, filters
from hydrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, User

from config import BotConfig
from core.archive_tools import (
    WorkspaceEntry,
    is_archive_file,
    list_workspace_dir,
    resolve_workspace_path,
)
from core.database import Database
from core.ffmpeg_engine import CompressionOptions, parse_timestamp
from core.host_extractors import HostExtractionError, ResolvedHostDownload, identify_file_host
from core.media_probe import MediaInfo, probe_media
from core.queue_manager import ActionKind, ActiveSessionInfo, CompressionJob, QueueManager
from core.torrent_downloader import is_magnet_link, is_torrent_url
from core.ui_helpers import (
    DIVIDER,
    build_advanced_keyboard,
    build_archive_action_keyboard,
    build_back_home_keyboard,
    build_extras_keyboard,
    build_job_cancel_keyboard,
    build_ls_item_keyboard,
    build_ls_keyboard,
    build_main_config_keyboard,
    build_presets_keyboard,
    build_start_keyboard,
    build_target_size_keyboard,
    build_tools_keyboard,
    build_tracks_keyboard,
    build_url_audio_lang_keyboard,
    build_url_quality_keyboard,
    format_draft_summary,
    human_bytes,
    human_duration,
)
from core.uploader import UrlProbeInfo, is_non_video_filename, probe_url_info
from core.user_session import UserSessionManager

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".ts", ".wmv"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}
URL_REGEX = re.compile(r"^(?:https?://\S+|magnet:\?xt=urn:[a-z0-9]+:[a-z0-9]{16,}\S*)$", re.IGNORECASE)
VALID_TOOL_KINDS: set[str] = {
    "mediainfo",
    "split_parts",
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
    local_source_path: Path | None = None
    url_info: UrlProbeInfo | None = None
    download_max_height: int | None = None
    selected_audio_lang: str | None = None
    embed_subs: bool = False
    over_2gb_mode: Literal["auto", "link", "split"] = "auto"
    url_menu_return_to: str = "main"
    media: MediaInfo | None = None
    created_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class ArchiveDraft:
    token: str
    user_id: int
    user_label: str
    chat_id: int
    filename: str
    file_size: int
    source_message: Message | None = None
    source_url: str | None = None
    resolved_host: ResolvedHostDownload | None = None
    prompt_message: Message | None = None
    created_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class LsViewState:
    token: str
    user_id: int
    chat_id: int
    rel_path: str
    entries: list[WorkspaceEntry]
    page: int = 0
    message: Message | None = None
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


def _is_torrent_document(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    mime = (doc.mime_type or "").lower()
    name = (doc.file_name or "").lower()
    return mime == "application/x-bittorrent" or name.endswith(".torrent")


def _is_archive_document(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    name = doc.file_name or ""
    if not name:
        return False
    return is_archive_file(Path(name))


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
    archive_drafts: dict[str, ArchiveDraft] = {}
    ls_views: dict[str, LsViewState] = {}
    # user_id -> (session_or_token, mode)
    awaiting_input: dict[int, tuple[str, str]] = {}

    def _cleanup_old_sessions() -> None:
        now = time.time()
        expired = [sid for sid, s in sessions.items() if now - s.created_at > 3600]
        for sid in expired:
            sessions.pop(sid, None)
            queue_mgr.release_session_cache(sid)
        for tok in [k for k, d in archive_drafts.items() if now - d.created_at > 3600]:
            archive_drafts.pop(tok, None)
        for tok in [k for k, v in ls_views.items() if now - v.created_at > 7200]:
            ls_views.pop(tok, None)

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

    async def _is_leech_mode(user_id: int) -> bool:
        return (await db.get_setting(f"leech_mode:{user_id}", "0")) == "1"

    async def _set_leech_mode(user_id: int, enabled: bool) -> None:
        await db.set_setting(f"leech_mode:{user_id}", "1" if enabled else "0")

    async def _render_home_text(user_id: int) -> tuple[str, bool, bool]:
        opts, auto_mode = await _load_user_options(user_id)
        leech_mode = await _is_leech_mode(user_id)
        active, pending = queue_mgr.get_queue_snapshot()
        auto_tag = "ACTIVADO" if auto_mode else "Manual"
        leech_tag = "ON (Solo descargar)" if leech_mode else "OFF (Encoder)"
        text = (
            "🎬 **HydroCompress Bot**\n"
            f"`{DIVIDER}`\n"
            "Envíame cualquier **video**, **archivo** (hasta 2 GB) o **enlace** (`YouTube`, `TikTok`, `Instagram`, `X` o descarga directa) para comprimirlo en **AV1 / H.265 / H.264**, usar herramientas rápidas o bajarlo sin comprimir con `/leech`.\n\n"
            "⚙️ **Tu Perfil**\n"
            f"├ **Modo Lotes:** `{auto_tag}`\n"
            f"├ **Modo Leech:** `{leech_tag}`\n"
            f"├ **Ajuste base:** {_format_saved_preset_line(opts)}\n"
            f"├ **Marca de agua:** `{opts.watermark_text or 'Ninguna'}`\n"
            f"└ **Cola actual:** `{len(active)}/{queue_mgr.concurrency_limit}` activos · `{len(pending)}` en espera\n\n"
            "💡 *Usa el menú `/` abajo o los botones rápidos.*"
        )
        return text, auto_mode, leech_mode

    def _render_help_text(is_owner: bool) -> str:
        lines = [
            "📖 **Guía de Comandos**",
            f"`{DIVIDER}`",
            "",
            "🎬 **Compresión, Leech y Herramientas**",
            "├ `/start` — Menú principal interactivo",
            "├ `/leech [url|magnet]` — Bajar sin comprimir (o activar **Modo Leech**)",
            "├ `/ls [carpeta]` — Explorar archivos del servidor, **descomprimir** (`.rar`/`.zip`/`.7z`/`.tar`), comprimir o enviar",
            "├ `/extract [url] [clave]` — Descargar y descomprimir directo en `/ls` (o responde a un `.rar`/`.zip`)",
            "├ `/auto` — Activar/desactivar **Modo Lotes** automático",
            "├ `/mediainfo` — Ficha técnica completa (responde a un video)",
            "├ `/ss` — Cuadrícula 3×3 de capturas (responde a un video)",
            "├ `/queue` — Ver cola y trabajos activos",
            "└ `/stats` — Total comprimido y espacio ahorrado",
            "",
            "🌐 **Fuentes soportadas en enlaces**",
            "├ **Servidores:** MediaFire, Pixeldrain, GoFile, Buzzheavier, Google Drive, Dropbox, Krakenfiles, 1Fichier, Qiwi, Bunkr",
            "├ **Redes/Video (`yt-dlp`):** YouTube, Twitch, TikTok, Instagram, X, Facebook, Vimeo, etc.",
            "└ **P2P y Directos:** `magnet:?`, archivos/URLs `.torrent`, enlaces HTTP directos (`.mp4`, `.rar`, `.zip`, `.pdf`)",
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
                    "├ `/purge` · `/cancelall` — Limpiar temporales / detener cola",
                    "└ `/login` · `/logout` · `/setdump <id>`",
                ]
            )
        lines.extend(
            [
                "",
                "💡 *En cada video tienes **AV1 / H.265 / H.264**, **Audio Opus**, **FPS**, **Metadatos**, **Remux MKV↔MP4**, **Extraer Audio**, **Screenshots** y **Muestra 15s** con caché compartido.*",
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
        download_max_height: int | None = None,
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
            download_max_height=download_max_height,
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

    def _resolve_sub_langs(url_info: UrlProbeInfo | None) -> str | None:
        if not url_info or not url_info.subtitle_langs:
            return None
        if any(c.lower().startswith(("es", "en")) for c in url_info.subtitle_langs):
            return "es.*,es,en.*,en"
        return ",".join(url_info.subtitle_langs[:3])

    async def _compute_over_2gb_flags(session: DraftSession) -> tuple[bool, bool]:
        est_size = (
            session.file_size
            or (session.url_info.direct_bytes if session.url_info else None)
            or 0
        )
        tg_limit = await queue_mgr.effective_tg_limit_bytes(session.user_id)
        if est_size <= tg_limit:
            return False, True
        already_cached = queue_mgr.has_cached_source(session.session_id)
        needs_merge = bool(session.url_info and session.url_info.formats)
        can_split = await asyncio.to_thread(
            queue_mgr.can_split_for_bytes,
            est_size,
            already_cached,
            needs_merge,
        )
        return True, can_split

    async def _enqueue_leech_job(
        message: Message,
        user_id: int,
        user_label: str,
        filename: str,
        file_size: int | None,
        opts: CompressionOptions,
        source_url: str | None = None,
        source_message: Message | None = None,
        download_max_height: int | None = None,
        status_message: Message | None = None,
        expect_video: bool = False,
        audio_lang: str | None = None,
        embed_subs: bool = False,
        sub_langs: str | None = None,
        source_is_direct: bool = False,
        over_2gb_mode: Literal["auto", "link", "split"] = "auto",
        resolved_host: ResolvedHostDownload | None = None,
        is_torrent: bool = False,
    ) -> None:
        job_id = secrets.token_hex(4)
        status_msg = status_message or await message.reply_text(
            f"{'🧲 **Torrent' if is_torrent else '⬇️ **Leech'} · Encolando `{job_id}`...**",
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
            download_max_height=download_max_height,
            expected_size=file_size or (500 * 1024 * 1024),
            action_kind="download_only",
            expect_video=expect_video,
            audio_lang=audio_lang,
            embed_subs=embed_subs,
            sub_langs=sub_langs,
            source_is_direct=source_is_direct,
            over_2gb_mode=over_2gb_mode,
            resolved_host=resolved_host,
            is_torrent=is_torrent,
        )
        pos = await queue_mgr.enqueue(job)
        res_note = f" (`{download_max_height}p`)" if download_max_height else ""
        size_note = f" · ~`{human_bytes(file_size)}`" if file_size else ""
        subs_note = " · `Subs MKV`" if embed_subs else ""
        lang_note = f" · `Audio {audio_lang}`" if audio_lang else ""
        title_badge = "🧲 **Torrent · En Cola (#{pos})**" if is_torrent else "⬇️ **Leech · En Cola (#{pos})**"
        with contextlib.suppress(Exception):
            await status_msg.edit_text(
                f"{title_badge.format(pos=pos)}\n"
                f"`{DIVIDER}`\n"
                f"├ **Archivo:** `{filename[:56]}`{res_note}{size_note}\n"
                f"├ **Modo:** `{'Descarga P2P' if is_torrent else 'Sin recomprimir'}`{lang_note}{subs_note}\n"
                f"└ **Trabajo:** `{job_id}`",
                reply_markup=build_job_cancel_keyboard(job_id),
            )

    async def _enqueue_workspace_job(
        message: Message,
        user_id: int,
        user_label: str,
        filename: str,
        file_size: int | None,
        action_kind: Literal["save_to_workspace", "extract_to_workspace"],
        source_message: Message | None = None,
        source_url: str | None = None,
        local_source_path: Path | None = None,
        resolved_host: ResolvedHostDownload | None = None,
        archive_password: str | None = None,
        status_message: Message | None = None,
    ) -> None:
        opts, _ = await _load_user_options(user_id)
        job_id = secrets.token_hex(4)
        verb = "Descomprimir en /ls" if action_kind == "extract_to_workspace" else "Guardar en /ls"
        status_msg = status_message or await message.reply_text(
            f"📦 **{verb} · Encolando `{job_id}`...**",
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
            local_source_path=local_source_path,
            resolved_host=resolved_host,
            expected_size=file_size or (300 * 1024 * 1024),
            action_kind=action_kind,
            archive_password=archive_password,
            source_is_direct=True,
        )
        pos = await queue_mgr.enqueue(job)
        with contextlib.suppress(Exception):
            await status_msg.edit_text(
                f"📦 **{verb} · En Cola (#{pos})**\n"
                f"`{DIVIDER}`\n"
                f"├ **Archivo:** `{filename[:56]}`\n"
                f"└ **Trabajo:** `{job_id}`",
                reply_markup=build_job_cancel_keyboard(job_id),
            )

    async def _handle_url_request(
        message: Message,
        user_id: int,
        user_label: str,
        url_text: str,
        force_leech: bool = False,
    ) -> None:
        _cleanup_old_sessions()
        opts, auto_mode = await _load_user_options(user_id)
        leech_mode = await _is_leech_mode(user_id)
        is_leech = force_leech or leech_mode

        # 0. Enlace magnet:? o .torrent directo -> cola P2P inmediata
        if is_magnet_link(url_text) or is_torrent_url(url_text):
            bt_name = "Descarga Magnet" if is_magnet_link(url_text) else (Path(urlparse(url_text).path).name or "archivo.torrent")
            await _enqueue_leech_job(
                message=message,
                user_id=user_id,
                user_label=user_label,
                filename=bt_name,
                file_size=None,
                opts=opts,
                source_url=url_text,
                is_torrent=True,
            )
            return

        parsed = urlparse(url_text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            if force_leech:
                await message.reply_text("❌ Enlace inválido. Envía una URL `http://`, `https://` o `magnet:?`.")
            return

        raw_name = Path(parsed.path).name or "video_enlace.mp4"
        maybe_non_video = is_non_video_filename(raw_name) or (identify_file_host(url_text) is not None)

        if auto_mode and not is_leech and not maybe_non_video:
            await _enqueue_auto_job(
                message=message,
                user_id=user_id,
                user_label=user_label,
                filename=raw_name,
                file_size=None,
                opts=opts,
                source_url=url_text,
                download_max_height=opts.max_height,
            )
            return

        probe_msg = await message.reply_text(
            (
                "⬇️ **Analizando enlace (Leech)...**\nConsultando archivo, resoluciones y peso..."
                if is_leech
                else "🔎 **Analizando enlace...**\nConsultando archivo, resoluciones y pesos disponibles..."
            ),
            quote=True,
        )

        url_info: UrlProbeInfo | None = None
        probe_err = ""
        try:
            url_info = await probe_url_info(url_text, gofile_token=config.gofile_token)
        except HostExtractionError as exc:
            logger.warning("Servidor de archivos bloqueado o no disponible (%s): %s", url_text, exc)
            with contextlib.suppress(Exception):
                await probe_msg.edit_text(f"❌ {exc}")
            return
        except RuntimeError as exc:
            err_txt = str(exc)
            if any(
                kw in err_txt.lower()
                for kw in ("privada", "locales", "http://", "resolver", "reservadas")
            ):
                logger.warning("URL bloqueada por validación (%s): %s", url_text, err_txt)
                with contextlib.suppress(Exception):
                    await probe_msg.edit_text(f"❌ {err_txt}")
                return
            logger.warning("No se pudo pre-analizar URL %s: %s", url_text, err_txt)
            probe_err = err_txt
            url_info = None
        except Exception as exc:
            logger.exception("Fallo inesperado analizando URL %s", url_text)
            probe_err = str(exc) or exc.__class__.__name__
            url_info = None

        sel_height: int | None = None
        file_size: int | None = None
        duration: float | None = None
        width: int | None = None
        height: int | None = None
        final_name = raw_name

        if url_info is not None:
            final_name = url_info.filename or raw_name
            duration = url_info.duration
            if url_info.formats:
                chosen_fmt = url_info.formats[0]
                if opts.max_height:
                    for fmt in url_info.formats:
                        if fmt.height == opts.max_height or fmt.width == opts.max_height:
                            chosen_fmt = fmt
                            break
                sel_height = chosen_fmt.height
                file_size = chosen_fmt.est_bytes
                width = chosen_fmt.width
                height = chosen_fmt.height
            elif url_info.direct_bytes:
                file_size = url_info.direct_bytes

        # Si el servidor respondió Content-Type .torrent -> Leech P2P
        if url_info is not None and (
            "bittorrent" in (url_info.content_type or "").lower()
            or final_name.lower().endswith(".torrent")
        ):
            await _enqueue_leech_job(
                message=message,
                user_id=user_id,
                user_label=user_label,
                filename=final_name,
                file_size=file_size,
                opts=opts,
                source_url=url_text,
                status_message=probe_msg,
                resolved_host=url_info.resolved_host,
                is_torrent=True,
            )
            return

        # Archivo no-video directo (.rar, .zip, .7z, .pdf, .iso, etc.) -> Auto-Leech sin menú de video
        is_non_video_direct = bool(
            url_info is not None and (url_info.is_non_video or (url_info.is_direct and is_non_video_filename(final_name)))
        )
        if is_non_video_direct:
            is_leech = True
        elif auto_mode and not is_leech:
            await _enqueue_auto_job(
                message=message,
                user_id=user_id,
                user_label=user_label,
                filename=final_name,
                file_size=file_size,
                opts=opts,
                source_url=url_text,
                download_max_height=sel_height or opts.max_height,
            )
            with contextlib.suppress(Exception):
                await probe_msg.delete()
            return

        tg_limit = await queue_mgr.effective_tg_limit_bytes(user_id)
        est_for_2gb = file_size or (url_info.direct_bytes if url_info else None) or 0
        show_over_2gb = est_for_2gb > tg_limit
        can_split_over_2gb = (
            False
            if is_non_video_direct
            else (
                await asyncio.to_thread(
                    queue_mgr.can_split_for_bytes,
                    est_for_2gb,
                    False,
                    bool(url_info and url_info.formats),
                )
                if show_over_2gb
                else True
            )
        )
        initial_over_2gb: Literal["auto", "link", "split"] = (
            "link" if (show_over_2gb and not can_split_over_2gb) else "auto"
        )

        if is_leech:
            has_choices = (
                url_info is not None
                and not url_info.is_direct
                and (
                    len(url_info.formats) > 1
                    or len(url_info.audio_languages) >= 2
                    or url_info.has_subtitles
                    or (show_over_2gb and can_split_over_2gb)
                )
            )
            if not has_choices or auto_mode or is_non_video_direct:
                await _enqueue_leech_job(
                    message=message,
                    user_id=user_id,
                    user_label=user_label,
                    filename=final_name,
                    file_size=file_size,
                    opts=opts,
                    source_url=url_text,
                    download_max_height=sel_height,
                    status_message=probe_msg,
                    expect_video=bool(url_info and url_info.formats),
                    source_is_direct=bool(url_info and url_info.is_direct),
                    over_2gb_mode=initial_over_2gb,
                    resolved_host=url_info.resolved_host if url_info else None,
                )
                return

        sid = secrets.token_hex(5)
        summary = format_draft_summary(
            filename=final_name,
            file_size=file_size,
            duration=duration,
            width=width,
            height=height,
            opts=opts,
            source_is_url=True,
            url_info=url_info,
            selected_dl_height=sel_height,
            over_2gb_mode=initial_over_2gb,
            show_over_2gb=show_over_2gb,
            can_split_over_2gb=can_split_over_2gb,
        )
        if probe_err:
            summary += f"\n⚠️ **Pre-análisis:** `{probe_err[:140]}`"

        if is_leech:
            kb = build_url_quality_keyboard(
                sid,
                url_info,
                sel_height,
                over_2gb_mode=initial_over_2gb,
                show_over_2gb=show_over_2gb,
                can_split_over_2gb=can_split_over_2gb,
            )
        else:
            kb = build_main_config_keyboard(
                sid,
                opts,
                source_is_url=True,
                url_info=url_info,
                selected_dl_height=sel_height,
                over_2gb_mode=initial_over_2gb,
                show_over_2gb=show_over_2gb,
                can_split_over_2gb=can_split_over_2gb,
            )
        with contextlib.suppress(Exception):
            await probe_msg.edit_text(summary, reply_markup=kb)

        sessions[sid] = DraftSession(
            session_id=sid,
            user_id=user_id,
            user_label=user_label,
            chat_id=message.chat.id,
            config_message=probe_msg,
            filename=final_name,
            file_size=file_size,
            duration=duration,
            width=width,
            height=height,
            options=opts,
            source_url=url_text,
            url_info=url_info,
            download_max_height=sel_height,
            over_2gb_mode=initial_over_2gb,
            url_menu_return_to="dl_menu" if is_leech else "main",
        )

    # ── Comandos informativos, perfil y accesos rápidos (/mediainfo, /ss) ────

    @app.on_message(filters.private & filters.command("start"))
    async def cmd_start(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        user_id = message.from_user.id
        is_owner = user_id == config.owner_id
        text, auto_mode, leech_mode = await _render_home_text(user_id)
        await message.reply_text(
            text,
            reply_markup=build_start_keyboard(
                is_owner=is_owner,
                auto_mode=auto_mode,
                leech_mode=leech_mode,
            ),
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

    @app.on_message(filters.private & filters.command("leech"))
    async def cmd_leech(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        user_id = message.from_user.id
        user_label = _format_user_label(message.from_user)
        raw_text = (message.text or "").strip()
        parts = raw_text.split(maxsplit=1)
        target_url: str | None = None

        if len(parts) >= 2 and parts[1].strip():
            match = re.search(
                r"(?:https?://\S+|magnet:\?xt=urn:[a-z0-9]+:[a-z0-9]{16,}\S*)",
                parts[1].strip(),
                re.IGNORECASE,
            )
            if not match:
                await message.reply_text(
                    "⬇️ **Uso de `/leech`**\n"
                    f"`{DIVIDER}`\n"
                    "├ `/leech https://...` o `/leech magnet:?...` — Descarga directa sin recomprimir\n"
                    "├ Responde `/leech` a un enlace enviado antes\n"
                    "└ `/leech` (solo) — Activa o desactiva el **Modo Leech** para tus próximos enlaces"
                )
                return
            target_url = match.group(0)
        elif message.reply_to_message:
            reply_txt = (
                message.reply_to_message.text
                or message.reply_to_message.caption
                or ""
            ).strip()
            match = re.search(
                r"(?:https?://\S+|magnet:\?xt=urn:[a-z0-9]+:[a-z0-9]{16,}\S*)",
                reply_txt,
                re.IGNORECASE,
            )
            if match:
                target_url = match.group(0)

        if target_url:
            await _handle_url_request(
                message=message,
                user_id=user_id,
                user_label=user_label,
                url_text=target_url,
                force_leech=True,
            )
            return

        currently_on = await _is_leech_mode(user_id)
        new_state = not currently_on
        await _set_leech_mode(user_id, new_state)
        if new_state:
            await message.reply_text(
                "⬇️ **Modo Leech ACTIVADO**\n"
                f"`{DIVIDER}`\n"
                "Los enlaces que envíes ahora se descargarán **sin recomprimir**.\n"
                "├ Si hay varias resoluciones (ej. YouTube), podrás elegir calidad y peso antes de bajar.\n"
                "├ Si es un archivo o enlace directo, se enviará tal cual.\n"
                "└ Los videos reenviados de Telegram siguen abriendo el Encoder normal.\n\n"
                "💡 *Vuelve a escribir `/leech` para regresar al modo Encoder por defecto.*"
            )
        else:
            await message.reply_text(
                "🎬 **Modo Leech DESACTIVADO (Modo Encoder)**\n"
                f"`{DIVIDER}`\n"
                "Los enlaces volverán a abrir el panel de compresión.\n"
                "💡 *Aún puedes usar `/leech <enlace>` cuando quieras bajar algo puntual sin comprimir.*"
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

    @app.on_callback_query(
        filters.regex(r"^home:(main|queue|stats|help|toggle_auto|toggle_leech)$")
    )
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
            text, auto_mode, leech_mode = await _render_home_text(user_id)
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    text,
                    reply_markup=build_start_keyboard(
                        is_owner=is_owner,
                        auto_mode=auto_mode,
                        leech_mode=leech_mode,
                    ),
                )
            return

        if action == "toggle_leech":
            cur_leech = await _is_leech_mode(user_id)
            await _set_leech_mode(user_id, not cur_leech)
            await query.answer(f"Modo Leech: {'ON' if not cur_leech else 'OFF'}")
            text, auto_mode, leech_mode = await _render_home_text(user_id)
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    text,
                    reply_markup=build_start_keyboard(
                        is_owner=is_owner,
                        auto_mode=auto_mode,
                        leech_mode=leech_mode,
                    ),
                )
            return

        if action == "main":
            await query.answer()
            text, auto_mode, leech_mode = await _render_home_text(user_id)
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    text,
                    reply_markup=build_start_keyboard(
                        is_owner=is_owner,
                        auto_mode=auto_mode,
                        leech_mode=leech_mode,
                    ),
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

        # 0b. Archivo .torrent enviado al chat -> Descarga P2P directa
        if _is_torrent_document(message):
            if not await _check_access(message):
                return
            t_name = message.document.file_name or f"archivo_{message.id}.torrent"
            await _enqueue_leech_job(
                message=message,
                user_id=user_id,
                user_label=user_label,
                filename=t_name,
                file_size=message.document.file_size,
                opts=(await _load_user_options(user_id))[0],
                source_message=message,
                is_torrent=True,
            )
            return

        # 0c. Archivo comprimido (.zip, .rar, .7z, .tar.*) -> Guardar / Descomprimir en /ls
        if _is_archive_document(message) and not _is_video_document(message):
            if not await _check_access(message):
                return
            _cleanup_old_sessions()
            doc = message.document
            a_name = doc.file_name or f"archivo_{message.id}.zip"
            a_size = int(doc.file_size or 0)
            tok = secrets.token_hex(5)
            prompt_msg = await message.reply_text(
                f"📦 **Archivo Comprimido Detectado**\n"
                f"`{DIVIDER}`\n"
                f"├ **Nombre:** `{a_name[:60]}`\n"
                f"└ **Peso:** `{human_bytes(a_size)}`\n\n"
                "Elige si quieres guardarlo o descomprimirlo en el workspace (`/ls`):",
                reply_markup=build_archive_action_keyboard(tok),
                quote=True,
            )
            archive_drafts[tok] = ArchiveDraft(
                token=tok,
                user_id=user_id,
                user_label=user_label,
                chat_id=message.chat.id,
                filename=a_name,
                file_size=a_size,
                source_message=message,
                prompt_message=prompt_msg,
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
                "leech",
                "ls",
                "extract",
                "unzip",
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

        # 1. Inputs interactivos del panel inline (/ls contraseña, comprimido contraseña, o DraftSession)
        if user_id in awaiting_input:
            sid, mode = awaiting_input.pop(user_id)

            if mode == "arc_pw":
                draft = archive_drafts.pop(sid, None)
                if not draft:
                    await message.reply_text("⚠️ Esa solicitud expiró. Reenvía el archivo comprimido.")
                    return
                if text.lower() in {"cancelar", "cancel"}:
                    with contextlib.suppress(Exception):
                        await message.delete()
                    return
                with contextlib.suppress(Exception):
                    await message.delete()
                await _enqueue_workspace_job(
                    message=message,
                    user_id=user_id,
                    user_label=user_label,
                    filename=draft.filename,
                    file_size=draft.file_size,
                    action_kind="extract_to_workspace",
                    source_message=draft.source_message,
                    source_url=draft.source_url,
                    resolved_host=draft.resolved_host,
                    archive_password=text,
                    status_message=draft.prompt_message,
                )
                return

            if mode.startswith("ls_pw:"):
                ls_state = ls_views.get(sid)
                if not ls_state:
                    await message.reply_text("⚠️ Esa vista de `/ls` expiró. Ejecuta `/ls` de nuevo.")
                    return
                if text.lower() in {"cancelar", "cancel"}:
                    with contextlib.suppress(Exception):
                        await message.delete()
                    return
                try:
                    idx = int(mode.split(":", 1)[1])
                    entry = ls_state.entries[idx]
                    target_path = resolve_workspace_path(config.workspace_dir, entry.rel_path)
                except (ValueError, IndexError):
                    await message.reply_text("⚠️ No se encontró ese archivo en `/ls`.")
                    return
                with contextlib.suppress(Exception):
                    await message.delete()
                await _enqueue_workspace_job(
                    message=message,
                    user_id=user_id,
                    user_label=user_label,
                    filename=entry.name,
                    file_size=entry.size_bytes,
                    action_kind="extract_to_workspace",
                    local_source_path=target_path,
                    archive_password=text,
                )
                return

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

        # 2. Enlace directo HTTP(S) o YouTube / TikTok / redes
        if URL_REGEX.match(text):
            if not await _check_access(message):
                return
            await _handle_url_request(
                message=message,
                user_id=user_id,
                user_label=user_label,
                url_text=text,
                force_leech=False,
            )

    async def _refresh_session_message(session: DraftSession, view: str = "main") -> None:
        if view in {"main", "dl_menu"}:
            session.url_menu_return_to = view

        show_over_2gb, can_split_over_2gb = await _compute_over_2gb_flags(session)
        effective_over_2gb: Literal["auto", "link", "split"] = (
            "link" if (show_over_2gb and not can_split_over_2gb) else session.over_2gb_mode
        )
        session.over_2gb_mode = effective_over_2gb

        summary = format_draft_summary(
            filename=session.filename,
            file_size=session.file_size,
            duration=session.duration,
            width=session.width,
            height=session.height,
            opts=session.options,
            media=session.media,
            source_is_url=bool(session.source_url),
            url_info=session.url_info,
            selected_dl_height=session.download_max_height,
            selected_audio_lang=session.selected_audio_lang,
            embed_subs=session.embed_subs,
            over_2gb_mode=effective_over_2gb,
            show_over_2gb=show_over_2gb,
            can_split_over_2gb=can_split_over_2gb,
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
        elif view == "aud_menu":
            kb = build_url_audio_lang_keyboard(
                session.session_id,
                session.url_info,
                session.selected_audio_lang,
                back_view=session.url_menu_return_to or "dl_menu",
            )
        elif view == "dl_menu":
            kb = build_url_quality_keyboard(
                session.session_id,
                session.url_info,
                session.download_max_height,
                selected_audio_lang=session.selected_audio_lang,
                embed_subs=session.embed_subs,
                over_2gb_mode=effective_over_2gb,
                show_over_2gb=show_over_2gb,
                can_split_over_2gb=can_split_over_2gb,
            )
        else:
            kb = build_main_config_keyboard(
                session.session_id,
                session.options,
                source_is_url=bool(session.source_url),
                url_info=session.url_info,
                selected_dl_height=session.download_max_height,
                selected_audio_lang=session.selected_audio_lang,
                embed_subs=session.embed_subs,
                over_2gb_mode=effective_over_2gb,
                show_over_2gb=show_over_2gb,
                can_split_over_2gb=can_split_over_2gb,
            )

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

        if action in {"main", "presets", "advanced", "target", "tracks", "extras", "tools", "dl_menu", "aud_menu"}:
            await query.answer()
            await _refresh_session_message(session, view=action)
            return

        if action == "set_dl_res" and len(parts) >= 4:
            with contextlib.suppress(ValueError):
                target_h = int(parts[3])
                session.download_max_height = target_h
                label = f"{target_h}p"
                if session.url_info and session.url_info.formats:
                    for fmt in session.url_info.formats:
                        if fmt.height == target_h:
                            session.file_size = fmt.est_bytes
                            session.width = fmt.width
                            session.height = fmt.height
                            w_str = f" (~{human_bytes(fmt.est_bytes)})" if fmt.est_bytes else ""
                            label = f"{fmt.label}{w_str}"
                            break
                await query.answer(f"Calidad descarga: {label}")
            await _refresh_session_message(session, view="dl_menu")
            return

        if action == "set_aud_lang" and len(parts) >= 4:
            raw_lang = ":".join(parts[3:])
            session.selected_audio_lang = None if raw_lang == "auto" else raw_lang
            await query.answer(
                "🔊 Audio URL: Auto / Original"
                if not session.selected_audio_lang
                else f"🔊 Audio URL: {session.selected_audio_lang}"
            )
            await _refresh_session_message(session, view=session.url_menu_return_to or "dl_menu")
            return

        if action == "toggle_url_subs":
            session.embed_subs = not session.embed_subs
            await query.answer(
                "💬 Subtítulos MKV: ON (Reproductor MX)"
                if session.embed_subs
                else "💬 Subtítulos MKV: OFF (WebM)"
            )
            await _refresh_session_message(session, view=session.url_menu_return_to or "dl_menu")
            return

        if action == "over2gb_info":
            await query.answer(
                "El espacio libre en disco no alcanza para guardar el archivo y dividirlo en partes (<1.9 GB). Se entregará por Enlace (>2 GB).",
                show_alert=True,
            )
            return

        if action == "cycle_over2gb":
            show_over_2gb, can_split = await _compute_over_2gb_flags(session)
            if not show_over_2gb or not can_split:
                session.over_2gb_mode = "link"
                await query.answer(
                    "Disco justo: solo está disponible el envío por Enlace (>2 GB).",
                    show_alert=True,
                )
            else:
                order: tuple[Literal["auto", "split", "link"], ...] = ("auto", "split", "link")
                idx = order.index(session.over_2gb_mode) if session.over_2gb_mode in order else 0
                nxt = order[(idx + 1) % len(order)]
                session.over_2gb_mode = nxt
                labels = {
                    "auto": "Al terminar: Preguntar Partes vs Enlace",
                    "split": "Si >2 GB: Partes Telegram (<1.9 GB)",
                    "link": "Si >2 GB: Enlace directo",
                }
                await query.answer(labels[nxt])
            await _refresh_session_message(session, view=session.url_menu_return_to or "main")
            return

        if action == "url_audio" and len(parts) >= 4 and parts[3] in {"opus", "mp3"}:
            if not session.source_url:
                await query.answer("Solo disponible para enlaces URL.", show_alert=True)
                return
            fmt_audio: ActionKind = "extract_opus" if parts[3] == "opus" else "extract_mp3"
            sessions.pop(sid, None)
            awaiting_input.pop(session.user_id, None)
            job_id = secrets.token_hex(4)
            job = CompressionJob(
                job_id=job_id,
                user_id=session.user_id,
                user_label=session.user_label,
                chat_id=session.chat_id,
                status_message=session.config_message,
                filename=session.filename,
                options=session.options.clone(),
                source_url=session.source_url,
                expected_size=60 * 1024 * 1024,
                action_kind=fmt_audio,
                audio_lang=session.selected_audio_lang,
                source_is_direct=bool(session.url_info and session.url_info.is_direct),
            )
            pos = await queue_mgr.enqueue(job)
            await query.answer(f"Audio {parts[3].upper()} encolado (#{pos})")
            lang_line = (
                f"\n├ **Idioma:** `{session.selected_audio_lang}`"
                if session.selected_audio_lang
                else ""
            )
            await session.config_message.edit_text(
                f"🎵 **Solo Audio ({parts[3].upper()}) · En Cola (#{pos})**\n"
                f"`{DIVIDER}`\n"
                f"├ **Archivo:** `{session.filename[:56]}`{lang_line}\n"
                f"└ **Trabajo:** `{job_id}`",
                reply_markup=build_job_cancel_keyboard(job_id),
            )
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
                "split_parts": "✂️ Dividir (<1.9 GB)",
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
                local_source_path=session.local_source_path,
                download_max_height=session.download_max_height,
                expected_size=session.file_size or (500 * 1024 * 1024),
                session_id=session.session_id,
                keep_cached_source=True,
                action_kind=tool_kind,
                audio_lang=session.selected_audio_lang,
                source_is_direct=bool(session.url_info and session.url_info.is_direct),
                over_2gb_mode=session.over_2gb_mode,
                resolved_host=session.url_info.resolved_host if session.url_info else None,
                on_media_probed=_on_probed,
            )
            pos = await queue_mgr.enqueue(job)
            await query.answer(f"{tool_titles[tool_kind]} encolado (#{pos})")
            return

        if action == "download_only":
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
                local_source_path=session.local_source_path,
                download_max_height=session.download_max_height,
                expected_size=session.file_size or (500 * 1024 * 1024),
                session_id=session.session_id if has_cache else None,
                keep_cached_source=False,
                action_kind="download_only",
                expect_video=bool(session.url_info and session.url_info.formats),
                audio_lang=session.selected_audio_lang,
                embed_subs=session.embed_subs,
                sub_langs=_resolve_sub_langs(session.url_info),
                source_is_direct=bool(session.url_info and session.url_info.is_direct),
                over_2gb_mode=session.over_2gb_mode,
                resolved_host=session.url_info.resolved_host if session.url_info else None,
            )
            pos = await queue_mgr.enqueue(job)
            await query.answer(f"Descarga directa encolada (#{pos})")
            res_note = (
                f" (`{session.download_max_height}p`)"
                if session.download_max_height
                else ""
            )
            subs_note = " · `Subs MKV`" if session.embed_subs else ""
            lang_note = f" · `Audio {session.selected_audio_lang}`" if session.selected_audio_lang else ""
            await session.config_message.edit_text(
                f"⬇️ **Descarga Directa · En Cola (#{pos})**\n"
                f"`{DIVIDER}`\n"
                f"├ **Archivo:** `{session.filename[:56]}`{res_note}{lang_note}{subs_note}\n"
                f"└ **Trabajo:** `{job_id}`",
                reply_markup=build_job_cancel_keyboard(job_id),
            )
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
                local_source_path=session.local_source_path,
                download_max_height=session.download_max_height,
                expected_size=session.file_size or (500 * 1024 * 1024),
                session_id=session.session_id,
                keep_cached_source=True,
                audio_lang=session.selected_audio_lang,
                source_is_direct=bool(session.url_info and session.url_info.is_direct),
                resolved_host=session.url_info.resolved_host if session.url_info else None,
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
                local_source_path=session.local_source_path,
                download_max_height=session.download_max_height,
                expected_size=session.file_size or (500 * 1024 * 1024),
                session_id=session.session_id if has_cache else None,
                keep_cached_source=False,
                audio_lang=session.selected_audio_lang,
                source_is_direct=bool(session.url_info and session.url_info.is_direct),
                over_2gb_mode=session.over_2gb_mode,
                resolved_host=session.url_info.resolved_host if session.url_info else None,
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

    # ── Elección interactiva >2 GB (Partes Telegram vs Enlace) ────────────────

    @app.on_callback_query(filters.regex(r"^over2gb:(split|link|cancel):"))
    async def cb_over2gb_choice(_client: Client, query: CallbackQuery) -> None:
        parts = query.data.split(":", 2)
        if len(parts) < 3:
            await query.answer()
            return
        choice, tok = parts[1], parts[2]
        pending = queue_mgr.get_pending_over2gb(tok)
        if not pending:
            await query.answer(
                "Esta elección expiró o ya fue procesada.",
                show_alert=True,
            )
            return
        if query.from_user.id != pending.user_id and query.from_user.id != config.owner_id:
            await query.answer("Solo el dueño de este trabajo puede decidir la entrega.", show_alert=True)
            return

        pending = queue_mgr.pop_pending_over2gb(tok)
        if not pending:
            await query.answer("Ya procesado.")
            return

        if choice == "cancel":
            await asyncio.to_thread(shutil.rmtree, pending.pending_dir, True)
            await query.answer("Cancelado y disco liberado")
            with contextlib.suppress(Exception):
                await pending.status_message.edit_text("✖ Entrega >2 GB cancelada y temporales liberados.")
            return

        action_kind: ActionKind = "deliver_split" if choice == "split" else "deliver_link"
        job_id = secrets.token_hex(4)
        job = CompressionJob(
            job_id=job_id,
            user_id=pending.user_id,
            user_label=pending.user_label or _format_user_label(query.from_user),
            chat_id=pending.chat_id,
            status_message=pending.status_message,
            filename=pending.filename,
            options=pending.options,
            source_url=pending.source_url,
            expected_size=pending.output_size,
            action_kind=action_kind,
            precomputed_file=pending.output_path,
            precomputed_dir=pending.pending_dir,
            precomputed_input_size=pending.input_size,
            precomputed_media=pending.out_media,
            precomputed_reduction_pct=pending.reduction_pct,
            precomputed_elapsed=pending.elapsed_total,
            precomputed_notes=pending.encode_notes,
            precomputed_record_stats=pending.record_stats,
        )
        pos = await queue_mgr.enqueue(job)
        label = "Partes Telegram (<1.9 GB)" if choice == "split" else "Enlace (>2 GB)"
        await query.answer(f"{label} encolado (#{pos})")
        with contextlib.suppress(Exception):
            await pending.status_message.edit_text(
                f"⏳ **{label} · En Cola (#{pos})**\n"
                f"`{DIVIDER}`\n"
                f"├ **Archivo:** `{pending.output_path.name[:56]}`\n"
                f"└ **Trabajo:** `{job_id}`",
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

    # ── Explorador de archivos /ls y descompresión en servidor ───────────────

    async def _render_ls_view(state: LsViewState) -> tuple[str, InlineKeyboardMarkup]:
        norm_rel, entries = await asyncio.to_thread(
            list_workspace_dir, config.workspace_dir, state.rel_path
        )
        state.rel_path = norm_rel
        state.entries = entries
        page_size = 8
        total_pages = max(1, (len(entries) + page_size - 1) // page_size)
        state.page = max(0, min(state.page, total_pages - 1))
        free_bytes = await asyncio.to_thread(queue_mgr._effective_free_bytes)
        total_folder_bytes = sum(e.size_bytes for e in entries)

        path_label = f"/{norm_rel}" if norm_rel else "/ (raíz)"
        if entries:
            body_lines = (
                f"├ **Elementos:** `{len(entries)}` · **Tamaño aquí:** `{human_bytes(total_folder_bytes)}`\n"
                f"└ **Disco libre seguro:** `{human_bytes(free_bytes)}`\n\n"
                "Toca un elemento para abrirlo, descomprimirlo, comprimirlo o enviarlo:"
            )
        else:
            body_lines = (
                f"└ **Disco libre seguro:** `{human_bytes(free_bytes)}`\n\n"
                "📭 *Esta carpeta está vacía.*\n"
                "💡 Envía un archivo `.zip`/`.rar`/`.7z` al chat o usa `/extract <enlace>` para descomprimirlo aquí."
            )

        text = (
            f"📂 **Explorador de Servidor (`/ls`)**\n"
            f"`{DIVIDER}`\n"
            f"├ **Ruta:** `{path_label}`\n"
            f"{body_lines}"
        )
        kb = build_ls_keyboard(
            tok=state.token,
            entries=entries,
            page=state.page,
            total_pages=total_pages,
            has_parent=bool(norm_rel),
        )
        return text, kb

    async def _open_ls_message(
        target: Message,
        user_id: int,
        rel_path: str = "",
        edit_existing: bool = False,
    ) -> None:
        _cleanup_old_sessions()
        tok = secrets.token_hex(5)
        state = LsViewState(
            token=tok,
            user_id=user_id,
            chat_id=target.chat.id,
            rel_path=rel_path,
            entries=[],
            page=0,
        )
        ls_views[tok] = state
        text, kb = await _render_ls_view(state)
        if edit_existing:
            with contextlib.suppress(Exception):
                await target.edit_text(text, reply_markup=kb)
                state.message = target
                return
        sent = await target.reply_text(text, reply_markup=kb, quote=True)
        state.message = sent

    @app.on_message(filters.private & filters.command("ls"))
    async def cmd_ls(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        parts = (message.text or "").split(maxsplit=1)
        rel_arg = parts[1].strip() if len(parts) >= 2 else ""
        try:
            resolve_workspace_path(config.workspace_dir, rel_arg)
        except ValueError:
            await message.reply_text("❌ Ruta inválida: no puedes salir del espacio de trabajo.")
            return
        await _open_ls_message(message, message.from_user.id, rel_path=rel_arg)

    @app.on_message(filters.private & filters.command(["extract", "unzip"]))
    async def cmd_extract(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        user_id = message.from_user.id
        user_label = _format_user_label(message.from_user)
        raw_parts = (message.text or "").split()
        args = raw_parts[1:]

        # Caso 1: responde a un documento comprimido o a un mensaje con URL
        reply = message.reply_to_message
        if reply and not args:
            if _is_archive_document(reply):
                doc = reply.document
                await _enqueue_workspace_job(
                    message=message,
                    user_id=user_id,
                    user_label=user_label,
                    filename=doc.file_name or f"archivo_{reply.id}.zip",
                    file_size=doc.file_size,
                    action_kind="extract_to_workspace",
                    source_message=reply,
                )
                return
            rtxt = (reply.text or reply.caption or "").strip()
            m_url = re.search(r"https?://\S+", rtxt, re.IGNORECASE)
            if m_url:
                args = [m_url.group(0)]

        if reply and args and not re.match(r"^https?://", args[0], re.IGNORECASE):
            if _is_archive_document(reply):
                doc = reply.document
                await _enqueue_workspace_job(
                    message=message,
                    user_id=user_id,
                    user_label=user_label,
                    filename=doc.file_name or f"archivo_{reply.id}.zip",
                    file_size=doc.file_size,
                    action_kind="extract_to_workspace",
                    source_message=reply,
                    archive_password=args[0],
                )
                return

        if not args or not re.match(r"^https?://\S+$", args[0], re.IGNORECASE):
            await message.reply_text(
                "📦 **Uso de `/extract`**\n"
                f"`{DIVIDER}`\n"
                "├ `/extract https://... [clave]` — Descarga un `.zip`/`.rar`/`.7z` y lo descomprime en `/ls`\n"
                "└ Responde `/extract [clave]` a un archivo comprimido enviado en el chat"
            )
            return

        target_url = args[0]
        pw = args[1] if len(args) >= 2 else None
        probe_msg = await message.reply_text("🔎 **Analizando archivo comprimido...**", quote=True)
        try:
            url_info = await probe_url_info(target_url, gofile_token=config.gofile_token)
            fname = url_info.filename or Path(urlparse(target_url).path).name or "archivo.zip"
            fsize = url_info.direct_bytes
            rhost = url_info.resolved_host
        except Exception as exc:
            with contextlib.suppress(Exception):
                await probe_msg.edit_text(f"❌ {exc}")
            return

        await _enqueue_workspace_job(
            message=message,
            user_id=user_id,
            user_label=user_label,
            filename=fname,
            file_size=fsize,
            action_kind="extract_to_workspace",
            source_url=target_url,
            resolved_host=rhost,
            archive_password=pw,
            status_message=probe_msg,
        )

    @app.on_callback_query(filters.regex(r"^home:ls$"))
    async def cb_home_ls(_client: Client, query: CallbackQuery) -> None:
        user_id = query.from_user.id
        if not await db.is_authorized(user_id, config.owner_id):
            await query.answer("Acceso restringido.", show_alert=True)
            return
        await query.answer()
        await _open_ls_message(query.message, user_id, rel_path="", edit_existing=False)

    @app.on_callback_query(filters.regex(r"^arc:(extract|extractpw|save|discard):"))
    async def cb_archive_draft(_client: Client, query: CallbackQuery) -> None:
        parts = query.data.split(":", 2)
        if len(parts) < 3:
            await query.answer()
            return
        action, tok = parts[1], parts[2]
        draft = archive_drafts.get(tok)
        if not draft:
            await query.answer("Esta opción expiró. Reenvía el archivo.", show_alert=True)
            return
        if query.from_user.id != draft.user_id and query.from_user.id != config.owner_id:
            await query.answer("Solo quien envió el archivo puede gestionarlo.", show_alert=True)
            return

        if action == "discard":
            archive_drafts.pop(tok, None)
            await query.answer("Descartado")
            with contextlib.suppress(Exception):
                await query.message.edit_text("✖ Archivo comprimido descartado.")
            return

        if action == "extractpw":
            awaiting_input[query.from_user.id] = (tok, "arc_pw")
            await query.answer("Envía la contraseña por mensaje")
            await query.message.reply_text(
                f"🔑 **Contraseña para `{draft.filename[:48]}`**\n"
                "Escribe la contraseña en el siguiente mensaje *(o `cancelar` para abortar)*."
            )
            return

        archive_drafts.pop(tok, None)
        kind: Literal["save_to_workspace", "extract_to_workspace"] = (
            "extract_to_workspace" if action == "extract" else "save_to_workspace"
        )
        await query.answer("Encolando...")
        await _enqueue_workspace_job(
            message=query.message,
            user_id=draft.user_id,
            user_label=draft.user_label,
            filename=draft.filename,
            file_size=draft.file_size,
            action_kind=kind,
            source_message=draft.source_message,
            source_url=draft.source_url,
            resolved_host=draft.resolved_host,
            status_message=query.message,
        )

    @app.on_callback_query(filters.regex(r"^ls:"))
    async def cb_ls(_client: Client, query: CallbackQuery) -> None:
        user_id = query.from_user.id
        if not await db.is_authorized(user_id, config.owner_id):
            await query.answer("Acceso restringido.", show_alert=True)
            return

        parts = query.data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "openroot":
            await query.answer()
            await _open_ls_message(query.message, user_id, rel_path="", edit_existing=False)
            return

        if len(parts) < 3:
            await query.answer()
            return

        tok = parts[2]
        state = ls_views.get(tok)
        if not state:
            await query.answer("Esta vista de /ls expiró. Ejecuta /ls nuevamente.", show_alert=True)
            return
        if user_id != state.user_id and user_id != config.owner_id:
            await query.answer("No puedes controlar la vista /ls de otro usuario.", show_alert=True)
            return

        if action == "noop":
            await query.answer()
            return

        if action == "refresh":
            await query.answer("Actualizado")
            text, kb = await _render_ls_view(state)
            with contextlib.suppress(Exception):
                await query.message.edit_text(text, reply_markup=kb)
            return

        if action == "up":
            state.rel_path = state.rel_path.rsplit("/", 1)[0] if "/" in state.rel_path else ""
            state.page = 0
            await query.answer()
            text, kb = await _render_ls_view(state)
            with contextlib.suppress(Exception):
                await query.message.edit_text(text, reply_markup=kb)
            return

        if action == "page" and len(parts) >= 4:
            with contextlib.suppress(ValueError):
                state.page = max(0, int(parts[3]))
            await query.answer()
            text, kb = await _render_ls_view(state)
            with contextlib.suppress(Exception):
                await query.message.edit_text(text, reply_markup=kb)
            return

        if action == "clearask":
            await query.answer()
            confirm_kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Sí, vaciar todo /ls", callback_data=f"ls:clearok:{tok}"),
                        InlineKeyboardButton("✖ Cancelar", callback_data=f"ls:refresh:{tok}"),
                    ]
                ]
            )
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    "⚠️ **¿Vaciar todo el Workspace (`/ls`)?**\n"
                    "Se eliminarán permanentemente todos los archivos y carpetas extraídas dentro de `/ls`.",
                    reply_markup=confirm_kb,
                )
            return

        if action == "clearok":
            ws_root = config.workspace_dir.resolve()
            if ws_root.exists():
                for child in list(ws_root.iterdir()):
                    if child.is_dir():
                        await asyncio.to_thread(shutil.rmtree, child, True)
                    else:
                        with contextlib.suppress(OSError):
                            child.unlink()
            state.rel_path = ""
            state.page = 0
            await query.answer("Workspace vaciado", show_alert=True)
            text, kb = await _render_ls_view(state)
            with contextlib.suppress(Exception):
                await query.message.edit_text(text, reply_markup=kb)
            return

        if len(parts) < 4:
            await query.answer()
            return
        try:
            idx = int(parts[3])
            entry = state.entries[idx]
            target_path = resolve_workspace_path(config.workspace_dir, entry.rel_path)
        except (ValueError, IndexError):
            await query.answer("Elemento no encontrado; refrescando /ls.", show_alert=True)
            text, kb = await _render_ls_view(state)
            with contextlib.suppress(Exception):
                await query.message.edit_text(text, reply_markup=kb)
            return

        if not target_path.exists():
            await query.answer("Ese archivo ya no existe.", show_alert=True)
            text, kb = await _render_ls_view(state)
            with contextlib.suppress(Exception):
                await query.message.edit_text(text, reply_markup=kb)
            return

        if action == "open":
            if entry.is_dir:
                state.rel_path = entry.rel_path
                state.page = 0
                await query.answer()
                text, kb = await _render_ls_view(state)
                with contextlib.suppress(Exception):
                    await query.message.edit_text(text, reply_markup=kb)
                return

            tg_limit = await queue_mgr.effective_tg_limit_bytes(user_id)
            is_over = entry.size_bytes > tg_limit
            can_split = (
                await asyncio.to_thread(queue_mgr.can_split_for_bytes, entry.size_bytes, True)
                if (is_over and entry.kind == "video")
                else False
            )
            kind_labels = {
                "archive": "Archivo Comprimido",
                "video": "Video",
                "audio": "Audio",
                "other": "Archivo / Documento",
            }
            await query.answer()
            with contextlib.suppress(Exception):
                await query.message.edit_text(
                    f"📄 **Archivo en `/ls`**\n"
                    f"`{DIVIDER}`\n"
                    f"├ **Nombre:** `{entry.name[:64]}`\n"
                    f"├ **Ruta:** `/{entry.rel_path[:60]}`\n"
                    f"├ **Tipo:** `{kind_labels.get(entry.kind, 'Archivo')}`\n"
                    f"└ **Peso:** `{human_bytes(entry.size_bytes)}`",
                    reply_markup=build_ls_item_keyboard(
                        tok=tok,
                        idx=idx,
                        entry=entry,
                        is_over_2gb=is_over,
                        can_split=can_split,
                    ),
                )
            return

        if action == "del":
            if target_path.is_dir():
                await asyncio.to_thread(shutil.rmtree, target_path, True)
            else:
                target_path.unlink(missing_ok=True)
            await query.answer(f"Eliminado: {entry.name[:24]}")
            text, kb = await _render_ls_view(state)
            with contextlib.suppress(Exception):
                await query.message.edit_text(text, reply_markup=kb)
            return

        if action == "extract":
            await query.answer("Descomprimiendo en cola...")
            await _enqueue_workspace_job(
                message=query.message,
                user_id=user_id,
                user_label=_format_user_label(query.from_user),
                filename=entry.name,
                file_size=entry.size_bytes,
                action_kind="extract_to_workspace",
                local_source_path=target_path,
            )
            return

        if action == "extractpw":
            awaiting_input[user_id] = (tok, f"ls_pw:{idx}")
            await query.answer("Envía la contraseña por mensaje")
            await query.message.reply_text(
                f"🔑 **Contraseña para `{entry.name[:48]}`**\n"
                "Escribe la contraseña en el siguiente mensaje *(o `cancelar` para abortar)*."
            )
            return

        if action == "compress":
            opts, _ = await _load_user_options(user_id)
            media_info: MediaInfo | None = None
            with contextlib.suppress(Exception):
                media_info = await probe_media(target_path)
            sid = secrets.token_hex(5)
            summary = format_draft_summary(
                filename=entry.name,
                file_size=entry.size_bytes,
                duration=media_info.duration if media_info else None,
                width=media_info.width if media_info else None,
                height=media_info.height if media_info else None,
                opts=opts,
                media=media_info,
            )
            cfg_msg = await query.message.reply_text(
                summary,
                reply_markup=build_main_config_keyboard(sid, opts),
                quote=True,
            )
            sessions[sid] = DraftSession(
                session_id=sid,
                user_id=user_id,
                user_label=_format_user_label(query.from_user),
                chat_id=query.message.chat.id,
                config_message=cfg_msg,
                filename=entry.name,
                file_size=entry.size_bytes,
                duration=media_info.duration if media_info else None,
                width=media_info.width if media_info else None,
                height=media_info.height if media_info else None,
                options=opts,
                local_source_path=target_path,
                media=media_info,
            )
            await query.answer("Panel de compresión abierto")
            return

        if action in {"mediainfo", "split", "send"}:
            opts, _ = await _load_user_options(user_id)
            kind_map: dict[str, ActionKind] = {
                "mediainfo": "mediainfo",
                "split": "split_parts",
                "send": "download_only",
            }
            act_kind = kind_map[action]
            job_id = secrets.token_hex(4)
            title_map = {
                "mediainfo": "🔎 **MediaInfo (`/ls`)**",
                "split": "✂️ **Dividir Partes (<1.9 GB)**",
                "send": "📤 **Enviando desde `/ls`**",
            }
            status_msg = await query.message.reply_text(
                f"{title_map[action]} · En cola\n"
                f"`{DIVIDER}`\n"
                f"├ **Archivo:** `{entry.name[:56]}`\n"
                f"└ **Trabajo:** `{job_id}`",
                reply_markup=build_job_cancel_keyboard(job_id),
            )
            job = CompressionJob(
                job_id=job_id,
                user_id=user_id,
                user_label=_format_user_label(query.from_user),
                chat_id=query.message.chat.id,
                status_message=status_msg,
                filename=entry.name,
                options=opts,
                local_source_path=target_path,
                expected_size=entry.size_bytes,
                action_kind=act_kind,
                source_is_direct=True,
            )
            pos = await queue_mgr.enqueue(job)
            await query.answer(f"Encolado (#{pos})")
            return

