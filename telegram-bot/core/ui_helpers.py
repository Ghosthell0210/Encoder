"""Formateadores visuales, barras de progreso anti-FloodWait y teclados inline."""

from __future__ import annotations

import asyncio
import contextlib
import time

from hydrogram.errors import FloodWait, MessageNotModified
from hydrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.ffmpeg_engine import PRESETS, CompressionOptions
from core.media_probe import MediaInfo

DIVIDER = "━━━━━━━━━━━━━━━━━━━━"


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
    return f"{'▰' * filled}{'▱' * (width - filled)}  {clamped:5.1f}%"


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


# ── Teclado del Menú Principal (/start) ──────────────────────────────────────

def build_start_keyboard(is_owner: bool, auto_mode: bool) -> InlineKeyboardMarkup:
    auto_label = "⚡ Modo Lotes: ON" if auto_mode else "⚡ Modo Lotes: OFF"
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(auto_label, callback_data="home:toggle_auto"),
            InlineKeyboardButton("📋 Ver Cola", callback_data="home:queue"),
        ],
        [
            InlineKeyboardButton("📊 Estadísticas", callback_data="home:stats"),
            InlineKeyboardButton("📖 Comandos", callback_data="home:help"),
        ],
    ]
    if is_owner:
        rows.append(
            [
                InlineKeyboardButton("📡 En Vivo", callback_data="home:live"),
                InlineKeyboardButton("🛡 Panel Admin", callback_data="home:admin"),
            ]
        )
    return InlineKeyboardMarkup(rows)


def build_back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅ Menú Principal", callback_data="home:main")]]
    )


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


def _audio_codec_label(opts: CompressionOptions) -> str:
    if opts.audio_mode == "none":
        return "—"
    if opts.audio_codec == "copy":
        return "Copiar"
    if opts.audio_codec == "libopus":
        return f"Opus {opts.audio_bitrate_k}k"
    return f"AAC {opts.audio_bitrate_k}k"


def _sub_mode_label(opts: CompressionOptions) -> str:
    if opts.subtitle_mode == "none":
        return "Sin subtítulos"
    kind = "Softsub" if opts.subtitle_mode == "soft" else "Hardsub"
    if opts.external_sub_name:
        return f"{kind} · {opts.external_sub_name[:22]}"
    if opts.subtitle_track_idx is not None:
        return f"{kind} #{opts.subtitle_track_idx + 1}"
    return f"{kind} (auto)"


