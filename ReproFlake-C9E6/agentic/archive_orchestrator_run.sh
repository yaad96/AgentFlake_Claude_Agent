#!/usr/bin/env bash
# archive_orchestrator_run.sh — copy a finished ReproFlake orchestrator run from
# its in-place Steps_Output_Files into the durable AGENTIC_FULL_RUNS layout:
#
#     data/AGENTIC_FULL_RUNS/<container>_runs/<model>/run_<N>/
#
# Auto-increments run_<N> (never overwrites a previous run). Copies the
# orchestrator outputs only, dropping any stale Claude-CLI driver artifacts that
# happen to share the Steps_Output_Files directory (trial.ndjson, thinking.txt,
# tool_calls.jsonl, prompt_*.txt, claude.stderr, usage.json).
#
# The claude_cli driver already persists its own runs to
# data/claude_agent/<container>/run_<NN>/, so this archiver is orchestrator-only.
#
# Usage: archive_orchestrator_run.sh <container> <steps_out_dir> <model> <reproflake_dir>
set -euo pipefail

CONTAINER="${1:?container}"
STEPS_OUT_DIR="${2:?steps_out_dir}"
MODEL="${3:-claude-sonnet-4-6}"
REPROFLAKE_DIR="${4:?reproflake_dir}"

if [[ ! -d "$STEPS_OUT_DIR" ]]; then
  echo "[archive] no Steps_Output_Files at $STEPS_OUT_DIR — nothing to archive" >&2
  exit 0
fi

base="$REPROFLAKE_DIR/data/AGENTIC_FULL_RUNS/${CONTAINER}_runs/$MODEL"
mkdir -p "$base"

# next free run_<N> (existing AGENTIC_FULL_RUNS uses run_1, run_2, ... — no padding)
n=1
while [[ -e "$base/run_$n" ]]; do n=$((n + 1)); done
run_dir="$base/run_$n"
mkdir -p "$run_dir/Steps_Output_Files"

# Copy orchestrator outputs only; skip stale claude_cli driver leftovers.
for f in "$STEPS_OUT_DIR"/*; do
  [[ -e "$f" ]] || continue
  case "$(basename "$f")" in
    trial.ndjson|thinking.txt|tool_calls.jsonl|prompt_system.txt|prompt_user.txt|claude.stderr|usage.json)
      continue ;;
  esac
  cp -pR "$f" "$run_dir/Steps_Output_Files/"
done

# Mirror run_1's top-level shape with the key artifacts.
if [[ -f "$STEPS_OUT_DIR/patch.diff"      ]]; then cp -p "$STEPS_OUT_DIR/patch.diff"      "$run_dir/Fixed.patch"; fi
if [[ -f "$STEPS_OUT_DIR/run_verdict.txt" ]]; then cp -p "$STEPS_OUT_DIR/run_verdict.txt" "$run_dir/"; fi
if [[ -f "$STEPS_OUT_DIR/run_summary.csv" ]]; then cp -p "$STEPS_OUT_DIR/run_summary.csv" "$run_dir/"; fi

verdict="$(cat "$STEPS_OUT_DIR/run_verdict.txt" 2>/dev/null \
          || cat "$STEPS_OUT_DIR/verify_after_fix.verdict" 2>/dev/null \
          || echo UNKNOWN)"
printf '%s\n' "$verdict" > "$run_dir/.run_complete"

echo "[archive] orchestrator run -> ${run_dir#"$REPROFLAKE_DIR"/}  (verdict=$verdict)"
