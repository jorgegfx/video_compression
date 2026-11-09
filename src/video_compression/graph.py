import os

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from video_compression.llm import get_planner
from video_compression.prompts import build_messages
from video_compression.script import write_ps1
from video_compression.state import FileResult, OverallState, VideoState
from video_compression.tools import assess_source, build_ffmpeg_command, probe_video

MAX_LLM_ATTEMPTS = 2


def list_files(directory) -> list[str]:
    return [
        f for f in os.listdir(directory) if os.path.isfile(os.path.join(directory, f))
    ]


def get_files_to_encode(state: OverallState) -> dict:
    return {"files_to_encode": list_files(state["input_dir"])}


def fan_out_files(state: OverallState) -> list[Send]:
    # Routing function (not a node): one Send per file runs the worker in parallel.
    return [
        Send(
            "probe_and_plan",
            {
                "file": os.path.join(state["input_dir"], file),
                "staging_dir": state.get("staging_dir"),
                "output_dir": state["output_dir"],
                "encoders": state["encoders"],
                "mode": state["mode"],
            },
        )
        for file in state["files_to_encode"]
    ]


def probe_and_plan(state: VideoState) -> dict:
    """ffprobe the file, ask the LLM for an ffmpeg plan, validate it."""
    file = state["file"]
    result: FileResult = {"file": file}
    print(f"Probing {file}")

    try:
        probe = probe_video(file)
    except Exception as e:
        result["error"] = str(e)
        return {"results": [result]}
    result["probe"] = probe

    # Decide in code, not in the LLM, whether re-encoding can actually help.
    source = assess_source(probe, state["mode"])
    if source.skip_reason:
        result["skipped"] = True
        result["reasoning"] = source.skip_reason
        return {"results": [result]}

    # With --input, the script copies the source into staging_dir and encodes the copy.
    encode_input = file
    if state.get("staging_dir"):
        encode_input = os.path.join(state["staging_dir"], os.path.basename(file))

    planner = get_planner()
    messages = build_messages(probe, state["encoders"], source.max_video_kbps, state["mode"])
    for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
        print(f"Planning {file} (attempt {attempt})")
        try:
            plan = planner.invoke(messages)
            result["reasoning"] = plan.reasoning
            if plan.skip:
                result["skipped"] = True
                result.pop("error", None)
                break
            result["command"] = build_ffmpeg_command(
                plan.args,
                plan.output_extension,
                encode_input,
                state["output_dir"],
                max_video_kbps=source.max_video_kbps,
            )
            if encode_input != file:
                result["staged_input"] = encode_input
            result.pop("error", None)
            break
        except Exception as e:
            # Feed the problem back so the model can correct itself.
            result["error"] = f"{type(e).__name__}: {e}"
            messages = [
                *messages,
                ("human", f"Your previous answer was rejected: {e}. Return a corrected answer."),
            ]

    return {"results": [result]}


def write_script(state: OverallState) -> dict:
    """Fan-in: runs once after every probe_and_plan worker has finished."""
    write_ps1(
        state["results"], state["output_dir"], state["script_path"], state.get("staging_dir")
    )
    return {}


def create_graph():
    builder = StateGraph(OverallState)
    builder.add_node("get_files_to_encode", get_files_to_encode)
    builder.add_node("probe_and_plan", probe_and_plan)
    builder.add_edge(START, "get_files_to_encode")
    builder.add_conditional_edges("get_files_to_encode", fan_out_files, ["probe_and_plan"])
    builder.add_node("write_script", write_script)
    builder.add_edge("probe_and_plan", "write_script")
    builder.add_edge("write_script", END)
    return builder.compile()
