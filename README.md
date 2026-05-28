# agentic-p2p-auditor

A **domain-agnostic** autonomous **three-agent QA pipeline** for auditing financial and compliance systems. An **exploration** agent runs a full happy-path workflow, an **adversarial** agent attacks declared control rules, and a **judge** agent reads both transcripts and emits a structured JSON verdict grounded in tool evidence -- not test assertions, agents that reason about why a control failed and explain it.

Built to solve a real problem: organizations acquiring product suites need to audit systems for control violations before integration. Manual review is slow, expensive, and error-prone. This agent pipeline scans the system autonomously, attacks every declared control boundary, and produces an auditable report a compliance team can trust. **One framework, any domain** -- add a new domain package and run the same pipeline.

---

## What it demonstrates

| Concept | Where |
|---|---|
| **Domain-agnostic three-agent architecture** | `exploration_agent.py` -> `adversarial_agent.py` -> `judge_agent.py` -- each agent has isolated context, any domain |
| **Pluggable domain packages** | `domains/p2p/`, `domains/medical_lien/` -- each package provides tools, prompts, mock store, controls |
| **Anthropic tool use (function calling)** | All three agents use Claude's tool use API with typed schemas for structured interaction |
| **Two production domains** | P2P financial controls (6 rules) and Medical lien management (6 rules) |
| **Deterministic mock backends** | `MockP2PStore` and `MockLienStore` with full business logic and Decimal math |
| **Live HTTP adapter** | `domains/p2p/live_adapter.py` -- flip one flag to run P2P against a real REST API |
| **Decimal-based money (no floats)** | All monetary calculations use `Decimal` -- float contamination is a control violation, not a rounding error |
| **Evidence-grounded verdicts** | Judge validates HELD/BREACHED claims against actual tool response JSON -- no hallucinated findings |
| **Wall-clock and iteration limits** | Configurable budgets prevent runaway agent loops from hanging indefinitely |
| **AgentOps observability** | `pipeline_agentops.py` -- optional per-agent traces with bounded init and graceful fallback |
| **Synthetic scenario generation** | `data_generator.py` -- 50 randomized P2P scenarios with controlled violation labels |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.11, type hints throughout |
| LLM | Anthropic Claude (Messages API with tool use) |
| HTTP client | urllib (stdlib -- zero external HTTP deps for live adapter) |
| Money | `decimal.Decimal` -- no floats in any financial calculation |
| Observability | AgentOps (optional, bounded init) |
| Mock backends | Deterministic in-process domain stores |
| CLI | argparse with domain/mode/model/live/output-dir flags |

---

## Quick start

```bash
git clone https://github.com/SDETBMan/agentic-p2p-auditor.git
cd agentic-p2p-auditor
pip install -r requirements.txt

# Set your Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-..."

# Run full pipeline with P2P domain (default)
python run_pipeline.py --mode full --domain p2p --output-dir pipeline_output

# Run full pipeline with medical lien domain
python run_pipeline.py --mode full --domain medical-lien --output-dir pipeline_output

# Or run individual phases
python run_pipeline.py --mode explore --domain p2p --output-dir pipeline_output
python run_pipeline.py --mode adversarial --domain medical-lien --output-dir pipeline_output
```

> Set `ANTHROPIC_API_KEY` in your shell or a `.env` loader. No keys are hardcoded in this repo.

---

## Domains

### P2P (Purchase-to-Pay)

Audits a complete P2P financial workflow: vendor management, purchase orders, goods receipt, invoice matching, and payment processing.

| # | Control Rule | What It Prevents |
|---|---|---|
| 1 | **Overpayment protection** | Cumulative payments exceeding PO/invoice authorization |
| 2 | **3-way match gate** | Invoice approval without matching PO + receipt within tolerance |
| 3 | **Partial receipt flag** | Full invoice matching against partially received goods |
| 4 | **Inactive vendor gate** | PO submission or invoice posting against blocked/inactive vendors |
| 5 | **GL balance** | Unbalanced debit/credit postings corrupting the general ledger |
| 6 | **Duplicate invoice detection** | Same vendor + invoice number paying twice |

**Tools:** `create_purchase_order`, `submit_invoice`, `process_payment`, `get_transaction_status`

### Medical Lien

