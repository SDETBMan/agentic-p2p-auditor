"""
Medical lien domain package.
Re-exports everything needed by the framework via DOMAIN_SPEC.
"""

from domains._base import DomainSpec
from domains.medical_lien.controls import CONTROL_RULES, rejection_signals
from domains.medical_lien.mock_store import MockLienStore
from domains.medical_lien.prompts import (
    ADVERSARIAL_SYSTEM_PROMPT,
    DEFAULT_ADVERSARIAL_USER_PROMPT,
    DEFAULT_EXPLORATION_USER_PROMPT,
    EXPLORATION_SYSTEM_PROMPT,
    JUDGE_SYSTEM_PROMPT,
)
from domains.medical_lien.tools import DOMAIN_TOOLS

DOMAIN_SPEC = DomainSpec(
    name="medical-lien",
    MockStore=MockLienStore,
    domain_tools=DOMAIN_TOOLS,
    exploration_system_prompt=EXPLORATION_SYSTEM_PROMPT,
    adversarial_system_prompt=ADVERSARIAL_SYSTEM_PROMPT,
    judge_system_prompt=JUDGE_SYSTEM_PROMPT,
    default_exploration_user_prompt=DEFAULT_EXPLORATION_USER_PROMPT,
    default_adversarial_user_prompt=DEFAULT_ADVERSARIAL_USER_PROMPT,
    control_rules=CONTROL_RULES,
    rejection_signals=rejection_signals,
    # No live adapter for medical lien domain
    dispatch_live_http=None,
    probe_live_api=None,
)
