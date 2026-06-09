#!/usr/bin/env bash
# ============================================================
# run_agentic_id.sh — agentic ID repair pipeline
#
# Mirrors run_id_tracemop.sh's setup (steps 1-7) but replaces steps
# 8-11 with a call to agentic_orchestrator.py. The agent then iterates
# through context tools and submit_patch attempts up to a bounded limit.
#
# Usage:  ./run_agentic_id.sh <result_container>
# Requires: ANTHROPIC_API_KEY + pip install anthropic
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
IFS=',' read -r TEST_TYPE _RC ZIP MODULE POLLUTER VICTIM ITERATIONS CONFIG JAVA NONDEXSEED URL <<< "$ROW"

if [[ "$TEST_TYPE" != "id" ]]; then
  echo "ERROR: this script targets id only; got '$TEST_TYPE'."; exit 1
fi
if [[ -z "$NONDEXSEED" ]]; then
  echo "ERROR: ID container '$RESULT_CONTAINER' must have a NonDex seed in CSV."; exit 1
fi

case "$JAVA" in
  8)  IMAGE="flaky_base_jdk_8_id_cover_new";  DOCKERFILE="Dockerfile8.id" ;;
  11) IMAGE="flaky_base_jdk_11_id_cover_new"; DOCKERFILE="Dockerfile11.id" ;;
  17) IMAGE="flaky_base_jdk_17_id_cover_new"; DOCKERFILE="Dockerfile17.id" ;;
  *)  echo "ERROR: unsupported java=$JAVA"; exit 1 ;;
esac

# Build the per-JDK ID image if it isn't present locally (these are local
# build images, never pushed to a registry — so a plain `docker run` would
# otherwise try to pull and fail). Mirrors the other run_agentic_*.sh scripts.
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
[AGENTIC ID]
result_container : $RESULT_CONTAINER
victim           : $VICTIM
nondex seed      : $NONDEXSEED
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
  if [[ ! -d "$DATA_DIR/Flaky" || ! -d "$DATA_DIR/Flakym2" ]]; then
    echo "[step 1a] Unzipping $ZIP_PATH"
    mkdir -p "$DATA_DIR"; unzip -o "$ZIP_PATH" -d "$DATA_DIR" >/dev/null
    if [[ -d "$DATA_DIR/$ZIP" ]]; then
      mv "$DATA_DIR/$ZIP/"* "$DATA_DIR/" 2>/dev/null || true
      rmdir "$DATA_DIR/$ZIP" 2>/dev/null || true
    fi
  fi
  if [[ ! -d "$DATA_DIR/Fixed" ]]; then
    [[ -f "$DATA_DIR/Fixed.patch" ]] || { echo "ERROR: $DATA_DIR/Fixed.patch missing"; exit 1; }
    echo "[step 1b] Creating Fixed/ = Flaky/ + Fixed.patch (evaluation only)"
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

# STEP 4c — NonDex/JavaMOP composition symlink (see run_id_tracemop.sh's note)
echo "[step 4c] Symlinking agent jar for NonDex/JavaMOP composition"
docker exec "$CONTAINER" bash -c "
  mkdir -p /javamop-agent/javamop-agent/1.0 &&
  ln -sf /root/.m2/repository/javamop-agent/javamop-agent/1.0/javamop-agent-1.0.jar \
         /javamop-agent/javamop-agent/1.0/javamop-agent-1.0.jar
"

EXT_JAR=/tmp/ext-build/target/javamop-extension-1.0.jar
MVNOPTS='-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false -Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip -Dmaven.javadoc.skip -Dfindbugs.skip -Dwarbucks.skip -Dmodernizer.skip -Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip -Dcobertura.skip=true -Dspotless.skip=true -Dspotless.check.skip=true -Dossindex.skip=true -Dmaven.bundle.plugin.skip=true -Dmaven.parallel.force=false'

NONDEX_RUNS="$ITERATIONS"
if (( NONDEX_RUNS > 10 )); then
  echo "[step 4d] capping NonDex runs at 10 (CSV says $ITERATIONS)"
  NONDEX_RUNS=10
fi

