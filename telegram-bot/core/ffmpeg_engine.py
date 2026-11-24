"""Motor de compresión FFmpeg: CRF, 2-Pass Exacto, 10-bit/Anime, Sample 15s,
Trim, Marca de Agua (drawtext), Multi-Audio y Subtítulos internos/externos.

Optimizado para 4 vCPUs (400% CPU) y ejecución segura sin shell=True.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.media_probe import MediaInfo

CodecName = Literal["libx265", "libx264"]
SpeedPreset = Literal["ultrafast", "veryfast", "fast", "medium"]
SubMode = Literal["none", "soft", "hard"]
WatermarkPos = Literal["tl", "tr", "bl", "br"]


@dataclass(slots=True)
class PresetDef:
    key: str
    title: str
    codec: CodecName
    crf: int
    speed: SpeedPreset
    max_height: int | None  # None = resolución original
    audio_bitrate_k: int
    bit_depth: Literal[8, 10] = 8
    tune: str | None = None


PRESETS: dict[str, PresetDef] = {
    "hq": PresetDef(
        key="hq",
        title="Alta Calidad (H.265 · Orig/1080p)",
        codec="libx265",
        crf=23,
        speed="fast",
        max_height=1080,
        audio_bitrate_k=128,
    ),
    "balanced": PresetDef(
        key="balanced",
        title="Equilibrado (H.265 · 720p)",
        codec="libx265",
        crf=26,
        speed="veryfast",
        max_height=720,
        audio_bitrate_k=96,
    ),
    "anime_10bit": PresetDef(
        key="anime_10bit",
        title="Anime 10-bit (H.265 · 720p · 10-bit)",
        codec="libx265",
        crf=25,
        speed="veryfast",
        max_height=720,
        audio_bitrate_k=96,
        bit_depth=10,
        tune="animation",
    ),
    "light": PresetDef(
        key="light",
        title="Ultra Ligero (H.265 · 480p)",
        codec="libx265",
        crf=30,
        speed="veryfast",
        max_height=480,
        audio_bitrate_k=64,
    ),
    "compat_720": PresetDef(
        key="compat_720",
        title="Compatible (H.264 · 720p)",
        codec="libx264",
        crf=24,
        speed="veryfast",
        max_height=720,
        audio_bitrate_k=128,
    ),
    "compat_480": PresetDef(
        key="compat_480",
        title="Rápido (H.264 · 480p)",
        codec="libx264",
        crf=26,
        speed="veryfast",
        max_height=480,
        audio_bitrate_k=96,
    ),
}


@dataclass(slots=True)
class CompressionOptions:
    preset_key: str = "balanced"
    codec: CodecName = "libx265"
    crf: int = 26
    speed: SpeedPreset = "veryfast"
    max_height: int | None = 720  # None = Original
    bit_depth: Literal[8, 10] = 8
    tune: str | None = None
    audio_bitrate_k: int = 96
    # Audio: "all" | "first" | "none" | "track:<rel_idx>"
    audio_mode: str = "first"
    # Subtítulos: "none" | "soft" | "hard"
    subtitle_mode: SubMode = "none"
    subtitle_track_idx: int | None = None
    # Subtítulo externo (.srt / .ass) adjuntado por el usuario
    external_sub_file_id: str | None = None
    external_sub_name: str | None = None
    # Tamaño exacto en MiB (2-Pass)
    target_mb: float | None = None
    # Recorte (en segundos)
    trim_start: float | None = None
    trim_end: float | None = None
    # Modo muestra rápida (ej. 15s del centro del video)
    sample_duration: float | None = None
    # Marca de agua discreta
    watermark_text: str = ""
    watermark_pos: WatermarkPos = "br"
    # Renombrado personalizado del archivo final (sin extensión)
    custom_output_name: str = ""
    send_as_document: bool = False

    def apply_preset(self, key: str) -> None:
        preset = PRESETS.get(key)
        if not preset:
            return
        self.preset_key = preset.key
        self.codec = preset.codec
        self.crf = preset.crf
        self.speed = preset.speed
        self.max_height = preset.max_height
        self.audio_bitrate_k = preset.audio_bitrate_k
        self.bit_depth = preset.bit_depth
        self.tune = preset.tune
        self.target_mb = None

    def clone(self) -> CompressionOptions:
        return CompressionOptions(
            preset_key=self.preset_key,
            codec=self.codec,
            crf=self.crf,
            speed=self.speed,
            max_height=self.max_height,
            bit_depth=self.bit_depth,
            tune=self.tune,
            audio_bitrate_k=self.audio_bitrate_k,
            audio_mode=self.audio_mode,
            subtitle_mode=self.subtitle_mode,
            subtitle_track_idx=self.subtitle_track_idx,
            external_sub_file_id=self.external_sub_file_id,
            external_sub_name=self.external_sub_name,
            target_mb=self.target_mb,
            trim_start=self.trim_start,
            trim_end=self.trim_end,
            sample_duration=self.sample_duration,
            watermark_text=self.watermark_text,
            watermark_pos=self.watermark_pos,
            custom_output_name=self.custom_output_name,
            send_as_document=self.send_as_document,
        )

    def to_saved_dict(self) -> dict[str, Any]:
        return {
            "preset_key": self.preset_key,
            "codec": self.codec,
            "crf": self.crf,
            "speed": self.speed,
            "max_height": self.max_height,
            "bit_depth": self.bit_depth,
            "tune": self.tune,
            "audio_bitrate_k": self.audio_bitrate_k,
            "audio_mode": self.audio_mode,
            "subtitle_mode": self.subtitle_mode,
            "target_mb": self.target_mb,
            "send_as_document": self.send_as_document,
        }

    @classmethod
    def from_saved_dict(
        cls,
        data: dict[str, Any] | None,
        watermark_text: str = "",
        watermark_pos: str = "br",
        send_as_document: bool = False,
    ) -> CompressionOptions:
        opts = cls(
            watermark_text=watermark_text,
            watermark_pos=watermark_pos if watermark_pos in {"tl", "tr", "bl", "br"} else "br",  # type: ignore[arg-type]
            send_as_document=send_as_document,
        )
        if not data:
            return opts
        if data.get("preset_key") in PRESETS:
            opts.preset_key = str(data["preset_key"])
        if data.get("codec") in {"libx265", "libx264"}:
            opts.codec = data["codec"]
        if isinstance(data.get("crf"), int):
            opts.crf = max(16, min(38, data["crf"]))
        if data.get("speed") in {"ultrafast", "veryfast", "fast", "medium"}:
            opts.speed = data["speed"]
        mh = data.get("max_height")
        opts.max_height = int(mh) if isinstance(mh, int) and mh > 0 else None
        opts.bit_depth = 10 if data.get("bit_depth") == 10 else 8
        opts.tune = str(data["tune"]) if data.get("tune") else None
        if isinstance(data.get("audio_bitrate_k"), int):
            opts.audio_bitrate_k = data["audio_bitrate_k"]
        if isinstance(data.get("audio_mode"), str):
            opts.audio_mode = data["audio_mode"]
        if data.get("subtitle_mode") in {"none", "soft", "hard"}:
            opts.subtitle_mode = data["subtitle_mode"]
        if isinstance(data.get("target_mb"), (int, float)) and data["target_mb"] > 0:
            opts.target_mb = float(data["target_mb"])
        if isinstance(data.get("send_as_document"), bool):
            opts.send_as_document = data["send_as_document"]
        return opts


@dataclass(slots=True)
class EncodeProgress:
    percent: float
    fps: float
    speed: str
    out_time_sec: float
    current_size_bytes: int
    pass_label: str


ProgressCallback = Callable[[EncodeProgress], Awaitable[None]]


class JobCancelledError(Exception):
    """Se lanza cuando el usuario o el administrador cancela un trabajo."""


def parse_timestamp(raw: str) -> float:
    """Convierte 'HH:MM:SS', 'MM:SS' o segundos a float."""
    token = raw.strip()
    if ":" not in token:
        val = float(token.replace(",", "."))
        if val < 0:
            raise ValueError("El tiempo no puede ser negativo.")
        return val
    parts = [float(p) for p in token.split(":")]
    if len(parts) == 2:
        return parts[0] * 60.0 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600.0 + parts[1] * 60.0 + parts[2]
    raise ValueError("Formato de tiempo inválido. Usa MM:SS o HH:MM:SS.")


def _escape_drawtext(text: str) -> str:
    """Escapa caracteres especiales para el filtro drawtext de FFmpeg."""
    escaped = text.replace("\\", "\\\\")
    escaped = escaped.replace(":", "\\:")
    escaped = escaped.replace("'", "\\'")
    escaped = escaped.replace("%", "\\%")
    return escaped


def _build_watermark_filter(text: str, pos: WatermarkPos) -> str:
    safe_txt = _escape_drawtext(text.strip()[:48])
    coords = {
        "tl": "x=20:y=20",
        "tr": "x=w-tw-20:y=20",
        "bl": "x=20:y=h-th-20",
        "br": "x=w-tw-20:y=h-th-20",
    }[pos]
    return (
        f"drawtext=text='{safe_txt}':{coords}:"
        "fontsize=max(16\\,h/34):fontcolor=white@0.68:"
        "shadowcolor=black@0.75:shadowx=2:shadowy=2"
    )


def _needs_mkv_container(
    media: MediaInfo,
    opts: CompressionOptions,
    external_sub_path: Path | None,
) -> bool:
    if opts.subtitle_mode != "soft":
        return False
    if external_sub_path is not None:
        return external_sub_path.suffix.lower() in {".ass", ".ssa"}
    if not media.subtitle_tracks:
        return False
    selected = (
        [t for t in media.subtitle_tracks if t.rel_index == opts.subtitle_track_idx]
        if opts.subtitle_track_idx is not None
        else media.subtitle_tracks
    )
    for track in selected:
        if track.is_bitmap or track.codec in {"ass", "ssa"}:
            return True
    return False


def _compute_target_bitrates(
    target_mb: float,
    duration_sec: float,
    audio_track_count: int,
) -> tuple[int, int]:
    if duration_sec <= 0:
        raise ValueError("La duración del video es inválida para modo tamaño exacto.")

    total_kbits = target_mb * 8192.0 * 0.96
    total_kbps = total_kbits / duration_sec

    if audio_track_count <= 0:
        audio_kbps = 0
    elif total_kbps < 280:
        audio_kbps = 48
    elif total_kbps < 500:
        audio_kbps = 64
    elif total_kbps < 1200:
        audio_kbps = 96
    else:
        audio_kbps = 128

    video_kbps = int(total_kbps - (audio_kbps * max(audio_track_count, 0)))

    if video_kbps < 100 and audio_track_count > 0:
        audio_kbps = 48
        video_kbps = int(total_kbps - (audio_kbps * audio_track_count))

    if video_kbps < 90:
        min_viable_mb = math.ceil(
            ((100 + audio_kbps * audio_track_count) * duration_sec) / (8192.0 * 0.96)
        )
        raise ValueError(
            f"El tamaño objetivo ({target_mb:.1f} MB) es demasiado pequeño para {int(duration_sec)}s. "
            f"Prueba al menos {max(min_viable_mb, 2)} MB."
        )

    return video_kbps, audio_kbps


def _count_selected_audio_tracks(media: MediaInfo, opts: CompressionOptions) -> int:
    if not media.audio_tracks or opts.audio_mode == "none":
        return 0
    if opts.audio_mode == "all":
        return len(media.audio_tracks)
    return 1


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()


async def extract_thumbnail(
    video_path: Path,
    thumb_path: Path,
    duration_sec: float,
) -> Path | None:
    seek_sec = max(1.0, min(duration_sec * 0.15, max(duration_sec - 1.0, 1.0)))
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{seek_sec:.2f}",
        "-i",
        str(video_path),
        "-vframes",
        "1",
        "-vf",
        "scale=320:-2",
        "-q:v",
        "4",
        str(thumb_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode == 0 and thumb_path.exists() and thumb_path.stat().st_size > 0:
        return thumb_path
    return None


async def _extract_subtitle_for_burn(
    input_path: Path,
    work_dir: Path,
    rel_sub_index: int,
    codec: str,
) -> Path:
    ext = "ass" if codec in {"ass", "ssa"} else "srt"
    sub_file = work_dir / f"burn_sub.{ext}"
    out_codec = "ass" if ext == "ass" else "srt"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-map",
        f"0:s:{rel_sub_index}",
        "-c:s",
        out_codec,
        str(sub_file),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not sub_file.exists():
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"No se pudo extraer el subtítulo para quemar: {err[:200]}")
    return sub_file


async def _run_ffmpeg_pass(
    cmd: list[str],
    cwd: Path,
    duration_sec: float,
    pass_label: str,
    progress_offset: float,
    progress_weight: float,
    cancel_event: asyncio.Event,
    on_progress: ProgressCallback | None,
    register_proc: Callable[[asyncio.subprocess.Process | None], None],
) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    register_proc(proc)
    stderr_lines: list[str] = []

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                stderr_lines.append(text)
                if len(stderr_lines) > 25:
                    stderr_lines.pop(0)

    stderr_task = asyncio.create_task(_drain_stderr())

    try:
        assert proc.stdout is not None
        fps = 0.0
        speed = "0.0x"
        out_time_sec = 0.0
        total_size = 0

        while True:
            if cancel_event.is_set():
                await _terminate_process(proc)
                raise JobCancelledError("Compresión cancelada por el usuario.")

            try:
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    break
                continue

            if not raw_line:
                break

            line = raw_line.decode("utf-8", errors="replace").strip()
            if "=" not in line:
                continue

            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()

            if key == "fps":
                with contextlib.suppress(ValueError):
                    fps = float(val)
            elif key == "speed":
                if val and val != "N/A":
                    speed = val
            elif key == "total_size":
                with contextlib.suppress(ValueError):
                    total_size = int(val)
            elif key == "out_time_us":
                with contextlib.suppress(ValueError):
                    out_time_sec = max(0.0, int(val) / 1_000_000.0)
            elif key == "progress":
                raw_pct = min(100.0, (out_time_sec / max(duration_sec, 1.0)) * 100.0)
                if val == "end":
                    raw_pct = 100.0
                weighted_pct = min(100.0, progress_offset + raw_pct * progress_weight)
                if on_progress is not None:
                    await on_progress(
                        EncodeProgress(
                            percent=weighted_pct,
                            fps=fps,
                            speed=speed,
                            out_time_sec=out_time_sec,
                            current_size_bytes=total_size,
                            pass_label=pass_label,
                        )
                    )
                if val == "end":
                    break

        await proc.wait()
        await stderr_task

        if cancel_event.is_set():
            raise JobCancelledError("Compresión cancelada por el usuario.")

        if proc.returncode != 0:
            tail = "\n".join(stderr_lines[-8:])
            raise RuntimeError(f"FFmpeg falló ({pass_label}, código {proc.returncode}): {tail[:450]}")
    finally:
        register_proc(None)
        if not stderr_task.done():
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task


async def compress_video(
    media: MediaInfo,
    output_dir: Path,
    opts: CompressionOptions,
    threads: int,
    cancel_event: asyncio.Event,
    on_progress: ProgressCallback | None = None,
    register_proc: Callable[[asyncio.subprocess.Process | None], None] | None = None,
    external_sub_path: Path | None = None,
) -> Path:
    """Comprime el video según todas las opciones configuradas y devuelve la ruta final."""
    reg = register_proc or (lambda _p: None)
    output_dir.mkdir(parents=True, exist_ok=True)

    use_mkv = _needs_mkv_container(media, opts, external_sub_path)
    ext = "mkv" if use_mkv else "mp4"

    raw_stem = opts.custom_output_name.strip() or media.path.stem[:140].strip() or "video"
    safe_stem = "".join(
        "_" if c in "/\\\0\r\n\t" or ord(c) < 32 else c for c in raw_stem
    ).strip(" .")
    suffix = "_sample15s" if opts.sample_duration else ""
    output_path = output_dir / f"{safe_stem or 'video'}{suffix}.{ext}"

    # ── Recorte (Trim) o Muestra rápida de 15s (Sample) ───────────────────────
    seek_args: list[str] = []
    dur_args: list[str] = []
    effective_duration = media.duration

    if opts.sample_duration and opts.sample_duration > 0:
        sample_len = min(opts.sample_duration, media.duration)
        start_sec = max(0.0, min(media.duration * 0.40, max(media.duration - sample_len, 0.0)))
        seek_args = ["-ss", f"{start_sec:.2f}"]
        dur_args = ["-t", f"{sample_len:.2f}"]
        effective_duration = max(1.0, sample_len)
    elif opts.trim_start is not None or opts.trim_end is not None:
        start_sec = max(0.0, min(opts.trim_start or 0.0, max(media.duration - 1.0, 0.0)))
        end_sec = min(opts.trim_end, media.duration) if opts.trim_end else media.duration
        if end_sec <= start_sec:
            raise ValueError("El tiempo final del recorte debe ser mayor que el tiempo inicial.")
        clip_dur = end_sec - start_sec
        if start_sec > 0:
            seek_args = ["-ss", f"{start_sec:.2f}"]
        dur_args = ["-t", f"{clip_dur:.2f}"]
        effective_duration = max(1.0, clip_dur)

    # ── Entradas FFmpeg (video + posible subtítulo externo) ───────────────────
    input_args: list[str] = [*seek_args, "-i", str(media.path)]
    ext_sub_local: Path | None = None
    if external_sub_path is not None and external_sub_path.exists():
        ext_suffix = external_sub_path.suffix.lower() or ".srt"
        ext_sub_local = output_dir / f"ext_sub{ext_suffix}"
        if external_sub_path.resolve() != ext_sub_local.resolve():
            ext_sub_local.write_bytes(external_sub_path.read_bytes())
        if opts.subtitle_mode == "soft":
            input_args.extend(["-i", str(ext_sub_local)])

    # ── Cadena de filtros de video (-vf) ──────────────────────────────────────
    vf_parts: list[str] = []
    if opts.max_height is not None and media.height > opts.max_height:
        vf_parts.append(f"scale=-2:{opts.max_height}:flags=lanczos")
    else:
        vf_parts.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    if opts.subtitle_mode == "hard":
        if ext_sub_local is not None:
            vf_parts.append(f"subtitles={ext_sub_local.name}")
        elif media.subtitle_tracks:
            sub_idx = opts.subtitle_track_idx if opts.subtitle_track_idx is not None else 0
            sub_track = next(
                (t for t in media.subtitle_tracks if t.rel_index == sub_idx),
                media.subtitle_tracks[0],
            )
            if sub_track.is_bitmap:
                raise ValueError(
                    "La pista de subtítulos seleccionada es de imagen (PGS/VOBSUB) y no puede quemarse. "
                    "Elige modo Softsub (MKV) o adjunta un archivo .srt/.ass."
                )
            burn_sub_file = await _extract_subtitle_for_burn(
                media.path, output_dir, sub_track.rel_index, sub_track.codec
            )
            vf_parts.append(f"subtitles={burn_sub_file.name}")

    if opts.watermark_text.strip():
        vf_parts.append(_build_watermark_filter(opts.watermark_text, opts.watermark_pos))

    vf_filter = ",".join(vf_parts)

    # ── Mapeo de audio y subtítulos ───────────────────────────────────────────
    audio_count = _count_selected_audio_tracks(media, opts)
    map_args: list[str] = ["-map", "0:v:0"]

    if audio_count > 0:
        if opts.audio_mode == "all":
            map_args.extend(["-map", "0:a?"])
        elif opts.audio_mode.startswith("track:"):
            t_idx = int(opts.audio_mode.split(":", 1)[1])
            map_args.extend(["-map", f"0:a:{t_idx}?"])
        else:
            map_args.extend(["-map", "0:a:0?"])

    sub_args: list[str] = []
    if opts.subtitle_mode == "soft":
        if ext_sub_local is not None:
            map_args.extend(["-map", "1:0"])
            sub_args.extend(["-c:s", "copy" if use_mkv else "mov_text"])
        elif media.subtitle_tracks:
            if opts.subtitle_track_idx is not None:
                map_args.extend(["-map", f"0:s:{opts.subtitle_track_idx}?"])
            else:
                map_args.extend(["-map", "0:s?"])
            sub_args.extend(["-c:s", "copy" if use_mkv else "mov_text"])
        else:
            sub_args.append("-sn")
    else:
        sub_args.append("-sn")

    # ── Códec, 8-bit vs 10-bit y optimización de 4 hilos ──────────────────────
    pix_fmt = "yuv420p10le" if (opts.bit_depth == 10 and opts.codec == "libx265") else "yuv420p"
    codec_base: list[str] = [
        "-c:v",
        opts.codec,
        "-preset",
        opts.speed,
        "-pix_fmt",
        pix_fmt,
        "-threads",
        str(max(1, threads)),
    ]
    if opts.tune:
        codec_base.extend(["-tune", opts.tune])

    def _x265_extra(extra_pairs: list[str] | None = None) -> list[str]:
        if opts.codec != "libx265":
            return []
        params = [f"pools={max(1, threads)}", "log-level=error"]
        if opts.bit_depth == 10:
            params.append("profile=main10")
        if extra_pairs:
            params.extend(extra_pairs)
        return ["-x265-params", ":".join(params), "-tag:v", "hvc1"]

    container_flags = ["-movflags", "+faststart"] if not use_mkv else []

    # ── MODO A: 2-Pass Tamaño Exacto en MB (no se aplica en Sample 15s) ───────
    if opts.target_mb is not None and opts.target_mb > 0 and not opts.sample_duration:
        video_kbps, audio_kbps = _compute_target_bitrates(
            opts.target_mb, effective_duration, audio_count
        )
        passlog_prefix = str(output_dir / "ffmpeg2pass")
        null_sink = "NUL" if os.name == "nt" else "/dev/null"

        pass1_cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            *seek_args,
            "-i",
            str(media.path),
            *dur_args,
            "-map",
            "0:v:0",
            "-vf",
            vf_filter,
            *codec_base,
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{int(video_kbps * 1.45)}k",
            "-bufsize",
            f"{int(video_kbps * 2.0)}k",
            *(
                _x265_extra(["pass=1", f"stats={passlog_prefix}.log"])
                if opts.codec == "libx265"
                else ["-pass", "1", "-passlogfile", passlog_prefix]
            ),
            "-an",
            "-sn",
            "-f",
            "null",
            null_sink,
        ]

        await _run_ffmpeg_pass(
            cmd=pass1_cmd,
            cwd=output_dir,
            duration_sec=effective_duration,
            pass_label="Pase 1/2 (Análisis)",
            progress_offset=0.0,
            progress_weight=0.45,
            cancel_event=cancel_event,
            on_progress=on_progress,
            register_proc=reg,
        )

        audio_args = (
            ["-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "2"]
            if audio_count > 0
            else ["-an"]
        )

        pass2_cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            *input_args,
            *dur_args,
            *map_args,
            "-vf",
            vf_filter,
            *codec_base,
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{int(video_kbps * 1.45)}k",
            "-bufsize",
            f"{int(video_kbps * 2.0)}k",
            *(
                _x265_extra(["pass=2", f"stats={passlog_prefix}.log"])
                if opts.codec == "libx265"
                else ["-pass", "2", "-passlogfile", passlog_prefix]
            ),
            *audio_args,
            *sub_args,
            *container_flags,
            str(output_path),
        ]

        await _run_ffmpeg_pass(
            cmd=pass2_cmd,
            cwd=output_dir,
            duration_sec=effective_duration,
            pass_label=f"Pase 2/2 (Objetivo {opts.target_mb:.0f} MB)",
            progress_offset=45.0,
            progress_weight=0.55,
            cancel_event=cancel_event,
            on_progress=on_progress,
            register_proc=reg,
        )
        return output_path

    # ── MODO B: CRF Inteligente (o Muestra 15s) ───────────────────────────────
    audio_args = (
        ["-c:a", "aac", "-b:a", f"{opts.audio_bitrate_k}k", "-ac", "2"]
        if audio_count > 0
        else ["-an"]
    )

    bit_tag = " 10-bit" if (opts.bit_depth == 10 and opts.codec == "libx265") else ""
    label_prefix = "Muestra 15s" if opts.sample_duration else "Codificando"

    crf_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        *input_args,
        *dur_args,
        *map_args,
        "-vf",
        vf_filter,
        *codec_base,
        "-crf",
        str(opts.crf),
        *_x265_extra(),
        *audio_args,
        *sub_args,
        *container_flags,
        str(output_path),
    ]

    await _run_ffmpeg_pass(
        cmd=crf_cmd,
        cwd=output_dir,
        duration_sec=effective_duration,
        pass_label=f"{label_prefix} ({opts.codec.replace('lib', '').upper()}{bit_tag} CRF {opts.crf})",
        progress_offset=0.0,
        progress_weight=1.0,
        cancel_event=cancel_event,
        on_progress=on_progress,
        register_proc=reg,
    )
    return output_path
