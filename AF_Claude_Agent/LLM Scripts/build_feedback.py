#!/usr/bin/env python3
"""
build_feedback.py — produce the feedback_payload.txt user-turn body.

Called by the orchestrator after step 11 detects a retriable failure
(compile_failed, test_failed, or patch_apply_failed). Reads
apply_report.json and (for test_failed only) verify_after_fix.log to
construct a category-specific feedback payload that's then handed to
call_llm_*.py --feedback-from as the next user turn in the conversation.

Usage:
    python3 build_feedback.py <result_container> <fail_category>

where fail_category is one of:
  - compile_failed      (patch landed, mvn test-compile failed)
  - test_failed         (patch landed and compiled, surefire still failed)
  - patch_apply_failed  (no layer of the applier could land the patch)

Output:
    data/<result_container>/Steps Output Files/feedback_payload.txt
"""

import json
import os
import re
import sys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Script lives in AF_Claude_Agent/LLM Scripts/; data is one level up.
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")

# Truncation budgets (kept tight to preserve cache-hit efficiency on the
# turn-3 replay — the cached prefix is turns 1-2, so the new feedback
# user-turn is the only un-cached input on Anthropic).
RECOMPILE_TAIL_CHARS = 3000
TEST_LOG_BLOCK_LINES = 25
TEST_LOG_MAX_BLOCKS = 2
# Per-layer cap for patch_apply_failed payload. `git apply --check` stderr
# can include hunk-context excerpts that balloon to several KB on a busy
# diff; capping each layer's reason keeps the whole payload comfortably
# under 3 kB even with all 3 layers reporting verbose rejections.
LAYERS_REASON_MAX_CHARS = 600


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

