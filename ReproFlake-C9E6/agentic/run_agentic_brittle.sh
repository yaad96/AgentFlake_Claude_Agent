#!/usr/bin/env bash
# ============================================================
# run_agentic_brittle.sh — agentic repair for Brittle tests
#
# "Brittle" tests are structurally identical to OD tests: a polluter
# test runs immediately before the victim and corrupts shared state.
# The pipeline is therefore the same as run_agentic_od.sh, with the
# type-check relaxed to accept "brittle" (and the CSV typo "britle").
#
# Usage:  ./run_agentic_brittle.sh <result_container>
# Requires: ANTHROPIC_API_KEY
# ============================================================

set -euo pipefail

RESULT_CONTAINER="${1:?Usage: $0 <result_container>}"

if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${OPENAI_API_KEY:-}" ]]; then
  echo "ERROR: no LLM API key is set (ANTHROPIC_API_KEY for claude-*, OPENAI_API_KEY for gpt-*)."; exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPROFLAKE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VALG_DIR="$(cd "$REPROFLAKE_DIR/.." && pwd)"
TRACEMOP_SCRIPTS="$REPROFLAKE_DIR/TraceMop Scripts"
LLM_SCRIPTS="$REPROFLAKE_DIR/LLM Scripts"

DATA_DIR="$REPROFLAKE_DIR/data/$RESULT_CONTAINER"
STEPS_OUT_DIR="$DATA_DIR/Steps_Output_Files"
CSV="$REPROFLAKE_DIR/test_config.csv"

TRACEMOP_JAR="$VALG_DIR/experiments/tracemop.jar"
EXT_SRC_DIR="$VALG_DIR/scripts/javamop-extension"
EVENTS_FILE="$VALG_DIR/scripts/events_encoding_id.txt"

COMPARE_TRACES_LOCAL="$TRACEMOP_SCRIPTS/compare-traces-official.py"
COMPARE_TRACES_URL="https://raw.githubusercontent.com/SoftEngResearch/tracemop/master/scripts/compare-traces.py"

[[ -f "$CSV" ]] || { echo "ERROR: $CSV not found"; exit 1; }
ROW=$(awk -F',' -v rc="$RESULT_CONTAINER" '$2 == rc { print; exit }' "$CSV")
[[ -n "$ROW" ]] || { echo "ERROR: '$RESULT_CONTAINER' not in $CSV"; exit 1; }
IFS=',' read -r TEST_TYPE _RC ZIP MODULE POLLUTER VICTIM ITERATIONS CONFIG JAVA NONDEX URL <<< "$ROW"

TTYPE_LOWER=$(echo "$TEST_TYPE" | tr '[:upper:]' '[:lower:]')
if [[ "$TTYPE_LOWER" != "brittle" && "$TTYPE_LOWER" != "britle" ]]; then
  echo "ERROR: this script targets brittle/britle only; got '$TEST_TYPE'."; exit 1
fi
if [[ -z "$POLLUTER" || -z "$VICTIM" ]]; then
  echo "ERROR: brittle container '$RESULT_CONTAINER' must have both polluter and victim in CSV."; exit 1
fi

case "$JAVA" in
  8)  IMAGE="flaky_base_jdk8_od_cov";  DOCKERFILE="Dockerfile.od" ;;
  11) IMAGE="flaky_base_jdk11_od_cov"; DOCKERFILE="Dockerfile.od11" ;;
  *)  echo "ERROR: brittle with java=$JAVA not supported"; exit 1 ;;
esac

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[setup] Docker image '$IMAGE' not found — building from $DOCKERFILE"
  docker build -t "$IMAGE" -f "$REPROFLAKE_DIR/$DOCKERFILE" "$REPROFLAKE_DIR"
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
[AGENTIC BRITTLE]
result_container : $RESULT_CONTAINER
polluter         : $POLLUTER
victim           : $VICTIM
java             : $JAVA  (image: $IMAGE)
container        : $CONTAINER
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
for d in Fixed Flaky Flakym2; do [[ -d "$DATA_DIR/$d" ]] || need_step1=1; done
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
  if [[ ! -d "$DATA_DIR/Fixed" ]]; then
    [[ -f "$DATA_DIR/Fixed.patch" ]] || { echo "ERROR: $DATA_DIR/Fixed.patch missing"; exit 1; }
    echo "[step 1b] Creating Fixed/ = Flaky/ + Fixed.patch"
    cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Fixed"
    patch -p1 -d "$DATA_DIR/Fixed" < "$DATA_DIR/Fixed.patch" >/dev/null
  fi
fi

# STEP 2 — start container
echo "[step 2 ] Starting container '$CONTAINER'"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  --mount type=bind,source="$DATA_DIR",target=/app/work \
  --mount type=bind,source="$DATA_DIR/Flakym2/.m2",target=/root/.m2 \
  "$IMAGE" tail -f /dev/null >/dev/null

# STEPS 3 + 4a + 4b — TraceMOP
echo "[step 3 ] Copying tracemop.jar"
[[ -f "$TRACEMOP_JAR" ]] || { echo "ERROR: $TRACEMOP_JAR not found"; exit 1; }
docker cp "$TRACEMOP_JAR" "$CONTAINER:/tmp/tracemop.jar"

