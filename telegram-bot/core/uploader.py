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
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp
from aiohttp import web

from config import BotConfig
from core.ffmpeg_bootstrap import ensure_ytdlp
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
class UrlFormatOption:
    height: int
    width: int | None
    label: str
    fps: int | None
    est_bytes: int | None
    is_approx: bool


@dataclass(slots=True)
class UrlProbeInfo:
    url: str
    title: str
    filename: str
    duration: float | None
    is_direct: bool
    direct_bytes: int | None
    formats: list[UrlFormatOption]


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


_DIRECT_MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv", ".3gp"}
_YTDLP_PROGRESS_RE = re.compile(
    r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+~?\s*([\d.]+)\s*([KMG]i?B)",
    re.IGNORECASE,
)


def _size_to_bytes(val: float, unit: str) -> int:
    u = unit.lower()
    if u.startswith("k"):
        return int(val * 1024)
    if u.startswith("m"):
        return int(val * 1024 * 1024)
    if u.startswith("g"):
        return int(val * 1024 * 1024 * 1024)
    return int(val)


def _is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


async def _validate_external_url(url: str) -> str:
    """Valida esquema HTTP/HTTPS y bloquea destinos locales, privados o reservados."""
    clean = url.strip()
    parsed = urlparse(clean)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("El enlace debe comenzar por http:// o https:// con un dominio válido.")

    host = parsed.hostname.strip().lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise RuntimeError("No se permiten enlaces hacia direcciones locales o internas.")

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        if _is_forbidden_ip(literal_ip):
            raise RuntimeError("No se permiten direcciones IP privadas o reservadas.")
        return clean

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise RuntimeError(f"No se pudo resolver el dominio del enlace ({host}).") from exc

    for _fam, _kind, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0].split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            if _is_forbidden_ip(ip_obj):
                raise RuntimeError("El dominio resuelve a una red privada no permitida.")
        except ValueError:
            continue
    return clean


