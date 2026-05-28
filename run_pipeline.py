"""
Pipeline orchestrator: exploration, adversarial, and full (merge + judge) modes.
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

import exploration_agent as ex
from adversarial_agent import run_adversarial
from exploration_agent import run_exploration
from judge_agent import run_judge
from p2p_live import LiveApiUnavailableError, probe_live_api
from pipeline_agentops import (
    finalize_agent_trace,
    init_agentops_in_daemon_thread,
    is_agentops_enabled,
    start_agent_trace,
)


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
    output_dir: Path
    test_report_path: Path | None = None
    judge_report_path: Path | None = None
    happy_path_status: str | None = None
    total_tool_events: int = 0
    verdict_found: str = ""
    judge_aggregate_confidence: float | None = None
    flags: list[str] = field(default_factory=list)
    judge_report: dict[str, Any] | None = None
    agentops_urls: list[dict[str, str]] = field(default_factory=list)


def run_pipeline(
    mode: str,
    output_dir: Path,
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
        ok, detail = probe_live_api(base)
        if not ok:
            raise LiveApiUnavailableError(
                f"Live P2P API is not reachable at {base}.\n{detail}\n"
                "Start the server on that host/port or run without --live (mock tools)."
            )
        os.environ["P2P_LIVE"] = "1"
        os.environ["P2P_LIVE_BASE_URL"] = base
    else:
        os.environ.pop("P2P_LIVE", None)
        os.environ.pop("P2P_LIVE_BASE_URL", None)

    out = output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)

    result = PipelineResult(mode=mode, output_dir=out)
    tool_events: list[dict[str, Any]] = []
    exploration_log = ""
    adversarial_log = ""
    judge_log = ""

    if mode in ("explore", "full"):
        ex.STORE = ex.MockP2PStore()
        ao_trace = start_agent_trace(
            "exploration",
            pipeline_mode=mode,
            live=bool(live),
        )
        buf = io.StringIO()
        exc: BaseException | None = None
        try:
            with redirect_stdout(buf):
                run_exploration()
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
        )
        buf = io.StringIO()
        exc = None
        try:
            with redirect_stdout(buf):
                run_adversarial()
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
        )
        jbuf = io.StringIO()
        exc = None
        jr: dict[str, Any] | None = None
        try:
            with redirect_stdout(jbuf):
                jr = run_judge(
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
    else:
        result.judge_report_path = None
        result.judge_aggregate_confidence = None
        result.happy_path_status = "N/A"

    test_report: dict[str, Any] = {
        "version": 1,
        "mode": mode,
        "live": bool(live),
        "live_base_url": base if live else None,
        "agentops_enabled": bool(is_agentops_enabled()),
        "agentops_session_urls": list(result.agentops_urls),
        "generated_at": _utc_now_iso(),
        "output_dir": str(out),
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
    print(f"flags raised:          {r.flags if r.flags else '[]'}")
    print("======================================")
    print()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run exploration / adversarial / full P2P QA pipeline.")
    p.add_argument(
        "--mode",
        choices=("explore", "adversarial", "full"),
        required=True,
        help="explore: exploration agent only; adversarial: adversarial only; full: both + judge",
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
        help="Use real HTTP calls to P2P_LIVE_BASE_URL (default http://localhost:8000) instead of mocks.",
    )
    p.add_argument(
        "--live-url",
        default=None,
        help="Base URL for live mode (default: http://localhost:8000). Endpoints from p2p_api_spec.md.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        r = run_pipeline(
            mode=args.mode,
            output_dir=args.output_dir,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
