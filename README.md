# video-compression

Generates GPU-accelerated `ffmpeg` commands for a folder of videos. Each video is inspected with `ffprobe`, an LLM on [Ollama Cloud](https://ollama.com) chooses the encoding settings, and every command is written to a PowerShell script for you to review and run.

Nothing is encoded automatically. The tool only produces `scripts/encode.ps1`.

## How it works

```
input/*.mp4 ──► get_files_to_encode
                    │  one parallel worker per file (LangGraph Send)
                    ▼
                probe_and_plan
                    1. ffprobe → trimmed JSON metadata
                    2. LLM (Ollama Cloud) → structured FfmpegPlan
                    3. validate + substitute real paths
                    │  fan-in
                    ▼
                write_script ──► scripts/encode.ps1
```

The LLM is told to:

- **Scale:** downscale anything larger than 720p to 720 on the short side, keeping the aspect ratio. It never upscales.
- **Hardware acceleration:** decode and scale on the GPU (`-hwaccel cuda -hwaccel_output_format cuda`, `scale_cuda`) and encode with `hevc_nvenc` (preset `p7`, `-tune hq`, constant quality `-cq` 22–24, spatial/temporal AQ).
- **Audio:** copy AAC/Opus at 160 kb/s or less, and re-encode anything else to AAC 160k.
- **Skip:** files that are already HEVC/AV1, 720p or smaller, at a modest bitrate.
- **Fallback:** use `libx265` when NVENC isn't available.

### Modes

`--mode` chooses the prompt and rules. You can also set `VIDEO_MODE` in `.env`; the CLI flag wins.

| Mode | Goal | Resolution | Size control |
|---|---|---|---|
| `compress` (default) | smaller files with good quality (rules above) | downscale to 720p, never upscale | bitrate capped at 70% of source; lean sources skipped |
| `enhance` | best image quality at a reasonable size | always 720p: downscale **and upscale** | bitrate capped at 90% of source, clamped to 1500–4000 kb/s; nothing skipped |

In `enhance` mode the LLM is told to:

- **Clean up:** deinterlace (`bwdif`) when needed, lightly denoise (`hqdn3d`) older codecs and upscaled sources, scale with `lanczos`, and sharpen gently (`unsharp`) after scaling.
- **Encode:** decode on the GPU (`-hwaccel cuda`), run the filters on the CPU, and encode 10-bit `hevc_nvenc` (`main10`, `-cq` 20–22, lookahead, B-frame refs). The `libx265` fallback is 10-bit, CRF 19–21.

```powershell
uv run video-compression --mode enhance
```

Example `enhance` command for a 1080p H.264 source:

```powershell
ffmpeg -hide_banner -n -hwaccel cuda -i 'C:\...\input\clip.mp4' -vf 'hqdn3d=1.5:1.5:6:6,scale=-2:720:flags=lanczos,unsharp=5:5:0.4:3:3:0' -c:v hevc_nvenc -preset p7 -tune hq -rc vbr -cq 21 -b:v 0 -spatial-aq 1 -temporal-aq 1 -rc-lookahead 32 -bf 4 -b_ref_mode middle -pix_fmt p010le -profile:v main10 -c:a copy -maxrate 4000k -bufsize 8000k 'C:\...\output\clip.mp4'
```

The upscaled output can be larger than a small, low-bitrate source, because the bitrate floor (1500 kb/s) is higher than the source bitrate.

### Safety

- **No paths sent to the model:** it never sees or writes real file paths. It returns `{input}` / `{output}` placeholders, which are replaced locally.
- **Only stream details leave your machine:** `ffprobe` tags such as titles and filenames are stripped before anything is sent to the cloud.
- **Every answer is checked:** placeholders must appear exactly once each, with no shell characters and no `-y`. The container must be mp4, mkv, mov or webm, and the output must stay inside `output/`. If a check fails, the model is asked once more with the error.
- **No overwrites:** every command uses `-n`, so existing output files are never overwritten.

## Requirements

- [uv](https://docs.astral.sh/uv/) and Python 3.13
- `ffmpeg` and `ffprobe` on `PATH`, for example `winget install Gyan.FFmpeg`
- An NVIDIA GPU with NVENC for the hardware path (otherwise `libx265` is used)
- An Ollama Cloud API key from <https://ollama.com/settings/keys>

## Setup

```powershell
uv sync
Copy-Item .env.example .env   # then put your real key in .env
```

`.env`:

```ini
OLLAMA_API_KEY=your-ollama-api-key
OLLAMA_MODEL=gpt-oss:120b                 # optional
FFMPEG_ENCODERS=libx265,hevc_nvenc        # optional: encoders the LLM may use
VIDEO_MODE=compress                       # optional: compress | enhance
```

`.env` is git-ignored. Never commit your key.

## Usage

1. Put videos in `input/`.
2. Generate the script. Run this from the project root, because paths are resolved from the current directory:
   ```powershell
   uv run video-compression
   ```
   The console shows a summary per file (source codec, resolution, bitrate and size, the LLM's reasoning, and the command).
3. Review `scripts/encode.ps1`, then run it:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\encode.ps1
   ```
   Encoded files are written to `output/`.

Example generated script:

```powershell
New-Item -ItemType Directory -Force -Path C:\...\video-compression\output | Out-Null

# --- clip.mp4
# Source is 1080p H.264; downscale to 720p and re-encode to HEVC on the GPU; AAC audio copied.
ffmpeg -hide_banner -n -hwaccel cuda -hwaccel_output_format cuda -i 'C:\...\input\clip.mp4' -vf scale_cuda=-2:720 -c:v hevc_nvenc -preset p7 -tune hq -rc vbr -cq 23 -b:v 0 -spatial-aq 1 -temporal-aq 1 -c:a copy 'C:\...\output\clip.mp4'
```

## Configuration

| Setting | Where | Default |
|---|---|---|
| Ollama API key | `OLLAMA_API_KEY` in `.env` | required |
| Model | `OLLAMA_MODEL` in `.env` | `gpt-oss:120b` |
| Mode | `--mode` flag or `VIDEO_MODE` in `.env` | `compress` |
| Allowed encoders | `FFMPEG_ENCODERS` in `.env` (only those in your ffmpeg build are kept) | `libx265,hevc_nvenc` |
| Input / output / script paths | `main.py` | `input/`, `output/`, `scripts/encode.ps1` |
| Parallel LLM calls | `max_concurrency` in `main.py` | `2` |
| Encoding rules (resolution, quality, audio, skip) | `SYSTEM_PROMPT` / `ENHANCE_SYSTEM_PROMPT` in `prompts.py` | see above |
| Enhance bitrate cap | `MAXRATE_RATIO_ENHANCE`, `ENHANCE_MIN_KBPS`, `ENHANCE_MAX_KBPS` in `tools.py` | `0.90`, `1500`, `4000` |

To list the models your key can use:

```powershell
curl.exe -s -H "Authorization: Bearer $env:OLLAMA_API_KEY" https://ollama.com/api/tags
```

## Project layout

```
src/video_compression/
  main.py     entry point: loads .env, builds state, runs the graph, prints results
  graph.py    LangGraph: list files → parallel probe_and_plan → write_script
  state.py    OverallState, per-file VideoState, FileResult
  llm.py      ChatOllama client for Ollama Cloud with structured output
  prompts.py  system prompts (compress / enhance) with the encoding rules
  schemas.py  FfmpegPlan (Pydantic) returned by the LLM
  tools.py    run_command, ffprobe summary, encoder detection, command validation
  script.py   renders scripts/encode.ps1
input/        source videos (git-ignored)
output/       encoded videos (git-ignored)
scripts/      generated scripts (git-ignored)
```

## Notes

- **Full paths in the script:** the generated script contains absolute paths, including your user folder. Keep that in mind before sharing it.
- **Quality vs. size:** `-cq 23` prioritises quality. For smaller files, raise the range in `prompts.py`, for example to 26–28.
- **Model output:** `gpt-oss:120b` on Ollama Cloud doesn't strictly enforce the JSON schema. The prompt includes an explicit example, and `args` returned as a single string is split automatically.
