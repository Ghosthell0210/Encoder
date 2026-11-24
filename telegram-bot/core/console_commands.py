"""Comandos interactivos desde la consola de Pelican / Pterodactyl (stdin).

Permite escribir directamente en la barra de consola de panel.plugged.host:
  - login   → Inicia sesión paso a paso con tu cuenta de Telegram (teléfono, código, 2FA)
  - logout  → Desconecta y borra la sesión de la cuenta de Telegram
  - status  → Muestra cuenta vinculada y trabajos activos/en cola
  - help    → Lista los comandos de consola
"""

from __future__ import annotations

import asyncio
import logging
import sys

from core.database import Database
from core.queue_manager import QueueManager
from core.user_session import UserSessionManager

logger = logging.getLogger("hydrocompress.console")


def _print_console(msg: str) -> None:
    print(msg, flush=True)


async def console_command_loop(
    db: Database,
    queue_mgr: QueueManager,
    user_session: UserSessionManager,
) -> None:
    """Lee líneas de stdin en segundo plano para controlar login desde la consola."""
    _print_console(
        "\n[CONSOLA] Comandos disponibles: login | logout | status | help\n"
        "[CONSOLA] Escribe 'login' y pulsa Enter para iniciar sesión con tu cuenta de Telegram.\n"
    )

    console_login_active = False

    while True:
        try:
            raw_line = await asyncio.to_thread(sys.stdin.readline)
        except asyncio.CancelledError:
            break
        except Exception:
            break

        # EOF real (stdin cerrado)
        if raw_line == "":
            break

        text = raw_line.strip()
        if not text:
            continue

        # ── Si hay un proceso de login activo en la consola ───────────────────
        if console_login_active and user_session.login_in_progress:
            if text.lower() in {"cancel", "cancelar", "salir", "exit"}:
                await user_session.cancel_login()
                console_login_active = False
                _print_console("[LOGIN] Inicio de sesión cancelado.")
                continue

            stage = user_session.current_stage
            try:
                if stage == "phone":
                    await user_session.submit_phone(text)
                    _print_console(
                        "[LOGIN] Código enviado a tu app de Telegram.\n"
                        "[LOGIN] Escribe el código recibido (ej. 12345) o 'cancelar':"
                    )
                elif stage == "code":
                    done, _msg, session_str = await user_session.submit_code(text)
                    if done and session_str:
                        await db.set_setting("user_session_string", session_str)
                        console_login_active = False
                        _print_console(
                            f"[LOGIN] ✅ Sesión iniciada correctamente: {user_session.status_label()}\n"
                            "[LOGIN] Sesión guardada en base de datos local (data/bot.db)."
                        )
                    else:
                        _print_console(
                            "[LOGIN] Tu cuenta tiene verificación en dos pasos (2FA).\n"
                            "[LOGIN] Escribe tu contraseña 2FA o 'cancelar':"
                        )
                elif stage == "password":
                    _msg, session_str = await user_session.submit_password(text)
                    await db.set_setting("user_session_string", session_str)
                    console_login_active = False
                    _print_console(
                        f"[LOGIN] ✅ Sesión 2FA iniciada correctamente: {user_session.status_label()}\n"
                        "[LOGIN] Sesión guardada en base de datos local (data/bot.db)."
                    )
            except Exception as exc:
                _print_console(f"[LOGIN] ⚠️ Error: {exc}")
                if not user_session.login_in_progress:
                    console_login_active = False
            continue

        # ── Comandos normales de consola ──────────────────────────────────────
        cmd = text.lower().lstrip("/")

        if cmd == "login":
            await user_session.begin_login()
            console_login_active = True
            _print_console(
                "\n[LOGIN] ═══ Inicio de sesión de cuenta de Telegram ═══\n"
                "[LOGIN] Escribe tu número de teléfono con código de país (ej. +5215512345678)\n"
                "[LOGIN] (Escribe 'cancelar' para abortar):"
            )

        elif cmd == "logout":
            await user_session.cancel_login()
            await user_session.stop()
            await db.set_setting("user_session_string", "")
            console_login_active = False
            _print_console("[CONSOLA] 🔌 Cuenta de Telegram desconectada y sesión borrada.")

        elif cmd == "status":
            active, pending = queue_mgr.get_queue_snapshot()
            _print_console(
                f"[ESTADO] Cuenta Telegram: {user_session.status_label()} | "
                f"Cola: {len(active)}/{queue_mgr.concurrency_limit} activos, {len(pending)} en espera | "
                f"Hilos/trabajo: {queue_mgr.threads_per_job()}"
            )

        elif cmd == "help":
            _print_console(
                "[CONSOLA] Comandos:\n"
                "  login   - Iniciar sesión con tu cuenta de Telegram (teléfono + código + 2FA)\n"
                "  logout  - Cerrar y borrar la sesión de la cuenta de Telegram\n"
                "  status  - Ver estado de la cuenta vinculada y la cola\n"
                "  help    - Mostrar esta ayuda"
            )

        else:
            _print_console(f"[CONSOLA] Comando no reconocido: '{text}'. Escribe 'login', 'logout', 'status' o 'help'.")
