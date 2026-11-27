"""Herramientas rápidas FFmpeg: Screenshots 3×3 / HD, Remux MKV↔MP4 y Extracción de Audio.

Todas las utilidades corren sin shell=True, respetan cancelación en vivo y envían
cualquier advertencia no fatal de stderr al capturador Auto-Log del trabajo actual.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from core.ffmpeg_engine import JobCancelledError, _terminate_process
from core.job_log_capture import record_current_job_ffmpeg_line
from core.media_probe import MediaInfo, probe_audio_duration

MP4_COMPATIBLE_AUDIO = {"aac", "mp3", "opus", "ac3", "eac3", "alac", "mp4a", "mp2"}
MP4_COMPATIBLE_VIDEO = {"h264", "hevc", "h265", "av1", "mpeg4", "vp9"}


def _safe_stem(raw: str) -> str:
    cleaned = "".join(
        "_" if c in "/\\\0\r\n\t" or ord(c) < 32 else c for c in raw[:140]
    ).strip(" .")
    return cleaned or "video"


def _fmt_ts(sec: float) -> str:
    total = max(0, int(round(sec)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


async def _run_simple_ffmpeg(
    cmd: list[str],
    cancel_event: asyncio.Event,
    register_proc: Callable[[asyncio.subprocess.Process | None], None],
    label: str,
) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    register_proc(proc)
    stderr_lines: list[str] = []

    async def _drain() -> None:
        assert proc.stderr is not None
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                stderr_lines.append(line)
                if len(stderr_lines) > 25:
                    stderr_lines.pop(0)
                record_current_job_ffmpeg_line(line)

    drain_task = asyncio.create_task(_drain())
    try:
        while proc.returncode is None:
            if cancel_event.is_set():
                await _terminate_process(proc)
                raise JobCancelledError(f"{label} cancelado por el usuario.")
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
        await drain_task
        if cancel_event.is_set():
            raise JobCancelledError(f"{label} cancelado por el usuario.")
        if proc.returncode != 0:
            tail = "\n".join(stderr_lines[-8:])
            raise RuntimeError(f"FFmpeg falló en {label} (código {proc.returncode}): {tail[:420]}")
    finally:
        register_proc(None)
        if not drain_task.done():
            drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain_task


async def generate_screenshot_grid(
    media: MediaInfo,
    out_dir: Path,
    cancel_event: asyncio.Event,
    register_proc: Callable[[asyncio.subprocess.Process | None], None],
    cols: int = 3,
    rows: int = 3,
) -> Path:
    """Genera una cuadrícula 3×3 (9 capturas con marcas de tiempo) en JPEG nítido."""
    out_dir.mkdir(parents=True, exist_ok=True)
    count = cols * rows
    dur = max(media.duration, 2.0)

    # Extraer cada frame con seek rápido (-ss antes de -i) para no decodificar todo el video
    frame_paths: list[Path] = []
    for i in range(count):
        if cancel_event.is_set():
            raise JobCancelledError("Capturas canceladas.")
        frac = 0.08 + (0.84 * i / max(count - 1, 1))
        ts = min(dur - 0.3, max(0.2, dur * frac))
        frame_out = out_dir / f"frame_{i:02d}.jpg"
        ts_label = _fmt_ts(ts).replace(":", "\\:")

        vf_with_ts = (
            f"scale=480:-2:flags=lanczos,"
            f"drawbox=x=0:y=ih-28:w=iw:h=28:color=black@0.55:t=fill,"
            f"drawtext=text='{ts_label}':x=w-tw-10:y=h-th-6:fontsize=16:fontcolor=white"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-ss",
            f"{ts:.2f}",
            "-i",
            str(media.path),
            "-vframes",
            "1",
            "-vf",
            vf_with_ts,
            "-q:v",
            "3",
            str(frame_out),
        ]
        try:
            await _run_simple_ffmpeg(cmd, cancel_event, register_proc, f"Captura {i + 1}/{count}")
        except RuntimeError:
            # Fallback seguro sin drawtext por si el contenedor carece de fuente TrueType
            cmd_fallback = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{ts:.2f}",
                "-i",
                str(media.path),
                "-vframes",
                "1",
                "-vf",
                "scale=480:-2:flags=lanczos",
                "-q:v",
                "3",
                str(frame_out),
            ]
            await _run_simple_ffmpeg(cmd_fallback, cancel_event, register_proc, f"Captura {i + 1}/{count}")
        if frame_out.exists() and frame_out.stat().st_size > 0:
            frame_paths.append(frame_out)

    if not frame_paths:
        raise RuntimeError("No se pudo extraer ningún fotograma del video.")

    grid_path = out_dir / f"{_safe_stem(media.path.stem)}_grid.jpg"
    tile_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-pattern_type",
        "glob",
        "-i",
        str(out_dir / "frame_*.jpg"),
        "-vf",
        f"tile={cols}x{rows}:padding=6:margin=8:color=0x111827",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(grid_path),
    ]
    await _run_simple_ffmpeg(tile_cmd, cancel_event, register_proc, "Cuadrícula 3×3")
    if not grid_path.exists() or grid_path.stat().st_size == 0:
        raise RuntimeError("No se pudo generar la hoja de capturas 3×3.")
    return grid_path


async def generate_hd_screenshots(
    media: MediaInfo,
    out_dir: Path,
    cancel_event: asyncio.Event,
    register_proc: Callable[[asyncio.subprocess.Process | None], None],
    count: int = 4,
) -> list[tuple[Path, str]]:
    """Extrae capturas HD individuales en resolución original (hasta 1920px de ancho)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = max(media.duration, 2.0)
    results: list[tuple[Path, str]] = []

    for i in range(count):
        if cancel_event.is_set():
            raise JobCancelledError("Capturas HD canceladas.")
        frac = 0.15 + (0.70 * i / max(count - 1, 1))
        ts = min(dur - 0.3, max(0.2, dur * frac))
        out_jpg = out_dir / f"hd_{i + 1}.jpg"
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-ss",
            f"{ts:.2f}",
            "-i",
            str(media.path),
            "-vframes",
            "1",
            "-vf",
            "scale='min(1920,iw)':-2:flags=lanczos",
            "-q:v",
            "2",
            str(out_jpg),
        ]
        await _run_simple_ffmpeg(cmd, cancel_event, register_proc, f"Captura HD {i + 1}/{count}")
        if out_jpg.exists() and out_jpg.stat().st_size > 0:
            results.append((out_jpg, _fmt_ts(ts)))

    if not results:
        raise RuntimeError("No se pudieron extraer capturas HD del video.")
    return results


