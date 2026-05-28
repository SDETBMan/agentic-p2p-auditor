"""
P2P (Purchase-to-Pay) domain package.
Re-exports everything needed by the framework via DOMAIN_SPEC.
"""

from domains._base import DomainSpec
from domains.p2p.controls import CONTROL_RULES, rejection_signals
from domains.p2p.live_adapter import dispatch_live_http, probe_live_api
from domains.p2p.mock_store import MockP2PStore
from domains.p2p.prompts import (
    ADVERSARIAL_SYSTEM_PROMPT,
    DEFAULT_ADVERSARIAL_USER_PROMPT,
    DEFAULT_EXPLORATION_USER_PROMPT,
    EXPLORATION_SYSTEM_PROMPT,
    JUDGE_SYSTEM_PROMPT,
)
from domains.p2p.tools import DOMAIN_TOOLS

DOMAIN_SPEC = DomainSpec(
    name="p2p",
    MockStore=MockP2PStore,
    domain_tools=DOMAIN_TOOLS,
    exploration_system_prompt=EXPLORATION_SYSTEM_PROMPT,
    adversarial_system_prompt=ADVERSARIAL_SYSTEM_PROMPT,
    judge_system_prompt=JUDGE_SYSTEM_PROMPT,
    default_exploration_user_prompt=DEFAULT_EXPLORATION_USER_PROMPT,
    default_adversarial_user_prompt=DEFAULT_ADVERSARIAL_USER_PROMPT,
    control_rules=CONTROL_RULES,
    rejection_signals=rejection_signals,
    dispatch_live_http=dispatch_live_http,
    probe_live_api=probe_live_api,
)
