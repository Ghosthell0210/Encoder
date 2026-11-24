"""Formateadores visuales, barras de progreso anti-FloodWait y teclados inline."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from hydrogram.errors import FloodWait, MessageNotModified
from hydrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.ffmpeg_engine import PRESETS, CompressionOptions
from core.media_probe import MediaInfo


def human_bytes(num_bytes: int | float) -> str:
    value = float(max(0, num_bytes))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.2f} {unit}" if unit in {"GiB", "TiB"} else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TiB"


def human_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hrs, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def progress_bar(percent: float, width: int = 12) -> str:
    clamped = max(0.0, min(100.0, percent))
    filled = int(round((clamped / 100.0) * width))
    return f"[{'█' * filled}{'░' * (width - filled)}] {clamped:.1f}%"


class ProgressUpdater:
    """Actualiza mensajes en Telegram respetando límites y manejando FloodWait."""

    def __init__(self, message: Message, interval_sec: float = 3.5) -> None:
        self.message = message
        self.interval_sec = interval_sec
        self._last_edit: float = 0.0
        self._last_text: str = ""
        self._lock = asyncio.Lock()

    async def update(
        self,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and (now - self._last_edit) < self.interval_sec:
            return
        if text == self._last_text and not force:
            return

        async with self._lock:
            now = time.monotonic()
            if not force and (now - self._last_edit) < self.interval_sec:
                return
            try:
                await self.message.edit_text(
                    text,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                self._last_edit = time.monotonic()
                self._last_text = text
            except MessageNotModified:
                self._last_edit = time.monotonic()
            except FloodWait as fw:
                wait_sec = int(getattr(fw, "value", 5)) + 1
                self._last_edit = time.monotonic() + wait_sec
                if force:
                    await asyncio.sleep(min(wait_sec, 15))
                    with contextlib.suppress(Exception):
                        await self.message.edit_text(
                            text,
                            reply_markup=reply_markup,
                            disable_web_page_preview=True,
                        )
            except Exception:
                pass


# ── Textos y Teclados de Configuración de Video ──────────────────────────────

def _audio_mode_label(opts: CompressionOptions) -> str:
    if opts.audio_mode == "all":
        return "Todas las pistas"
    if opts.audio_mode == "none":
        return "Sin audio"
    if opts.audio_mode.startswith("track:"):
        idx = int(opts.audio_mode.split(":", 1)[1]) + 1
        return f"Pista #{idx}"
    return "Principal (#1)"


def _sub_mode_label(opts: CompressionOptions) -> str:
    if opts.subtitle_mode == "none":
        return "Sin subtítulos"
    kind = "Softsub" if opts.subtitle_mode == "soft" else "Hardsub (quemado)"
    if opts.subtitle_track_idx is not None:
        return f"{kind} #{opts.subtitle_track_idx + 1}"
    return f"{kind} (auto)"


def format_draft_summary(
    filename: str,
    file_size: int | None,
    duration: float | None,
    width: int | None,
    height: int | None,
    opts: CompressionOptions,
    media: MediaInfo | None = None,
    source_is_url: bool = False,
) -> str:
    codec_label = "H.265 (HEVC)" if opts.codec == "libx265" else "H.264 (AVC)"
    res_label = f"{opts.max_height}p" if opts.max_height else "Original"
    mode_line = (
        f"🎯 **Tamaño exacto (2-Pass):** `{opts.target_mb:.0f} MB`"
        if opts.target_mb
        else f"🎚 **Calidad (CRF):** `{opts.crf}` · Velocidad `{opts.speed}`"
    )

    size_str = human_bytes(file_size) if file_size else "Se detectará al descargar"
    dur_str = human_duration(duration) if duration else "Auto"
    dim_str = f"{width}×{height}" if width and height else "Auto"
    src_tag = "🌐 Enlace directo" if source_is_url else "📎 Archivo de Telegram"

    lines = [
        "🎬 **Panel de Compresión**",
        f"• **Origen:** {src_tag}",
        f"• **Archivo:** `{filename[:64]}`",
        f"• **Peso:** `{size_str}` · **Duración:** `{dur_str}` · **Res:** `{dim_str}`",
    ]
    if media:
        lines.append(
            f"• **Pistas:** `{len(media.audio_tracks)}` audio · `{len(media.subtitle_tracks)}` subtítulos · Códec `{media.video_codec}`"
        )
    lines.extend(
        [
            "",
            "⚙️ **Configuración actual:**",
            f"• **Códec:** `{codec_label}` · **Resolución máx:** `{res_label}`",
            f"• {mode_line}",
            f"• **Audio:** `{_audio_mode_label(opts)}` (`{opts.audio_bitrate_k}k`)",
            f"• **Subtítulos:** `{_sub_mode_label(opts)}`",
            f"• **Envío:** `{'Documento' if opts.send_as_document else 'Video reproducible'}`",
            "",
            "ℹ️ *Si el resultado supera 2 GB, se enviará automáticamente por enlace de descarga directa.*",
        ]
    )
    return "\n".join(lines)


def build_main_config_keyboard(session_id: str, opts: CompressionOptions) -> InlineKeyboardMarkup:
    doc_label = "📄 Como Documento" if opts.send_as_document else "🎞 Como Video"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⚡ Presets Rápidos", callback_data=f"cfg:{session_id}:presets"),
                InlineKeyboardButton("🎯 Tamaño Exacto (MB)", callback_data=f"cfg:{session_id}:target"),
            ],
            [
                InlineKeyboardButton("🛠 Códec / Res / Vel", callback_data=f"cfg:{session_id}:advanced"),
                InlineKeyboardButton("🎧 Audio y Subtítulos", callback_data=f"cfg:{session_id}:tracks"),
            ],
            [
                InlineKeyboardButton(doc_label, callback_data=f"cfg:{session_id}:toggle_doc"),
            ],
            [
                InlineKeyboardButton("🚀 Iniciar Compresión", callback_data=f"cfg:{session_id}:start"),
                InlineKeyboardButton("✖ Cancelar", callback_data=f"cfg:{session_id}:discard"),
            ],
        ]
    )


def build_presets_keyboard(session_id: str, current_key: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, p in PRESETS.items():
        mark = "✓ " if key == current_key else ""
        rows.append(
            [InlineKeyboardButton(f"{mark}{p.title}", callback_data=f"cfg:{session_id}:set_preset:{key}")]
        )
    rows.append([InlineKeyboardButton("⬅ Volver", callback_data=f"cfg:{session_id}:main")])
    return InlineKeyboardMarkup(rows)


def build_advanced_keyboard(session_id: str, opts: CompressionOptions) -> InlineKeyboardMarkup:
    c265 = "✓ H.265 (HEVC)" if opts.codec == "libx265" else "H.265 (HEVC)"
    c264 = "✓ H.264 (AVC)" if opts.codec == "libx264" else "H.264 (AVC)"

    def _res_btn(label: str, val: int | None) -> InlineKeyboardButton:
        mark = "✓ " if opts.max_height == val else ""
        code = "orig" if val is None else str(val)
        return InlineKeyboardButton(f"{mark}{label}", callback_data=f"cfg:{session_id}:res:{code}")

    def _spd_btn(val: str) -> InlineKeyboardButton:
        mark = "✓ " if opts.speed == val else ""
        return InlineKeyboardButton(f"{mark}{val}", callback_data=f"cfg:{session_id}:speed:{val}")

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(c265, callback_data=f"cfg:{session_id}:codec:libx265"),
                InlineKeyboardButton(c264, callback_data=f"cfg:{session_id}:codec:libx264"),
            ],
            [
                _res_btn("Orig", None),
                _res_btn("1080p", 1080),
                _res_btn("720p", 720),
                _res_btn("480p", 480),
                _res_btn("360p", 360),
            ],
            [
                _spd_btn("ultrafast"),
                _spd_btn("veryfast"),
                _spd_btn("fast"),
                _spd_btn("medium"),
            ],
            [
                InlineKeyboardButton("CRF -2 (Más calidad)", callback_data=f"cfg:{session_id}:crf:-2"),
                InlineKeyboardButton(f"CRF: {opts.crf}", callback_data=f"cfg:{session_id}:noop"),
                InlineKeyboardButton("CRF +2 (Más ligero)", callback_data=f"cfg:{session_id}:crf:+2"),
            ],
            [InlineKeyboardButton("⬅ Volver", callback_data=f"cfg:{session_id}:main")],
        ]
    )


def build_target_size_keyboard(session_id: str, opts: CompressionOptions) -> InlineKeyboardMarkup:
    sizes = [10, 25, 50, 100, 250, 500, 1000, 1900]
    row1 = [
        InlineKeyboardButton(
            f"{'✓ ' if opts.target_mb == s else ''}{s} MB",
            callback_data=f"cfg:{session_id}:set_mb:{s}",
        )
        for s in sizes[:4]
    ]
    row2 = [
        InlineKeyboardButton(
            f"{'✓ ' if opts.target_mb == s else ''}{'1.9 GB TG' if s == 1900 else f'{s} MB'}",
            callback_data=f"cfg:{session_id}:set_mb:{s}",
        )
        for s in sizes[4:]
    ]
    return InlineKeyboardMarkup(
        [
            row1,
            row2,
            [
                InlineKeyboardButton("✍️ Escribir tamaño en MB", callback_data=f"cfg:{session_id}:custom_mb"),
                InlineKeyboardButton("♻ Volver a modo CRF", callback_data=f"cfg:{session_id}:clear_mb"),
            ],
            [InlineKeyboardButton("⬅ Volver", callback_data=f"cfg:{session_id}:main")],
        ]
    )


def build_tracks_keyboard(
    session_id: str,
    opts: CompressionOptions,
    media: MediaInfo | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"{'✓ ' if opts.audio_mode == 'first' else ''}Audio #1",
                callback_data=f"cfg:{session_id}:audio:first",
            ),
            InlineKeyboardButton(
                f"{'✓ ' if opts.audio_mode == 'all' else ''}Todos los audios",
                callback_data=f"cfg:{session_id}:audio:all",
            ),
            InlineKeyboardButton(
                f"{'✓ ' if opts.audio_mode == 'none' else ''}Sin audio",
                callback_data=f"cfg:{session_id}:audio:none",
            ),
        ],
    ]

    if media and len(media.audio_tracks) > 1:
        for track in media.audio_tracks[:6]:
            key = f"track:{track.rel_index}"
            mark = "✓ " if opts.audio_mode == key else ""
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{mark}🔊 {track.label}",
                        callback_data=f"cfg:{session_id}:audio:{key}",
                    )
                ]
            )

    rows.append(
        [
            InlineKeyboardButton(
                f"{'✓ ' if opts.subtitle_mode == 'none' else ''}Sin subs",
                callback_data=f"cfg:{session_id}:sub:none",
            ),
            InlineKeyboardButton(
                f"{'✓ ' if opts.subtitle_mode == 'soft' else ''}Softsub",
                callback_data=f"cfg:{session_id}:sub:soft",
            ),
            InlineKeyboardButton(
                f"{'✓ ' if opts.subtitle_mode == 'hard' else ''}Hardsub",
                callback_data=f"cfg:{session_id}:sub:hard",
            ),
        ]
    )

    if media and media.subtitle_tracks:
        for sub in media.subtitle_tracks[:6]:
            mark = "✓ " if opts.subtitle_track_idx == sub.rel_index else ""
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{mark}💬 {sub.label}",
                        callback_data=f"cfg:{session_id}:subtrack:{sub.rel_index}",
                    )
                ]
            )

    rows.append(
        [
            InlineKeyboardButton("Audio 64k", callback_data=f"cfg:{session_id}:abr:64"),
            InlineKeyboardButton("Audio 96k", callback_data=f"cfg:{session_id}:abr:96"),
            InlineKeyboardButton("Audio 128k", callback_data=f"cfg:{session_id}:abr:128"),
            InlineKeyboardButton("Audio 192k", callback_data=f"cfg:{session_id}:abr:192"),
        ]
    )
    rows.append([InlineKeyboardButton("⬅ Volver", callback_data=f"cfg:{session_id}:main")])
    return InlineKeyboardMarkup(rows)


def build_job_cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖ Cancelar trabajo", callback_data=f"job:cancel:{job_id}")]]
    )


def build_large_file_link_keyboard(
    download_url: str,
    recompress_token: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("⬇ Descargar Video (>2 GB)", url=download_url)]
    ]
    if recompress_token:
        rows.append(
            [
                InlineKeyboardButton(
                    "📉 Re-comprimir a 1.90 GB (para Telegram)",
                    callback_data=f"recomp:{recompress_token}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def build_admin_keyboard(
    access_mode: str,
    concurrent_jobs: int,
    uploader: str,
) -> InlineKeyboardMarkup:
    mode_btn = (
        "🔒 Acceso: Solo Lista Blanca"
        if access_mode == "private"
        else "🌍 Acceso: Público"
    )
    conc_btn = (
        "⚙️ Concurrencia: 1 trabajo × 4 hilos"
        if concurrent_jobs == 1
        else "⚙️ Concurrencia: 2 trabajos × 2 hilos"
    )
    up_names = {
        "gofile": "GoFile",
        "buzzheavier": "Buzzheavier",
        "direct_http": "Enlace Directo",
    }
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(mode_btn, callback_data="adm:toggle_access")],
            [InlineKeyboardButton(conc_btn, callback_data="adm:toggle_conc")],
            [
                InlineKeyboardButton(
                    f"🔗 Host >2 GB: {up_names.get(uploader, uploader)}",
                    callback_data="adm:cycle_uploader",
                )
            ],
            [
                InlineKeyboardButton("👥 Ver Lista Blanca", callback_data="adm:users"),
                InlineKeyboardButton("🧹 Purgar Temporales", callback_data="adm:purge"),
            ],
            [
                InlineKeyboardButton("🔄 Actualizar Métricas", callback_data="adm:refresh"),
                InlineKeyboardButton("🛑 Cancelar Cola", callback_data="adm:cancelall"),
            ],
        ]
    )


def safe_caption(text: str, limit: int = 1020) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
