"""Explorador seguro de workspace (/ls) y descompresión rootless (.zip, .rar, .7z, .tar.*).

Incluye:
  - Navegación sandboxed dentro de WORKSPACE_DIR (bloqueo estricto de path traversal).
  - Descompresión automática de .zip, .tar, .tar.gz/.tgz, .tar.bz2, .tar.xz, .7z y .rar
    (RAR4 y RAR5) mediante librerías estándar + binario estático `7zz` sin root.
  - Detección de archivos protegidos con contraseña y validación previa de espacio en disco
    (protección anti-zip-bomb).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.ffmpeg_engine import JobCancelledError
from core.ffmpeg_bootstrap import ensure_7zz

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".ts", ".wmv"}
AUDIO_EXTENSIONS = {".mp3", ".opus", ".m4a", ".ogg", ".oga", ".aac", ".flac", ".wav"}
ARCHIVE_EXTENSIONS = {
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".tbz2",
    ".xz",
    ".txz",
}
MULTI_TAR_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")


class ArchivePasswordRequiredError(RuntimeError):
    """El archivo comprimido requiere contraseña para extraerse."""


class ArchiveWrongPasswordError(RuntimeError):
    """La contraseña proporcionada para el archivo comprimido es incorrecta."""


EntryKind = Literal["dir", "archive", "video", "audio", "other"]


@dataclass(slots=True)
class WorkspaceEntry:
    name: str
    rel_path: str
    is_dir: bool
    size_bytes: int
    kind: EntryKind
    mtime: float


@dataclass(slots=True)
class ArchiveExtractResult:
    dest_dir: Path
    extracted_files: list[Path]
    total_bytes: int


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def is_archive_file(path: Path) -> bool:
    low = path.name.lower()
    if low.endswith(MULTI_TAR_SUFFIXES):
        return True
    return path.suffix.lower() in ARCHIVE_EXTENSIONS


def _is_tar_archive(path: Path) -> bool:
    low = path.name.lower()
    return low.endswith(MULTI_TAR_SUFFIXES) or path.suffix.lower() in {
        ".tar",
        ".tgz",
        ".tbz2",
        ".txz",
    }


def strip_archive_suffix(filename: str) -> str:
    low = filename.lower()
    for multi in MULTI_TAR_SUFFIXES:
        if low.endswith(multi):
            return filename[: -len(multi)] or "extraido"
    stem = Path(filename).stem
    # Manejar .part1.rar / .part01.rar
    stem_clean = re.sub(r"\.part\d+$", "", stem, flags=re.IGNORECASE)
    return stem_clean or stem or "extraido"


def sanitize_rel_path(raw: str | None) -> str:
    if not raw:
        return ""
    norm = raw.replace("\\", "/").strip("/")
    parts = [p for p in norm.split("/") if p and p not in {".", ".."}]
    return "/".join(parts)


def resolve_workspace_path(workspace_root: Path, rel_path: str = "") -> Path:
    root = workspace_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    clean_rel = sanitize_rel_path(rel_path)
    candidate = (root / clean_rel).resolve() if clean_rel else root
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Ruta fuera del espacio de trabajo permitido.") from exc
    return candidate


def _fast_tree_bytes(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def classify_entry(path: Path) -> EntryKind:
    if path.is_dir():
        return "dir"
    if is_archive_file(path):
        return "archive"
    if is_video_file(path):
        return "video"
    if is_audio_file(path):
        return "audio"
    return "other"


def list_workspace_dir(
    workspace_root: Path,
    rel_path: str = "",
) -> tuple[str, list[WorkspaceEntry]]:
    root = workspace_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = resolve_workspace_path(root, rel_path)
    if not target.exists() or not target.is_dir():
        target = root
    norm_rel = "" if target == root else target.relative_to(root).as_posix()

    entries: list[WorkspaceEntry] = []
    try:
        with os.scandir(target) as it:
            for item in it:
                if item.name.startswith("."):
                    continue
                p = Path(item.path)
                try:
                    st = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                is_dir = item.is_dir(follow_symlinks=False)
                size_b = _fast_tree_bytes(p) if is_dir else st.st_size
                item_rel = p.relative_to(root).as_posix()
                entries.append(
                    WorkspaceEntry(
                        name=item.name,
                        rel_path=item_rel,
                        is_dir=is_dir,
                        size_bytes=size_b,
                        kind="dir" if is_dir else classify_entry(p),
                        mtime=st.st_mtime,
                    )
                )
    except OSError:
        return norm_rel, []

    entries.sort(key=lambda e: (0 if e.is_dir else 1, e.name.lower()))
    return norm_rel, entries


def unique_dest_dir(parent_dir: Path, base_name: str) -> Path:
    clean_base = "".join(
        c if c.isalnum() or c in " -_.()[]" else "_" for c in base_name
    ).strip(" .") or "extraido"
    candidate = parent_dir / clean_base[:80]
    if not candidate.exists():
        return candidate
    for idx in range(1, 100):
        cand = parent_dir / f"{clean_base[:72]}_{idx}"
        if not cand.exists():
            return cand
    return parent_dir / f"{clean_base[:64]}_{os.getpid()}"


def _check_safe_member_target(dest_root: Path, member_name: str) -> Path:
    clean = member_name.replace("\\", "/").lstrip("/")
    target = (dest_root / clean).resolve()
    try:
        target.relative_to(dest_root.resolve())
    except ValueError as exc:
        raise RuntimeError(
            f"Archivo comprimido rechazado por ruta peligrosa: {member_name[:80]}"
        ) from exc
    return target


def _collect_extracted_files(dest_dir: Path) -> list[Path]:
    files: list[Path] = []
    for p in dest_dir.rglob("*"):
        if p.is_file():
            files.append(p)
    files.sort(key=lambda x: str(x).lower())
    return files


def _extract_tar_sync(
    archive_path: Path,
    dest_dir: Path,
    max_allowed_bytes: int,
) -> ArchiveExtractResult:
    dest_root = dest_dir.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="r:*") as tf:
        members = tf.getmembers()
        safe_members: list[tarfile.TarInfo] = []
        total_declared = 0
        for m in members:
            if m.issym() or m.islnk() or m.ischr() or m.isblk() or m.isfifo():
                continue
            _check_safe_member_target(dest_root, m.name)
            if m.isfile():
                total_declared += max(0, int(m.size))
                if total_declared > max_allowed_bytes:
                    raise RuntimeError(
                        f"El contenido descomprimido ({total_declared / (1024**3):.2f} GiB) excede el disco libre seguro."
                    )
            safe_members.append(m)

        extracted_bytes = 0
        for m in safe_members:
            target = _check_safe_member_target(dest_root, m.name)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    continue
                with src, target.open("wb") as out:
                    while True:
                        chunk = src.read(512 * 1024)
                        if not chunk:
                            break
                        extracted_bytes += len(chunk)
                        if extracted_bytes > max_allowed_bytes:
                            raise RuntimeError(
                                "La descompresión excedió el espacio libre seguro del servidor."
                            )
                        out.write(chunk)

    files = _collect_extracted_files(dest_root)
    return ArchiveExtractResult(
        dest_dir=dest_root,
        extracted_files=files,
        total_bytes=sum(f.stat().st_size for f in files),
    )


def _extract_zip_sync(
    archive_path: Path,
    dest_dir: Path,
    max_allowed_bytes: int,
    password: str | None = None,
) -> ArchiveExtractResult:
    dest_root = dest_dir.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    pwd_bytes = password.encode("utf-8") if password else None

    with zipfile.ZipFile(archive_path, mode="r") as zf:
        infos = zf.infolist()
        total_declared = sum(max(0, info.file_size) for info in infos if not info.is_dir())
        if total_declared > max_allowed_bytes:
            raise RuntimeError(
                f"El contenido descomprimido ({total_declared / (1024**3):.2f} GiB) excede el disco libre seguro."
            )
        has_encrypted = any((info.flag_bits & 0x1) != 0 for info in infos if not info.is_dir())
        if has_encrypted and not password:
            raise ArchivePasswordRequiredError(
                "Este archivo ZIP está protegido con contraseña."
            )

        extracted_bytes = 0
        for info in infos:
            target = _check_safe_member_target(dest_root, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info, mode="r", pwd=pwd_bytes) as src, target.open("wb") as out:
                    while True:
                        chunk = src.read(512 * 1024)
                        if not chunk:
                            break
                        extracted_bytes += len(chunk)
                        if extracted_bytes > max_allowed_bytes:
                            raise RuntimeError(
                                "La descompresión excedió el espacio libre seguro del servidor."
                            )
                        out.write(chunk)
            except RuntimeError as exc:
                low = str(exc).lower()
                if "bad password" in low or "password required" in low:
                    if password:
                        raise ArchiveWrongPasswordError(
                            "Contraseña incorrecta para este archivo ZIP."
                        ) from exc
                    raise ArchivePasswordRequiredError(
                        "Este archivo ZIP está protegido con contraseña."
                    ) from exc
                raise

    files = _collect_extracted_files(dest_root)
    return ArchiveExtractResult(
        dest_dir=dest_root,
        extracted_files=files,
        total_bytes=sum(f.stat().st_size for f in files),
    )


async def _run_7zz_probe(
    sevenzip_exe: str,
    archive_path: Path,
    password: str | None,
) -> tuple[int | None, bool]:
    """Devuelve (tamaño_descomprimido_estimado, esta_cifrado)."""
    pwd_arg = f"-p{password}" if password else "-p-"
    cmd = [sevenzip_exe, "l", "-slt", "-y", pwd_arg, "--", str(archive_path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=45.0)
    out = (stdout_b or b"").decode("utf-8", errors="replace")
    err = (stderr_b or b"").decode("utf-8", errors="replace")
    combined = (out + "\n" + err).lower()

    if "wrong password" in combined:
        if password:
            raise ArchiveWrongPasswordError("Contraseña incorrecta para este archivo.")
        raise ArchivePasswordRequiredError("Este archivo está protegido con contraseña.")
    if "can not open encrypted archive" in combined or "enter password" in combined:
        raise ArchivePasswordRequiredError("Este archivo está protegido con contraseña.")
    if proc.returncode != 0 and "size =" not in combined:
        if "password" in combined or "encrypted" in combined:
            if password:
                raise ArchiveWrongPasswordError("Contraseña incorrecta para este archivo.")
            raise ArchivePasswordRequiredError("Este archivo está protegido con contraseña.")
        raise RuntimeError(
            f"No se pudo abrir el archivo comprimido ({(err or out).strip()[:180] or 'archivo dañado'})."
        )

    encrypted = "encrypted = +" in combined
    if encrypted and not password:
        raise ArchivePasswordRequiredError("Este archivo está protegido con contraseña.")

    total_size = 0
    for m in re.finditer(r"^Size\s*=\s*(\d+)", out, flags=re.MULTILINE):
        total_size += int(m.group(1))
    return (total_size if total_size > 0 else None), encrypted


async def _extract_with_7zz(
    sevenzip_exe: str,
    archive_path: Path,
    dest_dir: Path,
    max_allowed_bytes: int,
    password: str | None = None,
    cancel_event: asyncio.Event | None = None,
) -> ArchiveExtractResult:
    est_bytes, _ = await _run_7zz_probe(sevenzip_exe, archive_path, password)
    if est_bytes is not None and est_bytes > max_allowed_bytes:
        raise RuntimeError(
            f"El contenido descomprimido ({est_bytes / (1024**3):.2f} GiB) excede el disco libre seguro."
        )

    dest_root = dest_dir.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    pwd_arg = f"-p{password}" if password else "-p-"
    cmd = [sevenzip_exe, "x", "-y", pwd_arg, f"-o{dest_root}", "--", str(archive_path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        while proc.returncode is None:
            if cancel_event and cancel_event.is_set():
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                raise JobCancelledError("Descompresión cancelada.")
            current_bytes = _fast_tree_bytes(dest_root)
            if current_bytes > max_allowed_bytes:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                raise RuntimeError("La descompresión excedió el espacio libre seguro del servidor.")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=0.6)

        stdout_b, stderr_b = await proc.communicate()
        combined = ((stdout_b or b"") + b"\n" + (stderr_b or b"")).decode("utf-8", errors="replace")
        low = combined.lower()
        if "wrong password" in low:
            if password:
                raise ArchiveWrongPasswordError("Contraseña incorrecta para este archivo.")
            raise ArchivePasswordRequiredError("Este archivo está protegido con contraseña.")
        if "can not open encrypted archive" in low or "enter password" in low or ("data error in encrypted file" in low):
            if password:
                raise ArchiveWrongPasswordError("Contraseña incorrecta para este archivo.")
            raise ArchivePasswordRequiredError("Este archivo está protegido con contraseña.")
        if proc.returncode != 0:
            err_line = combined.strip().splitlines()[-1] if combined.strip() else f"código {proc.returncode}"
            raise RuntimeError(f"Error al descomprimir con 7zz: {err_line[:180]}")

        files = _collect_extracted_files(dest_root)
        # Limpiar cualquier posible symlink creado por archivos malformados
        safe_files: list[Path] = []
        for f in files:
            if f.is_symlink():
                f.unlink(missing_ok=True)
                continue
            try:
                f.resolve().relative_to(dest_root)
                safe_files.append(f)
            except ValueError:
                f.unlink(missing_ok=True)

        return ArchiveExtractResult(
            dest_dir=dest_root,
            extracted_files=safe_files,
            total_bytes=sum(f.stat().st_size for f in safe_files),
        )
    except Exception:
        shutil.rmtree(dest_root, ignore_errors=True)
        raise


async def extract_archive(
    archive_path: Path,
    dest_dir: Path,
    max_free_bytes: int,
    bin_dir: Path,
    password: str | None = None,
    cancel_event: asyncio.Event | None = None,
) -> ArchiveExtractResult:
    """Descomprime un archivo (.zip, .rar, .7z, .tar.*) de forma segura y sin root."""
    if not archive_path.is_file():
        raise FileNotFoundError(f"No se encontró el archivo: {archive_path.name}")

    max_allowed = max(50 * 1024 * 1024, int(max_free_bytes * 0.85))
    suffix = archive_path.suffix.lower()

    if _is_tar_archive(archive_path):
        try:
            return await asyncio.to_thread(_extract_tar_sync, archive_path, dest_dir, max_allowed)
        except Exception:
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise

    if suffix == ".zip":
        try:
            return await asyncio.to_thread(
                _extract_zip_sync, archive_path, dest_dir, max_allowed, password
            )
        except NotImplementedError:
            # ZIP con AES-256 o método moderno que zipfile stdlib no soporta -> usar 7zz
            shutil.rmtree(dest_dir, ignore_errors=True)
        except Exception:
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise

    sevenzip_exe = await asyncio.to_thread(ensure_7zz, bin_dir)
    if not sevenzip_exe:
        raise RuntimeError(
            "No está disponible el extractor `7zz` para archivos .rar / .7z en este sistema."
        )
    return await _extract_with_7zz(
        sevenzip_exe=sevenzip_exe,
        archive_path=archive_path,
        dest_dir=dest_dir,
        max_allowed_bytes=max_allowed,
        password=password,
        cancel_event=cancel_event,
    )