Audits medical lien management for pre-settlement funding and personal injury cases: lien filing, balance management, reduction negotiation, and settlement distribution.

| # | Control Rule | What It Prevents |
|---|---|---|
| 1 | **Lien priority enforcement** | Federal liens (Medicare/Medicaid) subordinated in payment order |
| 2 | **Balance cap** | Lien balance inflated beyond original billed amount |
| 3 | **Duplicate lien detection** | Same provider + case + service date paying twice |
| 4 | **Provider status gate** | Liens filed by suspended/excluded providers accepted |
| 5 | **Settlement waterfall order** | Disbursement out of legal priority order (federal -> state -> private) |
| 6 | **Reduction negotiation cap** | Negotiated reduction exceeding policy max percentage (50%) |

**Tools:** `create_lien`, `adjust_lien_balance`, `negotiate_reduction`, `distribute_settlement`, `get_entity_status`, `check_lien_compliance`

---

## Architecture

```
                      ┌─────────────┐
                      │ Domain Spec │  (tools, prompts, store, controls)
                      └──────┬──────┘
                             │
    ┌────────────────────────┼────────────────────────┐
    │                        │                        │
    v                        v                        v
Exploration Agent ──> Adversarial Agent ──> Judge Agent ──> Structured Report
    │                        │                   │
Happy-path workflow     Attacks declared     Reads both
end-to-end with         control rules        transcripts,
domain tools            with evidence        verifies claims
                        tracking             against tool JSON
```

| Component | Role |
|---|---|
| **Domain Package** | Provides tools, system prompts, mock store, control rules, and rejection signal detection. Pluggable -- add a new domain without changing the framework. |
| **Exploration Agent** | Runs a complete valid workflow for the domain. Logs all tool interactions. Emits `[[EXPLORATION_COMPLETE]]` marker. |
| **Adversarial Agent** | Red-teams the domain's control rules. Probes edge cases. Tracks HELD/BREACHED verdicts with evidence. Emits `[[ADVERSARIAL_COMPLETE]]` marker. |
| **Judge Agent** | Independent verification layer. Reads transcripts and verifies verdicts against tool response JSON using domain-specific rejection signals. Emits structured JSON assessment. |
| **Pipeline Orchestrator** | Serializes phases, captures stdout, parses tool events, writes reports, manages AgentOps traces. |

---

## Domain interface contract

Every domain package must export a `DOMAIN_SPEC` of type `DomainSpec` providing:

| Export | Type | Description |
|---|---|---|
| `name` | `str` | Domain slug (e.g., `"p2p"`, `"medical-lien"`) |
| `MockStore` | `class` | Mock backend with `dispatch(name, tool_input) -> dict` and `__init__()` |
| `domain_tools` | `list[dict]` | Anthropic tool schemas (domain-specific, NOT report_findings) |
| `exploration_system_prompt` | `str` | Exploration agent system prompt |
| `adversarial_system_prompt` | `str` | Adversarial agent system prompt |
| `judge_system_prompt` | `str` | Judge agent system prompt |
| `default_exploration_user_prompt` | `str` | Default first user message for exploration |
| `default_adversarial_user_prompt` | `str` | Default first user message for adversarial |
| `control_rules` | `list[dict]` | Control rule metadata (name, description) |
| `rejection_signals` | `Callable` | Extracts domain-specific rejection indicators from tool responses |
| `dispatch_live_http` | `Callable` (optional) | Live HTTP adapter |
| `probe_live_api` | `Callable` (optional) | Live API health check |

---

## Adding a new domain

1. Create `domains/your_domain/` with `__init__.py`, `mock_store.py`, `tools.py`, `prompts.py`, `controls.py`
2. Implement `MockStore` with a `dispatch(name, tool_input) -> dict` method
3. Define tool schemas, system prompts, and control rules
4. Implement `rejection_signals(obj) -> list[str]` for the judge
5. Export `DOMAIN_SPEC = DomainSpec(...)` from `__init__.py`
6. Register in `domains/__init__.py` `_REGISTRY`
7. Run: `python run_pipeline.py --mode full --domain your-domain`

---

## Live HTTP mode

The pipeline runs against deterministic mocks by default. For domains with a live adapter (currently P2P), pass `--live` to hit a real API:

