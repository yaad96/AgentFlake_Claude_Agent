#!/usr/bin/env bash
# Run ID containers through the Claude Agent sequentially, one after another.
#
# Usage (from anywhere):
#   AF_Claude_Agent/agentic/run_id_batch.sh              # the 10 listed below
#   AF_Claude_Agent/agentic/run_id_batch.sh 3 5          # entries 3..5 (1-indexed, inclusive)
#   AF_Claude_Agent/agentic/run_id_batch.sh <name> ...   # named containers
#
# Environment:
#   TIMEOUT_SECS=<n>       per-container wall-clock cap (default 4200)
#   MIN_FREE_GB=<n>        abort if free disk drops below this (default 15)
#   RUNS=<n>               --runs passed to run_agentic.py (default 1)
#   MODELS=<list>          --models passed to run_agentic.py (default claude)
#   MAX_TURNS=<n>          --max-iterations (Claude Code turns); unset = tool default
#   VERIFY_PASS_RUNS=<n>   --verify-pass-runs; unset = tool default
#   SKIP_DONE=1            skip containers that already have a finished run
#   KEEP_ZIPS=1            keep every dataset zip (default: delete a zip once no
#                          remaining container in this batch needs it)
#   BATCH_LOG_DIR          where per-container logs go
#
# The crane4j JDK override lives in run_agentic_id.sh, so it applies here too.
# Deliberately does NOT use `set -e`: one container failing must not stop the
# batch. Each container's output goes to its own log.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"      # AF_Claude_Agent
REPO_ROOT="$(cd "$PROJECT_DIR/.." && pwd)"       # AgentFlake_Claude_Agent
CSV_FILE="$PROJECT_DIR/test_config.csv"
PY="$REPO_ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

export PYTHONUNBUFFERED=1

TIMEOUT_SECS="${TIMEOUT_SECS:-4200}"
MIN_FREE_GB="${MIN_FREE_GB:-15}"
RUNS="${RUNS:-1}"
MODELS="${MODELS:-claude}"

ALL=(
  crane4jcrane4jcoreb73311aget
  crane4jcrane4jcore679c3f8process
  idflink1c06b74btable
  idhbaseb162d1aserver
  avrolangjavaavro7fd098atestRecord
  karatekaratecore935f0a8testBeanConversion
  graylog2servergraylog2server036bdb5serializePrefixOnly
  graylog2servergraylog2serverf169d54serializeInteger
  dubbodubbocommon83c466etestGetMetaAnnotations
  cloudstackpluginsnetworkelementstungstendf4cd2alistTungstenNetworkTest
)

