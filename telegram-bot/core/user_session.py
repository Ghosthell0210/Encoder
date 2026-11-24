"""Gestión de sesión de cuenta de Telegram (User Client opcional).

Permite iniciar sesión con tu cuenta de Telegram (vía /login en el bot,
python3 login_account.py en consola o USER_SESSION_STRING en .env):
  - Si la cuenta tiene Telegram Premium: desbloquea subidas nativas hasta 4 GB.
  - Si es cuenta estándar (2 GB): permite transferir y gestionar archivos con
    la cuenta vinculada cuando se necesite.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Literal

from hydrogram import Client
from hydrogram.errors import (
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)

from config import TELEGRAM_MAX_UPLOAD_BYTES, TELEGRAM_PREMIUM_MAX_UPLOAD_BYTES, BotConfig

logger = logging.getLogger("hydrocompress.user_session")

LoginStage = Literal["phone", "code", "password"]


@dataclass(slots=True)
class PendingLogin:
    stage: LoginStage
    client: Client | None = None
    phone_number: str = ""
    phone_code_hash: str = ""


class UserSessionManager:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.client: Client | None = None
        self.user_id: int | None = None
        self.username: str = ""
        self.first_name: str = ""
        self.is_premium: bool = False
        self._pending: PendingLogin | None = None

    @property
    def is_connected(self) -> bool:
        return self.client is not None

    @property
    def max_upload_bytes(self) -> int:
        if self.is_connected and self.is_premium:
            return TELEGRAM_PREMIUM_MAX_UPLOAD_BYTES
        return TELEGRAM_MAX_UPLOAD_BYTES

    def status_label(self) -> str:
        if not self.is_connected:
            return "No vinculada (usa /login)"
        handle = f"@{self.username}" if self.username else self.first_name or str(self.user_id)
        tier = "Premium · hasta 4 GB" if self.is_premium else "Estándar · hasta 2 GB"
        return f"Conectada ({handle} · {tier})"

    async def start_with_session(self, session_string: str) -> None:
        session_string = session_string.strip()
        if not session_string:
            return
        await self.stop()

        client = Client(
            name="hydrocompress_user",
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            session_string=session_string,
            in_memory=True,
            no_updates=True,
            max_concurrent_transmissions=4,
        )
        await client.start()
        me = await client.get_me()
        self.client = client
        self.user_id = me.id
        self.username = me.username or ""
        self.first_name = me.first_name or ""
        self.is_premium = bool(getattr(me, "is_premium", False))
        logger.info("Cuenta de Telegram vinculada: %s", self.status_label())

    async def stop(self) -> None:
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.stop()
            self.client = None
        self.user_id = None
        self.username = ""
        self.first_name = ""
        self.is_premium = False

    # ── Flujo interactivo /login desde Telegram ───────────────────────────────

    @property
    def login_in_progress(self) -> bool:
        return self._pending is not None

    @property
    def current_stage(self) -> LoginStage | None:
        return self._pending.stage if self._pending else None

    async def begin_login(self) -> None:
        await self.cancel_login()
        self._pending = PendingLogin(stage="phone")

    async def cancel_login(self) -> None:
        if self._pending and self._pending.client is not None:
            with contextlib.suppress(Exception):
                await self._pending.client.disconnect()
        self._pending = None

    async def submit_phone(self, phone: str) -> str:
        if not self._pending or self._pending.stage != "phone":
            raise RuntimeError("No hay ningún inicio de sesión activo.")

        clean_phone = "".join(c for c in phone.strip() if c.isdigit() or c == "+")
        if not clean_phone.startswith("+") or len(clean_phone) < 8:
            raise ValueError("Envía el número con código de país (ejemplo: `+5215512345678`).")

        temp_client = Client(
            name="hydrocompress_login_tmp",
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            in_memory=True,
            no_updates=True,
        )
        await temp_client.connect()
        try:
            sent = await temp_client.send_code(clean_phone)
        except PhoneNumberInvalid:
            await temp_client.disconnect()
            raise ValueError("Número de teléfono inválido según Telegram. Revisa el prefijo internacional.")
        except Exception:
            await temp_client.disconnect()
            raise

        self._pending.client = temp_client
        self._pending.phone_number = clean_phone
        self._pending.phone_code_hash = sent.phone_code_hash
        self._pending.stage = "code"
        return (
            "📨 **Código enviado a tu app de Telegram.**\n\n"
            "Envíame ahora el código **separado con espacios** (ejemplo: `1 2 3 4 5`) "
            "para evitar que Telegram lo invalide al enviarlo por chat.\n"
            "*(El mensaje se borrará automáticamente al instante. Usa `/cancellogin` para abortar.)*"
        )

    async def submit_code(self, raw_code: str) -> tuple[bool, str, str | None]:
        """Devuelve (completado, mensaje_para_usuario, session_string | None)."""
        if not self._pending or self._pending.stage != "code" or not self._pending.client:
            raise RuntimeError("No se está esperando un código en este momento.")

        digits = "".join(c for c in raw_code if c.isdigit())
        if len(digits) < 4:
            raise ValueError("Código inválido. Envíalo con espacios, por ejemplo: `1 2 3 4 5`.")

        client = self._pending.client
        try:
            await client.sign_in(
                phone_number=self._pending.phone_number,
                phone_code_hash=self._pending.phone_code_hash,
                phone_code=digits,
            )
        except SessionPasswordNeeded:
            self._pending.stage = "password"
            hint = ""
            with contextlib.suppress(Exception):
                hint_val = await client.get_password_hint()
                if hint_val:
                    hint = f"\n💡 Pista: `{hint_val}`"
            return (
                False,
                f"🔐 Tu cuenta tiene **verificación en dos pasos (2FA)**.{hint}\n\n"
                "Envíame ahora tu contraseña de nube (se borrará del chat al instante).",
                None,
            )
        except PhoneCodeInvalid:
            raise ValueError("Código incorrecto. Revisa el código en Telegram y envíalo de nuevo con espacios.")
        except PhoneCodeExpired:
            await self.cancel_login()
            raise ValueError("El código expiró. Ejecuta `/login` nuevamente.")

        session_str = await self._finalize_login(client)
        return (
            True,
            f"✅ **Cuenta de Telegram vinculada correctamente**\n• Estado: `{self.status_label()}`",
            session_str,
        )

    async def submit_password(self, password: str) -> tuple[str, str]:
        if not self._pending or self._pending.stage != "password" or not self._pending.client:
            raise RuntimeError("No se está esperando una contraseña 2FA.")

        client = self._pending.client
        try:
            await client.check_password(password=password)
        except PasswordHashInvalid:
            raise ValueError("Contraseña 2FA incorrecta. Inténtalo de nuevo o escribe `/cancellogin`.")

        session_str = await self._finalize_login(client)
        return (
            f"✅ **Cuenta de Telegram vinculada correctamente (2FA)**\n• Estado: `{self.status_label()}`",
            session_str,
        )

    async def _finalize_login(self, temp_client: Client) -> str:
        session_str = await temp_client.export_session_string()
        with contextlib.suppress(Exception):
            await temp_client.disconnect()
        self._pending = None
        await self.start_with_session(session_str)
        return session_str
