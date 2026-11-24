import JSZip from 'jszip';

import botPy from '../../telegram-bot/bot.py?raw';
import configPy from '../../telegram-bot/config.py?raw';
import loginAccountPy from '../../telegram-bot/login_account.py?raw';
import requirementsTxt from '../../telegram-bot/requirements.txt?raw';
import envExample from '../../telegram-bot/.env.example?raw';
import dockerfile from '../../telegram-bot/Dockerfile?raw';
import startSh from '../../telegram-bot/start.sh?raw';
import readmeMd from '../../telegram-bot/README.md?raw';

import coreInitPy from '../../telegram-bot/core/__init__.py?raw';
import consoleCommandsPy from '../../telegram-bot/core/console_commands.py?raw';
import databasePy from '../../telegram-bot/core/database.py?raw';
import fastTransferPy from '../../telegram-bot/core/fast_transfer.py?raw';
import ffmpegBootstrapPy from '../../telegram-bot/core/ffmpeg_bootstrap.py?raw';
import gitUpdaterPy from '../../telegram-bot/core/git_updater.py?raw';
import jobLogCapturePy from '../../telegram-bot/core/job_log_capture.py?raw';
import loggerSetupPy from '../../telegram-bot/core/logger_setup.py?raw';
import mediaProbePy from '../../telegram-bot/core/media_probe.py?raw';
import mediaToolsPy from '../../telegram-bot/core/media_tools.py?raw';
import ffmpegEnginePy from '../../telegram-bot/core/ffmpeg_engine.py?raw';
import uploaderPy from '../../telegram-bot/core/uploader.py?raw';
import userSessionPy from '../../telegram-bot/core/user_session.py?raw';
import uiHelpersPy from '../../telegram-bot/core/ui_helpers.py?raw';
import queueManagerPy from '../../telegram-bot/core/queue_manager.py?raw';
import resourceMonitorPy from '../../telegram-bot/core/resource_monitor.py?raw';

import handlersInitPy from '../../telegram-bot/handlers/__init__.py?raw';
import userHandlersPy from '../../telegram-bot/handlers/user_handlers.py?raw';
import adminHandlersPy from '../../telegram-bot/handlers/admin_handlers.py?raw';

export interface BotFileItem {
  path: string;
  name: string;
  group: 'Raíz' | 'core/' | 'handlers/';
  summary: string;
  content: string;
  lines: number;
  bytes: number;
}

export interface EnvFormState {
  apiId: string;
  apiHash: string;
  botToken: string;
  ownerId: string;
  userSessionString: string;
  dumpChatId: string;
  accessMode: 'private' | 'public';
  maxConcurrentJobs: '1' | '2';
  largeFileUploader: 'gofile' | 'buzzheavier' | 'direct_http';
  gofileToken: string;
  publicDownloadBaseUrl: string;
  directLinkPort: string;
  directLinkTtlHours: string;
  includeEnvInZip: boolean;
}

export const DEFAULT_ENV_FORM: EnvFormState = {
  apiId: '',
  apiHash: '',
  botToken: '',
  ownerId: '',
  userSessionString: '',
  dumpChatId: '',
  accessMode: 'private',
  maxConcurrentJobs: '1',
  largeFileUploader: 'gofile',
  gofileToken: '',
  publicDownloadBaseUrl: '',
  directLinkPort: '8080',
  directLinkTtlHours: '6',
  includeEnvInZip: false,
};

function makeFile(
  path: string,
  group: BotFileItem['group'],
  summary: string,
  content: string,
): BotFileItem {
  const normalized = content.replace(/\r\n/g, '\n');
  const lines = normalized.split('\n').length;
  const bytes = new TextEncoder().encode(normalized).length;
  const name = path.split('/').pop() || path;
  return { path, name, group, summary, content: normalized, lines, bytes };
}