# Pre-build
echo "[step 4d] pre-build: mvn install -DskipTests"
docker exec "$CONTAINER" bash -c "
  set -e
  cd /app/work/Flaky
  mvn install -DskipTests -pl '$MODULE' -am -q $MVNOPTS
"

# Run #1: traces-pass (plain mvn test, no TraceMOP)
echo "[step 4d] /app/work/Flaky -> /app/work/traces-pass (no TraceMOP)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-pass; mkdir -p /app/work/traces-pass
  cd /app/work/Flaky
  mvn test \
    -pl '$MODULE' -Dtest='$VICTIM' \
    $MVNOPTS 2>&1 | tee /app/work/traces-pass/mvn.log || true
"

# Run #2: traces-fail (NonDex with seed; no TraceMOP — captures failure log)
echo "[step 4d] /app/work/Flaky -> /app/work/traces-fail (NonDex seed=$NONDEXSEED runs=$NONDEX_RUNS)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-fail; mkdir -p /app/work/traces-fail
  cd /app/work/Flaky
  mvn edu.illinois:nondex-maven-plugin:2.1.1:nondex \
    -DnondexSeed=$NONDEXSEED -DnondexRuns=$NONDEX_RUNS \
    -pl '$MODULE' -Dtest='$VICTIM' \
    $MVNOPTS 2>&1 | tee /app/work/traces-fail/mvn.log || true
"

# Sanity: at least one NonDex iteration must have failed.
echo "[sanity ] Verifying at least one NonDex iteration failed"
ITER_SUMMARIES=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
                  "$DATA_DIR/traces-fail/mvn.log" 2>/dev/null || true)
if [[ -z "$ITER_SUMMARIES" ]]; then
  echo "ERROR: no Surefire summary in traces-fail/mvn.log"; exit 1
fi
TOTAL_TESTS=0; TOTAL_FAIL=0; TOTAL_ERR=0; FAIL_ITERS=0
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  t=$(sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p' <<<"$line"); t=${t:-0}
  f=$(sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'  <<<"$line"); f=${f:-0}
  e=$(sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'    <<<"$line"); e=${e:-0}
  TOTAL_TESTS=$((TOTAL_TESTS + t)); TOTAL_FAIL=$((TOTAL_FAIL + f)); TOTAL_ERR=$((TOTAL_ERR + e))
  (( f + e >= 1 )) && FAIL_ITERS=$((FAIL_ITERS + 1))
done <<< "$ITER_SUMMARIES"
echo "[sanity ] Totals: Tests=$TOTAL_TESTS Failures=$TOTAL_FAIL Errors=$TOTAL_ERR  (failing iters=$FAIL_ITERS)"
if (( TOTAL_TESTS < 1 )); then echo "ERROR: NonDex executed 0 tests"; exit 1; fi
if (( TOTAL_FAIL + TOTAL_ERR < 1 )); then
  echo "ERROR: NonDex produced 0 failures across iterations — bug not reproduced"; exit 1
fi

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
docker exec -u 0 "$CONTAINER" python3 /tmp/patch_compare.py >/dev/null

mkdir -p "$STEPS_OUT_DIR"

# STEP 9.5 — snapshot
echo "[step 9.5] snapshotting Flaky/ -> Flaky.pristine"
rm -rf "$DATA_DIR/Flaky.pristine"
cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Flaky.pristine"

echo "[step 9.5] Writing trace_config.json"
cat > "$STEPS_OUT_DIR/trace_config.json" <<JSONEOF
{
  "docker_container": "$CONTAINER",
  "test_type": "id",
  "module": "$MODULE",
  "polluter": "",
  "victim": "$VICTIM",
  "nondex_seed": "$NONDEXSEED",
  "nondex_runs": $NONDEX_RUNS,
  "wrapper_fqcn": "",
  "surefire_version": "",
  "tracemop_ready": true
}
JSONEOF

# AGENT — verify_victim for ID needs NONDEXSEED + NONDEX_RUNS in env;
# agentic_verify.py reads them, mirroring run_id_tracemop.sh's verify_victim().
export NONDEXSEED NONDEX_RUNS
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
echo "[AGENTIC ID] Done."
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