def _watermark_pos_label(pos: str) -> str:
    return {
        "tl": "↖ Arriba-Izq",
        "tr": "↗ Arriba-Der",
        "bl": "↙ Abajo-Izq",
        "br": "↘ Abajo-Der",
    }.get(pos, "↘ Abajo-Der")


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
    codec_label = opts.codec_display_label()
    res_label = f"{opts.max_height}p" if opts.max_height else "Original"
    fps_label = f" · {opts.target_fps} fps" if opts.target_fps else ""
    if opts.target_mb:
        mode_val = f"2-Pass · {opts.target_mb:.0f} MB"
    elif opts.codec == "libsvtav1":
        mode_val = f"CRF {opts.crf} · Preset {opts.av1_preset}"
    else:
        mode_val = f"CRF {opts.crf} · {opts.speed}"

    size_str = human_bytes(file_size) if file_size else "Al descargar"
    dur_str = human_duration(duration) if duration else "Auto"
    dim_str = f"{width}×{height}" if width and height else "Auto"
    src_tag = "Enlace (URL/yt-dlp)" if source_is_url else "Telegram"

    lines = [
        "🎬 **Configurar Compresión**",
        f"`{DIVIDER}`",
        f"📄 `{filename[:64]}`",
        f"├ **Origen:** `{src_tag}` · **Peso:** `{size_str}`",
        f"└ **Duración:** `{dur_str}` · **Resolución:** `{dim_str}`",
    ]
    if opts.custom_output_name:
        lines.append(f"✏️ **Renombrado:** `{opts.custom_output_name[:56]}`")
    if media:
        hdr_tag = " · HDR" if media.is_hdr else ""
        lines.append(
            f"🔎 **Pistas:** `{len(media.audio_tracks)}` audio · `{len(media.subtitle_tracks)}` subs · `{media.video_codec.upper()}{hdr_tag}` (`{media.fps or '?'} fps`)"
        )

    lines.extend(
        [
            "",
            "⚙️ **Ajustes Activos**",
            f"├ **Video:** `{codec_label}` · `{res_label}{fps_label}`",
            f"├ **Modo:** `{mode_val}`",
            f"├ **Audio:** `{_audio_mode_label(opts)}` (`{_audio_codec_label(opts)}`)",
            f"├ **Subtítulos:** `{_sub_mode_label(opts)}`",
        ]
    )
    pro_tags: list[str] = []
    if opts.hdr_to_sdr != "auto":
        pro_tags.append(f"HDR→SDR: {opts.hdr_to_sdr.upper()}")
    elif media and media.is_hdr:
        pro_tags.append("HDR→SDR (Auto)")
    if opts.auto_crop:
        pro_tags.append("Auto-Crop")
    if opts.codec == "libsvtav1" and opts.av1_film_grain:
        pro_tags.append("Grano AV1")
    if opts.deband:
        pro_tags.append("Deband")
    if opts.deinterlace:
        pro_tags.append("Desentrelazar")
    if opts.loudnorm:
        pro_tags.append("Loudnorm")
    if pro_tags:
        lines.append(f"├ **Filtros Pro:** `{' · '.join(pro_tags)}`")

    if opts.trim_start is not None or opts.trim_end is not None:
        s = human_duration(opts.trim_start or 0.0)
        e = human_duration(opts.trim_end) if opts.trim_end else "fin"
        lines.append(f"├ **Recorte:** `{s} → {e}`")
    if opts.watermark_text.strip():
        lines.append(
            f"├ **Marca:** `{opts.watermark_text}` ({_watermark_pos_label(opts.watermark_pos)})"
        )
    if opts.strip_metadata or opts.meta_title or opts.meta_artist:
        meta_parts: list[str] = []
        if opts.strip_metadata:
            meta_parts.append("Limpiar")
        if opts.meta_title:
            meta_parts.append(f"Título: {opts.meta_title[:20]}")
        if opts.meta_artist:
            meta_parts.append(f"Autor: {opts.meta_artist[:18]}")
        lines.append(f"├ **Metadatos:** `{' · '.join(meta_parts)}`")
    lines.append(
        f"└ **Formato envío:** `{'Documento' if opts.send_as_document else 'Video reproducible'}`"
    )
    return "\n".join(lines)