export const BOT_FILES: BotFileItem[] = [
  makeFile(
    'bot.py',
    'Raíz',
    'Arranque principal Hydrogram + TgCrypto, auto-instalación FFmpeg y sesión de cuenta',
    botPy,
  ),
  makeFile(
    'config.py',
    'Raíz',
    'Variables de entorno, asistente interactivo y auto-detección cgroup de CPU/RAM (500% CPU / 4 GiB RAM / 30 GiB Disco)',
    configPy,
  ),
  makeFile(
    'login_account.py',
    'Raíz',
    'Script opcional de consola para iniciar sesión con tu cuenta de Telegram y generar USER_SESSION_STRING',
    loginAccountPy,
  ),
  makeFile(
    'requirements.txt',
    'Raíz',
    'Dependencias Python (hydrogram, tgcrypto, aiohttp, aiosqlite, psutil)',
    requirementsTxt,
  ),
  makeFile(
    '.env.example',
    'Raíz',
    'Plantilla de credenciales API_ID/API_HASH, cuenta Telegram y proveedor >2 GB',
    envExample,
  ),
  makeFile(
    'start.sh',
    'Raíz',
    'Script de arranque automático para Pelican / Pterodactyl',
    startSh,
  ),
  makeFile(
    'Dockerfile',
    'Raíz',
    'Imagen opcional Python 3.11 + FFmpeg + TgCrypto lista para producción',
    dockerfile,
  ),
  makeFile(
    'README.md',
    'Raíz',
    'Guía completa para Egg Python Generic, API_ID/API_HASH y /login',
    readmeMd,
  ),
  makeFile(
    'core/console_commands.py',
    'core/',
    'Consola interactiva de Pelican/Pterodactyl (comandos login, logout, status)',
    consoleCommandsPy,
  ),
  makeFile(
    'core/fast_transfer.py',
    'core/',
    'Motor legal de descarga/subida paralela MTProto (múltiples conexiones media 20–60+ MB/s)',
    fastTransferPy,
  ),
  makeFile(
    'core/ffmpeg_bootstrap.py',
    'core/',
    'Descarga automática sin root de ffmpeg y ffprobe estáticos para Egg Python Generic',
    ffmpegBootstrapPy,
  ),
  makeFile(
    'core/git_updater.py',
    'core/',
    'Actualizador automático desde GitHub (funciona incluso sin .git inicial)',
    gitUpdaterPy,
  ),
  makeFile(
    'core/job_log_capture.py',
    'core/',
    'Auto-Log por trabajo con ContextVar: captura avisos no fatales/errores de Python y FFmpeg y los envía al dueño',
    jobLogCapturePy,
  ),
  makeFile(
    'core/logger_setup.py',
    'core/',
    'Logging rotativo en data/bot.log (5 MiB × 3) y lector rápido de errores para /logs',
    loggerSetupPy,
  ),
  makeFile(
    'core/ffmpeg_engine.py',
    'core/',
    'Motor FFmpeg AV1 (SVT-AV1 P4/P6/P8), H.265/H.264, 10-bit, Opus/AAC/Copy, FPS, Metadatos, Trim y subs',
    ffmpegEnginePy,
  ),
  makeFile(
    'core/media_tools.py',
    'core/',
    'Herramientas rápidas FFmpeg: cuadrícula 3×3, 4 capturas HD, Remux MKV↔MP4 sin recomprimir y extraer Opus/MP3',
    mediaToolsPy,
  ),
  makeFile(
    'core/user_session.py',
    'core/',
    'Gestor de inicio de sesión de cuenta Telegram (/login con 2FA) y soporte Premium 4 GB',
    userSessionPy,
  ),
  makeFile(
    'core/queue_manager.py',
    'core/',
    'Cola FIFO asíncrona, caché compartido con GC periódico, herramientas, Auto-Log y telemetría /live',
    queueManagerPy,
  ),
  makeFile(
    'core/resource_monitor.py',
    'core/',
    'Medición en vivo de RAM, CPU y disco (os.scandir) por video + snapshot instantáneo para /live',
    resourceMonitorPy,
  ),
  makeFile(
    'core/uploader.py',
    'core/',
    'Descarga HTTP directa y subida streaming >2 GB (GoFile, Buzzheavier y enlace temporal)',
    uploaderPy,
  ),
  makeFile(
    'core/media_probe.py',
    'core/',
    'Inspector FFprobe JSON para códec, duración, pistas de audio y subtítulos',
    mediaProbePy,
  ),
  makeFile(
    'core/database.py',
    'core/',
    'Persistencia SQLite asíncrona (lista blanca, miniaturas, sesión y estadísticas)',
    databasePy,
  ),
  makeFile(
    'core/ui_helpers.py',
    'core/',
    'Barras de progreso anti-FloodWait y menús inline de Telegram',
    uiHelpersPy,
  ),
  makeFile('core/__init__.py', 'core/', 'Inicializador del paquete core', coreInitPy),
  makeFile(
    'handlers/user_handlers.py',
    'handlers/',
    'Videos/enlaces, presets, tamaño exacto, pistas, re-compresión 1.9 GB y asistente /login',
    userHandlersPy,
  ),
  makeFile(
    'handlers/admin_handlers.py',
    'handlers/',
    'Panel /admin con telemetría psutil, /login, /logout, /setdump, lista blanca y purga',
    adminHandlersPy,
  ),
  makeFile('handlers/__init__.py', 'handlers/', 'Inicializador del paquete handlers', handlersInitPy),
];