async def remux_video(
    media: MediaInfo,
    out_dir: Path,
    target_format: Literal["mp4", "mkv"],
    cancel_event: asyncio.Event,
    register_proc: Callable[[asyncio.subprocess.Process | None], None],
    custom_name: str = "",
    strip_metadata: bool = False,
    meta_title: str = "",
    meta_artist: str = "",
) -> tuple[Path, str]:
    """Remux rápido sin recomprimir el video (-c:v copy). Devuelve (ruta, nota_técnica)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(custom_name.strip() or media.path.stem)
    output_path = out_dir / f"{stem}.{target_format}"

    map_args = ["-map", "0:v:0", "-map", "0:a?"]
    video_args = ["-c:v", "copy"]
    audio_args = ["-c:a", "copy"]
    sub_args: list[str] = []
    extra_args: list[str] = []
    note = "Video y audio copiados sin pérdida (0% re-codificación)"

    if target_format == "mp4":
        if media.video_codec.lower() not in MP4_COMPATIBLE_VIDEO:
            raise RuntimeError(
                f"El códec de video '{media.video_codec}' no es compatible con remux directo a MP4. "
                "Usa compresión normal para convertirlo a H.265/H.264/AV1."
            )
        if media.video_codec.lower() in {"hevc", "h265"}:
            video_args.extend(["-tag:v", "hvc1"])

        incompat_audio = [
            a for a in media.audio_tracks if a.codec.lower() not in MP4_COMPATIBLE_AUDIO
        ]
        if incompat_audio:
            audio_args = ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
            note = f"Video copiado sin pérdida · audio ({incompat_audio[0].codec}) convertido a AAC 192k para MP4"

        sub_args = ["-sn"]
        extra_args = ["-movflags", "+faststart"]
    else:
        map_args.extend(["-map", "0:s?"])
        sub_args = ["-c:s", "copy"]

    meta_args: list[str] = []
    if strip_metadata:
        meta_args.extend(["-map_metadata", "-1"])
    if meta_title.strip():
        meta_args.extend(["-metadata", f"title={meta_title.strip()[:96]}"])
    if meta_artist.strip():
        meta_args.extend(["-metadata", f"artist={meta_artist.strip()[:96]}"])

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(media.path),
        *map_args,
        *video_args,
        *audio_args,
        *sub_args,
        *meta_args,
        *extra_args,
        str(output_path),
    ]
    await _run_simple_ffmpeg(cmd, cancel_event, register_proc, f"Remux a {target_format.upper()}")
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"El remux a {target_format.upper()} no produjo un archivo válido.")
    return output_path, note


async def extract_audio(
    media: MediaInfo,
    out_dir: Path,
    audio_format: Literal["opus", "mp3"],
    cancel_event: asyncio.Event,
    register_proc: Callable[[asyncio.subprocess.Process | None], None],
    audio_mode: str = "first",
    bitrate_k: int = 128,
    custom_name: str = "",
) -> Path:
    """Extrae la pista de audio seleccionada en .opus o .mp3."""
    if not media.audio_tracks:
        raise RuntimeError("Este video no tiene ninguna pista de audio para extraer.")

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(custom_name.strip() or media.path.stem)
    ext = "opus" if audio_format == "opus" else "mp3"
    output_path = out_dir / f"{stem}.{ext}"

    track_idx = 0
    if audio_mode.startswith("track:"):
        try:
            track_idx = int(audio_mode.split(":", 1)[1])
        except ValueError:
            track_idx = 0

    br = max(48, min(320, bitrate_k))
    if audio_format == "opus":
        codec_args = ["-c:a", "libopus", "-b:a", f"{br}k", "-vbr", "on", "-ac", "2"]
    else:
        codec_args = ["-c:a", "libmp3lame", "-b:a", f"{br}k", "-ac", "2"]

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(media.path),
        "-vn",
        "-sn",
        "-map",
        f"0:a:{track_idx}?",
        *codec_args,
        str(output_path),
    ]
    await _run_simple_ffmpeg(cmd, cancel_event, register_proc, f"Extraer audio {ext.upper()}")
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"No se pudo extraer el audio en formato {ext.upper()}.")
    return output_path


async def _run_ffmpeg_segment(
    src_path: Path,
    out_dir: Path,
    ext: str,
    seg_time: float,
    cancel_event: asyncio.Event,
    register_proc: Callable[[asyncio.subprocess.Process | None], None],
    label: str,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if ext == ".mkv":
        map_args = ["-map", "0:v:0", "-map", "0:a?", "-map", "0:s?"]
    else:
        map_args = ["-map", "0:v:0", "-map", "0:a?", "-sn"]

    pattern = str(out_dir / f"seg_%03d{ext}")
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(src_path),
        *map_args,
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        f"{max(5.0, seg_time):.2f}",
        "-reset_timestamps",
        "1",
        "-avoid_negative_ts",
        "make_zero",
        pattern,
    ]
    await _run_simple_ffmpeg(cmd, cancel_event, register_proc, label)
    parts = sorted(
        (p for p in out_dir.glob(f"seg_*{ext}") if p.is_file() and p.stat().st_size > 0),
        key=lambda p: p.name,
    )
    if not parts:
        raise RuntimeError("No se pudo generar ningún segmento del video.")
    return parts


async def split_video_for_telegram(
    source_path: Path,
    duration: float,
    out_dir: Path,
    cancel_event: asyncio.Event,
    register_proc: Callable[[asyncio.subprocess.Process | None], None],
    max_part_bytes: int = 1990 * 1024 * 1024,
    target_part_bytes: int = 1820 * 1024 * 1024,
    custom_name: str = "",
    delete_source_after: bool = False,
) -> list[Path]:
    """Divide un video grande sin recomprimir (-c copy) en partes reproducibles <1.90 GiB."""
    total_size = source_path.stat().st_size
    if total_size <= max_part_bytes:
        return [source_path]

    dur = duration if duration > 1.0 else await probe_audio_duration(source_path)
    if dur <= 2.0:
        raise RuntimeError("No se pudo determinar la duración del video para dividirlo por segmentos.")

    ext = source_path.suffix.lower()
    if ext not in {".mp4", ".mkv", ".webm", ".mov"}:
        ext = ".mkv"

    n_parts = max(2, math.ceil(total_size / max(target_part_bytes, 1)))
    seg_time = max(15.0, dur / n_parts)

    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "_raw"
    raw_parts = await _run_ffmpeg_segment(
        src_path=source_path,
        out_dir=raw_dir,
        ext=ext,
        seg_time=seg_time,
        cancel_event=cancel_event,
        register_proc=register_proc,
        label="Dividir video (-c copy)",
    )

    # Verificar que ningún segmento VBR haya excedido el tope de Telegram; subdividir si hace falta.
    checked_parts: list[Path] = []
    for idx, part in enumerate(raw_parts):
        p_size = part.stat().st_size
        if p_size <= max_part_bytes:
            checked_parts.append(part)
            continue

        p_dur = await probe_audio_duration(part)
        if p_dur <= 5.0:
            raise RuntimeError(
                f"Una escena quedó en {p_size / (1024**2):.0f} MiB y no pudo subdividirse más."
            )
        sub_n = max(2, math.ceil(p_size / max(int(target_part_bytes * 0.9), 1)))
        sub_seg_time = max(5.0, p_dur / sub_n)
        sub_dir = out_dir / f"_sub_{idx:03d}"
        sub_parts = await _run_ffmpeg_segment(
            src_path=part,
            out_dir=sub_dir,
            ext=ext,
            seg_time=sub_seg_time,
            cancel_event=cancel_event,
            register_proc=register_proc,
            label=f"Subdividir parte {idx + 1}",
        )
        oversized = [sp for sp in sub_parts if sp.stat().st_size > max_part_bytes]
        if oversized:
            raise RuntimeError(
                f"Un tramo del video supera el límite de Telegram ({oversized[0].stat().st_size / (1024**2):.0f} MiB) incluso tras subdividir."
            )
        checked_parts.extend(sub_parts)
        with contextlib.suppress(OSError):
            part.unlink()

    if delete_source_after and source_path.exists():
        with contextlib.suppress(OSError):
            source_path.unlink()

    stem = _safe_stem(custom_name.strip() or source_path.stem)[:80]
    total_count = len(checked_parts)
    pad_w = max(2, len(str(total_count)))
    final_parts: list[Path] = []
    for i, part in enumerate(checked_parts, start=1):
        final_name = f"{stem}_Parte_{i:0{pad_w}d}_de_{total_count:0{pad_w}d}{ext}"
        dest_part = out_dir / final_name
        part.replace(dest_part)
        final_parts.append(dest_part)

    return final_parts
