"""Panel de administración /admin, monitor en vivo /live, /usage, /logs, /login y lista blanca."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from hydrogram import Client, filters
from hydrogram.types import CallbackQuery, Message

from config import BotConfig
from core.database import Database
from core.git_updater import pull_from_github, restart_process
from core.logger_setup import read_last_log_lines
from core.queue_manager import QueueManager
from core.resource_monitor import recommended_panel_specs, sample_live_resources
from core.ui_helpers import (
    DIVIDER,
    build_admin_keyboard,
    build_live_keyboard,
    human_bytes,
    human_duration,
)
from core.user_session import UserSessionManager


def _format_log_block(title: str, content: str) -> str:
    body = content[-3600:] if len(content) > 3600 else content
    body = body.replace("```", "'''")
    return f"📄 **{title}**\n`{DIVIDER}`\n```\n{body}\n```"


def register_admin_handlers(
    app: Client,
    config: BotConfig,
    db: Database,
    queue_mgr: QueueManager,
    user_session: UserSessionManager,
) -> None:
    log_file = config.data_dir / "bot.log"

    def _is_owner(user_id: int | None) -> bool:
        return user_id == config.owner_id

    async def _render_admin_text() -> tuple[str, str, int, str, bool]:
        settings = await db.get_all_settings()
        access_mode = settings.get("access_mode", config.default_access_mode)
        conc = queue_mgr.concurrency_limit
        uploader = settings.get("large_file_uploader", config.large_file_uploader)
        dump_id = settings.get("dump_chat_id", str(config.dump_chat_id or ""))
        error_alerts = settings.get("error_alerts", "1") == "1"
        stats = await db.get_stats()
        users = await db.list_users()
        active, pending = queue_mgr.get_queue_snapshot()

        snap = await sample_live_resources(
            work_dir=config.work_dir,
            links_dir=config.links_dir,
            bot_start_time=queue_mgr.bot_started_at,
            job_pids=queue_mgr.get_active_job_pids(),
        )

        ram_budget_bytes = int(config.max_ram_gb * (1024**3))
        disk_budget_bytes = int(config.max_disk_gb * (1024**3))
        threads_each = queue_mgr.threads_per_job()
        dump_label = dump_id if dump_id and dump_id != "0" else "Mensajes Guardados"
        log_size = human_bytes(log_file.stat().st_size) if log_file.exists() else "0 B"

        text = (
            "🛡 **Panel de Administración**\n"
            f"`{DIVIDER}`\n\n"
            "🖥 **Recursos del Contenedor**\n"
            f"├ **CPU (Bot+FFmpeg):** `{snap.bot_tree_cpu_pct:.0f}%` / `{config.max_cpu_threads * 100}%` · `{threads_each}` hilos/job\n"
            f"├ **RAM (Bot+FFmpeg):** `{human_bytes(snap.bot_tree_ram_bytes)}` / `{human_bytes(ram_budget_bytes)}`\n"
            f"├ **Disco:** `{human_bytes(snap.temp_disk_bytes)}` / `{human_bytes(disk_budget_bytes)}` · Libre `{human_bytes(snap.disk_free_bytes)}`\n"
            f"└ **Activo:** `{human_duration(snap.uptime_sec)}` · **Caché:** `{snap.cache_count}` (`{human_bytes(snap.cache_disk_bytes)}`)\n\n"
            "⚙️ **Operación**\n"
            f"├ **Cola:** `{len(active)}/{conc}` activos · `{len(pending)}` en espera\n"
            f"├ **Acceso:** `{'Lista Blanca' if access_mode == 'private' else 'Público'}` (`{len(users)}` usuarios)\n"
            f"├ **Host >2 GB:** `{uploader}` · **Canal puente:** `{dump_label}`\n"
            f"├ **Cuenta:** `{user_session.status_label()}`\n"
            f"└ **Logs:** `{log_size}` · **Auto-Log:** `{'ON' if error_alerts else 'OFF'}`\n\n"
            "📊 **Acumulado**\n"
            f"├ **Videos:** `{stats.total_jobs}` · **Enlaces >2 GB:** `{stats.total_link_deliveries}`\n"
            f"└ **Ahorro total:** `{human_bytes(stats.total_saved_bytes)}`"
        )
        return text, access_mode, conc, uploader, error_alerts

    async def _render_live_text() -> tuple[str, bool]:
        active, pending = queue_mgr.get_queue_snapshot()
        open_sessions = queue_mgr.get_active_sessions_snapshot()
        snap = await sample_live_resources(
            work_dir=config.work_dir,
            links_dir=config.links_dir,
            bot_start_time=queue_mgr.bot_started_at,
            job_pids=queue_mgr.get_active_job_pids(),
        )

        ram_budget = int(config.max_ram_gb * (1024**3))
        max_cpu = config.max_cpu_threads * 100
        conc = queue_mgr.concurrency_limit

        lines: list[str] = [
            "📡 **Monitor en Vivo**",
            f"`{DIVIDER}`",
            "",
            "🖥 **Recursos Ahora Mismo**",
            f"├ **CPU Bot+FFmpeg:** `{snap.bot_tree_cpu_pct:.0f}%` / `{max_cpu}%` *(Host `{snap.host_cpu_pct:.0f}%`)*",
            f"├ **RAM Bot+FFmpeg:** `{human_bytes(snap.bot_tree_ram_bytes)}` / `{human_bytes(ram_budget)}`",
            f"├ **Disco Temp:** `{human_bytes(snap.temp_disk_bytes)}` · **Caché:** `{snap.cache_count}` (`{human_bytes(snap.cache_disk_bytes)}`)",
            f"└ **Libre:** `{human_bytes(snap.disk_free_bytes)}` · **Uptime:** `{human_duration(snap.uptime_sec)}`",
            "",
            f"👥 **Trabajos en Ejecución ({len(active)}/{conc})**",
        ]

        now = time.time()
        if active:
            for idx, job in enumerate(active):
                branch = "└" if idx == len(active) - 1 else "├"
                user_tag = job.user_label or f"ID {job.user_id}"
                elapsed = human_duration(now - (job.started_at or job.created_at))
                pct_str = f" {job.progress_pct:.0f}%" if job.progress_pct > 0 else ""
                det_str = f" · {job.progress_detail}" if job.progress_detail else ""
                proc_u = snap.per_job.get(job.job_id)
                proc_str = (
                    f"\n   ⚡ FFmpeg: CPU `{proc_u.cpu_pct:.0f}%` · RAM `{human_bytes(proc_u.ram_bytes)}`"
                    if proc_u
                    else ""
                )
                lines.append(
                    f"{branch} **{user_tag}** (`{job.user_id}`)\n"
                    f"   📄 `{job.filename[:38]}`\n"
                    f"   ⚙️ `{job.action_badge}` · `{job.phase}{pct_str}`{det_str} (`{elapsed}`){proc_str}"
                )
        else:
            lines.append("└ *En reposo — nadie está ejecutando un trabajo ahora.*")

        if pending:
            lines.append("")
            lines.append(f"⏳ **En Cola ({len(pending)})**")
            shown_p = pending[:6]
            for idx, job in enumerate(shown_p):
                branch = "└" if idx == len(shown_p) - 1 else "├"
                user_tag = job.user_label or f"ID {job.user_id}"
                lines.append(
                    f"{branch} `{idx + 1}.` **{user_tag}** (`{job.user_id}`) · `{job.action_badge}` · `{job.filename[:30]}`"
                )

        if open_sessions:
            lines.append("")
            lines.append(f"🎛 **Configurando Panel ({len(open_sessions)})**")
            shown_s = open_sessions[:6]
            for idx, sess in enumerate(shown_s):
                branch = "└" if idx == len(shown_s) - 1 else "├"
                age = human_duration(now - sess.created_at)
                lines.append(
                    f"{branch} **{sess.user_label}** (`{sess.user_id}`) · `{sess.filename[:30]}` *(hace {age})*"
                )

        return "\n".join(lines), bool(active or pending)

    # ── /admin y /live ────────────────────────────────────────────────────────

    @app.on_message(filters.private & filters.command("admin"))
    async def cmd_admin(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            await message.reply_text("⛔ Comando exclusivo del administrador.")
            return
        text, access_mode, conc, uploader, error_alerts = await _render_admin_text()
        await message.reply_text(
            text,
            reply_markup=build_admin_keyboard(
                access_mode,
                conc,
                uploader,
                error_alerts=error_alerts,
                threads_per_job=queue_mgr.threads_per_job(),
            ),
        )

    @app.on_message(filters.private & filters.command("live"))
    async def cmd_live(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            await message.reply_text("⛔ Comando exclusivo del administrador.")
            return
        text, has_jobs = await _render_live_text()
        await message.reply_text(text, reply_markup=build_live_keyboard(has_jobs))

    @app.on_callback_query(filters.regex(r"^home:(admin|live)$"))
    async def cb_open_owner_views(_client: Client, query: CallbackQuery) -> None:
        if not _is_owner(query.from_user.id):
            await query.answer("Solo el administrador puede abrir este panel.", show_alert=True)
            return
        target = query.data.split(":", 1)[1]
        await query.answer()
        if target == "live":
            text, has_jobs = await _render_live_text()
            with contextlib.suppress(Exception):
                await query.message.edit_text(text, reply_markup=build_live_keyboard(has_jobs))
            return
        text, access_mode, conc, uploader, error_alerts = await _render_admin_text()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                text,
                reply_markup=build_admin_keyboard(
                    access_mode,
                    conc,
                    uploader,
                    error_alerts=error_alerts,
                    threads_per_job=queue_mgr.threads_per_job(),
                ),
            )

    @app.on_callback_query(filters.regex(r"^live:(refresh|cancelall)$"))
    async def cb_live(_client: Client, query: CallbackQuery) -> None:
        if not _is_owner(query.from_user.id):
            await query.answer("Solo el administrador puede usar este monitor.", show_alert=True)
            return
        action = query.data.split(":", 1)[1]
        if action == "cancelall":
            count = await queue_mgr.cancel_all()
            await query.answer(f"Cancelados {count} trabajos", show_alert=True)
        else:
            await query.answer("Monitor actualizado")
        text, has_jobs = await _render_live_text()
        with contextlib.suppress(Exception):
            await query.message.edit_text(text, reply_markup=build_live_keyboard(has_jobs))

    # ── Consumo real de RAM, CPU y disco por video (/usage [reset]) ───────────

    async def _render_usage_text() -> str:
        rep = await db.get_resource_report(limit=6)
        if rep.total_measured == 0:
            return (
                "📈 **Consumo Real por Video**\n"
                f"`{DIVIDER}`\n"
                "Todavía no hay videos medidos.\n\n"
                "Comprime al menos **1 video completo** y aquí verás el pico real de **RAM**, **CPU** y **disco** "
                "(bot + FFmpeg) junto con los GB exactos recomendados para tu panel."
            )

        ram_1, cpu_1, disk_1 = recommended_panel_specs(
            rep.max_ram_bytes, rep.max_cpu_pct, rep.max_disk_bytes
        )
        ram_2, _, disk_2 = recommended_panel_specs(
            int(rep.max_ram_bytes * 1.85), rep.max_cpu_pct, int(rep.max_disk_bytes * 2)
        )

        lines = [
            "📈 **Consumo Real por Video (Bot + FFmpeg)**",
            f"`{DIVIDER}`",
            "",
            f"📊 **Picos Registrados ({rep.total_measured} videos)**",
            f"├ **RAM:** máx `{human_bytes(rep.max_ram_bytes)}` · prom `{human_bytes(rep.avg_ram_bytes)}`",
            f"├ **CPU:** máx `{rep.max_cpu_pct:.0f}%` · prom `{rep.avg_cpu_pct:.0f}%` *(100% = 1 vCPU)*",
            f"└ **Disco:** máx `{human_bytes(rep.max_disk_bytes)}` · prom `{human_bytes(rep.avg_disk_bytes)}`",
            "",
            "🖥 **Especificaciones Sugeridas (panel.plugged.host)**",
            f"├ **1 video a la vez:** `{ram_1:.1f} GiB RAM` · `{cpu_1}% CPU` · `{disk_1:.0f} GiB Disco`",
            f"└ **2 videos a la vez:** `{ram_2:.1f} GiB RAM` · `{max(200, cpu_1)}% CPU` · `{disk_2:.0f} GiB Disco`",
            "",
            "🎬 **Últimos videos medidos**",
        ]

        for idx, rec in enumerate(rep.recent):
            branch = "└" if idx == len(rep.recent) - 1 else "├"
            short_name = rec.filename[:28] + ("…" if len(rec.filename) > 28 else "")
            lines.append(
                f"{branch} `{short_name}` ({rec.resolution} · {rec.codec})\n"
                f"   `{human_bytes(rec.input_bytes)}`→`{human_bytes(rec.output_bytes)}` · "
                f"RAM `{human_bytes(rec.peak_ram_bytes)}` · CPU `{rec.peak_cpu_pct:.0f}%` · Disco `{human_bytes(rec.peak_disk_bytes)}`"
            )

        lines.append("")
        lines.append("💡 *Usa `/usage reset` para reiniciar las mediciones.*")
        return "\n".join(lines)

    @app.on_message(filters.private & filters.command("usage"))
    async def cmd_usage(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            await message.reply_text("⛔ Comando exclusivo del administrador.")
            return
        parts = message.text.split()
        arg = parts[1].lower() if len(parts) > 1 else ""
        if arg in {"reset", "clear", "limpiar"}:
            deleted = await db.clear_job_metrics()
            await message.reply_text(
                f"🧹 Historial de consumo reiniciado (`{deleted}` registros eliminados)."
            )
            return
        await message.reply_text(await _render_usage_text())

    # ── Visor y descarga de logs (/logs [n|errors|file]) ──────────────────────

    @app.on_message(filters.private & filters.command("logs"))
    async def cmd_logs(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            await message.reply_text("⛔ Comando exclusivo del administrador.")
            return

        parts = message.text.split()
        arg = parts[1].lower() if len(parts) > 1 else ""

        if arg in {"file", "archivo", "doc"}:
            if not log_file.exists():
                await message.reply_text("ℹ️ Todavía no se ha generado `data/bot.log`.")
                return
            await message.reply_document(
                document=str(log_file),
                caption=f"📄 `bot.log` (`{human_bytes(log_file.stat().st_size)}`)",
            )
            return

        if arg in {"errors", "error", "err"}:
            content = read_last_log_lines(log_file, lines=40, errors_only=True)
            await message.reply_text(_format_log_block("Últimos errores y avisos", content))
            return

        num_lines = 35
        if arg.isdigit():
            num_lines = max(5, min(120, int(arg)))

        content = read_last_log_lines(log_file, lines=num_lines, errors_only=False)
        await message.reply_text(_format_log_block(f"Últimas {num_lines} líneas (bot.log)", content))

    # ── Inicio de sesión de cuenta Telegram (/login, /logout, /setdump) ──────

    @app.on_message(filters.private & filters.command("login"))
    async def cmd_login(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            await message.reply_text("⛔ Solo el dueño puede vincular una cuenta de Telegram.")
            return
        await user_session.begin_login()
        await message.reply_text(
            "🔑 **Vincular Cuenta de Telegram**\n"
            f"`{DIVIDER}`\n"
            "├ **Estándar:** hasta 2 GB (igual que el bot)\n"
            "└ **Telegram Premium:** hasta 4 GB nativos\n\n"
            "📱 Envía tu **número con código de país** (ej: `+5215512345678`).\n"
            "*(Tu mensaje se borrará al instante. Usa `/cancellogin` para cancelar.)*"
        )

    @app.on_message(filters.private & filters.command("cancellogin"))
    async def cmd_cancellogin(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            return
        await user_session.cancel_login()
        await message.reply_text("✖ Inicio de sesión cancelado.")

    @app.on_message(filters.private & filters.command("logout"))
    async def cmd_logout(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            return
        await user_session.stop()
        await db.set_setting("user_session_string", "")
        await message.reply_text("🔌 Cuenta de Telegram desconectada y sesión eliminada.")

    @app.on_message(filters.private & filters.command("setdump"))
    async def cmd_setdump(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await message.reply_text(
                "Uso: `/setdump <channel_id>` (ej. `/setdump -1001234567890`).\n"
                "Usa `/setdump 0` para desactivarlo."
            )
            return
        chat_id = int(parts[1])
        await db.set_setting("dump_chat_id", str(chat_id))
        if chat_id == 0:
            await message.reply_text("✅ Canal puente desactivado.")
        else:
            await message.reply_text(f"✅ Canal puente configurado en `{chat_id}`.")

    # ── Lista blanca y mantenimiento ──────────────────────────────────────────

    @app.on_message(filters.private & filters.command("adduser"))
    async def cmd_adduser(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await message.reply_text("Uso: `/adduser <telegram_user_id>`")
            return
        target_id = int(parts[1])
        await db.add_user(target_id, added_by=message.from_user.id)
        await message.reply_text(f"✅ Usuario `{target_id}` autorizado en la lista blanca.")

    @app.on_message(filters.private & filters.command("deluser"))
    async def cmd_deluser(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await message.reply_text("Uso: `/deluser <telegram_user_id>`")
            return
        target_id = int(parts[1])
        if target_id == config.owner_id:
            await message.reply_text("⚠️ No puedes eliminar al dueño principal.")
            return
        removed = await db.remove_user(target_id)
        if removed:
            await message.reply_text(f"🗑 Usuario `{target_id}` eliminado de la lista blanca.")
        else:
            await message.reply_text(f"ℹ️ El usuario `{target_id}` no estaba en la lista blanca.")

    @app.on_message(filters.private & filters.command("users"))
    async def cmd_users(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            return
        users = await db.list_users()
        lines = [f"👥 **Lista Blanca ({len(users)})**", f"`{DIVIDER}`"]
        for idx, u in enumerate(users):
            branch = "└" if idx == len(users) - 1 else "├"
            role = " · Dueño" if u.user_id == config.owner_id else ""
            uname = f" @{u.username}" if u.username else ""
            lines.append(f"{branch} `{u.user_id}`{uname}{role}")
        await message.reply_text("\n".join(lines))

    @app.on_message(filters.private & filters.command("purge"))
    async def cmd_purge(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            return
        removed = await asyncio.to_thread(queue_mgr.purge_inactive_temp)
        await message.reply_text(f"🧹 **Limpieza completada:** `{removed}` temporales eliminados.")

    @app.on_message(filters.private & filters.command("cancelall"))
    async def cmd_cancelall(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            return
        count = await queue_mgr.cancel_all()
        await message.reply_text(f"🛑 Se cancelaron `{count}` trabajos activos/en cola.")

    @app.on_message(filters.private & filters.command("update"))
    async def cmd_update(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            return
        status = await message.reply_text("🔄 Buscando actualizaciones en GitHub...")
        ok, msg, changed = await pull_from_github(Path.cwd())
        if not ok:
            await status.edit_text(f"❌ {msg}")
            return
        if not changed:
            await status.edit_text(f"✅ {msg}")
            return
        await status.edit_text(f"✅ {msg}\n♻️ Reiniciando bot...")
        restart_process()

    # ── Callbacks inline de /admin ────────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^adm:"))
    async def cb_admin(_client: Client, query: CallbackQuery) -> None:
        if not _is_owner(query.from_user.id):
            await query.answer("Solo el administrador puede usar este panel.", show_alert=True)
            return

        action = query.data.split(":", 1)[1]

        if action == "toggle_access":
            cur = await db.get_setting("access_mode", config.default_access_mode)
            nxt = "public" if cur == "private" else "private"
            await db.set_setting("access_mode", nxt)
            await query.answer(f"Acceso: {nxt}")

        elif action == "toggle_conc":
            nxt = 2 if queue_mgr.concurrency_limit == 1 else 1
            await queue_mgr.set_concurrency(nxt)
            await query.answer(f"Concurrencia: {nxt} × {queue_mgr.threads_per_job()} hilos")

        elif action == "cycle_uploader":
            order = ["gofile", "buzzheavier"]
            if queue_mgr.direct_store.enabled:
                order.append("direct_http")
            cur = await db.get_setting("large_file_uploader", config.large_file_uploader)
            idx = (order.index(cur) + 1) % len(order) if cur in order else 0
            nxt = order[idx]
            await db.set_setting("large_file_uploader", nxt)
            await query.answer(f"Host >2 GB: {nxt}")

        elif action == "toggle_alerts":
            cur = await db.get_setting("error_alerts", "1")
            nxt = "0" if cur == "1" else "1"
            await db.set_setting("error_alerts", nxt)
            await query.answer(f"Auto-Log: {'ON' if nxt == '1' else 'OFF'}")

        elif action == "live":
            await query.answer()
            text, has_jobs = await _render_live_text()
            with contextlib.suppress(Exception):
                await query.message.edit_text(text, reply_markup=build_live_keyboard(has_jobs))
            return

        elif action == "usage":
            await query.answer("Cargando reporte de consumo...")
            await query.message.reply_text(await _render_usage_text())
            return

        elif action == "logs":
            await query.answer("Cargando logs...")
            content = read_last_log_lines(log_file, lines=35, errors_only=False)
            await query.message.reply_text(_format_log_block("Últimas 35 líneas (bot.log)", content))
            return

        elif action == "logfile":
            if not log_file.exists():
                await query.answer("Todavía no existe data/bot.log", show_alert=True)
                return
            await query.answer("Enviando bot.log...")
            await query.message.reply_document(
                document=str(log_file),
                caption=f"📄 `bot.log` (`{human_bytes(log_file.stat().st_size)}`)",
            )
            return

        elif action == "purge":
            removed = await asyncio.to_thread(queue_mgr.purge_inactive_temp)
            await query.answer(f"Limpiados {removed} temporales", show_alert=True)

        elif action == "cancelall":
            count = await queue_mgr.cancel_all()
            await query.answer(f"Cancelados {count} trabajos", show_alert=True)

        elif action == "users":
            users = await db.list_users()
            summary = ", ".join(str(u.user_id) for u in users[:15])
            await query.answer(f"Autorizados ({len(users)}): {summary}", show_alert=True)
            return

        elif action == "update":
            await query.answer("Buscando actualizaciones en GitHub...")
            status = await query.message.reply_text("🔄 Buscando actualizaciones en GitHub...")
            ok, msg, changed = await pull_from_github(Path.cwd())
            if not ok:
                await status.edit_text(f"❌ {msg}")
                return
            if not changed:
                await status.edit_text(f"✅ {msg}")
                return
            await status.edit_text(f"✅ {msg}\n♻️ Reiniciando bot...")
            restart_process()
            return

        elif action == "refresh":
            await query.answer("Métricas actualizadas")

        text, access_mode, conc, uploader, error_alerts = await _render_admin_text()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                text,
                reply_markup=build_admin_keyboard(
                    access_mode,
                    conc,
                    uploader,
                    error_alerts=error_alerts,
                    threads_per_job=queue_mgr.threads_per_job(),
                ),
            )
