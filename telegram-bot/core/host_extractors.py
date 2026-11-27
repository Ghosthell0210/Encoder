"""Extractores nativos para servidores de archivos y bypass TLS (curl-cffi).

Soporta:
  - MediaFire (mediafire.com)
  - Pixeldrain (pixeldrain.com / pixeldrain.net)
  - GoFile (gofile.io)
  - Buzzheavier (buzzheavier.com / bzzhr.co)
  - Google Drive público (drive.google.com / drive.usercontent.google.com)

Usa impersonación TLS de Chrome vía `curl-cffi` cuando está disponible (con
instalación automática sin root) para evitar bloqueos 403 por huella TLS de
Cloudflare, y reporta errores legibles cuando existe Captcha/Turnstile JS activo,
contraseña o archivo privado/eliminado.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html as html_lib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlparse

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


async def _resolve_mediafire(url: str, validate_url: UrlValidator) -> ResolvedHostDownload:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("download") and "." in Path(unquote(parsed.path)).name:
        fname, fsize = await _probe_direct_metadata(url, validate_url, use_curl_cffi=True)
        return ResolvedHostDownload(
            provider="MediaFire",
            direct_url=url,
            filename=fname or _clean_filename(Path(unquote(parsed.path)).name),
            file_size=fsize,
            use_curl_cffi=True,
        )

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
    return None
