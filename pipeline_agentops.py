"""
AgentOps observability for the P2P pipeline: init (bounded wait), per-agent traces, URLs.
All AgentOps calls are wrapped so failures never propagate to the pipeline.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Optional

# Set once per process: True = init completed and usable, False = skip AgentOps
_agentops_usable: Optional[bool] = None
_init_lock = threading.Lock()


def is_agentops_enabled() -> bool:
    return _agentops_usable is True


def init_agentops_in_daemon_thread(timeout_seconds: float = 5.0) -> bool:
    """
    Start AgentOps initialization in a daemon thread; block up to `timeout_seconds`
    for completion. If init does not finish in time, return False and continue
    without tracing (init may still complete in the background).
    """
    global _agentops_usable

    with _init_lock:
        if _agentops_usable is not None:
            return _agentops_usable

        api_key = os.environ.get("AGENTOPS_API_KEY", "").strip()
        if not api_key:
            _agentops_usable = False
            return False

        done = threading.Event()
        result: list[bool] = [False]

        def _worker() -> None:
            try:
                import agentops

                try:
                    agentops.init(
                        api_key=api_key,
                        auto_start_session=False,
                        fail_safe=True,
                        instrument_llm_calls=True,
                        log_session_replay_url=False,
                    )
                except UnicodeEncodeError:
                    # Windows consoles (e.g. cp1252) can raise when AgentOps logs emoji.
                    result[0] = False
                else:
                    result[0] = True
            except UnicodeEncodeError:
                result[0] = False
            except Exception:
                result[0] = False
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True).start()

        if not done.wait(timeout=timeout_seconds):
            _agentops_usable = False
            return False

        _agentops_usable = bool(result[0])
        return _agentops_usable


def start_agent_trace(
    agent_name: str,
    *,
    pipeline_mode: str,
    live: bool,
) -> Any:
    """Start a named trace for one agent session. Returns TraceContext or None."""
    if not is_agentops_enabled():
        return None
    try:
        import agentops

        tags: list[str] = [
            f"agent:{agent_name}",
            f"mode:{pipeline_mode}",
            f"live:{live}",
        ]
        ctx = agentops.start_trace(
            trace_name=f"P2P {agent_name}",
            tags=tags,
        )
        return ctx
    except Exception:
        return None


def finalize_agent_trace(
    trace_ctx: Any,
    *,
    agent_name: str,
    pipeline_mode: str,
    live: bool,
    tool_events: list[dict[str, Any]],
    marker_reached: bool,
) -> Optional[str]:
    """
    Record metadata, end trace with Success/Fail, return dashboard session URL if available.
    """
    if trace_ctx is None:
        return None

    url: Optional[str] = None
    try:
        from agentops.helpers.dashboard import get_trace_url

        if getattr(trace_ctx, "span", None) is not None:
            url = get_trace_url(trace_ctx.span)
    except Exception:
        pass

    try:
        import agentops
        from agentops import update_trace_metadata

        summary = [
            {"tool": e.get("tool"), "iteration": e.get("iteration"), "phase": e.get("phase")}
            for e in tool_events
        ]
        payload = json.dumps(summary, default=str)[:16000]

        update_trace_metadata(
            {
                "agent_name": agent_name,
                "pipeline_mode": pipeline_mode,
                "live": live,
                "tool_calls": payload,
                "completion_marker_reached": marker_reached,
            }
        )
    except Exception:
        pass

    try:
        import agentops

        end_state = "Success" if marker_reached else "Fail"
        agentops.end_trace(trace_ctx, end_state=end_state)
    except Exception:
        pass

    return url

