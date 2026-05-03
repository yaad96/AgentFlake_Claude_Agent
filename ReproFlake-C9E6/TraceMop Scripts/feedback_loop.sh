#!/usr/bin/env bash
# feedback_loop.sh — sourced by run_<type>_tracemop.sh.
#
# Provides run_apply_verify_feedback_loop() which executes step 10
# (apply_fix.py) + step 11 (verify) up to twice:
#   iter 1: original LLM response
#   iter 2: only if iter 1 FAILED with a retriable category
#           (compile_failed or test_failed) — feedback turn happens
#           between iterations
#
# The two iterations call the SAME apply_fix.py + verify_victim. Iter 2
# differs only in that:
#   - llm_response.json has been overwritten by `call_llm.py
#     --feedback-from` with the corrected patch
#   - Flaky/ has been restored from Flaky.pristine
# So apply_fix.py and verify_victim are unchanged across iterations.
#
# Expected globals (set by the orchestrator before sourcing):
#   STEPS_OUT_DIR  DATA_DIR  CONTAINER  RESULT_CONTAINER
#   LLM_BACKEND    LLM_SCRIPTS_DIR
# Expected function (defined by the orchestrator):
#   verify_victim — runs the per-test-type surefire invocation,
#                   captures output to "$STEPS_OUT_DIR/verify_after_fix.log",
#                   and sets the VERDICT global to PASSED or FAILED.
# Expected pre-condition:
#   "$DATA_DIR/Flaky.pristine" exists (the orchestrator must snapshot
#   Flaky/ before sourcing this script — see step 9.5 in each
#   per-type orchestrator).