def build_main_config_keyboard(session_id: str, opts: CompressionOptions) -> InlineKeyboardMarkup:
    doc_label = "📄 Envío: Doc" if opts.send_as_document else "🎞 Envío: Video"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⚡ Presets (AV1/H265)", callback_data=f"cfg:{session_id}:presets"),
                InlineKeyboardButton("🎯 Tamaño (MB)", callback_data=f"cfg:{session_id}:target"),
            ],
            [
                InlineKeyboardButton("🛠 Códec / FPS / 10b", callback_data=f"cfg:{session_id}:advanced"),
                InlineKeyboardButton("🎧 Audio / Subs", callback_data=f"cfg:{session_id}:tracks"),
            ],
            [
                InlineKeyboardButton("✂️ Trim / Marca / Meta", callback_data=f"cfg:{session_id}:extras"),
                InlineKeyboardButton("🧰 Herramientas", callback_data=f"cfg:{session_id}:tools"),
            ],
            [
                InlineKeyboardButton("✏️ Renombrar", callback_data=f"cfg:{session_id}:rename"),
                InlineKeyboardButton(doc_label, callback_data=f"cfg:{session_id}:toggle_doc"),
            ],
            [
                InlineKeyboardButton("🎞 Muestra 15s", callback_data=f"cfg:{session_id}:sample"),
                InlineKeyboardButton("💾 Guardar Auto", callback_data=f"cfg:{session_id}:save_auto"),
            ],
            [
                InlineKeyboardButton("🚀 Comprimir", callback_data=f"cfg:{session_id}:start"),
                InlineKeyboardButton("✖ Descartar", callback_data=f"cfg:{session_id}:discard"),
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
    c265 = "✓ H.265" if opts.codec == "libx265" else "H.265"
    c264 = "✓ H.264" if opts.codec == "libx264" else "H.264"
    cav1 = "✓ AV1" if opts.codec == "libsvtav1" else "AV1"

    if opts.codec == "libx264":
        bit_label = "🎨 8-bit (H.264)"
    else:
        bit_label = "🎨 10-bit Activado" if opts.bit_depth == 10 else "🎨 8-bit Estándar"

    def _res_btn(label: str, val: int | None) -> InlineKeyboardButton:
        mark = "✓ " if opts.max_height == val else ""
        code = "orig" if val is None else str(val)
        return InlineKeyboardButton(f"{mark}{label}", callback_data=f"cfg:{session_id}:res:{code}")

    def _fps_btn(label: str, val: int | None) -> InlineKeyboardButton:
        mark = "✓ " if opts.target_fps == val else ""
        code = "orig" if val is None else str(val)
        return InlineKeyboardButton(f"{mark}{label}", callback_data=f"cfg:{session_id}:fps:{code}")

    if opts.codec == "libsvtav1":
        speed_row = [
            InlineKeyboardButton(
                f"{'✓ ' if opts.av1_preset == p else ''}P{p}",
                callback_data=f"cfg:{session_id}:av1p:{p}",
            )
            for p in (10, 8, 6, 4)
        ]
    else:
        speed_row = [
            InlineKeyboardButton(
                f"{'✓ ' if opts.speed == val else ''}{val}",
                callback_data=f"cfg:{session_id}:speed:{val}",
            )
            for val in ("ultrafast", "veryfast", "fast", "medium")
        ]

    bit_row = [InlineKeyboardButton(bit_label, callback_data=f"cfg:{session_id}:toggle_10bit")]
    if opts.codec == "libsvtav1":
        grain_lbl = "🎞 Grano AV1: ON" if opts.av1_film_grain else "🎞 Grano AV1: OFF"
        bit_row.append(InlineKeyboardButton(grain_lbl, callback_data=f"cfg:{session_id}:toggle_grain"))

    crop_lbl = "✂️ Auto-Crop: ON" if opts.auto_crop else "✂️ Auto-Crop: OFF"
    hdr_lbl = {
        "auto": "🌈 HDR→SDR: Auto",
        "on": "🌈 HDR→SDR: ON",
        "off": "🌈 HDR→SDR: OFF",
    }.get(opts.hdr_to_sdr, "🌈 HDR→SDR: Auto")

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(c265, callback_data=f"cfg:{session_id}:codec:libx265"),
                InlineKeyboardButton(c264, callback_data=f"cfg:{session_id}:codec:libx264"),
                InlineKeyboardButton(cav1, callback_data=f"cfg:{session_id}:codec:libsvtav1"),
            ],
            bit_row,
            [
                _res_btn("Orig", None),
                _res_btn("1080p", 1080),
                _res_btn("720p", 720),
                _res_btn("480p", 480),
                _res_btn("360p", 360),
            ],
            speed_row,
            [
                _fps_btn("FPS Orig", None),
                _fps_btn("24 fps", 24),
                _fps_btn("30 fps", 30),
                _fps_btn("60 fps", 60),
            ],
            [
                InlineKeyboardButton(crop_lbl, callback_data=f"cfg:{session_id}:toggle_crop"),
                InlineKeyboardButton(hdr_lbl, callback_data=f"cfg:{session_id}:cycle_hdr"),
            ],
            [
                InlineKeyboardButton("➖ CRF -2", callback_data=f"cfg:{session_id}:crf:-2"),
                InlineKeyboardButton(f"CRF {opts.crf}", callback_data=f"cfg:{session_id}:noop"),
                InlineKeyboardButton("➕ CRF +2", callback_data=f"cfg:{session_id}:crf:+2"),
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
            f"{'✓ ' if opts.target_mb == s else ''}{'1.9 GB' if s == 1900 else f'{s} MB'}",
            callback_data=f"cfg:{session_id}:set_mb:{s}",
        )
        for s in sizes[4:]
    ]
    return InlineKeyboardMarkup(
        [
            row1,
            row2,
            [
                InlineKeyboardButton("✍️ Escribir MB", callback_data=f"cfg:{session_id}:custom_mb"),
                InlineKeyboardButton("♻ Modo CRF", callback_data=f"cfg:{session_id}:clear_mb"),
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
                f"{'✓ ' if opts.audio_mode == 'all' else ''}Todos",
                callback_data=f"cfg:{session_id}:audio:all",
            ),
            InlineKeyboardButton(
                f"{'✓ ' if opts.audio_mode == 'none' else ''}Sin audio",
                callback_data=f"cfg:{session_id}:audio:none",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'✓ ' if opts.audio_codec == 'aac' else ''}AAC",
                callback_data=f"cfg:{session_id}:acodec:aac",
            ),
            InlineKeyboardButton(
                f"{'✓ ' if opts.audio_codec == 'libopus' else ''}Opus",
                callback_data=f"cfg:{session_id}:acodec:libopus",
            ),
            InlineKeyboardButton(
                f"{'✓ ' if opts.audio_codec == 'copy' else ''}Copiar",
                callback_data=f"cfg:{session_id}:acodec:copy",
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

    ext_row = [
        InlineKeyboardButton(
            "📎 Adjuntar .srt / .ass" if not opts.external_sub_name else f"📎 {opts.external_sub_name[:16]}",
            callback_data=f"cfg:{session_id}:attach_sub",
        )
    ]
    if opts.external_sub_name:
        ext_row.append(
            InlineKeyboardButton("🗑 Quitar", callback_data=f"cfg:{session_id}:clear_ext_sub")
        )
    rows.append(ext_row)

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
            InlineKeyboardButton(
                f"{'✓ ' if opts.audio_bitrate_k == br else ''}{br}k",
                callback_data=f"cfg:{session_id}:abr:{br}",
            )
            for br in (48, 64, 96, 128, 192)
        ]
    )
    rows.append([InlineKeyboardButton("⬅ Volver", callback_data=f"cfg:{session_id}:main")])
    return InlineKeyboardMarkup(rows)


