#!/usr/bin/env python3
"""
build_feedback.py — produce the feedback_payload.txt user-turn body.

Called by the orchestrator after step 11 detects a retriable failure
(compile_failed or test_failed). Reads apply_report.json and (for
test_failed) verify_after_fix.log to construct a category-specific
feedback payload that's then handed to call_llm_*.py --feedback-from
as the next user turn in the conversation.

Usage:
    python3 build_feedback.py <result_container> <fail_category>

where fail_category is one of: compile_failed, test_failed.

Output:
    data/<result_container>/Steps Output Files/feedback_payload.txt
"""

import json
import os
import re
import sys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Script lives in ReproFlake-C9E6/LLM Scripts/; data is one level up.
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")

# Truncation budgets (kept tight to preserve cache-hit efficiency on the
# turn-3 replay — the cached prefix is turns 1-2, so the new feedback
# user-turn is the only un-cached input on Anthropic).
RECOMPILE_TAIL_CHARS = 3000
TEST_LOG_BLOCK_LINES = 25
TEST_LOG_MAX_BLOCKS = 2


# Three places the "previous patch failed" assertion lands so the model
# can't easily skim past it: header, lead paragraph, closing.
COMPILE_TEMPLATE = """=== ROUND 2: YOUR PATCH FAILED TO COMPILE ===

Your previous patch was applied successfully:
{applied}

But the in-container Maven recompile then failed.

Compiler output (last lines of mvn stdout/stderr):

{errs}

Note: Flaky/ has been restored to its pre-patch state. Reply with the SAME
schema (OUTPUT 0 / A / B). In OUTPUT B, emit a complete fixed_code list
that resolves the compile errors — including any field declarations or
imports your previous methods referenced.

If you need to reference an API not currently in your conversation context
(e.g., to verify a constructor signature, method name, or class hierarchy
before guessing), reply with an <ARTIFACTS_REQUESTED> block instead — the
next turn will return the requested artifacts and you can then emit the
corrected fix.
"""

TEST_TEMPLATE = """=== ROUND 2: PATCH COMPILED, BUT THE FLAKINESS WAS NOT MITIGATED ===

Your previous patch applied cleanly and compiled successfully — but the
underlying flakiness is still reproducing. Your fix did NOT eliminate
the bug; the test still fails.

Applied successfully:
{applied}

Surefire result: {summary}

Failure details (first {n_blocks} failure marker(s), ~{n_lines} lines stack each):

{fails}

[truncated — full log at verify_after_fix_pre_feedback.log]

Reconsider the diagnosis:
  - Did the patch target the wrong site?
  - Is the flakiness caused by a different mechanism than you identified?
  - Did the fix address a symptom rather than the root cause?

Note: Flaky/ has been restored to its pre-patch state. Reply with the
SAME schema (OUTPUT 0 / A / B). You may emit a fundamentally different
fix — your previous attempt did not work, so do not anchor on it.

If your reconsidered diagnosis points to an API you can't see clearly in
your current context (e.g., to check a method signature, field declaration,
or class hierarchy), reply with an <ARTIFACTS_REQUESTED> block instead —
the next turn will return the requested artifacts.
"""


def format_applied(apply_report: dict) -> str:
    """Bullet list of files/operations the previous patch landed.

    apply_fix.py uses two different application strategies:
      - "git apply" (unified diff from output_a) — succeeds without
        recording per-method operations. result.applied[] is empty/null.
      - "splice output_b" (structured fixed_code entries) — records each
        operation in result.applied[].

    For git-apply successes we fall back to compile.results[].file, which
    lists every .java file the host-side javac smoke test inspected (i.e.
    every file the patch touched). compile.results entries with ok=False
    are kept — for the compile_failed payload we want the LLM to see all
    targeted files, not just the ones that built cleanly.
    """
    result = apply_report.get("result") or {}
    applied = result.get("applied") or []
    layer = result.get("layer") or "?"

    if applied:
        lines = []
        for a in applied:
            f = a.get("file", "?")
            op = a.get("operation", "?")
            method = a.get("method", "")
            suffix = f" {method}" if method else ""
            lines.append(f"  - {f} :: {op}{suffix}")
        return "\n".join(lines)

    compile_results = (apply_report.get("compile") or {}).get("results") or []
    if compile_results:
        lines = [f"  - {r.get('file', '?')}  (applied via {layer})"
                 for r in compile_results]
        return "\n".join(lines)

    return (f"  (apply_fix.py reported success via layer={layer!r} but no "
            f"per-file data is available; check apply_report.json directly)")


