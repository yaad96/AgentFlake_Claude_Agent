#!/usr/bin/env bash
# ============================================================
# run_id_tracemop.sh — END-TO-END pipeline for ID flaky tests
#
# Same shape as run_od_tracemop.sh / run_td_tracemop.sh, with ID-specific
# differences:
#   1. Test type must be 'id'.
#   2. NO polluter — ID flakiness is caused by JDK iteration order shuffled
#      by NonDex on a given seed, not by a preceding test.
#   3. Variants: only Flaky/ is used at runtime. Fixed/ is materialised but
#      reserved for evaluation only (we never feed Fixed/ source to the LLM).
#   4. Image: flaky_base_jdk_{8,11,17}_id_cover_new (NonDex pre-installed).
#   5. mvn invocation: edu.illinois:nondex-maven-plugin:2.1.1:nondex
#                      with -DnondexSeed=<seed> and the JavaMOP extension.
#      A KNOWN ISSUE: the JavaMOP extension supplies an absolute m2 path for
#      the agent jar; NonDex's argLine reconstruction strips the
#      '/root/.m2/repository' prefix, leaving the JVM looking for the agent
#      at /javamop-agent/javamop-agent/1.0/javamop-agent-1.0.jar. We fix this
#      by symlinking that path before any mvn run (step 4c below).
#   6. Trace pair (step 4d):
#       traces-pass/  — Flaky/ + plain `mvn surefire:test` (no NonDex)
#                       This is the "for-sure passing" baseline.
#       traces-fail/  — Flaky/ + `mvn nondex:nondex -DnondexSeed=<seed>`
#                       (clean baseline + 1 shuffled run, both written to
#                        the same TRACEDB; the shuffled run reproduces the
#                        ID failure deterministically)
#      Diffing traces-fail against traces-pass surfaces the runtime events
#      that are specific to the failing ordering.
#   7. Step 11 (verify): re-run NonDex with the same seed against the patched
#      Flaky/ and assert the test now passes (Failures+Errors == 0).
#
# Usage:
#   ./run_id_tracemop.sh <result_container> <claude|openai>
#
# Requires:
#   For backend=claude: ANTHROPIC_API_KEY in the environment + pip install anthropic
#   For backend=openai: OPENAI_API_KEY    in the environment + pip install openai
#
# Steps performed (output dir = data/<container>/Steps Output Files/):
#   1.  unzip + apply Fixed.patch
#   2.  start container (mounts data dir + Flakym2/.m2)
#   3.  copy tracemop.jar
#   4a. build javamop-extension inside container
#   4b. install tracemop.jar into container's local Maven repo
#   4c. symlink agent jar to /javamop-agent/.../1.0/ (NonDex+JavaMOP fix)
#   4d. TWO mvn runs on Flaky/:
#         - plain surefire -> /app/work/traces-pass
#         - nondex+seed   -> /app/work/traces-fail
#   sanity. Verify traces-fail/mvn.log shows Failures+Errors >= 1.
#   5.  prepare trace-comparison tooling
#   6.  compare-traces-official.py traces-fail traces-pass -> step_8_C_official.txt
#   7.  generate_llm_summary.py          -> llm_trace_summary.txt
#   8.  assemble_llm_context_id.py       -> llm_context.txt
#   9.  call_llm.py (dispatches to claude or openai) -> llm_response.json
#   10. apply_fix.py                     -> patches Flaky/ + recompiles bytecode
#   11. re-run nondex+seed against patched Flaky/ -> verify_after_fix.log
#
# Container is left running for iteration.
# ============================================================

set -euo pipefail

# ----- args -------------------------------------------------
RESULT_CONTAINER="${1:?Usage: $0 <result_container> <claude|openai>   (e.g. junitquickcheckgeneratorsbcec1aeverifyInteractionWithRandomness claude)}"
LLM_BACKEND="${2:?Usage: $0 <result_container> <claude|openai>   (second arg picks the LLM backend)}"

