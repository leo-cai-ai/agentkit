"""Server-Sent Events (SSE) helpers for streaming LLM replies to the browser.

The agent pipeline runs synchronously (and, for command agents, may pause for
human approval), but the *final user-facing* generation streams its tokens
through a sink bound by :func:`agentkit.core.llm_client.stream_sink`. These
helpers run the producer in a worker thread, bind that sink to a queue, and
relay tokens to the client as ``event: token`` SSE frames followed by a single
``event: final`` frame carrying the structured result.

Governance / JSON nodes stay on the blocking path, so only the final answer
streams. SSE frames:

    event: token   data: {"delta": "..."}
    event: final   data: {<structured result>}
    event: error   data: {"error": "..."}
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable, Iterator
from typing import Any

from agentkit.core import llm_client

_DONE = "__done__"


def sse_frame(event: str, data: Any) -> str:
    """Render a single SSE frame (``event:`` + ``data:`` + blank line)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def stream_response(produce: Callable[[], dict[str, Any]]) -> Iterator[str]:
    """Run ``produce`` in a worker thread and yield SSE frames.

    ``produce`` performs the (blocking) agent run and returns the structured
    result dict; while it runs, any ``require_chat_streaming`` call emits chunks
    to the bound sink, which we forward as ``token`` frames. A ``final`` frame
    with the returned dict (or an ``error`` frame) terminates the stream.
    """
    events: queue.Queue[tuple[str, Any]] = queue.Queue()

    def emit(chunk: str) -> None:
        events.put(("token", chunk))

    def worker() -> None:
        try:
            with llm_client.stream_sink(emit):
                result = produce()
            events.put(("final", result))
        except Exception as exc:  # noqa: BLE001 - relayed to the client as an error frame
            events.put(("error", str(exc) or exc.__class__.__name__))
        finally:
            events.put((_DONE, None))

    thread = threading.Thread(target=worker, name="agentkit-sse", daemon=True)
    thread.start()

    # Prelude comment flushes headers and defeats proxy buffering early.
    yield ": stream-open\n\n"
    while True:
        kind, data = events.get()
        if kind == _DONE:
            break
        if kind == "token":
            yield sse_frame("token", {"delta": data})
        elif kind == "error":
            yield sse_frame("error", {"error": data})
        else:
            yield sse_frame("final", data)


__all__ = ["sse_frame", "stream_response"]
