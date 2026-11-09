import argparse
import os
import subprocess
import sys

from dotenv import load_dotenv

from video_compression.graph import create_graph
from video_compression.state import FileResult, OverallState
from video_compression.tools import available_encoders

DEFAULT_ENCODERS = "libx265,hevc_nvenc"
MODES = ("compress", "enhance")
DEFAULT_INPUT_DIR = "input"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="video-compression",
        description="Generate ffmpeg commands for a folder of videos.",
    )
    parser.add_argument(
        "--input",
        default=None,
        metavar="DIR",
        help=(
            f"Folder with the source videos (default: {DEFAULT_INPUT_DIR}). When given, "
            f"the script first copies each file into {DEFAULT_INPUT_DIR}/ and encodes that copy."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=None,
        help=(
            "compress: smaller files, downscale to 720p (default). "
            "enhance: best image quality at 720p (up- or downscaled), reasonable size. "
            "Falls back to VIDEO_MODE in .env."
        ),
    )
    return parser.parse_args(argv)


def main():
    # Windows consoles default to cp1252; LLM text often has Unicode punctuation.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    load_dotenv()  # OLLAMA_API_KEY, OLLAMA_MODEL, FFMPEG_ENCODERS, VIDEO_MODE

    mode = args.mode or os.environ.get("VIDEO_MODE", "compress").strip().lower()
    if mode not in MODES:
        raise SystemExit(f"VIDEO_MODE must be one of {MODES}, got {mode!r}.")

    default_input = os.path.abspath(DEFAULT_INPUT_DIR)
    input_dir = os.path.abspath(args.input) if args.input else default_input
    if not os.path.isdir(input_dir):
        raise SystemExit(f"Input folder not found: {input_dir}")
    # Only with --input (pointing elsewhere): the script copies sources into input/ first.
    staging_dir = default_input if os.path.normcase(input_dir) != os.path.normcase(default_input) else None

    wanted = os.environ.get("FFMPEG_ENCODERS", DEFAULT_ENCODERS).split(",")
    encoders = available_encoders([e.strip() for e in wanted if e.strip()])
    if not encoders:
        raise SystemExit(f"None of the encoders {wanted} are available in this ffmpeg build.")

    state: OverallState = {
        # Absolute paths so the generated script works from any directory.
        "input_dir": input_dir,
        "staging_dir": staging_dir,
        "output_dir": os.path.abspath("output"),
        "encoders": encoders,
        "mode": mode,
        "script_path": os.path.abspath(os.path.join("scripts", "encode.ps1")),
        "results": [],
    }
    graph = create_graph()
    # Limit parallel Ollama Cloud calls to stay under rate limits.
    result = graph.invoke(state, {"max_concurrency": 2})
    print_result(result)


def summarize_probe(probe: dict) -> str:
    fmt = probe.get("format", {})
    parts = []
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video":
            parts.append(f"{s.get('codec_name')} {s.get('width')}x{s.get('height')}")
        elif s.get("codec_type") == "audio":
            parts.append(f"{s.get('codec_name')} {s.get('channels')}ch")
    if "bit_rate" in fmt:
        parts.append(f"{int(fmt['bit_rate']) // 1000} kb/s")
    if "size" in fmt:
        parts.append(f"{int(fmt['size']) / 1024**3:.2f} GB")
    return " | ".join(parts)


def print_result(result: OverallState) -> None:
    results: list[FileResult] = result.get("results", [])
    print(f"\nInput:    {result['input_dir']}")
    print(f"Output:   {result['output_dir']}")
    print(f"Encoders: {', '.join(result['encoders'])}")
    print(f"Mode:     {result['mode']}")
    print(f"Files:    {len(results)}")

    for i, r in enumerate(results, start=1):
        print(f"\n{'=' * 80}")
        print(f"[{i}/{len(results)}] {r['file']}")
        if "probe" in r:
            print(f"  Source:  {summarize_probe(r['probe'])}")
        if "reasoning" in r:
            print(f"  Plan:    {r['reasoning']}")
        if r.get("skipped"):
            print("  Result:  skipped")
        elif "command" in r:
            print(f"  Command: {subprocess.list2cmdline(r['command'])}")
        if "error" in r:
            print(f"  Error:   {r['error']}")

    print(f"\nScript written to {result['script_path']} (not executed)")


if __name__ == "__main__":
    main()
