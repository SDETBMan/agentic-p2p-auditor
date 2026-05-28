"""
Pipeline orchestrator: exploration, adversarial, and full (merge + judge) modes.
Domain-agnostic: loads domain package via --domain flag.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import statistics
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domains import available_domains, load_domain
from domains._base import DomainSpec
from exploration_agent import run_exploration
from adversarial_agent import run_adversarial
from judge_agent import run_judge
from pipeline_agentops import (
    finalize_agent_trace,
    init_agentops_in_daemon_thread,
    is_agentops_enabled,
    start_agent_trace,
)


class LiveApiUnavailableError(RuntimeError):
    """Raised when --live is set but the domain's live API cannot be reached."""


# ---------------------------------------------------------------------------
# Release gate evaluation
# ---------------------------------------------------------------------------

# Severity tiers in descending order of impact
_SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "EVAL"]


def _evaluate_release_gate(
    judge_report: dict[str, Any] | None,
    domain: DomainSpec,
) -> dict[str, Any]:
    """Evaluate the judge report against domain control severities.

    Returns a dict with:
        signal: PASS | WARN | BLOCK
        exit_code: 0 (PASS/WARN) or 1 (BLOCK)
        breached: list of {rule, severity} for breached controls
        held: list of {rule, severity} for held controls
        detail: human-readable summary
    """
    if judge_report is None:
        return {
            "signal": "BLOCK",
            "exit_code": 1,
            "breached": [],
            "held": [],
            "detail": "No judge report produced — cannot verify controls.",
        }

    # Build severity lookup from domain control rules
    severity_map: dict[str, str] = {}
    for rule in domain.control_rules:
        severity_map[rule["name"]] = rule.get("severity", "MEDIUM")

    adversarial = judge_report.get("adversarial", [])
    breached: list[dict[str, str]] = []
    held: list[dict[str, str]] = []

    for item in adversarial:
        if not isinstance(item, dict):
            continue
        rule = item.get("rule", "")
        status = item.get("status", "")
        # Fuzzy-match rule name against domain control rules
        severity = _match_severity(rule, severity_map)
        entry = {"rule": rule, "severity": severity, "status": status}
        if status == "BREACHED":
            breached.append(entry)
        elif status == "HELD":
            held.append(entry)

    # Check happy path
    hp = judge_report.get("happy_path", {})
    hp_failed = isinstance(hp, dict) and hp.get("status") == "FAIL"

    # Determine gate signal
    if hp_failed:
        return {
            "signal": "BLOCK",
            "exit_code": 1,
            "breached": breached,
            "held": held,
            "detail": "Happy path FAILED — system does not function correctly under normal operation.",
        }

    critical_breached = [b for b in breached if b["severity"] == "CRITICAL"]
    high_breached = [b for b in breached if b["severity"] == "HIGH"]
    medium_breached = [b for b in breached if b["severity"] == "MEDIUM"]

    if critical_breached:
        rules = ", ".join(b["rule"] for b in critical_breached)
        return {
            "signal": "BLOCK",
            "exit_code": 1,
            "breached": breached,
            "held": held,
            "detail": f"CRITICAL control(s) breached: {rules}. Deploy must not proceed.",
        }

    if high_breached:
        rules = ", ".join(b["rule"] for b in high_breached)
        return {
            "signal": "WARN",
            "exit_code": 0,
            "breached": breached,
            "held": held,
            "detail": f"HIGH control(s) breached: {rules}. Deploy blocked unless override ticket filed.",
        }

    if medium_breached:
        rules = ", ".join(b["rule"] for b in medium_breached)
        return {
            "signal": "WARN",
            "exit_code": 0,
            "breached": breached,
            "held": held,
            "detail": f"MEDIUM control(s) breached: {rules}. Alert created; deploy proceeds.",
        }

    return {
        "signal": "PASS",
        "exit_code": 0,
        "breached": breached,
        "held": held,
        "detail": "All controls held. Deploy proceeds.",
    }


