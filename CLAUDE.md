# CLAUDE.md — Project Context for agentic-p2p-auditor

## What This Project Is

A **domain-agnostic agentic QA auditor** that uses a three-agent pipeline (exploration → adversarial → judge) to autonomously audit any system with declared control rules. The framework is designed to scale across an acquisition portfolio — add a new domain package, run the same pipeline.

## Who Built It and Why

Built by **Brian Padgett** for a QA leadership role at **Libra Solutions Group** (pre-settlement funding, medical lien management via MoveDocs, inheritance funding). Libra is actively acquiring companies and building an agentic AI team. This framework demonstrates: one auditor pipeline that works across every product they acquire.

## Current Domains

### P2P (Purchase-to-Pay) — `domains/p2p/`
Audits financial controls in procurement workflows. Six controls: overpayment protection, 3-way match gate, partial receipt flag, inactive vendor gate, GL balance, duplicate invoice detection. Has a live HTTP adapter for real API testing.

### Medical Lien — `domains/medical_lien/`
Audits medical lien management for pre-settlement funding (maps to Libra's MoveDocs product). Six controls: lien priority enforcement (federal before state before private), balance cap, duplicate lien detection, provider status gate, settlement waterfall order, reduction negotiation cap (50% max). Mock only (no live adapter yet).

## Architecture

```
DomainSpec (tools, prompts, store, controls, rejection_signals)
    │
    ├── Exploration Agent — runs happy-path workflow end-to-end
    ├── Adversarial Agent — attacks each declared control rule
    └── Judge Agent — reads both transcripts, verifies verdicts against tool JSON
```

**The three-agent loop is domain-agnostic.** Domain-specific pieces are: system prompts, tool schemas, mock store business logic, control rules, and rejection signal detection. These live in pluggable domain packages under `domains/`.

## Key Files

| File | Purpose |
|---|---|
| `run_pipeline.py` | CLI orchestrator. `--domain p2p` or `--domain medical-lien` |
| `exploration_agent.py` | Domain-agnostic exploration loop, accepts `DomainSpec` |
| `adversarial_agent.py` | Domain-agnostic adversarial loop, accepts `DomainSpec` |
| `judge_agent.py` | Judge with injected `rejection_signals` from domain |
| `domains/__init__.py` | Registry + `load_domain()` |
| `domains/_base.py` | `DomainSpec` dataclass, universal `report_findings` |
| `domains/p2p/` | P2P domain package (mock_store, tools, prompts, controls, live_adapter) |
| `domains/medical_lien/` | Medical lien domain package (mock_store, tools, prompts, controls) |
| `agent_client.py` | Anthropic client factories with explicit timeouts |
| `pipeline_agentops.py` | Optional AgentOps observability |

## How to Run

```bash
# P2P domain
python run_pipeline.py --mode full --domain p2p --output-dir pipeline_output

# Medical lien domain
python run_pipeline.py --mode full --domain medical-lien --output-dir pipeline_output

# Individual phases
python run_pipeline.py --mode explore --domain medical-lien --output-dir pipeline_output
python run_pipeline.py --mode adversarial --domain p2p --output-dir pipeline_output
```

## Domain Interface Contract

Every domain package exports `DOMAIN_SPEC: DomainSpec` with:
- `MockStore` class with `dispatch(name, tool_input) -> dict`
- `domain_tools` — Anthropic tool schemas
- 3 system prompts (exploration, adversarial, judge)
- 2 default user prompts
- `control_rules` — list of {name, severity, description}
- `rejection_signals(obj) -> list[str]` — for judge evidence grounding
- Optional: `dispatch_live_http`, `probe_live_api`

## Adding a New Domain

1. Create `domains/new_domain/` with `__init__.py`, `mock_store.py`, `tools.py`, `prompts.py`, `controls.py`
2. Implement `MockStore.dispatch(name, tool_input) -> dict` enforcing all controls
3. Define tool schemas matching the mock store's dispatch routing
4. Write system prompts that tell agents about the domain's workflow and controls
5. Implement `rejection_signals()` so the judge can verify HELD/BREACHED verdicts
6. Export `DOMAIN_SPEC = DomainSpec(...)` from `__init__.py`
7. Add to `_REGISTRY` in `domains/__init__.py`

## Release Gate

After the judge phase in `--mode full`, the pipeline evaluates a **release gate** based on control severity tiers:

| Severity | On Breach | Exit Code |
|---|---|---|
| CRITICAL | BLOCK | 1 |
| HIGH | WARN | 0 |
| MEDIUM | WARN | 0 |

- **BLOCK** (exit 1): Any CRITICAL control breached, or happy path failed, or no judge report
- **WARN** (exit 0): HIGH or MEDIUM controls breached — deploy proceeds with alert
- **PASS** (exit 0): All controls held

This enables CI/CD integration: `python run_pipeline.py --mode full --domain p2p && deploy.sh` — the deploy step only runs if no CRITICAL controls were breached.

## Technical Decisions

- **Decimal math everywhere** — no floats in financial calculations
- **No mutable global state** — stores created locally and passed as params
- **Each adversarial run gets a fresh store** — no cross-contamination
- **report_findings is universal** — framework-owned, not domain-specific
- **Tool schemas stay in Python** — no YAML/JSON parsing layer
- **Live adapter is optional per domain** — `--live` rejected if domain lacks one
- **Wall-clock budgets** (300s default) — agents always terminate
- **AgentOps optional** — observability never blocks execution

## Environment Variables

Required: `ANTHROPIC_API_KEY`
Optional: `ANTHROPIC_MODEL` (default claude-sonnet-4-20250514), `AGENT_WALL_CLOCK_SECONDS` (300), `AGENTOPS_API_KEY`, `P2P_LIVE_BASE_URL`
