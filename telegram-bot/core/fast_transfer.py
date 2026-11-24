"""Motor legal de transferencia paralela MTProto para Hydrogram (20–60+ MB/s).

Utiliza el protocolo oficial de Telegram (múltiples sesiones media is_media=True
con upload.GetFile de 1 MiB en paralelo para descargas y upload.SaveBigFilePart
de 512 KiB con pipelining en paralelo para subidas), igual que Telegram Desktop y TDLib.

- Escribe/lee bloques directo en disco con os.pwrite/os.pread (consumo mínimo de RAM).
- Pipelining multi-conexión (hasta 12 conexiones × 4 bloques en vuelo en subida) para
  saturar enlaces de alta latencia (ej. VPS Europa ↔ Telegram DC1 Miami).
- Re-encolado automático de bloques si una conexión cae (sin abortar toda la subida).
- Fallback transparente al método estándar de Hydrogram solo si el pool completo falla.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import mimetypes
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from hydrogram import Client, raw
from hydrogram.errors import AuthBytesInvalid, FileMigrate, FloodWait
from hydrogram.file_id import FileId
from hydrogram.session import Auth, Session
from hydrogram.types import Message

from core.ffmpeg_engine import JobCancelledError

logger = logging.getLogger("hydrocompress.fast_transfer")

DOWNLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MiB (máximo oficial de upload.GetFile)
UPLOAD_PART_SIZE = 512 * 1024  # 512 KiB (máximo oficial de upload.SaveBigFilePart)
MIN_FAST_TRANSFER_BYTES = 10 * 1024 * 1024  # Para < 10 MiB el método estándar ya es instantáneo

ProgressCb = Callable[[int, int], Awaitable[None]]

# Caché en memoria de auth_key autorizadas por (id(client), dc_id)
_DC_AUTH_CACHE: dict[tuple[int, int], bytes] = {}
_DC_AUTH_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


async def _session_invoke(session: Session, query):
    invoke_fn = getattr(session, "invoke", None) or getattr(session, "send")
    return await invoke_fn(query)


async def _get_dc_auth_key(client: Client, dc_id: int) -> tuple[bytes, bool]:
    """Obtiene y autoriza una auth_key oficial para el DC del archivo."""
    main_dc = await client.storage.dc_id()
    test_mode = await client.storage.test_mode()

    if dc_id == main_dc:
        return await client.storage.auth_key(), test_mode

    cache_key = (id(client), dc_id)
    if cache_key in _DC_AUTH_CACHE:
        return _DC_AUTH_CACHE[cache_key], test_mode

    lock = _DC_AUTH_LOCKS.setdefault(cache_key, asyncio.Lock())
    async with lock:
        if cache_key in _DC_AUTH_CACHE:
            return _DC_AUTH_CACHE[cache_key], test_mode

        existing = getattr(client, "media_sessions", {}).get(dc_id)
        if existing is not None and getattr(existing, "auth_key", None):
            _DC_AUTH_CACHE[cache_key] = existing.auth_key
            return existing.auth_key, test_mode

        auth_key = await Auth(client, dc_id, test_mode).create()
        bootstrap_session = Session(client, dc_id, auth_key, test_mode, is_media=True)
        await bootstrap_session.start()
        try:
            for _ in range(3):
                exported = await client.invoke(
                    raw.functions.auth.ExportAuthorization(dc_id=dc_id)
                )
                try:
                    await _session_invoke(
                        bootstrap_session,
                        raw.functions.auth.ImportAuthorization(
                            id=exported.id,
                            bytes=exported.bytes,
                        ),
                    )
                    break
                except AuthBytesInvalid:
                    continue
        finally:
            with contextlib.suppress(Exception):
                await bootstrap_session.stop()

        _DC_AUTH_CACHE[cache_key] = auth_key
        return auth_key, test_mode


async def _open_media_pool(
    client: Client,
    dc_id: int,
    pool_size: int,
) -> list[Session]:
    """Abre N conexiones MTProto media en paralelo hacia dc_id."""
    auth_key, test_mode = await _get_dc_auth_key(client, dc_id)
    sessions = [
        Session(client, dc_id, auth_key, test_mode, is_media=True)
        for _ in range(pool_size)
    ]

    async def _start_one(sess: Session) -> Session | None:
        try:
            await asyncio.wait_for(sess.start(), timeout=20.0)
            return sess
        except Exception as exc:
            logger.debug("No se pudo abrir sesión media paralela en DC%s: %s", dc_id, exc)
            with contextlib.suppress(Exception):
                await sess.stop()
            return None

    started = await asyncio.gather(*(_start_one(s) for s in sessions))
    active = [s for s in started if s is not None]
    if not active:
        raise RuntimeError(f"No se pudo abrir ninguna conexión media en DC{dc_id}.")
    return active


async def _close_media_pool(sessions: list[Session]) -> None:
    async def _stop_one(sess: Session) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(sess.stop(), timeout=5.0)

    if sessions:
        await asyncio.gather(*(_stop_one(s) for s in sessions))


def _extract_media_meta(message: Message) -> tuple[str, int] | None:
    media = message.video or message.document
    if not media or not getattr(media, "file_id", None):
        return None
    file_size = int(getattr(media, "file_size", 0) or 0)
    return str(media.file_id), file_size


async def fast_download_media(
    client: Client,
    message: Message,
    dest_path: Path,
    cancel_event: asyncio.Event,
    on_progress: ProgressCb | None = None,
    workers: int = 10,
) -> Path:
    """Descarga un video/documento de Telegram en paralelo (múltiples conexiones MTProto)."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    meta = _extract_media_meta(message)

    if not meta or meta[1] < MIN_FAST_TRANSFER_BYTES:
        downloaded = await client.download_media(
            message,
            file_name=str(dest_path),
            progress=on_progress,
        )
        if not downloaded:
            raise RuntimeError("No se pudo descargar el archivo desde Telegram.")
        return Path(downloaded)

    raw_file_id, total_size = meta

    try:
        decoded = FileId.decode(raw_file_id)
        if decoded is None:
            raise ValueError("No se pudo decodificar file_id.")

        dc_id = int(decoded.dc_id)
        location = raw.types.InputDocumentFileLocation(
            id=decoded.media_id,
            access_hash=decoded.access_hash,
            file_reference=decoded.file_reference,
            thumb_size="",
        )

        total_chunks = math.ceil(total_size / DOWNLOAD_CHUNK_SIZE)
        conn_count = max(2, min(workers, total_chunks, 12))
        in_flight_per_conn = 3

        t0 = time.monotonic()
        sessions = await _open_media_pool(client, dc_id, conn_count)
        logger.info(
            "Descarga paralela MTProto iniciada: %d conexiones × %d en vuelo en DC%d (%d bloques de 1 MiB)",
            len(sessions),
            in_flight_per_conn,
            dc_id,
            total_chunks,
        )

        chunk_queue: asyncio.Queue[tuple[int, int]] = asyncio.Queue()
        for idx in range(total_chunks):
            chunk_queue.put_nowait((idx * DOWNLOAD_CHUNK_SIZE, 0))

        downloaded_bytes = 0
        last_cb_time = 0.0
        progress_lock = asyncio.Lock()

        fd = os.open(str(dest_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        try:
            with contextlib.suppress(OSError):
                os.ftruncate(fd, total_size)

            async def _worker(sess: Session) -> None:
                nonlocal downloaded_bytes, last_cb_time
                while True:
                    if cancel_event.is_set():
                        raise JobCancelledError("Descarga cancelada por el usuario.")
                    try:
                        offset, retries = chunk_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return

                    try:
                        res = await asyncio.wait_for(
                            _session_invoke(
                                sess,
                                raw.functions.upload.GetFile(
                                    location=location,
                                    offset=offset,
                                    limit=DOWNLOAD_CHUNK_SIZE,
                                ),
                            ),
                            timeout=35.0,
                        )
                        data: bytes = res.bytes
                        if data:
                            await asyncio.to_thread(os.pwrite, fd, data, offset)
                            should_notify = False
                            async with progress_lock:
                                downloaded_bytes += len(data)
                                cur = downloaded_bytes
                                now = time.monotonic()
                                if cur >= total_size or (now - last_cb_time) >= 0.5:
                                    last_cb_time = now
                                    should_notify = True
                            if should_notify and on_progress:
                                await on_progress(min(cur, total_size), total_size)
                    except FloodWait as fw:
                        wait_sec = int(getattr(fw, "value", 5)) + 1
                        await asyncio.sleep(min(wait_sec, 30))
                        if retries < 6:
                            chunk_queue.put_nowait((offset, retries + 1))
                        else:
                            raise
                    except FileMigrate:
                        raise
                    except Exception as exc:
                        if retries < 6:
                            await asyncio.sleep(0.4 * (retries + 1))
                            chunk_queue.put_nowait((offset, retries + 1))
                        else:
                            raise exc
                    finally:
                        chunk_queue.task_done()

            tasks = [
                asyncio.create_task(_worker(sess))
                for sess in sessions
                for _ in range(in_flight_per_conn)
            ]

            try:
                await asyncio.gather(*tasks)
            except Exception:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                with contextlib.suppress(Exception):
                    await asyncio.gather(*tasks, return_exceptions=True)
                raise
        finally:
            os.close(fd)
            await _close_media_pool(sessions)

        if cancel_event.is_set():
            raise JobCancelledError("Descarga cancelada por el usuario.")

        if not dest_path.exists() or dest_path.stat().st_size == 0:
            raise RuntimeError("Archivo descargado vacío.")

        elapsed = max(0.1, time.monotonic() - t0)
        logger.info(
            "Descarga paralela completada: %.1f MiB en %.1fs (%.2f MiB/s)",
            total_size / (1024 * 1024),
            elapsed,
            (total_size / (1024 * 1024)) / elapsed,
        )
        return dest_path

    except JobCancelledError:
        dest_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        logger.warning(
            "Descarga paralela falló (%s); usando descarga estándar de Hydrogram como respaldo.",
            exc,
        )
        dest_path.unlink(missing_ok=True)
        downloaded = await client.download_media(
            message,
            file_name=str(dest_path),
            progress=on_progress,
        )
        if not downloaded:
            raise RuntimeError("No se pudo descargar el archivo desde Telegram.")
        return Path(downloaded)


async def _fast_upload_big_file(
    client: Client,
    file_path: Path,
    cancel_event: asyncio.Event,
    on_progress: ProgressCb | None = None,
    workers: int = 10,
) -> raw.types.InputFileBig:
    """Sube un archivo >10 MiB en paralelo mediante upload.SaveBigFilePart (512 KiB).

    Usa hasta 12 conexiones TCP media independientes con 4 partes en vuelo por conexión
    (ventana de hasta 24 MiB) para maximizar la velocidad incluso con alta latencia a DC1.
    """
    total_size = file_path.stat().st_size
    total_parts = math.ceil(total_size / UPLOAD_PART_SIZE)
    file_id = secrets.randbits(63)
    main_dc = await client.storage.dc_id()

    conn_count = max(2, min(workers, total_parts, 12))
    in_flight_per_conn = 4

    t0 = time.monotonic()
    sessions = await _open_media_pool(client, main_dc, conn_count)
    logger.info(
        "Subida paralela MTProto iniciada: %d conexiones × %d en vuelo en DC%d (%d partes de 512 KiB)",
        len(sessions),
        in_flight_per_conn,
        main_dc,
        total_parts,
    )

    # Cola de (part_idx, retries) para reintentos tolerantes a fallos de un socket individual
    part_queue: asyncio.Queue[tuple[int, int]] = asyncio.Queue()
    for part_idx in range(total_parts):
        part_queue.put_nowait((part_idx, 0))

    uploaded_bytes = 0
    last_cb_time = 0.0
    progress_lock = asyncio.Lock()
    fd = os.open(str(file_path), os.O_RDONLY)

    try:
        async def _worker(sess: Session) -> None:
            nonlocal uploaded_bytes, last_cb_time
            consecutive_errors = 0

            while True:
                if cancel_event.is_set():
                    raise JobCancelledError("Subida cancelada por el usuario.")
                try:
                    part_idx, retries = part_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                offset = part_idx * UPLOAD_PART_SIZE
                chunk = await asyncio.to_thread(os.pread, fd, UPLOAD_PART_SIZE, offset)
                if not chunk:
                    part_queue.task_done()
                    continue

                try:
                    ok = await asyncio.wait_for(
                        _session_invoke(
                            sess,
                            raw.functions.upload.SaveBigFilePart(
                                file_id=file_id,
                                file_part=part_idx,
                                file_total_parts=total_parts,
                                bytes=chunk,
                            ),
                        ),
                        timeout=40.0,
                    )
                    if not ok:
                        raise RuntimeError(f"SaveBigFilePart devolvió False en parte {part_idx}")

                    consecutive_errors = 0
                    should_notify = False
                    async with progress_lock:
                        uploaded_bytes += len(chunk)
                        cur = uploaded_bytes
                        now = time.monotonic()
                        if cur >= total_size or (now - last_cb_time) >= 0.5:
                            last_cb_time = now
                            should_notify = True
                    if should_notify and on_progress:
                        await on_progress(min(cur, total_size), total_size)
                except FloodWait as fw:
                    wait_sec = int(getattr(fw, "value", 5)) + 1
                    logger.warning("FloodWait de %ss en subida paralela (parte %d)", wait_sec, part_idx)
                    await asyncio.sleep(min(wait_sec, 30))
                    if retries < 6:
                        part_queue.put_nowait((part_idx, retries + 1))
                    else:
                        raise
                except Exception as exc:
                    consecutive_errors += 1
                    if retries < 6:
                        await asyncio.sleep(0.4 * (retries + 1))
                        part_queue.put_nowait((part_idx, retries + 1))
                        # Si esta conexión TCP específica está atascada, retirarla y dejar que las otras terminen
                        if consecutive_errors >= 3 and len(sessions) > 2:
                            logger.warning(
                                "Una conexión de subida falló 3 veces seguidas (%s); retirando ese worker.",
                                exc,
                            )
                            return
                    else:
                        raise exc
                finally:
                    with contextlib.suppress(ValueError):
                        part_queue.task_done()

        tasks = [
            asyncio.create_task(_worker(sess))
            for sess in sessions
            for _ in range(in_flight_per_conn)
        ]

        try:
            await asyncio.gather(*tasks)
        except Exception:
            for t in tasks:
                if not t.done():
                    t.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
            raise
    finally:
        os.close(fd)
        await _close_media_pool(sessions)

    if uploaded_bytes < total_size:
        raise RuntimeError(
            f"Subida paralela incompleta ({uploaded_bytes}/{total_size} bytes)."
        )

    elapsed = max(0.1, time.monotonic() - t0)
    logger.info(
        "Subida paralela completada: %.1f MiB en %.1fs (%.2f MiB/s)",
        total_size / (1024 * 1024),
        elapsed,
        (total_size / (1024 * 1024)) / elapsed,
    )
    return raw.types.InputFileBig(
        id=file_id,
        parts=total_parts,
        name=file_path.name,
    )


async def fast_send_video_or_doc(
    client: Client,
    chat_id: int | str,
    file_path: Path,
    duration: int,
    width: int,
    height: int,
    thumb_path: str | None,
    caption: str,
    send_as_doc: bool,
    cancel_event: asyncio.Event,
    on_progress: ProgressCb | None = None,
    workers: int = 10,
) -> Message:
    """Envía un video o documento usando subida paralela MTProto (>10 MiB) con respaldo estándar."""
    total_size = file_path.stat().st_size

    async def _send_standard() -> Message:
        if send_as_doc:
            return await client.send_document(
                chat_id=chat_id,
                document=str(file_path),
                file_name=file_path.name,
                thumb=thumb_path,
                caption=caption or "",
                progress=on_progress,
            )
        return await client.send_video(
            chat_id=chat_id,
            video=str(file_path),
            file_name=file_path.name,
            duration=duration,
            width=width,
            height=height,
            thumb=thumb_path,
            supports_streaming=True,
            caption=caption or "",
            progress=on_progress,
        )

    if total_size < MIN_FAST_TRANSFER_BYTES:
        return await _send_standard()

    try:
        input_file = await _fast_upload_big_file(
            client=client,
            file_path=file_path,
            cancel_event=cancel_event,
            on_progress=on_progress,
            workers=workers,
        )

        uploaded_thumb = None
        if thumb_path and Path(thumb_path).exists():
            with contextlib.suppress(Exception):
                uploaded_thumb = await client.save_file(thumb_path)

        peer = await client.resolve_peer(chat_id)
        if caption:
            parsed = await client.parser.parse(caption, None)
            msg_text = parsed.get("message", "")
            msg_entities = parsed.get("entities") or None
        else:
            msg_text = ""
            msg_entities = None

        mime, _ = mimetypes.guess_type(file_path.name)
        mime_type = mime or ("video/x-matroska" if file_path.suffix.lower() == ".mkv" else "video/mp4")

        attributes: list = [raw.types.DocumentAttributeFilename(file_name=file_path.name)]
        if not send_as_doc:
            attributes.insert(
                0,
                raw.types.DocumentAttributeVideo(
                    duration=max(1, duration),
                    w=max(1, width),
                    h=max(1, height),
                    supports_streaming=True,
                ),
            )

        media = raw.types.InputMediaUploadedDocument(
            file=input_file,
            mime_type=mime_type,
            attributes=attributes,
            thumb=uploaded_thumb,
            force_file=send_as_doc,
        )

        updates = await client.invoke(
            raw.functions.messages.SendMedia(
                peer=peer,
                media=media,
                message=msg_text,
                entities=msg_entities,
                random_id=client.rnd_id(),
            )
        )

        for upd in getattr(updates, "updates", []):
            raw_msg = getattr(upd, "message", None)
            if raw_msg is not None and getattr(raw_msg, "id", None):
                return raw_msg  # type: ignore[return-value]
        return updates  # type: ignore[return-value]

    except JobCancelledError:
        raise
    except Exception as exc:
        logger.exception(
            "Subida paralela falló (%s); usando subida estándar de Hydrogram como respaldo.",
            exc,
        )
        return await _send_standard()
