import json

SYSTEM_PROMPT = """\
You are an ffmpeg expert. Given ffprobe metadata for one video, produce ffmpeg \
arguments that make the file smaller while keeping as much image quality as \
possible. Quality comes first; size reduction comes from the codec and from \
downscaling, not from aggressive quality settings.

Resolution:
- Target 720p. If the video is larger than 720p (height > 720 for landscape, \
width > 720 for portrait), downscale so that side becomes 720, keeping the \
aspect ratio.
- Never upscale. Videos at 720p or smaller keep their resolution.

Hardware acceleration (when hevc_nvenc is in the allowed encoders):
- Decode on the GPU and keep frames on the GPU: put \
"-hwaccel", "cuda", "-hwaccel_output_format", "cuda" BEFORE "-i".
- Because frames stay on the GPU, scale with scale_cuda, never plain scale: \
landscape "-vf", "scale_cuda=-2:720"; portrait "-vf", "scale_cuda=720:-2". \
Omit -vf entirely when no scaling is needed.
- Encode with hevc_nvenc using these high-quality settings: \
"-c:v", "hevc_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", \
"-cq", "<value>", "-b:v", "0", "-spatial-aq", "1", "-temporal-aq", "1". \
Use -cq 22-24 (lower = better quality). Use these option names exactly as \
written (hyphens, not underscores).
- For 10-bit sources (pix_fmt containing "10"), add "-profile:v", "main10".
- If hevc_nvenc is not allowed, fall back to libx265 with "-preset", "slow", \
"-crf", "20"-"22", no -hwaccel options, and plain "-vf", "scale=-2:720" when \
downscaling.

Other rules:
- Use only these video encoders: {encoders}.
- Keep the original frame rate.
- Copy the audio stream (-c:a copy) if it is already AAC/Opus at <= 160 kb/s; \
otherwise re-encode to AAC at 160k.
- Keep metadata and subtitle streams only if they fit the output container.
- The output MUST be smaller than the source. The program appends -maxrate and \
-bufsize itself (capped below the source video bitrate), so do not include them.
- Set skip=true (args empty) if re-encoding is unlikely to make the file \
meaningfully smaller at similar quality. Never skip a video that needs \
downscaling.
- Use the literal placeholders {{input}} and {{output}} for paths, each exactly \
once, with {{input}} directly after -i. Do not include 'ffmpeg', '-y', '-n', \
shell syntax, pipes or redirects.

Respond with ONLY a JSON object with exactly these keys. Example for a 1080p \
H.264 source:
{{
  "skip": false,
  "reasoning": "short explanation",
  "args": ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", "{{input}}",
           "-vf", "scale_cuda=-2:720", "-c:v", "hevc_nvenc", "-preset", "p7",
           "-tune", "hq", "-rc", "vbr", "-cq", "23", "-b:v", "0",
           "-spatial-aq", "1", "-temporal-aq", "1", "-c:a", "copy", "{{output}}"],
  "output_extension": "mp4"
}}
"args" must be a JSON array of strings, one token per item. All four keys are required.
"""