# Three places the "no layer landed" assertion repeats so the model can't
# skim past it: header, lead paragraph, closing. Mirrors the structure of
# COMPILE_TEMPLATE and TEST_TEMPLATE. The hint taxonomy intentionally maps
# 1:1 to the rejection paths in apply_fix.py — wrong path, wrong method,
# wrong anchor, context-line drift, malformed file-create. It does NOT
# mention generic Java syntax errors (variables outside methods/classes,
# unbalanced braces, etc.) because those land in compile_failed, never
# patch_apply_failed — the applier doesn't validate Java structure beyond
# "newly-created .java file has package + top-level type declaration."
PATCH_APPLY_TEMPLATE = """=== ROUND 2: YOUR PATCH FAILED TO APPLY ===

Your previous response was processed but NO layer of the applier could
land the patch on disk. The Flaky/ source tree is unchanged from its
pre-attempt state.

Per-layer rejection (in attempt order):

{layers}

{path_rewrites}Final result: {final_reason}

What this usually means (in order of frequency seen in practice):
  1. The `file` path references a file that does NOT exist in the
     source tree — often a missing Maven module prefix, or the path
     was hallucinated. The applier tries a unique-suffix match against
     the real tree before giving up; if that also fails, the file
     genuinely doesn't exist where you claimed.
  2. (replace_method) The named method does not exist in the target
     file — typo, wrong file, or the method is on an inner class
     that a different `file` field would target.
  3. (insert_method) Either the named method already exists in the
     target file (operation contradicts state — should be replace_method
     instead), or the `anchor` target method does not exist.
  4. (unified diff) The @@ context lines do not match the real file
     content — line numbers or surrounding code were guessed, and
     git apply rejects on context mismatch (or applies silently to
     no effect, which the applier detects via hash and rolls back).
  5. (rare) The patch CREATES a new .java file whose contents are
     just a method/statement fragment with no `package X.Y.Z;`
     declaration and no top-level class declaration. The applier
     rolls these back as malformed Java.

Note: Flaky/ has been restored to its pre-attempt state — no partial
edits remain. Reply with the SAME schema (OUTPUT 0 / A / B). Make
sure every `file` path references an existing file in the tree,
every `replace_method` names a method that actually exists in that
file, and every unified-diff @@ context line matches the real file
content verbatim.

If you are uncertain whether a file path, class, or method exists in
the tree, reply with an <ARTIFACTS_REQUESTED> block instead — the next
turn will return the requested artifacts and you can then emit a
corrected fix using the verified information.
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


def format_layers_attempted(apply_report: dict) -> tuple:
    """Render apply_report['layers_attempted'] into two payload chunks:
    (layers_block, path_rewrites_block).

    layers_block: one bullet per attempted layer, with the layer's
    rejection reason indented underneath. The splicer's structured
    `failed[]` list (per-entry breakdown) is rendered as nested bullets
    when present — that's where the most actionable diagnostic lives
    (which exact entry's file/method/anchor was unfindable).

    path_rewrites_block: a labelled section listing every original→resolved
    path mapping the applier performed. Two sources are merged:
      - `layer.path_rewritten` (apply_patch's unified-diff header rewrites)
      - `layer.applied[i].path_resolved` (splicer per-entry suffix-match
        rewrites, only present on entries that landed — apply_fix.py
        doesn't preserve the original path on failed-after-resolve entries)
    Empty string when neither source contributed — keeps the template's
    slot vacant so the rendered payload doesn't emit a stray blank section.

    Reasons are truncated to LAYERS_REASON_MAX_CHARS so a verbose
    `git apply --check` stderr (with embedded hunk-context excerpts)
    can't blow up the user-turn length. The same cap applies to
    per-entry splicer failures, which are typically short (<150 chars)
    so the cap rarely fires for them in practice.

    Defensive about field shapes: layers_attempted may be missing or
    wrong-typed if apply_report.json was hand-edited or written by a
    crashing apply_fix.py — a dict whose `layers_attempted` is None,
    a string, or a non-list iterable just produces an empty layers
    block rather than raising.
    """
    layers = apply_report.get("layers_attempted")
    if not isinstance(layers, list):
        layers = []
    blocks = []
    rewrites_seen = {}

    for layer in layers:
        if not isinstance(layer, dict):
            continue

        # apply_patch's aggregate failure dict and apply_fixed_code's empty-
        # entries early return both emit layer=null. Distinguish them by
        # structural fields so the LLM gets a meaningful label rather than
        # "(unknown layer)" — the unified-diff failure is the dominant
        # patch_apply_failed contributor and losing its identity hides the
        # most actionable signal (was it the diff or the splicer that
        # failed?).
        name = layer.get("layer")
        if not name:
            if "applied" in layer or "failed" in layer:
                name = "splice output_b"
            else:
                name = "output_a (git apply / unified diff)"
        bullet = [f"  [FAIL] {name}"]

        # Splicer with per-entry failures: render entry-level breakdown.
        # The splicer's top-level dict has no `reason` field when failed[]
        # is populated — the per-entry reasons ARE the diagnostic.
        failed = layer.get("failed") or []
        applied = layer.get("applied") or []
        if failed:
            n_total = len(applied) + len(failed)
            noun = "entry" if n_total == 1 else "entries"
            bullet.append(
                f"         {len(failed)} of {n_total} {noun} failed:"
            )
            for f in failed:
                if not isinstance(f, dict):
                    continue
                entry = f.get("entry") if isinstance(f.get("entry"), dict) else {}
                fl = entry.get("file") or "?"
                op = entry.get("operation") or "?"
                method = entry.get("method") or ""
                head = f"{fl} :: {op}" + (f" {method}" if method else "")
                bullet.append(f"           - {head}")
                reason = (f.get("reason") or "")[:LAYERS_REASON_MAX_CHARS]
                reason_lines = reason.splitlines() or [""]
                bullet.append(f"             reason: {reason_lines[0]}")
                for ln in reason_lines[1:]:
                    bullet.append(f"                     {ln}")
        else:
            # Plain single-reason layer — output_a missing, unified-diff
            # aggregate failure, or splicer's "no fixed_code entries".
            reason = (layer.get("reason") or "(no reason recorded)")
            reason = reason[:LAYERS_REASON_MAX_CHARS]
            for ln in reason.splitlines():
                bullet.append(f"         {ln}")

        blocks.append("\n".join(bullet))

        # Collect path-rewrite mappings across all layers for the
        # consolidated section below. Two sources contribute:
        #   1. Layer-level `path_rewritten` from apply_patch (unified-diff
        #      header rewrites — applies to all targets in the patch).
        #   2. Per-entry `path_resolved` on splicer `applied[]` items
        #      (one rewrite per entry whose `file` field needed suffix-match
        #      resolution and then succeeded). apply_fix.py only attaches
        #      `path_resolved` to applied entries — for failed-after-resolve
        #      entries, the resolved path is silently substituted into
        #      `entry.file` and the original path is not preserved.
        # Surfacing #2 matters when the splicer landed some entries (so
        # path_resolved was recorded) before another entry's failure tipped
        # the layer overall to ok=false; without it the LLM would think its
        # paths were verbatim correct when actually the applier rerouted them.
        pr = layer.get("path_rewritten")
        if isinstance(pr, dict):
            rewrites_seen.update(pr)
        for item in (layer.get("applied") or []):
            if not isinstance(item, dict):
                continue
            pr_entry = item.get("path_resolved")
            if isinstance(pr_entry, dict):
                orig = pr_entry.get("original")
                new = pr_entry.get("resolved")
                if orig and new and orig != new:
                    rewrites_seen[orig] = new

    layers_block = "\n\n".join(blocks) if blocks else "  (no layers attempted)"

    if rewrites_seen:
        rew_lines = ["Path-fuzzy-matcher rewrote these paths before applying:"]
        for orig, new in rewrites_seen.items():
            rew_lines.append(f"  - {orig}")
            rew_lines.append(f"    -> {new}")
        # Trailing blank line so the next template section ("Final result:")
        # is visually separated from the rewrites block.
        path_rewrites_block = "\n".join(rew_lines) + "\n\n"
    else:
        path_rewrites_block = ""

    return layers_block, path_rewrites_block


def build_patch_apply_payload(apply_report: dict) -> str:
    layers_block, path_rewrites_block = format_layers_attempted(apply_report)
    result = apply_report.get("result")
    if not isinstance(result, dict):
        result = {}
    final_reason = (result.get("reason") or "no layer landed the fix")[:200]
    return PATCH_APPLY_TEMPLATE.format(
        layers=layers_block,
        path_rewrites=path_rewrites_block,
        final_reason=final_reason,
    )


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <result_container> <fail_category>",
              file=sys.stderr)
        print(f"  fail_category: compile_failed | test_failed | patch_apply_failed",
              file=sys.stderr)
        sys.exit(1)

    result_container = sys.argv[1]
    fail_category = sys.argv[2]

    RETRIABLE = ("compile_failed", "test_failed", "patch_apply_failed")
    if fail_category not in RETRIABLE:
        print(f"ERROR: fail_category {fail_category!r} is not retriable. "
              f"Only {', '.join(RETRIABLE)} are supported.",
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
    elif fail_category == "patch_apply_failed":
        # No verify_after_fix.log read here — when no layer landed, step10_ok=0
        # in feedback_loop.sh and verify_victim is skipped, so the log doesn't
        # exist. apply_report.json carries every diagnostic this branch needs.
        payload = build_patch_apply_payload(apply_report)
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