case "$LLM_BACKEND" in
  claude)
    if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
      echo "ERROR: ANTHROPIC_API_KEY is not set. Step 9 (claude backend) requires it."
      echo "       export ANTHROPIC_API_KEY=sk-ant-...   then re-run."
      exit 1
    fi
    ;;
  openai)
    if [[ -z "${OPENAI_API_KEY:-}" ]]; then
      echo "ERROR: OPENAI_API_KEY is not set. Step 9 (openai backend) requires it."
      echo "       export OPENAI_API_KEY=sk-...   then re-run."
      exit 1
    fi
    ;;
  *)
    echo "ERROR: backend must be 'claude' or 'openai', got '$LLM_BACKEND'."
    exit 1
    ;;
esac

# ----- paths ------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPROFLAKE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VALG_DIR="$(cd "$REPROFLAKE_DIR/.." && pwd)"

DATA_DIR="$REPROFLAKE_DIR/data/$RESULT_CONTAINER"
STEPS_OUT_DIR="$DATA_DIR/Steps Output Files"
CSV="$REPROFLAKE_DIR/test_config.csv"
LLM_SCRIPTS_DIR="$REPROFLAKE_DIR/LLM Scripts"

TRACEMOP_JAR="$VALG_DIR/experiments/tracemop.jar"
EXT_SRC_DIR="$VALG_DIR/scripts/javamop-extension"
EVENTS_FILE="$VALG_DIR/scripts/events_encoding_id.txt"

COMPARE_TRACES_LOCAL="$SCRIPT_DIR/compare-traces-official.py"
COMPARE_TRACES_URL="https://raw.githubusercontent.com/SoftEngResearch/tracemop/master/scripts/compare-traces.py"

# ----- parse CSV row ----------------------------------------
[[ -f "$CSV" ]] || { echo "ERROR: $CSV not found"; exit 1; }
ROW=$(awk -F',' -v rc="$RESULT_CONTAINER" '$2 == rc { print; exit }' "$CSV")
[[ -n "$ROW" ]] || { echo "ERROR: '$RESULT_CONTAINER' not in $CSV"; exit 1; }
IFS=',' read -r TEST_TYPE _RC ZIP MODULE POLLUTER VICTIM ITERATIONS CONFIG JAVA NONDEXSEED URL <<< "$ROW"

if [[ "$TEST_TYPE" != "id" ]]; then
  echo "ERROR: this script targets id only; got '$TEST_TYPE'."
  echo "       Use run_od_tracemop.sh for od or run_td_tracemop.sh for td."
  exit 1
fi

if [[ -z "$VICTIM" ]]; then
  echo "ERROR: ID container '$RESULT_CONTAINER' must have a victim test in CSV."
  exit 1
fi

if [[ -z "$NONDEXSEED" ]]; then
  echo "ERROR: ID container '$RESULT_CONTAINER' must have a NonDex seed in CSV (10th column)."
  exit 1
fi

case "$JAVA" in
  8)  IMAGE="flaky_base_jdk_8_id_cover_new" ;;
  11) IMAGE="flaky_base_jdk_11_id_cover_new" ;;
  17) IMAGE="flaky_base_jdk_17_id_cover_new" ;;
  *)  echo "ERROR: ID with java=$JAVA is not supported by this pipeline."
      echo "       test_config.csv has ID rows for java=8, 11, 17."
      exit 1 ;;
esac

# Auto-build the ID image on first use. All three JDKs have a matching
# Dockerfile in the repo (Dockerfile{8,11,17}.id). After the build, subsequent
# invocations short-circuit here and reuse the cached image.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  DOCKERFILE="Dockerfile${JAVA}.id"
  echo "[setup] Docker image '$IMAGE' not found — building from $DOCKERFILE"
  echo "[setup] (one-time setup, takes a few minutes)"
  docker build -t "$IMAGE" -f "$REPROFLAKE_DIR/$DOCKERFILE" "$REPROFLAKE_DIR"
  echo "[setup] image '$IMAGE' built successfully"
fi

CONTAINER="tm_${RESULT_CONTAINER//[^a-zA-Z0-9]/_}"

# Cleanup trap — kills the container on ANY exit (success, error, signal)
# unless KEEP_CONTAINER=1 is set. See run_od_tracemop.sh for the full
# rationale. `run_pass_at_k.py` sets KEEP_CONTAINER=1 internally.
cleanup_container() {
  local rc=$?
  if [[ "${KEEP_CONTAINER:-0}" == "1" ]]; then
    return $rc
  fi
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
    echo "[cleanup] removing container '$CONTAINER' (set KEEP_CONTAINER=1 to skip)"
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  fi
  return $rc
}
trap cleanup_container EXIT