CONTAINERS=()
if (( $# == 0 )); then
  CONTAINERS=("${ALL[@]}")
elif (( $# == 2 )) && [[ "$1" =~ ^[0-9]+$ && "$2" =~ ^[0-9]+$ ]]; then
  from=$1; to=$2
  if (( from < 1 || to > ${#ALL[@]} || from > to )); then
    echo "ERROR: range $from..$to is outside 1..${#ALL[@]}"; exit 1
  fi
  for (( i = from - 1; i <= to - 1; i++ )); do CONTAINERS+=("${ALL[$i]}"); done
else
  CONTAINERS=("$@")
fi
TOTAL=${#CONTAINERS[@]}

zip_of()  { awk -F, -v c="$1" '$2==c {print $3; exit}' "$CSV_FILE"; }
free_gb() { df -BG --output=avail "$REPO_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9'; }

for c in "${CONTAINERS[@]}"; do
  if [[ "$(awk -F, -v c="$c" '$2==c {print $1; exit}' "$CSV_FILE")" != "id" ]]; then
    echo "ERROR: '$c' is not an id row in $CSV_FILE"; exit 1
  fi
done

if [[ -z "${ANTHROPIC_API_KEY:-}" && ! -s "$PROJECT_DIR/.anthropic_api_key" ]]; then
  echo "ERROR: no Anthropic key. Export ANTHROPIC_API_KEY or write $PROJECT_DIR/.anthropic_api_key"
  exit 1
fi

# Ctrl-C must abort the whole batch, not just the container in flight.
trap 'echo; echo "[batch] interrupted - stopping."; exit 130' INT TERM

avail=$(free_gb)
if [[ -n "$avail" ]] && (( avail < MIN_FREE_GB )); then
  echo "ERROR: only ${avail}G free on $REPO_ROOT; need >= ${MIN_FREE_GB}G."; exit 1
fi

STAMP="$(date +%Y%m%d_%H%M%S)"
LOG_DIR="${BATCH_LOG_DIR:-$REPO_ROOT/batch_logs/id_claude_agent_${STAMP}}"
mkdir -p "$LOG_DIR"

echo "=========================================="
echo "[batch] Claude Agent ID batch: $TOTAL containers"
echo "[batch] runs=$RUNS models=$MODELS max_turns=${MAX_TURNS:-default} verify_pass_runs=${VERIFY_PASS_RUNS:-default}"
echo "[batch] free disk      : ${avail:-?}G (min ${MIN_FREE_GB}G)"
echo "[batch] timeout/cont   : ${TIMEOUT_SECS}s"
echo "[batch] logs           : $LOG_DIR"
echo "[batch] started        : $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

ok=0; failed=0; skipped=0
FAILED_LIST=()

for i in "${!CONTAINERS[@]}"; do
  c="${CONTAINERS[$i]}"
  n=$((i + 1))

  if [[ "${SKIP_DONE:-0}" == "1" ]] && compgen -G "$PROJECT_DIR/data/$c/run_*/.run_complete" > /dev/null; then
    echo "[$n/$TOTAL] $c - already has a finished run, skipping"
    skipped=$((skipped + 1))
    continue
  fi

  avail=$(free_gb)
  if [[ -n "$avail" ]] && (( avail < MIN_FREE_GB )); then
    echo "[$n/$TOTAL] ABORT: only ${avail}G free (min ${MIN_FREE_GB}G)."
    break
  fi

  log="$LOG_DIR/${c}.log"
  start=$(date +%s)
  printf '[%d/%d] %s (%sG free) ... ' "$n" "$TOTAL" "$c" "${avail:-?}"

  args=("$SCRIPT_DIR/run_agentic.py" "$c" --runs "$RUNS" --models "$MODELS")
  [[ -n "${MAX_TURNS:-}" ]] && args+=(--max-iterations "$MAX_TURNS")
  [[ -n "${VERIFY_PASS_RUNS:-}" ]] && args+=(--verify-pass-runs "$VERIFY_PASS_RUNS")

  timeout --signal=INT --kill-after=60 "$TIMEOUT_SECS" "$PY" "${args[@]}" > "$log" 2>&1
  rc=$?
  dur=$(( $(date +%s) - start ))

  # timeout leaves the docker container and the child processes behind.
  if (( rc == 124 || rc == 137 )); then
    for p in run_agentic_pass_at_k.py run_agentic_id.sh agentic_claude_cli.py; do
      pkill -f "$p $c" 2>/dev/null
    done
    docker rm -f "tm_${c//[^a-zA-Z0-9]/_}" > /dev/null 2>&1
  fi

  verdict="$(grep -h '^Final verdict:' "$log" 2>/dev/null | tail -1 | sed 's/^Final verdict: *//')"
  if [[ -z "$verdict" ]]; then
    verdict="$(grep -hE '^ERROR:' "$log" 2>/dev/null | tail -1)"
    [[ -n "$verdict" ]] || verdict="(no verdict; rc=$rc)"
  fi

  if (( rc == 0 )); then
    ok=$((ok + 1)); echo "done in ${dur}s - $verdict"
  elif (( rc == 124 )); then
    failed=$((failed + 1)); FAILED_LIST+=("$c (timeout)"); echo "TIMEOUT after ${dur}s  (see $log)"
  else
    failed=$((failed + 1)); FAILED_LIST+=("$c"); echo "EXIT $rc after ${dur}s - $verdict  (see $log)"
  fi

  if [[ "${KEEP_ZIPS:-0}" != "1" ]]; then
    z="$(zip_of "$c")"
    if [[ -n "$z" && -f "$PROJECT_DIR/data/${z}.zip" ]]; then
      still_needed=0
      for (( j = i + 1; j < TOTAL; j++ )); do
        [[ "$(zip_of "${CONTAINERS[$j]}")" == "$z" ]] && { still_needed=1; break; }
      done
      (( still_needed == 0 )) && rm -f "$PROJECT_DIR/data/${z}.zip"
    fi
  fi
done

echo "=========================================="
echo "[batch] finished: $(date '+%Y-%m-%d %H:%M:%S')  rc0=$ok  nonzero_or_timeout=$failed  skipped=$skipped"
for f in ${FAILED_LIST[@]+"${FAILED_LIST[@]}"}; do echo "  - $f"; done
echo "[batch] verdicts: $PROJECT_DIR/data/<container>/summary.csv and $PROJECT_DIR/Complete_Containers_Summary.csv"
echo "=========================================="
