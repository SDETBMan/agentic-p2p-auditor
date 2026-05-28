"""
Domain registry: discovers and loads domain packages by name.
"""

from __future__ import annotations

import importlib
from typing import Any

from domains._base import DomainSpec

# Canonical registry of known domain slugs -> Python module paths
_REGISTRY: dict[str, str] = {
    "p2p": "domains.p2p",
    "medical-lien": "domains.medical_lien",
}


def available_domains() -> list[str]:
    """Return sorted list of registered domain slugs."""
    return sorted(_REGISTRY)


def load_domain(name: str) -> DomainSpec:
    """
    Load a domain package by slug and return its DomainSpec.
    Raises ValueError if the domain is not registered.
    """
    module_path = _REGISTRY.get(name)
    if module_path is None:
        avail = ", ".join(available_domains())
        raise ValueError(
            f"Unknown domain: {name!r}. Available domains: {avail}"
        )
    mod = importlib.import_module(module_path)
    spec: DomainSpec = mod.DOMAIN_SPEC  # type: ignore[attr-defined]
    return spec
