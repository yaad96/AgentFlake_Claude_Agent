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

DATA_DIR="$REPROFLAKE_DIR/data/$RESULT_CONTAINER"
STEPS_OUT_DIR="$DATA_DIR/Steps_Output_Files"
CSV="$REPROFLAKE_DIR/test_config.csv"

[[ -f "$CSV" ]] || { echo "ERROR: $CSV not found"; exit 1; }
ROW=$(awk -F',' -v rc="$RESULT_CONTAINER" '$2 == rc { print; exit }' "$CSV")
[[ -n "$ROW" ]] || { echo "ERROR: '$RESULT_CONTAINER' not in $CSV"; exit 1; }
ROW="${ROW%$'\r'}"  # strip trailing CR if CSV has CRLF endings
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
PROJECT_KEY="$(printf '%s\n' "$MODULE" | tr '[:upper:]' '[:lower:]')"
if [[ "$PROJECT_KEY" == *hadoop* ]]; then
  IMAGE="flaky_base_jdk8_hadoop"
  DOCKERFILE="Dockerfile.hadoop"
fi
NONDEX_PLUGIN_VERSION="2.1.1"
if [[ "$JAVA" == "17" ]]; then
  NONDEX_PLUGIN_VERSION="2.1.7"
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

# Build the per-JDK ID image if it isn't present locally (these are local
# build images, never pushed to a registry — so a plain `docker run` would
# otherwise try to pull and fail). Mirrors the other run_agentic_*.sh scripts.
if ((${#DOCKER_PLATFORM_ARGS[@]})) || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[setup] Docker image '$IMAGE' not found — building from $DOCKERFILE"
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
M2_MOUNT_ARGS=()
if [[ -d "$DATA_DIR/Flakym2/.m2" ]]; then
  M2_MOUNT_ARGS=(--mount type=bind,source="$DATA_DIR/Flakym2/.m2",target=/root/.m2)
fi
docker run -d "${DOCKER_PLATFORM_ARGS[@]}" --name "$CONTAINER" \
  --mount type=bind,source="$DATA_DIR",target=/app/work \
  "${M2_MOUNT_ARGS[@]}" \
  "$IMAGE" tail -f /dev/null >/dev/null

MVNOPTS='-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false -Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip -Dmaven.javadoc.skip -Dfindbugs.skip -Dwarbucks.skip -Dmodernizer.skip -Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip -Dcobertura.skip=true -Dspotless.skip=true -Dspotless.check.skip=true -Dossindex.skip=true -Dmaven.bundle.plugin.skip=true -Dmaven.parallel.force=false'

NONDEX_RUNS="$ITERATIONS"
if (( NONDEX_RUNS > 10 )); then
  echo "[step 4d] capping NonDex runs at 10 (CSV says $ITERATIONS)"
  NONDEX_RUNS=10
fi

# Pre-build
PREBUILD_SKIP_ARG="-Dmaven.test.skip=true"
PREBUILD_TARGET_ARGS="-pl '$MODULE' -am"
if [[ "$PROJECT_KEY" == *flink* ]]; then
  PREBUILD_SKIP_ARG="-DskipTests"
  PREBUILD_TARGET_ARGS="-pl flink-runtime,flink-test-utils-parent/flink-test-utils,'$MODULE' -am"
fi
echo "[step 4d] pre-build: mvn install $PREBUILD_SKIP_ARG"
docker exec "$CONTAINER" bash -c "
  set -e
  cd /app/work/Flaky
  mvn install $PREBUILD_SKIP_ARG $PREBUILD_TARGET_ARGS -q $MVNOPTS
"

# Run #1: traces-pass (plain mvn test)
echo "[step 3 ] /app/work/Flaky -> /app/work/traces-pass"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-pass; mkdir -p /app/work/traces-pass
  cd /app/work/Flaky
  mvn test \
    -pl '$MODULE' -Dtest='$VICTIM' \
    $MVNOPTS 2>&1 | tee /app/work/traces-pass/mvn.log || true
"

# Run #2: traces-fail (NonDex with seed; captures failure log)
echo "[step 3 ] /app/work/Flaky -> /app/work/traces-fail (NonDex seed=$NONDEXSEED max-runs=$NONDEX_RUNS)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-fail; mkdir -p /app/work/traces-fail
  cd /app/work/Flaky
  : > /app/work/traces-fail/mvn.log
  python3 - <<'PY' > /app/work/traces-fail/seeds.txt
seed = int('$NONDEXSEED')
mask = (1 << 48) - 1
mult = 0x5DEECE66D
add = 0xB
state = (seed ^ mult) & mask
print(seed)
for _ in range(1, int('$NONDEX_RUNS')):
    # Java Random.next(32) gives a 32-bit signed int seed. NonDex nondexSeed is
    # an int parameter, so 64-bit longs overflow it. Keep fallback seeds within
    # int range -- valid for NonDex 2.1.1 and 2.1.7.
    state = (state * mult + add) & mask
    val = state >> 16
    if val >= (1 << 31):
        val -= 1 << 32
    print(val)
PY
  i=0
  while IFS= read -r seed; do
    i=\$((i + 1))
    echo \"[nondex] attempt \$i/$NONDEX_RUNS seed=\$seed\" | tee -a /app/work/traces-fail/mvn.log
    mvn edu.illinois:nondex-maven-plugin:$NONDEX_PLUGIN_VERSION:nondex \
      -DnondexSeed=\$seed -DnondexRuns=1 \
      -pl '$MODULE' -Dtest='$VICTIM' \
      $MVNOPTS 2>&1 | tee -a /app/work/traces-fail/mvn.log || true
    if tail -n 200 /app/work/traces-fail/mvn.log | grep -Eq 'Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[1-9][0-9]*|Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[1-9][0-9]*'; then
      echo \"\$seed\" > /app/work/traces-fail/failing_seed
      break
    fi
  done < /app/work/traces-fail/seeds.txt
"

if [[ -f "$DATA_DIR/traces-fail/failing_seed" ]]; then
  NONDEXSEED="$(cat "$DATA_DIR/traces-fail/failing_seed")"
  NONDEX_RUNS=1
  echo "[step 4d] using reproduced failing NonDex seed=$NONDEXSEED"
fi

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
  "nondex_plugin_version": "$NONDEX_PLUGIN_VERSION",
  "wrapper_fqcn": "",
  "surefire_version": "",
  "tracemop_ready": false
}
JSONEOF

# AGENT — verify_victim for ID needs NONDEXSEED + NONDEX_RUNS in env;
# agentic_verify.py reads them, mirroring run_id_tracemop.sh's verify_victim().
export NONDEXSEED NONDEX_RUNS NONDEX_PLUGIN_VERSION
if [[ "${AGENTIC_DRIVER:-orchestrator}" == "claude_cli" ]]; then
  echo "[agent ] launching agentic_claude_cli.py (Claude Code agent, model=${AGENTIC_MODEL:-claude-sonnet-4-6})"
  set +e
  python3 "$SCRIPT_DIR/agentic_claude_cli.py" "$RESULT_CONTAINER" \
    --docker-container "$CONTAINER" \
    --model "${AGENTIC_MODEL:-claude-sonnet-4-6}" \
    ${AGENTIC_MAX_BUDGET_USD:+--max-budget-usd "$AGENTIC_MAX_BUDGET_USD"}
  AGENT_RC=$?
  set -e
else
  echo "[agent ] launching agentic_orchestrator.py (max_iterations=${AGENTIC_MAX_ITERATIONS:-10})"
  set +e
  python3 "$SCRIPT_DIR/agentic_orchestrator.py" "$RESULT_CONTAINER" \
    --docker-container "$CONTAINER" \
    --max-iterations "${AGENTIC_MAX_ITERATIONS:-10}" \
    ${AGENTIC_MODEL:+--model "$AGENTIC_MODEL"}
  AGENT_RC=$?
  set -e

  # Durably archive this orchestrator run into the AGENTIC_FULL_RUNS layout:
  #   data/AGENTIC_FULL_RUNS/<container>_runs/<model>/run_<N>   (never overwrites).
  # (The claude_cli branch already writes data/claude_agent/<container>/run_<NN>.)
  if [[ -f "$STEPS_OUT_DIR/verify_after_fix.verdict" || -f "$STEPS_OUT_DIR/run_verdict.txt" ]]; then
    bash "$SCRIPT_DIR/archive_orchestrator_run.sh" \
      "$RESULT_CONTAINER" "$STEPS_OUT_DIR" "${AGENTIC_MODEL:-claude-sonnet-4-6}" "$REPROFLAKE_DIR" \
      || echo "[archive] WARNING: archival failed (run still in $STEPS_OUT_DIR)"
  fi
fi

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
