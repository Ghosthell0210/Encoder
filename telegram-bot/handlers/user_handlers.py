"""Manejadores de usuario: recepción de videos/enlaces, menús inline, cola y miniaturas."""

from __future__ import annotations

import contextlib
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from hydrogram import Client, filters
from hydrogram.types import CallbackQuery, Message

from config import BotConfig
from core.database import Database
from core.ffmpeg_engine import CompressionOptions
from core.media_probe import MediaInfo
from core.queue_manager import CompressionJob, QueueManager
from core.ui_helpers import (
    build_advanced_keyboard,
    build_job_cancel_keyboard,
    build_main_config_keyboard,
    build_presets_keyboard,
    build_target_size_keyboard,
    build_tracks_keyboard,
    format_draft_summary,
    human_bytes,
    human_duration,
)
from core.user_session import UserSessionManager

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".ts", ".wmv"}
URL_REGEX = re.compile(r"^https?://\S+$", re.IGNORECASE)


@dataclass(slots=True)
class DraftSession:
    session_id: str
    user_id: int
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


def _is_video_document(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    mime = (doc.mime_type or "").lower()
    if mime.startswith("video/"):
        return True
    name = (doc.file_name or "").lower()
    return Path(name).suffix in VIDEO_EXTENSIONS


def register_user_handlers(
    app: Client,
    config: BotConfig,
    db: Database,
    queue_mgr: QueueManager,
    user_session: UserSessionManager,
) -> None:
    sessions: dict[str, DraftSession] = {}
    awaiting_custom_mb: dict[int, str] = {}  # user_id -> session_id

    def _cleanup_old_sessions() -> None:
        now = time.time()
        expired = [sid for sid, s in sessions.items() if now - s.created_at > 3600]
        for sid in expired:
            sessions.pop(sid, None)

    async def _check_access(message: Message) -> bool:
        user = message.from_user
        if not user:
            return False
        if await db.is_authorized(user.id, config.owner_id):
            await db.ensure_user_profile(user.id, user.username or "")
            return True
        await message.reply_text(
            "🔒 **Acceso restringido**\n\n"
            f"Este bot está en modo lista blanca.\n"
            f"Tu ID de usuario es: `{user.id}`\n"
            "Compártelo con el administrador para recibir autorización."
        )
        return False

    # ── Comandos informativos y de perfil ─────────────────────────────────────

    @app.on_message(filters.private & filters.command(["start", "help"]))
    async def cmd_start(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        is_owner = message.from_user and message.from_user.id == config.owner_id
        admin_hint = (
            "\n• `/admin` — Panel de servidor, lista blanca y concurrencia"
            "\n• `/login` — Vincular tu cuenta de Telegram (MTProto / hasta 4 GB con Premium)"
            if is_owner
            else ""
        )
        await message.reply_text(
            "🎬 **HydroCompress Bot — Compresor de Videos de Alta Potencia**\n\n"
            "**Cómo usarlo:**\n"
            "1. Envíame o reenvíame cualquier **video** o **archivo de video** (MP4, MKV, MOV, AVI, WEBM hasta 2 GB).\n"
            "2. También puedes enviarme un **enlace directo `http(s)://`** de un video (incluso mayor a 2 GB).\n"
            "3. Elige un preset rápido, ajusta códec/resolución/pistas o fija un **tamaño exacto en MB (2-Pass)**.\n"
            "4. Si el resultado comprimido pesa **≤ 2 GB** lo recibes directo en Telegram; si supera **2 GB**, te entrego el **enlace de descarga directa** automáticamente.\n\n"
            "**Comandos disponibles:**\n"
            "• `/queue` — Ver trabajos activos y en cola\n"
            "• `/setthumb` — Responde a una foto para fijarla como tu miniatura\n"
            "• `/delthumb` — Borrar tu miniatura personalizada\n"
            "• `/stats` — Ver total de videos y espacio ahorrado\n"
            "• `/id` — Mostrar tu ID de Telegram"
            f"{admin_hint}"
        )

    @app.on_message(filters.private & filters.command("id"))
    async def cmd_id(_client: Client, message: Message) -> None:
        if not message.from_user:
            return
        await message.reply_text(f"🆔 Tu Telegram ID es: `{message.from_user.id}`")

    @app.on_message(filters.private & filters.command("setthumb"))
    async def cmd_setthumb(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        reply = message.reply_to_message
        if not reply or not reply.photo:
            await message.reply_text(
                "🖼 Responde con `/setthumb` a una **foto** para usarla como miniatura en tus videos comprimidos."
            )
            return
        await db.set_custom_thumb(message.from_user.id, reply.photo.file_id)
        await message.reply_text("✅ Miniatura personalizada guardada. Se aplicará a tus próximos videos.")

    @app.on_message(filters.private & filters.command("delthumb"))
    async def cmd_delthumb(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        await db.set_custom_thumb(message.from_user.id, None)
        await message.reply_text("🗑 Miniatura personalizada eliminada. Se extraerá automáticamente de cada video.")

    @app.on_message(filters.private & filters.command("queue"))
    async def cmd_queue(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        active, pending = queue_mgr.get_queue_snapshot()
        lines = [
            f"📋 **Estado de la Cola** (`{len(active)}/{queue_mgr.concurrency_limit}` activos · `{queue_mgr.threads_per_job()}` hilos/trabajo)",
            "",
        ]
        if active:
            lines.append("**En ejecución:**")
            for j in active:
                elapsed = human_duration(time.time() - (j.started_at or j.created_at))
                lines.append(f"• `{j.job_id}` — `{j.filename[:36]}` ({j.phase}, {elapsed})")
        else:
            lines.append("• No hay trabajos comprimiendo ahora mismo.")

        if pending:
            lines.append("")
            lines.append(f"**En espera ({len(pending)}):**")
            for idx, j in enumerate(pending[:10], start=1):
                lines.append(f"{idx}. `{j.job_id}` — `{j.filename[:36]}`")

        await message.reply_text("\n".join(lines))

    @app.on_message(filters.private & filters.command("stats"))
    async def cmd_stats(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        st = await db.get_stats()
        pct = (st.total_saved_bytes / max(st.total_input_bytes, 1)) * 100.0
        await message.reply_text(
            "📊 **Estadísticas Globales del Bot**\n\n"
            f"• **Videos comprimidos:** `{st.total_jobs}`\n"
            f"• **Entregas por enlace (>2 GB):** `{st.total_link_deliveries}`\n"
            f"• **Peso original procesado:** `{human_bytes(st.total_input_bytes)}`\n"
            f"• **Peso final generado:** `{human_bytes(st.total_output_bytes)}`\n"
            f"• **Espacio total ahorrado:** `{human_bytes(st.total_saved_bytes)}` (`{pct:.1f}%`)"
        )

    # ── Recepción de Video o Documento de Video ───────────────────────────────

    @app.on_message(filters.private & (filters.video | filters.document))
    async def on_video_message(_client: Client, message: Message) -> None:
        if message.document and not _is_video_document(message):
            return
        if not await _check_access(message):
            return

        _cleanup_old_sessions()
        user_id = message.from_user.id
        send_as_doc = await db.get_send_as_document(user_id)
        opts = CompressionOptions(send_as_document=send_as_doc)

        if message.video:
            v = message.video
            filename = v.file_name or f"video_{message.id}.mp4"
            file_size = v.file_size
            duration = float(v.duration) if v.duration else None
            width = v.width
            height = v.height
        else:
            d = message.document
            filename = d.file_name or f"video_{message.id}.mkv"
            file_size = d.file_size
            duration = None
            width = None
            height = None

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

    # ── Recepción de texto: tamaño manual en MB o enlace directo HTTP(S) ──────

    @app.on_message(
        filters.private
        & filters.text
        & ~filters.command(
            [
                "start",
                "help",
                "id",
                "queue",
                "stats",
                "setthumb",
                "delthumb",
                "admin",
                "login",
                "cancellogin",
                "logout",
                "setdump",
                "adduser",
                "deluser",
                "users",
                "purge",
                "cancelall",
            ]
        )
    )
    async def on_text_message(_client: Client, message: Message) -> None:
        if not message.from_user or not message.text:
            return
        user_id = message.from_user.id
        text = message.text.strip()

        # 0. ¿El dueño está completando el asistente seguro /login?
        if user_id == config.owner_id and user_session.login_in_progress:
            # Borrar inmediatamente el mensaje sensible (teléfono, código o contraseña 2FA)
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

        # 1. ¿El usuario estaba escribiendo un tamaño manual en MB?
        if user_id in awaiting_custom_mb:
            sid = awaiting_custom_mb.pop(user_id)
            session = sessions.get(sid)
            if not session:
                await message.reply_text("⚠️ Esa sesión expiró. Reenvía el video para configurarlo de nuevo.")
                return
            try:
                mb_val = float(text.replace(",", ".").replace("mb", "").replace("MB", "").strip())
                if mb_val < 2 or mb_val > 10240:
                    raise ValueError
            except ValueError:
                await message.reply_text("❌ Escribe un número válido entre `2` y `10240` (ejemplo: `45` o `150`).")
                return

            session.options.target_mb = round(mb_val, 1)
            with contextlib.suppress(Exception):
                await message.delete()
            await _refresh_session_message(session, view="main")
            return

        # 2. ¿Envió un enlace directo HTTP(S) para comprimir?
        if URL_REGEX.match(text):
            if not await _check_access(message):
                return
            _cleanup_old_sessions()
            parsed = urlparse(text)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return

            raw_name = Path(parsed.path).name or "video_enlace.mp4"
            send_as_doc = await db.get_send_as_document(user_id)
            opts = CompressionOptions(send_as_document=send_as_doc)
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

        if action in {"main", "presets", "advanced", "target", "tracks"}:
            await query.answer()
            await _refresh_session_message(session, view=action)
            return

        if action == "set_preset" and len(parts) >= 4:
            session.options.apply_preset(parts[3])
            await query.answer("Preset aplicado")
            await _refresh_session_message(session, view="main")
            return

        if action == "codec" and len(parts) >= 4:
            if parts[3] in {"libx265", "libx264"}:
                session.options.codec = parts[3]  # type: ignore[assignment]
            await query.answer(f"Códec: {parts[3]}")
            await _refresh_session_message(session, view="advanced")
            return

        if action == "res" and len(parts) >= 4:
            session.options.max_height = None if parts[3] == "orig" else int(parts[3])
            await query.answer(f"Resolución: {parts[3]}")
            await _refresh_session_message(session, view="advanced")
            return

        if action == "speed" and len(parts) >= 4:
            if parts[3] in {"ultrafast", "veryfast", "fast", "medium"}:
                session.options.speed = parts[3]  # type: ignore[assignment]
            await query.answer(f"Velocidad: {parts[3]}")
            await _refresh_session_message(session, view="advanced")
            return

        if action == "crf" and len(parts) >= 4:
            delta = int(parts[3])
            session.options.crf = max(16, min(38, session.options.crf + delta))
            session.options.target_mb = None
            await query.answer(f"CRF: {session.options.crf}")
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
            awaiting_custom_mb[session.user_id] = sid
            await query.answer("Envía ahora el tamaño en MB (ej: 75)", show_alert=True)
            return

        if action == "audio" and len(parts) >= 4:
            val = ":".join(parts[3:])
            session.options.audio_mode = val
            await query.answer("Pista de audio actualizada")
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
            await query.answer(f"Bitrate de audio: {parts[3]} kbps")
            await _refresh_session_message(session, view="tracks")
            return

        if action == "toggle_doc":
            session.options.send_as_document = not session.options.send_as_document
            await db.set_send_as_document(session.user_id, session.options.send_as_document)
            await query.answer("Modo de envío actualizado")
            await _refresh_session_message(session, view="main")
            return

        if action == "discard":
            sessions.pop(sid, None)
            awaiting_custom_mb.pop(session.user_id, None)
            await query.answer("Cancelado")
            with contextlib.suppress(Exception):
                await session.config_message.edit_text("✖ Operación descartada.")
            return

        if action == "start":
            sessions.pop(sid, None)
            awaiting_custom_mb.pop(session.user_id, None)
            job_id = secrets.token_hex(4)

            job = CompressionJob(
                job_id=job_id,
                user_id=session.user_id,
                chat_id=session.chat_id,
                status_message=session.config_message,
                filename=session.filename,
                options=session.options,
                source_message=session.source_message,
                source_url=session.source_url,
                expected_size=session.file_size or (500 * 1024 * 1024),
            )
            pos = await queue_mgr.enqueue(job)
            await query.answer(f"Trabajo #{job_id} encolado (posición {pos})")
            await session.config_message.edit_text(
                f"⏳ **En cola (posición #{pos})**\n"
                f"• **Trabajo:** `{job_id}`\n"
                f"• **Archivo:** `{session.filename[:60]}`\n"
                f"• **Hilos asignados:** `{queue_mgr.threads_per_job()}` (`{queue_mgr.concurrency_limit}` en paralelo)",
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
                "Ese acceso rápido expiró. Reenvía el video y elige 1.9 GB TG en Tamaño Exacto.",
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
            f"⏳ **Re-compresión 2-Pass a 1.90 GB (para Telegram) encolada**\n"
            f"• **Trabajo:** `{job_id}`\n"
            f"• **Archivo:** `{src.filename[:60]}`",
            reply_markup=build_job_cancel_keyboard(job_id),
        )
        job = CompressionJob(
            job_id=job_id,
            user_id=src.user_id,
            chat_id=src.chat_id,
            status_message=status_msg,
            filename=src.filename,
            options=src.base_options,
            source_message=source_msg,
            source_url=src.source_url,
            expected_size=2 * 1024 * 1024 * 1024,
        )
        pos = await queue_mgr.enqueue(job)
        await query.answer(f"Re-compresión a 1.90 GB encolada (#{pos})")
