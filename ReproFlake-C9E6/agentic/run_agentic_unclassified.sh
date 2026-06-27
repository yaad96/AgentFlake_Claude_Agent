#!/usr/bin/env bash
# ============================================================
# run_agentic_unclassified.sh — agentic repair for Unclassified tests
#
# For tests with no known flaky-test category the pipeline cannot run
# TraceMOP trace comparison (there is no "correct" variant to compare
# against) and cannot offer category-specific exemplars to the agent.
#
# What this script does instead:
#   1.  unzip + Fixed.patch (evaluation scaffold only)
#   2.  start docker container
#   3.  pre-build + run  mvn test -Dtest=<victim>  to capture the failure
#       log in  traces-fail/mvn.log  (the same path the orchestrator probes)
#   4.  snapshot Flaky/ -> Flaky.pristine
#   AGENT  agentic_orchestrator.py  with  --exclude-tools get_flaky_example
#          (category unknown => no exemplar offered)
#
# Usage:  ./run_agentic_unclassified.sh <result_container>
# Requires: ANTHROPIC_API_KEY
# ============================================================

set -euo pipefail

RESULT_CONTAINER="${1:?Usage: $0 <result_container>}"

if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${OPENAI_API_KEY:-}" ]]; then
  echo "ERROR: no LLM API key is set (ANTHROPIC_API_KEY for claude-*, OPENAI_API_KEY for gpt-*)."; exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPROFLAKE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DATA_DIR="$REPROFLAKE_DIR/data/$RESULT_CONTAINER"
STEPS_OUT_DIR="$DATA_DIR/Steps_Output_Files"
CSV="$REPROFLAKE_DIR/test_config.csv"

[[ -f "$CSV" ]] || { echo "ERROR: $CSV not found"; exit 1; }
ROW=$(awk -F',' -v rc="$RESULT_CONTAINER" '$2 == rc { print; exit }' "$CSV")
[[ -n "$ROW" ]] || { echo "ERROR: '$RESULT_CONTAINER' not in $CSV"; exit 1; }
ROW="${ROW%$'\r'}"  # strip trailing CR if CSV has CRLF endings
IFS=',' read -r TEST_TYPE _RC ZIP MODULE POLLUTER VICTIM ITERATIONS CONFIG JAVA NONDEX URL <<< "$ROW"

# Accept both capitalisation variants and the brittle-in-wrong-column edge case
TTYPE_LOWER=$(echo "$TEST_TYPE" | tr '[:upper:]' '[:lower:]')
if [[ "$TTYPE_LOWER" != "unclassified" && "$TTYPE_LOWER" != "unassigned" ]]; then
  echo "ERROR: this script targets Unclassified/Unassigned only; got '$TEST_TYPE'."; exit 1
fi

case "$JAVA" in
  8)  IMAGE="flaky_base_jdk8"; DOCKERFILE="Dockerfile" ;;
  11) IMAGE="flaky_base_jdk11" ;;
  17) IMAGE="flaky_base_jdk17" ;;
  *)  echo "ERROR: unsupported java=$JAVA"; exit 1 ;;
esac
PROJECT_KEY="$(printf '%s\n%s\n%s\n' "$RESULT_CONTAINER" "$ZIP" "$MODULE" | tr '[:upper:]' '[:lower:]')"
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