run_apply_verify_feedback_loop() {
  local step10_ok fail_category iter

  # Clean stale feedback-snapshot artifacts from a previous orchestrator run
  # that triggered feedback. The orchestrator's step 0 only wipes mutated
  # source dirs (Fixed/, Flaky/, etc.), not Steps Output Files/. Without this
  # cleanup, run_pass_at_k.py's archive_run() would copy the orphan
  # *_pre_feedback.* files into the next run's archive, and parse_run would
  # mis-classify a clean iter-1 PASS as feedback_used=yes.
  rm -f "$STEPS_OUT_DIR/verify_after_fix_pre_feedback.verdict" \
        "$STEPS_OUT_DIR/verify_after_fix_pre_feedback.log" \
        "$STEPS_OUT_DIR/apply_report_pre_feedback.json" \
        "$STEPS_OUT_DIR/llm_response_pre_feedback.json" \
        "$STEPS_OUT_DIR/fail_category.txt" \
        "$STEPS_OUT_DIR/feedback_payload.txt"

  for iter in 1 2; do
    echo "[step 10] (iter $iter) apply_fix.py                 -> $STEPS_OUT_DIR/apply_report.json"
    step10_ok=1
    ( cd "$LLM_SCRIPTS_DIR" && python3 apply_fix.py "$RESULT_CONTAINER" \
        --docker-container "$CONTAINER" ) || step10_ok=0

    if (( ! step10_ok )); then
      echo "[step 10] (iter $iter) apply_fix.py exited non-zero — LLM patch did not land cleanly."
    fi

    echo "[step 11] (iter $iter) verifying patched Flaky/"
    VERDICT="FAILED"
    if (( step10_ok )); then
      verify_victim
    fi
    printf '%s\n' "$VERDICT" > "$STEPS_OUT_DIR/verify_after_fix.verdict"

    [[ "$VERDICT" == "PASSED" ]] && return 0
    (( iter >= 2 )) && return 0

    # FAILED on iter 1 — classify and decide whether to retry. Single source
    # of truth for the classification logic is run_pass_at_k.classify;
    # mirroring it here keeps the orchestrator's branching independent of
    # any future schema changes downstream.
    fail_category=$(python3 - "$STEPS_OUT_DIR" <<'CLASSIFY_PY'
# Defensive classifier — must always print exactly one category to stdout
# without raising. The orchestrator's `set -euo pipefail` would otherwise
# treat a Python crash as a fatal error and abort mid-feedback. Any
# unexpected shape in apply_report.json (not-a-dict JSON, missing keys,
# wrong types) collapses to "unknown_failure", which is non-retriable
# and exits the loop cleanly.
import json, re, sys
steps = sys.argv[1]

def _cat():
    try:
        apply = json.load(open(f"{steps}/apply_report.json"))
    except Exception:
        return "unknown_failure"
    if not isinstance(apply, dict):
        return "unknown_failure"

    result = apply.get("result")
    if not isinstance(result, dict):
        result = {}
    if not result.get("ok") and result.get("layer") in (None, "none"):
        return "patch_apply_failed"

    recompile = apply.get("recompile")
    if not isinstance(recompile, dict):
        recompile = {}
    if recompile.get("ok") is False and not recompile.get("skipped"):
        return "compile_failed"

    try:
        log = open(f"{steps}/verify_after_fix.log", errors="replace").read()
        f = e = 0
        for m in re.finditer(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)", log):
            f, e = int(m.group(2)), int(m.group(3))
        markers = len(re.findall(r"<<< (FAILURE|ERROR)!", log))
        if f + e > 0 or markers > 0:
            return "test_failed"
    except Exception:
        pass
    return "unknown_failure"

try:
    print(_cat())
except Exception:
    # last-ditch defense: never let an exception escape this script
    print("unknown_failure")
CLASSIFY_PY
)

    case "$fail_category" in
      compile_failed|test_failed) ;;
      *)
        echo "[feedback] category=$fail_category — not retriable, finishing."
        return 0 ;;
    esac

    echo "[feedback] iter $iter → iter $((iter+1)): category=$fail_category"

    # Snapshot the pre-feedback canonical artifacts so parse_run can
    # populate the verdict_pre_feedback / feedback_category CSV columns
    # (Phase 5) and so a human auditor can compare the two attempts.
    #
    # Each cp is guarded with `[[ -f ]]` because some artifacts are
    # conditionally produced. In particular:
    #   - verify_after_fix.log only exists if verify_victim() ran, which
    #     requires step10_ok=1. When apply_fix.py fails (compile_failed
    #     case), step10_ok=0 and verify_victim is skipped — but classify
    #     still returns compile_failed (from apply_report.recompile.ok).
    #     Without this guard the cp fails and set -e kills the orchestrator
    #     mid-feedback.
    #   - llm_response.json and apply_report.json always exist (step 9 and
    #     apply_fix.py write them unconditionally), but the guard is cheap
    #     defense in depth.
    [[ -f "$STEPS_OUT_DIR/llm_response.json" ]] && \
      cp "$STEPS_OUT_DIR/llm_response.json"        "$STEPS_OUT_DIR/llm_response_pre_feedback.json"
    [[ -f "$STEPS_OUT_DIR/apply_report.json" ]] && \
      cp "$STEPS_OUT_DIR/apply_report.json"        "$STEPS_OUT_DIR/apply_report_pre_feedback.json"
    [[ -f "$STEPS_OUT_DIR/verify_after_fix.log" ]] && \
      cp "$STEPS_OUT_DIR/verify_after_fix.log"     "$STEPS_OUT_DIR/verify_after_fix_pre_feedback.log"
    [[ -f "$STEPS_OUT_DIR/verify_after_fix.verdict" ]] && \
      cp "$STEPS_OUT_DIR/verify_after_fix.verdict" "$STEPS_OUT_DIR/verify_after_fix_pre_feedback.verdict"
    echo "$fail_category" > "$STEPS_OUT_DIR/fail_category.txt"

    echo "[feedback] build_feedback.py"
    ( cd "$LLM_SCRIPTS_DIR" && python3 build_feedback.py \
        "$RESULT_CONTAINER" "$fail_category" )

    echo "[feedback] call_llm.py --feedback-from"
    ( cd "$LLM_SCRIPTS_DIR" && python3 call_llm.py \
        "$RESULT_CONTAINER" "$LLM_BACKEND" \
        --feedback-from "$STEPS_OUT_DIR/feedback_payload.txt" )

    echo "[feedback] restoring Flaky/ from Flaky.pristine for clean re-apply"
    if [[ ! -d "$DATA_DIR/Flaky.pristine" ]]; then
      echo "ERROR: $DATA_DIR/Flaky.pristine missing — cannot restore for feedback re-apply."
      echo "       Orchestrator must snapshot Flaky/ before sourcing feedback_loop.sh."
      VERDICT="FAILED"
      return 1
    fi
    rm -rf "$DATA_DIR/Flaky"
    cp -r "$DATA_DIR/Flaky.pristine" "$DATA_DIR/Flaky"
  done
}
