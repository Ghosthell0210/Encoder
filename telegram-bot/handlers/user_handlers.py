"""Manejadores de usuario: recepción de videos/enlaces, menús inline, Modo Lotes (/auto),
Muestra 15s, subtítulos externos (.srt/.ass), 10-bit, Trim, Marca de Agua y Renombrado."""

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
from core.ffmpeg_engine import CompressionOptions, parse_timestamp
from core.media_probe import MediaInfo
from core.queue_manager import CompressionJob, QueueManager
from core.ui_helpers import (
    build_advanced_keyboard,
    build_extras_keyboard,
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
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}
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


def _is_subtitle_document(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    name = (doc.file_name or "").lower()
    return Path(name).suffix in SUBTITLE_EXTENSIONS


def _format_saved_preset_line(opts: CompressionOptions) -> str:
    bit = " 10-bit" if (opts.bit_depth == 10 and opts.codec == "libx265") else ""
    codec = f"H.265{bit}" if opts.codec == "libx265" else "H.264"
    res = f"{opts.max_height}p" if opts.max_height else "Original"
    mode = f"2-Pass {opts.target_mb:.0f} MB" if opts.target_mb else f"CRF {opts.crf} ({opts.speed})"
    return f"`{codec}` · `{res}` · `{mode}` · Audio `{opts.audio_bitrate_k}k`"


def register_user_handlers(
    app: Client,
    config: BotConfig,
    db: Database,
    queue_mgr: QueueManager,
    user_session: UserSessionManager,
) -> None:
    sessions: dict[str, DraftSession] = {}
    # user_id -> (session_id, mode) donde mode ∈ {"custom_mb", "rename", "trim", "watermark", "attach_sub"}
    awaiting_input: dict[int, tuple[str, str]] = {}

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

    async def _enqueue_auto_job(
        message: Message,
        user_id: int,
        filename: str,
        file_size: int | None,
        opts: CompressionOptions,
        source_message: Message | None = None,
        source_url: str | None = None,
    ) -> None:
        job_id = secrets.token_hex(4)
        status_msg = await message.reply_text(
            f"⚡ **Modo Auto — Encolando trabajo `{job_id}`...**",
            quote=True,
        )
        job = CompressionJob(
            job_id=job_id,
            user_id=user_id,
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
                f"⚡ **Modo Auto — En cola (posición #{pos})**\n"
                f"• **Trabajo:** `{job_id}`\n"
                f"• **Archivo:** `{filename[:60]}`\n"
                f"• **Ajuste:** {_format_saved_preset_line(opts)}\n"
                f"• **Hilos asignados:** `{queue_mgr.threads_per_job()}` (`{queue_mgr.concurrency_limit}` en paralelo)\n"
                "*(Usa `/auto` para volver al menú manual)*",
                reply_markup=build_job_cancel_keyboard(job_id),
            )

    # ── Comandos informativos y de perfil ─────────────────────────────────────

    @app.on_message(filters.private & filters.command(["start", "help"]))
    async def cmd_start(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        is_owner = message.from_user and message.from_user.id == config.owner_id
        admin_hint = (
            "\n• `/admin` — Panel de servidor, lista blanca, alertas y concurrencia"
            "\n• `/logs [n|errors|file]` — Ver o descargar `bot.log` con errores"
            "\n• `/update` — Actualizar el bot desde GitHub y reiniciar"
            "\n• `/login` — Vincular tu cuenta de Telegram (4 GB con Premium)"
            if is_owner
            else ""
        )
        await message.reply_text(
            "🎬 **HydroCompress Bot — Compresor de Videos de Alta Potencia**\n\n"
            "**Cómo usarlo:**\n"
            "1. Envíame o reenvíame cualquier **video** o **archivo de video** (MP4, MKV, MOV, AVI, WEBM hasta 2 GB).\n"
            "2. También puedes enviarme un **enlace directo `http(s)://`** de un video (incluso mayor a 2 GB).\n"
            "3. Elige un preset (incluye **Anime 10-bit**), ajusta códec/CRF/10-bit/pistas, adjunta subtítulos `.srt`/`.ass`, recorta (**Trim**), añade **Marca de Agua**, prueba una **Muestra 15s** o fija un **tamaño exacto en MB (2-Pass)**.\n"
            "4. Si el resultado pesa **≤ 2 GB** lo recibes directo en Telegram; si supera **2 GB**, se entrega por Premium (4 GB) o **enlace directo**.\n\n"
            "**Comandos disponibles:**\n"
            "• `/auto` — Activar/desactivar **Modo Lotes** (encola videos automáticamente con tu ajuste guardado)\n"
            "• `/queue` — Ver trabajos activos y en cola\n"
            "• `/setthumb` / `/delthumb` — Miniatura personalizada (responde a una foto)\n"
            "• `/setwatermark <texto>` / `/delwatermark` — Marca de agua fija en tus videos\n"
            "• `/stats` — Ver total de videos y espacio ahorrado\n"
            "• `/id` — Mostrar tu ID de Telegram"
            f"{admin_hint}"
        )

    @app.on_message(filters.private & filters.command("id"))
    async def cmd_id(_client: Client, message: Message) -> None:
        if not message.from_user:
            return
        await message.reply_text(f"🆔 Tu Telegram ID es: `{message.from_user.id}`")

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
                "⚡ **Modo Lotes (/auto) ACTIVADO**\n\n"
                "Ahora puedes reenviarme 5, 10 o 20 videos seguidos y todos entrarán directo a la cola sin pedir confirmación.\n"
                f"• **Ajuste activo:** {_format_saved_preset_line(opts)}\n"
                f"• **Marca de agua:** `{opts.watermark_text or 'Ninguna'}`\n\n"
                "💡 *Para cambiar tu ajuste favorito, desactiva `/auto`, configura un video y pulsa `💾 Guardar Auto`.*"
            )
        else:
            await message.reply_text(
                "🎛 **Modo Lotes (/auto) DESACTIVADO**\n"
                "Cada video volverá a mostrar el panel de configuración manual."
            )

    @app.on_message(filters.private & filters.command("setwatermark"))
    async def cmd_setwatermark(_client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.reply_text(
                "🏷 Uso: `/setwatermark @TuCanal`\n"
                "Se añadirá una marca de agua discreta a tus próximos videos. Usa `/delwatermark` para quitarla."
            )
            return
        wm_text = parts[1].strip()[:48]
        await db.set_watermark(message.from_user.id, wm_text)
        await message.reply_text(
            f"✅ Marca de agua guardada: `{wm_text}`\n"
            "*(Puedes elegir la esquina desde `✂️ Trim y Marca` al configurar un video.)*"
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

    # ── Recepción de Video, Documento de Video o Subtítulo Externo (.srt/.ass) ─

    @app.on_message(filters.private & (filters.video | filters.document))
    async def on_video_message(_client: Client, message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id

        # 0. ¿Estaba esperando un archivo de subtítulo externo (.srt / .ass / .vtt)?
        pending_state = awaiting_input.get(user_id)
        if pending_state and pending_state[1] == "attach_sub" and message.document:
            if not _is_subtitle_document(message):
                await message.reply_text(
                    "❌ Ese archivo no es un subtítulo compatible. Envía un `.srt`, `.ass`, `.ssa` o `.vtt`."
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
                f"✅ Subtítulo externo `{sub_name[:48]}` adjuntado (`{session.options.subtitle_mode}`).\n"
                "*(Puedes alternar entre Softsub y Hardsub en el panel de arriba.)*",
                quote=True,
            )
            return

        if message.document and not _is_video_document(message):
            return
        if not await _check_access(message):
            return

        _cleanup_old_sessions()
        opts, auto_mode = await _load_user_options(user_id)

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

        # Si el usuario activó /auto (Modo Lotes), encolar directamente sin menú
        if auto_mode:
            await _enqueue_auto_job(
                message=message,
                user_id=user_id,
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
                "setwatermark",
                "delwatermark",
                "queue",
                "stats",
                "setthumb",
                "delthumb",
                "admin",
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
        text = message.text.strip()

        # 0. ¿El dueño está completando el asistente seguro /login?
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

        # 1. ¿El usuario estaba respondiendo a una solicitud del panel inline?
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
                    await message.reply_text("❌ Escribe un número válido entre `2` y `10240` (ejemplo: `45` o `150`).")
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
                    # Quitar extensión si el usuario la incluyó
                    stem = Path(cleaned).stem if Path(cleaned).suffix.lower() in VIDEO_EXTENSIONS else cleaned
                    session.options.custom_output_name = stem[:80].strip()
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
                        "Ejemplos válidos: `00:30 - 05:00`, `01:20 - fin` o `15 - 90`."
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

        # 2. ¿Envió un enlace directo HTTP(S) para comprimir?
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

        if action in {"main", "presets", "advanced", "target", "tracks", "extras"}:
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
                if parts[3] == "libx264":
                    session.options.bit_depth = 8
            await query.answer(f"Códec: {parts[3]}")
            await _refresh_session_message(session, view="advanced")
            return

        if action == "toggle_10bit":
            if session.options.bit_depth == 10:
                session.options.bit_depth = 8
                await query.answer("Profundidad cambiada a 8-bit")
            else:
                session.options.bit_depth = 10
                session.options.codec = "libx265"
                await query.answer("10-bit activado (H.265 Main10)")
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
            await query.answer("Posición de marca actualizada")
            await _refresh_session_message(session, view="extras")
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

        if action == "save_auto":
            await db.save_default_options(session.user_id, session.options.to_saved_dict())
            await query.answer(
                "Ajuste guardado como favorito. Usa /auto para comprimir videos en lote automáticamente.",
                show_alert=True,
            )
            return

        if action == "sample":
            sample_opts = session.options.clone()
            sample_opts.sample_duration = 15.0
            sample_opts.target_mb = None
            job_id = secrets.token_hex(4)
            sample_status = await session.config_message.reply_text(
                f"🎞 **Muestra 15s encolada (`{job_id}`)**\n"
                f"• **Archivo:** `{session.filename[:60]}`\n"
                "*(El panel superior sigue abierto para que ajustes o inicies el video completo)*",
                reply_markup=build_job_cancel_keyboard(job_id),
            )
            job = CompressionJob(
                job_id=job_id,
                user_id=session.user_id,
                chat_id=session.chat_id,
                status_message=sample_status,
                filename=session.filename,
                options=sample_opts,
                source_message=session.source_message,
                source_url=session.source_url,
                expected_size=session.file_size or (500 * 1024 * 1024),
            )
            pos = await queue_mgr.enqueue(job)
            await query.answer(f"Muestra 15s encolada (#{pos})")
            return

        if action == "discard":
            sessions.pop(sid, None)
            awaiting_input.pop(session.user_id, None)
            await query.answer("Cancelado")
            with contextlib.suppress(Exception):
                await session.config_message.edit_text("✖ Operación descartada.")
            return

        if action == "start":
            sessions.pop(sid, None)
            awaiting_input.pop(session.user_id, None)
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