def build_extras_keyboard(session_id: str, opts: CompressionOptions) -> InlineKeyboardMarkup:
    def _pos_btn(pos: str, label: str) -> InlineKeyboardButton:
        mark = "✓ " if opts.watermark_pos == pos else ""
        return InlineKeyboardButton(f"{mark}{label}", callback_data=f"cfg:{session_id}:wmpos:{pos}")

    strip_lbl = "🧼 Limpiar Meta: ON" if opts.strip_metadata else "🧼 Limpiar Meta: OFF"
    deband_lbl = "🎨 Deband: ON" if opts.deband else "🎨 Deband: OFF"
    deint_lbl = "📺 Desentrelazar: ON" if opts.deinterlace else "📺 Desentrelazar: OFF"
    loud_lbl = "🔊 Nivelar Audio: ON" if opts.loudnorm else "🔊 Nivelar Audio: OFF"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✂️ Fijar Recorte", callback_data=f"cfg:{session_id}:set_trim"),
                InlineKeyboardButton("♻ Quitar Recorte", callback_data=f"cfg:{session_id}:clear_trim"),
            ],
            [
                InlineKeyboardButton("🏷 Texto Marca", callback_data=f"cfg:{session_id}:set_wm"),
                InlineKeyboardButton("🗑 Quitar Marca", callback_data=f"cfg:{session_id}:clear_wm"),
            ],
            [
                _pos_btn("tl", "↖ Arr-Izq"),
                _pos_btn("tr", "↗ Arr-Der"),
                _pos_btn("bl", "↙ Abj-Izq"),
                _pos_btn("br", "↘ Abj-Der"),
            ],
            [
                InlineKeyboardButton(deband_lbl, callback_data=f"cfg:{session_id}:toggle_deband"),
                InlineKeyboardButton(deint_lbl, callback_data=f"cfg:{session_id}:toggle_deint"),
            ],
            [
                InlineKeyboardButton(loud_lbl, callback_data=f"cfg:{session_id}:toggle_loudnorm"),
                InlineKeyboardButton(strip_lbl, callback_data=f"cfg:{session_id}:toggle_strip_meta"),
            ],
            [
                InlineKeyboardButton("📝 Título Meta", callback_data=f"cfg:{session_id}:set_meta_title"),
                InlineKeyboardButton("👤 Autor Meta", callback_data=f"cfg:{session_id}:set_meta_artist"),
            ],
            [InlineKeyboardButton("⬅ Volver", callback_data=f"cfg:{session_id}:main")],
        ]
    )


