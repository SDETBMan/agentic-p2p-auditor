# agentic-p2p-auditor

An autonomous **three-agent QA pipeline** for auditing Purchase-to-Pay (P2P) financial systems. An **exploration** agent runs a full happy-path workflow, an **adversarial** agent attacks six financial control rules, and a **judge** agent reads both transcripts and emits a structured JSON verdict grounded in tool evidence -- not test assertions, agents that reason about why a control failed and explain it.

Built to solve a real problem: PE firms acquiring product suites need to audit legacy P2P systems for control violations before integration. Manual review of millions of lines of financial code is slow, expensive, and error-prone. This agent pipeline scans the system autonomously, attacks every declared control boundary, and produces an auditable report a compliance team can trust.

---

## What it demonstrates

| Concept | Where |
|---|---|
| **Three-agent architecture** | `exploration_agent.py` -> `adversarial_agent.py` -> `judge_agent.py` -- each agent has isolated context |
| **Anthropic tool use (function calling)** | All three agents use Claude's tool use API with typed schemas for structured interaction |
| **Deterministic mock P2P backend** | `exploration_agent.py` -- MockP2PStore with full P2P lifecycle (vendors, POs, receipts, invoices, payments) |
| **Live HTTP adapter** | `p2p_live.py` -- flip one flag to run against a real REST API instead of mocks |
| **Six financial control rules** | Overpayment protection, 3-way match, partial receipt, inactive vendor, GL balance, duplicate invoice |
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
| Mock backend | Deterministic in-process P2P store |
| CLI | argparse with mode/model/live/output-dir flags |

---

## Quick start

```bash
git clone https://github.com/SDETBMan/agentic-p2p-auditor.git
cd agentic-p2p-auditor
pip install -r requirements.txt

# Set your Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-..."

# Run full pipeline: exploration -> adversarial -> judge
python run_pipeline.py --mode full --output-dir pipeline_output

# Or run individual phases
python run_pipeline.py --mode explore --output-dir pipeline_output
python run_pipeline.py --mode adversarial --output-dir pipeline_output
```

> Set `ANTHROPIC_API_KEY` in your shell or a `.env` loader. No keys are hardcoded in this repo.

---

## Financial controls under attack

| # | Control Rule | What It Prevents |
|---|---|---|
| 1 | **Overpayment protection** | Cumulative payments exceeding PO/invoice authorization |
| 2 | **3-way match gate** | Invoice approval without matching PO + receipt within tolerance |
| 3 | **Partial receipt flag** | Full invoice matching against partially received goods |
| 4 | **Inactive vendor gate** | PO submission or invoice posting against blocked/inactive vendors |
| 5 | **GL balance** | Unbalanced debit/credit postings corrupting the general ledger |
| 6 | **Duplicate invoice detection** | Same vendor + invoice number paying twice |

---

## Architecture

```
Exploration Agent ──> Adversarial Agent ──> Judge Agent ──> Structured Report
      │                      │                   │
  Happy-path P2P         Attacks six          Reads both
  workflow end-to-end    control rules        transcripts,
  with mock or live      with evidence        verifies claims
  tools                  tracking             against tool JSON
```

| Component | Role |
|---|---|
| **Exploration Agent** | Runs a complete valid P2P workflow (create vendor, PO, receipt, invoice, match, approve). Logs all tool interactions. Emits `[[EXPLORATION_COMPLETE]]` marker. |
| **Adversarial Agent** | Red-teams the six financial controls. Probes edge cases (overpayment, duplicate invoices, inactive vendors). Tracks HELD/BREACHED verdicts with evidence. Emits `[[ADVERSARIAL_COMPLETE]]` marker. |
| **Judge Agent** | Independent verification layer. Reads exploration and adversarial transcripts via its own tool suite (`read_test_report`, `verify_tool_evidence`, `score_finding`, `generate_judge_report`). Emits structured JSON assessment. |
| **Pipeline Orchestrator** | Serializes phases, captures stdout, parses tool events, writes `test_report.json` and `judge_report.json`, manages AgentOps traces. |
| **Live HTTP Adapter** | Maps mock tool names to REST calls against a real server following `p2p_api_spec.md`. One flag (`--live`) switches from mock to HTTP. |

---

## Live HTTP mode

The pipeline runs against deterministic mocks by default. Pass `--live` to hit a real P2P API:

```bash
# Against default localhost:8000
python run_pipeline.py --mode full --output-dir pipeline_output --live

# Against a custom URL
python run_pipeline.py --mode full --output-dir pipeline_output --live --live-url http://staging:8000
```

The live adapter (`p2p_live.py`) probes the API before running. If unreachable, the pipeline exits with a clear error instead of hanging.

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
| `P2P_LIVE_BASE_URL` | `http://localhost:8000` | Base URL for live mode |

---

## File structure

```
agentic-p2p-auditor/
├── run_pipeline.py            # CLI entrypoint: explore / adversarial / full mode orchestration
├── exploration_agent.py       # Mock P2P store + exploration agent loop (happy-path workflow)
├── adversarial_agent.py       # Red-team agent attacking six financial control rules
├── judge_agent.py             # Independent judge with evidence verification tools
├── agent_client.py            # Anthropic client factories with explicit timeouts
├── p2p_live.py                # Live HTTP adapter: mock tool names -> REST calls
├── pipeline_agentops.py       # Bounded AgentOps init + per-agent trace helpers
├── data_generator.py          # Synthetic P2P scenario generator with controlled violations
├── p2p_api_spec.md            # REST API spec + six financial control rule definitions
├── requirements.txt           # anthropic, agentops
└── pipeline_output/           # Generated: logs, test_report.json, judge_report.json
```

---

## Why the judge runs last (not in parallel)

The judge's job is to verify whether exploration and adversarial claims are supported by actual tool responses. That requires **complete** transcripts from both phases. Running the judge in parallel would force it to assess empty or partial outputs. Serializing exploration -> adversarial -> judge guarantees the judge sees the same artifacts the pipeline wrote to disk and that all tool events are finalized before verification begins.

---

## Design decisions

- **Why three agents instead of one?** Context isolation. The exploration agent sees only the happy path. The adversarial agent sees only attack scenarios. The judge sees only transcripts and evidence -- it never saw the execution context, so it cannot be biased by it. A judge that has seen the attack execution context is a compromised judge.
- **Why Decimal instead of float?** Float contamination in financial calculations is a compliance finding in regulated environments, not a rounding error. `Decimal("100.50")` is exact. `100.50` is `100.49999999999999289457264239899814128875732421875`.
- **Why mock + live toggle?** Mocks give deterministic, repeatable results for development and CI. Live mode gives real API behavior for staging validation. Same agent code, same tool schemas, different backend -- one flag.
- **Why wall-clock budgets?** Agentic loops can spin indefinitely on ambiguous tool responses. A 300-second ceiling ensures the pipeline always terminates, even if an agent gets stuck in a reasoning loop.
- **Why AgentOps is optional?** Observability should not block execution. The pipeline initializes AgentOps in a daemon thread with a 5-second timeout. If it's slow or unavailable, the pipeline runs without it.

---

## License & Attribution

MIT License -- Copyright 2026 Brian Padgett

This framework was independently developed prior to any employment engagement.
All code, architecture decisions, and documentation represent original work
authored and committed by Brian Padgett. Commit history and timestamps are
the authoritative record of authorship.