echo "[step 4a] Building javamop-extension"
docker exec "$CONTAINER" mkdir -p /tmp/ext-build
docker cp "$EXT_SRC_DIR/pom.xml" "$CONTAINER:/tmp/ext-build/pom.xml"
docker cp "$EXT_SRC_DIR/src"     "$CONTAINER:/tmp/ext-build/src"
docker exec "$CONTAINER" bash -c "cd /tmp/ext-build && mvn package -DskipTests -q"

echo "[step 4b] Installing tracemop.jar"
docker exec "$CONTAINER" bash -c "mvn install:install-file \
  -Dfile=/tmp/tracemop.jar -DgroupId=javamop-agent \
  -DartifactId=javamop-agent -Dversion=1.0 -Dpackaging=jar -q"

# Run Flaky to capture initial failure log (no TraceMOP)
# TraceMOP traces are computed on demand via get_rv_trace_diff.
EXT_JAR=/tmp/ext-build/target/javamop-extension-1.0.jar
MVNOPTS='-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Dmaven.javadoc.skip'

echo "[step 4d] /app/work/Flaky -> /app/work/traces-flaky (failure log; no TraceMOP)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-flaky; mkdir -p /app/work/traces-flaky
  cd /app/work/Flaky
  mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS
  mvn surefire:test \
    -pl $MODULE -Dtest='$POLLUTER,$VICTIM' \
    -Dsurefire.runOrder=testorder \
    $MVNOPTS 2>&1 | tee /app/work/traces-flaky/mvn.log || true
"

# Sanity: the Flaky run must fail.
echo "[sanity ] Verifying the Flaky run produced a test failure"
SUMMARY=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
            "$DATA_DIR/traces-flaky/mvn.log" 2>/dev/null | tail -1 || true)
if [[ -z "$SUMMARY" ]]; then
  echo "ERROR: no Surefire summary line in traces-flaky/mvn.log"; exit 1
fi
TESTS=$(  sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p' <<<"$SUMMARY"); TESTS=${TESTS:-0}
FAILURES=$(sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'  <<<"$SUMMARY"); FAILURES=${FAILURES:-0}
ERRORS=$(  sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'    <<<"$SUMMARY"); ERRORS=${ERRORS:-0}
if (( TESTS < 1 || FAILURES + ERRORS < 1 )); then
  echo "ERROR: Flaky run did not fail as expected (Tests=$TESTS Failures=$FAILURES Errors=$ERRORS)"; exit 1
fi
echo "[sanity ] Flaky run failed as expected (Tests=$TESTS Failures=$FAILURES Errors=$ERRORS)"

# STEP 5 — copy trace-comparison tooling (used by lazy get_rv_trace_diff)
echo "[step 5 ] Preparing trace-comparison tooling"
if [[ ! -f "$COMPARE_TRACES_LOCAL" ]]; then
  if   command -v curl >/dev/null; then curl -fsSL "$COMPARE_TRACES_URL" -o "$COMPARE_TRACES_LOCAL"
  elif command -v wget >/dev/null; then wget -q "$COMPARE_TRACES_URL" -O "$COMPARE_TRACES_LOCAL"
  else echo "ERROR: need curl or wget"; exit 1; fi
fi
docker cp "$COMPARE_TRACES_LOCAL"          "$CONTAINER:/tmp/compare-traces-official.py"
docker cp "$EVENTS_FILE"                   "$CONTAINER:/tmp/events_encoding_id.txt"
docker cp "$LLM_SCRIPTS/patch_compare.py"  "$CONTAINER:/tmp/patch_compare.py"
docker exec "$CONTAINER" python3 /tmp/patch_compare.py >/dev/null

mkdir -p "$STEPS_OUT_DIR"

# STEP 9.5 — snapshot
echo "[step 9.5] snapshotting Flaky/ -> Flaky.pristine"
rm -rf "$DATA_DIR/Flaky.pristine"
cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Flaky.pristine"

echo "[step 9.5] Writing trace_config.json"
cat > "$STEPS_OUT_DIR/trace_config.json" <<JSONEOF
{
  "docker_container": "$CONTAINER",
  "test_type": "brittle",
  "module": "$MODULE",
  "polluter": "$POLLUTER",
  "victim": "$VICTIM",
  "nondex_seed": "",
  "nondex_runs": 0,
  "wrapper_fqcn": "",
  "surefire_version": "",
  "tracemop_ready": true
}
JSONEOF

# AGENT
echo "[agent ] launching agentic_orchestrator.py (max_iterations=${AGENTIC_MAX_ITERATIONS:-10})"
set +e
python3 "$SCRIPT_DIR/agentic_orchestrator.py" "$RESULT_CONTAINER" \
  --docker-container "$CONTAINER" \
  --max-iterations "${AGENTIC_MAX_ITERATIONS:-10}" \
  ${AGENTIC_MODEL:+--model "$AGENTIC_MODEL"}
AGENT_RC=$?
set -e

if [[ "${KEEP_SOURCE:-0}" != "1" ]]; then
  rm -rf "$DATA_DIR/Flaky.pristine"
fi

echo
echo "=========================================="
echo "[AGENTIC BRITTLE] Done."
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
