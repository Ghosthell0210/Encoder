"""Inspección segura de archivos de video mediante ffprobe (JSON)."""

from __future__ import annotations

import asyncio
import json
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


async def probe_media(file_path: Path) -> MediaInfo:
    """Ejecuta ffprobe sin shell para leer metadatos técnicos del video."""
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

    width = 0
    height = 0
    video_codec = "unknown"
    fps = 0.0
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
            if duration <= 0 and stream.get("duration"):
                try:
                    duration = float(stream["duration"])
                except ValueError:
                    pass
        elif stype == "audio":
            br = int(int(stream.get("bit_rate") or 0) / 1000)
            audio_tracks.append(
                AudioTrack(
                    rel_index=audio_rel,
                    stream_index=s_idx,
                    codec=codec,
                    channels=int(stream.get("channels") or 2),
                    language=lang,
                    title=title,
                    bitrate_kbps=br,
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
    )
