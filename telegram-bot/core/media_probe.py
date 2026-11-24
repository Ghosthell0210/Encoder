"""Inspección segura de archivos de video mediante ffprobe (JSON) y reporte MediaInfo."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

BITMAP_SUB_CODECS = {
    "hdmv_pgs_subtitle",
    "dvd_subtitle",
    "dvb_subtitle",
    "xsub",
}


@dataclass(slots=True)
class AudioTrack:
    rel_index: int  # 0:a:<rel_index>
    stream_index: int
    codec: str
    channels: int
    language: str
    title: str
    bitrate_kbps: int
    sample_rate: int = 0

    @property
    def label(self) -> str:
        lang = self.language.upper() if self.language else "UND"
        ch = f"{self.channels}ch" if self.channels else ""
        extra = f" · {self.title[:16]}" if self.title else ""
        return f"#{self.rel_index + 1} {lang} ({self.codec} {ch}){extra}".strip()


@dataclass(slots=True)
class SubtitleTrack:
    rel_index: int  # 0:s:<rel_index>
    stream_index: int
    codec: str
    language: str
    title: str
    is_bitmap: bool

    @property
    def label(self) -> str:
        lang = self.language.upper() if self.language else "UND"
        kind = "img" if self.is_bitmap else self.codec
        extra = f" · {self.title[:14]}" if self.title else ""
        return f"#{self.rel_index + 1} {lang} ({kind}){extra}".strip()


@dataclass(slots=True)
class MediaInfo:
    path: Path
    file_size: int
    duration: float
    width: int
    height: int
    video_codec: str
    fps: float
    bitrate_kbps: int
    audio_tracks: list[AudioTrack] = field(default_factory=list)
    subtitle_tracks: list[SubtitleTrack] = field(default_factory=list)
    format_name: str = ""
    format_long_name: str = ""
    pix_fmt: str = ""
    color_space: str = ""
    color_transfer: str = ""
    color_primaries: str = ""
    bit_depth: int = 8
    video_profile: str = ""
    video_bitrate_kbps: int = 0
    container_tags: dict[str, str] = field(default_factory=dict)

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer.lower() in {"smpte2084", "arib-std-b67"} or self.color_primaries.lower() == "bt2020"


def _parse_fps(rate_str: str | None) -> float:
    if not rate_str or rate_str in {"0/0", "N/A"}:
        return 0.0
    if "/" in rate_str:
        num, den = rate_str.split("/", 1)
        try:
            d = float(den)
            return round(float(num) / d, 2) if d > 0 else 0.0
        except ValueError:
            return 0.0
    try:
        return round(float(rate_str), 2)
    except ValueError:
        return 0.0


def _infer_bit_depth(stream: dict) -> int:
    raw = stream.get("bits_per_raw_sample")
    if raw and str(raw).isdigit():
        return int(raw)
    pix = str(stream.get("pix_fmt") or "").lower()
    if "12" in pix:
        return 12
    if "10" in pix:
        return 10
    return 8


async def probe_media(file_path: Path) -> MediaInfo:
    """Ejecuta ffprobe sin shell para leer metadatos técnicos completos del video."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffprobe no pudo leer el video: {err[:300]}")

    payload = json.loads(stdout.decode("utf-8", errors="replace"))
    fmt = payload.get("format", {})
    streams = payload.get("streams", [])

    file_size = int(fmt.get("size") or file_path.stat().st_size)
    duration = float(fmt.get("duration") or 0.0)
    overall_bitrate = int(int(fmt.get("bit_rate") or 0) / 1000)
    format_name = str(fmt.get("format_name") or "")
    format_long_name = str(fmt.get("format_long_name") or "")

    raw_fmt_tags = fmt.get("tags") or {}
    container_tags = {
        str(k).lower(): str(v).strip()
        for k, v in raw_fmt_tags.items()
        if str(v).strip()
    }

    width = 0
    height = 0
    video_codec = "unknown"
    fps = 0.0
    pix_fmt = ""
    color_space = ""
    color_transfer = ""
    color_primaries = ""
    bit_depth = 8
    video_profile = ""
    video_bitrate_kbps = 0
    audio_tracks: list[AudioTrack] = []
    subtitle_tracks: list[SubtitleTrack] = []

    audio_rel = 0
    sub_rel = 0

    for stream in streams:
        stype = stream.get("codec_type")
        tags = stream.get("tags") or {}
        lang = str(tags.get("language") or tags.get("LANGUAGE") or "und")
        title = str(tags.get("title") or tags.get("TITLE") or "")
        codec = str(stream.get("codec_name") or "unknown")
        s_idx = int(stream.get("index", 0))

        if stype == "video" and width == 0 and stream.get("disposition", {}).get("attached_pic", 0) == 0:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            video_codec = codec
            fps = _parse_fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
            pix_fmt = str(stream.get("pix_fmt") or "")
            color_space = str(stream.get("color_space") or "")
            color_transfer = str(stream.get("color_transfer") or "")
            color_primaries = str(stream.get("color_primaries") or "")
            bit_depth = _infer_bit_depth(stream)
            video_profile = str(stream.get("profile") or "")
            video_bitrate_kbps = int(int(stream.get("bit_rate") or 0) / 1000)
            if duration <= 0 and stream.get("duration"):
                try:
                    duration = float(stream["duration"])
                except ValueError:
                    pass
        elif stype == "audio":
            br = int(int(stream.get("bit_rate") or 0) / 1000)
            sr = int(stream.get("sample_rate") or 0)
            audio_tracks.append(
                AudioTrack(
                    rel_index=audio_rel,
                    stream_index=s_idx,
                    codec=codec,
                    channels=int(stream.get("channels") or 2),
                    language=lang,
                    title=title,
                    bitrate_kbps=br,
                    sample_rate=sr,
                )
            )
            audio_rel += 1
        elif stype == "subtitle":
            subtitle_tracks.append(
                SubtitleTrack(
                    rel_index=sub_rel,
                    stream_index=s_idx,
                    codec=codec,
                    language=lang,
                    title=title,
                    is_bitmap=codec in BITMAP_SUB_CODECS,
                )
            )
            sub_rel += 1

    if width == 0 or height == 0:
        raise RuntimeError("El archivo no contiene una pista de video válida.")

    return MediaInfo(
        path=file_path,
        file_size=file_size,
        duration=max(duration, 1.0),
        width=width,
        height=height,
        video_codec=video_codec,
        fps=fps,
        bitrate_kbps=overall_bitrate,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
        format_name=format_name,
        format_long_name=format_long_name,
        pix_fmt=pix_fmt,
        color_space=color_space,
        color_transfer=color_transfer,
        color_primaries=color_primaries,
        bit_depth=bit_depth,
        video_profile=video_profile,
        video_bitrate_kbps=video_bitrate_kbps,
        container_tags=container_tags,
    )


