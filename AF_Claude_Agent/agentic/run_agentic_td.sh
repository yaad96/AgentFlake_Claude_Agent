#!/usr/bin/env bash
# ============================================================
# run_agentic_td.sh — agentic TD repair pipeline
#
# Mirrors run_td_tracemop.sh's setup (steps 1-7) but replaces steps
# 8-11 with a call to agentic_claude_cli.py. The Claude CLI agent then iterates
# through context tools up to the configured Claude Code turn cap.
#
# Usage:  ./run_agentic_td.sh <result_container>
# Requires: ANTHROPIC_API_KEY or .anthropic_api_key + install AF_Claude_Agent/requirements.txt
# ============================================================

set -euo pipefail

RESULT_CONTAINER="${1:?Usage: $0 <result_container>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPROFLAKE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ANTHROPIC_API_KEY_FILE="$REPROFLAKE_DIR/.anthropic_api_key"

if [[ -z "${ANTHROPIC_API_KEY:-}" && -f "$ANTHROPIC_API_KEY_FILE" ]]; then
  ANTHROPIC_API_KEY="$(sed -n "s/^[[:space:]]*//; s/[[:space:]]*$//; /^[#]/d; /^$/d; p; q" "$ANTHROPIC_API_KEY_FILE")"
  export ANTHROPIC_API_KEY
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ERROR: ANTHROPIC_API_KEY is required. Export it or put it in $ANTHROPIC_API_KEY_FILE."; exit 1
fi

DATA_ROOT="$REPROFLAKE_DIR/data/$RESULT_CONTAINER"
if [[ -n "${AGENTIC_RUN_LABEL:-}" ]]; then
  RUN_LABEL="$AGENTIC_RUN_LABEL"
else
  n=1
  while :; do
    RUN_LABEL="$(printf 'run_%02d' "$n")"
    [[ ! -e "$DATA_ROOT/$RUN_LABEL" ]] && break
    n=$((n + 1))
  done
fi
if [[ ! "$RUN_LABEL" =~ ^run_[0-9]+$ ]]; then
  echo "ERROR: AGENTIC_RUN_LABEL must look like run_NN (got '$RUN_LABEL')."; exit 1
fi
export AGENTIC_RUN_LABEL="$RUN_LABEL"
DATA_DIR="$DATA_ROOT/$RUN_LABEL"
CLAUDE_INPUTS_DIR="$DATA_DIR/claude_inputs"
CLAUDE_OUTPUTS_DIR="$DATA_DIR/claude_outputs"
STEPS_OUT_DIR="$CLAUDE_OUTPUTS_DIR"
CSV="$REPROFLAKE_DIR/test_config.csv"

[[ -f "$CSV" ]] || { echo "ERROR: $CSV not found"; exit 1; }
ROW=$(awk -F',' -v rc="$RESULT_CONTAINER" '$2 == rc { print; exit }' "$CSV")
[[ -n "$ROW" ]] || { echo "ERROR: '$RESULT_CONTAINER' not in $CSV"; exit 1; }
ROW="${ROW%$'\r'}"  # strip trailing CR if CSV has CRLF endings
IFS=',' read -r TEST_TYPE _RC ZIP MODULE POLLUTER VICTIM ITERATIONS CONFIG JAVA NONDEX URL <<< "$ROW"

if [[ "$TEST_TYPE" != "td" ]]; then
  echo "ERROR: this script targets td only; got '$TEST_TYPE'."; exit 1
fi

case "$JAVA" in
  8)  IMAGE="flaky_base_jdk8"; DOCKERFILE="Dockerfile" ;;
  11) IMAGE="flaky_base_jdk11" ;;
  17) IMAGE="flaky_base_jdk17" ;;
  *)  echo "ERROR: unsupported java=$JAVA"; exit 1 ;;
esac
PROJECT_KEY="$(printf '%s\n' "$MODULE" | tr '[:upper:]' '[:lower:]')"
if [[ "$PROJECT_KEY" == *hadoop* ]]; then
  IMAGE="flaky_base_jdk8_hadoop"
  DOCKERFILE="Dockerfile.hadoop"
