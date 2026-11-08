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
    "avg_frame_rate", "bit_rate", "color_space", "color_transfer",
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


def available_encoders(wanted: list[str]) -> list[str]:
    """Filter `wanted` to the encoders this ffmpeg build actually has."""
    result = run_command(["ffmpeg", "-hide_banner", "-encoders"], timeout=30)
    listed = {line.split()[1] for line in result.stdout.splitlines() if len(line.split()) > 1}
    return [e for e in wanted if e in listed]


# --- ffmpeg command validation -------------------------------------------------

_ALLOWED_EXTENSIONS = {"mp4", "mkv", "mov", "webm"}
_SHELL_CHARS = set("|;&<>`$\n")
_FORBIDDEN_ARGS = {"-y", "-n"}


def build_ffmpeg_command(
    args: list[str], output_extension: str, input_path: str, output_dir: str
) -> list[str]:
    """Validate LLM-produced args and substitute real paths.

    Raises ValueError with a message the LLM can act on if anything is off.
    """
    args = list(args)
    if args and args[0].lower() in ("ffmpeg", "ffmpeg.exe"):
        args = args[1:]

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
