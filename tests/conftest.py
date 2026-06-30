"""Shared pytest fixtures for agentic-p2p-auditor safety tests."""

from __future__ import annotations

import pytest

from domains.p2p.mock_store import MockP2PStore


@pytest.fixture(scope="module")
def p2p_store() -> MockP2PStore:
    """Module-scoped MockP2PStore for generating representative audit outputs."""
    return MockP2PStore()
