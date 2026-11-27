"""Motor P2P BitTorrent rootless para enlaces magnet:? y archivos .torrent.

Usa `libtorrent` (con auto-instalación sin root si falta en el entorno) para:
  1. Resolver metadatos antes de descargar y validar que el tamaño total quepa en disco.
  2. Mostrar progreso en tiempo real (porcentaje, velocidad, pares/semillas y ETA).
  3. Cancelar instantáneamente o abortar ante torrents muertos sin semillas.
  4. Detener el seeding inmediatamente al completar (seed_time = 0).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from core.ffmpeg_bootstrap import ensure_libtorrent, has_libtorrent
from core.ffmpeg_engine import JobCancelledError

logger = logging.getLogger(__name__)

MAGNET_REGEX = re.compile(r"^magnet:\?xt=urn:[a-z0-9]+:[a-z0-9]{16,}\S*$", re.IGNORECASE)

TorrentProgressCb = Callable[[int, int, float, int, int], Awaitable[None]]
TorrentStageCb = Callable[[str, str, int], Awaitable[None]]


@dataclass(slots=True)
class TorrentDownloadResult:
    root_dir: Path
    files: list[Path]
    name: str
    total_bytes: int


def is_magnet_link(text: str) -> bool:
    return bool(MAGNET_REGEX.match((text or "").strip()))


def is_torrent_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    return Path(unquote(parsed.path)).suffix.lower() == ".torrent"


def _extract_magnet_display_name(magnet_uri: str) -> str:
    m = re.search(r"[?&]dn=([^&]+)", magnet_uri, re.IGNORECASE)
    if m:
        name = unquote(m.group(1)).replace("+", " ").strip()
        if name:
            return name[:80]
    return "descarga_magnet"


async def download_torrent_or_magnet(
    source: str | Path,
    dest_dir: Path,
    get_free_bytes: Callable[[], int],
    cancel_event: asyncio.Event,
    on_progress: TorrentProgressCb | None = None,
    on_stage: TorrentStageCb | None = None,
    metadata_timeout_sec: float = 90.0,
    stall_timeout_sec: float = 150.0,
) -> TorrentDownloadResult:
    """Descarga un enlace magnet:? o archivo .torrent sin seeding posterior."""
    if not has_libtorrent():
        ok = await asyncio.to_thread(ensure_libtorrent)
        if not ok:
            raise RuntimeError(
                "El motor BitTorrent (`libtorrent`) no está disponible en este entorno."
            )

    import libtorrent as lt

    dest_root = dest_dir.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)

    settings = {
        "user_agent": "libtorrent/2.0",
        "listen_interfaces": "0.0.0.0:0",
        "enable_dht": True,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
        "upload_rate_limit": 64 * 1024,
        "connections_limit": 120,
        "active_downloads": 2,
        "active_seeds": 0,
        "dht_bootstrap_nodes": (
            "router.bittorrent.com:6881,"
            "router.utorrent.com:6881,"
            "dht.transmissionbt.com:6881,"
            "dht.aelitis.com:6881"
        ),
    }

    session = lt.session(settings)
    handle = None
    is_magnet = isinstance(source, str) and is_magnet_link(source)

    try:
        if isinstance(source, Path):
            ti = lt.torrent_info(str(source))
            params = lt.add_torrent_params()
            params.ti = ti
            params.save_path = str(dest_root)
            params.storage_mode = lt.storage_mode_t.storage_mode_sparse
            handle = session.add_torrent(params)
            display_name = ti.name() or source.stem
        else:
            params = lt.parse_magnet_uri(str(source).strip())
            params.save_path = str(dest_root)
            params.storage_mode = lt.storage_mode_t.storage_mode_sparse
            handle = session.add_torrent(params)
            display_name = _extract_magnet_display_name(str(source))

        if on_stage:
            await on_stage("metadata" if is_magnet else "preparing", display_name, 0)

        meta_start = time.monotonic()
        while not handle.status().has_metadata:
            if cancel_event.is_set():
                raise JobCancelledError("Descarga torrent cancelada por el usuario.")
            if (time.monotonic() - meta_start) > metadata_timeout_sec:
                raise RuntimeError(
                    "Tiempo agotado esperando metadatos del magnet (sin semillas o trackers accesibles)."
                )
            await asyncio.sleep(0.5)

        t_info = handle.torrent_file()
        torrent_name = (t_info.name() if t_info else display_name) or display_name
        total_size = int(t_info.total_size()) if t_info else int(handle.status().total_wanted)

        free_bytes = await asyncio.to_thread(get_free_bytes)
        safe_limit = int(free_bytes * 0.85)
        if total_size > 0 and total_size > safe_limit:
            raise RuntimeError(
                f"El torrent '{torrent_name[:40]}' pesa {total_size / (1024**3):.2f} GiB "
                f"y excede el margen de disco seguro ({safe_limit / (1024**3):.2f} GiB libres)."
            )

        if on_stage:
            await on_stage("downloading", torrent_name, total_size)

        last_progress_at = time.monotonic()
        last_done = 0
        last_cb = 0.0

        while True:
            if cancel_event.is_set():
                raise JobCancelledError("Descarga torrent cancelada por el usuario.")

            st = handle.status()
            done = int(st.total_wanted_done)
            wanted = int(st.total_wanted) or total_size
            dl_rate = float(st.download_rate)
            peers = int(st.num_peers)
            seeds = int(st.num_seeds)

            if done > last_done or dl_rate >= 2048:
                last_done = done
                last_progress_at = time.monotonic()
            elif (time.monotonic() - last_progress_at) > stall_timeout_sec and peers == 0 and seeds == 0:
                raise RuntimeError(
                    "La descarga P2P se estancó sin pares ni semillas disponibles."
                )

            if wanted > safe_limit:
                raise RuntimeError(
                    "El torrent superó el margen de disco seguro disponible en el servidor."
                )

            now = time.monotonic()
            if on_progress and (st.is_seeding or (now - last_cb) >= 0.8):
                last_cb = now
                await on_progress(done, wanted, dl_rate, peers, seeds)

            if st.is_seeding or (wanted > 0 and done >= wanted and st.state == lt.torrent_status.seeding):
                break

            await asyncio.sleep(0.6)

        with contextlib.suppress(Exception):
            handle.pause()
            session.remove_torrent(handle)
        handle = None

        downloaded_files = [
            p for p in sorted(dest_root.rglob("*"), key=lambda x: str(x).lower())
            if p.is_file() and p.stat().st_size > 0 and not p.name.endswith(".!qB")
        ]
        if not downloaded_files:
            raise RuntimeError("El torrent finalizó pero no generó archivos descargados válidos.")

        actual_total = sum(f.stat().st_size for f in downloaded_files)
        return TorrentDownloadResult(
            root_dir=dest_root,
            files=downloaded_files,
            name=torrent_name,
            total_bytes=actual_total,
        )

    except Exception:
        if handle is not None:
            with contextlib.suppress(Exception):
                session.remove_torrent(handle, lt.session.delete_files)
        with contextlib.suppress(Exception):
            shutil.rmtree(dest_root, ignore_errors=True)
        raise
    finally:
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.pause()
                session.remove_torrent(handle)
        with contextlib.suppress(Exception):
            del session
