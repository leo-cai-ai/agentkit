"""Server-Sent Events (SSE) helpers for streaming LLM replies to the browser.

The agent pipeline runs synchronously (and, for action agents, may pause for
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
import logging
import queue
import threading
from collections.abc import Callable, Iterator
from typing import Any

from agentkit.core import llm_client

_DONE = "__done__"
_DEFAULT_QUEUE_SIZE = 256
_LOG = logging.getLogger(__name__)


class StreamCancelled(RuntimeError):
    """Raised inside the producer when the SSE consumer has gone away."""


class _ObservedTokenSink:
    """同时驱动 transport 与可选 durable observer，并暴露 force-flush。"""

    def __init__(
        self,
        emit: Callable[[str], None],
        observer: Callable[[str], None] | None,
    ) -> None:
        self._emit = emit
        self._observer = observer

    def __call__(self, chunk: str) -> None:
        self._emit(chunk)

    def flush(self) -> None:
        flush = getattr(self._observer, "flush", None)
        if callable(flush):
            flush()


def sse_frame(event: str, data: Any) -> str:
    """Render a single SSE frame (``event:`` + ``data:`` + blank line)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def stream_response(
    produce: Callable[[], dict[str, Any]],
    *,
    initial_events: tuple[tuple[str, dict[str, Any]], ...] = (),
    token_observer: Callable[[str], None] | None = None,
    continue_on_disconnect: bool = False,
    max_queue_size: int = _DEFAULT_QUEUE_SIZE,
    stream_tokens: bool = True,
    error_context: dict[str, Any] | None = None,
) -> Iterator[str]:
    """Run ``produce`` in a worker thread and yield SSE frames.

    ``produce`` performs the (blocking) agent run and returns the structured
    result dict. When ``stream_tokens`` is true, any ``require_chat_streaming``
    call emits chunks to the bound sink, which we forward as ``token`` frames.
    A ``final`` frame with the returned dict (or an ``error`` frame) terminates
    the stream.
    """
    events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=max(1, int(max_queue_size)))
    cancelled = threading.Event()

    def put_event(item: tuple[str, Any]) -> bool:
        while not cancelled.is_set():
            try:
                events.put(item, timeout=0.25)
                return True
            except queue.Full:
                continue
        if continue_on_disconnect:
            return False
        raise StreamCancelled()

    def emit(chunk: str) -> None:
        if token_observer is not None:
            try:
                token_observer(chunk)
            except Exception:  # noqa: BLE001 - observer 不得中断客户端输出
                _LOG.exception("stream token observer failed")
        if stream_tokens:
            put_event(("token", chunk))
        elif cancelled.is_set() and not continue_on_disconnect:
            raise StreamCancelled()

    def worker() -> None:
        try:
            with llm_client.stream_sink(_ObservedTokenSink(emit, token_observer)):
                result = produce()
            put_event(("final", result))
        except StreamCancelled:
            return
        except Exception as exc:  # noqa: BLE001 - relayed to the client as an error frame
            if not cancelled.is_set():
                try:
                    error = dict(error_context or {})
                    error["error"] = str(exc) or exc.__class__.__name__
                    put_event(("error", error))
                except StreamCancelled:
                    return
        finally:
            if not cancelled.is_set():
                try:
                    put_event((_DONE, None))
                except StreamCancelled:
                    return

    thread = threading.Thread(target=worker, name="agentkit-sse", daemon=True)
    thread.start()

    # Prelude comment flushes headers and defeats proxy buffering early.
    try:
        yield ": stream-open\n\n"
        for event, data in initial_events:
            yield sse_frame(event, data)
        while True:
            kind, data = events.get()
            if kind == _DONE:
                break
            if kind == "token":
                yield sse_frame("token", {"delta": data})
            elif kind == "error":
                yield sse_frame("error", data)
            else:
                yield sse_frame("final", data)
    except GeneratorExit:
        cancelled.set()
        raise
    finally:
        cancelled.set()


__all__ = ["sse_frame", "stream_response", "StreamCancelled"]
