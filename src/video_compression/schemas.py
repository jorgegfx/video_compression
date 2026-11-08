import shlex

from pydantic import BaseModel, Field, field_validator


class FfmpegPlan(BaseModel):
    """Structured answer the LLM must return for one video."""

    skip: bool = Field(
        description=(
            "True if re-encoding would not meaningfully reduce size "
            "(e.g. already efficient codec at a low bitrate)."
        )
    )
    reasoning: str = Field(
        description="One or two sentences explaining the codec/quality choices or why it was skipped."
    )
    args: list[str] = Field(
        default_factory=list,
        description=(
            "ffmpeg arguments, one token per item, WITHOUT the leading 'ffmpeg'. "
            "Use the literal placeholders '{input}' and '{output}' for file paths. "
            "Empty when skip is true."
        ),
    )
    output_extension: str = Field(
        default="mp4", description="Output container extension without a dot: mp4, mkv, mov or webm."
    )

    @field_validator("args", mode="before")
    @classmethod
    def split_string_args(cls, v):
        # Some models return the args as one string instead of a list.
        # Paths are placeholders, so POSIX-style splitting is safe here.
        return shlex.split(v) if isinstance(v, str) else v