cat <<EOF
==========================================
result_container : $RESULT_CONTAINER
test_type        : $TEST_TYPE
module           : $MODULE
victim           : $VICTIM
nondex seed      : $NONDEXSEED  (deterministic reproducer)
java             : $JAVA  (image: $IMAGE)
container        : $CONTAINER
data dir         : $DATA_DIR
==========================================
EOF

# ============================================================
# STEP 0 — START-OF-RUN CLEANUP
#
# We deliberately do cleanup HERE (before step 1) instead of at the end of
# the script. Rationale: leaving the mutated source tree in place after a
# run lets you inspect the post-patch Flaky/, the apply-stage javac errors,
# the surefire-reports/, etc. — invaluable for debugging an LLM patch that
# compiled but didn't actually fix the bug.
#
# Set KEEP_SOURCE=1 to skip this cleanup (e.g., to resume a partial run
# without redoing step 1's unzip + patch).
#
# KEPT (across runs): Steps Output Files/, traces-*/, Fixed.patch +
#                     flaky_info.txt, original .zip.
# REMOVED (every run, unless KEEP_SOURCE=1): Fixed/, Flaky/, FlakyCodeChange/,
#                     Flakym2/, result/. Step 1 re-materialises them from
#                     the zip + patches.
# ============================================================
if [[ "${KEEP_SOURCE:-0}" != "1" ]]; then
  if [[ -d "$DATA_DIR/Fixed" || -d "$DATA_DIR/Flaky" || -d "$DATA_DIR/FlakyCodeChange" || -d "$DATA_DIR/Flakym2" || -d "$DATA_DIR/result" ]]; then
    echo "[step 0 ] Cleaning mutated source dirs from previous run in $DATA_DIR/"
    echo "          (set KEEP_SOURCE=1 to keep them and resume from existing state)"
    rm -rf "$DATA_DIR/Fixed" \
           "$DATA_DIR/FlakyCodeChange" \
           "$DATA_DIR/Flaky" \
           "$DATA_DIR/Flakym2" \
           "$DATA_DIR/result"
  fi
fi

# ============================================================
# STEP 1 — unzip + apply Fixed.patch (same shape as OD)
# Fixed/ is materialised for evaluation use only; the LLM never sees it.
# ============================================================
need_step1=0
for d in Fixed Flaky Flakym2; do
  [[ -d "$DATA_DIR/$d" ]] || need_step1=1
done

if (( need_step1 )); then
  ZIP_PATH="$REPROFLAKE_DIR/data/${ZIP}.zip"
  if [[ ! -f "$ZIP_PATH" ]]; then
    [[ -n "$URL" ]] || { echo "ERROR: $ZIP_PATH not found and CSV URL is empty"; exit 1; }
    echo "[step 1a] Downloading $URL -> $ZIP_PATH"
    mkdir -p "$REPROFLAKE_DIR/data"
    if command -v curl >/dev/null 2>&1; then
      curl -fL "$URL" -o "$ZIP_PATH"
    elif command -v wget >/dev/null 2>&1; then
      wget "$URL" -O "$ZIP_PATH"
    else
      echo "ERROR: need curl or wget to download $URL"; exit 1
    fi
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

  if [[ ! -d "$DATA_DIR/Fixed" ]]; then
    [[ -f "$DATA_DIR/Fixed.patch" ]] || { echo "ERROR: $DATA_DIR/Fixed.patch missing"; exit 1; }
    echo "[step 1b] Creating Fixed/ = Flaky/ + Fixed.patch (evaluation only — never shown to LLM)"
    cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Fixed"
    patch -p1 -d "$DATA_DIR/Fixed" < "$DATA_DIR/Fixed.patch" >/dev/null
  fi
else
  echo "[step 1c] Fixed/, Flaky/, Flakym2/ already present — skipping."
fi

for d in Fixed Flaky Flakym2; do
  [[ -d "$DATA_DIR/$d" ]] || { echo "ERROR: $DATA_DIR/$d missing after step 1"; exit 1; }
done

# ============================================================
# STEP 2 — Start container (mounts data dir + m2)
# ============================================================
echo "[step 2 ] Starting container '$CONTAINER' from image '$IMAGE'"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  --mount type=bind,source="$DATA_DIR",target=/app/work \
  --mount type=bind,source="$DATA_DIR/Flakym2/.m2",target=/root/.m2 \
  "$IMAGE" tail -f /dev/null >/dev/null

