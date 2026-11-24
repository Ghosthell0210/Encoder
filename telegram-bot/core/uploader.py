"""Descarga por enlace directo HTTP(S) y entrega por enlace para videos > 2 GB.

Cuando un video comprimido supera el límite de 2 GB de Telegram:
  1. Sube por streaming en bloques de 1 MiB (consumo mínimo de RAM) a GoFile
     (gratuito, sin API key obligatoria y sin tope de 2 GB).
  2. Si GoFile falla temporalmente, hace fallback automático a Buzzheavier.
  3. Opcionalmente, si el admin define PUBLIC_DOWNLOAD_BASE_URL, puede servir
     el archivo mediante un token temporal directo (/dl/<token>/<nombre>) con
     borrado automático al expirar, sin ninguna web ni interfaz gráfica.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import re
import secrets
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp
from aiohttp import web

from config import BotConfig
from core.ffmpeg_engine import JobCancelledError

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming chunks

TransferProgressCb = Callable[[int, int], Awaitable[None]]


@dataclass(slots=True)
class UploadLinkResult:
    url: str
    provider: str
    expires_note: str


@dataclass(slots=True)
class DirectLinkEntry:
    token: str
    file_path: Path
    filename: str
    expires_at: float


class DirectLinkStore:
    """Servidor mínimo solo para descargas directas por token (opcional, sin UI web)."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._entries: dict[str, DirectLinkEntry] = {}
        self._runner: web.AppRunner | None = None
        self._cleanup_task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.public_download_base_url)

    async def start(self) -> None:
        if not self.enabled:
            return
        app = web.Application()
        app.router.add_get("/dl/{token}/{filename}", self._handle_download)
        app.router.add_head("/dl/{token}/{filename}", self._handle_download)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner,
            host=self.config.direct_link_host,
            port=self.config.direct_link_port,
        )
        await site.start()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            "Ruta de enlaces directos temporales activa en %s:%s (sin web UI).",
            self.config.direct_link_host,
            self.config.direct_link_port,
        )

    async def stop(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    def register_file(self, source_path: Path) -> UploadLinkResult:
        if not self.enabled:
            raise RuntimeError("PUBLIC_DOWNLOAD_BASE_URL no está configurado.")
        token = secrets.token_urlsafe(24)
        self.config.links_dir.mkdir(parents=True, exist_ok=True)
        dest_dir = self.config.links_dir / token
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / source_path.name
        shutil.move(str(source_path), str(dest_path))

        ttl_hours = self.config.direct_link_ttl_hours
        self._entries[token] = DirectLinkEntry(
            token=token,
            file_path=dest_path,
            filename=dest_path.name,
            expires_at=time.time() + ttl_hours * 3600,
        )
        url = f"{self.config.public_download_base_url}/dl/{token}/{dest_path.name}"
        return UploadLinkResult(
            url=url,
            provider="Enlace Directo del Servidor",
            expires_note=f"Expira automáticamente en {ttl_hours}h para liberar disco.",
        )

    async def _handle_download(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info.get("token", "")
        entry = self._entries.get(token)
        if not entry or time.time() > entry.expires_at or not entry.file_path.exists():
            raise web.HTTPNotFound(text="Enlace expirado o inexistente.")

        mime, _ = mimetypes.guess_type(entry.filename)
        headers = {
            "Content-Disposition": f'attachment; filename="{entry.filename}"',
            "Content-Type": mime or "application/octet-stream",
        }
        return web.FileResponse(path=entry.file_path, headers=headers)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(600)
            now = time.time()
            expired = [k for k, v in self._entries.items() if now > v.expires_at]
            for token in expired:
                entry = self._entries.pop(token, None)
                if entry:
                    shutil.rmtree(entry.file_path.parent, ignore_errors=True)

    def purge_all(self) -> int:
        count = len(self._entries)
        self._entries.clear()
        shutil.rmtree(self.config.links_dir, ignore_errors=True)
        self.config.links_dir.mkdir(parents=True, exist_ok=True)
        return count


def _extract_filename_from_response(url: str, resp: aiohttp.ClientResponse) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', cd, flags=re.IGNORECASE)
    if match:
        raw = unquote(match.group(1).strip())
    else:
        path = unquote(urlparse(url).path)
        raw = Path(path).name or "video_url.mp4"

    clean = "".join(c if c.isalnum() or c in " -_." else "_" for c in raw).strip(" .")
    if "." not in clean:
        clean += ".mp4"
    return clean[:100]


async def download_from_url(
    url: str,
    dest_dir: Path,
    max_allowed_bytes: int,
    cancel_event: asyncio.Event,
    on_progress: TransferProgressCb | None = None,
) -> Path:
    """Descarga un video desde un enlace HTTP/HTTPS en streaming controlado."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"El enlace respondió con error HTTP {resp.status}.")

            total = int(resp.headers.get("Content-Length") or 0)
            if total > 0 and total > max_allowed_bytes:
                raise RuntimeError(
                    f"El archivo remoto ({total / (1024**3):.2f} GiB) excede el margen de disco seguro."
                )

            filename = _extract_filename_from_response(str(resp.url), resp)
            dest_path = dest_dir / filename
            downloaded = 0

            with dest_path.open("wb") as fp:
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    if cancel_event.is_set():
                        raise JobCancelledError("Descarga cancelada por el usuario.")
                    fp.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > max_allowed_bytes:
                        raise RuntimeError("La descarga excedió el límite de espacio libre seguro.")
                    if on_progress:
                        await on_progress(downloaded, total)

            if downloaded == 0:
                raise RuntimeError("El enlace devolvió un archivo vacío.")
            return dest_path


class _AsyncFileStreamer:
    """Iterador asíncrono por bloques para subir archivos >2 GB sin cargarlos en RAM."""

    def __init__(
        self,
        file_path: Path,
        cancel_event: asyncio.Event,
        on_progress: TransferProgressCb | None = None,
    ) -> None:
        self.file_path = file_path
        self.total_size = file_path.stat().st_size
        self.cancel_event = cancel_event
        self.on_progress = on_progress
        self.sent = 0

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        with self.file_path.open("rb") as fp:
            while True:
                if self.cancel_event.is_set():
                    raise JobCancelledError("Subida cancelada por el usuario.")
                chunk = await asyncio.to_thread(fp.read, CHUNK_SIZE)
                if not chunk:
                    break
                self.sent += len(chunk)
                if self.on_progress:
                    await self.on_progress(self.sent, self.total_size)
                yield chunk


async def _upload_to_gofile(
    file_path: Path,
    gofile_token: str,
    cancel_event: asyncio.Event,
    on_progress: TransferProgressCb | None,
) -> UploadLinkResult:
    """Sube un archivo grande en streaming multipart a GoFile (sin límite de 2 GB)."""
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
    headers: dict[str, str] = {}
    if gofile_token:
        headers["Authorization"] = f"Bearer {gofile_token}"

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get("https://api.gofile.io/servers") as s_resp:
            if s_resp.status != 200:
                raise RuntimeError(f"GoFile /servers respondió HTTP {s_resp.status}")
            s_data = await s_resp.json()
            servers = (s_data.get("data") or {}).get("servers") or []
            if not servers:
                raise RuntimeError("GoFile no devolvió servidores disponibles.")
            server_name = servers[0]["name"]

        upload_url = f"https://{server_name}.gofile.io/contents/uploadfile"
        streamer = _AsyncFileStreamer(file_path, cancel_event, on_progress)

        form = aiohttp.FormData()
        form.add_field(
            "file",
            streamer.iter_chunks(),
            filename=file_path.name,
            content_type="application/octet-stream",
        )

        async with session.post(upload_url, data=form) as u_resp:
            if u_resp.status >= 400:
                text = await u_resp.text()
                raise RuntimeError(f"GoFile rechazó la subida (HTTP {u_resp.status}): {text[:200]}")
            payload = await u_resp.json()
            if payload.get("status") != "ok":
                raise RuntimeError(f"GoFile devolvió error: {payload}")
            data = payload.get("data") or {}
            page_url = data.get("downloadPage")
            if not page_url:
                raise RuntimeError("GoFile no devolvió enlace de descarga.")
            return UploadLinkResult(
                url=str(page_url),
                provider="GoFile",
                expires_note="Enlace directo en la nube (sin límite de 2 GB).",
            )


async def _upload_to_buzzheavier(
    file_path: Path,
    cancel_event: asyncio.Event,
    on_progress: TransferProgressCb | None,
) -> UploadLinkResult:
    """Respaldo streaming PUT a Buzzheavier (rápido y sin límite de 2 GB)."""
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
    streamer = _AsyncFileStreamer(file_path, cancel_event, on_progress)
    safe_name = file_path.name.replace(" ", "_")
    upload_url = f"https://w.buzzheavier.com/{safe_name}"

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.put(
            upload_url,
            data=streamer.iter_chunks(),
            headers={"Content-Length": str(streamer.total_size)},
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"Buzzheavier falló (HTTP {resp.status}): {text[:200]}")
            payload = await resp.json(content_type=None)
            file_id = (payload.get("data") or {}).get("id") or payload.get("id")
            if not file_id:
                raise RuntimeError(f"Buzzheavier no devolvió ID: {payload}")
            return UploadLinkResult(
                url=f"https://buzzheavier.com/{file_id}",
                provider="Buzzheavier",
                expires_note="Enlace de descarga directa de respaldo (>2 GB).",
            )


async def upload_large_file(
    file_path: Path,
    preferred_provider: str,
    config: BotConfig,
    direct_store: DirectLinkStore,
    cancel_event: asyncio.Event,
    on_progress: TransferProgressCb | None = None,
) -> UploadLinkResult:
    """Sube archivos >2 GB probando el proveedor elegido y respaldos automáticos."""
    order: list[str] = []
    for candidate in (preferred_provider, "gofile", "buzzheavier", "direct_http"):
        if candidate not in order:
            order.append(candidate)

    errors: list[str] = []
    for provider in order:
        if cancel_event.is_set():
            raise JobCancelledError("Subida cancelada por el usuario.")

        try:
            if provider == "direct_http":
                if not direct_store.enabled:
                    continue
                return direct_store.register_file(file_path)
            if provider == "gofile":
                return await _upload_to_gofile(
                    file_path, config.gofile_token, cancel_event, on_progress
                )
            if provider == "buzzheavier":
                return await _upload_to_buzzheavier(
                    file_path, cancel_event, on_progress
                )
        except JobCancelledError:
            raise
        except Exception as exc:
            logger.warning("Fallo proveedor %s para archivo >2 GB: %s", provider, exc)
            errors.append(f"{provider}: {exc}")

    raise RuntimeError(
        "No se pudo generar el enlace de descarga para el video >2 GB.\n"
        + "\n".join(errors[:3])
    )
