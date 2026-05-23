#!/usr/bin/env python3
"""
agentic_orchestrator.py — agentic flaky-test repair driver.

Replaces the legacy `assemble_llm_context_*.py` + `call_llm_*.py` +
feedback_loop steps in the original ReproFlake pipeline with a single
iterative agent loop.

Architecture (matches the agentic workflow doc):

  Step 1 — Initialization
    Build a minimal initial prompt: GOAL + relevant test name(s) +
    flaky-test category + initial failure log (extracted from the
    appropriate traces-*/mvn.log under data/<container>/). Iteration
    counter starts at 0. No source code is pre-bundled.

  Step 2 — Planning & Context Collection Loop
    The model calls one or more of the read-only context tools
    (get_test_code, get_code, get_error_logs, get_flaky_example,
    get_rv_trace_diff). Each call's output is handed back as a
    tool_result content block.

  Step 3 — Sufficiency Check
    Implicit: the model continues calling context tools until it
    decides it has enough evidence to commit to a fix. We do not gate
    on this; the model controls the cadence.

  Step 4 — Patch Generation, Application & Validation
    When the model calls `submit_patch`, this script:
      (a) writes llm_response.json in the same shape apply_fix.py
          consumes (output_a.patch + output_b.fixed_code + diagnosis +
          root_cause + fix_description)
      (b) runs apply_fix.py against Flaky/
      (c) on apply success, runs agentic_verify.py against the patched
          tree inside the running docker container

  Step 5 — Evaluation & Feedback
    Verdict PASSED -> success, loop exits with verdict file written.
    Verdict FAILED -> restore Flaky/ from Flaky.pristine, hand back a
    tool_result describing what went wrong (apply error vs compile
    error vs test still failing, plus the relevant log tail), and let
    the agent submit another patch.

  Step 6 — Termination
    Hard iteration cap MAX_ITERATIONS (default 10). If hit, write
    INCOMPLETE to verdict and exit failure.

The orchestrator preserves the same on-disk artifacts the non-agentic
pipeline produced where they overlap:
    Steps_Output_Files/
        llm_context.txt              — initial prompt (for parity / audit)
        llm_response.json            — final-iteration submit_patch payload
        agentic_conversation.json    — full transcript of all turns + tools
        agentic_iterations.jsonl     — per-iteration summary (verdict, etc.)
        apply_report.json            — written by apply_fix.py
        verify_after_fix.log         — written by agentic_verify.py
        verify_after_fix.verdict     — PASSED | FAILED | INCOMPLETE

Usage:
    python3 agentic_orchestrator.py <result_container>
                                    [--docker-container NAME]
                                    [--max-iterations N]
                                    [--model claude-sonnet-4-6]

Requires:
    ANTHROPIC_API_KEY in env
    pip install anthropic
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from anthropic import Anthropic
except ImportError:
    print("ERROR: anthropic package not installed. Run: "
          "py -3 -m pip install anthropic", file=sys.stderr)
    sys.exit(1)

# Local imports: agent_tools sits next to this file; the legacy LLM Scripts
# helpers live one folder up.
SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
LLM_SCRIPTS_DIR = REPROFLAKE_DIR / "LLM Scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(LLM_SCRIPTS_DIR))

import agent_tools  # type: ignore  # noqa: E402
import agentic_config  # type: ignore  # noqa: E402
import prompts  # type: ignore  # noqa: E402
from assemble_llm_context import (  # type: ignore  # noqa: E402
    DATA_DIR,
    load_csv_row,
    extract_failure_from_log,
)

# All tuneable constants come from agentic_config.py. Import them into the
# module namespace so the rest of the file reads identically to before.
DEFAULT_MODEL               = agentic_config.DEFAULT_MODEL
MAX_TOKENS                  = agentic_config.MAX_TOKENS
TEMPERATURE                 = agentic_config.TEMPERATURE
MAX_TOOL_TURNS_PER_ITERATION = agentic_config.MAX_TOOL_TURNS_PER_ITERATION
DEFAULT_MAX_ITERATIONS      = agentic_config.MAX_ITERATIONS
TOOL_OUTPUT_MAX_CHARS       = agentic_config.TOOL_OUTPUT_MAX_CHARS
VERIFY_PASS_RUNS            = agentic_config.VERIFY_PASS_RUNS


# ---------------------------------------------------------------------------
# Prompt construction — templates live in prompts.py (edit that file)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = prompts.SYSTEM_PROMPT

_PRETTY_TYPE = {
    "od":           "OD (Order-Dependent — a polluter test corrupts shared state)",
    "td":           "TD (Timing-Dependent — race, async, or non-deterministic source)",
    "id":           "ID (Implementation-Dependent — relies on JVM iteration order)",
    "nio":          "NIO (Non-Idempotent-Outcome — self-pollutes across same-JVM invocations)",
    "brittle":      "Brittle (Order-Dependent variant — polluter corrupts shared state; "
                    "structurally identical to OD)",
    "unclassified": "Unclassified (root cause unknown — no category-specific exemplar "
                    "is available; diagnose from code and error logs alone)",
}


def _build_initial_user_prompt(container: str, row: dict,
                               failure_text: str) -> str:
    """Render prompts.INITIAL_USER_TEMPLATE with the run-specific values.
    Edit prompts.py to change the prompt structure.
    """
    test_type  = (row.get("test_type") or "").strip().lower()
    victim_fqn = (row.get("flaky_test") or "").strip()
    polluter   = (row.get("polluter/state setter") or "").strip()
    module     = (row.get("module") or ".").strip()
    java_ver   = (row.get("java") or "").strip()

    return prompts.INITIAL_USER_TEMPLATE.format(
        container    = container,
        pretty_type  = _PRETTY_TYPE.get(test_type, test_type.upper()),
        polluter_line= f"Polluter:   {polluter}\n" if polluter else "",
        victim_fqn   = victim_fqn,
        module       = module,
        java_line    = f"Java:       {java_ver}\n" if java_ver else "",
        failure_text = failure_text.strip() or "(no failure block was extracted)",
    ).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Tool schemas — context tools come from agent_tools; submit_patch is local.
# ---------------------------------------------------------------------------

SUBMIT_PATCH_SCHEMA = {
    "name": "submit_patch",
    "description": (
        "Terminal action: submit the proposed fix for this iteration. The "
        "orchestrator will (1) write llm_response.json, (2) apply the patch "
        "to Flaky/ via apply_fix.py, (3) recompile in the docker container, "
        "(4) re-run the original failing test command. You will receive a "
        "report describing what happened (applied? compiled? passed?). If "
        "the patch fails, Flaky/ is restored to its pre-patch state and you "
        "can try again. Provide BOTH a unified diff in `patch` AND a "
        "structured `fixed_code` list — the diff is preferred, the "
        "structured list is the fallback applier."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "diagnosis": {
                "type": "string",
                "description": (
                    "Short chain-of-thought: what is the root cause, what "
                    "evidence confirms it, why is your chosen fix the "
                    "smallest correct one. Persisted in llm_response.json."
                ),
            },
            "root_cause": {
                "type": "string",
                "description": (
                    "2-4 sentences naming the underlying defect (not a "
                    "restatement of the diff)."
                ),
            },
            "fix_description": {
                "type": "string",
                "description": (
                    "2-4 sentences: which file(s) you edit, what you "
                    "add/remove/change, and why that addresses the root cause."
                ),
            },
            "patch": {
                "type": "string",
                "description": (
                    "Unified diff applied with `git apply --recount`. Use "
                    "absolute paths from the project root. Hunk headers "
                    "'@@ -L +L @@' (no counts) are accepted; --recount fixes "
                    "off-by-one counts. Every non-empty hunk-body line MUST "
                    "start with ' ', '+' or '-'."
                ),
            },
            "fixed_code": {
                "type": "array",
                "description": (
                    "Structured fallback applier. One entry per modified "
                    "method. The orchestrator falls back to splicing these "
                    "into Flaky/ if the unified diff fails to apply."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string",
                                 "description": "path relative to project root"},
                        "imports": {"type": "string",
                                    "description": "new imports to add, one per line; empty if none"},
                        "method": {"type": "string"},
                        "operation": {
                            "type": "string",
                            "enum": ["replace_method", "insert_method"],
                        },
                        "anchor": {
                            "type": "string",
                            "description": (
                                "Required when operation='insert_method'. "
                                "Forms: 'before_method=NAME', 'after_method=NAME', "
                                "'end_of_class'."
                            ),
                        },
                        "code": {
                            "type": "string",
                            "description": (
                                "Complete method source including annotations, "
                                "signature, body, and closing brace."
                            ),
                        },
                    },
                    "required": ["file", "method", "operation", "code"],
                },
            },
        },
        "required": ["diagnosis", "root_cause", "fix_description",
                     "patch", "fixed_code"],
    },
}


def _all_tool_schemas() -> list[dict]:
    return list(agent_tools.TOOL_SCHEMAS) + [SUBMIT_PATCH_SCHEMA]


# ---------------------------------------------------------------------------
# Patch application + verification
# ---------------------------------------------------------------------------

def _write_llm_response_json(steps_dir: Path, container: str,
                             args_dict: dict, iteration: int) -> Path:
    """Write the submit_patch payload in the legacy `llm_response.json`
    shape so apply_fix.py consumes it unchanged.
    """
    response_path = steps_dir / "llm_response.json"
    # Coerce missing fields defensively — the tool schema requires them, but
    # being explicit here means a malformed call still produces a file
    # apply_fix.py can attempt to read.
    payload = {
        "model": "claude-sonnet-4-6",
        "result_container": container,
        "iteration": iteration,
        "stop_reason": "tool_use",
        "turns_taken": iteration,
        "raw_response": json.dumps(args_dict, ensure_ascii=False),
        "response": {
            "output_0": {"diagnosis": args_dict.get("diagnosis") or ""},
            "output_a": {"patch": args_dict.get("patch") or ""},
            "output_b": {
                "root_cause": args_dict.get("root_cause") or "",
                "fix_description": args_dict.get("fix_description") or "",
                "fixed_code": args_dict.get("fixed_code") or [],
            },
        },
    }
    response_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8")
    return response_path


def _restore_flaky(base: Path) -> None:
    """Restore Flaky/ from the snapshot the per-type orchestrator made
    at step 9.5. Required between iterations so each submit_patch starts
    against a clean tree, identical to the feedback_loop.sh contract.
    """
    pristine = base / "Flaky.pristine"
    flaky = base / "Flaky"
    if not pristine.is_dir():
        print(f"[restore] WARNING: {pristine} missing — cannot restore Flaky/.")
        return
    if flaky.is_dir():
        shutil.rmtree(flaky)
    shutil.copytree(pristine, flaky, symlinks=True)


def _run_apply_fix(container: str, docker_container: str) -> dict:
    """Invoke apply_fix.py and return the parsed apply_report.json."""
    cmd = [
        sys.executable, str(LLM_SCRIPTS_DIR / "apply_fix.py"),
        container, "--docker-container", docker_container,
    ]
    print(f"[apply ] {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # apply_fix.py prints a summary to stdout; we mirror it onto our log.
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stdout.write(proc.stderr)

    report_path = (Path(DATA_DIR) / container
                   / "Steps_Output_Files" / "apply_report.json")
    if not report_path.is_file():
        return {
            "result": {"ok": False, "layer": None,
                       "reason": "apply_fix.py did not produce apply_report.json"},
            "layers_attempted": [],
        }
    return json.loads(report_path.read_text(encoding="utf-8"))


def _run_verify(container: str, docker_container: str) -> tuple[str, str]:
    """Invoke agentic_verify.py. Returns (verdict, log_tail)."""
    cmd = [
        sys.executable, str(SCRIPT_DIR / "agentic_verify.py"),
        container, "--docker-container", docker_container,
    ]
    print(f"[verify] {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env={**os.environ})
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0 and proc.stderr:
        sys.stderr.write(proc.stderr)

    verdict_path = (Path(DATA_DIR) / container
                    / "Steps_Output_Files" / "verify_after_fix.verdict")
    log_path = (Path(DATA_DIR) / container
                / "Steps_Output_Files" / "verify_after_fix.log")
    verdict = verdict_path.read_text(encoding="utf-8").strip() \
        if verdict_path.is_file() else "FAILED"
    log_text = ""
    if log_path.is_file():
        lines = log_path.read_text(encoding="utf-8",
                                   errors="replace").splitlines()
        log_text = "\n".join(lines[-120:]) if len(lines) > 120 else \
            "\n".join(lines)
    return verdict, log_text


def _classify_failure(apply_report: dict, verdict: str) -> str:
    """Mirror feedback_loop.sh's classifier so the agent sees the same
    category names it might already have seen examples of."""
    result = apply_report.get("result") or {}
    if not result.get("ok") and result.get("layer") in (None, "none"):
        return "patch_apply_failed"
    rc = apply_report.get("recompile") or {}
    if rc.get("ok") is False and not rc.get("skipped"):
        return "compile_failed"
    if verdict != "PASSED":
        return "test_failed"
    return "unknown_failure"


def _restrategy_hint(category: str) -> str:
    """Structured checklist appended to failure reports to prompt the agent
    to reconsider its approach before the next submit_patch attempt."""
    hints = {
        "patch_apply_failed": (
            "Re-strategize: the diff did not apply cleanly.\n"
            "  • Verify the file path (relative to project root).\n"
            "  • Check context lines match exactly (whitespace, encoding).\n"
            "  • Use a smaller diff targeting only the changed lines.\n"
            "  • Ensure fixed_code entries cover the same change as a fallback."
        ),
        "compile_failed": (
            "Re-strategize: the patched project does not compile.\n"
            "  • Read the compile error above and fix the import or syntax issue.\n"
            "  • Use get_code to re-read the class before retrying.\n"
            "  • Check that any new annotations (e.g. @After) are imported."
        ),
        "test_failed": (
            "Re-strategize: the test still fails after the patch.\n"
            "  • The error log above shows what assertion or exception is still triggered.\n"
            "  • Consider whether you have the right root cause — use get_rv_trace_diff "
            "for runtime evidence.\n"
            "  • Check for shared state that is NOT reset by your fix.\n"
            "  • Ensure your cleanup/init targets the correct lifecycle method "
            "(@Before vs @BeforeClass, @After vs @AfterClass)."
        ),
        "confirm_failed": (
            "Re-strategize: the fix is non-deterministic (passed once, "
            "then failed in a confirmation run).\n"
            "  • A race condition or ordering sensitivity may remain.\n"
            "  • Strengthen the cleanup: reset ALL shared state, not just the obvious fields.\n"
            "  • Consider whether @BeforeClass / @AfterClass scope is needed instead of "
            "@Before / @After.\n"
            "  • Use get_rv_trace_diff to look for spec violations that differ between runs."
        ),
    }
    hint = hints.get(category, (
        "Re-strategize: request more context with get_test_code, get_code, or "
        "get_rv_trace_diff before submitting the next patch."
    ))
    return f"\n=== RE-STRATEGIZE ===\n{hint}\n"


def _format_failure_report(apply_report: dict, verdict: str,
                           verify_tail: str) -> str:
    """Build the tool_result body for a failed submit_patch attempt.

    The shape is deliberately structured: the agent should be able to
    grep for the category, see which applier layer landed (or didn't),
    read the compile error tail, and read the surefire failure block.
    """
    category = _classify_failure(apply_report, verdict)
    result = apply_report.get("result") or {}
    layers = apply_report.get("layers_attempted") or []
    rc = apply_report.get("recompile") or {}
    compile_section = ""
    tail = rc.get("stderr_tail") or rc.get("stdout_tail") or ""
    if tail and not rc.get("skipped"):
        ok = "ok" if rc.get("ok") else "failed"
        compile_section = (
            f"\n--- mvn test-compile ({ok}); tail of output ---\n"
            f"{tail.rstrip()}\n")

    layers_section = "\n--- applier layers ---\n"
    for la in layers:
        layer = la.get("layer") or "?"
        ok = "ok" if la.get("ok") else "fail"
        reason = (la.get("reason") or "").splitlines()
        reason_short = " ".join(reason)[:300]
        layers_section += f"  - {layer:32s} {ok}  {reason_short}\n"

    verify_section = ""
    if verify_tail:
        verify_section = (
            "\n--- verify_after_fix.log (tail) ---\n"
            f"{verify_tail.rstrip()}\n")

    landed = result.get("layer") if result.get("ok") else None

    return (
        f"=== submit_patch attempt result: FAILED ===\n"
        f"category:        {category}\n"
        f"verdict:         {verdict}\n"
        f"applier landed:  {landed or 'no layer landed the fix'}\n"
        f"{layers_section.rstrip()}\n"
        f"{compile_section}"
        f"{verify_section}"
        f"\nFlaky/ has been restored to its pre-patch state. Read the "
        f"output above, decide whether you need more context, and submit "
        f"a corrected patch.\n"
        + _restrategy_hint(category)
    )


# ---------------------------------------------------------------------------
# Anthropic message helpers
# ---------------------------------------------------------------------------

def _usage_dict(response) -> dict:
    """Standard usage dict matching call_llm_claude.py's shape so parse_run
    in the pass@k harness can read tokens without per-script changes."""
    u = response.usage
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "total_tokens": u.input_tokens + u.output_tokens,
        "cache_read_input_tokens":
            getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens":
            getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def _sum_usage(*usages) -> dict:
    keys = ("input_tokens", "output_tokens", "total_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: sum(u.get(k, 0) for u in usages) for k in keys}


def _extract_assistant_blocks(response):
    """Return the list of content blocks the SDK returned, as plain dicts
    suitable to append back into the running messages list."""
    out = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            out.append({"type": "text", "text": block.text})
        elif getattr(block, "type", None) == "tool_use":
            out.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return out


# ---------------------------------------------------------------------------
# Per-run summary
# ---------------------------------------------------------------------------

def _tool_sequence_str(tools: list[str]) -> str:
    """'get_test_code → get_code → get_test_code' (shows repetitions)."""
    return " → ".join(tools) if tools else "(none)"


def _tool_counts_str(tools: list[str]) -> str:
    """'get_test_code×2, get_code×1' (ordered by first occurrence)."""
    seen: dict[str, int] = {}
    for t in tools:
        seen[t] = seen.get(t, 0) + 1
    return ", ".join(f"{t}×{n}" for t, n in seen.items()) if seen else "(none)"


_RUN_SUMMARY_COLS = [
    "iteration", "verdict", "category", "applied_ok",
    "tools_sequence", "tool_counts", "confirm_runs",
    "elapsed_seconds", "tokens_in", "tokens_out", "cache_read",
]


def _write_run_summary(path: "Path", container: str, model: str,
                       test_type: str, max_iters: int,
                       iter_rows: list[dict],
                       final_verdict: str, submit_attempts: int,
                       total_elapsed: float,
                       cumulative_usage: dict) -> None:
    """Write a CSV run_summary.csv — one row per submit_patch attempt plus
    a SUMMARY row with aggregated totals."""
    import csv as _csv
    import datetime

    with open(path, "w", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=_RUN_SUMMARY_COLS,
                            quoting=_csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        for row in iter_rows:
            confirms = row.get("confirm_runs", [])
            conf_str = (", ".join(f"run_{r['run']}={r['verdict']}"
                                  for r in confirms)
                        if confirms else "")
            w.writerow({
                "iteration":      row["iteration"],
                "verdict":        row["verdict"],
                "category":       row.get("category", ""),
                "applied_ok":     "yes" if row.get("applied_ok") else "no",
                "tools_sequence": _tool_sequence_str(row.get("tools_used", [])),
                "tool_counts":    _tool_counts_str(row.get("tools_used", [])),
                "confirm_runs":   conf_str,
                "elapsed_seconds": round(row.get("elapsed_seconds", 0.0), 1),
                "tokens_in":      row.get("tokens_in", 0),
                "tokens_out":     row.get("tokens_out", 0),
                "cache_read":     row.get("cache_read", 0),
            })
        w.writerow({
            "iteration":      "SUMMARY",
            "verdict":        final_verdict,
            "category":       "",
            "applied_ok":     f"{submit_attempts}/{max_iters}",
            "tools_sequence": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tool_counts":    f"model={model} test_type={test_type}",
            "confirm_runs":   "",
            "elapsed_seconds": round(total_elapsed, 1),
            "tokens_in":      cumulative_usage.get("input_tokens", 0),
            "tokens_out":     cumulative_usage.get("output_tokens", 0),
            "cache_read":     cumulative_usage.get("cache_read_input_tokens", 0),
        })


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("container")
    ap.add_argument("--docker-container",
                    help="docker container name (default tm_<container>)")
    ap.add_argument("--max-iterations", type=int,
                    default=DEFAULT_MAX_ITERATIONS,
                    help=f"hard cap on submit_patch attempts (default {DEFAULT_MAX_ITERATIONS})")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Anthropic model ID or alias (default: {DEFAULT_MODEL})")
    ap.add_argument("--exclude-tools", default="",
                    help="comma-separated tool names to remove from the agent's toolset "
                         "(e.g. get_flaky_example for unclassified tests)")
    args = ap.parse_args()

    row = load_csv_row(args.container)
    if not row:
        sys.exit(f"ERROR: container '{args.container}' not in test_config.csv")
    test_type = (row.get("test_type") or "").strip().lower()
    # normalise the brittle typo from the CSV
    if test_type == "britle":
        test_type = "brittle"
    if test_type not in {"od", "td", "id", "nio", "unclassified", "brittle"}:
        sys.exit(f"ERROR: unsupported test_type '{test_type}'")

    # API key: env var takes precedence, then agentic_config.ANTHROPIC_API_KEY.
    api_key = (os.environ.get("ANTHROPIC_API_KEY", "")
               or getattr(agentic_config, "ANTHROPIC_API_KEY", "")).strip()
    if not api_key:
        sys.exit("ERROR: ANTHROPIC_API_KEY is not set.\n"
                 "       Set it in agentic_config.py or export it as an env var.")

    docker_container = args.docker_container or (
        "tm_" + re.sub(r"[^a-zA-Z0-9]", "_", args.container))

    base = Path(DATA_DIR) / args.container
    steps_dir = base / "Steps_Output_Files"
    steps_dir.mkdir(parents=True, exist_ok=True)

    # The traces-*/mvn.log path differs per test_type. Use the same probe
    # order assemble_llm_context_*.py uses so the agent sees an identical
    # initial failure block to what the non-agentic pipeline would have.
    source_base = base
    zip_name = (row.get("zip") or "").strip()
    if zip_name and zip_name != args.container and \
       (Path(DATA_DIR) / zip_name / "Flaky" / "src").is_dir():
        source_base = Path(DATA_DIR) / zip_name
    failure_text = ""
    for cand in ("traces-flakycc", "traces-flaky", "traces-fail",
                 "traces-fixed"):
        text = extract_failure_from_log(
            str(source_base / cand / "mvn.log"))
        if not text.startswith("("):
            failure_text = text
            break
    if not failure_text:
        print("[init ] WARNING: no failure block found in any traces-*/mvn.log; "
              "agent will see an empty failure log section.")

    initial_user = _build_initial_user_prompt(args.container, row, failure_text)
    # For audit parity with the non-agentic pipeline, persist the initial
    # prompt under the same filename non-agentic uses.
    (steps_dir / "llm_context.txt").write_text(initial_user, encoding="utf-8")

    client = Anthropic(api_key=api_key)
    excluded_tools = {t.strip() for t in args.exclude_tools.split(",") if t.strip()}
    tools = [t for t in _all_tool_schemas() if t["name"] not in excluded_tools]
    if excluded_tools:
        print(f"[init ] excluded tools: {sorted(excluded_tools)}")

    messages = [{"role": "user", "content": initial_user}]
    iter_log_path = steps_dir / "agentic_iterations.jsonl"
    conv_path = steps_dir / "agentic_conversation.json"
    iter_log_path.unlink(missing_ok=True)
    conv_path.unlink(missing_ok=True)

    cumulative_usage = {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    total_elapsed = 0.0
    submit_attempts = 0
    final_verdict = "INCOMPLETE"
    final_category = ""
    last_apply_report: dict | None = None
    iter_summary_rows: list[dict] = []

    print(f"[init ] container={args.container}  test_type={test_type}  "
          f"model={args.model}  max_iterations={args.max_iterations}")

    # Outer loop = submit_patch attempts. Each iteration runs a nested
    # tool-use loop until the agent calls submit_patch (or the per-iter
    # tool-turn cap fires, which we treat as the agent stalling).
    for attempt in range(1, args.max_iterations + 1):
        print(f"\n[iter {attempt}/{args.max_iterations}] ============")
        t_iter_start = time.time()
        iter_start_usage = dict(cumulative_usage)
        tool_turn = 0
        submitted_this_iter = False
        # All context-tool calls made before submit_patch in this iteration,
        # recorded in order. Written to agentic_iterations.jsonl so runs can
        # be analysed to see which tools the agent found useful per attempt.
        tools_used_this_iter: list[str] = []

        while tool_turn < MAX_TOOL_TURNS_PER_ITERATION:
            tool_turn += 1
            t0 = time.time()
            response = client.messages.create(
                model=args.model,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )
            elapsed = time.time() - t0
            total_elapsed += elapsed
            usage = _usage_dict(response)
            cumulative_usage = _sum_usage(cumulative_usage, usage)

            print(f"[iter {attempt}/turn {tool_turn}] {elapsed:.1f}s  "
                  f"in={usage['input_tokens']} out={usage['output_tokens']}  "
                  f"cache_read={usage['cache_read_input_tokens']}  "
                  f"stop={response.stop_reason}")

            assistant_blocks = _extract_assistant_blocks(response)
            messages.append({"role": "assistant", "content": assistant_blocks})

            # If the model didn't request any tools, it's stalling — write
            # any text out, then end the iteration. (Without this guard, a
            # model that responds with prose instead of a tool_use would
            # loop indefinitely waiting for a tool_result that never comes.)
            tool_uses = [b for b in assistant_blocks if b["type"] == "tool_use"]
            if not tool_uses:
                print(f"[iter {attempt}] assistant returned no tool calls; "
                      f"ending iteration with no submit_patch.")
                break

            # Run each tool the model requested. submit_patch is terminal
            # within an iteration: after it fires, we stop processing
            # further tool calls in the same assistant turn and move on
            # to apply+verify.
            tool_results_block: list[dict] = []
            submit_args = None
            submit_tool_use_id = None
            for tu in tool_uses:
                if tu["name"] == "submit_patch":
                    submit_args = tu["input"] or {}
                    submit_tool_use_id = tu["id"]
                    # Don't execute the other tools — they're moot now.
                    break
                tools_used_this_iter.append(tu["name"])
                result_text = agent_tools.dispatch_tool(
                    args.container, tu["name"], tu["input"] or {})
                # Bound each tool's output so a stray multi-megabyte file
                # can't blow our context. Threshold from agentic_config.
                if len(result_text) > TOOL_OUTPUT_MAX_CHARS:
                    result_text = (
                        result_text[:TOOL_OUTPUT_MAX_CHARS]
                        + f"\n\n(tool output truncated at "
                          f"{TOOL_OUTPUT_MAX_CHARS} chars)\n")
                tool_results_block.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": result_text,
                })

            if submit_args is None:
                # Pure context-tool turn: send the tool_results back and
                # let the agent iterate.
                remaining = MAX_TOOL_TURNS_PER_ITERATION - tool_turn
                if remaining <= 3:
                    nudge = (
                        f"\n[SYSTEM] WARNING: you have {remaining} tool "
                        f"turn(s) left in this iteration (cap = "
                        f"{MAX_TOOL_TURNS_PER_ITERATION}). You MUST call "
                        f"submit_patch within the next {remaining} turn(s) "
                        f"or this iteration will be abandoned as INCOMPLETE. "
                        f"Commit to your best fix now."
                    )
                    tool_results_block.append(
                        {"type": "text", "text": nudge})
                messages.append({"role": "user",
                                 "content": tool_results_block})
                continue

            # submit_patch fired — exit the inner loop and run apply+verify.
            submit_attempts += 1
            submitted_this_iter = True

            print(f"[iter {attempt}] submit_patch received "
                  f"({len(submit_args.get('patch') or '')} char diff, "
                  f"{len(submit_args.get('fixed_code') or [])} fixed_code entries)")

            _write_llm_response_json(steps_dir, args.container,
                                     submit_args, attempt)

            apply_report = _run_apply_fix(args.container, docker_container)
            last_apply_report = apply_report
            applied_ok = bool((apply_report.get("result") or {}).get("ok"))

            verdict = "FAILED"
            verify_tail = ""
            if applied_ok:
                verdict, verify_tail = _run_verify(
                    args.container, docker_container)
            else:
                # Write a FAILED verdict so downstream artefacts are
                # consistent even when apply failed before verify ran.
                (steps_dir / "verify_after_fix.verdict").write_text(
                    "FAILED\n", encoding="utf-8")

            final_category = _classify_failure(apply_report, verdict)

            # If the first verify passed, run VERIFY_PASS_RUNS additional
            # confirmation passes to ensure the fix is deterministic.
            confirm_runs: list[dict] = []
            if verdict == "PASSED":
                for confirm_num in range(1, VERIFY_PASS_RUNS + 1):
                    c_verdict, c_tail = _run_verify(
                        args.container, docker_container)
                    confirm_runs.append(
                        {"run": confirm_num, "verdict": c_verdict})
                    print(f"[confirm {confirm_num}/{VERIFY_PASS_RUNS}] "
                          f"{c_verdict}")
                    if c_verdict != "PASSED":
                        verdict = c_verdict
                        verify_tail = c_tail
                        final_category = _classify_failure(
                            apply_report, verdict)
                        break

            iter_elapsed = round(time.time() - t_iter_start, 2)
            iter_delta = {
                k: cumulative_usage.get(k, 0) - iter_start_usage.get(k, 0)
                for k in cumulative_usage
            }
            iter_row = {
                "iteration": attempt,
                "tool_turns": tool_turn,
                "tools_used": tools_used_this_iter,
                "verdict": verdict,
                "category": final_category,
                "applied_ok": applied_ok,
                "elapsed_seconds": iter_elapsed,
                "confirm_runs": confirm_runs,
                "tokens_in":  iter_delta.get("input_tokens", 0),
                "tokens_out": iter_delta.get("output_tokens", 0),
                "cache_read": iter_delta.get("cache_read_input_tokens", 0),
                "max_iters":  args.max_iterations,
            }
            with open(iter_log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(iter_row) + "\n")
            iter_summary_rows.append(iter_row)

            if verdict == "PASSED":
                final_verdict = "PASSED"
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": submit_tool_use_id,
                        "content": (
                            f"=== submit_patch attempt result: PASSED ===\n"
                            f"The test passes in the initial run and all "
                            f"{VERIFY_PASS_RUNS} confirmation runs. "
                            f"Repair confirmed successful."),
                    }],
                })
                break

            # FAILED or CONFIRM_FAILED — restore Flaky/ and feed back.
            _restore_flaky(base)
            if confirm_runs:
                # First verify passed; a confirmation run failed.
                confirm_summary = "\n".join(
                    f"  run {r['run']}: {r['verdict']}"
                    for r in confirm_runs)
                failure_report = (
                    f"=== submit_patch attempt result: CONFIRM_FAILED ===\n"
                    f"category:        confirm_failed\n"
                    f"verdict:         {verdict}\n"
                    f"The patch passed the first verification run but failed "
                    f"in a subsequent confirmation run — the fix is still "
                    f"non-deterministic.\n\n"
                    f"Confirmation runs ({VERIFY_PASS_RUNS} total):\n"
                    f"{confirm_summary}\n"
                    f"\n--- last failing verify log (tail) ---\n"
                    f"{verify_tail.rstrip()}\n"
                    f"\nFlaky/ has been restored to its pre-patch state. "
                    f"The fix does not pass consistently. Re-examine the "
                    f"root cause and submit a more robust patch.\n"
                ) + _restrategy_hint("confirm_failed")
            else:
                failure_report = _format_failure_report(
                    apply_report, verdict, verify_tail)

            print(f"[iter {attempt}] verdict={verdict} "
                  f"category={final_category} — feeding failure back to agent.")
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": submit_tool_use_id,
                    "content": failure_report,
                    "is_error": True,
                }],
            })
            break  # exit inner loop, advance to the next attempt

        if final_verdict == "PASSED":
            break

        # Persist a snapshot of the conversation after each outer-loop
        # iteration so a crash mid-loop still leaves something inspectable.
        conv_path.write_text(json.dumps({
            "model": args.model,
            "system": SYSTEM_PROMPT,
            "messages": messages,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

        if not submitted_this_iter:
            # The agent stalled without calling submit_patch — there's no
            # meaningful way to recover; break out and let the verdict
            # fall through to INCOMPLETE.
            print(f"[iter {attempt}] no submit_patch this iteration; aborting.")
            break

    # Final verdict bookkeeping. parse_run / the pass@k harness reads
    # verify_after_fix.verdict; surface our terminal state there too so
    # an early abort (no submit ever happened) isn't mis-read as PASSED
    # because of a stale verdict file from a previous run.
    if final_verdict != "PASSED" and submit_attempts == 0:
        (steps_dir / "verify_after_fix.verdict").write_text(
            "INCOMPLETE\n", encoding="utf-8")
        final_verdict = "INCOMPLETE"

    # Write a canonical final response file in the legacy shape so
    # downstream parsers (parse_run in run_pass_at_k.py) don't crash if
    # no successful submit was made (they'd otherwise miss llm_response.json
    # token counts entirely; here we still pin the cumulative usage).
    final_response_path = steps_dir / "llm_response.json"
    if final_response_path.is_file():
        try:
            existing = json.loads(
                final_response_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        existing.update({
            "model": args.model,
            "result_container": args.container,
            "elapsed_seconds": round(total_elapsed, 2),
            "turns_taken": submit_attempts,
            "usage": cumulative_usage,
            "agentic": True,
            "submit_attempts": submit_attempts,
            "final_verdict": final_verdict,
            "final_category": final_category,
        })
        final_response_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8")
    else:
        # No submit_patch ever fired — still write a minimal record.
        final_response_path.write_text(json.dumps({
            "model": args.model,
            "result_container": args.container,
            "elapsed_seconds": round(total_elapsed, 2),
            "turns_taken": 0,
            "usage": cumulative_usage,
            "agentic": True,
            "submit_attempts": 0,
            "final_verdict": final_verdict,
            "final_category": "no_submit",
            "response": {
                "output_0": {"diagnosis": None},
                "output_a": {"patch": None},
                "output_b": {"root_cause": None,
                             "fix_description": None,
                             "fixed_code": []},
            },
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    # Always flush the final conversation snapshot.
    conv_path.write_text(json.dumps({
        "model": args.model,
        "system": SYSTEM_PROMPT,
        "messages": messages,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    _write_run_summary(
        path             = steps_dir / "run_summary.csv",
        container        = args.container,
        model            = args.model,
        test_type        = test_type,
        max_iters        = args.max_iterations,
        iter_rows        = iter_summary_rows,
        final_verdict    = final_verdict,
        submit_attempts  = submit_attempts,
        total_elapsed    = total_elapsed,
        cumulative_usage = cumulative_usage,
    )

    print(f"\n[done ] verdict={final_verdict}  attempts={submit_attempts}  "
          f"elapsed={total_elapsed:.1f}s  "
          f"tokens={cumulative_usage['total_tokens']} "
          f"(cache_read={cumulative_usage['cache_read_input_tokens']})")
    sys.exit(0 if final_verdict == "PASSED" else 1)


if __name__ == "__main__":
    main()
