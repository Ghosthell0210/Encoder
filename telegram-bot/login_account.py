"""Script de consola opcional para iniciar sesión con tu cuenta de Telegram.

Ejecuta en la consola de panel.plugged.host o en tu PC:
    python3 login_account.py

Genera un USER_SESSION_STRING listo para pegar en tu archivo .env (o también
puedes hacerlo directamente dentro de Telegram usando el comando /login).
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from hydrogram import Client
from hydrogram.errors import SessionPasswordNeeded

load_dotenv()


async def main() -> None:
    print("═══ Inicio de sesión de cuenta de Telegram (Hydrogram MTProto) ═══\n")
    api_id_raw = os.getenv("API_ID", "").strip() or input("API_ID (de my.telegram.org): ").strip()
    api_hash = os.getenv("API_HASH", "").strip() or input("API_HASH (de my.telegram.org): ").strip()
    api_id = int(api_id_raw)

    client = Client(
        name="hydrocompress_cli_login",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
        no_updates=True,
    )
    await client.connect()
    try:
        phone = input("Número con código de país (ej. +5215512345678): ").strip()
        sent = await client.send_code(phone)
        code = input("Código recibido en Telegram: ").strip().replace(" ", "")
        try:
            await client.sign_in(
                phone_number=phone,
                phone_code_hash=sent.phone_code_hash,
                phone_code=code,
            )
        except SessionPasswordNeeded:
            hint = await client.get_password_hint()
            if hint:
                print(f"Pista 2FA: {hint}")
            password = input("Contraseña de verificación en dos pasos (2FA): ").strip()
            await client.check_password(password=password)

        me = await client.get_me()
        session_string = await client.export_session_string()
        tier = "Telegram Premium (hasta 4 GB)" if getattr(me, "is_premium", False) else "Estándar (hasta 2 GB)"

        print(f"\n✅ Sesión iniciada como {me.first_name} (@{me.username or 'sin_usuario'}) — {tier}")
        print("\nCopia esta línea completa y pégala en tu archivo .env:\n")
        print(f"USER_SESSION_STRING={session_string}\n")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