def _match_severity(rule_name: str, severity_map: dict[str, str]) -> str:
    """Match a judge-reported rule name to a domain control severity.

    The judge may use slightly different rule names (e.g., "overpayment protection"
    vs "overpayment_protection"), so we normalize and do substring matching.
    """
    # Exact match first
    if rule_name in severity_map:
        return severity_map[rule_name]
    # Normalize: lowercase, replace spaces/hyphens with underscores
    normalized = rule_name.lower().replace(" ", "_").replace("-", "_")
    if normalized in severity_map:
        return severity_map[normalized]
    # Substring match: check if any control name is contained in the rule name
    for control_name, severity in severity_map.items():
        cn = control_name.lower()
        rn = normalized
        if cn in rn or rn in cn:
            return severity
    # Default to MEDIUM for unrecognized rules (edge cases the adversary found)
    return "MEDIUM"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_tool_events(stdout: str) -> list[dict[str, Any]]:
    """
    Parse exploration/adversarial/judge stdout for paired tool request/response blocks.
    """
    events: list[dict[str, Any]] = []
    lines = stdout.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        req_m = re.match(r"\s*--- iteration (\d+) tool request: (\w+) ---\s*$", line)
        if req_m:
            iteration = int(req_m.group(1))
            tool = req_m.group(2)
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("---"):
                buf.append(lines[i])
                i += 1
            raw_req = "\n".join(buf).strip()
            try:
                request = json.loads(raw_req) if raw_req else {}
            except json.JSONDecodeError:
                request = {"_parse_error": True, "raw": raw_req}
            if i < len(lines):
                resp_m = re.match(
                    r"\s*--- iteration (\d+) tool response: (\w+) ---\s*$",
                    lines[i],
                )
                if resp_m:
                    i += 1
                    buf2: list[str] = []
                    while i < len(lines) and not lines[i].strip().startswith("---"):
                        buf2.append(lines[i])
                        i += 1
                    raw_resp = "\n".join(buf2).strip()
                    try:
                        response = json.loads(raw_resp) if raw_resp else {}
                    except json.JSONDecodeError:
                        response = {"_parse_error": True, "raw": raw_resp}
                    events.append(
                        {
                            "iteration": iteration,
                            "tool": tool,
                            "request": request,
                            "response": response,
                        }
                    )
                    continue
        i += 1
    return events