export function buildEnvFileContent(form: EnvFormState): string {
  const apiId = form.apiId.trim() || '12345678';
  const apiHash = form.apiHash.trim() || 'tu_api_hash_aqui';
  const botToken = form.botToken.trim() || '123456789:tu_bot_token_aqui';
  const ownerId = form.ownerId.trim() || '123456789';
  const userSession = form.userSessionString.trim();
  const dumpChatId = form.dumpChatId.trim();
  const gofileToken = form.gofileToken.trim();
  const publicUrl = form.publicDownloadBaseUrl.trim().replace(/\/+$/, '');
  const port = form.directLinkPort.trim() || '8080';
  const ttl = form.directLinkTtlHours.trim() || '6';

  return [
    '# 1. Credenciales obligatorias de Telegram (MTProto hasta 2 GB)',
    `API_ID=${apiId}`,
    `API_HASH=${apiHash}`,
    `BOT_TOKEN=${botToken}`,
    `OWNER_ID=${ownerId}`,
    '',
    '# 2. Cuenta de Telegram opcional (puedes dejarlo vacío y usar /login en Telegram)',
    `USER_SESSION_STRING=${userSession}`,
    `DUMP_CHAT_ID=${dumpChatId}`,
    '',
    '# 3. Recursos asignados en panel.plugged.host (auto-detecta CPU/RAM del contenedor)',
    'AUTO_DETECT_RESOURCES=true',
    'MAX_CPU_THREADS=5',
    'MAX_RAM_GB=4',
    'MAX_DISK_GB=30',
    'DISK_RESERVE_GB=2.0',
    `MAX_CONCURRENT_JOBS=${form.maxConcurrentJobs}`,
    `ACCESS_MODE=${form.accessMode}`,
    '',
    '# 4. Entrega automática por enlace si el video comprimido supera 2 GB',
    `LARGE_FILE_UPLOADER=${form.largeFileUploader}`,
    `GOFILE_TOKEN=${gofileToken}`,
    '',
    '# Opcional: enlace directo temporal desde tu puerto (sin web UI)',
    `PUBLIC_DOWNLOAD_BASE_URL=${publicUrl}`,
    'DIRECT_LINK_HOST=0.0.0.0',
    `DIRECT_LINK_PORT=${port}`,
    `DIRECT_LINK_TTL_HOURS=${ttl}`,
    'PROGRESS_INTERVAL_SEC=3.5',
    '',
  ].join('\n');
}

export async function downloadBotZip(envForm: EnvFormState): Promise<void> {
  const zip = new JSZip();
  for (const file of BOT_FILES) {
    zip.file(file.path, file.content, {
      unixPermissions: file.path === 'start.sh' ? '755' : '644',
    });
  }
  if (envForm.includeEnvInZip) {
    zip.file('.env', buildEnvFileContent(envForm), { unixPermissions: '600' });
  }

  const blob = await zip.generateAsync({
    type: 'blob',
    compression: 'DEFLATE',
    compressionOptions: { level: 6 },
  });

  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'hydrocompress-telegram-bot.zip';
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export function downloadSingleTextFile(filename: string, content: string): void {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
