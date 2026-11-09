import operator
from typing import Annotated, Literal

from typing_extensions import NotRequired, TypedDict


Mode = Literal["compress", "enhance"]


class FileResult(TypedDict):
    """Outcome for one input file."""

    file: str
    probe: NotRequired[dict]
    reasoning: NotRequired[str]
    skipped: NotRequired[bool]
    command: NotRequired[list[str]]  # final ffmpeg args, ready for run_command
    error: NotRequired[str]


class OverallState(TypedDict):
    input_dir: str
    output_dir: str
    encoders: list[str]  # video encoders the LLM may choose from
    mode: Mode  # which prompt/rules to use
    script_path: str  # where the generated .ps1 with all commands is written
    files_to_encode: NotRequired[list[str]]
    # One entry per file, so probe/plan/command/error always stay together.
    results: Annotated[list[FileResult], operator.add]


class VideoState(TypedDict):
    """Input sent to each parallel probe_and_plan worker via Send."""

    file: str
    output_dir: str
    encoders: list[str]
    mode: Mode
