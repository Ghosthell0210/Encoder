"""Punto de entrada principal del Bot de Compresión de Videos para Telegram.

Arquitectura 100% Telegram nativa (sin panel web), construida con Hydrogram +
TgCrypto + FFmpeg para panel.plugged.host (Egg Python Generic Pelican/Pterodactyl).
Incluye auto-instalación rootless de FFmpeg/FFprobe, registro automático de
comandos nativos de Telegram y soporte opcional de cuenta (/login) para 4 GB Premium.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import signal
import sys

# Compatibilidad Python 3.11–3.14 sin DeprecationWarning antes de importar hydrogram
_MAIN_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_MAIN_LOOP)

from hydrogram import Client, idle
from hydrogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from config import BotConfig
from core.console_commands import console_command_loop
from core.database import Database
from core.ffmpeg_bootstrap import ensure_ffmpeg
from core.logger_setup import setup_logging
from core.queue_manager import QueueManager
from core.uploader import DirectLinkStore
from core.user_session import UserSessionManager
from handlers.admin_handlers import register_admin_handlers
from handlers.user_handlers import register_user_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("hydrocompress")

USER_BOT_COMMANDS: list[BotCommand] = [
    BotCommand("start", "Inicio rápido y menú principal"),
    BotCommand("help", "Guía de uso y lista de comandos"),
    BotCommand("auto", "Activar o desactivar Modo Lotes automático"),
    BotCommand("queue", "Ver cola de compresión y trabajos activos"),
    BotCommand("stats", "Ver videos comprimidos y espacio ahorrado"),
    BotCommand("setthumb", "Fijar miniatura (responde a una foto)"),
    BotCommand("delthumb", "Borrar miniatura personalizada"),
    BotCommand("setwatermark", "Guardar marca de agua para tus videos"),
    BotCommand("delwatermark", "Borrar marca de agua guardada"),
    BotCommand("id", "Mostrar tu ID de Telegram"),
]

OWNER_BOT_COMMANDS: list[BotCommand] = [
    *USER_BOT_COMMANDS,
    BotCommand("admin", "Panel del servidor, recursos y alertas"),
    BotCommand("usage", "Consumo real de RAM, CPU y disco por video"),
    BotCommand("logs", "Ver últimos errores o descargar bot.log"),
    BotCommand("update", "Actualizar el bot desde GitHub y reiniciar"),
    BotCommand("users", "Ver usuarios autorizados (lista blanca)"),
    BotCommand("adduser", "Autorizar usuario por ID"),
    BotCommand("deluser", "Quitar usuario de la lista blanca"),
    BotCommand("purge", "Limpiar archivos y enlaces temporales"),
    BotCommand("cancelall", "Cancelar todos los trabajos activos/en cola"),
    BotCommand("login", "Vincular cuenta Telegram (Premium 4 GB)"),
    BotCommand("logout", "Desconectar cuenta Telegram vinculada"),
    BotCommand("setdump", "Configurar canal puente para 2–4 GB"),
]


async def _sync_bot_commands(app: Client, owner_id: int) -> None:
    """Registra el menú nativo '/' en Telegram separado por rol (usuarios vs dueño)."""
    try:
        await app.set_bot_commands(USER_BOT_COMMANDS, scope=BotCommandScopeDefault())
        logger.info("Menú de comandos de usuario registrado (%d comandos).", len(USER_BOT_COMMANDS))
    except Exception as exc:
        logger.warning("No se pudieron registrar los comandos por defecto: %s", exc)

    try:
        await app.set_bot_commands(
            OWNER_BOT_COMMANDS,
            scope=BotCommandScopeChat(chat_id=owner_id),
        )
        logger.info("Menú de comandos de dueño registrado para %s (%d comandos).", owner_id, len(OWNER_BOT_COMMANDS))
    except Exception as exc:
        logger.warning("No se pudo registrar el menú extendido del dueño (%s): %s", owner_id, exc)


def _check_tgcrypto() -> None:
    try:
        import tgcrypto  # noqa: F401

        logger.info("TgCrypto C-extension activa: transferencias MTProto a máxima velocidad.")
    except ImportError:
        logger.warning(
            "TgCrypto no está instalado. Las subidas/descargas de 2 GB serán más lentas. "
            "Instálalo con: pip install tgcrypto"
        )


def _clean_startup_temps(config: BotConfig) -> None:
    shutil.rmtree(config.work_dir, ignore_errors=True)
    config.work_dir.mkdir(parents=True, exist_ok=True)
    config.links_dir.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    config = BotConfig.from_env()
    log_file = setup_logging(config.data_dir / "bot.log")
    logger.info("Registro rotativo activo en: %s", log_file)

    # Instala ffmpeg y ffprobe estáticos en ./bin sin root si el Egg no los trae
    ensure_ffmpeg(config.bin_dir)
    _check_tgcrypto()
    _clean_startup_temps(config)

    db = Database(config.db_path)
    await db.init(
        owner_id=config.owner_id,
        default_access_mode=config.default_access_mode,
        default_concurrent_jobs=config.max_concurrent_jobs,
        default_max_threads=config.max_cpu_threads,
        default_uploader=config.large_file_uploader,
    )

    direct_store = DirectLinkStore(config)
    user_session = UserSessionManager(config)

    # Restaurar cuenta de Telegram vinculada (.env o SQLite vía consola 'login' / '/login')
    saved_session = config.user_session_string or await db.get_setting("user_session_string", "")
    if saved_session:
        try:
            await user_session.start_with_session(saved_session)
        except Exception as exc:
            logger.warning("No se pudo iniciar la sesión de cuenta Telegram guardada: %s", exc)

    app = Client(
        name=str(config.data_dir / "hydrocompress"),
        api_id=config.api_id,
        api_hash=config.api_hash,
        bot_token=config.bot_token,
        workers=16,
        max_concurrent_transmissions=16,
    )

    queue_mgr = QueueManager(
        client=app,
        config=config,
        db=db,
        direct_store=direct_store,
        user_session=user_session,
    )

    register_admin_handlers(app, config, db, queue_mgr, user_session)
    register_user_handlers(app, config, db, queue_mgr, user_session)

    await direct_store.start()
    await app.start()
    await _sync_bot_commands(app, config.owner_id)
    await queue_mgr.start()

    me = await app.get_me()
    logger.info(
        "Bot iniciado como @%s (ID: %s) | Presupuesto: %s%% CPU · %.0f GiB RAM · %.0f GiB Disco | Cuenta: %s",
        me.username,
        me.id,
        config.max_cpu_threads * 100,
        config.max_ram_gb,
        config.max_disk_gb,
        user_session.status_label(),
    )

    console_task = asyncio.create_task(
        console_command_loop(db, queue_mgr, user_session, log_file=log_file)
    )

    try:
        await idle()
    finally:
        logger.info("Deteniendo bot y limpiando recursos...")
        console_task.cancel()
        await queue_mgr.stop()
        await user_session.stop()
        await app.stop()
        await direct_store.stop()
        await db.close()
        logger.info("Apagado limpio completado.")


if __name__ == "__main__":
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        _MAIN_LOOP.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        pass