def format_mediainfo_report(
    media: MediaInfo,
    human_bytes_fn: Callable[[int | float], str],
    human_duration_fn: Callable[[float], str],
    divider: str = "━━━━━━━━━━━━━━━━━━━━",
) -> tuple[str, str]:
    """Devuelve (tarjeta_markdown_telegram, reporte_plano_completo_txt)."""
    hdr_tag = "HDR" if media.is_hdr else "SDR"
    color_bits = f"{media.bit_depth}-bit · {hdr_tag}"
    if media.pix_fmt:
        color_bits += f" ({media.pix_fmt})"
    prof = f" · {media.video_profile}" if media.video_profile else ""
    v_br = f"{media.video_bitrate_kbps} kbps" if media.video_bitrate_kbps else f"~{media.bitrate_kbps} kbps total"

    md: list[str] = [
        "🔎 **MediaInfo Técnico**",
        f"`{divider}`",
        f"📄 `{media.path.name[:68]}`",
        f"├ **Contenedor:** `{media.format_name or media.path.suffix.lstrip('.')}`",
        f"├ **Peso:** `{human_bytes_fn(media.file_size)}` · **Duración:** `{human_duration_fn(media.duration)}`",
        f"└ **Bitrate total:** `{media.bitrate_kbps} kbps`",
        "",
        "🎞 **Pista de Video**",
        f"├ **Códec:** `{media.video_codec.upper()}{prof}`",
        f"├ **Resolución:** `{media.width}×{media.height}` · **FPS:** `{media.fps or 'Auto'}`",
        f"├ **Color:** `{color_bits}`",
        f"└ **Bitrate video:** `{v_br}`",
    ]

    if media.audio_tracks:
        md.append("")
        md.append(f"🎧 **Audio ({len(media.audio_tracks)})**")
        for idx, a in enumerate(media.audio_tracks[:8]):
            branch = "└" if idx == min(len(media.audio_tracks), 8) - 1 else "├"
            br_txt = f" · {a.bitrate_kbps}k" if a.bitrate_kbps else ""
            sr_txt = f" · {a.sample_rate // 1000}kHz" if a.sample_rate else ""
            title_txt = f" · {a.title[:22]}" if a.title else ""
            md.append(
                f"{branch} `#{a.rel_index + 1}` `{a.language.upper()}` · `{a.codec.upper()}` `{a.channels}ch{br_txt}{sr_txt}`{title_txt}"
            )
        if len(media.audio_tracks) > 8:
            md.append(f"   *(+{len(media.audio_tracks) - 8} pistas adicionales en el .txt)*")

    if media.subtitle_tracks:
        md.append("")
        md.append(f"💬 **Subtítulos ({len(media.subtitle_tracks)})**")
        for idx, s in enumerate(media.subtitle_tracks[:8]):
            branch = "└" if idx == min(len(media.subtitle_tracks), 8) - 1 else "├"
            kind = "imagen/PGS" if s.is_bitmap else s.codec.upper()
            title_txt = f" · {s.title[:24]}" if s.title else ""
            md.append(f"{branch} `#{s.rel_index + 1}` `{s.language.upper()}` · `{kind}`{title_txt}")
        if len(media.subtitle_tracks) > 8:
            md.append(f"   *(+{len(media.subtitle_tracks) - 8} pistas adicionales en el .txt)*")

    meta_keys = ("title", "artist", "author", "encoder", "comment")
    shown_meta = [(k, media.container_tags[k]) for k in meta_keys if k in media.container_tags]
    if shown_meta:
        md.append("")
        md.append("🏷 **Metadatos**")
        for idx, (k, v) in enumerate(shown_meta):
            branch = "└" if idx == len(shown_meta) - 1 else "├"
            md.append(f"{branch} **{k.capitalize()}:** `{v[:56]}`")

    # Texto plano completo para adjuntar .txt cuando sea muy largo
    plain: list[str] = [
        f"Archivo: {media.path.name}",
        f"Contenedor: {media.format_long_name or media.format_name}",
        f"Tamaño: {human_bytes_fn(media.file_size)} ({media.file_size} bytes)",
        f"Duración: {human_duration_fn(media.duration)} ({media.duration:.2f}s)",
        f"Bitrate total: {media.bitrate_kbps} kbps",
        "",
        "[VIDEO]",
        f"Códec: {media.video_codec} ({media.video_profile or 'N/A'})",
        f"Resolución: {media.width}x{media.height}",
        f"FPS: {media.fps}",
        f"Color: {media.bit_depth}-bit | {hdr_tag} | pix_fmt={media.pix_fmt or 'N/A'} | space={media.color_space or 'N/A'} | transfer={media.color_transfer or 'N/A'} | primaries={media.color_primaries or 'N/A'}",
        f"Bitrate video: {media.video_bitrate_kbps} kbps",
        "",
        f"[AUDIO ({len(media.audio_tracks)})]",
    ]
    for a in media.audio_tracks:
        plain.append(
            f"  #{a.rel_index + 1} (stream {a.stream_index}): lang={a.language} codec={a.codec} ch={a.channels} br={a.bitrate_kbps}k sr={a.sample_rate}Hz title={a.title}"
        )
    plain.append("")
    plain.append(f"[SUBTITULOS ({len(media.subtitle_tracks)})]")
    for s in media.subtitle_tracks:
        plain.append(
            f"  #{s.rel_index + 1} (stream {s.stream_index}): lang={s.language} codec={s.codec} bitmap={s.is_bitmap} title={s.title}"
        )
    if media.container_tags:
        plain.append("")
        plain.append("[METADATOS]")
        for k, v in sorted(media.container_tags.items()):
            plain.append(f"  {k}: {v}")

    return "\n".join(md), "\n".join(plain) + "\n"
