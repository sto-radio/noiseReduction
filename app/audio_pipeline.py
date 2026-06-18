import hashlib
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .settings import Settings


class PipelineError(RuntimeError):
    pass


@dataclass
class CommandResult:
    args: list[str]
    stdout: str
    stderr: str
    elapsed_seconds: float


@dataclass
class AudioProcessResult:
    output_path: Path
    input_info: dict[str, Any]
    output_info: dict[str, Any]
    loudnorm: dict[str, Any]
    commands: list[CommandResult]


def process_audio(
    input_path: Path,
    output_dir: Path,
    work_dir: Path,
    output_basename: str,
    settings: Settings,
) -> AudioProcessResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    commands: list[CommandResult] = []
    input_info = probe_audio(input_path, settings)

    prepared_wav = work_dir / "01_prepared_48k.wav"
    enhanced_dir = work_dir / "02_deepfilter"
    enhanced_dir.mkdir(parents=True, exist_ok=True)

    prepare_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ar",
        str(settings.processing_sample_rate),
    ]
    if settings.mono_mixdown:
        prepare_cmd.extend(["-ac", "1"])
    prepare_cmd.extend(["-c:a", "pcm_s16le", str(prepared_wav)])
    commands.append(run_command(prepare_cmd, settings.process_timeout_seconds))

    deepfilter_cmd = [
        settings.deepfilter_command,
        "-m",
        settings.deepfilter_model,
        "-o",
        str(enhanced_dir),
        "--log-level",
        "info",
        "--no-suffix",
    ]
    if settings.deepfilter_postfilter:
        deepfilter_cmd.append("--pf")
    if settings.deepfilter_atten_lim_db is not None:
        deepfilter_cmd.extend(["--atten-lim", str(settings.deepfilter_atten_lim_db)])
    deepfilter_cmd.append(str(prepared_wav))
    commands.append(run_command(deepfilter_cmd, settings.process_timeout_seconds))

    enhanced_wav = enhanced_dir / prepared_wav.name
    if not enhanced_wav.exists():
        enhanced_wav = newest_audio_file(enhanced_dir)

    output_format = normalized_output_format(settings.output_format)
    output_path = output_dir / f"{safe_filename(output_basename)}.{output_format}"
    loudnorm_stats, loudnorm_commands = loudness_normalize(
        enhanced_wav,
        output_path,
        output_format,
        settings,
    )
    commands.extend(loudnorm_commands)

    output_info = probe_audio(output_path, settings)
    return AudioProcessResult(
        output_path=output_path,
        input_info=input_info,
        output_info=output_info,
        loudnorm=loudnorm_stats,
        commands=commands,
    )


def run_command(args: list[str], timeout_seconds: int) -> CommandResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise PipelineError(f"Command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"Command timed out after {timeout_seconds}s: {args[0]}") from exc

    elapsed = time.monotonic() - started
    result = CommandResult(
        args=args,
        stdout=completed.stdout,
        stderr=completed.stderr,
        elapsed_seconds=elapsed,
    )
    if completed.returncode != 0:
        raise PipelineError(
            f"Command failed with exit code {completed.returncode}: {' '.join(args)}\n"
            f"{completed.stderr[-4000:]}"
        )
    return result


def loudness_normalize(
    input_path: Path,
    output_path: Path,
    output_format: str,
    settings: Settings,
) -> tuple[dict[str, Any], list[CommandResult]]:
    target = trim_float(settings.target_loudness_lufs)
    peak = trim_float(settings.true_peak_dbtp)
    lra = trim_float(settings.lra)
    measure_filter = f"loudnorm=I={target}:TP={peak}:LRA={lra}:print_format=json"
    measure_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-i",
        str(input_path),
        "-af",
        measure_filter,
        "-f",
        "null",
        "-",
    ]
    measure_result = run_command(measure_cmd, settings.process_timeout_seconds)
    stats = parse_loudnorm_json(measure_result.stderr)

    normalize_filter = (
        "loudnorm="
        f"I={target}:TP={peak}:LRA={lra}:"
        f"measured_I={stats['input_i']}:"
        f"measured_TP={stats['input_tp']}:"
        f"measured_LRA={stats['input_lra']}:"
        f"measured_thresh={stats['input_thresh']}:"
        f"offset={stats['target_offset']}:"
        "linear=true:print_format=summary"
    )
    normalize_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(input_path),
        "-af",
        normalize_filter,
        "-ar",
        str(settings.processing_sample_rate),
    ]
    if settings.mono_mixdown:
        normalize_cmd.extend(["-ac", "1"])
    normalize_cmd.extend(codec_args(output_format, settings))
    normalize_cmd.append(str(output_path))
    normalize_result = run_command(normalize_cmd, settings.process_timeout_seconds)
    return stats, [measure_result, normalize_result]


def parse_loudnorm_json(stderr: str) -> dict[str, Any]:
    matches = re.findall(r"\{[\s\S]*?\}", stderr)
    for candidate in reversed(matches):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        required = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
        if required.issubset(data):
            return data
    raise PipelineError("ffmpeg loudnorm did not return parseable JSON measurements")


def probe_audio(path: Path, settings: Settings) -> dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,channels,sample_rate,bit_rate,duration",
        "-show_entries",
        "format=duration,bit_rate,format_name",
        "-of",
        "json",
        str(path),
    ]
    result = run_command(cmd, settings.process_timeout_seconds)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"ffprobe returned invalid JSON for {path}") from exc

    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    duration = stream.get("duration") or fmt.get("duration")
    bitrate = stream.get("bit_rate") or fmt.get("bit_rate")
    return {
        "filename": path.name,
        "format": fmt.get("format_name") or stream.get("codec_name") or "",
        "codec": stream.get("codec_name") or "",
        "channels": as_int(stream.get("channels")),
        "samplerate": as_int(stream.get("sample_rate")),
        "bitrate": as_float(bitrate),
        "length": as_float(duration),
        "length_timestring": seconds_to_timestring(as_float(duration)),
        "size": path.stat().st_size if path.exists() else 0,
    }


def codec_args(output_format: str, settings: Settings) -> list[str]:
    if output_format == "wav":
        return ["-c:a", "pcm_s16le"]
    if output_format == "flac":
        return ["-c:a", "flac"]
    if output_format == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", settings.mp3_bitrate]
    if output_format in {"m4a", "aac"}:
        return ["-c:a", "aac", "-b:a", settings.aac_bitrate]
    raise PipelineError(f"Unsupported output format: {output_format}")


def normalized_output_format(value: str) -> str:
    value = value.lower().strip().lstrip(".")
    if value == "wave":
        return "wav"
    if value == "mp4":
        return "m4a"
    if value in {"wav", "flac", "mp3", "m4a", "aac"}:
        return value
    raise PipelineError(f"Unsupported output format: {value}")


def newest_audio_file(directory: Path) -> Path:
    candidates = [p for p in directory.iterdir() if p.is_file()]
    if not candidates:
        raise PipelineError(f"DeepFilterNet did not create an output file in {directory}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "clean_audio"


def trim_float(value: float) -> str:
    return f"{value:g}"


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def seconds_to_timestring(seconds: float | None) -> str:
    if seconds is None:
        return "00:00:00.000"
    millis = int(round(seconds * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def size_string(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} B"
    return f"{value:.1f} {unit}"


def reset_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
