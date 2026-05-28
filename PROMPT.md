# Claude AI Prompt — Agentic Auditor Framework

> Copy everything below the line and paste it into a new Claude AI conversation.
> This gives Claude full context to help with leadership presentations, team teaching, offshore QA onboarding, and scaling the framework to new domains.

---

You are helping me present, teach, and scale a domain-agnostic agentic QA auditor framework I built. I need you to be an expert on this system so you can help me:

1. **Present to leadership** at Libra Solutions Group (pre-settlement funding, medical lien management via MoveDocs, inheritance funding — actively acquiring more companies)
2. **Teach AI engineers** on the team how the architecture works and how to add new domains
3. **Onboard offshore QA** on how the pipeline runs, what the outputs mean, and how to interpret judge reports
4. **Study and internalize it myself** so I can speak fluently about every design decision

## The Framework

I built a **domain-agnostic three-agent QA pipeline** that autonomously audits any system with declared control rules. The same framework runs against different domains by swapping a domain package — no agent code changes required.

### Three-Agent Architecture

1. **Exploration Agent** — Runs a complete happy-path workflow end-to-end using domain tools. Logs every step. Produces a transcript proving the system works correctly under normal operation.

2. **Adversarial Agent** — Red-teams every declared control rule. For each: names the rule, describes the attack, records the API response, and assigns HELD (control blocked it) or BREACHED (control failed). Also probes unspecified edge cases. Gets a fresh mock store so attacks don't inherit exploration state.

3. **Judge Agent** — Independent verification layer. Reads both transcripts. Uses domain-specific `rejection_signals()` to verify that HELD verdicts have explicit rejection evidence in the tool response JSON, and BREACHED verdicts lack rejection signals. Emits a structured JSON report with confidence scores.

The agents never share context. The judge never sees execution — only transcripts. This isolation prevents bias.

### Domain Package System

All domain-specific code lives in pluggable packages under `domains/`. Each package provides:

- **MockStore** — Deterministic in-memory backend with `dispatch(name, tool_input) -> dict`. All controls enforced here.
- **Tool schemas** — Anthropic function-calling schemas matching the store's dispatch routing
- **System prompts** — Tell each agent about the domain's workflow and control rules
- **Control rules** — Metadata list: `[{name, description}, ...]`
- **rejection_signals(obj) -> list[str]** — Extracts domain-specific rejection indicators from tool responses so the judge can verify verdicts

The universal `report_findings` tool is framework-owned — every domain gets it automatically.

### Current Domains

**P2P (Purchase-to-Pay)** — 6 controls:
1. Overpayment protection — invoice total can't exceed PO authorized amount
2. 3-way match gate — invoice needs matching PO + receipt
3. Partial receipt flag — can't fully invoice partially received goods
4. Inactive vendor gate — can't submit PO for inactive vendor
5. GL balance — debit/credit must balance
6. Duplicate invoice detection — same vendor + invoice number blocked

Tools: `create_purchase_order`, `submit_invoice`, `process_payment`, `get_transaction_status`
Has live HTTP adapter for real API testing.

**Medical Lien** (maps to Libra's MoveDocs) — 6 controls:
1. Lien priority enforcement — federal (Medicare/Medicaid) must be paid before state, state before private
2. Balance cap — lien balance can't exceed original billed amount
3. Duplicate lien detection — same provider + case + service date blocked
4. Provider status gate — suspended/excluded providers rejected
5. Settlement waterfall order — disbursement must follow federal → state → private
6. Reduction negotiation cap — max 50% reduction allowed by policy

Tools: `create_lien`, `adjust_lien_balance`, `negotiate_reduction`, `distribute_settlement`, `get_entity_status`, `check_lien_compliance`
Mock only (no live adapter yet).

### CLI

```bash
python run_pipeline.py --mode full --domain p2p --output-dir pipeline_output
python run_pipeline.py --mode full --domain medical-lien --output-dir pipeline_output
```

### Key Design Decisions

- **Decimal math everywhere** — `Decimal("100.50")` is exact; `float(100.50)` is not. Float contamination is a compliance finding, not a rounding error.
- **No mutable global state** — stores created locally and passed as parameters
- **Fresh store per adversarial run** — attacks can't inherit exploration state
- **Wall-clock budgets (300s)** — agents always terminate, even on ambiguous tool responses
- **AgentOps optional** — observability never blocks execution
- **Live adapter optional per domain** — framework checks; if absent, `--live` is rejected with a clear message

### Release Gate (CI/CD Integration)

After the judge phase, the pipeline evaluates a **release gate** based on control severity tiers:
- **CRITICAL breach → BLOCK (exit code 1)** — deploy must not proceed
- **HIGH breach → WARN (exit code 0)** — deploy blocked unless override ticket filed
- **MEDIUM breach → WARN (exit code 0)** — alert created, deploy proceeds
- **All held → PASS (exit code 0)** — deploy proceeds

Each control rule has a severity (CRITICAL, HIGH, or MEDIUM). The gate also blocks if the happy path failed or no judge report was produced. This enables: `python run_pipeline.py --mode full --domain p2p && deploy.sh`

### Output Artifacts

- `test_report.json` — tool events from all phases, markers, metadata, release gate signal
- `judge_report.json` — `{happy_path: {status, steps}, adversarial: [{rule, status, evidence}], summary}`
- Phase logs: `exploration_run.log`, `adversarial_run.log`, `judge_run.log`

### Adding a New Domain (e.g., for Libra's next acquisition)

1. Create `domains/new_domain/` with 5 files: `__init__.py`, `mock_store.py`, `tools.py`, `prompts.py`, `controls.py`
2. Implement `MockStore.dispatch()` enforcing all controls
3. Define tool schemas, system prompts, control rules
4. Implement `rejection_signals()` for judge verification
5. Export `DOMAIN_SPEC` from `__init__.py`
6. Register slug in `domains/__init__.py`
7. Run: `python run_pipeline.py --mode full --domain new-domain`

No changes to exploration_agent.py, adversarial_agent.py, judge_agent.py, or run_pipeline.py.

## What I Need From You

Based on this context, help me with whatever I ask — whether that's:
- Drafting a leadership presentation deck outline
- Creating a team training walkthrough
- Explaining a specific design decision in depth
- Writing onboarding docs for offshore QA
- Planning the next domain to add (inheritance funding, etc.)
- Answering technical questions about the architecture
- Preparing for interview questions about the framework
- Generating a demo script I can run live

Ask me what I need, and reference the architecture above in your answers. Be specific — use real file names, tool names, control names, and CLI commands.