fi

DOCKER_PLATFORM_ARGS=()
if [[ -n "${AGENTIC_DOCKER_PLATFORM:-}" ]]; then
  DOCKER_PLATFORM_ARGS=(--platform "$AGENTIC_DOCKER_PLATFORM")
elif [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
  DOCKER_PLATFORM_ARGS=(--platform linux/amd64)
fi
if ((${#DOCKER_PLATFORM_ARGS[@]})); then
  echo "[setup] Docker platform: ${DOCKER_PLATFORM_ARGS[*]}"
fi

image_has_claude() {
  docker run --rm --entrypoint sh "$1" -lc 'command -v claude >/dev/null 2>&1'
}

ensure_docker_image() {
  local image="$1"
  local dockerfile="${2:-}"

  if [[ "${AGENTIC_FORCE_REBUILD_IMAGE:-0}" == "1" ]] && docker image inspect "$image" >/dev/null 2>&1; then
    echo "[setup] force rebuilding Docker image '$image'"
  elif ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "[setup] Docker image '$image' not found"
  elif image_has_claude "$image"; then
    echo "[setup] Docker image '$image' is ready"
    return 0
  else
    echo "[setup] Docker image '$image' exists but lacks Claude CLI or cannot run"
  fi

  if [[ -z "$dockerfile" ]]; then
    echo "ERROR: image '$image' is missing/stale and no Dockerfile is available in this repo." >&2
    echo "       Rebuild or install an image with the Claude CLI, or choose a supported Java/test-type combination." >&2
    exit 1
  fi
  echo "[setup] building Docker image '$image' from $dockerfile"
  docker build "${DOCKER_PLATFORM_ARGS[@]}" -t "$image" -f "$REPROFLAKE_DIR/$dockerfile" "$REPROFLAKE_DIR"
}

ensure_docker_image "$IMAGE" "${DOCKERFILE:-}"

CONTAINER="tm_${RESULT_CONTAINER//[^a-zA-Z0-9]/_}"
cleanup_container() {
  local rc=$?
  [[ "${KEEP_CONTAINER:-0}" == "1" ]] && return $rc
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  return $rc
}
trap cleanup_container EXIT

cat <<EOF
==========================================
[AGENTIC TD]
result_container : $RESULT_CONTAINER
victim           : $VICTIM
java             : $JAVA  (image: $IMAGE)
container        : $CONTAINER
==========================================
EOF

# STEP 0 — cleanup
if [[ "${KEEP_SOURCE:-0}" != "1" ]]; then
  if [[ -d "$DATA_DIR/Fixed" || -d "$DATA_DIR/Flaky" || -d "$DATA_DIR/FlakyCodeChange" || -d "$DATA_DIR/Flakym2" || -d "$DATA_DIR/Flaky.pristine" || -d "$DATA_DIR/result" ]]; then
    echo "[step 0 ] Cleaning mutated source dirs from previous run"
    rm -rf "$DATA_DIR/Fixed" "$DATA_DIR/FlakyCodeChange" "$DATA_DIR/Flaky" \
           "$DATA_DIR/Flakym2" "$DATA_DIR/Flaky.pristine" "$DATA_DIR/result"
  fi
fi

# STEP 1 — unzip + patches
need_step1=0
for d in Fixed FlakyCodeChange Flakym2; do
  [[ -d "$DATA_DIR/$d" ]] || need_step1=1
done
if (( need_step1 )); then
  ZIP_PATH="$REPROFLAKE_DIR/data/${ZIP}.zip"
  if [[ ! -f "$ZIP_PATH" ]]; then
    [[ -n "$URL" ]] || { echo "ERROR: $ZIP_PATH not found and URL empty"; exit 1; }
    echo "[step 1a] Downloading $URL"
    mkdir -p "$REPROFLAKE_DIR/data"
    if   command -v curl >/dev/null; then curl -fL "$URL" -o "$ZIP_PATH"
    elif command -v wget >/dev/null; then wget "$URL" -O "$ZIP_PATH"
    else echo "ERROR: need curl or wget"; exit 1; fi
  fi
  if [[ ! -d "$DATA_DIR/Flaky" || ! -d "$DATA_DIR/Flakym2" ]]; then
    echo "[step 1a] Unzipping $ZIP_PATH"
    mkdir -p "$DATA_DIR"
    unzip -o "$ZIP_PATH" -d "$DATA_DIR" > /dev/null
    if [[ -d "$DATA_DIR/$ZIP" ]]; then
      mv "$DATA_DIR/$ZIP/"* "$DATA_DIR/" 2>/dev/null || true
      rmdir "$DATA_DIR/$ZIP" 2>/dev/null || true
    fi
  fi
  apply_variant() {
    local target="$1" patch_file="$2"
    [[ -d "$DATA_DIR/$target" ]] && return
    [[ -f "$DATA_DIR/$patch_file" ]] || { echo "ERROR: $DATA_DIR/$patch_file missing"; exit 1; }
    echo "[step 1b] Creating $target/ = Flaky/ + $patch_file"
    cp -r "$DATA_DIR/Flaky" "$DATA_DIR/$target"
    patch -p1 -d "$DATA_DIR/$target" < "$DATA_DIR/$patch_file" >/dev/null
  }
  apply_variant "Fixed"           "Fixed.patch"
  apply_variant "FlakyCodeChange" "FlakyCodeChange.patch"
fi

# STEP 2 — start container
echo "[step 2 ] Starting container '$CONTAINER' from image '$IMAGE'"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
mkdir -p "$DATA_DIR/Flakym2/.m2"
docker run -d "${DOCKER_PLATFORM_ARGS[@]}" --name "$CONTAINER" \
  --mount type=bind,source="$DATA_DIR",target=/app/work \
  --mount type=bind,source="$DATA_DIR/Flakym2/.m2",target=/root/.m2 \
  "$IMAGE" tail -f /dev/null >/dev/null

# STEP 3 — Run FlakyCodeChange to capture failure log.
MVNOPTS='-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Dmaven.javadoc.skip'

echo "[step 3 ] /app/work/FlakyCodeChange -> /app/work/traces-flakycc (failure log)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-flakycc; mkdir -p /app/work/traces-flakycc
  cd /app/work/FlakyCodeChange
  mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS
  mvn surefire:test \
    -pl $MODULE -Dtest='$VICTIM' \
    $MVNOPTS 2>&1 | tee /app/work/traces-flakycc/mvn.log || true
"

# Sanity (HARD GATE): TD FlakyCodeChange MUST reproduce the failure
# deterministically. The forced-verify oracle merges the agent's fix WITH this
# forcing, so if the forcing itself does not fail there is nothing to
# discriminate against -> abort instead of silently scoring a non-discriminative
# PASSED later. Mirrors run_agentic_od.sh's hard gate.
echo "[sanity ] Verifying FlakyCodeChange produced a test failure"
SUMMARY=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
            "$DATA_DIR/traces-flakycc/mvn.log" 2>/dev/null | tail -1 || true)
if [[ -z "$SUMMARY" ]]; then
  echo "ERROR: no Surefire summary in traces-flakycc/mvn.log — FlakyCodeChange did not run; cannot reproduce the TD failure."
  exit 1
fi
TESTS=$(  sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p' <<<"$SUMMARY"); TESTS=${TESTS:-0}
FAILURES=$(sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'  <<<"$SUMMARY"); FAILURES=${FAILURES:-0}
ERRORS=$(  sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'    <<<"$SUMMARY"); ERRORS=${ERRORS:-0}
echo "[sanity ] FlakyCodeChange: Tests=$TESTS Failures=$FAILURES Errors=$ERRORS"
if (( TESTS < 1 )); then
  echo "ERROR: FlakyCodeChange ran 0 tests (Tests=$TESTS) — TD failure not reproduced."; exit 1
fi
if (( FAILURES + ERRORS < 1 )); then
  echo "ERROR: FlakyCodeChange did NOT fail (Failures=$FAILURES Errors=$ERRORS) — TD flakiness not reproduced from FlakyCodeChange; refusing to score a run whose forcing does not fail."; exit 1
fi

mkdir -p "$CLAUDE_INPUTS_DIR" "$CLAUDE_OUTPUTS_DIR"

# STEP 9.5 — snapshot
echo "[step 9.5] snapshotting Flaky/ -> Flaky.pristine"
rm -rf "$DATA_DIR/Flaky.pristine"
cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Flaky.pristine"

echo "[step 9.5] Writing trace_config.json"
cat > "$CLAUDE_INPUTS_DIR/trace_config.json" <<JSONEOF
{
  "docker_container": "$CONTAINER",
  "test_type": "td",
  "module": "$MODULE",
  "polluter": "",
  "victim": "$VICTIM",
  "nondex_seed": "",
  "nondex_runs": 0,
  "wrapper_fqcn": "",
  "surefire_version": "",
  "tracemop_ready": false
}
JSONEOF

# AGENT
  echo "[agent ] launching agentic_claude_cli.py (Claude Code agent, model=${AGENTIC_MODEL:-claude-sonnet-4-6})"
  set +e
  "${AGENTIC_PYTHON:-python3}" "$SCRIPT_DIR/agentic_claude_cli.py" "$RESULT_CONTAINER" \
    --docker-container "$CONTAINER" \
    --model "${AGENTIC_MODEL:-claude-sonnet-4-6}" \
    ${AGENTIC_MAX_BUDGET_USD:+--max-budget-usd "$AGENTIC_MAX_BUDGET_USD"}
  AGENT_RC=$?
  set -e

cleanup_completed_source_dirs() {
  local verdict=""
  if [[ -f "$STEPS_OUT_DIR/run_verdict.txt" ]]; then
    verdict="$(cat "$STEPS_OUT_DIR/run_verdict.txt")"
  elif [[ -f "$STEPS_OUT_DIR/verify_after_fix.verdict" ]]; then
    verdict="$(cat "$STEPS_OUT_DIR/verify_after_fix.verdict")"
  fi

  if [[ "$verdict" == "PASSED" || "$verdict" == "FAILED" ]]; then
    if [[ "${KEEP_SOURCE:-0}" != "1" ]]; then
      echo "[cleanup] removing completed-run source dirs: Fixed Flaky Flakym2 FlakyCodeChange"
      if command -v docker >/dev/null 2>&1; then
        docker exec -u 0 "$CONTAINER" chown -R "$(id -u):$(id -g)" /app/work >/dev/null 2>&1 || true
      fi
      rm -rf "$DATA_DIR/Fixed" "$DATA_DIR/Flaky" "$DATA_DIR/Flakym2" "$DATA_DIR/FlakyCodeChange" ||         echo "[cleanup] WARNING: failed to remove one or more source dirs" >&2
    fi
  fi
}
cleanup_completed_source_dirs

if [[ "${KEEP_SOURCE:-0}" != "1" ]]; then
  rm -rf "$DATA_DIR/Flaky.pristine"
fi

echo
echo "=========================================="
echo "[AGENTIC TD] Done."
for f in run_summary.csv trace_config.json rv_trace_diff.log llm_trace_summary.txt llm_context.txt \
         llm_response.json apply_report.json verify_after_fix.log \
         verify_after_fix.verdict agentic_conversation.json \
         agentic_iterations.jsonl; do
  if [[ -f "$STEPS_OUT_DIR/$f" ]]; then
    sz=$(wc -c < "$STEPS_OUT_DIR/$f" | tr -d ' ')
    printf "  %-30s  %s bytes\n" "$f" "$sz"
  fi
done
if [[ -f "$STEPS_OUT_DIR/verify_after_fix.verdict" ]]; then
  if [[ -f "$STEPS_OUT_DIR/run_verdict.txt" ]]; then
    echo "Final verdict: $(cat "$STEPS_OUT_DIR/run_verdict.txt")   (verification: $(cat "$STEPS_OUT_DIR/verify_after_fix.verdict" 2>/dev/null))"
  else
    echo "Final verdict: $(cat "$STEPS_OUT_DIR/verify_after_fix.verdict")"
  fi
fi
echo "=========================================="
exit $AGENT_RC
