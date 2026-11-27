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
from core.ffmpeg_bootstrap import ensure_curl_cffi, ensure_ytdlp, has_curl_cffi
from core.ffmpeg_engine import JobCancelledError
from core.host_extractors import (
    CHROME_UA,
    HostExtractionError,
    identify_file_host,
    resolve_host_download,
)

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


def _extract_filename_from_response(
    url: str,
    headers_map: Any,
    fallback_name: str | None = None,
) -> str:
    cd = headers_map.get("Content-Disposition", "") if headers_map else ""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', cd, flags=re.IGNORECASE)
    if match:
        raw = unquote(match.group(1).strip())
    elif fallback_name:
        raw = fallback_name
    else:
        path = unquote(urlparse(url).path)
        raw = Path(path).name or "archivo_url.bin"

    clean = "".join(c if c.isalnum() or c in " -_.()[]" else "_" for c in raw).strip(" .")
    if not clean:
        clean = "archivo_url.bin"
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
                fname = _extract_filename_from_response(str(resp.url), resp.headers)
                is_direct = (
                    path_ext in _DIRECT_MEDIA_EXTS
                    or ctype.startswith(("video/", "audio/", "application/x-matroska"))
                    or ("attachment" in cd and not ctype.startswith("text/"))
                    or (
                        not ctype.startswith("text/")
                        and ctype.startswith(
                            (
                                "application/octet-stream",
                                "application/zip",
                                "application/x-rar",
                                "application/x-7z-compressed",
                                "application/pdf",
                            )
                        )
                        and (cl_val is not None and cl_val > 65536 or "attachment" in cd or "." in Path(fname).name)
                    )
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
    if not duration or duration <= 0:
        return None, True

    protocol = str(fmt.get("protocol") or "").lower()
    is_hls = ("m3u8" in protocol) or ("hls" in protocol)
    vcodec = str(fmt.get("vcodec") or "none").lower()
    is_video = vcodec != "none"

    vbr = fmt.get("vbr")
    abr = fmt.get("abr")
    tbr = fmt.get("tbr")

    rate: float | None = None
    if is_hls:
        if is_video:
            if isinstance(vbr, (int, float)) and vbr > 0:
                rate = float(vbr) + (float(abr) if isinstance(abr, (int, float)) and abr > 0 else 0.0)
            elif isinstance(tbr, (int, float)) and tbr > 0:
                # En HLS sin vbr explícito (ej. Twitch VODs), tbr trae el BANDWIDTH del manifiesto.
                # Ignorar solo picos absurdos (>35 Mbps en <=1080p o >80 Mbps total) de manifiestos inflados.
                h_raw = fmt.get("height")
                h_num = int(h_raw) if isinstance(h_raw, (int, float)) else 0
                tbr_val = float(tbr)
                if tbr_val <= 80000 and not (0 < h_num <= 1080 and tbr_val > 35000):
                    rate = tbr_val
        else:
            raw_a = abr or tbr
            if isinstance(raw_a, (int, float)) and raw_a > 0:
                rate = float(raw_a)
    else:
        raw_r = tbr or vbr or abr
        if isinstance(raw_r, (int, float)) and raw_r > 0:
            rate = float(raw_r)

    if rate and rate > 0:
        return int((rate * 1000.0 / 8.0) * duration), True
    return None, True


async def _fill_missing_direct_format_sizes(
    items: list[tuple[UrlFormatOption, str | None]],
) -> None:
    """Consulta Content-Length vía HEAD rápido para formatos MP4/directos sin peso (ej. Twitch Clips)."""
    missing = [(opt, fmt_url) for opt, fmt_url in items if opt.est_bytes is None and fmt_url]
    if not missing:
        return

    timeout = aiohttp.ClientTimeout(total=3.0, sock_connect=2.0, sock_read=2.0)

    async def _head_one(session: aiohttp.ClientSession, opt: UrlFormatOption, fmt_url: str) -> None:
        with contextlib.suppress(Exception):
            await _validate_external_url(fmt_url)
            async with session.head(fmt_url, allow_redirects=True) as resp:
                if resp.status < 400:
                    cl_raw = resp.headers.get("Content-Length")
                    if cl_raw and cl_raw.isdigit() and int(cl_raw) > 0:
                        opt.est_bytes = int(cl_raw)
                        opt.is_approx = False

    with contextlib.suppress(Exception):
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": CHROME_UA}
        ) as session:
            await asyncio.gather(*(_head_one(session, opt, u) for opt, u in missing[:6]))


