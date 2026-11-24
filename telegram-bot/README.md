# HydroCompress Bot — Compresor de Videos para Telegram

Bot nativo de compresión de videos para Telegram (**sin servidor web**), construido con **Python 3.11+**, **Hydrogram + TgCrypto** y **FFmpeg**, calibrado específicamente para tus recursos libres en **panel.plugged.host**:

- **CPU:** 400% (4 hilos FFmpeg — conmutable entre `1 trabajo × 4 hilos` o `2 trabajos × 2 hilos` desde `/admin`)
- **RAM:** 10 GiB asignados
- **Disco:** 30 GiB asignados con guardia automática de espacio libre y borrado inmediato tras cada envío
- **Límite de Telegram y >2 GB:** Envía nativamente hasta **1.99 GB (2000 MiB)** en Telegram y, si el archivo comprimido supera ese peso, genera automáticamente un **enlace de descarga directa** (GoFile con respaldo Buzzheavier o enlace temporal directo) + botón de 1 toque para re-comprimirlo a `1.90 GB` (2-Pass).

---

## Instalación rápida en `panel.plugged.host`

1. **Crea el servidor Python** en `panel.plugged.host` asignando tus recursos libres (`400% CPU`, `10 GiB RAM`, `30 GiB Disco`).
2. **Sube el ZIP** desde el **Administrador de Archivos** del panel y haz clic en **Descomprimir (Unarchive)**.
3. **Configura tus credenciales**: renombra `.env.example` a `.env` (o sube el `.env` generado al exportar) y completa:
   - `API_ID` y `API_HASH` desde [https://my.telegram.org](https://my.telegram.org) (sección *API development tools*).
   - `BOT_TOKEN` desde [@BotFather](https://t.me/BotFather).
   - `OWNER_ID` (tu ID numérico) desde [@userinfobot](https://t.me/userinfobot).
4. **Verifica FFmpeg**: si tu Egg de Python en Pterodactyl no trae `ffmpeg` preinstalado, selecciona una imagen Docker con FFmpeg (o construye usando el `Dockerfile` incluido).
5. **Comando de arranque (Startup)**:
   ```bash
   bash start.sh
   ```
   *(o directamente `python3 bot.py` después de `pip install -r requirements.txt`)*.

---

## Comandos principales en Telegram

| Comando | Descripción |
| :--- | :--- |
| `/start` | Guía rápida de uso |
| `/queue` | Ver trabajos en ejecución y en espera |
| `/setthumb` | Responder a una foto para fijarla como miniatura personalizada |
| `/delthumb` | Eliminar miniatura personalizada |
| `/stats` | Videos comprimidos, enlaces >2 GB generados y GB ahorrados |
| `/admin` | Panel interactivo (solo dueño): CPU/RAM/Disco, concurrencia, modo acceso y host >2 GB |
| `/adduser <id>` | Autorizar un usuario en la lista blanca |
| `/deluser <id>` | Revocar acceso de un usuario |
| `/purge` | Borrar carpetas y enlaces temporales inactivos |
| `/cancelall` | Detener y limpiar toda la cola |