async def _probe_direct_head(url: str) -> tuple[bool, int | None, str]:
    """Detecta si el enlace apunta directo a un video HTTP y extrae Content-Length/nombre."""
    path = unquote(urlparse(url).path)
    path_name = Path(path).name or "video_enlace.mp4"
    path_ext = Path(path).suffix.lower()

    timeout = aiohttp.ClientTimeout(total=12, sock_connect=10, sock_read=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.head(url, allow_redirects=True) as resp:
                await _validate_external_url(str(resp.url))
                if resp.status >= 400:
                    return (path_ext in _DIRECT_MEDIA_EXTS), None, path_name
                ctype = (resp.headers.get("Content-Type") or "").lower()
                cd = (resp.headers.get("Content-Disposition") or "").lower()
                cl_raw = resp.headers.get("Content-Length")
                cl_val = int(cl_raw) if cl_raw and cl_raw.isdigit() and int(cl_raw) > 0 else None
                fname = _extract_filename_from_response(str(resp.url), resp)
                is_direct = (
                    path_ext in _DIRECT_MEDIA_EXTS
                    or ctype.startswith("video/")
                    or ctype.startswith("audio/")
                    or ("attachment" in cd and any(ext in cd for ext in _DIRECT_MEDIA_EXTS))
                    or ("octet-stream" in ctype and path_ext in _DIRECT_MEDIA_EXTS)
                )
                return is_direct, cl_val, fname
    except RuntimeError:
        raise
    except Exception:
        return (path_ext in _DIRECT_MEDIA_EXTS), None, path_name


async def _probe_direct_media(url: str) -> bool:
    """Detecta rápidamente si el enlace apunta directo a un archivo de video descargable por HTTP."""
    is_direct, _, _ = await _probe_direct_head(url)
    return is_direct


def _estimate_stream_bytes(fmt: dict, duration: float | None) -> tuple[int | None, bool]:
    exact = fmt.get("filesize")
    if isinstance(exact, (int, float)) and exact > 0:
        return int(exact), False
    approx = fmt.get("filesize_approx")
    if isinstance(approx, (int, float)) and approx > 0:
        return int(approx), True
    protocol = str(fmt.get("protocol") or "").lower()
    is_hls = ("m3u8" in protocol) or ("hls" in protocol)
    # En HLS (m3u8), 'tbr' suele ser el BANDWIDTH pico del manifiesto (hasta 5-10x mayor al peso real).
    if is_hls:
        rate = fmt.get("vbr") or fmt.get("abr")
    else:
        rate = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
    if duration and duration > 0 and isinstance(rate, (int, float)) and rate > 0:
        return int((float(rate) * 1000.0 / 8.0) * duration), True
    return None, True


def _format_res_label(width: int | None, height: int) -> str:
    if width and width > 0 and width < height:
        if width in {2160, 1440, 1080, 720, 480, 360, 240, 144}:
            return f"{width}p ↕"
        return f"{width}×{height}"
    return f"{height}p"


async def probe_url_info(url: str) -> UrlProbeInfo:
    """Inspecciona un enlace antes de descargarlo para conocer resoluciones y pesos disponibles."""
    safe_url = await _validate_external_url(url)
    is_direct, direct_bytes, direct_name = await _probe_direct_head(safe_url)
    if is_direct:
        clean_name = "".join(
            c if c.isalnum() or c in " -_." else "_" for c in direct_name
        ).strip(" .") or "video_enlace.mp4"
        if "." not in clean_name:
            clean_name += ".mp4"
        return UrlProbeInfo(
            url=safe_url,
            title=Path(clean_name).stem,
            filename=clean_name[:100],
            duration=None,
            is_direct=True,
            direct_bytes=direct_bytes,
            formats=[],
        )

    base_cmd = await asyncio.to_thread(ensure_ytdlp, Path("bin"))
    if base_cmd is None:
        raise RuntimeError(
            "No se pudo instalar automáticamente 'yt-dlp'. "
            "Verifica la salida a internet del servidor o ejecuta 'pip install yt-dlp'."
        )

    cmd = [
        *base_cmd,
        "--dump-single-json",
        "--no-playlist",
        "--no-color",
        "--skip-download",
        "--",
        safe_url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=25.0)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        logger.warning("yt-dlp probe timeout (>25s) para URL: %s", safe_url)
        raise RuntimeError("Tiempo agotado al inspeccionar el enlace.")

    stderr_txt = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        err_msg = stderr_txt[:280]
        logger.error("yt-dlp probe falló (rc=%s) para %s: %s", proc.returncode, safe_url, stderr_txt[:500])
        raise RuntimeError(f"No se pudo analizar el enlace: {err_msg or f'código {proc.returncode}'}")

    if stderr_txt:
        logger.warning("yt-dlp probe avisos para %s: %s", safe_url, stderr_txt[:500])

    raw_json = stdout.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        logger.error("yt-dlp devolvió JSON inválido para %s: %s (inicio=%r)", safe_url, exc, raw_json[:200])
        raise RuntimeError("La respuesta del analizador de enlaces no fue válida.") from exc

    if isinstance(data.get("entries"), list) and data["entries"]:
        first = data["entries"][0]
        if isinstance(first, dict):
            data = first

    title = str(data.get("title") or "video_enlace").strip()
    safe_stem = "".join(c if c.isalnum() or c in " -_." else "_" for c in title).strip(" .")
    filename = f"{(safe_stem or 'video_enlace')[:84]}.mp4"
    dur_raw = data.get("duration")
    duration = float(dur_raw) if isinstance(dur_raw, (int, float)) and dur_raw > 0 else None

    raw_formats = data.get("formats") or []
    best_audio_score: tuple[int, int, int, float] = (-1, -1, -1, 0.0)
    best_audio_bytes = 0
    best_audio_approx = True
    for f in raw_formats:
        if not isinstance(f, dict):
            continue
        vcodec = str(f.get("vcodec") or "none").lower()
        acodec = str(f.get("acodec") or "none").lower()
        if vcodec == "none" and acodec != "none":
            a_bytes, a_approx = _estimate_stream_bytes(f, duration)
            if not a_bytes:
                continue
            a_proto = str(f.get("protocol") or "").lower()
            a_hls = ("m3u8" in a_proto) or ("hls" in a_proto)
            a_ext = str(f.get("ext") or "").lower()
            a_abr = float(f.get("abr") or f.get("tbr") or 0.0)
            a_score = (
                1 if not a_hls else 0,
                1 if a_ext in {"m4a", "mp4"} else 0,
                1 if not a_approx else 0,
                a_abr,
            )
            if a_score > best_audio_score:
                best_audio_score = a_score
                best_audio_bytes = a_bytes
                best_audio_approx = a_approx

    grouped: dict[int, tuple[tuple[int, int, int, int, int, float], UrlFormatOption]] = {}
    for f in raw_formats:
        if not isinstance(f, dict):
            continue
        vcodec = str(f.get("vcodec") or "none").lower()
        if vcodec == "none":
            continue
        h_raw = f.get("height")
        if not isinstance(h_raw, (int, float)) or int(h_raw) <= 0:
            continue
        h_val = int(h_raw)
        w_raw = f.get("width")
        w_val = int(w_raw) if isinstance(w_raw, (int, float)) and int(w_raw) > 0 else None
        fps_raw = f.get("fps")
        fps_val = int(round(float(fps_raw))) if isinstance(fps_raw, (int, float)) and fps_raw > 0 else None

        v_bytes, v_approx = _estimate_stream_bytes(f, duration)
        acodec = str(f.get("acodec") or "none").lower()
        has_audio = acodec != "none"
        total_bytes = v_bytes
        is_approx = v_approx
        if total_bytes is not None and not has_audio and best_audio_bytes > 0:
            total_bytes += best_audio_bytes
            is_approx = is_approx or best_audio_approx

        proto = str(f.get("protocol") or "").lower()
        is_hls = ("m3u8" in proto) or ("hls" in proto)
        ext = str(f.get("ext") or "").lower()
        tbr = float(f.get("tbr") or f.get("vbr") or 0.0)
        score = (
            1 if not is_hls else 0,
            1 if (total_bytes is not None and not is_approx) else 0,
            1 if ext == "mp4" else 0,
            1 if total_bytes is not None else 0,
            fps_val or 0,
            tbr,
        )
        opt = UrlFormatOption(
            height=h_val,
            width=w_val,
            label=_format_res_label(w_val, h_val),
            fps=fps_val,
            est_bytes=total_bytes,
            is_approx=is_approx,
        )
        prev = grouped.get(h_val)
        if prev is None or score > prev[0]:
            grouped[h_val] = (score, opt)

    formats_sorted = [
        item[1] for _, item in sorted(grouped.items(), key=lambda kv: kv[0], reverse=True)
    ][:7]

    top_bytes, _ = _estimate_stream_bytes(data, duration)
    if top_bytes is None and formats_sorted:
        top_bytes = formats_sorted[0].est_bytes

    fmt_dbg = ", ".join(
        f"{o.label}:{(o.est_bytes // (1024 * 1024)) if o.est_bytes else '?'}MiB({'~' if o.is_approx else '='})"
        for o in formats_sorted[:5]
    )
    logger.info(
        "URL probe OK (%s) | dur=%ss | formatos=%d/%d | %s",
        safe_url[:80],
        int(duration) if duration else "?",
        len(formats_sorted),
        len(raw_formats),
        fmt_dbg or "sin formatos",
    )

    return UrlProbeInfo(
        url=safe_url,
        title=title,
        filename=filename,
        duration=duration,
        is_direct=False,
        direct_bytes=top_bytes,
        formats=formats_sorted,
    )


async def _download_http_direct(
    url: str,
    dest_dir: Path,
    max_allowed_bytes: int,
    cancel_event: asyncio.Event,
    on_progress: TransferProgressCb | None = None,
) -> Path:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
    dest_root = dest_dir.resolve()

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as resp:
            await _validate_external_url(str(resp.url))
            if resp.status >= 400:
                raise RuntimeError(f"El enlace respondió con error HTTP {resp.status}.")

            total = int(resp.headers.get("Content-Length") or 0)
            if total > 0 and total > max_allowed_bytes:
                raise RuntimeError(
                    f"El archivo remoto ({total / (1024**3):.2f} GiB) excede el margen de disco seguro."
                )

            filename = _extract_filename_from_response(str(resp.url), resp)
            dest_path = (dest_root / filename).resolve()
            if not str(dest_path).startswith(str(dest_root) + os.sep):
                raise RuntimeError("Nombre de archivo remoto inválido.")

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


async def _download_with_ytdlp(
    url: str,
    dest_dir: Path,
    max_allowed_bytes: int,
    cancel_event: asyncio.Event,
    on_progress: TransferProgressCb | None = None,
    max_height: int | None = None,
) -> Path:
    base_cmd = await asyncio.to_thread(ensure_ytdlp, Path("bin"))
    if base_cmd is None:
        raise RuntimeError(
            "No se pudo instalar automáticamente 'yt-dlp'. "
            "Verifica la salida a internet del servidor o ejecuta 'pip install yt-dlp'."
        )

    if max_height and max_height > 0:
        fmt_expr = (
            f"bv*[height<={max_height}][ext=mp4][protocol^=http]+ba[ext=m4a][protocol^=http]/"
            f"bv*[height<={max_height}][protocol^=http]+ba[protocol^=http]/"
            f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={max_height}]+ba/"
            f"b[height<={max_height}][ext=mp4]/b[height<={max_height}]/b"
        )
    else:
        fmt_expr = (
            "bv*[ext=mp4][protocol^=http]+ba[ext=m4a][protocol^=http]/"
            "bv*[protocol^=http]+ba[protocol^=http]/"
            "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b[ext=mp4]/b"
        )

    dest_root = dest_dir.resolve()
    out_tmpl = str(dest_root / "%(title).70B [%(id).20B].%(ext)s")
    cmd = [
        *base_cmd,
        "--no-playlist",
        "--newline",
        "--no-color",
        "--no-warnings",
        "--restrict-filenames",
        "--max-filesize",
        str(max_allowed_bytes),
        "--format",
        fmt_expr,
        "--merge-output-format",
        "mp4",
        "-o",
        out_tmpl,
        "--",
        url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    output_tail: list[str] = []
    last_cb = 0.0
    try:
        assert proc.stdout is not None
        while True:
            if cancel_event.is_set():
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                raise JobCancelledError("Descarga cancelada por el usuario.")

            try:
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    break
                continue

            if not raw_line:
                break

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            output_tail.append(line)
            if len(output_tail) > 20:
                output_tail.pop(0)

            m = _YTDLP_PROGRESS_RE.search(line)
            if m and on_progress is not None:
                now = time.monotonic()
                pct = float(m.group(1))
                total_bytes = _size_to_bytes(float(m.group(2)), m.group(3))
                cur_bytes = int(total_bytes * (pct / 100.0))
                if cur_bytes > max_allowed_bytes:
                    with contextlib.suppress(ProcessLookupError):
                        proc.terminate()
                    raise RuntimeError("El video del enlace excede el espacio seguro disponible.")
                if pct >= 99.5 or (now - last_cb) >= 0.8:
                    last_cb = now
                    await on_progress(cur_bytes, total_bytes)

        await proc.wait()
        if cancel_event.is_set():
            raise JobCancelledError("Descarga cancelada por el usuario.")
        if proc.returncode != 0:
            err_msg = " ".join(output_tail[-4:])[:360] or f"código {proc.returncode}"
            logger.error("yt-dlp descarga falló (rc=%s) para %s: %s", proc.returncode, url, "\n".join(output_tail[-8:]))
            raise RuntimeError(f"yt-dlp no pudo descargar el enlace: {err_msg}")
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

    candidates = [
        p
        for p in dest_root.iterdir()
        if p.is_file()
        and not p.name.endswith((".part", ".ytdl", ".temp"))
        and p.stat().st_size > 0
    ]
    if not candidates:
        raise RuntimeError("yt-dlp terminó sin generar ningún archivo de video.")

    chosen = max(candidates, key=lambda p: p.stat().st_size).resolve()
    if not str(chosen).startswith(str(dest_root) + os.sep):
        raise RuntimeError("Ruta de descarga fuera del directorio permitido.")
    return chosen


async def download_from_url(
    url: str,
    dest_dir: Path,
    max_allowed_bytes: int,
    cancel_event: asyncio.Event,
    on_progress: TransferProgressCb | None = None,
    max_height: int | None = None,
) -> Path:
    """Descarga un video desde enlace HTTP directo o plataformas (YouTube, TikTok, etc. vía yt-dlp)."""
    safe_url = await _validate_external_url(url)
    dest_dir.mkdir(parents=True, exist_ok=True)

    is_direct = await _probe_direct_media(safe_url)
    if is_direct:
        return await _download_http_direct(
            url=safe_url,
            dest_dir=dest_dir,
            max_allowed_bytes=max_allowed_bytes,
            cancel_event=cancel_event,
            on_progress=on_progress,
        )

    return await _download_with_ytdlp(
        url=safe_url,
        dest_dir=dest_dir,
        max_allowed_bytes=max_allowed_bytes,
        cancel_event=cancel_event,
        on_progress=on_progress,
        max_height=max_height,
    )


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