def _format_res_label(width: int | None, height: int) -> str:
    if width and width > 0 and width < height:
        if width in {2160, 1440, 1080, 720, 480, 360, 240, 144}:
            return f"{width}p ↕"
        return f"{width}×{height}"
    return f"{height}p"


async def _run_ytdlp_probe(
    base_cmd: list[str],
    safe_url: str,
    impersonate_chrome: bool = False,
) -> tuple[int, bytes, bytes]:
    cmd = [*base_cmd, "--dump-single-json", "--no-playlist", "--no-color", "--skip-download"]
    if impersonate_chrome:
        cmd.extend(["--impersonate", "chrome"])
    cmd.extend(["--", safe_url])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=25.0)
        return proc.returncode or 0, stdout, stderr
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        logger.warning("yt-dlp probe timeout (>25s) para URL: %s", safe_url)
        raise RuntimeError("Tiempo agotado al inspeccionar el enlace.")


def _looks_like_cloudflare_block(err_text: str) -> bool:
    low = err_text.lower()
    return any(
        tok in low
        for tok in ("cloudflare", "403", "impersonat", "anti-bot", "challenge", "captcha")
    )


async def probe_url_info(url: str, gofile_token: str = "") -> UrlProbeInfo:
    """Inspecciona un enlace antes de descargarlo para conocer resoluciones y pesos disponibles."""
    safe_url = await _validate_external_url(url)

    if identify_file_host(safe_url) is not None:
        resolved = await resolve_host_download(
            safe_url,
            _validate_external_url,
            gofile_token=gofile_token,
        )
        if resolved is not None:
            clean_name = "".join(
                c if c.isalnum() or c in " -_.()[]" else "_"
                for c in (resolved.filename or "archivo_enlace.bin")
            ).strip(" .") or "archivo_enlace.bin"
            return UrlProbeInfo(
                url=safe_url,
                title=Path(clean_name).stem,
                filename=clean_name[:100],
                duration=None,
                is_direct=True,
                direct_bytes=resolved.file_size,
                formats=[],
            )

    is_direct, direct_bytes, direct_name = await _probe_direct_head(safe_url)
    if is_direct:
        clean_name = "".join(
            c if c.isalnum() or c in " -_.()[]" else "_" for c in direct_name
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

    rc, stdout, stderr = await _run_ytdlp_probe(base_cmd, safe_url, impersonate_chrome=False)
    stderr_txt = stderr.decode("utf-8", errors="replace").strip()
    if rc != 0 and _looks_like_cloudflare_block(stderr_txt):
        curl_ready = has_curl_cffi() or await asyncio.to_thread(ensure_curl_cffi)
        if curl_ready:
            logger.info("Reintentando pre-análisis yt-dlp con --impersonate chrome: %s", safe_url[:80])
            rc, stdout, stderr = await _run_ytdlp_probe(base_cmd, safe_url, impersonate_chrome=True)
            stderr_txt = stderr.decode("utf-8", errors="replace").strip()

    if rc != 0:
        err_msg = stderr_txt[:280]
        logger.error("yt-dlp probe falló (rc=%s) para %s: %s", rc, safe_url, stderr_txt[:500])
        if _looks_like_cloudflare_block(stderr_txt):
            raise RuntimeError(
                "El servidor bloqueó la inspección con Cloudflare/Captcha (HTTP 403). "
                "Si es un enlace directo o de descarga web, verifica que no exija verificación manual."
            )
        raise RuntimeError(f"No se pudo analizar el enlace: {err_msg or f'código {rc}'}")

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
            a_is_webm = a_ext == "webm" or "opus" in acodec or "vorbis" in acodec
            a_score = (
                1 if not a_hls else 0,
                1 if a_is_webm else 0,
                1 if not a_approx else 0,
                a_abr,
            )
            if a_score > best_audio_score:
                best_audio_score = a_score
                best_audio_bytes = a_bytes
                best_audio_approx = a_approx

    grouped: dict[int, tuple[tuple[int, int, int, int, int, float], UrlFormatOption, str, str | None]] = {}
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
        fmt_direct_url: str | None = None
        if not is_hls and has_audio:
            raw_f_url = str(f.get("url") or "").strip()
            if raw_f_url.startswith(("http://", "https://")):
                fmt_direct_url = raw_f_url

        score = (
            1 if not is_hls else 0,
            1 if (total_bytes is not None and not is_approx) else 0,
            1 if ext == "webm" else 0,
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
            grouped[h_val] = (score, opt, ext, fmt_direct_url)

    sorted_items = sorted(grouped.items(), key=lambda kv: kv[0], reverse=True)[:7]
    await _fill_missing_direct_format_sizes([(item[1][1], item[1][3]) for item in sorted_items])
    formats_sorted = [item[1][1] for item in sorted_items]
    top_ext = sorted_items[0][1][2] if sorted_items else "mp4"
    disp_ext = "webm" if top_ext == "webm" else "mp4"
    filename = f"{(safe_stem or 'video_enlace')[:84]}.{disp_ext}"

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


def _check_unexpected_html_stream(ctype: str, first_chunk: bytes) -> None:
    if "text/html" not in ctype:
        return
    preview = first_chunk[:4096].decode("utf-8", errors="replace").lower()
    if any(m in preview for m in ("cf-turnstile", "challenges.cloudflare.com", "just a moment", "captcha")):
        raise HostExtractionError(
            "El servidor activó un desafío JavaScript / Captcha de Cloudflare que no puede "
            "resolverse automáticamente; genera un enlace directo o usa otro servidor."
        )
    if "password" in preview or "contraseña" in preview:
        raise HostExtractionError("El servidor solicita contraseña para descargar este archivo.")
    raise HostExtractionError(
        "El servidor devolvió una página HTML en lugar del archivo (posible enlace expirado, cuota o bloqueo anti-bot)."
    )


async def _stream_with_curl_cffi(
    url: str,
    dest_root: Path,
    max_allowed_bytes: int,
    cancel_event: asyncio.Event,
    on_progress: TransferProgressCb | None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    forced_filename: str | None = None,
) -> Path:
    from curl_cffi.requests import AsyncSession

    req_headers = {"User-Agent": CHROME_UA, **(headers or {})}
    async with AsyncSession(impersonate="chrome", timeout=None) as session:
        async with session.stream(
            "GET",
            url,
            headers=req_headers,
            cookies=cookies or None,
            allow_redirects=True,
        ) as resp:
            await _validate_external_url(str(resp.url))
            if resp.status_code >= 400:
                if resp.status_code in {403, 503}:
                    raise HostExtractionError(
                        f"El servidor bloqueó la descarga (HTTP {resp.status_code}, posible Cloudflare o enlace expirado)."
                    )
                raise RuntimeError(f"El enlace respondió con error HTTP {resp.status_code}.")

            cl_raw = resp.headers.get("Content-Length")
            total = int(cl_raw) if cl_raw and cl_raw.isdigit() else 0
            if total > 0 and total > max_allowed_bytes:
                raise RuntimeError(
                    f"El archivo remoto ({total / (1024**3):.2f} GiB) excede el margen de disco seguro."
                )

            ctype = (resp.headers.get("Content-Type") or "").lower()
            filename = _extract_filename_from_response(
                str(resp.url), resp.headers, fallback_name=forced_filename
            )
            dest_path = (dest_root / filename).resolve()
            if not str(dest_path).startswith(str(dest_root) + os.sep):
                raise RuntimeError("Nombre de archivo remoto inválido.")

            downloaded = 0
            last_cb = 0.0
            first_checked = False
            try:
                with dest_path.open("wb") as fp:
                    async for chunk in resp.aiter_content():
                        if cancel_event.is_set():
                            raise JobCancelledError("Descarga cancelada por el usuario.")
                        if not chunk:
                            continue
                        if not first_checked:
                            first_checked = True
                            _check_unexpected_html_stream(ctype, chunk)
                        fp.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > max_allowed_bytes:
                            raise RuntimeError("La descarga excedió el límite de espacio libre seguro.")
                        if on_progress:
                            now = time.monotonic()
                            if (total > 0 and downloaded >= total) or (now - last_cb) >= 0.7:
                                last_cb = now
                                await on_progress(downloaded, total)
            except Exception:
                dest_path.unlink(missing_ok=True)
                raise

            if downloaded == 0:
                dest_path.unlink(missing_ok=True)
                raise RuntimeError("El enlace devolvió un archivo vacío.")
            if on_progress:
                await on_progress(downloaded, total or downloaded)
            return dest_path


async def _download_http_direct(
    url: str,
    dest_dir: Path,
    max_allowed_bytes: int,
    cancel_event: asyncio.Event,
    on_progress: TransferProgressCb | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    prefer_curl_cffi: bool = False,
    forced_filename: str | None = None,
) -> Path:
    dest_root = dest_dir.resolve()
    if prefer_curl_cffi and (has_curl_cffi() or await asyncio.to_thread(ensure_curl_cffi)):
        return await _stream_with_curl_cffi(
            url=url,
            dest_root=dest_root,
            max_allowed_bytes=max_allowed_bytes,
            cancel_event=cancel_event,
            on_progress=on_progress,
            headers=headers,
            cookies=cookies,
            forced_filename=forced_filename,
        )

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
    req_headers = {"User-Agent": CHROME_UA, **(headers or {})}

    try:
        async with aiohttp.ClientSession(
            timeout=timeout, headers=req_headers, cookies=cookies
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                await _validate_external_url(str(resp.url))
                if resp.status in {403, 503} and (
                    has_curl_cffi() or await asyncio.to_thread(ensure_curl_cffi)
                ):
                    logger.info("HTTP %s con aiohttp; reintentando descarga directa con curl-cffi...", resp.status)
                    return await _stream_with_curl_cffi(
                        url=url,
                        dest_root=dest_root,
                        max_allowed_bytes=max_allowed_bytes,
                        cancel_event=cancel_event,
                        on_progress=on_progress,
                        headers=headers,
                        cookies=cookies,
                        forced_filename=forced_filename,
                    )
                if resp.status >= 400:
                    raise RuntimeError(f"El enlace respondió con error HTTP {resp.status}.")

                total = int(resp.headers.get("Content-Length") or 0)
                if total > 0 and total > max_allowed_bytes:
                    raise RuntimeError(
                        f"El archivo remoto ({total / (1024**3):.2f} GiB) excede el margen de disco seguro."
                    )

                ctype = (resp.headers.get("Content-Type") or "").lower()
                filename = _extract_filename_from_response(
                    str(resp.url), resp.headers, fallback_name=forced_filename
                )
                dest_path = (dest_root / filename).resolve()
                if not str(dest_path).startswith(str(dest_root) + os.sep):
                    raise RuntimeError("Nombre de archivo remoto inválido.")

                downloaded = 0
                first_checked = False
                try:
                    with dest_path.open("wb") as fp:
                        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                            if cancel_event.is_set():
                                raise JobCancelledError("Descarga cancelada por el usuario.")
                            if not chunk:
                                continue
                            if not first_checked:
                                first_checked = True
                                _check_unexpected_html_stream(ctype, chunk)
                            fp.write(chunk)
                            downloaded += len(chunk)
                            if downloaded > max_allowed_bytes:
                                raise RuntimeError("La descarga excedió el límite de espacio libre seguro.")
                            if on_progress:
                                await on_progress(downloaded, total)
                except Exception:
                    dest_path.unlink(missing_ok=True)
                    raise

                if downloaded == 0:
                    dest_path.unlink(missing_ok=True)
                    raise RuntimeError("El enlace devolvió un archivo vacío.")
                return dest_path
    except HostExtractionError:
        raise


_YTDLP_VIDEO_EXTS = {".webm", ".mkv", ".mp4", ".mov", ".m4v", ".avi", ".ts", ".flv"}
_YTDLP_AUDIO_EXTS = {".m4a", ".opus", ".mp3", ".aac", ".ogg", ".oga", ".wav", ".flac"}
_YTDLP_SPLIT_FORMAT_RE = re.compile(r"\.f\d+\.[a-z0-9]{2,5}$", re.IGNORECASE)


async def _file_has_video_stream(file_path: Path) -> bool | None:
    """Verifica con ffprobe si el archivo contiene una pista de video real (excluye carátulas)."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        str(file_path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return None

    if proc.returncode != 0:
        return False
    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None

    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        if stream.get("codec_type") != "video":
            continue
        disp = stream.get("disposition") or {}
        if int(disp.get("attached_pic") or 0) != 0:
            continue
        w = int(stream.get("width") or 0)
        h = int(stream.get("height") or 0)
        if w > 0 and h > 0:
            return True
    return False


async def _exec_ytdlp_download(
    base_cmd: list[str],
    url: str,
    dest_root: Path,
    max_allowed_bytes: int,
    fmt_expr: str,
    cancel_event: asyncio.Event,
    on_progress: TransferProgressCb | None,
    impersonate_chrome: bool = False,
) -> tuple[int, list[str], bool]:
    out_tmpl = str(dest_root / "%(title).70B [%(id).20B].%(ext)s")
    cmd = [
        *base_cmd,
        "--no-playlist",
        "--newline",
        "--no-color",
        "--no-warnings",
        "--restrict-filenames",
        "--concurrent-fragments",
        "6",
        "--retries",
        "5",
        "--fragment-retries",
        "5",
        "--max-filesize",
        str(max_allowed_bytes),
        "--format",
        fmt_expr,
    ]
    if impersonate_chrome:
        cmd.extend(["--impersonate", "chrome"])
    cmd.extend(["-o", out_tmpl, "--", url])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    output_tail: list[str] = []
    saw_max_filesize = False
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
            low_line = line.lower()
            if "max-filesize" in low_line:
                saw_max_filesize = True
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
        return proc.returncode or 0, output_tail, saw_max_filesize
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()


def _cleanup_dir_files(dest_root: Path, keep: Path | None = None) -> None:
    keep_res = keep.resolve() if keep is not None else None
    for item in dest_root.iterdir():
        if keep_res is not None and item.resolve() == keep_res:
            continue
        if item.is_file():
            with contextlib.suppress(OSError):
                item.unlink()


async def _download_with_ytdlp(
    url: str,
    dest_dir: Path,
    max_allowed_bytes: int,
    cancel_event: asyncio.Event,
    on_progress: TransferProgressCb | None = None,
    max_height: int | None = None,
    expect_video: bool = False,
) -> Path:
    base_cmd = await asyncio.to_thread(ensure_ytdlp, Path("bin"))
    if base_cmd is None:
        raise RuntimeError(
            "No se pudo instalar automáticamente 'yt-dlp'. "
            "Verifica la salida a internet del servidor o ejecuta 'pip install yt-dlp'."
        )

    if max_height and max_height > 0:
        fmt_expr = (
            f"bv*[height<={max_height}][ext=webm][protocol^=http]+ba[ext=webm][protocol^=http]/"
            f"bv*[height<={max_height}][ext=webm]+ba[ext=webm]/"
            f"bv*[height<={max_height}][protocol^=http]+ba[protocol^=http]/"
            f"bv*[height<={max_height}]+ba/"
            f"b[height<={max_height}][ext=webm]/b[height<={max_height}][ext=mp4]/b[height<={max_height}]/b"
        )
    else:
        fmt_expr = (
            "bv*[ext=webm][protocol^=http]+ba[ext=webm][protocol^=http]/"
            "bv*[ext=webm]+ba[ext=webm]/"
            "bv*[protocol^=http]+ba[protocol^=http]/"
            "bv*+ba/b[ext=webm]/b[ext=mp4]/b"
        )

    dest_root = dest_dir.resolve()
    rc, output_tail, saw_max_filesize = await _exec_ytdlp_download(
        base_cmd=base_cmd,
        url=url,
        dest_root=dest_root,
        max_allowed_bytes=max_allowed_bytes,
        fmt_expr=fmt_expr,
        cancel_event=cancel_event,
        on_progress=on_progress,
        impersonate_chrome=False,
    )
    joined_tail = " ".join(output_tail[-6:])
    if rc != 0 and _looks_like_cloudflare_block(joined_tail):
        curl_ready = has_curl_cffi() or await asyncio.to_thread(ensure_curl_cffi)
        if curl_ready:
            logger.info("yt-dlp recibió bloqueo 403/anti-bot; reintentando con --impersonate chrome: %s", url[:80])
            rc, output_tail, saw_retry_limit = await _exec_ytdlp_download(
                base_cmd=base_cmd,
                url=url,
                dest_root=dest_root,
                max_allowed_bytes=max_allowed_bytes,
                fmt_expr=fmt_expr,
                cancel_event=cancel_event,
                on_progress=on_progress,
                impersonate_chrome=True,
            )
            saw_max_filesize = saw_max_filesize or saw_retry_limit
            joined_tail = " ".join(output_tail[-6:])

    if rc != 0:
        _cleanup_dir_files(dest_root)
        err_msg = " ".join(output_tail[-4:])[:360] or f"código {rc}"
        logger.error("yt-dlp descarga falló (rc=%s) para %s: %s", rc, url, "\n".join(output_tail[-8:]))
        if _looks_like_cloudflare_block(joined_tail):
            raise RuntimeError(
                "El servidor bloqueó la descarga con un desafío Cloudflare / Captcha que requiere navegador completo."
            )
        raise RuntimeError(f"yt-dlp no pudo descargar el enlace: {err_msg}")

    all_files = [p for p in dest_root.iterdir() if p.is_file()]
    part_files = [
        p
        for p in all_files
        if ".part" in p.name or p.name.endswith((".ytdl", ".temp"))
    ]
    completed = [
        p
        for p in all_files
        if p not in part_files
        and p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}
        and p.stat().st_size > 0
    ]

    if not completed:
        _cleanup_dir_files(dest_root)
        if saw_max_filesize or part_files:
            raise RuntimeError(
                f"La descarga superó el límite de espacio seguro ({max_allowed_bytes / (1024**3):.1f} GiB) "
                "y quedó incompleta. Prueba una resolución menor (ej. 1080p o 720p)."
            )
        raise RuntimeError("yt-dlp terminó sin generar ningún archivo descargable.")

    video_candidates = sorted(
        (p for p in completed if p.suffix.lower() in _YTDLP_VIDEO_EXTS),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    chosen_video: Path | None = None
    for cand in video_candidates:
        has_vid = await _file_has_video_stream(cand)
        if has_vid is True:
            chosen_video = cand
            break
        if has_vid is None and not _YTDLP_SPLIT_FORMAT_RE.search(cand.name):
            chosen_video = cand
            break

    if chosen_video is not None:
        chosen = chosen_video.resolve()
        if not str(chosen).startswith(str(dest_root) + os.sep):
            raise RuntimeError("Ruta de descarga fuera del directorio permitido.")
        _cleanup_dir_files(dest_root, keep=chosen)
        return chosen

    # No hubo video válido: proteger contra .m4a / .opus / .webm solo-audio huérfano
    largest_other = max(completed, key=lambda p: p.stat().st_size).resolve()
    has_split_tag = any(_YTDLP_SPLIT_FORMAT_RE.search(p.name) for p in completed)
    looks_like_failed_video = (
        expect_video
        or bool(part_files)
        or saw_max_filesize
        or (max_height is not None and max_height > 0)
        or has_split_tag
        or largest_other.suffix.lower() in _YTDLP_VIDEO_EXTS
    )
    if looks_like_failed_video:
        _cleanup_dir_files(dest_root)
        if saw_max_filesize or part_files:
            raise RuntimeError(
                f"La pista de video superó el margen de disco seguro (~{max_allowed_bytes / (1024**3):.1f} GiB) "
                "o quedó incompleta (.part); se abortó para no subir solo el audio. Prueba una resolución menor (ej. 1080p o 720p)."
            )
        raise RuntimeError(
            "yt-dlp no pudo obtener una pista de video válida (solo quedó audio suelto sin video). "
            "Intenta otra calidad o revisa el enlace."
        )

    if not str(largest_other).startswith(str(dest_root) + os.sep):
        raise RuntimeError("Ruta de descarga fuera del directorio permitido.")
    _cleanup_dir_files(dest_root, keep=largest_other)
    return largest_other


async def download_from_url(
    url: str,
    dest_dir: Path,
    max_allowed_bytes: int,
    cancel_event: asyncio.Event,
    on_progress: TransferProgressCb | None = None,
    max_height: int | None = None,
    gofile_token: str = "",
    max_ytdlp_bytes: int | None = None,
    expect_video: bool = False,
) -> Path:
    """Descarga un archivo/video desde servidor nativo, HTTP directo o yt-dlp."""
    safe_url = await _validate_external_url(url)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if identify_file_host(safe_url) is not None:
        resolved = await resolve_host_download(
            safe_url,
            _validate_external_url,
            gofile_token=gofile_token,
        )
        if resolved is not None:
            return await _download_http_direct(
                url=resolved.direct_url,
                dest_dir=dest_dir,
                max_allowed_bytes=max_allowed_bytes,
                cancel_event=cancel_event,
                on_progress=on_progress,
                headers=resolved.headers,
                cookies=resolved.cookies,
                prefer_curl_cffi=resolved.use_curl_cffi,
                forced_filename=resolved.filename,
            )

    is_direct = await _probe_direct_media(safe_url)
    if is_direct:
        return await _download_http_direct(
            url=safe_url,
            dest_dir=dest_dir,
            max_allowed_bytes=max_allowed_bytes,
            cancel_event=cancel_event,
            on_progress=on_progress,
        )

    ytdlp_limit = max_ytdlp_bytes if max_ytdlp_bytes is not None else max_allowed_bytes
    return await _download_with_ytdlp(
        url=safe_url,
        dest_dir=dest_dir,
        max_allowed_bytes=ytdlp_limit,
        cancel_event=cancel_event,
        on_progress=on_progress,
        max_height=max_height,
        expect_video=expect_video,
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