```bash
# P2P against default localhost:8000
python run_pipeline.py --mode full --domain p2p --live

# P2P against a custom URL
python run_pipeline.py --mode full --domain p2p --live --live-url http://staging:8000

# Medical lien does not have a live adapter -- this will error with a clear message
python run_pipeline.py --mode full --domain medical-lien --live
```

---

## Environment variables

### Required

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | API key for Claude (used by the SDK, not hardcoded) |

### Optional

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-sonnet-4-20250514` | Model for all agents |
| `AGENT_WALL_CLOCK_SECONDS` | `300` | Max wall-clock time per agent phase |
| `ANTHROPIC_HTTP_READ_TIMEOUT` | `120` | HTTP read timeout for exploration/adversarial |
| `JUDGE_HTTP_READ_TIMEOUT` | `300` | HTTP read timeout for judge (longer due to transcript analysis) |
| `AGENTOPS_API_KEY` | _(unset)_ | Enables AgentOps tracing when set |
| `P2P_LIVE_BASE_URL` | `http://localhost:8000` | Base URL for P2P live mode |

---

## File structure

```
agentic-p2p-auditor/
├── run_pipeline.py                    # CLI entrypoint: --domain, --mode, orchestration
├── exploration_agent.py               # Domain-agnostic exploration agent loop
├── adversarial_agent.py               # Domain-agnostic adversarial agent loop
├── judge_agent.py                     # Domain-agnostic judge with injected rejection signals
├── agent_client.py                    # Anthropic client factories with explicit timeouts
├── pipeline_agentops.py               # Bounded AgentOps init + per-agent trace helpers
├── data_generator.py                  # Synthetic P2P scenario generator (standalone tool)
├── p2p_api_spec.md                    # P2P REST API specification
├── requirements.txt                   # anthropic, agentops
├── domains/
│   ├── __init__.py                    # Domain registry + load_domain()
│   ├── _base.py                       # DomainSpec dataclass, universal report_findings
│   ├── p2p/
│   │   ├── __init__.py                # DOMAIN_SPEC re-export
│   │   ├── mock_store.py              # MockP2PStore with 6 financial controls
│   │   ├── tools.py                   # 4 P2P tool schemas
│   │   ├── prompts.py                 # System + user prompts
│   │   ├── controls.py                # Control rules + rejection_signals()
│   │   └── live_adapter.py            # Live HTTP adapter for P2P REST API
│   └── medical_lien/
│       ├── __init__.py                # DOMAIN_SPEC re-export
│       ├── mock_store.py              # MockLienStore with 6 lien controls
│       ├── tools.py                   # 6 medical lien tool schemas
│       ├── prompts.py                 # System + user prompts
│       └── controls.py                # Control rules + rejection_signals()
└── pipeline_output/                   # Generated: logs, test_report.json, judge_report.json
```

---

## Design decisions

- **Why domain-agnostic?** Organizations acquiring product suites need one pipeline that works across every product. Adding a new domain is a package, not a rewrite.
- **Why three agents instead of one?** Context isolation. The exploration agent sees only the happy path. The adversarial agent sees only attack scenarios. The judge sees only transcripts and evidence -- it never saw the execution context, so it cannot be biased by it.
- **Why Decimal instead of float?** Float contamination in financial calculations is a compliance finding in regulated environments, not a rounding error. `Decimal("100.50")` is exact. `100.50` is `100.49999999999999289457264239899814128875732421875`.
- **Why mock + live toggle?** Mocks give deterministic, repeatable results for development and CI. Live mode gives real API behavior for staging validation. Same agent code, same tool schemas, different backend -- one flag.
- **Why wall-clock budgets?** Agentic loops can spin indefinitely on ambiguous tool responses. A 300-second ceiling ensures the pipeline always terminates, even if an agent gets stuck in a reasoning loop.
- **Why AgentOps is optional?** Observability should not block execution. The pipeline initializes AgentOps in a daemon thread with a 5-second timeout. If it's slow or unavailable, the pipeline runs without it.
- **Why report_findings is universal?** Every domain needs to log QA steps. The framework owns this tool; domains own only their business-logic tools.

---

## License & Attribution

MIT License -- Copyright 2026 Brian Padgett

This framework was independently developed prior to any employment engagement.
All code, architecture decisions, and documentation represent original work
authored and committed by Brian Padgett. Commit history and timestamps are
the authoritative record of authorship.