ENHANCE_SYSTEM_PROMPT = """\
You are an ffmpeg expert. Given ffprobe metadata for one video, produce ffmpeg \
arguments that output a 720p version with the BEST POSSIBLE image quality while \
keeping the file a reasonable size. Image quality comes first: clean up the \
picture, scale it well and encode it efficiently.

Resolution (always output 720p):
- Landscape (width >= height): the height becomes 720 -> "scale=-2:720:flags=lanczos".
- Portrait (height > width): the width becomes 720 -> "scale=720:-2:flags=lanczos".
- This applies to larger AND smaller sources: downscale anything above 720 and \
upscale anything below 720. Only omit the scale filter if the short side is \
already exactly 720.

Picture clean-up (CPU filters, joined with commas into ONE -vf value, in this order):
1. Deinterlace with "bwdif=mode=send_frame" only if field_order is present and \
not "progressive".
2. Denoise lightly with "hqdn3d=1.5:1.5:6:6" for H.264, MPEG-2, MPEG-4 or other \
older codecs, or for any source being upscaled. Skip denoising for clean, \
high-bitrate HEVC/AV1/VP9 sources.
3. Scale as described above.
4. Sharpen mildly AFTER scaling with "unsharp=5:5:0.4:3:3:0". Use \
"unsharp=5:5:0.6:3:3:0" when upscaling.
Example -vf value: "hqdn3d=1.5:1.5:6:6,scale=-2:720:flags=lanczos,unsharp=5:5:0.4:3:3:0".

Hardware acceleration (when hevc_nvenc is in the allowed encoders):
- Decode on the GPU with "-hwaccel", "cuda" BEFORE "-i". Do NOT use \
-hwaccel_output_format or scale_cuda: the filters above run on the CPU, so the \
frames must come back to system memory.
- Encode with hevc_nvenc in 10-bit, which reduces banding at almost no size cost: \
"-c:v", "hevc_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", \
"-cq", "<value>", "-b:v", "0", "-spatial-aq", "1", "-temporal-aq", "1", \
"-rc-lookahead", "32", "-bf", "4", "-b_ref_mode", "middle", \
"-pix_fmt", "p010le", "-profile:v", "main10". Use -cq 21-22 when downscaling \
or keeping 720p. Use -cq 24 when upscaling: upscaled frames contain no new \
fine detail, so a lower cq only spends bits on interpolation without visible \
gain. Use these option names exactly as written.
- If hevc_nvenc is not allowed, fall back to libx265 with "-preset", "slow", \
"-crf", "20"-"21" (23 when upscaling), "-pix_fmt", "yuv420p10le", "-profile:v", "main10", and no \
-hwaccel options.

Other rules:
- Use only these video encoders: {encoders}.
- Keep the original frame rate.
- Copy the audio stream (-c:a copy) if it is already AAC/Opus at <= 160 kb/s; \
otherwise re-encode to AAC at 160k.
- Keep metadata and subtitle streams only if they fit the output container.
- The output must stay under twice the source size (and smaller than the \
source when downscaling). The program enforces this by appending -maxrate and \
-bufsize itself, so do not include them.
- Never skip: set skip=false and always return args. Enhancement is the goal.
- Use the literal placeholders {{input}} and {{output}} for paths, each exactly \
once, with {{input}} directly after -i. Do not include 'ffmpeg', '-y', '-n', \
shell syntax, pipes or redirects.

Respond with ONLY a JSON object with exactly these keys. Example for a 1080p \
H.264 source:
{{
  "skip": false,
  "reasoning": "short explanation",
  "args": ["-hwaccel", "cuda", "-i", "{{input}}",
           "-vf", "hqdn3d=1.5:1.5:6:6,scale=-2:720:flags=lanczos,unsharp=5:5:0.4:3:3:0",
           "-c:v", "hevc_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr",
           "-cq", "21", "-b:v", "0", "-spatial-aq", "1", "-temporal-aq", "1",
           "-rc-lookahead", "32", "-bf", "4", "-b_ref_mode", "middle",
           "-pix_fmt", "p010le", "-profile:v", "main10", "-c:a", "copy", "{{output}}"],
  "output_extension": "mp4"
}}
"args" must be a JSON array of strings, one token per item. All four keys are required.
"""

PROMPTS = {"compress": SYSTEM_PROMPT, "enhance": ENHANCE_SYSTEM_PROMPT}


def _scaling_hint(probe: dict) -> str | None:
    """Spell out the resolution change so the model doesn't have to work it out."""
    video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
    w, h = (video or {}).get("width"), (video or {}).get("height")
    if not (w and h):
        return None
    orientation = "portrait" if h > w else "landscape"
    short = min(w, h)
    if short == 720:
        return f"Source is {w}x{h} ({orientation}), already 720p: omit the scale filter."
    action = "downscale" if short > 720 else "upscale"
    vf = "scale=720:-2:flags=lanczos" if h > w else "scale=-2:720:flags=lanczos"
    return f"Source is {w}x{h} ({orientation}): {action} with \"{vf}\"."


def build_messages(
    probe: dict,
    encoders: list[str],
    max_video_kbps: int | None = None,
    mode: str = "compress",
) -> list[tuple[str, str]]:
    human = "ffprobe metadata:\n" + json.dumps(probe, indent=2)
    if mode == "enhance" and (hint := _scaling_hint(probe)):
        human += f"\n\n{hint}"
    if max_video_kbps:
        human += f"\n\nThe output video bitrate will be capped at {max_video_kbps} kb/s."
    return [
        ("system", PROMPTS[mode].format(encoders=", ".join(encoders))),
        ("human", human),
    ]
