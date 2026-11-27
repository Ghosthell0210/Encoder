"""Extractores nativos para servidores de archivos y bypass TLS (curl-cffi).

Soporta:
  - MediaFire (mediafire.com)
  - Pixeldrain (pixeldrain.com / pixeldrain.net)
  - GoFile (gofile.io)
  - Buzzheavier (buzzheavier.com / bzzhr.co)
  - Google Drive público (drive.google.com / drive.usercontent.google.com)
  - Dropbox (dropbox.com / dl.dropboxusercontent.com)
  - Krakenfiles (krakenfiles.com)
  - 1Fichier (1fichier.com)
  - Qiwi (qiwi.gg)
  - Bunkr (bunkr.* archivos individuales)

Usa impersonación TLS de Chrome vía `curl-cffi` cuando está disponible (con
instalación automática sin root) para evitar bloqueos 403 por huella TLS de
Cloudflare, y reporta errores legibles cuando existe Captcha/Turnstile JS activo,
contraseña, cooldown o archivo privado/eliminado.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html as html_lib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import aiohttp

from core.ffmpeg_bootstrap import ensure_curl_cffi, has_curl_cffi

logger = logging.getLogger(__name__)

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)

UrlValidator = Callable[[str], Awaitable[str]]


class HostExtractionError(RuntimeError):
    """Error definitivo al resolver un servidor de archivos (no reintentar con yt-dlp)."""


@dataclass(slots=True)
class ResolvedHostDownload:
    provider: str
    direct_url: str
    filename: str | None = None
    file_size: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    use_curl_cffi: bool = False
    resolved_at: float = field(default_factory=time.time)


_BUNKR_HOST_RE = re.compile(r"(?:^|\.)bunkr+\.[a-z]{2,}$", re.IGNORECASE)


def identify_file_host(url: str) -> str | None:
    """Devuelve el identificador del servidor soportado, o None si no aplica."""
    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return None
    if host == "mediafire.com" or host.endswith(".mediafire.com"):
        return "mediafire"
    if host in {"pixeldrain.com", "pixeldrain.net", "pixeldrain.dev"} or host.endswith(
        (".pixeldrain.com", ".pixeldrain.net")
    ):
        return "pixeldrain"
    if host == "gofile.io" or host.endswith(".gofile.io"):
        return "gofile"
    if host in {"buzzheavier.com", "bzzhr.co"} or host.endswith(
        (".buzzheavier.com", ".bzzhr.co")
    ):
        return "buzzheavier"
    if host in {"drive.google.com", "drive.usercontent.google.com"}:
        return "gdrive"
    if host == "docs.google.com" and "/file/" in urlparse(url).path.lower():
        return "gdrive"
    if host in {"dropbox.com", "dl.dropboxusercontent.com"} or host.endswith(
        (".dropbox.com", ".dropboxusercontent.com")
    ):
        return "dropbox"
    if host == "krakenfiles.com" or host.endswith(".krakenfiles.com"):
        return "krakenfiles"
    if host == "1fichier.com" or host.endswith(".1fichier.com"):
        return "1fichier"
    if host == "qiwi.gg" or host.endswith(".qiwi.gg"):
        return "qiwi"
    if _BUNKR_HOST_RE.search(host):
        return "bunkr"
    return None


def _clean_filename(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = unquote(html_lib.unescape(raw)).strip()
    cleaned = "".join(c if c.isalnum() or c in " -_.()[]" else "_" for c in cleaned).strip(" .")
    return cleaned[:120] or None


def _filename_from_cd(cd: str | None, fallback_url: str) -> str | None:
    if cd:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', cd, flags=re.IGNORECASE)
        if m:
            name = _clean_filename(m.group(1))
            if name:
                return name
    path_name = Path(unquote(urlparse(fallback_url).path)).name
    if path_name and "." in path_name:
        return _clean_filename(path_name)
    return None


def _detect_cf_challenge(status: int, body: str) -> str | None:
    low = body.lower()
    markers = (
        "cf-turnstile",
        "challenges.cloudflare.com",
        "just a moment...",
        "__cf_chl_",
        "attention required! | cloudflare",
        "enable javascript and cookies to continue",
        "checking your browser before accessing",
        "g-recaptcha",
        "h-captcha",
    )
    if any(m in low for m in markers):
        return (
            "El servidor activó un desafío JavaScript / Captcha de Cloudflare que no puede "
            "resolverse automáticamente; genera un enlace directo o usa otro servidor."
        )
    if status in {403, 503} and "cloudflare" in low:
        return (
            f"Cloudflare bloqueó el acceso (HTTP {status}). "
            "Prueba de nuevo en unos momentos o usa un enlace directo."
        )
    return None


async def _ensure_impersonation_ready() -> bool:
    if has_curl_cffi():
        return True
    return await asyncio.to_thread(ensure_curl_cffi)


def _create_curl_session():
    from curl_cffi.requests import AsyncSession

    return AsyncSession(impersonate="chrome", timeout=25)


async def _probe_direct_metadata(
    direct_url: str,
    validate_url: UrlValidator,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    use_curl_cffi: bool = True,
) -> tuple[str | None, int | None]:
    """Consulta rápida de encabezados para conocer nombre y tamaño exactos sin descargar el cuerpo."""
    await validate_url(direct_url)
    req_headers = {"User-Agent": CHROME_UA, **(headers or {})}

    if use_curl_cffi and await _ensure_impersonation_ready():
        try:
            async with _create_curl_session() as session:
                async with session.stream(
                    "GET",
                    direct_url,
                    headers=req_headers,
                    cookies=cookies or None,
                    allow_redirects=True,
                ) as resp:
                    await validate_url(str(resp.url))
                    if resp.status_code >= 400:
                        return None, None
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if "text/html" in ctype:
                        return None, None
                    cl_raw = resp.headers.get("Content-Length")
                    size = int(cl_raw) if cl_raw and cl_raw.isdigit() and int(cl_raw) > 0 else None
                    fname = _filename_from_cd(resp.headers.get("Content-Disposition"), str(resp.url))
                    return fname, size
        except HostExtractionError:
            raise
        except RuntimeError:
            raise
        except Exception:
            pass

    timeout = aiohttp.ClientTimeout(total=12, sock_connect=10, sock_read=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=req_headers, cookies=cookies) as session:
            async with session.head(direct_url, allow_redirects=True) as resp:
                await validate_url(str(resp.url))
                if resp.status >= 400:
                    return None, None
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/html" in ctype:
                    return None, None
                cl_raw = resp.headers.get("Content-Length")
                size = int(cl_raw) if cl_raw and cl_raw.isdigit() and int(cl_raw) > 0 else None
                fname = _filename_from_cd(resp.headers.get("Content-Disposition"), str(resp.url))
                return fname, size
    except RuntimeError:
        raise
    except Exception:
        return None, None


async def _fetch_text_with_session(
    url: str,
    validate_url: UrlValidator,
    headers: dict[str, str] | None = None,
) -> tuple[int, str, str, dict[str, str], bool]:
    """Descarga HTML/JSON usando curl-cffi Chrome TLS (con fallback aiohttp)."""
    req_headers = {"User-Agent": CHROME_UA, **(headers or {})}
    if await _ensure_impersonation_ready():
        async with _create_curl_session() as session:
            resp = await session.get(url, headers=req_headers, allow_redirects=True)
            final_url = str(resp.url)
            await validate_url(final_url)
            cookies_dict = dict(session.cookies.items()) if hasattr(session, "cookies") else {}
            return resp.status_code, final_url, resp.text or "", cookies_dict, True

    timeout = aiohttp.ClientTimeout(total=20, sock_connect=10, sock_read=15)
    async with aiohttp.ClientSession(timeout=timeout, headers=req_headers) as session:
        async with session.get(url, allow_redirects=True) as resp:
            final_url = str(resp.url)
            await validate_url(final_url)
            raw = await resp.content.read(1024 * 1024)
            text = raw.decode("utf-8", errors="replace")
            cookies_dict = {k: v.value for k, v in resp.cookies.items()}
            return resp.status, final_url, text, cookies_dict, False


def _extract_mediafire_url(page_html: str) -> str | None:
    # 1. data-scrambled-url (Base64)
    m_scram = re.search(r'data-scrambled-url=["\']([^"\']+)["\']', page_html, re.IGNORECASE)
    if m_scram:
        with contextlib.suppress(Exception):
            decoded = base64.b64decode(m_scram.group(1).strip()).decode("utf-8", errors="ignore").strip()
            if decoded.startswith(("http://", "https://")):
                return html_lib.unescape(decoded)

    # 2. Botón principal id="downloadButton"
    for pat in (
        r'<a[^>]+id=["\']downloadButton["\'][^>]+href=["\'](https?://[^"\']+)["\']',
        r'<a[^>]+href=["\'](https?://[^"\']+)["\'][^>]+id=["\']downloadButton["\']',
        r'<a[^>]+aria-label=["\']Download file["\'][^>]+href=["\'](https?://[^"\']+)["\']',
        r'(https?://download\d*\.mediafire\.com/[^"\'\s<>]+)',
    ):
        m = re.search(pat, page_html, re.IGNORECASE)
        if m:
            candidate = html_lib.unescape(m.group(1).strip())
            if candidate.startswith(("http://", "https://")):
                return candidate
    return None


def _extract_mediafire_filename(page_html: str, direct_url: str) -> str | None:
    for pat in (
        r'class=["\'][^"\']*dl-btn-label[^"\']*["\'][^>]*title=["\']([^"\']+)["\']',
        r'<div[^>]+class=["\'][^"\']*filename[^"\']*["\'][^>]*>([^<]+)</div>',
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    ):
        m = re.search(pat, page_html, re.IGNORECASE)
        if m:
            name = _clean_filename(m.group(1))
            if name and name.lower() not in {"mediafire", "download"}:
                return name
    return _filename_from_cd(None, direct_url)


async def _probe_mediafire_direct(
    url: str,
    validate_url: UrlValidator,
) -> ResolvedHostDownload:
    """Inspecciona un enlace downloadXXXX.mediafire.com para validar si sirve archivo o HTML/redirección."""
    await validate_url(url)
    parsed = urlparse(url)
    fallback_name = _clean_filename(Path(unquote(parsed.path)).name)
    req_headers = {"User-Agent": CHROME_UA, "Referer": "https://www.mediafire.com/"}

    if await _ensure_impersonation_ready():
        try:
            async with _create_curl_session() as session:
                async with session.stream(
                    "GET",
                    url,
                    headers=req_headers,
                    allow_redirects=True,
                ) as resp:
                    final_url = str(resp.url)
                    await validate_url(final_url)
                    status = resp.status_code
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if status < 400 and "text/html" not in ctype:
                        cl_raw = resp.headers.get("Content-Length")
                        fsize = int(cl_raw) if cl_raw and cl_raw.isdigit() and int(cl_raw) > 0 else None
                        fname = _filename_from_cd(resp.headers.get("Content-Disposition"), final_url)
                        return ResolvedHostDownload(
                            provider="MediaFire",
                            direct_url=final_url,
                            filename=fname or fallback_name,
                            file_size=fsize,
                            headers={"Referer": "https://www.mediafire.com/"},
                            cookies=dict(session.cookies.items()) if hasattr(session, "cookies") else {},
                            use_curl_cffi=True,
                        )

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_content():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= 128 * 1024:
                            break
                    html_snippet = b"".join(chunks).decode("utf-8", errors="replace")
                    cookies = dict(session.cookies.items()) if hasattr(session, "cookies") else {}
        except HostExtractionError:
            raise
        except RuntimeError:
            raise
        except Exception as exc:
            logger.debug("Error inspeccionando downloadXXXX MediaFire (%s): %s", url[:60], exc)
            html_snippet = ""
            final_url = url
            status = 0
            cookies = {}
    else:
        status, final_url, html_snippet, cookies, _ = await _fetch_text_with_session(
            url, validate_url, headers={"Referer": "https://www.mediafire.com/"}
        )

    if html_snippet:
        # Si redirigió a la ficha mediafire.com/file/... intentar extraer el enlace nuevo para esta IP
        recovered = _extract_mediafire_url(html_snippet)
        if recovered and not recovered.startswith(url.split("?", 1)[0]):
            await validate_url(recovered)
            dl_headers = {"Referer": final_url}
            fname_head, fsize = await _probe_direct_metadata(
                recovered, validate_url, headers=dl_headers, cookies=cookies, use_curl_cffi=True
            )
            return ResolvedHostDownload(
                provider="MediaFire",
                direct_url=recovered,
                filename=fname_head or _extract_mediafire_filename(html_snippet, recovered) or fallback_name,
                file_size=fsize,
                headers=dl_headers,
                cookies=cookies,
                use_curl_cffi=True,
            )

        cf_err = _detect_cf_challenge(status, html_snippet)
        if cf_err:
            raise HostExtractionError(
                "El enlace temporal 'downloadXXXX.mediafire.com' está atado a otra IP, expiró o activó Cloudflare. "
                "Envía el enlace normal de la página (https://www.mediafire.com/file/...)."
            )

        low = html_snippet.lower()
        if status == 404 or "invalid or deleted file" in low or "file removed" in low:
            raise HostExtractionError("El archivo no existe o fue eliminado de MediaFire.")
        if 'name="downloadp"' in low or "password protected" in low:
            raise HostExtractionError("Este archivo de MediaFire está protegido con contraseña.")

        raise HostExtractionError(
            "El enlace directo temporal de MediaFire ('downloadXXXX.mediafire.com') devolvió una página HTML en lugar del archivo "
            "(caducado o bloqueado para otra IP). Envía el enlace normal de la ficha (https://www.mediafire.com/file/...)."
        )

    if status in {403, 503}:
        raise HostExtractionError(
            f"MediaFire bloqueó el enlace directo temporal (HTTP {status}). "
            "Envía el enlace de la ficha (https://www.mediafire.com/file/...)."
        )
    if status >= 400:
        raise HostExtractionError(f"MediaFire devolvió error HTTP {status}.")

    return ResolvedHostDownload(
        provider="MediaFire",
        direct_url=final_url,
        filename=fallback_name,
        file_size=None,
        headers={"Referer": "https://www.mediafire.com/"},
        cookies=cookies,
        use_curl_cffi=True,
    )


async def _resolve_mediafire(url: str, validate_url: UrlValidator) -> ResolvedHostDownload:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("download") and "." in Path(unquote(parsed.path)).name:
        return await _probe_mediafire_direct(url, validate_url)

    if "/folder/" in parsed.path.lower():
        raise HostExtractionError(
            "Las carpetas completas de MediaFire aún no se soportan; envía el enlace individual del archivo."
        )

    status, final_url, body, cookies, used_curl = await _fetch_text_with_session(url, validate_url)
    cf_err = _detect_cf_challenge(status, body)
    if cf_err:
        raise HostExtractionError(cf_err)

    low = body.lower()
    if status == 404 or "invalid or deleted file" in low or "file removed" in low or "error.php" in final_url.lower():
        raise HostExtractionError("El archivo no existe o fue eliminado de MediaFire.")
    if 'name="downloadp"' in low or "this file is password protected" in low or "enter password" in low:
        raise HostExtractionError("Este archivo de MediaFire está protegido con contraseña.")
    if status >= 400:
        raise HostExtractionError(f"MediaFire devolvió error HTTP {status}.")

    direct_url = _extract_mediafire_url(body)
    if not direct_url:
        raise HostExtractionError(
            "No se encontró el enlace directo en la página de MediaFire (posible cambio de diseño o bloqueo)."
        )

    await validate_url(direct_url)
    headers = {"Referer": final_url}
    fname_head, fsize = await _probe_direct_metadata(
        direct_url, validate_url, headers=headers, cookies=cookies, use_curl_cffi=used_curl
    )
    fname = fname_head or _extract_mediafire_filename(body, direct_url)
    return ResolvedHostDownload(
        provider="MediaFire",
        direct_url=direct_url,
        filename=fname,
        file_size=fsize,
        headers=headers,
        cookies=cookies,
        use_curl_cffi=used_curl,
    )


async def _resolve_pixeldrain(url: str, validate_url: UrlValidator) -> ResolvedHostDownload:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    m_list = re.search(r"/l/([A-Za-z0-9_-]+)", path)
    m_file = re.search(r"/(?:u|api/file)/([A-Za-z0-9_-]+)", path)

    if m_list:
        list_id = m_list.group(1)
        info_url = f"https://pixeldrain.com/api/list/{list_id}"
        status, _, body, _, used_curl = await _fetch_text_with_session(info_url, validate_url)
        cf_err = _detect_cf_challenge(status, body)
        if cf_err:
            raise HostExtractionError(cf_err)
        if status == 404:
            raise HostExtractionError("La lista de Pixeldrain no existe o fue eliminada.")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HostExtractionError("Pixeldrain devolvió una respuesta inválida para la lista.") from exc
        files = data.get("files") or []
        if not files:
            raise HostExtractionError("La lista de Pixeldrain está vacía.")
        if len(files) > 1:
            raise HostExtractionError(
                f"La lista de Pixeldrain contiene {len(files)} archivos; envía el enlace individual (/u/...) del archivo que deseas bajar."
            )
        f_obj = files[0]
        file_id = str(f_obj.get("id") or "")
        if not file_id:
            raise HostExtractionError("No se pudo identificar el archivo dentro de la lista de Pixeldrain.")
        direct_url = f"https://pixeldrain.com/api/file/{file_id}?download"
        await validate_url(direct_url)
        return ResolvedHostDownload(
            provider="Pixeldrain",
            direct_url=direct_url,
            filename=_clean_filename(f_obj.get("name")),
            file_size=int(f_obj["size"]) if isinstance(f_obj.get("size"), (int, float)) else None,
            use_curl_cffi=used_curl,
        )

    if not m_file:
        raise HostExtractionError("Enlace de Pixeldrain no reconocido. Usa formato /u/ID o /l/ID.")

    file_id = m_file.group(1)
    info_url = f"https://pixeldrain.com/api/file/{file_id}/info"
    status, _, body, _, used_curl = await _fetch_text_with_session(info_url, validate_url)
    cf_err = _detect_cf_challenge(status, body)
    if cf_err:
        raise HostExtractionError(cf_err)
    if status == 404:
        raise HostExtractionError("El archivo no existe o fue eliminado en Pixeldrain.")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HostExtractionError("Pixeldrain devolvió una respuesta inválida.") from exc

    if data.get("success") is False:
        raise HostExtractionError(
            f"Pixeldrain rechazó el archivo: {data.get('message') or data.get('value') or 'no disponible'}."
        )
    if data.get("can_download") is False:
        reason = data.get("availability_message") or data.get("availability") or "límite de ancho de banda o bloqueo"
        raise HostExtractionError(f"Pixeldrain no permite descargar este archivo ahora ({reason}).")

    direct_url = f"https://pixeldrain.com/api/file/{file_id}?download"
    await validate_url(direct_url)
    size_val = int(data["size"]) if isinstance(data.get("size"), (int, float)) and data["size"] > 0 else None
    return ResolvedHostDownload(
        provider="Pixeldrain",
        direct_url=direct_url,
        filename=_clean_filename(data.get("name")),
        file_size=size_val,
        use_curl_cffi=used_curl,
    )


async def _create_gofile_guest_token(use_curl: bool) -> str:
    headers = {"User-Agent": CHROME_UA}
    if use_curl:
        async with _create_curl_session() as session:
            resp = await session.post("https://api.gofile.io/accounts", headers=headers, json={})
            payload = resp.json()
            if payload.get("status") == "ok":
                tok = (payload.get("data") or {}).get("token")
                if tok:
                    return str(tok)
    else:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.post("https://api.gofile.io/accounts", json={}) as resp:
                payload = await resp.json()
                if payload.get("status") == "ok":
                    tok = (payload.get("data") or {}).get("token")
                    if tok:
                        return str(tok)
    raise HostExtractionError("No se pudo iniciar sesión temporal en la API de GoFile.")


def _collect_gofile_files(node: Any) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    if not isinstance(node, dict):
        return files
    if node.get("type") == "file" and node.get("link"):
        files.append(node)
        return files
    children = node.get("children") or node.get("contents") or {}
    if isinstance(children, dict):
        items = list(children.values())
    elif isinstance(children, list):
        items = children
    else:
        items = []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "file" and item.get("link"):
            files.append(item)
    return files


async def _resolve_gofile(
    url: str,
    validate_url: UrlValidator,
    gofile_token: str = "",
) -> ResolvedHostDownload:
    parsed = urlparse(url)
    m_id = re.search(r"/d/([A-Za-z0-9_-]+)", parsed.path)
    if not m_id:
        raise HostExtractionError("Enlace de GoFile no reconocido. Debe tener el formato gofile.io/d/ID.")
    content_id = m_id.group(1)

    use_curl = await _ensure_impersonation_ready()
    token = gofile_token.strip() or await _create_gofile_guest_token(use_curl)

    api_url = f"https://api.gofile.io/contents/{content_id}?wt=4fd6sg8h7s6d"
    headers = {
        "User-Agent": CHROME_UA,
        "Authorization": f"Bearer {token}",
    }
    status, _, body, _, used_curl = await _fetch_text_with_session(
        api_url, validate_url, headers=headers
    )
    cf_err = _detect_cf_challenge(status, body)
    if cf_err:
        raise HostExtractionError(cf_err)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HostExtractionError("GoFile devolvió una respuesta inválida.") from exc

    api_status = str(payload.get("status") or "")
    if api_status != "ok":
        low_st = api_status.lower()
        if "notfound" in low_st or "notpublic" in low_st:
            raise HostExtractionError("El enlace de GoFile no existe, expiró o es privado.")
        if "password" in low_st:
            raise HostExtractionError("Esta carpeta de GoFile está protegida con contraseña.")
        raise HostExtractionError(f"GoFile devolvió un error al consultar el enlace ({api_status}).")

    data = payload.get("data") or {}
    if data.get("password"):
        raise HostExtractionError("Esta carpeta de GoFile está protegida con contraseña.")

    files = _collect_gofile_files(data)
    if not files:
        raise HostExtractionError("La carpeta de GoFile está vacía o no contiene archivos descargables.")

    chosen = max(files, key=lambda f: int(f.get("size") or 0))
    direct_url = str(chosen["link"])
    await validate_url(direct_url)
    dl_cookies = {"accountToken": token}
    dl_headers = {"Authorization": f"Bearer {token}", "Referer": "https://gofile.io/"}
    return ResolvedHostDownload(
        provider="GoFile",
        direct_url=direct_url,
        filename=_clean_filename(chosen.get("name")),
        file_size=int(chosen["size"]) if isinstance(chosen.get("size"), (int, float)) else None,
        headers=dl_headers,
        cookies=dl_cookies,
        use_curl_cffi=used_curl,
    )


async def _resolve_buzzheavier(url: str, validate_url: UrlValidator) -> ResolvedHostDownload:
    use_curl = await _ensure_impersonation_ready()
    req_headers = {"User-Agent": CHROME_UA}
    cookies: dict[str, str] = {}

    if use_curl:
        async with _create_curl_session() as session:
            page_resp = await session.get(url, headers=req_headers, allow_redirects=True)
            final_page_url = str(page_resp.url)
            await validate_url(final_page_url)
            page_status = page_resp.status_code
            page_html = page_resp.text or ""
            cf_err = _detect_cf_challenge(page_status, page_html)
            if cf_err:
                raise HostExtractionError(cf_err)
            if page_status == 404 or "file not found" in page_html.lower() or "does not exist" in page_html.lower():
                raise HostExtractionError("El archivo no existe o fue eliminado de Buzzheavier.")
            if page_status >= 400:
                raise HostExtractionError(f"Buzzheavier devolvió error HTTP {page_status}.")

            m_hx = re.search(r'hx-get=["\']([^"\']*\/download[^"\']*)["\']', page_html, re.IGNORECASE)
            hx_path = html_lib.unescape(m_hx.group(1)) if m_hx else f"{urlparse(final_page_url).path.rstrip('/')}/download"
            ajax_url = urljoin(final_page_url, hx_path)
            await validate_url(ajax_url)

            htmx_headers = {
                **req_headers,
                "HX-Request": "true",
                "HX-Current-URL": final_page_url,
                "Referer": final_page_url,
            }
            ajax_resp = await session.get(ajax_url, headers=htmx_headers, allow_redirects=False)
            redirect_target = (
                ajax_resp.headers.get("HX-Redirect")
                or ajax_resp.headers.get("hx-redirect")
                or ajax_resp.headers.get("Location")
            )
            cookies = dict(session.cookies.items()) if hasattr(session, "cookies") else {}
    else:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=req_headers) as session:
            async with session.get(url, allow_redirects=True) as page_resp:
                final_page_url = str(page_resp.url)
                await validate_url(final_page_url)
                page_status = page_resp.status
                page_html = (await page_resp.content.read(1024 * 1024)).decode("utf-8", errors="replace")
            cf_err = _detect_cf_challenge(page_status, page_html)
            if cf_err:
                raise HostExtractionError(cf_err)
            if page_status == 404 or "file not found" in page_html.lower():
                raise HostExtractionError("El archivo no existe o fue eliminado de Buzzheavier.")
            if page_status >= 400:
                raise HostExtractionError(f"Buzzheavier devolvió error HTTP {page_status}.")

            m_hx = re.search(r'hx-get=["\']([^"\']*\/download[^"\']*)["\']', page_html, re.IGNORECASE)
            hx_path = html_lib.unescape(m_hx.group(1)) if m_hx else f"{urlparse(final_page_url).path.rstrip('/')}/download"
            ajax_url = urljoin(final_page_url, hx_path)
            await validate_url(ajax_url)

            htmx_headers = {
                **req_headers,
                "HX-Request": "true",
                "HX-Current-URL": final_page_url,
                "Referer": final_page_url,
            }
            async with session.get(ajax_url, headers=htmx_headers, allow_redirects=False) as ajax_resp:
                redirect_target = (
                    ajax_resp.headers.get("HX-Redirect")
                    or ajax_resp.headers.get("Location")
                )
                cookies = {k: v.value for k, v in ajax_resp.cookies.items()}

    if not redirect_target:
        raise HostExtractionError(
            "Buzzheavier no entregó el enlace de descarga directa (posible bloqueo temporal o captcha)."
        )

    direct_url = urljoin(final_page_url, html_lib.unescape(redirect_target.strip()))
    await validate_url(direct_url)

    fname: str | None = None
    for pat in (
        r'<span[^>]+class=["\'][^"\']*text-2xl[^"\']*["\'][^>]*>([^<]+)</span>',
        r'<title>\s*([^<]+?)\s*(?:-|—|\|)?\s*Buzzheavier\s*</title>',
    ):
        m_name = re.search(pat, page_html, re.IGNORECASE)
        if m_name:
            fname = _clean_filename(m_name.group(1))
            if fname:
                break

    dl_headers = {"Referer": final_page_url}
    fname_head, fsize = await _probe_direct_metadata(
        direct_url, validate_url, headers=dl_headers, cookies=cookies, use_curl_cffi=use_curl
    )
    return ResolvedHostDownload(
        provider="Buzzheavier",
        direct_url=direct_url,
        filename=fname_head or fname or _filename_from_cd(None, direct_url),
        file_size=fsize,
        headers=dl_headers,
        cookies=cookies,
        use_curl_cffi=use_curl,
    )


def _extract_gdrive_id(url: str) -> str | None:
    for pat in (
        r"/file/d/([A-Za-z0-9_-]{10,})",
        r"/d/([A-Za-z0-9_-]{10,})",
        r"[?&]id=([A-Za-z0-9_-]{10,})",
    ):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _parse_gdrive_confirm_form(page_html: str) -> str | None:
    m_form = re.search(
        r'<form[^>]+id=["\']download-form["\'][^>]+action=["\']([^"\']+)["\']',
        page_html,
        re.IGNORECASE,
    )
    if not m_form:
        m_form = re.search(
            r'<form[^>]+action=["\']([^"\']*usercontent\.google\.com[^"\']*)["\']',
            page_html,
            re.IGNORECASE,
        )
    if not m_form:
        return None
    action = html_lib.unescape(m_form.group(1).strip())
    params: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]+>", page_html, re.IGNORECASE):
        m_name = re.search(r'name=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        m_val = re.search(r'value=["\']([^"\']*)["\']', tag, re.IGNORECASE)
        if m_name:
            params[html_lib.unescape(m_name.group(1))] = html_lib.unescape(
                m_val.group(1) if m_val else ""
            )
    if not params:
        return action
    sep = "&" if "?" in action else "?"
    return f"{action}{sep}{urlencode(params)}"


async def _resolve_gdrive(url: str, validate_url: UrlValidator) -> ResolvedHostDownload:
    parsed = urlparse(url)
    if "/folders/" in parsed.path.lower():
        raise HostExtractionError(
            "Las carpetas completas de Google Drive no están soportadas; envía el enlace del archivo individual."
        )

    file_id = _extract_gdrive_id(url)
    if not file_id:
        raise HostExtractionError("No se pudo extraer el ID del archivo en el enlace de Google Drive.")

    candidate_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    await validate_url(candidate_url)

    use_curl = await _ensure_impersonation_ready()
    req_headers = {"User-Agent": CHROME_UA}
    cookies: dict[str, str] = {}
    status = 0
    final_url = candidate_url
    ctype = ""
    cd = None
    cl_raw = None
    html_snippet = ""

    if use_curl:
        async with _create_curl_session() as session:
            async with session.stream("GET", candidate_url, headers=req_headers, allow_redirects=True) as resp:
                final_url = str(resp.url)
                await validate_url(final_url)
                status = resp.status_code
                ctype = (resp.headers.get("Content-Type") or "").lower()
                cd = resp.headers.get("Content-Disposition")
                cl_raw = resp.headers.get("Content-Length")
                if "text/html" in ctype or status >= 400:
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_content():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= 256 * 1024:
                            break
                    html_snippet = b"".join(chunks).decode("utf-8", errors="replace")
                cookies = dict(session.cookies.items()) if hasattr(session, "cookies") else {}
    else:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=req_headers) as session:
            async with session.get(candidate_url, allow_redirects=True) as resp:
                final_url = str(resp.url)
                await validate_url(final_url)
                status = resp.status
                ctype = (resp.headers.get("Content-Type") or "").lower()
                cd = resp.headers.get("Content-Disposition")
                cl_raw = resp.headers.get("Content-Length")
                if "text/html" in ctype or status >= 400:
                    html_snippet = (await resp.content.read(256 * 1024)).decode("utf-8", errors="replace")
                cookies = {k: v.value for k, v in resp.cookies.items()}

    if status < 400 and "text/html" not in ctype:
        size = int(cl_raw) if cl_raw and cl_raw.isdigit() and int(cl_raw) > 0 else None
        fname = _filename_from_cd(cd, final_url)
        return ResolvedHostDownload(
            provider="Google Drive",
            direct_url=final_url,
            filename=fname,
            file_size=size,
            cookies=cookies,
            use_curl_cffi=use_curl,
        )

    low = html_snippet.lower()
    if "servicelogin" in final_url.lower() or "you need access" in low or "request access" in low or "necesitas acceso" in low:
        raise HostExtractionError("Este archivo de Google Drive es privado o requiere autorización.")
    if "too many users have viewed or downloaded" in low or "quota exceeded" in low:
        raise HostExtractionError(
            "Google Drive bloqueó temporalmente este archivo por exceso de descargas (cuota superada)."
        )
    if status == 404 or "file you have requested does not exist" in low:
        raise HostExtractionError("El archivo no existe en Google Drive o fue eliminado.")

    confirmed_url = _parse_gdrive_confirm_form(html_snippet)
    if confirmed_url:
        await validate_url(confirmed_url)
        fname_head, fsize = await _probe_direct_metadata(
            confirmed_url, validate_url, cookies=cookies, use_curl_cffi=use_curl
        )
        m_title = re.search(r'<span[^>]+class=["\']uc-name-size["\'][^>]*>\s*<a[^>]*>([^<]+)</a>', html_snippet, re.IGNORECASE)
        fallback_name = _clean_filename(m_title.group(1)) if m_title else None
        return ResolvedHostDownload(
            provider="Google Drive",
            direct_url=confirmed_url,
            filename=fname_head or fallback_name,
            file_size=fsize,
            cookies=cookies,
            use_curl_cffi=use_curl,
        )

    if status >= 400:
        raise HostExtractionError(f"Google Drive devolvió error HTTP {status}.")
    raise HostExtractionError(
        "Google Drive no entregó la descarga directa (verifica que el enlace sea público para cualquiera)."
    )


async def _resolve_dropbox(url: str, validate_url: UrlValidator) -> ResolvedHostDownload:
    parsed = urlparse(url)
    if "/sh/" in parsed.path.lower() or "/scl/fo/" in parsed.path.lower():
        # Dropbox entrega carpetas como .zip si lleva dl=1, pero avisar si es link sin archivo
        pass

    q_items = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in {"dl", "raw"}]
    q_items.append(("dl", "1"))
    direct_url = urlunparse(parsed._replace(query=urlencode(q_items)))
    await validate_url(direct_url)

    use_curl = await _ensure_impersonation_ready()
    req_headers = {"User-Agent": CHROME_UA}
    status = 0
    final_url = direct_url
    ctype = ""
    cd = None
    cl_raw = None
    cookies: dict[str, str] = {}
    html_snippet = ""

    if use_curl:
        async with _create_curl_session() as session:
            async with session.stream("GET", direct_url, headers=req_headers, allow_redirects=True) as resp:
                final_url = str(resp.url)
                await validate_url(final_url)
                status = resp.status_code
                ctype = (resp.headers.get("Content-Type") or "").lower()
                cd = resp.headers.get("Content-Disposition")
                cl_raw = resp.headers.get("Content-Length")
                cookies = dict(session.cookies.items()) if hasattr(session, "cookies") else {}
                if "text/html" in ctype or status >= 400:
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_content():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= 128 * 1024:
                            break
                    html_snippet = b"".join(chunks).decode("utf-8", errors="replace")
    else:
        status, final_url, html_snippet, cookies, _ = await _fetch_text_with_session(direct_url, validate_url)

    if status < 400 and "text/html" not in ctype:
        size = int(cl_raw) if cl_raw and cl_raw.isdigit() and int(cl_raw) > 0 else None
        fname = _filename_from_cd(cd, final_url)
        return ResolvedHostDownload(
            provider="Dropbox",
            direct_url=final_url,
            filename=fname,
            file_size=size,
            cookies=cookies,
            use_curl_cffi=use_curl,
        )

    cf_err = _detect_cf_challenge(status, html_snippet)
    if cf_err:
        raise HostExtractionError(cf_err)
    low = html_snippet.lower()
    if status == 404 or "error (404)" in low or "file isn’t here anymore" in low or "file isn't here anymore" in low or "deleted" in low:
        raise HostExtractionError("El archivo de Dropbox no existe o fue eliminado.")
    if "password" in low or "sign in" in low or "iniciar sesión" in low or "request access" in low:
        raise HostExtractionError("Este enlace de Dropbox es privado o requiere contraseña/sesión.")
    if "disabled" in low or "too much traffic" in low:
        raise HostExtractionError("Dropbox deshabilitó temporalmente este enlace por exceso de tráfico.")
    raise HostExtractionError(f"Dropbox no entregó la descarga directa (HTTP {status or 'HTML'}).")


async def _resolve_krakenfiles(url: str, validate_url: UrlValidator) -> ResolvedHostDownload:
    status, final_url, body, cookies, used_curl = await _fetch_text_with_session(url, validate_url)
    cf_err = _detect_cf_challenge(status, body)
    if cf_err:
        raise HostExtractionError(cf_err)

    low = body.lower()
    if status == 404 or "page not found" in low or "file not found" in low:
        raise HostExtractionError("El archivo no existe o fue eliminado de Krakenfiles.")
    if status >= 400:
        raise HostExtractionError(f"Krakenfiles devolvió error HTTP {status}.")

    m_tok = re.search(
        r'<input[^>]+name=["\']token["\'][^>]+value=["\']([^"\']+)["\']',
        body,
        re.IGNORECASE,
    ) or re.search(
        r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']token["\']',
        body,
        re.IGNORECASE,
    )
    m_act = re.search(
        r'<form[^>]+id=["\']dl-form["\'][^>]+action=["\']([^"\']+)["\']',
        body,
        re.IGNORECASE,
    ) or re.search(
        r'action=["\']([^"\']*?/download/[^"\']+)["\']',
        body,
        re.IGNORECASE,
    )
    if not m_tok or not m_act:
        raise HostExtractionError("No se encontró el formulario de descarga en Krakenfiles.")

    token = html_lib.unescape(m_tok.group(1).strip())
    post_url = urljoin(final_url, html_lib.unescape(m_act.group(1).strip()))
    await validate_url(post_url)

    post_headers = {
        "User-Agent": CHROME_UA,
        "Referer": final_url,
        "Origin": "https://krakenfiles.com",
        "X-Requested-With": "XMLHttpRequest",
    }

    resp_text = ""
    post_status = 0
    if used_curl and await _ensure_impersonation_ready():
        async with _create_curl_session() as session:
            resp = await session.post(
                post_url,
                data={"token": token},
                headers=post_headers,
                cookies=cookies or None,
                allow_redirects=True,
            )
            post_status = resp.status_code
            resp_text = resp.text or ""
            if hasattr(session, "cookies"):
                cookies.update(dict(session.cookies.items()))
    else:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=post_headers, cookies=cookies) as session:
            async with session.post(post_url, data={"token": token}, allow_redirects=True) as resp:
                post_status = resp.status
                resp_text = await resp.text(errors="replace")

    if post_status >= 400:
        cf_post = _detect_cf_challenge(post_status, resp_text)
        if cf_post:
            raise HostExtractionError(cf_post)
        raise HostExtractionError(f"Krakenfiles rechazó la descarga (HTTP {post_status}).")

    try:
        payload = json.loads(resp_text)
    except json.JSONDecodeError as exc:
        raise HostExtractionError("Krakenfiles devolvió una respuesta inválida al generar el enlace.") from exc

    raw_dl = str(payload.get("url") or "").strip()
    if not raw_dl:
        raise HostExtractionError("Krakenfiles no entregó la URL directa del archivo.")
    if raw_dl.startswith("//"):
        raw_dl = "https:" + raw_dl
    direct_url = urljoin(final_url, raw_dl)
    await validate_url(direct_url)

    fname_page: str | None = None
    m_name = re.search(r'<div[^>]+class=["\'][^"\']*coin-name[^"\']*["\'][^>]*>.*?<h5[^>]*>([^<]+)</h5>', body, re.IGNORECASE | re.DOTALL)
    if m_name:
        fname_page = _clean_filename(m_name.group(1))

    dl_headers = {"Referer": final_url}
    fname_head, fsize = await _probe_direct_metadata(
        direct_url, validate_url, headers=dl_headers, cookies=cookies, use_curl_cffi=used_curl
    )
    return ResolvedHostDownload(
        provider="Krakenfiles",
        direct_url=direct_url,
        filename=fname_head or fname_page or _filename_from_cd(None, direct_url),
        file_size=fsize,
        headers=dl_headers,
        cookies=cookies,
        use_curl_cffi=used_curl,
    )


def _check_1fichier_errors(status: int, html_text: str) -> None:
    cf_err = _detect_cf_challenge(status, html_text)
    if cf_err:
        raise HostExtractionError(cf_err)
    low = html_text.lower()
    if status == 404 or "file could not be found" in low or "requested file has been deleted" in low or "fichier demandé n'existe pas" in low:
        raise HostExtractionError("El archivo no existe o fue eliminado de 1Fichier.")
    if 'name="pass"' in low or "password" in low and "protected" in low:
        raise HostExtractionError("Este archivo de 1Fichier está protegido con contraseña.")
    m_wait = re.search(r"you must wait\s+(\d+\s*(?:minutes?|seconds?|hours?))", html_text, re.IGNORECASE) or re.search(
        r"vous devez attendre\s+(\d+\s*(?:minutes?|secondes?|heures?))", html_text, re.IGNORECASE
    )
    if m_wait or "free download is not possible" in low or "without waiting" in low and "warning" in low:
        wait_txt = f" ({m_wait.group(1)})" if m_wait else ""
        raise HostExtractionError(
            f"1Fichier aplicó tiempo de espera por descarga gratuita compartida en esta IP{wait_txt}. Intenta más tarde o usa otro servidor."
        )


async def _resolve_1fichier(url: str, validate_url: UrlValidator) -> ResolvedHostDownload:
    status, final_url, body, cookies, used_curl = await _fetch_text_with_session(url, validate_url)
    _check_1fichier_errors(status, body)
    if status >= 400:
        raise HostExtractionError(f"1Fichier devolvió error HTTP {status}.")

    # ¿Ya trae enlace directo en la página?
    m_direct = re.search(
        r'<a[^>]+class=["\'][^"\']*ok[^"\']*btn-general[^"\']*["\'][^>]+href=["\'](https?://[^"\']+)["\']',
        body,
        re.IGNORECASE,
    )
    dl_html = body
    post_url = final_url
    if not m_direct:
        form_data: dict[str, str] = {"dl_no_ssl": "on", "dlinline": "on"}
        m_adz = re.search(r'name=["\']adz["\'][^>]+value=["\']([^"\']*)["\']', body, re.IGNORECASE)
        if m_adz:
            form_data["adz"] = html_lib.unescape(m_adz.group(1))
        m_act = re.search(r'<form[^>]+class=["\'][^"\']*alc[^"\']*["\'][^>]+action=["\']([^"\']+)["\']', body, re.IGNORECASE)
        if m_act:
            post_url = urljoin(final_url, html_lib.unescape(m_act.group(1)))
            await validate_url(post_url)

        post_headers = {"User-Agent": CHROME_UA, "Referer": final_url, "Origin": "https://1fichier.com"}
        post_status = 0
        if used_curl and await _ensure_impersonation_ready():
            async with _create_curl_session() as session:
                resp = await session.post(
                    post_url,
                    data=form_data,
                    headers=post_headers,
                    cookies=cookies or None,
                    allow_redirects=True,
                )
                post_status = resp.status_code
                dl_html = resp.text or ""
                if hasattr(session, "cookies"):
                    cookies.update(dict(session.cookies.items()))
        else:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout, headers=post_headers, cookies=cookies) as session:
                async with session.post(post_url, data=form_data, allow_redirects=True) as resp:
                    post_status = resp.status
                    dl_html = await resp.text(errors="replace")

        _check_1fichier_errors(post_status, dl_html)
        m_direct = re.search(
            r'<a[^>]+class=["\'][^"\']*ok[^"\']*btn-general[^"\']*["\'][^>]+href=["\'](https?://[^"\']+)["\']',
            dl_html,
            re.IGNORECASE,
        ) or re.search(
            r'<a[^>]+href=["\'](https?://[a-z0-9.-]+\.1fichier\.com/c[^"\']+)["\']',
            dl_html,
            re.IGNORECASE,
        )

    if not m_direct:
        raise HostExtractionError(
            "1Fichier no entregó el enlace de descarga directa (posible captcha o bloqueo temporal de IP)."
        )

    direct_url = html_lib.unescape(m_direct.group(1).strip())
    await validate_url(direct_url)
    dl_headers = {"Referer": post_url}
    fname_head, fsize = await _probe_direct_metadata(
        direct_url, validate_url, headers=dl_headers, cookies=cookies, use_curl_cffi=used_curl
    )
    return ResolvedHostDownload(
        provider="1Fichier",
        direct_url=direct_url,
        filename=fname_head or _filename_from_cd(None, direct_url),
        file_size=fsize,
        headers=dl_headers,
        cookies=cookies,
        use_curl_cffi=used_curl,
    )


async def _resolve_qiwi(url: str, validate_url: UrlValidator) -> ResolvedHostDownload:
    status, final_url, body, cookies, used_curl = await _fetch_text_with_session(url, validate_url)
    cf_err = _detect_cf_challenge(status, body)
    if cf_err:
        raise HostExtractionError(cf_err)
    low = body.lower()
    if status == 404 or "file not found" in low or "404" in re.findall(r"<title>\s*404", low):
        raise HostExtractionError("El archivo no existe o fue eliminado de Qiwi.")
    if status >= 400:
        raise HostExtractionError(f"Qiwi devolvió error HTTP {status}.")

    direct_url: str | None = None
    for pat in (
        r'(https?://spyderrock\.com/[A-Za-z0-9._-]+)',
        r'<a[^>]+href=["\'](https?://[^"\']+)["\'][^>]*download',
        r'<source[^>]+src=["\'](https?://[^"\']+)["\']',
    ):
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            direct_url = html_lib.unescape(m.group(1).strip())
            break

    if not direct_url:
        slug = Path(urlparse(final_url).path).name.strip()
        if slug:
            m_ext = re.search(r'<h1[^>]*>([^<]+)</h1>', body, re.IGNORECASE)
            ext_guess = Path(m_ext.group(1).strip()).suffix if m_ext else ""
            if ext_guess:
                direct_url = f"https://spyderrock.com/{slug}{ext_guess}"

    if not direct_url:
        raise HostExtractionError("No se pudo extraer el enlace directo desde la página de Qiwi.")

    await validate_url(direct_url)
    m_title = re.search(r'<h1[^>]*>([^<]+)</h1>', body, re.IGNORECASE) or re.search(
        r'<title>\s*([^<]+?)\s*(?:-|\|)?\s*qiwi\.gg\s*</title>', body, re.IGNORECASE
    )
    fname_page = _clean_filename(m_title.group(1)) if m_title else None

    dl_headers = {"Referer": final_url}
    fname_head, fsize = await _probe_direct_metadata(
        direct_url, validate_url, headers=dl_headers, cookies=cookies, use_curl_cffi=used_curl
    )
    return ResolvedHostDownload(
        provider="Qiwi",
        direct_url=direct_url,
        filename=fname_head or fname_page or _filename_from_cd(None, direct_url),
        file_size=fsize,
        headers=dl_headers,
        cookies=cookies,
        use_curl_cffi=used_curl,
    )


def _decrypt_bunkr_vs_url(encrypted_b64: str, timestamp: int | float) -> str | None:
    try:
        raw = base64.b64decode(encrypted_b64)
        key = f"SECRET_KEY_{int(timestamp) // 3600}".encode("utf-8")
        out = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
        decoded = out.decode("utf-8", errors="strict").strip()
        if decoded.startswith(("http://", "https://")):
            return decoded
    except Exception:
        return None
    return None


async def _resolve_bunkr(url: str, validate_url: UrlValidator) -> ResolvedHostDownload:
    parsed = urlparse(url)
    if parsed.path.lower().startswith("/a/"):
        raise HostExtractionError(
            "Los álbumes completos de Bunkr (/a/...) no están soportados; envía el enlace individual del archivo."
        )

    status, final_url, body, cookies, used_curl = await _fetch_text_with_session(url, validate_url)
    cf_err = _detect_cf_challenge(status, body)
    if cf_err:
        raise HostExtractionError(cf_err)
    if status == 404 or "file not found" in body.lower():
        raise HostExtractionError("El archivo no existe o fue eliminado de Bunkr.")
    if status >= 400:
        raise HostExtractionError(f"Bunkr devolvió error HTTP {status}.")

    direct_url: str | None = None
    # 1. Intentar enlace directo en HTML / video source
    for pat in (
        r'<a[^>]+id=["\']czmDownloadz["\'][^>]+href=["\'](https?://[^"\']+)["\']',
        r'<a[^>]+href=["\'](https?://(?:get\.bunkr[^"\']+|[^"\']*bunkr[^"\']*/file/[^"\']+))["\']',
        r'<source[^>]+src=["\'](https?://[^"\']+)["\']',
    ):
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            direct_url = html_lib.unescape(m.group(1).strip())
            break

    # 2. Fallback moderno: endpoint /api/vs con cifrado XOR por hora
    if not direct_url:
        slug_match = re.search(r"/(?:f|v|i|d)/([A-Za-z0-9_-]+)", urlparse(final_url).path)
        if slug_match:
            slug = slug_match.group(1)
            api_url = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}/api/vs"
            await validate_url(api_url)
            api_headers = {
                "User-Agent": CHROME_UA,
                "Referer": final_url,
                "Content-Type": "application/json",
            }
            api_text = ""
            if used_curl and await _ensure_impersonation_ready():
                async with _create_curl_session() as session:
                    resp = await session.post(
                        api_url,
                        json={"slug": slug},
                        headers=api_headers,
                        cookies=cookies or None,
                    )
                    if resp.status_code < 400:
                        api_text = resp.text or ""
            else:
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout, headers=api_headers, cookies=cookies) as session:
                    async with session.post(api_url, json={"slug": slug}) as resp:
                        if resp.status < 400:
                            api_text = await resp.text(errors="replace")
            if api_text:
                with contextlib.suppress(Exception):
                    vs_data = json.loads(api_text)
                    enc_url = str(vs_data.get("url") or "")
                    ts_val = vs_data.get("timestamp")
                    if enc_url and isinstance(ts_val, (int, float)):
                        direct_url = _decrypt_bunkr_vs_url(enc_url, ts_val)
                    elif enc_url.startswith(("http://", "https://")):
                        direct_url = enc_url

    if not direct_url:
        raise HostExtractionError(
            "No se pudo extraer el enlace directo desde Bunkr (usa el enlace individual /f/... o /v/...)."
        )

    await validate_url(direct_url)
    m_name = re.search(r'<h1[^>]*>([^<]+)</h1>', body, re.IGNORECASE) or re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        body,
        re.IGNORECASE,
    )
    fname_page = _clean_filename(m_name.group(1)) if m_name else None

    dl_headers = {"Referer": final_url}
    fname_head, fsize = await _probe_direct_metadata(
        direct_url, validate_url, headers=dl_headers, cookies=cookies, use_curl_cffi=used_curl
    )
    return ResolvedHostDownload(
        provider="Bunkr",
        direct_url=direct_url,
        filename=fname_head or fname_page or _filename_from_cd(None, direct_url),
        file_size=fsize,
        headers=dl_headers,
        cookies=cookies,
        use_curl_cffi=used_curl,
    )


async def resolve_host_download(
    url: str,
    validate_url: UrlValidator,
    gofile_token: str = "",
) -> ResolvedHostDownload | None:
    """Resuelve un enlace de servidor de archivos soportado hacia su URL de descarga directa."""
    host_kind = identify_file_host(url)
    if host_kind is None:
        return None

    logger.info("Resolviendo enlace de servidor '%s': %s", host_kind, url[:100])
    if host_kind == "mediafire":
        return await _resolve_mediafire(url, validate_url)
    if host_kind == "pixeldrain":
        return await _resolve_pixeldrain(url, validate_url)
    if host_kind == "gofile":
        return await _resolve_gofile(url, validate_url, gofile_token=gofile_token)
    if host_kind == "buzzheavier":
        return await _resolve_buzzheavier(url, validate_url)
    if host_kind == "gdrive":
        return await _resolve_gdrive(url, validate_url)
    if host_kind == "dropbox":
        return await _resolve_dropbox(url, validate_url)
    if host_kind == "krakenfiles":
        return await _resolve_krakenfiles(url, validate_url)
    if host_kind == "1fichier":
        return await _resolve_1fichier(url, validate_url)
    if host_kind == "qiwi":
        return await _resolve_qiwi(url, validate_url)
    if host_kind == "bunkr":
        return await _resolve_bunkr(url, validate_url)
    return None
