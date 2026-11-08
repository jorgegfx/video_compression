import os
import subprocess
import sys

from dotenv import load_dotenv

from video_compression.graph import create_graph
from video_compression.state import FileResult, OverallState
from video_compression.tools import available_encoders

DEFAULT_ENCODERS = "libx265,hevc_nvenc"


def main():
    # Windows consoles default to cp1252; LLM text often has Unicode punctuation.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()  # OLLAMA_API_KEY, OLLAMA_MODEL, FFMPEG_ENCODERS

    wanted = os.environ.get("FFMPEG_ENCODERS", DEFAULT_ENCODERS).split(",")
    encoders = available_encoders([e.strip() for e in wanted if e.strip()])
    if not encoders:
        raise SystemExit(f"None of the encoders {wanted} are available in this ffmpeg build.")

    state: OverallState = {
        # Absolute paths so the generated script works from any directory.
        "input_dir": os.path.abspath("input"),
        "output_dir": os.path.abspath("output"),
        "encoders": encoders,
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
    print(f"Files:    {len(results)}")

    for i, r in enumerate(results, start=1):
        print(f"\n{'=' * 80}")
        print(f"[{i}/{len(results)}] {r['file']}")
        if "probe" in r:
            print(f"  Source:  {summarize_probe(r['probe'])}")
        if "reasoning" in r:
            print(f"  Plan:    {r['reasoning']}")
        if r.get("skipped"):
            print("  Result:  skipped (already efficient)")
        elif "command" in r:
            print(f"  Command: {subprocess.list2cmdline(r['command'])}")
        if "error" in r:
            print(f"  Error:   {r['error']}")

    print(f"\nScript written to {result['script_path']} (not executed)")


if __name__ == "__main__":
    main()
