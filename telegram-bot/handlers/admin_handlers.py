"""Panel de administración /admin, /login de cuenta Telegram y lista blanca."""

from __future__ import annotations

import contextlib
from pathlib import Path
import shutil

import psutil
from hydrogram import Client, filters
from hydrogram.types import CallbackQuery, Message

from config import BotConfig
from core.database import Database
from core.git_updater import pull_from_github, restart_process
from core.queue_manager import QueueManager, dir_size_bytes
from core.ui_helpers import build_admin_keyboard, human_bytes
from core.user_session import UserSessionManager


def register_admin_handlers(
    app: Client,
    config: BotConfig,
    db: Database,
    queue_mgr: QueueManager,
    user_session: UserSessionManager,
) -> None:
    def _is_owner(user_id: int | None) -> bool:
        return user_id == config.owner_id

    async def _render_admin_text() -> tuple[str, str, int, str]:
        settings = await db.get_all_settings()
        access_mode = settings.get("access_mode", config.default_access_mode)
        conc = queue_mgr.concurrency_limit
        uploader = settings.get("large_file_uploader", config.large_file_uploader)
        dump_id = settings.get("dump_chat_id", str(config.dump_chat_id or ""))
        stats = await db.get_stats()
        users = await db.list_users()
        active, pending = queue_mgr.get_queue_snapshot()

        cpu_pct = psutil.cpu_percent(interval=0.1)
        proc = psutil.Process()
        proc_ram = proc.memory_info().rss
        vm = psutil.virtual_memory()
        disk = shutil.disk_usage(config.work_dir)
        bot_temp_used = dir_size_bytes(config.work_dir) + dir_size_bytes(config.links_dir)

        ram_budget_bytes = int(config.max_ram_gb * (1024**3))
        disk_budget_bytes = int(config.max_disk_gb * (1024**3))
        threads_each = queue_mgr.threads_per_job()
        dump_label = dump_id if dump_id and dump_id != "0" else "Mensajes Guardados (dueño)"

        text = (
            "🛡 **Panel de Administración (panel.plugged.host)**\n\n"
            "🖥 **Recursos del Bot (Máxima Potencia):**\n"
            f"• **CPU:** `{cpu_pct:.1f}%` (Asignado: `{config.max_cpu_threads * 100}%` · `{threads_each}` hilos/trabajo)\n"
            f"• **RAM Bot / Sistema:** `{human_bytes(proc_ram)}` / `{human_bytes(vm.used)}` (Límite: `{human_bytes(ram_budget_bytes)}`)\n"
            f"• **Disco Temporal Bot:** `{human_bytes(bot_temp_used)}` / `{human_bytes(disk_budget_bytes)}`\n"
            f"• **Disco Libre del Host:** `{human_bytes(disk.free)}`\n\n"
            "⚙️ **Operación y Cuenta Telegram:**\n"
            f"• **Cuenta Telegram:** `{user_session.status_label()}`\n"
            f"• **Canal puente (2–4 GB):** `{dump_label}`\n"
            f"• **Cola:** `{len(active)}/{conc}` activos · `{len(pending)}` en espera\n"
            f"• **Acceso:** `{'Solo Lista Blanca' if access_mode == 'private' else 'Público'}` (`{len(users)}` usuarios)\n"
            f"• **Proveedor >2 GB:** `{uploader}`\n\n"
            "📊 **Acumulado:**\n"
            f"• **Videos:** `{stats.total_jobs}` · **Enlaces >2 GB:** `{stats.total_link_deliveries}`\n"
            f"• **Ahorro total:** `{human_bytes(stats.total_saved_bytes)}`\n\n"
            "**Comandos de dueño:**\n"
            "`/login` · `/logout` · `/setdump <id>` · `/update`\n"
            "`/adduser <id>` · `/deluser <id>` · `/users` · `/purge` · `/cancelall`"
        )
        return text, access_mode, conc, uploader

    @app.on_message(filters.private & filters.command("admin"))
    async def cmd_admin(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            await message.reply_text("⛔ Comando exclusivo del administrador.")
            return
        text, access_mode, conc, uploader = await _render_admin_text()
        await message.reply_text(
            text,
            reply_markup=build_admin_keyboard(access_mode, conc, uploader),
        )

    # ── Inicio de sesión de cuenta Telegram (/login, /logout, /setdump) ──────

    @app.on_message(filters.private & filters.command("login"))
    async def cmd_login(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            await message.reply_text("⛔ Solo el dueño puede vincular una cuenta de Telegram.")
            return
        await user_session.begin_login()
        await message.reply_text(
            "🔑 **Vincular cuenta de Telegram (MTProto)**\n\n"
            "Usa tu `API_ID` y `API_HASH` configurados para iniciar sesión con tu cuenta:\n"
            "• **Cuenta estándar:** límite nativo de **2 GB** (igual que el bot).\n"
            "• **Cuenta Telegram Premium:** sube el límite nativo hasta **4 GB**.\n\n"
            "📱 Envíame ahora tu **número de teléfono con código de país** (ejemplo: `+5215512345678`).\n"
            "*(Tu mensaje será eliminado al instante. Escribe `/cancellogin` para cancelar.)*"
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
        await message.reply_text("🔌 Cuenta de Telegram desconectada y sesión eliminada de la base de datos.")

    @app.on_message(filters.private & filters.command("setdump"))
    async def cmd_setdump(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await message.reply_text(
                "Uso: `/setdump <channel_id>` (ej. `/setdump -1001234567890`).\n"
                "Usa `/setdump 0` para desactivarlo. Añade tanto a tu cuenta como al bot como administradores en ese canal privado para entregar videos de 2–4 GB (Premium) a cualquier usuario."
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
        await message.reply_text(f"✅ Usuario `{target_id}` añadido a la lista blanca.")

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
        lines = [f"👥 **Lista Blanca ({len(users)} usuarios):**", ""]
        for u in users:
            role = " (Dueño)" if u.user_id == config.owner_id else ""
            uname = f" @{u.username}" if u.username else ""
            lines.append(f"• `{u.user_id}`{uname}{role}")
        await message.reply_text("\n".join(lines))

    @app.on_message(filters.private & filters.command("purge"))
    async def cmd_purge(_client: Client, message: Message) -> None:
        if not message.from_user or not _is_owner(message.from_user.id):
            return
        removed = queue_mgr.purge_inactive_temp()
        await message.reply_text(f"🧹 Limpieza completada: `{removed}` carpetas/enlaces temporales eliminados.")

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
            await query.answer(f"Modo cambiado a: {nxt}")

        elif action == "toggle_conc":
            nxt = 2 if queue_mgr.concurrency_limit == 1 else 1
            await queue_mgr.set_concurrency(nxt)
            await query.answer(f"Concurrencia: {nxt} trabajo(s) × {queue_mgr.threads_per_job()} hilos")

        elif action == "cycle_uploader":
            order = ["gofile", "buzzheavier"]
            if queue_mgr.direct_store.enabled:
                order.append("direct_http")
            cur = await db.get_setting("large_file_uploader", config.large_file_uploader)
            idx = (order.index(cur) + 1) % len(order) if cur in order else 0
            nxt = order[idx]
            await db.set_setting("large_file_uploader", nxt)
            await query.answer(f"Proveedor >2 GB: {nxt}")

        elif action == "purge":
            removed = queue_mgr.purge_inactive_temp()
            await query.answer(f"Limpiados {removed} temporales", show_alert=True)

        elif action == "cancelall":
            count = await queue_mgr.cancel_all()
            await query.answer(f"Cancelados {count} trabajos", show_alert=True)

        elif action == "users":
            users = await db.list_users()
            summary = ", ".join(str(u.user_id) for u in users[:15])
            await query.answer(f"Autorizados ({len(users)}): {summary}", show_alert=True)
            return

        elif action == "refresh":
            await query.answer("Métricas actualizadas")

        text, access_mode, conc, uploader = await _render_admin_text()
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                text,
                reply_markup=build_admin_keyboard(access_mode, conc, uploader),
            )
