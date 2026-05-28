"""
Shared Anthropic HTTP client factories with explicit connect/read timeouts.
"""

from __future__ import annotations

import os

import httpx
from anthropic import Anthropic

_CONNECT_SECONDS = 10.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def agent_wall_clock_seconds() -> float:
    """Max wall-clock runtime for exploration/adversarial agent loops (seconds)."""
    return _env_float("AGENT_WALL_CLOCK_SECONDS", 300.0)


def make_anthropic_client() -> Anthropic:
    """
    Default pipeline client: connect 10s, read from ANTHROPIC_HTTP_READ_TIMEOUT (default 120s).
    """
    read_s = _env_float("ANTHROPIC_HTTP_READ_TIMEOUT", 120.0)
    timeout = httpx.Timeout(
        connect=_CONNECT_SECONDS,
        read=read_s,
        write=_CONNECT_SECONDS,
        pool=_CONNECT_SECONDS,
    )
    return Anthropic(timeout=timeout)


def make_judge_anthropic_client() -> Anthropic:
    """
    Judge client: connect 10s, read from JUDGE_HTTP_READ_TIMEOUT (default 300s).
    """
    read_s = _env_float("JUDGE_HTTP_READ_TIMEOUT", 300.0)
    timeout = httpx.Timeout(
        connect=_CONNECT_SECONDS,
        read=read_s,
        write=_CONNECT_SECONDS,
        pool=_CONNECT_SECONDS,
    )
    return Anthropic(timeout=timeout)