def extract_surefire_summary(log: str) -> str:
    """Last `Tests run: X, Failures: Y, Errors: Z[, Skipped: S]` line in
    the verify log. Surefire prints this after each test class and at the
    end of the run — the *last* one is the run-wide totals."""
    matches = re.findall(
        r"Tests run:\s*\d+,\s*Failures:\s*\d+,\s*Errors:\s*\d+(?:,\s*Skipped:\s*\d+)?",
        log,
    )
    return matches[-1] if matches else "(no Surefire summary line found in verify log)"


def extract_failure_blocks(log: str,
                           max_blocks: int = TEST_LOG_MAX_BLOCKS,
                           lines_each: int = TEST_LOG_BLOCK_LINES) -> list:
    """Return up to `max_blocks` excerpts of `<<< FAILURE!`/`<<< ERROR!`
    markers and the following `lines_each` lines (the stack trace).

    Skips ahead by lines_each+1 after each match so adjacent markers
    don't produce overlapping snippets. Bounded by file length, so a
    short log returns smaller blocks gracefully."""
    log_lines = log.splitlines()
    blocks = []
    i = 0
    while i < len(log_lines) and len(blocks) < max_blocks:
        line = log_lines[i]
        if "<<< FAILURE!" in line or "<<< ERROR!" in line:
            block_lines = log_lines[i:i + lines_each + 1]
            blocks.append("\n".join(block_lines))
            i += lines_each + 1
        else:
            i += 1
    return blocks


def build_compile_payload(apply_report: dict) -> str:
    applied = format_applied(apply_report)
    recompile = apply_report.get("recompile") or {}
    errs = recompile.get("stdout_tail") or "(empty stdout_tail in apply_report.recompile — check apply_report.json directly)"
    errs_tail = errs[-RECOMPILE_TAIL_CHARS:]
    return COMPILE_TEMPLATE.format(applied=applied, errs=errs_tail)


def build_test_payload(apply_report: dict, log: str) -> str:
    applied = format_applied(apply_report)
    summary = extract_surefire_summary(log)
    blocks = extract_failure_blocks(log)
    if not blocks:
        fails = "(no <<< FAILURE!/<<< ERROR! markers found in verify log — inspect the log directly)"
        n_blocks = 0
    else:
        fails = "\n  ---\n".join(blocks)
        n_blocks = len(blocks)
    return TEST_TEMPLATE.format(
        applied=applied,
        summary=summary,
        fails=fails,
        n_blocks=n_blocks,
        n_lines=TEST_LOG_BLOCK_LINES,
    )


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <result_container> <fail_category>",
              file=sys.stderr)
        print(f"  fail_category: compile_failed | test_failed",
              file=sys.stderr)
        sys.exit(1)

    result_container = sys.argv[1]
    fail_category = sys.argv[2]

    if fail_category not in ("compile_failed", "test_failed"):
        print(f"ERROR: fail_category {fail_category!r} is not retriable. "
              f"Only compile_failed and test_failed are supported.",
              file=sys.stderr)
        sys.exit(1)

    base = os.path.join(DATA_DIR, result_container)
    steps = os.path.join(base, "Steps Output Files")
    apply_path = os.path.join(steps, "apply_report.json")
    verify_log_path = os.path.join(steps, "verify_after_fix.log")
    out_path = os.path.join(steps, "feedback_payload.txt")

    if not os.path.isfile(apply_path):
        print(f"ERROR: required file not found: {apply_path}", file=sys.stderr)
        sys.exit(1)

    with open(apply_path, encoding="utf-8") as f:
        apply_report = json.load(f)

    if fail_category == "compile_failed":
        payload = build_compile_payload(apply_report)
    else:  # test_failed
        if not os.path.isfile(verify_log_path):
            print(f"ERROR: required file not found: {verify_log_path}",
                  file=sys.stderr)
            sys.exit(1)
        with open(verify_log_path, encoding="utf-8", errors="replace") as f:
            log = f.read()
        payload = build_test_payload(apply_report, log)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(payload)

    print(f"[build_feedback] {fail_category}: wrote {len(payload)} chars → {out_path}")


if __name__ == "__main__":
    main()