# Build the basic JDK8 image if it does not exist yet (same check as run_agentic_td.sh).
if ((${#DOCKER_PLATFORM_ARGS[@]})) || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  DOCKERFILE="${DOCKERFILE:-Dockerfile}"
  echo "[setup] Building image '$IMAGE' from $DOCKERFILE (one-time)"
  docker build "${DOCKER_PLATFORM_ARGS[@]}" -t "$IMAGE" -f "$REPROFLAKE_DIR/$DOCKERFILE" "$REPROFLAKE_DIR"
fi

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
[AGENTIC UNCLASSIFIED]
result_container : $RESULT_CONTAINER
victim           : $VICTIM
java             : $JAVA  (image: $IMAGE)
container        : $CONTAINER
NOTE: no category-specific exemplar offered to the agent
==========================================
EOF

# STEP 0 — cleanup
if [[ "${KEEP_SOURCE:-0}" != "1" ]]; then
  if [[ -d "$DATA_DIR/Fixed" || -d "$DATA_DIR/Flaky" || -d "$DATA_DIR/Flakym2" || -d "$DATA_DIR/Flaky.pristine" || -d "$DATA_DIR/result" ]]; then
    echo "[step 0 ] Cleaning mutated source dirs from previous run"
    rm -rf "$DATA_DIR/Fixed" "$DATA_DIR/Flaky" "$DATA_DIR/Flakym2" \
           "$DATA_DIR/Flaky.pristine" "$DATA_DIR/result"
  fi
fi

# STEP 1 — unzip + Fixed.patch
need_step1=0
for d in Flaky Flakym2; do [[ -d "$DATA_DIR/$d" ]] || need_step1=1; done
if (( need_step1 )); then
  ZIP_PATH="$REPROFLAKE_DIR/data/${ZIP}.zip"
  if [[ ! -f "$ZIP_PATH" ]]; then
    [[ -n "$URL" ]] || { echo "ERROR: $ZIP_PATH not found and URL empty"; exit 1; }
    mkdir -p "$REPROFLAKE_DIR/data"
    if   command -v curl >/dev/null; then curl -fL "$URL" -o "$ZIP_PATH"
    elif command -v wget >/dev/null; then wget "$URL" -O "$ZIP_PATH"
    else echo "ERROR: need curl or wget"; exit 1; fi
  fi
  echo "[step 1a] Unzipping $ZIP_PATH"
  mkdir -p "$DATA_DIR"; unzip -o "$ZIP_PATH" -d "$DATA_DIR" >/dev/null
  if [[ -d "$DATA_DIR/$ZIP" ]]; then
    mv "$DATA_DIR/$ZIP/"* "$DATA_DIR/" 2>/dev/null || true
    rmdir "$DATA_DIR/$ZIP" 2>/dev/null || true
  fi
  if [[ ! -d "$DATA_DIR/Fixed" && -f "$DATA_DIR/Fixed.patch" ]]; then
    echo "[step 1b] Creating Fixed/ = Flaky/ + Fixed.patch (evaluation only)"
    cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Fixed"
    patch -p1 -d "$DATA_DIR/Fixed" < "$DATA_DIR/Fixed.patch" >/dev/null
  fi
fi

# STEP 2 — start container
echo "[step 2 ] Starting container '$CONTAINER'"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
mkdir -p "$DATA_DIR/Flakym2/.m2"
docker run -d "${DOCKER_PLATFORM_ARGS[@]}" --name "$CONTAINER" \
  --mount type=bind,source="$DATA_DIR",target=/app/work \
  --mount type=bind,source="$DATA_DIR/Flakym2/.m2",target=/root/.m2 \
  "$IMAGE" tail -f /dev/null >/dev/null

MVNOPTS='-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Dmaven.javadoc.skip'

# STEP 3 — pre-build + run victim to capture failure log
echo "[step 3 ] pre-build: mvn install -Dmaven.test.skip=true"
docker exec "$CONTAINER" bash -c "
  set -e
  cd /app/work/Flaky
  mvn install -Dmaven.test.skip=true -pl '$MODULE' -am -q $MVNOPTS || true
"

echo "[step 3 ] Running victim to capture failure -> traces-fail/mvn.log"
docker exec "$CONTAINER" bash -c "
  rm -rf /app/work/traces-fail
  mkdir -p /app/work/traces-fail
  cd /app/work/Flaky
  mvn test -pl '$MODULE' -Dtest='$VICTIM' \
    $MVNOPTS 2>&1 | tee /app/work/traces-fail/mvn.log || true
"

# Sanity: the test must actually fail before we hand off to the agent.
echo "[sanity ] Verifying the run produced a test failure"
SUMMARY=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
            "$DATA_DIR/traces-fail/mvn.log" 2>/dev/null | tail -1 || true)
if [[ -z "$SUMMARY" ]]; then
  echo "WARNING: no Surefire summary in traces-fail/mvn.log — continuing anyway"
else
  TESTS=$(  sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p' <<<"$SUMMARY"); TESTS=${TESTS:-0}
  FAILURES=$(sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'  <<<"$SUMMARY"); FAILURES=${FAILURES:-0}
  ERRORS=$(  sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'    <<<"$SUMMARY"); ERRORS=${ERRORS:-0}
  echo "[sanity ] Tests=$TESTS Failures=$FAILURES Errors=$ERRORS"
  if (( TESTS > 0 && FAILURES + ERRORS == 0 )); then
    echo "WARNING: test passed on first run (possibly intermittent) — continuing anyway"
  fi
fi

mkdir -p "$STEPS_OUT_DIR"

# STEP 9.5 — snapshot Flaky/ for between-iteration restore
echo "[step 9.5] snapshotting Flaky/ -> Flaky.pristine"
rm -rf "$DATA_DIR/Flaky.pristine"
cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Flaky.pristine"

echo "[step 9.5] Writing trace_config.json"
cat > "$STEPS_OUT_DIR/trace_config.json" <<JSONEOF
{
  "docker_container": "$CONTAINER",
  "test_type": "unclassified",
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

# AGENT — exclude get_flaky_example because no category is known.
echo "[agent ] launching agentic_orchestrator.py (max_iterations=${AGENTIC_MAX_ITERATIONS:-10})"
set +e
python3 "$SCRIPT_DIR/agentic_orchestrator.py" "$RESULT_CONTAINER" \
  --docker-container "$CONTAINER" \
  --max-iterations "${AGENTIC_MAX_ITERATIONS:-10}" \
  --exclude-tools "get_flaky_example" \
  ${AGENTIC_MODEL:+--model "$AGENTIC_MODEL"}
AGENT_RC=$?
set -e

if [[ "${KEEP_SOURCE:-0}" != "1" ]]; then
  rm -rf "$DATA_DIR/Flaky.pristine"
fi

echo
echo "=========================================="
echo "[AGENTIC UNCLASSIFIED] Done."
for f in run_summary.csv trace_config.json llm_context.txt llm_response.json apply_report.json \
         verify_after_fix.log verify_after_fix.verdict \
         agentic_conversation.json agentic_iterations.jsonl; do
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