def _write_json_fsync(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _scores_from_judge_stdout(stdout: str) -> list[float]:
    scores: list[float] = []
    lines = stdout.splitlines()
    i = 0
    while i < len(lines):
        if re.match(r"\s*--- iteration \d+ tool response: score_finding ---\s*$", lines[i]):
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("---"):
                buf.append(lines[i])
                i += 1
            raw = "\n".join(buf).strip()
            try:
                obj = json.loads(raw)
                s = obj.get("score")
                if isinstance(s, (int, float)):
                    scores.append(float(s))
            except json.JSONDecodeError:
                pass
            continue
        i += 1
    return scores


def _markers(stdout: str) -> dict[str, bool]:
    return {
        "exploration_complete": "[[EXPLORATION_COMPLETE]]" in stdout,
        "adversarial_complete": "[[ADVERSARIAL_COMPLETE]]" in stdout,
        "judge_complete": "[[JUDGE_COMPLETE]]" in stdout,
    }


@dataclass
class PipelineResult:
    mode: str
    domain: str
    output_dir: Path
    test_report_path: Path | None = None
    judge_report_path: Path | None = None
    happy_path_status: str | None = None
    total_tool_events: int = 0
    verdict_found: str = ""
    judge_aggregate_confidence: float | None = None
    release_gate: dict[str, Any] | None = None
    flags: list[str] = field(default_factory=list)
    judge_report: dict[str, Any] | None = None
    agentops_urls: list[dict[str, str]] = field(default_factory=list)


def run_pipeline(
    mode: str,
    output_dir: Path,
    domain: DomainSpec,
    model: str | None = None,
    *,
    live: bool = False,
    live_url: str | None = None,
) -> PipelineResult:
    if model:
        os.environ["ANTHROPIC_MODEL"] = model

    try:
        init_agentops_in_daemon_thread(timeout_seconds=5.0)
    except Exception:
        pass

    base = (live_url or "http://localhost:8000").rstrip("/")
    if live:
        if domain.probe_live_api is None:
            raise LiveApiUnavailableError(
                f"Domain {domain.name!r} does not support live mode (no live adapter).\n"
                "Run without --live (mock tools)."
            )
        ok, detail = domain.probe_live_api(base)
        if not ok:
            raise LiveApiUnavailableError(
                f"Live API is not reachable at {base}.\n{detail}\n"
                "Start the server on that host/port or run without --live (mock tools)."
            )
        os.environ["P2P_LIVE"] = "1"
        os.environ["P2P_LIVE_BASE_URL"] = base
    else:
        os.environ.pop("P2P_LIVE", None)
        os.environ.pop("P2P_LIVE_BASE_URL", None)

    out = output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)

    result = PipelineResult(mode=mode, domain=domain.name, output_dir=out)
    tool_events: list[dict[str, Any]] = []
    exploration_log = ""
    adversarial_log = ""
    judge_log = ""

    if mode in ("explore", "full"):
        store = domain.MockStore()
        ao_trace = start_agent_trace(
            "exploration",
            pipeline_mode=mode,
            live=bool(live),
            domain_name=domain.name,
        )
        buf = io.StringIO()
        exc: BaseException | None = None
        try:
            with redirect_stdout(buf):
                run_exploration(domain, store=store, live=live)
        except BaseException as e:
            exc = e
        exploration_log = buf.getvalue()
        ev_expl = parse_tool_events(exploration_log)
        for ev in ev_expl:
            tool_events.append({"phase": "exploration", **ev})
        ok_exp = bool(exploration_log) and _markers(exploration_log)["exploration_complete"]
        if exc is not None:
            ok_exp = False
        url_e = finalize_agent_trace(
            ao_trace,
            agent_name="exploration",
            pipeline_mode=mode,
            live=bool(live),
            tool_events=[{"phase": "exploration", **e} for e in ev_expl],
            marker_reached=ok_exp,
        )
        if url_e:
            result.agentops_urls.append({"agent": "exploration", "url": url_e})
        (out / "exploration_run.log").write_text(exploration_log, encoding="utf-8")
        if not _markers(exploration_log)["exploration_complete"]:
            result.flags.append("exploration_marker_missing")
        if exc is not None:
            raise exc

    if mode in ("adversarial", "full"):
        ao_trace = start_agent_trace(
            "adversarial",
            pipeline_mode=mode,
            live=bool(live),
            domain_name=domain.name,
        )
        buf = io.StringIO()
        exc = None
        try:
            with redirect_stdout(buf):
                run_adversarial(domain, live=live)
        except BaseException as e:
            exc = e
        adversarial_log = buf.getvalue()
        ev_adv = parse_tool_events(adversarial_log)
        for ev in ev_adv:
            tool_events.append({"phase": "adversarial", **ev})
        ok_adv = bool(adversarial_log) and _markers(adversarial_log)["adversarial_complete"]
        if exc is not None:
            ok_adv = False
        url_a = finalize_agent_trace(
            ao_trace,
            agent_name="adversarial",
            pipeline_mode=mode,
            live=bool(live),
            tool_events=[{"phase": "adversarial", **e} for e in ev_adv],
            marker_reached=ok_adv,
        )
        if url_a:
            result.agentops_urls.append({"agent": "adversarial", "url": url_a})
        (out / "adversarial_run.log").write_text(adversarial_log, encoding="utf-8")
        if not _markers(adversarial_log)["adversarial_complete"]:
            result.flags.append("adversarial_marker_missing")
        if exc is not None:
            raise exc

    verdict_parts: list[str] = []
    if exploration_log and _markers(exploration_log)["exploration_complete"]:
        verdict_parts.append("EXPLORATION_COMPLETE")
    if adversarial_log and _markers(adversarial_log)["adversarial_complete"]:
        verdict_parts.append("ADVERSARIAL_COMPLETE")
    result.verdict_found = ",".join(verdict_parts) if verdict_parts else "none"

    if mode == "full":
        expl_path = out / "exploration_run.log"
        adv_path = out / "adversarial_run.log"
        ao_trace = start_agent_trace(
            "judge",
            pipeline_mode=mode,
            live=bool(live),
            domain_name=domain.name,
        )
        jbuf = io.StringIO()
        exc = None
        jr: dict[str, Any] | None = None
        try:
            with redirect_stdout(jbuf):
                jr = run_judge(
                    domain,
                    exploration_path=str(expl_path),
                    adversarial_path=str(adv_path),
                    model=model,
                )
        except BaseException as e:
            exc = e
        judge_log = jbuf.getvalue()
        ev_j = parse_tool_events(judge_log)
        for ev in ev_j:
            tool_events.append({"phase": "judge", **ev})
        ok_j = bool(judge_log) and _markers(judge_log)["judge_complete"]
        if exc is not None:
            ok_j = False
        url_j = finalize_agent_trace(
            ao_trace,
            agent_name="judge",
            pipeline_mode=mode,
            live=bool(live),
            tool_events=[{"phase": "judge", **e} for e in ev_j],
            marker_reached=ok_j,
        )
        if url_j:
            result.agentops_urls.append({"agent": "judge", "url": url_j})
        (out / "judge_run.log").write_text(judge_log, encoding="utf-8")
        if exc is not None:
            raise exc

        if not _markers(judge_log)["judge_complete"]:
            result.flags.append("judge_marker_missing")

        scores = _scores_from_judge_stdout(judge_log)
        if scores:
            result.judge_aggregate_confidence = float(statistics.mean(scores))
        elif isinstance(jr, dict):
            hp = jr.get("happy_path")
            if isinstance(hp, dict) and hp.get("status") == "PASS":
                result.judge_aggregate_confidence = 1.0
            elif isinstance(hp, dict) and hp.get("status") == "FAIL":
                result.judge_aggregate_confidence = 0.0
            else:
                result.judge_aggregate_confidence = None
        else:
            result.judge_aggregate_confidence = None

        if jr is None:
            result.flags.append("judge_report_missing")
            result.happy_path_status = None
        else:
            result.judge_report = jr
            hp = jr.get("happy_path") if isinstance(jr, dict) else None
            if isinstance(hp, dict):
                result.happy_path_status = str(hp.get("status", ""))
            jr_path = out / "judge_report.json"
            _write_json_fsync(jr_path, jr)
            result.judge_report_path = jr_path

        if result.happy_path_status == "FAIL":
            result.flags.append("happy_path_failed")

        # Evaluate release gate against domain control severities
        result.release_gate = _evaluate_release_gate(jr, domain)
    else:
        result.judge_report_path = None
        result.judge_aggregate_confidence = None
        result.happy_path_status = "N/A"

    test_report: dict[str, Any] = {
        "version": 2,
        "domain": domain.name,
        "mode": mode,
        "live": bool(live),
        "live_base_url": base if live else None,
        "agentops_enabled": bool(is_agentops_enabled()),
        "agentops_session_urls": list(result.agentops_urls),
        "generated_at": _utc_now_iso(),
        "output_dir": str(out),
        "release_gate": result.release_gate,
        "tool_events": tool_events,
        "phases": {},
    }
    if exploration_log:
        test_report["phases"]["exploration"] = {
            "log_path": str(out / "exploration_run.log"),
            "markers": _markers(exploration_log),
            "tool_event_count": sum(1 for e in tool_events if e.get("phase") == "exploration"),
        }
    if adversarial_log:
        test_report["phases"]["adversarial"] = {
            "log_path": str(out / "adversarial_run.log"),
            "markers": _markers(adversarial_log),
            "tool_event_count": sum(1 for e in tool_events if e.get("phase") == "adversarial"),
        }
    if mode == "full" and judge_log:
        test_report["phases"]["judge"] = {
            "log_path": str(out / "judge_run.log"),
            "markers": _markers(judge_log),
            "tool_event_count": sum(1 for e in tool_events if e.get("phase") == "judge"),
        }

    tr_path = out / "test_report.json"
    _write_json_fsync(tr_path, test_report)
    result.test_report_path = tr_path
    result.total_tool_events = len(tool_events)

    if mode != "full" and result.total_tool_events == 0:
        result.flags.append("no_tool_events_parsed")

    return result