# ============================================================
# STEP 3 — Copy tracemop.jar into container
# ============================================================
echo "[step 3 ] Copying tracemop.jar"
[[ -f "$TRACEMOP_JAR" ]] || { echo "ERROR: $TRACEMOP_JAR not found"; exit 1; }
docker cp "$TRACEMOP_JAR" "$CONTAINER:/tmp/tracemop.jar"

# ============================================================
# STEP 4a — Build the Maven extension (one-time per container)
# ============================================================
echo "[step 4a] Building javamop-extension inside container"
[[ -d "$EXT_SRC_DIR" ]] || { echo "ERROR: $EXT_SRC_DIR not found"; exit 1; }
docker exec "$CONTAINER" mkdir -p /tmp/ext-build
docker cp "$EXT_SRC_DIR/pom.xml" "$CONTAINER:/tmp/ext-build/pom.xml"
docker cp "$EXT_SRC_DIR/src"     "$CONTAINER:/tmp/ext-build/src"
docker exec "$CONTAINER" bash -c "cd /tmp/ext-build && mvn package -DskipTests -q"

# ============================================================
# STEP 4b — Install tracemop.jar into the container's local Maven repo
# ============================================================
echo "[step 4b] Installing tracemop.jar into /root/.m2"
docker exec "$CONTAINER" bash -c "mvn install:install-file \
  -Dfile=/tmp/tracemop.jar \
  -DgroupId=javamop-agent \
  -DartifactId=javamop-agent \
  -Dversion=1.0 \
  -Dpackaging=jar -q"

# ============================================================
# STEP 4c — Workaround: symlink the agent jar to the path the JVM looks for.
#
# JavaMOPExtension supplies '-javaagent:/root/.m2/repository/.../agent.jar'
# but NonDex's argLine reconstruction strips '/root/.m2/repository' from
# every argument it emits. The forked JVM ends up trying to load
# '/javamop-agent/javamop-agent/1.0/javamop-agent-1.0.jar' which does not
# exist. Symlinking that path to the real m2 location makes both attach
# correctly. Verified empirically (compose probe, 2026-04-30).
# ============================================================
echo "[step 4c] Symlinking agent jar for NonDex/JavaMOP composition"
docker exec "$CONTAINER" bash -c "
  mkdir -p /javamop-agent/javamop-agent/1.0 &&
  ln -sf /root/.m2/repository/javamop-agent/javamop-agent/1.0/javamop-agent-1.0.jar \
         /javamop-agent/javamop-agent/1.0/javamop-agent-1.0.jar
"

# ============================================================
# STEP 4d — Run TraceMOP twice on Flaky/, with separate TRACEDB_PATH dirs:
#   traces-pass/  via plain surefire (no NonDex; the test passes naturally)
#   traces-fail/  via nondex+seed   (the failing run for the ID bug)
# ============================================================
EXT_JAR=/tmp/ext-build/target/javamop-extension-1.0.jar

# Maven flags lifted from id_statistics_generator_11.sh — these silence the
# many side-plugins that ID-flaky projects often have so that surefire's exit
# is the only meaningful signal.
MVNOPTS='-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false -Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip -Dmaven.javadoc.skip -Dfindbugs.skip -Dwarbucks.skip -Dmodernizer.skip -Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip -Dcobertura.skip=true -Dspotless.skip=true -Dspotless.check.skip=true -Dossindex.skip=true -Dmaven.bundle.plugin.skip=true -Dmaven.parallel.force=false'

echo "[step 4d] pre-build: mvn install -DskipTests -pl $MODULE -am"
docker exec "$CONTAINER" bash -c "
  set -e
  cd /app/work/Flaky
  mvn install -DskipTests -pl '$MODULE' -am -q $MVNOPTS
"

# Number of NonDex iterations to walk through. The CSV's nondex column is a
# STARTING seed for NonDex's deterministic seed walk — not a guaranteed-failing
# seed. With nondexRuns=1, only the starting iteration runs, which may or may
# not happen to expose the bug. Using ITERATIONS from the CSV makes NonDex
# walk through enough seeds to hit at least one failing one. We cap at 10 so a
# CSV row of iterations=100 doesn't make this step take ~2.5 hours.
NONDEX_RUNS="$ITERATIONS"
if (( NONDEX_RUNS > 10 )); then
  echo "[step 4d] capping NonDex runs at 10 (CSV says $ITERATIONS — too expensive for trace collection)"
  NONDEX_RUNS=10
