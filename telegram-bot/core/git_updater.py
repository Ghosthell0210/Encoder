"""Actualizador seguro desde GitHub para Pelican / Pterodactyl.

Funciona incluso si el servidor se subió inicialmente por ZIP (sin carpeta .git)
y evita que cambios locales de permisos bloqueen 'git pull'. Nunca toca archivos
no rastreados como .env, data/bot.db ni bin/ffmpeg.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

logger = logging.getLogger("hydrocompress.git_updater")


def _build_remote_url(repo_url: str, username: str, token: str) -> str:
    clean = repo_url.strip()
    if not clean.endswith(".git") and "github.com" in clean:
        clean = clean.rstrip("/") + ".git"
    if not token:
        return clean
    parsed = urlparse(clean)
    user = quote(username.strip() or "git", safe="")
    tok = quote(token.strip(), safe="")
    netloc = f"{user}:{tok}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


async def _run_git(cwd: Path, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode("utf-8", errors="replace").strip(),
        err.decode("utf-8", errors="replace").strip(),
    )


async def _sync_requirements(repo_root: Path) -> bool:
    req_file = repo_root / "requirements.txt"
    if not req_file.is_file():
        return True

    attempts = [
        [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "-r", str(req_file)],
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--quiet",
            "--break-system-packages",
            "-r",
            str(req_file),
        ],
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--quiet",
            "--user",
            "-r",
            str(req_file),
        ],
    ]
    for cmd in attempts:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode == 0:
                logger.info("Dependencias Python sincronizadas tras actualizar.")
                return True
            logger.debug("pip install falló (%s): %s", proc.returncode, err.decode("utf-8", errors="replace")[:200])
        except Exception as exc:
            logger.debug("Error ejecutando pip install: %s", exc)
    logger.warning("No se pudo ejecutar pip install -r requirements.txt automáticamente tras actualizar.")
    return False


async def pull_from_github(repo_root: Path) -> tuple[bool, str, bool]:
    """Sincroniza el código con GitHub.

    Devuelve (exito, mensaje_limpio, hubo_cambios).
    """
    if not shutil.which("git"):
        return False, "git no está instalado en este contenedor.", False

    repo_url = os.getenv("GIT_ADDRESS", "").strip()
    branch = os.getenv("BRANCH", "").strip() or "main"
    username = os.getenv("USERNAME", "").strip()
    token = os.getenv("ACCESS_TOKEN", "").strip()

    git_dir = repo_root / ".git"

    if not git_dir.exists():
        if not repo_url:
            return (
                False,
                "No existe carpeta .git ni variable GIT_ADDRESS en Startup.",
                False,
            )
        remote_url = _build_remote_url(repo_url, username, token)
        code, _, err = await _run_git(repo_root, "init")
        if code != 0:
            return False, f"No se pudo inicializar git: {err[:160]}", False
        await _run_git(repo_root, "remote", "remove", "origin")
        code, _, err = await _run_git(repo_root, "remote", "add", "origin", remote_url)
        if code != 0:
            return False, f"No se pudo configurar origin: {err[:160]}", False
    elif repo_url and token:
        remote_url = _build_remote_url(repo_url, username, token)
        await _run_git(repo_root, "remote", "set-url", "origin", remote_url)

    _, before_sha, _ = await _run_git(repo_root, "rev-parse", "HEAD")

    code, _, err = await _run_git(repo_root, "fetch", "--depth", "1", "origin", branch)
    if code != 0:
        # Ocultar cualquier credencial del mensaje de error
        safe_err = err.replace(token, "***") if token else err
        return False, f"Error descargando rama '{branch}' desde GitHub: {safe_err[:200]}", False

    code, _, err = await _run_git(repo_root, "reset", "--hard", "FETCH_HEAD")
    if code != 0:
        return False, f"Error aplicando actualización: {err[:200]}", False

    _, after_sha, _ = await _run_git(repo_root, "rev-parse", "HEAD")
    _, commit_msg, _ = await _run_git(repo_root, "log", "-1", "--pretty=%h %s")

    changed = bool(after_sha and after_sha != before_sha)
    if changed:
        await _sync_requirements(repo_root)
        return True, f"Actualizado a `{commit_msg}`.", True
    return True, f"Ya estabas en la última versión (`{commit_msg}`).", False


def restart_process() -> None:
    """Reinicia el proceso actual de Python conservando argumentos."""
    os.execv(sys.executable, [sys.executable, *sys.argv])