def _print_summary(r: PipelineResult, *, live: bool = False) -> None:
    print()
    print("========== PIPELINE SUMMARY ==========")
    print(f"domain:                {r.domain}")
    print(f"live (HTTP):           {live}")
    print(f"AgentOps tracing:      {is_agentops_enabled()}")
    if r.agentops_urls:
        print("AgentOps session URLs (open in browser):")
        for row in r.agentops_urls:
            print(f"  {row.get('agent', '?')}: {row.get('url', '')}")
    else:
        print("AgentOps session URLs:  N/A (set AGENTOPS_API_KEY or check init timeout)")
    print(f"mode:                  {r.mode}")
    print(f"test report file:      {r.test_report_path}")
    print(f"judge JSON report:     {r.judge_report_path or 'N/A'}")
    print(
        "happy_path status:     "
        + (str(r.happy_path_status) if r.happy_path_status is not None else "N/A")
    )
    print(f"total tool events:     {r.total_tool_events}")
    print(f"verdict found:         {r.verdict_found}")
    conf = r.judge_aggregate_confidence
    print(
        "judge agg. confidence: "
        + (f"{conf:.4f}" if isinstance(conf, float) else "N/A")
    )
    if r.release_gate:
        gate = r.release_gate
        signal = gate.get("signal", "N/A")
        detail = gate.get("detail", "")
        breached = gate.get("breached", [])
        held = gate.get("held", [])
        print(f"release gate:          {signal}")
        if breached:
            print(f"  breached ({len(breached)}):")
            for b in breached:
                print(f"    [{b['severity']}] {b['rule']}")
        if held:
            print(f"  held ({len(held)}):")
            for h in held:
                print(f"    [{h['severity']}] {h['rule']}")
        print(f"  detail: {detail}")
    else:
        print("release gate:          N/A (full mode only)")
    print(f"flags raised:          {r.flags if r.flags else '[]'}")
    print("======================================")
    print()