fi

# --- Run #1: traces-pass ---
# Use `mvn test` (not `mvn surefire:test`) so JaCoCo's prepare-agent runs in
# the `initialize` phase first. The Flaky/ pom in ID-coverage projects has been
# mutated by modify_pom_for_coverage.sh to reference ${argLine}; without
# prepare-agent that placeholder stays unresolved and gets handed to `java`
# as a literal class name (boom: "Could not find or load main class ${argLine}").
echo "[step 4d] /app/work/Flaky -> /app/work/traces-pass (mvn test, NO NonDex)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-pass
  mkdir -p /app/work/traces-pass

  export TRACEDB_CONFIG_PATH=/tmp/.trace-db.config
  printf 'db=memory\ndumpDB=false\n' > \$TRACEDB_CONFIG_PATH
  export RVMLOGGINGLEVEL=UNIQUE
  export TRACEDB_PATH=/app/work/traces-pass

  cd /app/work/Flaky

  # Tolerate non-zero exit; some build plugins may complain even on a clean run.
  mvn test \
    -Dmaven.ext.class.path=$EXT_JAR \
    -pl '$MODULE' \
    -Dtest='$VICTIM' \
    $MVNOPTS 2>&1 | tee /app/work/traces-pass/mvn.log || true
"

# --- Run #2: traces-fail ---
# Walk $NONDEX_RUNS NonDex iterations starting from the recorded seed. Even if
# the recorded seed itself happens to pass (the CSV column is a starting seed,
# not a known-failing seed), later iterations with derived seeds typically
# expose the bug. Trace events from all iterations land in TRACEDB_PATH; the
# subsequent diff against traces-pass surfaces failing-iteration-specific
# events.
echo "[step 4d] /app/work/Flaky -> /app/work/traces-fail (NonDex seed=$NONDEXSEED, runs=$NONDEX_RUNS)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-fail
  mkdir -p /app/work/traces-fail

  export TRACEDB_CONFIG_PATH=/tmp/.trace-db.config
  printf 'db=memory\ndumpDB=false\n' > \$TRACEDB_CONFIG_PATH
  export RVMLOGGINGLEVEL=UNIQUE
  export TRACEDB_PATH=/app/work/traces-fail

  cd /app/work/Flaky

  mvn edu.illinois:nondex-maven-plugin:2.1.1:nondex \
    -DnondexSeed=$NONDEXSEED -DnondexRuns=$NONDEX_RUNS \
    -Dmaven.ext.class.path=$EXT_JAR \
    -pl '$MODULE' \
    -Dtest='$VICTIM' \
    $MVNOPTS 2>&1 | tee /app/work/traces-fail/mvn.log || true
"

# ============================================================
# SANITY CHECK — confirm the NonDex+seed run actually failed.
#
# Without this guard, a wrong seed (or NonDex+TraceMOP failing to compose)
# would silently produce a passing flaky log and an empty / meaningless trace
# diff, which would in turn produce a meaningless LLM prompt.
#
# The NonDex plugin runs a clean baseline first (which passes) and then the
# seeded shuffled run (which should fail). The LAST surefire summary in the
# log corresponds to the shuffled run; we parse that and require Failures or
# Errors >= 1.
# ============================================================
echo "[sanity ] Verifying at least one NonDex iteration failed"

# With multi-iteration NonDex, the LAST surefire summary in the log is from the
# LAST iteration — which may have happened to pass. We need a SUM across all
# iterations: if any single iteration shows Failures>=1 or Errors>=1, sanity
# passes.
ITER_SUMMARIES=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
                  "$DATA_DIR/traces-fail/mvn.log" 2>/dev/null || true)
if [[ -z "$ITER_SUMMARIES" ]]; then
  echo "ERROR: no Surefire summary line found in $DATA_DIR/traces-fail/mvn.log"
  echo "       The test may not have been built or run at all. Inspect the log directly."
  exit 1
fi

