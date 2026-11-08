import os
from functools import cache

from langchain_ollama import ChatOllama

from video_compression.schemas import FfmpegPlan

OLLAMA_CLOUD_URL = "https://ollama.com"
DEFAULT_MODEL = "gpt-oss:120b"


@cache
def get_planner():
    """ChatOllama pointed at Ollama Cloud, returning FfmpegPlan objects.

    Reads OLLAMA_API_KEY (required) and OLLAMA_MODEL (optional) from the
    environment; main() loads them from .env.
    """
    api_key = os.environ.get("OLLAMA_API_KEY")
    if not api_key:
        raise RuntimeError("OLLAMA_API_KEY is not set. Add it to .env.")

    llm = ChatOllama(
        model=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL),
        base_url=OLLAMA_CLOUD_URL,
        client_kwargs={"headers": {"Authorization": f"Bearer {api_key}"}},
        temperature=0,
    )
    return llm.with_structured_output(FfmpegPlan, method="json_schema")
