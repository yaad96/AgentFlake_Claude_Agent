#!/usr/bin/env python3
"""
orchestrator_common.py — provider-neutral core of the agentic repair loop.

Everything here is independent of which LLM provider is used: prompt
construction, the tool *implementations* (via agent_tools), the submit_patch
schema, patch apply/verify, failure classification and formatting, the
run-summary writer, and the shared per-run setup/teardown.

The two provider backends — agentic_orchestrator_anthropic.py and
agentic_orchestrator_openai.py — import this module and add only the
provider-specific bits (client construction, the API call, message/tool
plumbing, and usage accounting). agentic_orchestrator.py is the thin parent
dispatcher that picks a backend based on the model id.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
LLM_SCRIPTS_DIR = REPROFLAKE_DIR / "LLM Scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(LLM_SCRIPTS_DIR))

import agent_tools        # type: ignore  # noqa: E402
import agentic_config     # type: ignore  # noqa: E402
import prompts            # type: ignore  # noqa: E402
from assemble_llm_context import (  # type: ignore  # noqa: E402
    DATA_DIR,
    load_csv_row,
    extract_failure_from_log,
)

# Tuneable constants (shared by both backends).
MAX_TOKENS                   = agentic_config.MAX_TOKENS
TEMPERATURE                  = agentic_config.TEMPERATURE
MAX_TOOL_TURNS_PER_ITERATION = agentic_config.MAX_TOOL_TURNS_PER_ITERATION
DEFAULT_MAX_ITERATIONS       = agentic_config.MAX_ITERATIONS
TOOL_OUTPUT_MAX_CHARS        = agentic_config.TOOL_OUTPUT_MAX_CHARS
VERIFY_PASS_RUNS             = agentic_config.VERIFY_PASS_RUNS

SUPPORTED_TEST_TYPES = {"od", "td", "id", "nio", "unclassified", "brittle"}

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


# ---------------------------------------------------------------------------
# Prompt construction — templates live in prompts.py (edit that file)
# ---------------------------------------------------------------------------

def build_initial_user_prompt(container: str, row: dict,
                              failure_text: str) -> str:
    """Render prompts.INITIAL_USER_TEMPLATE with the run-specific values."""
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


def all_tool_schemas() -> list[dict]:
    """Anthropic-format tool schemas (context tools + submit_patch). The
    OpenAI backend translates these into function-calling format."""
    return list(agent_tools.TOOL_SCHEMAS) + [SUBMIT_PATCH_SCHEMA]


# ---------------------------------------------------------------------------
# Patch application + verification
# ---------------------------------------------------------------------------

def write_llm_response_json(steps_dir: Path, container: str,
                            args_dict: dict, iteration: int,
                            model: str = "") -> Path:
    """Write the submit_patch payload in the legacy `llm_response.json`
    shape so apply_fix.py consumes it unchanged."""
    response_path = steps_dir / "llm_response.json"
    payload = {
        "model": model,
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
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return response_path


def restore_flaky(base: Path) -> None:
    """Restore Flaky/ from the snapshot the per-type orchestrator made at
    step 9.5, so each submit_patch starts against a clean tree."""
    pristine = base / "Flaky.pristine"
    flaky = base / "Flaky"
    if not pristine.is_dir():
        print(f"[restore] WARNING: {pristine} missing — cannot restore Flaky/.")
        return
    if flaky.is_dir():
        shutil.rmtree(flaky)
    shutil.copytree(pristine, flaky, symlinks=True)


def run_apply_fix(container: str, docker_container: str) -> dict:
    """Invoke apply_fix.py and return the parsed apply_report.json."""
    cmd = [
        sys.executable, str(LLM_SCRIPTS_DIR / "apply_fix.py"),
        container, "--docker-container", docker_container,
    ]
    print(f"[apply ] {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
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


def run_verify(container: str, docker_container: str) -> tuple[str, str]:
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


def classify_failure(apply_report: dict, verdict: str) -> str:
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


def restrategy_hint(category: str) -> str:
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


def format_failure_report(apply_report: dict, verdict: str,
                          verify_tail: str) -> str:
    """Build the tool_result body for a failed submit_patch attempt."""
    category = classify_failure(apply_report, verdict)
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
        + restrategy_hint(category)
    )


# ---------------------------------------------------------------------------
# Usage helpers
# ---------------------------------------------------------------------------

_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens",
               "cache_read_input_tokens", "cache_creation_input_tokens")


def zero_usage() -> dict:
    return {k: 0 for k in _USAGE_KEYS}


def sum_usage(*usages) -> dict:
    return {k: sum(u.get(k, 0) for u in usages) for k in _USAGE_KEYS}


# ---------------------------------------------------------------------------
# Per-run summary
# ---------------------------------------------------------------------------

def _tool_sequence_str(tools: list[str]) -> str:
    return " → ".join(tools) if tools else "(none)"


def _tool_counts_str(tools: list[str]) -> str:
    seen: dict[str, int] = {}
    for t in tools:
        seen[t] = seen.get(t, 0) + 1
    return ", ".join(f"{t}×{n}" for t, n in seen.items()) if seen else "(none)"


_RUN_SUMMARY_COLS = [
    "iteration", "verdict", "category", "applied_ok",
    "tools_sequence", "tool_counts", "confirm_runs",
    "elapsed_seconds", "tokens_in", "tokens_out", "cache_read",
    "test_integrity",
]


def write_run_summary(path: Path, container: str, model: str,
                      test_type: str, max_iters: int,
                      iter_rows: list[dict],
                      final_verdict: str, submit_attempts: int,
                      total_elapsed: float,
                      cumulative_usage: dict,
                      test_integrity: str = "") -> None:
    """Write run_summary.csv — one row per submit_patch attempt plus a
    SUMMARY row with aggregated totals."""
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
            "test_integrity": test_integrity,
        })


# ---------------------------------------------------------------------------
# Shared per-run setup / teardown (used by both backends)
# ---------------------------------------------------------------------------

class RunContext:
    """Bundle of resolved per-run values shared across backends."""
    __slots__ = ("row", "test_type", "docker_container", "base",
                 "steps_dir", "initial_user", "iter_log_path", "conv_path")

    def __init__(self, *, row, test_type, docker_container, base, steps_dir,
                 initial_user, iter_log_path, conv_path):
        self.row = row
        self.test_type = test_type
        self.docker_container = docker_container
        self.base = base
        self.steps_dir = steps_dir
        self.initial_user = initial_user
        self.iter_log_path = iter_log_path
        self.conv_path = conv_path


def prepare_run(args) -> RunContext:
    """Provider-neutral pre-loop work: validate the container/test_type,
    resolve paths, extract the initial failure block, build the initial user
    prompt, persist llm_context.txt, and clear stale per-run logs.

    Exits (sys.exit) on an unknown container or unsupported test_type — the
    same contract both backends had inline.
    """
    import re

    row = load_csv_row(args.container)
    if not row:
        sys.exit(f"ERROR: container '{args.container}' not in test_config.csv")
    test_type = (row.get("test_type") or "").strip().lower()
    if test_type == "britle":            # normalise the CSV typo
        test_type = "brittle"
    if test_type not in SUPPORTED_TEST_TYPES:
        sys.exit(f"ERROR: unsupported test_type '{test_type}'")

    docker_container = args.docker_container or (
        "tm_" + re.sub(r"[^a-zA-Z0-9]", "_", args.container))

    base = Path(DATA_DIR) / args.container
    steps_dir = base / "Steps_Output_Files"
    steps_dir.mkdir(parents=True, exist_ok=True)

    # Same failure-block probe order the per-type assemblers use, so the agent
    # sees an identical initial prompt regardless of provider.
    source_base = base
    zip_name = (row.get("zip") or "").strip()
    if zip_name and zip_name != args.container and \
       (Path(DATA_DIR) / zip_name / "Flaky" / "src").is_dir():
        source_base = Path(DATA_DIR) / zip_name
    failure_text = ""
    for cand in ("traces-flakycc", "traces-flaky", "traces-fail", "traces-fixed"):
        text = extract_failure_from_log(str(source_base / cand / "mvn.log"))
        if not text.startswith("("):
            failure_text = text
            break
    if not failure_text:
        print("[init ] WARNING: no failure block found in any traces-*/mvn.log; "
              "agent will see an empty failure log section.")

    initial_user = build_initial_user_prompt(args.container, row, failure_text)
    (steps_dir / "llm_context.txt").write_text(initial_user, encoding="utf-8")

    iter_log_path = steps_dir / "agentic_iterations.jsonl"
    conv_path = steps_dir / "agentic_conversation.json"
    iter_log_path.unlink(missing_ok=True)
    conv_path.unlink(missing_ok=True)

    return RunContext(
        row=row, test_type=test_type, docker_container=docker_container,
        base=base, steps_dir=steps_dir, initial_user=initial_user,
        iter_log_path=iter_log_path, conv_path=conv_path)


def save_conversation(conv_path: Path, model: str, messages: list,
                      *, provider: str, system: str | None = None) -> None:
    """Persist the running transcript for audit. `system` is included for
    Anthropic (where the system prompt is a separate API param); for OpenAI
    it lives inside `messages` so `system` is omitted."""
    snapshot: dict = {"model": model, "provider": provider}
    if system is not None:
        snapshot["system"] = system
    snapshot["messages"] = messages
    conv_path.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")


def finalize_run(*, ctx: RunContext, container: str, model: str, provider: str,
                 messages: list, system: str | None,
                 final_verdict: str, final_category: str,
                 submit_attempts: int, total_elapsed: float,
                 cumulative_usage: dict, iter_summary_rows: list[dict],
                 max_iters: int) -> int:
    """Provider-neutral post-loop bookkeeping. Writes the verdict fallback,
    finalises llm_response.json, snapshots the conversation, writes the run
    summary, prints the done line, and returns the process exit code."""
    steps_dir = ctx.steps_dir

    # Canonical run-level verdict — unambiguous, exactly one of:
    #   PASSED      a submitted patch verified
    #   FAILED      >=1 patch was submitted, none verified
    #   INCOMPLETE  the agent never submitted a patch (stalled / no attempt)
    if final_verdict == "PASSED":
        run_verdict = "PASSED"
    elif submit_attempts > 0:
        run_verdict = "FAILED"
    else:
        run_verdict = "INCOMPLETE"
    final_verdict = run_verdict

    # Two-file separation (kept deliberately distinct so neither is ambiguous):
    #   verify_after_fix.verdict  — strictly binary VERIFICATION result:
    #                               PASSED only if a patch verified, else FAILED.
    #   run_verdict.txt           — authoritative three-state RUN outcome, shown
    #                               in [done], llm_response.json, the per-type
    #                               script, and the dispatcher.
    (steps_dir / "verify_after_fix.verdict").write_text(
        ("PASSED" if run_verdict == "PASSED" else "FAILED") + "\n",
        encoding="utf-8")
    (steps_dir / "run_verdict.txt").write_text(
        run_verdict + "\n", encoding="utf-8")

    # Test-integrity guard — only meaningful for a PASSED run (where a fake
    # green could hide). Emits flags only; never changes the verdict.
    if run_verdict == "PASSED":
        try:
            import test_integrity  # type: ignore
            integrity = test_integrity.evaluate(
                container=container, row=ctx.row, test_type=ctx.test_type,
                base=ctx.base, steps_dir=steps_dir)
        except Exception as exc:  # noqa: BLE001 — never let the guard break a run
            integrity = {"checked": False, "severity": "unknown",
                         "flags": [], "review": [], "note": f"guard error: {exc}"}
    else:
        integrity = {"checked": False, "severity": "n/a", "flags": [],
                     "review": [], "reason": "run did not pass"}
    # One-line summary for run_summary.csv, derived from the dict (no import dep).
    _sig = (integrity.get("flags") or []) + (integrity.get("review") or [])
    integrity_str = (integrity.get("severity", "unknown") if not _sig
                     else f"{integrity.get('severity')}: {','.join(_sig)}")
    if not integrity.get("checked") and integrity.get("severity") != "n/a":
        integrity_str = f"not_checked ({integrity.get('note', '')})".strip()
    (steps_dir / "test_integrity.json").write_text(
        json.dumps(integrity, indent=2, ensure_ascii=False), encoding="utf-8")
    if integrity.get("severity") == "suspect":
        print(f"[integrity] ⚠ SUSPECT — patch may have weakened the test: "
              f"{','.join(integrity.get('flags', []))}. Review llm_response.json.")
    elif integrity.get("severity") == "review":
        print(f"[integrity] review — {','.join(integrity.get('review', []))}")
    elif integrity.get("checked"):
        print("[integrity] clean — victim test/assertions not weakened.")

    final_response_path = steps_dir / "llm_response.json"
    if final_response_path.is_file():
        try:
            existing = json.loads(final_response_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        existing.update({
            "model": model,
            "result_container": container,
            "elapsed_seconds": round(total_elapsed, 2),
            "turns_taken": submit_attempts,
            "usage": cumulative_usage,
            "agentic": True,
            "provider": provider,
            "submit_attempts": submit_attempts,
            "final_verdict": final_verdict,
            "final_category": final_category,
            "test_integrity": integrity,
        })
        final_response_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        final_response_path.write_text(json.dumps({
            "model": model,
            "result_container": container,
            "elapsed_seconds": round(total_elapsed, 2),
            "turns_taken": 0,
            "usage": cumulative_usage,
            "agentic": True,
            "provider": provider,
            "submit_attempts": 0,
            "final_verdict": final_verdict,
            "final_category": "no_submit",
            "response": {
                "output_0": {"diagnosis": None},
                "output_a": {"patch": None},
                "output_b": {"root_cause": None, "fix_description": None,
                             "fixed_code": []},
            },
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    save_conversation(ctx.conv_path, model, messages,
                      provider=provider, system=system)

    write_run_summary(
        path             = steps_dir / "run_summary.csv",
        container        = container,
        model            = model,
        test_type        = ctx.test_type,
        max_iters        = max_iters,
        iter_rows        = iter_summary_rows,
        final_verdict    = final_verdict,
        submit_attempts  = submit_attempts,
        total_elapsed    = total_elapsed,
        cumulative_usage = cumulative_usage,
        test_integrity   = integrity_str,
    )

    print(f"\n[done ] verdict={final_verdict}  attempts={submit_attempts}  "
          f"elapsed={total_elapsed:.1f}s  "
          f"tokens={cumulative_usage['total_tokens']} "
          f"(cache_read={cumulative_usage['cache_read_input_tokens']})")
    return 0 if final_verdict == "PASSED" else 1
