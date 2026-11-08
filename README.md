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
| Allowed encoders | `FFMPEG_ENCODERS` in `.env` (only those in your ffmpeg build are kept) | `libx265,hevc_nvenc` |
| Input / output / script paths | `main.py` | `input/`, `output/`, `scripts/encode.ps1` |
| Parallel LLM calls | `max_concurrency` in `main.py` | `2` |
| Encoding rules (resolution, quality, audio, skip) | `SYSTEM_PROMPT` in `prompts.py` | see above |

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
  prompts.py  system prompt with the encoding rules
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
