"""Punto de entrada principal del Bot de Compresión de Videos para Telegram.

Arquitectura 100% Telegram nativa (sin panel web), construida con Hydrogram +
TgCrypto + FFmpeg para panel.plugged.host (400% CPU, 10 GiB RAM, 30 GiB Disco).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import signal
import sys

from hydrogram import Client, idle

from config import BotConfig
from core.database import Database
from core.queue_manager import QueueManager
from core.uploader import DirectLinkStore
from handlers.admin_handlers import register_admin_handlers
from handlers.user_handlers import register_user_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("hydrocompress")


def _verify_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        logger.critical("FFmpeg o FFprobe no están instalados en el PATH del contenedor.")
        sys.exit(1)


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
    _verify_ffmpeg()
    _check_tgcrypto()

    config = BotConfig.from_env()
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

    app = Client(
        name=str(config.data_dir / "hydrocompress"),
        api_id=config.api_id,
        api_hash=config.api_hash,
        bot_token=config.bot_token,
        workers=16,
        max_concurrent_transmissions=4,
    )

    queue_mgr = QueueManager(
        client=app,
        config=config,
        db=db,
        direct_store=direct_store,
    )

    register_admin_handlers(app, config, db, queue_mgr)
    register_user_handlers(app, config, db, queue_mgr)

    await direct_store.start()
    await app.start()
    await queue_mgr.start()

    me = await app.get_me()
    logger.info(
        "Bot iniciado como @%s (ID: %s) | Presupuesto: %s%% CPU · %.0f GiB RAM · %.0f GiB Disco",
        me.username,
        me.id,
        config.max_cpu_threads * 100,
        config.max_ram_gb,
        config.max_disk_gb,
    )

    try:
        await idle()
    finally:
        logger.info("Deteniendo bot y limpiando recursos...")
        await queue_mgr.stop()
        await app.stop()
        await direct_store.stop()
        await db.close()
        logger.info("Apagado limpio completado.")


if __name__ == "__main__":
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
