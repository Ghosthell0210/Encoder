"""Motor de compresión de video con FFmpeg (CRF, 2-Pass Tamaño Exacto, Audio, Subtítulos).

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
from typing import Literal

from core.media_probe import MediaInfo

CodecName = Literal["libx265", "libx264"]
SpeedPreset = Literal["ultrafast", "veryfast", "fast", "medium"]
SubMode = Literal["none", "soft", "hard"]


@dataclass(slots=True)
class PresetDef:
    key: str
    title: str
    codec: CodecName
    crf: int
    speed: SpeedPreset
    max_height: int | None  # None = resolución original
    audio_bitrate_k: int


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
        speed="fast",
        max_height=720,
        audio_bitrate_k=96,
    ),
    "light": PresetDef(
        key="light",
        title="Ultra Ligero (H.265 · 480p)",
        codec="libx265",
        crf=30,
        speed="fast",
        max_height=480,
        audio_bitrate_k=64,
    ),
    "compat_720": PresetDef(
        key="compat_720",
        title="Compatible (H.264 · 720p)",
        codec="libx264",
        crf=24,
        speed="fast",
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
    speed: SpeedPreset = "fast"
    max_height: int | None = 720  # None = Original
    audio_bitrate_k: int = 96
    # Audio: "all" | "first" | "none" | "track:<rel_idx>"
    audio_mode: str = "first"
    # Subtítulos: "none" | "soft" | "hard"
    subtitle_mode: SubMode = "none"
    subtitle_track_idx: int | None = None  # None = todas (en soft) o 0 (en hard)
    # Si se define > 0, usa compresión 2-Pass a ese tamaño exacto en MiB
    target_mb: float | None = None
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
        self.target_mb = None


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


def _needs_mkv_container(media: MediaInfo, opts: CompressionOptions) -> bool:
    """Usa MKV si se conservan softsubs de tipo mapa de bits (PGS/VOBSUB) o ASS."""
    if opts.subtitle_mode != "soft" or not media.subtitle_tracks:
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
    """Calcula (video_kbps, audio_kbps) para clavar un peso exacto en MiB.

    Aplica factor 0.96 (4% de margen para sobrecarga de contenedor MP4/MKV).
    """
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

    # Si con varias pistas de audio no alcanza, bajar audio a 48k
    if video_kbps < 100 and audio_track_count > 0:
        audio_kbps = 48
        video_kbps = int(total_kbps - (audio_kbps * audio_track_count))

    if video_kbps < 90:
        min_viable_mb = math.ceil(((100 + audio_kbps * audio_track_count) * duration_sec) / (8192.0 * 0.96))
        raise ValueError(
            f"El tamaño objetivo ({target_mb:.1f} MB) es demasiado pequeño para un video de "
            f"{int(duration_sec)}s. Prueba al menos {max(min_viable_mb, 2)} MB."
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
    """Extrae una miniatura limpia al 15% del video (máx 320px de ancho)."""
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
    """Extrae la pista de subtítulos a un archivo con nombre seguro para hardsub."""
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
            raise RuntimeError(f"FFmpeg falló ({pass_label}, código {proc.returncode}): {tail[:400]}")
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
) -> Path:
    """Comprime el video según opciones (CRF o 2-Pass Exacto) y devuelve la ruta final."""
    reg = register_proc or (lambda _p: None)
    output_dir.mkdir(parents=True, exist_ok=True)

    use_mkv = _needs_mkv_container(media, opts)
    ext = "mkv" if use_mkv else "mp4"
    stem = media.path.stem[:60].strip() or "video"
    # Limpiar caracteres problemáticos en nombre de salida
    safe_stem = "".join(c if c.isalnum() or c in " -_." else "_" for c in stem).strip(" .")
    output_path = output_dir / f"{safe_stem or 'video'}_compressed.{ext}"

    # ── Construir cadena de filtros de video (-vf) ────────────────────────────
    vf_parts: list[str] = []
    if opts.max_height is not None and media.height > opts.max_height:
        # Escala manteniendo proporción y garantizando dimensiones pares (requisito yuv420p)
        vf_parts.append(f"scale=-2:{opts.max_height}:flags=lanczos")
    else:
        # Garantizar dimensiones pares incluso sin reescalar
        vf_parts.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    burn_sub_file: Path | None = None
    if opts.subtitle_mode == "hard" and media.subtitle_tracks:
        sub_idx = opts.subtitle_track_idx if opts.subtitle_track_idx is not None else 0
        sub_track = next((t for t in media.subtitle_tracks if t.rel_index == sub_idx), media.subtitle_tracks[0])
        if sub_track.is_bitmap:
            raise ValueError(
                "La pista de subtítulos seleccionada es de imagen (PGS/VOBSUB) y no puede quemarse como texto. "
                "Elige modo Softsub (MKV) o selecciona una pista de texto (SRT/ASS)."
            )
        burn_sub_file = await _extract_subtitle_for_burn(
            media.path, output_dir, sub_track.rel_index, sub_track.codec
        )
        # Nombre relativo simple dentro de output_dir (sin problemas de escape)
        vf_parts.append(f"subtitles={burn_sub_file.name}")

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
    if opts.subtitle_mode == "soft" and media.subtitle_tracks:
        if opts.subtitle_track_idx is not None:
            map_args.extend(["-map", f"0:s:{opts.subtitle_track_idx}?"])
        else:
            map_args.extend(["-map", "0:s?"])
        sub_args.extend(["-c:s", "copy" if use_mkv else "mov_text"])
    else:
        sub_args.append("-sn")

    # ── Parámetros de códec y optimización de hilos (400% CPU) ────────────────
    codec_base: list[str] = [
        "-c:v",
        opts.codec,
        "-preset",
        opts.speed,
        "-pix_fmt",
        "yuv420p",
        "-threads",
        str(max(1, threads)),
    ]

    def _x265_extra(extra_pairs: list[str] | None = None) -> list[str]:
        if opts.codec != "libx265":
            return []
        params = [f"pools={max(1, threads)}", "log-level=error"]
        if extra_pairs:
            params.extend(extra_pairs)
        return ["-x265-params", ":".join(params), "-tag:v", "hvc1"]

    container_flags = ["-movflags", "+faststart"] if not use_mkv else []

    # ── MODO A: 2-Pass Tamaño Exacto en MB ────────────────────────────────────
    if opts.target_mb is not None and opts.target_mb > 0:
        video_kbps, audio_kbps = _compute_target_bitrates(
            opts.target_mb, media.duration, audio_count
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
            "-i",
            str(media.path),
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
                _x265_extra([f"pass=1", f"stats={passlog_prefix}.log"])
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
            duration_sec=media.duration,
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
            "-i",
            str(media.path),
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
                _x265_extra([f"pass=2", f"stats={passlog_prefix}.log"])
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
            duration_sec=media.duration,
            pass_label=f"Pase 2/2 (Objetivo {opts.target_mb:.0f} MB)",
            progress_offset=45.0,
            progress_weight=0.55,
            cancel_event=cancel_event,
            on_progress=on_progress,
            register_proc=reg,
        )
        return output_path

    # ── MODO B: CRF Inteligente (Calidad Constante) ───────────────────────────
    audio_args = (
        ["-c:a", "aac", "-b:a", f"{opts.audio_bitrate_k}k", "-ac", "2"]
        if audio_count > 0
        else ["-an"]
    )

    crf_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-i",
        str(media.path),
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
        duration_sec=media.duration,
        pass_label=f"Codificando ({opts.codec.replace('lib', '').upper()} CRF {opts.crf})",
        progress_offset=0.0,
        progress_weight=1.0,
        cancel_event=cancel_event,
        on_progress=on_progress,
        register_proc=reg,
    )
    return output_path