TOTAL_TESTS=0; TOTAL_FAILURES=0; TOTAL_ERRORS=0; ITER_COUNT=0; FAILING_ITERS=0
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  ITER_COUNT=$((ITER_COUNT + 1))
  t=$(sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p' <<<"$line"); t=${t:-0}
  f=$(sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'  <<<"$line"); f=${f:-0}
  e=$(sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'    <<<"$line"); e=${e:-0}
  TOTAL_TESTS=$((TOTAL_TESTS + t))
  TOTAL_FAILURES=$((TOTAL_FAILURES + f))
  TOTAL_ERRORS=$((TOTAL_ERRORS + e))
  if (( f + e >= 1 )); then
    FAILING_ITERS=$((FAILING_ITERS + 1))
  fi
done <<< "$ITER_SUMMARIES"

echo "[sanity ] Surefire summaries: $ITER_COUNT (failing iterations: $FAILING_ITERS)"
echo "[sanity ] Totals: Tests=$TOTAL_TESTS Failures=$TOTAL_FAILURES Errors=$TOTAL_ERRORS"

if (( TOTAL_TESTS < 1 )); then
  echo "ERROR: NonDex+seed run executed 0 tests. The -Dtest= filter probably did not match."
  exit 1
fi

if (( TOTAL_FAILURES + TOTAL_ERRORS < 1 )); then
  echo "ERROR: NonDex run produced 0 failures across $ITER_COUNT iterations."
  echo
  echo "       Likely causes:"
  echo "         (a) JavaMOP / NonDex did not compose — check that the symlink at"
  echo "             /javamop-agent/javamop-agent/1.0/javamop-agent-1.0.jar exists."
  echo "             grep '\\[TraceMOP\\]' $DATA_DIR/traces-fail/mvn.log"
  echo "         (b) None of the $ITER_COUNT iterations exposed the bug on this"
  echo "             image / JDK version. Try increasing ITERATIONS in test_config.csv,"
  echo "             or pick a known-failing seed from rounds-test-results.csv that"
  echo "             id_statistics_generator_${JAVA}.sh produced."
  echo "         (c) The bug requires more than iteration shuffling (e.g., specific"
  echo "             locale or platform property in addition to the seed)."
  echo "       Victim:        $VICTIM"
  echo "       Starting seed: $NONDEXSEED"
  echo "       Iterations:    $NONDEX_RUNS"
  exit 1
fi

echo "[sanity ] Failing run confirmed ($FAILING_ITERS / $ITER_COUNT iterations failed)"

# ============================================================
# STEP 5 — Prepare trace-comparison tooling
# ============================================================
echo "[step 5 ] Preparing trace-comparison tooling"

if [[ ! -f "$COMPARE_TRACES_LOCAL" ]]; then
  echo "[step 5a] Downloading compare-traces.py from upstream"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$COMPARE_TRACES_URL" -o "$COMPARE_TRACES_LOCAL"
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$COMPARE_TRACES_URL" -O "$COMPARE_TRACES_LOCAL"
  else
    echo "ERROR: need curl or wget to fetch $COMPARE_TRACES_URL"; exit 1
  fi
fi

docker cp "$COMPARE_TRACES_LOCAL"             "$CONTAINER:/tmp/compare-traces-official.py"
docker cp "$EVENTS_FILE"                      "$CONTAINER:/tmp/events_encoding_id.txt"
docker cp "$LLM_SCRIPTS_DIR/patch_compare.py" "$CONTAINER:/tmp/patch_compare.py"

docker exec "$CONTAINER" python3 /tmp/patch_compare.py >/dev/null

# Steps 8 onwards write into a dedicated subfolder.
mkdir -p "$STEPS_OUT_DIR"
STEPS_REL="data/$RESULT_CONTAINER/Steps Output Files"

# ============================================================
# STEP 6 — Full trace comparison
# (actual=fail, expected=pass → "only in actual" = failing-seed-specific events)
# ============================================================
echo "[step 6 ] compare-traces-official.py  -> $STEPS_REL/step_8_C_official.txt"
docker exec -w /tmp "$CONTAINER" python3 compare-traces-official.py \
  /app/work/traces-fail \
  /app/work/traces-pass \
  false > "$STEPS_OUT_DIR/step_8_C_official.txt"

# ============================================================
# STEP 7 — Generate LLM-ready trace summary
# (generate_llm_summary.py handles ID — has_polluter is gated on test_type
#  in {od, britle}, so ID rows correctly skip the Polluter line.)
# ============================================================
echo "[step 7 ] generate_llm_summary.py     -> $STEPS_REL/llm_trace_summary.txt"
( cd "$LLM_SCRIPTS_DIR" && python3 generate_llm_summary.py "$RESULT_CONTAINER" ) >/dev/null

# ============================================================
# STEP 8 — Assemble LLM context (ID-specific)
# ============================================================
# Pick the assembler variant based on the RV-traces ablation switch set by
# run_pass_at_k.py. ${RV_TRACES:-yes} preserves the historical behavior
# (include the RV TRACE ANALYSIS section) for any direct caller that doesn't
# set the var.
ASSEMBLER_VARIANT="rv"
[[ "${RV_TRACES:-yes}" == "no" ]] && ASSEMBLER_VARIANT="no_rv"
echo "[step 8 ] $ASSEMBLER_VARIANT/assemble_llm_context_id.py  -> $STEPS_REL/llm_context.txt"
( cd "$LLM_SCRIPTS_DIR" && python3 "$ASSEMBLER_VARIANT/assemble_llm_context_id.py" "$RESULT_CONTAINER" ) >/dev/null

# ============================================================
# STEP 9 — Call LLM (mandatory; backend = $LLM_BACKEND)
# ============================================================
echo "[step 9 ] call_llm.py ($LLM_BACKEND)  -> $STEPS_REL/llm_response.json"
( cd "$LLM_SCRIPTS_DIR" && python3 call_llm.py "$RESULT_CONTAINER" "$LLM_BACKEND" )

# ============================================================
# STEP 10 — Apply the LLM-proposed fix to Flaky/ + recompile bytecode
# (apply_fix.py operates on Flaky/ and recompiles inside $CONTAINER —
#  identical to OD/TD usage.)
# ============================================================
echo "[step 10] apply_fix.py                -> $STEPS_REL/apply_report.json"
STEP12_OK=1
( cd "$LLM_SCRIPTS_DIR" && python3 apply_fix.py "$RESULT_CONTAINER" \
    --docker-container "$CONTAINER" ) || STEP12_OK=0

if (( ! STEP12_OK )); then
  echo "[step 10] apply_fix.py exited non-zero — LLM patch did not land cleanly."
  echo "          See $STEPS_REL/apply_report.json for details."
  echo "          Verdict will be FAILED (no compiled fix to verify)."
fi

# ============================================================
# STEP 11 — Verify the LLM fix removes the ID flakiness.
#
# Strict binary verdict:
#   PASSED iff step 12 landed AND every NonDex iteration on the patched
#          Flaky/ passes (Tests>0, Failures=0, Errors=0 across all runs).
#   FAILED in all other cases.
#
# Re-uses the same seed sequence as the traces-fail run so we directly test
# whether the failing orderings still fail.
# ============================================================
VERDICT="FAILED"
if (( STEP12_OK )); then
  VERIFY_LOG="$STEPS_OUT_DIR/verify_after_fix.log"
  echo "[step 11] Re-running '${VICTIM}' under NonDex seed=$NONDEXSEED, runs=$NONDEX_RUNS against patched Flaky/  -> $STEPS_REL/verify_after_fix.log"

  # -Dsurefire.timeout=180 caps each forked surefire JVM at 3 minutes.
  # NonDex runs N independent surefire iterations; each one gets its own
  # 180s budget. Without this, an LLM-patched test that busy-loops or
  # deadlocks could hang ID verification indefinitely.
  docker exec "$CONTAINER" bash -c "
    cd /app/work/Flaky
    mvn edu.illinois:nondex-maven-plugin:2.1.1:nondex \
      -DnondexSeed=$NONDEXSEED -DnondexRuns=$NONDEX_RUNS \
      -Dmaven.ext.class.path=$EXT_JAR \
      -pl '$MODULE' \
      -Dtest='$VICTIM' \
      -Dsurefire.timeout=180 \
      $MVNOPTS 2>&1
  " > "$VERIFY_LOG" 2>&1 || true

  V_SUMS=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
            "$VERIFY_LOG" 2>/dev/null || true)

  if [[ -n "$V_SUMS" ]]; then
    V_TESTS=0; V_FAIL=0; V_ERR=0; V_ITERS=0; V_FAIL_ITERS=0
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      V_ITERS=$((V_ITERS + 1))
      t=$(sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p' <<<"$line"); t=${t:-0}
      f=$(sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'  <<<"$line"); f=${f:-0}
      e=$(sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'    <<<"$line"); e=${e:-0}
      V_TESTS=$((V_TESTS + t))
      V_FAIL=$((V_FAIL + f))
      V_ERR=$((V_ERR + e))
      if (( f + e >= 1 )); then
        V_FAIL_ITERS=$((V_FAIL_ITERS + 1))
      fi
    done <<< "$V_SUMS"
    echo "[step 11] $V_ITERS iterations: Tests=$V_TESTS Failures=$V_FAIL Errors=$V_ERR (failing iters: $V_FAIL_ITERS)"
    if (( V_TESTS > 0 && V_FAIL == 0 && V_ERR == 0 )); then
      # Defensive cross-check: even if every NonDex iteration's summary line
      # claims 0 failures/errors, scan the log for per-test failure markers.
      # Discrepancy indicates the summary aggregation is unreliable; treat
      # as FAILED — false-positive PASS is the worst outcome. For NonDex
      # multi-iteration runs, ANY iteration's <<< FAILURE!/ERROR! marker
      # downgrades the whole verdict.
      MARKERS=$(grep -cE '<<< FAILURE!|<<< ERROR!' "$VERIFY_LOG" 2>/dev/null || true)
      MARKERS=${MARKERS:-0}
      if (( MARKERS > 0 )); then
        echo "[step 11] WARNING: summary claims 0 failures across $V_ITERS iteration(s)"
        echo "          but log has $MARKERS per-test failure marker(s) (<<< FAILURE! / <<< ERROR!)."
        echo "          Summary is unreliable; treating as FAILED."
        VERDICT="FAILED"
      else
        VERDICT="PASSED"
      fi
    fi
  else
    echo "[step 11] No Surefire summary lines in verify log — verdict FAILED."
  fi
fi
printf '%s\n' "$VERDICT" > "$STEPS_OUT_DIR/verify_after_fix.verdict"

# ============================================================
# Summary
# ============================================================
echo
echo "=========================================="
echo "Done."
echo
echo "Trace dirs:"
for v in pass fail; do
  d="$DATA_DIR/traces-$v"
  ut=0; loc=0
  [[ -f "$d/unique-traces.txt" ]] && ut=$(wc -l < "$d/unique-traces.txt" | tr -d ' ')
  [[ -f "$d/locations.txt"     ]] && loc=$(wc -l < "$d/locations.txt"     | tr -d ' ')
  printf "  traces-%-5s  unique-traces=%s  locations=%s\n" "$v" "$ut" "$loc"
done
echo
echo "Pipeline outputs ($STEPS_REL/):"
for f in step_8_C_official.txt llm_trace_summary.txt llm_context.txt llm_response.json \
         apply_report.json verify_after_fix.log verify_after_fix.verdict; do
  if [[ -f "$STEPS_OUT_DIR/$f" ]]; then
    sz=$(wc -c < "$STEPS_OUT_DIR/$f" | tr -d ' ')
    printf "  %-26s  %s bytes\n" "$f" "$sz"
  fi
done
echo
echo "Post-fix verdict   : $VERDICT"

# ============================================================
# (Cleanup of mutated source dirs runs at START-OF-RUN — see STEP 0 near
# the top of this script. Leaving Fixed/, Flaky/, Flakym2/, and result/ in
# place after the run is intentional: they are the primary evidence for
# debugging an LLM fix that compiled but didn't behave correctly. The next
# invocation of this script will wipe them before STEP 1.)
# ============================================================

echo
if [[ "${KEEP_CONTAINER:-0}" == "1" ]]; then
  echo "Container '$CONTAINER' left running (KEEP_CONTAINER=1) for inspection:"
  echo "  Flaky/                 — LLM-patched source"
  echo "  target/                — recompiled bytecode"
  echo "  surefire-reports/      — verify run output"
  echo "Remove when done: docker rm -f $CONTAINER"
else
  echo "Container '$CONTAINER' will be removed by the cleanup trap."
  echo "(Set KEEP_CONTAINER=1 next time to preserve it for inspection.)"
fi
echo "=========================================="