def build_tools_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔎 MediaInfo Técnico", callback_data=f"cfg:{session_id}:tool:mediainfo"),
            ],
            [
                InlineKeyboardButton("🖼 Cuadrícula 3×3", callback_data=f"cfg:{session_id}:tool:screenshots_grid"),
                InlineKeyboardButton("📸 4 Capturas HD", callback_data=f"cfg:{session_id}:tool:screenshots_hd"),
            ],
            [
                InlineKeyboardButton("🔄 Remux a MP4", callback_data=f"cfg:{session_id}:tool:remux_mp4"),
                InlineKeyboardButton("🔄 Remux a MKV", callback_data=f"cfg:{session_id}:tool:remux_mkv"),
            ],
            [
                InlineKeyboardButton("🎵 Extraer Opus", callback_data=f"cfg:{session_id}:tool:extract_opus"),
                InlineKeyboardButton("🎵 Extraer MP3", callback_data=f"cfg:{session_id}:tool:extract_mp3"),
            ],
            [InlineKeyboardButton("⬅ Volver", callback_data=f"cfg:{session_id}:main")],
        ]
    )


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
                    "📉 Re-comprimir a 1.90 GB (Telegram)",
                    callback_data=f"recomp:{recompress_token}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def build_admin_keyboard(
    access_mode: str,
    concurrent_jobs: int,
    uploader: str,
    error_alerts: bool = True,
    threads_per_job: int | None = None,
) -> InlineKeyboardMarkup:
    mode_btn = "🔒 Lista Blanca" if access_mode == "private" else "🌍 Público"
    if threads_per_job is not None:
        conc_btn = f"⚙️ {concurrent_jobs} × {threads_per_job} hilos"
    else:
        conc_btn = "⚙️ 1 trabajo" if concurrent_jobs == 1 else "⚙️ 2 en paralelo"
    up_names = {
        "gofile": "GoFile",
        "buzzheavier": "Buzzheavier",
        "direct_http": "Directo",
    }
    alert_btn = "🔔 Auto-Log: ON" if error_alerts else "🔕 Auto-Log: OFF"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📡 En Vivo (/live)", callback_data="adm:live"),
                InlineKeyboardButton("📈 Consumo (/usage)", callback_data="adm:usage"),
            ],
            [
                InlineKeyboardButton(mode_btn, callback_data="adm:toggle_access"),
                InlineKeyboardButton(conc_btn, callback_data="adm:toggle_conc"),
            ],
            [
                InlineKeyboardButton(
                    f"🔗 Host: {up_names.get(uploader, uploader)}",
                    callback_data="adm:cycle_uploader",
                ),
                InlineKeyboardButton(alert_btn, callback_data="adm:toggle_alerts"),
            ],
            [
                InlineKeyboardButton("📄 Ver Logs", callback_data="adm:logs"),
                InlineKeyboardButton("⬇ Bajar bot.log", callback_data="adm:logfile"),
            ],
            [
                InlineKeyboardButton("👥 Usuarios", callback_data="adm:users"),
                InlineKeyboardButton("🧹 Purgar Temp", callback_data="adm:purge"),
            ],
            [
                InlineKeyboardButton("🛑 Cancelar Cola", callback_data="adm:cancelall"),
                InlineKeyboardButton("⬆️ Actualizar Bot", callback_data="adm:update"),
            ],
            [
                InlineKeyboardButton("🔄 Refrescar Panel", callback_data="adm:refresh"),
            ],
        ]
    )


def build_live_keyboard(has_active_jobs: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("🔄 Actualizar", callback_data="live:refresh"),
            InlineKeyboardButton("📈 Historial /usage", callback_data="adm:usage"),
        ]
    ]
    if has_active_jobs:
        rows.append([InlineKeyboardButton("🛑 Cancelar Cola", callback_data="live:cancelall")])
    rows.append([InlineKeyboardButton("🛡 Panel Admin", callback_data="home:admin")])
    return InlineKeyboardMarkup(rows)


def safe_caption(text: str, limit: int = 1020) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