def _parse_args() -> argparse.Namespace:
    avail = ", ".join(available_domains())
    p = argparse.ArgumentParser(description="Run exploration / adversarial / full QA pipeline.")
    p.add_argument(
        "--mode",
        choices=("explore", "adversarial", "full"),
        required=True,
        help="explore: exploration agent only; adversarial: adversarial only; full: both + judge",
    )
    p.add_argument(
        "--domain",
        default="p2p",
        help=f"Domain to audit. Available: {avail} (default: p2p)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("pipeline_output"),
        help="Directory for logs, test_report.json, and judge_report.json (full).",
    )
    p.add_argument("--model", default=None, help="Sets ANTHROPIC_MODEL for all agents.")
    p.add_argument(
        "--live",
        action="store_true",
        help="Use real HTTP calls instead of mocks (requires domain live adapter).",
    )
    p.add_argument(
        "--live-url",
        default=None,
        help="Base URL for live mode (default: http://localhost:8000).",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        domain = load_domain(args.domain)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    try:
        r = run_pipeline(
            mode=args.mode,
            output_dir=args.output_dir,
            domain=domain,
            model=args.model,
            live=bool(args.live),
            live_url=args.live_url,
        )
    except LiveApiUnavailableError as e:
        print(str(e), file=sys.stderr)
        return 2
    except Exception as e:
        print(f"pipeline failed: {e}", file=sys.stderr)
        return 1
    _print_summary(r, live=bool(args.live))

    # Return release gate exit code: 1 for BLOCK, 0 otherwise
    if r.release_gate:
        return r.release_gate.get("exit_code", 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
