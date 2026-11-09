import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class CommandResult:
    command: str | list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(
    command: str | list[str], timeout: float | None = None
) -> CommandResult:
    """Run a command on the OS and capture its output.

    `command` can be a list of args (safest) or a single string. No shell is
    used, so pipes/redirects aren't supported, but paths with spaces are safe
    as long as they're quoted in the string or passed as separate list items.

    Raises FileNotFoundError if the program isn't on PATH and
    subprocess.TimeoutExpired if `timeout` seconds elapse.
    """
    args = command
    # On Windows, CreateProcess parses a quoted string itself. Elsewhere, split it.
    if isinstance(command, str) and sys.platform != "win32":
        args = shlex.split(command)

    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return CommandResult(
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


# --- ffprobe -----------------------------------------------------------------

_FORMAT_KEYS = ("format_name", "duration", "size", "bit_rate")
_STREAM_KEYS = (
    "codec_type", "codec_name", "profile", "width", "height", "pix_fmt",
    "avg_frame_rate", "field_order", "bit_rate", "color_space", "color_transfer",
    "color_primaries", "channels", "channel_layout", "sample_rate",
)


def probe_video(path: str) -> dict:
    """Return a trimmed ffprobe summary of `path` (no tags, so no titles/filenames)."""
    result = run_command(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        timeout=60,
    )
    if not result.ok:
        raise ValueError(f"ffprobe failed: {result.stderr.strip() or result.returncode}")

    data = json.loads(result.stdout)
    fmt = data.get("format", {})
    return {
        "format": {k: fmt[k] for k in _FORMAT_KEYS if k in fmt},
        "streams": [
            {k: s[k] for k in _STREAM_KEYS if k in s}
            for s in data.get("streams", [])
            if s.get("codec_type") in ("video", "audio", "subtitle")
        ],
    }


# --- deciding whether re-encoding is worth it ------------------------------------

TARGET_SHORT_SIDE = 720
EFFICIENT_CODECS = {"hevc", "av1", "vp9"}
# Bits per pixel per frame at or below which a source is already "lean": re-encoding
# it without downscaling saves little and constant-quality modes may even grow it.
SKIP_BPP_EFFICIENT = 0.10  # HEVC / AV1 / VP9
SKIP_BPP_OTHER = 0.07  # H.264, MPEG-4, ...
# Encoded video bitrate is capped at this fraction of the source video bitrate.
MAXRATE_RATIO = 0.70
# Enhance mode caps, as a fraction of the source video bitrate. Upscaling adds
# pixels but no real detail, so a modest budget looks nearly the same as an
# uncapped encode: measured on a 360x202 H.264 source, 1.8x gave a 1.6x file at
# SSIM 0.995 vs 0.997 for a 4x file. Keep it below 2 so files never double.
ENHANCE_UPSCALE_RATIO = 1.8
MAXRATE_RATIO_ENHANCE = 0.90  # downscaling / already 720p: never grow
ENHANCE_MAX_KBPS = 4000  # ceiling for 720p 10-bit HEVC, also used if bitrate is unknown


@dataclass
class SourceAssessment:
    codec: str | None
    width: int | None
    height: int | None
    video_kbps: int | None
    bpp: float | None
    needs_downscale: bool  # in enhance mode: needs any rescale to 720p
    max_video_kbps: int | None  # cap to force on the encode; None if unknown
    skip_reason: str | None  # set when the file should not be re-encoded


def _parse_fps(rate: str | None) -> float | None:
    try:
        num, den = (rate or "").split("/")
        return float(num) / float(den) if float(den) else None
    except ValueError:
        return None


def assess_source(probe: dict, mode: str = "compress") -> SourceAssessment:
    """Decide deterministically (no LLM) whether a file is worth re-encoding.

    In "enhance" mode every video is re-encoded (it is always scaled to 720p) and
    the bitrate cap is relative to the source: ENHANCE_UPSCALE_RATIO when
    upscaling, MAXRATE_RATIO_ENHANCE otherwise, at most ENHANCE_MAX_KBPS.
    """
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return SourceAssessment(None, None, None, None, None, False, None, "no video stream")

    codec = video.get("codec_name")
    w, h = video.get("width"), video.get("height")
    fps = _parse_fps(video.get("avg_frame_rate"))

    # Prefer the stream's own bitrate; otherwise container bitrate minus audio.
    bps = int(video["bit_rate"]) if video.get("bit_rate") else None
    if bps is None and probe.get("format", {}).get("bit_rate"):
        audio = sum(int(s.get("bit_rate", 0)) for s in streams if s.get("codec_type") == "audio")
        bps = int(probe["format"]["bit_rate"]) - audio
    video_kbps = bps // 1000 if bps else None

    bpp = bps / (w * h * fps) if bps and w and h and fps else None
    needs_downscale = bool(w and h and min(w, h) > TARGET_SHORT_SIDE)
    if mode == "enhance":
        needs_downscale = bool(w and h and min(w, h) != TARGET_SHORT_SIDE)  # up or down
        upscale = bool(w and h and min(w, h) < TARGET_SHORT_SIDE)
        ratio = ENHANCE_UPSCALE_RATIO if upscale else MAXRATE_RATIO_ENHANCE
        cap = int(video_kbps * ratio) if video_kbps else ENHANCE_MAX_KBPS
        max_video_kbps = min(cap, ENHANCE_MAX_KBPS)
        return SourceAssessment(codec, w, h, video_kbps, bpp, needs_downscale, max_video_kbps, None)

    max_video_kbps = int(video_kbps * MAXRATE_RATIO) if video_kbps else None

    skip_reason = None
    if not needs_downscale and bpp is not None:
        limit = SKIP_BPP_EFFICIENT if codec in EFFICIENT_CODECS else SKIP_BPP_OTHER
        if bpp <= limit:
            skip_reason = (
                f"{codec} {w}x{h} at {video_kbps} kb/s ({bpp:.3f} bits/pixel/frame, "
                f"limit {limit}) is already lean and needs no downscaling; "
                "re-encoding would save little or grow the file."
            )

    return SourceAssessment(codec, w, h, video_kbps, bpp, needs_downscale, max_video_kbps, skip_reason)


def available_encoders(wanted: list[str]) -> list[str]:
    """Filter `wanted` to the encoders this ffmpeg build actually has."""
    result = run_command(["ffmpeg", "-hide_banner", "-encoders"], timeout=30)
    listed = {line.split()[1] for line in result.stdout.splitlines() if len(line.split()) > 1}
    return [e for e in wanted if e in listed]


# --- ffmpeg command validation -------------------------------------------------

_ALLOWED_EXTENSIONS = {"mp4", "mkv", "mov", "webm"}
_SHELL_CHARS = set("|;&<>`$\n")
_FORBIDDEN_ARGS = {"-y", "-n"}


def _strip_option(args: list[str], *names: str) -> list[str]:
    """Remove options (and their value) such as '-maxrate 2M' or '-maxrate:v 2M'."""
    out, skip_next = [], False
    for a in args:
        if skip_next:
            skip_next = False
        elif a.split(":")[0] in names:
            skip_next = True
        else:
            out.append(a)
    return out


def build_ffmpeg_command(
    args: list[str],
    output_extension: str,
    input_path: str,
    output_dir: str,
    max_video_kbps: int | None = None,
) -> list[str]:
    """Validate LLM-produced args and substitute real paths.

    If max_video_kbps is given, any model-provided -maxrate/-bufsize is replaced
    with that cap so the output video can't exceed it.

    Raises ValueError with a message the LLM can act on if anything is off.
    """
    args = list(args)
    if args and args[0].lower() in ("ffmpeg", "ffmpeg.exe"):
        args = args[1:]
    if max_video_kbps:
        args = _strip_option(args, "-maxrate", "-bufsize")
        if "{output}" in args:
            i = args.index("{output}")
            args[i:i] = ["-maxrate", f"{max_video_kbps}k", "-bufsize", f"{max_video_kbps * 2}k"]

    if args.count("{input}") != 1 or args.count("{output}") != 1:
        raise ValueError("args must contain '{input}' and '{output}' exactly once each, as separate items.")
    i = args.index("{input}")
    if i == 0 or args[i - 1] != "-i":
        raise ValueError("'{input}' must come directly after '-i'.")
    if any(a in _FORBIDDEN_ARGS for a in args):
        raise ValueError("Do not include -y or -n; overwrite behaviour is set by the program.")
    if bad := [a for a in args if _SHELL_CHARS & set(a)]:
        raise ValueError(f"args contain shell characters, which are not allowed: {bad}")

    ext = output_extension.lower().lstrip(".")
    if ext not in _ALLOWED_EXTENSIONS:
        raise ValueError(f"output_extension must be one of {sorted(_ALLOWED_EXTENSIONS)}.")

    stem = os.path.splitext(os.path.basename(input_path))[0]
    output_path = os.path.join(output_dir, f"{stem}.{ext}")
    out_root = os.path.realpath(output_dir)
    if os.path.commonpath([out_root, os.path.realpath(output_path)]) != out_root:
        raise ValueError("output path escapes output_dir")

    resolved = [input_path if a == "{input}" else output_path if a == "{output}" else a for a in args]
    # -n: never overwrite an existing output file.
    return ["ffmpeg", "-hide_banner", "-n", *resolved]
