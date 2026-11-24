# HydroCompress Bot — Compresor de Videos para Telegram

Bot nativo de compresión de videos para Telegram (**sin servidor web**), construido con **Python 3.11+**, **Hydrogram + TgCrypto** y **FFmpeg**, listo para el Egg **Python Generic (Pelican / Pterodactyl)** en **panel.plugged.host**:

- **Auto-instalación rootless de FFmpeg/FFprobe:** Si tu Egg `Python Generic` no trae `ffmpeg`, `bot.py` descarga automáticamente binarios estáticos Linux (con `libx264`, `libx265` y `libass`) en `./bin/` al arrancar por primera vez, sin requerir permisos root.
- **Recursos auto-detectados:** lee automáticamente la CPU y RAM asignadas al contenedor (calibrado para `500% CPU / 5 hilos` · `4 GiB RAM` · `30 GiB Disco`) con guardia automática de espacio libre y borrado inmediato tras cada envío.
- **Límite de 2 GB, cuenta Telegram y >2 GB por enlace:**
  - Con `API_ID` + `API_HASH` + `BOT_TOKEN` el bot ya soporta nativamente **hasta 2 GB** vía MTProto.
  - Además puedes vincular tu cuenta de Telegram con **`/login`** dentro del propio chat (o con `python3 login_account.py`): si tu cuenta es **Telegram Premium**, desbloquea entregas nativas **hasta 4 GB**.
  - Si un video comprimido supera el límite de Telegram, el bot genera automáticamente un **enlace de descarga directa** (GoFile con respaldo Buzzheavier) + botón de 1 toque para re-comprimirlo a `1.90 GB` (2-Pass).

---

## 1. Cómo obtener `API_ID` y `API_HASH` (para desbloquear los 2 GB)

1. Entra a **[https://my.telegram.org](https://my.telegram.org)** e inicia sesión con tu número de teléfono de Telegram (recibirás un código dentro de la app de Telegram).
2. Entra a **API development tools**.
3. Crea una app (puedes poner cualquier nombre/título, ej. `HydroCompress`, plataforma `Desktop`) y guarda:
   - **`App api_id`** → tu `API_ID` (número).
   - **`App api_hash`** → tu `API_HASH` (texto largo).
4. Obtén tu **`BOT_TOKEN`** creando un bot en **[@BotFather](https://t.me/BotFather)** (`/newbot`).
5. Obtén tu **`OWNER_ID`** numérico escribiendo a **[@userinfobot](https://t.me/userinfobot)**.

---

## 2. Instalación en `panel.plugged.host` (Egg Python Generic)

1. Crea el servidor con el Egg **Python Generic (Pelican/Pterodactyl)** asignando **500% CPU**, **4096 MiB RAM** y **30720 MiB Disco**.
2. En **Startup**, elige imagen **Python 3.11** (recomendada por ruedas binarias precompiladas de `tgcrypto`) y cambia **`App py file` (`PY_FILE`)** a:
   ```text
   bot.py
   ```
3. Sube `hydrocompress-telegram-bot.zip` en **Archivos** y pulsa **Descomprimir**.
4. Coloca tu `.env` (puedes generarlo en la pestaña *Configurar .env* antes de bajar el ZIP, o renombrar `.env.example` a `.env` y pegar tus 4 credenciales).
5. Pulsa **Start**. En el primer arranque descargará `ffmpeg`/`ffprobe` estáticos en `./bin/` automáticamente y conectará tu bot.

---

## 3. Cómo entrar con tu cuenta de Telegram (opcional / Premium 4 GB)

Una vez encendido el bot, tienes dos formas:
- **Opción A (la más fácil, directo en Telegram):** escribe **`/login`** a tu bot. Te pedirá tu número (`+...`), luego el código recibido **separado por espacios** (ej. `1 2 3 4 5`) y tu contraseña 2FA si tienes. El bot borra tus mensajes al instante y guarda la sesión en SQLite.
- **Opción B (desde consola):** ejecuta `python3 login_account.py` y pega el `USER_SESSION_STRING` resultante en tu `.env`.

*(Si tu cuenta es Premium y quieres que videos de 2–4 GB también se entreguen a otros usuarios de tu lista blanca, crea un canal privado, añade a tu cuenta y al bot como administradores y ejecuta `/setdump -100XXXXXXXXXX`.)*
