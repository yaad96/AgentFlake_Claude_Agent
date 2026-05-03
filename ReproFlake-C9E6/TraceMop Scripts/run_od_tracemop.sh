#!/usr/bin/env bash
# ============================================================
# run_od_tracemop.sh — END-TO-END pipeline for OD flaky tests
#
# Same shape as run_td_tracemop.sh, with OD-specific differences:
#   1. Test type must be 'od'.
#   2. Variants used: Fixed/ (deterministic pass) and Flaky/ (deterministic
#      fail) — no FlakyCodeChange/ injection needed because OD is already
#      deterministic given the polluter→victim order.
#   3. Image: flaky_base_jdk8_od_cov for java=8, flaky_base_jdk11_od_cov
#      for java=11. No java=17 OD rows exist in test_config.csv.
#   4. mvn invocation: -Dmaven.ext.class.path=$EXT_JAR
#      -Dtest="$POLLUTER,$VICTIM" -Dsurefire.runOrder=testorder
#      with env var SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT.
#      (TraceMOP is attached via the JavaMOP extension's argLine injection.
#      Plain -DargLine='-javaagent:tracemop.jar' attaches AspectJ but
#      produces empty traces; the extension does additional plumbing.
#      The SUREFIRE_VERSION env var tells the extension to pin Surefire
#      to 3.0.0-M8-SNAPSHOT instead of upgrading to 3.1.2, so that the
#      'testorder' runOrder is honoured for METHOD-level ordering — required
#      for same-class polluter/victim pairs.)
#   5. Trace dirs: traces-fixed/ (passing) and traces-flaky/ (failing).
#
# Usage:
#   ./run_od_tracemop.sh <result_container> <claude|openai>
#
# Requires:
#   For backend=claude: ANTHROPIC_API_KEY in the environment + pip install anthropic
#   For backend=openai: OPENAI_API_KEY    in the environment + pip install openai
#
# Steps performed (output dir = data/<container>/Steps Output Files/):
#   1.  unzip + apply Fixed.patch
#   2.  start container with parent data dir mounted
#   3.  copy tracemop.jar
#   4a. build javamop-extension inside container
#   4b. install tracemop.jar into container's local Maven repo
#   4d. run mvn surefire:test with TraceMOP on Fixed/ then Flaky/
#       (both with the JavaMOP extension + SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT
#        + -Dtest=POLLUTER,VICTIM + -Dsurefire.runOrder=testorder)
#   5.  prepare trace-comparison tooling
#   6.  compare-traces-official.py       -> step_8_C_official.txt
#   7.  generate_llm_summary.py          -> llm_trace_summary.txt
#   8.  assemble_llm_context_od.py       -> llm_context.txt
#   9.  call_llm.py (dispatches to claude or openai) -> llm_response.json
#   10. apply_fix.py                     -> patches Flaky/ + recompiles bytecode
#   11. re-run polluter,victim against patched Flaky/ -> verify_after_fix.log
#
# Container is left running for iteration.
# ============================================================

set -euo pipefail

# ----- args -------------------------------------------------
RESULT_CONTAINER="${1:?Usage: $0 <result_container> <claude|openai>   (e.g. shardingsphereelasticjobelasticjoblitecore4b9afa4 claude)}"
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
IFS=',' read -r TEST_TYPE _RC ZIP MODULE POLLUTER VICTIM ITERATIONS CONFIG JAVA NONDEX URL <<< "$ROW"

if [[ "$TEST_TYPE" != "od" ]]; then
  echo "ERROR: this script targets od only; got '$TEST_TYPE'."
  echo "       Use run_td_tracemop.sh for td."
  exit 1
fi

if [[ -z "$POLLUTER" || -z "$VICTIM" ]]; then
  echo "ERROR: OD container '$RESULT_CONTAINER' must have both polluter and victim in CSV."
  exit 1
fi

case "$JAVA" in
  8)  IMAGE="flaky_base_jdk8_od_cov";  DOCKERFILE="Dockerfile.od" ;;
  11) IMAGE="flaky_base_jdk11_od_cov"; DOCKERFILE="Dockerfile.od11" ;;
  *)  echo "ERROR: OD with java=$JAVA is not supported by this pipeline."
      echo "       test_config.csv has OD rows for java=8 and java=11 only."
      exit 1 ;;
esac

# Auto-build the OD image on first use. Both JDK 8 and JDK 11 OD images have
# Dockerfiles in the repo. After the build, subsequent invocations short-
# circuit at the `docker image inspect` check above and reuse the cached image.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[setup] Docker image '$IMAGE' not found — building from $DOCKERFILE"
  echo "[setup] (one-time setup, takes a few minutes)"
  docker build -t "$IMAGE" -f "$REPROFLAKE_DIR/$DOCKERFILE" "$REPROFLAKE_DIR"
  echo "[setup] image '$IMAGE' built successfully"
fi

CONTAINER="tm_${RESULT_CONTAINER//[^a-zA-Z0-9]/_}"

# Cleanup trap — kills the container on ANY exit (success, error, signal)
# unless KEEP_CONTAINER=1 is set. Prevents orphan containers from
# accumulating when a run aborts mid-pipeline (Ctrl+C, mvn hang, etc.) or
# when a user runs this script standalone and forgets to clean up.
# `run_pass_at_k.py` sets KEEP_CONTAINER=1 internally so its (model, run)
# loop can reuse the same container across iterations and only clean up
# once at the end of the whole batch.
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
polluter         : $POLLUTER
victim           : $VICTIM
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
  if [[ -d "$DATA_DIR/Fixed" || -d "$DATA_DIR/Flaky" || -d "$DATA_DIR/FlakyCodeChange" || -d "$DATA_DIR/Flakym2" || -d "$DATA_DIR/Flaky.pristine" || -d "$DATA_DIR/result" ]]; then
    echo "[step 0 ] Cleaning mutated source dirs from previous run in $DATA_DIR/"
    echo "          (set KEEP_SOURCE=1 to keep them and resume from existing state)"
    # Flaky.pristine is the snapshot taken in step 9.5 for the feedback
    # round's clean re-apply. It's a duplicate of the unzipped Flaky/, so
    # cleaning it here is safe and keeps disk usage bounded across runs.
    rm -rf "$DATA_DIR/Fixed" \
           "$DATA_DIR/FlakyCodeChange" \
           "$DATA_DIR/Flaky" \
           "$DATA_DIR/Flakym2" \
           "$DATA_DIR/Flaky.pristine" \
           "$DATA_DIR/result"
  fi
fi

# ============================================================
# STEP 1 — lean materialisation: unzip + apply Fixed.patch
# (no FlakyCodeChange/ for OD — Flaky/ is already deterministic
#  given the polluter→victim test order)
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
    echo "[step 1b] Creating Fixed/ = Flaky/ + Fixed.patch"
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
# STEP 2 — Start container with PARENT dir mounted
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
# STEP 4d — Run TraceMOP on each codebase variant (polluter → victim order)
#
# Final approach (verified empirically 2026-04-29):
#
# Two requirements: (i) attach TraceMOP correctly to the forked test JVM;
# (ii) sequence polluter and victim methods within their class.
#
# (i) TraceMOP attachment:
#   tracemop.jar's manifest is "Premain-Class: org.aspectj.weaver.loadtime.Agent"
#   (the AspectJ load-time weaver, configured to weave the bundled
#   mop/*MonitorAspect classes). Empirically, attaching it via plain
#   -DargLine='-javaagent:tracemop.jar' produces ZERO project-code traces
#   — neither locations.txt nor unique-traces.txt is even created. The
#   JavaMOP Maven extension (Valg/scripts/javamop-extension/) is the only
#   known mechanism that produces real traces: its source shows it edits
#   Surefire's argLine in the in-memory project model and does additional
#   lifecycle wiring that a plain -DargLine doesn't replicate.
#
# (ii) Method-level ordering:
#   Stock Surefire's runOrder sequences CLASSES, not methods within a
#   class. Same-class OD (e.g. shardingsphere) needs 'testorder', a custom
#   runOrder added by the TestingResearchIllinois fork (apache/maven-surefire
#   PR #348) that sequences methods by -Dtest= order. The fork is shipped
#   as Surefire 3.0.0-M8-SNAPSHOT and must be pre-installed in the selected
#   OD image.
#
# The trick: by default the JavaMOP extension upgrades Surefire to 3.1.2
# (which has no testorder), BUT it respects the env var SUREFIRE_VERSION.
# Setting SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT before mvn keeps the extension
# active for TraceMOP attachment AND keeps Surefire on the testorder-capable
# fork. Both requirements met.
# ============================================================
EXT_JAR=/tmp/ext-build/target/javamop-extension-1.0.jar
MVNOPTS='-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Dmaven.javadoc.skip'

run_with_tracemop() {
  local variant="$1"   # "Fixed" or "Flaky"
  local label="$2"     # "fixed"  or "flaky"

  echo "[step 4d] /app/work/$variant  ->  /app/work/traces-$label"

  # Inner bash: outer "..." is double-quoted so host expands $variant, $label,
  # $MODULE, $POLLUTER, $VICTIM, $MVNOPTS, $EXT_JAR.
  # \$TRACEDB_CONFIG_PATH is escaped so it expands inside the container.
  #
  # Why we use both the JavaMOP extension AND testorder:
  #   - The extension is the only thing that knows how to wire tracemop.jar
  #     into Surefire's forked-JVM lifecycle (it adds -javaagent to argLine
  #     by editing the in-memory project model). Plain -DargLine doesn't
  #     produce instrumented traces — empirically verified.
  #   - The extension would normally force Surefire to 3.1.2 (which doesn't
  #     support testorder), but it RESPECTS the SUREFIRE_VERSION env var,
  #     so we set it to 3.0.0-M8-SNAPSHOT (the TestingResearchIllinois fork
  #     pre-installed in the selected OD image) — that fork supports
  #     testorder, which sequences METHODS by -Dtest= order, which is what
  #     same-class OD requires.
  docker exec "$CONTAINER" bash -c "
    set -e
    rm -rf /app/work/traces-$label
    mkdir -p /app/work/traces-$label

    export TRACEDB_CONFIG_PATH=/tmp/.trace-db.config
    printf 'db=memory\ndumpDB=false\n' > \$TRACEDB_CONFIG_PATH
    export RVMLOGGINGLEVEL=UNIQUE
    export TRACEDB_PATH=/app/work/traces-$label

    # Tells the JavaMOP extension to pin Surefire to 3.0.0-M8-SNAPSHOT instead
    # of forcing 3.1.2 (which lacks testorder). See JavaMOPExtension.java.
    export SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT

    cd /app/work/$variant

    echo '--- pre-build: mvn install -DskipTests -pl $MODULE -am ---'
    mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS

    echo '--- mvn surefire:test with JavaMOP extension + Surefire 3.0.0-M8-SNAPSHOT + runOrder=testorder ---'
    # Tolerate non-zero exit (Flaky variant will fail the victim by design).
    mvn surefire:test \
      -Dmaven.ext.class.path=$EXT_JAR \
      -pl $MODULE \
      -Dtest='$POLLUTER,$VICTIM' \
      -Dsurefire.runOrder=testorder \
      $MVNOPTS 2>&1 | tee /app/work/traces-$label/mvn.log || true
  "
}

run_with_tracemop "Fixed" "fixed"
run_with_tracemop "Flaky" "flaky"

# ============================================================
# SANITY CHECK — confirm the Flaky run actually failed
#
# Without this guard, a wrong test order (polluter ran AFTER victim) would
# silently produce two passing runs, an empty trace diff, and a meaningless
# LLM prompt. We parse the last Surefire summary line of the Flaky-run
# mvn.log and assert the victim test actually ran AND something failed.
#
# Step 4d uses the JavaMOP extension + SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT
# + -Dsurefire.runOrder=testorder. 'testorder' sequences the two methods
# by their order in -Dtest=. If for any reason the agent didn't attach, or
# testorder isn't actually being honoured by the active Surefire version,
# this guard catches the silent passing-Flaky case before we feed empty
# traces to the LLM.
# ============================================================
echo "[sanity ] Verifying the Flaky run produced an actual test failure"
SUMMARY=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
            "$DATA_DIR/traces-flaky/mvn.log" 2>/dev/null | tail -1 || true)
if [[ -z "$SUMMARY" ]]; then
  echo "ERROR: no Surefire summary line found in $DATA_DIR/traces-flaky/mvn.log"
  echo "       The test may not have been built or run at all."
  echo "       Inspect the log directly to diagnose."
  exit 1
fi
echo "[sanity ] Surefire reported: ${SUMMARY#*\] }"

TESTS=$(   sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p'   <<<"$SUMMARY")
FAILURES=$(sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'    <<<"$SUMMARY")
ERRORS=$(  sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'      <<<"$SUMMARY")
TESTS=${TESTS:-0}; FAILURES=${FAILURES:-0}; ERRORS=${ERRORS:-0}

if (( TESTS < 1 )); then
  echo "ERROR: Flaky run executed 0 tests (Tests run: $TESTS)."
  echo "       The -Dtest= filter probably did not match. Verify the polluter and"
  echo "       victim names in the CSV row exactly match the @Test method names."
  exit 1
fi

if (( FAILURES + ERRORS < 1 )); then
  echo "ERROR: Flaky run had Failures=$FAILURES, Errors=$ERRORS — nothing failed."
  echo
  echo "       Step 4d uses -Dsurefire.runOrder=testorder (provided by the"
  echo "       custom TestingResearchIllinois Surefire fork) to sequence the"
  echo "       two methods in -Dtest= order. If you're seeing this error,"
  echo "       likely causes:"
  echo "         (a) the agent didn't attach (check traces-flaky/mvn.log for"
  echo "             [TraceMOP] lines — if missing, the JavaMOP extension"
  echo "             didn't load, or the agent jar isn't installed in m2);"
  echo "         (b) Surefire didn't honour 'testorder' (look for a runOrder"
  echo "             error in the log; if you see 'Changed surefire version"
  echo "             to 3.1.2', the SUREFIRE_VERSION env var didn't reach"
  echo "             the JavaMOPExtension — e.g., set in the wrong shell);"
  echo "         (c) the OD bug needs more than just polluter→victim ordering"
  echo "             (e.g., a third test or a system property)."
  echo "       Polluter/victim for this run:"
  echo "         polluter: $POLLUTER"
  echo "         victim:   $VICTIM"
  exit 1
fi

echo "[sanity ] Flaky run failed as expected (Tests=$TESTS Failures=$FAILURES Errors=$ERRORS)"

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
# (actual=flaky, expected=fixed → "only in actual" = flaky-unique)
# ============================================================
echo "[step 6 ] compare-traces-official.py  -> $STEPS_REL/step_8_C_official.txt"
docker exec -w /tmp "$CONTAINER" python3 compare-traces-official.py \
  /app/work/traces-flaky \
  /app/work/traces-fixed \
  false > "$STEPS_OUT_DIR/step_8_C_official.txt"

# ============================================================
# STEP 7 — Generate LLM-ready trace summary
# ============================================================
echo "[step 7 ] generate_llm_summary.py     -> $STEPS_REL/llm_trace_summary.txt"
( cd "$LLM_SCRIPTS_DIR" && python3 generate_llm_summary.py "$RESULT_CONTAINER" ) >/dev/null

# ============================================================
# STEP 8 — Assemble LLM context
# ============================================================
# Pick the assembler variant based on the RV-traces ablation switch set by
# run_pass_at_k.py. ${RV_TRACES:-yes} preserves the historical behavior
# (include the RV TRACE ANALYSIS section) for any direct caller that doesn't
# set the var.
ASSEMBLER_VARIANT="rv"
[[ "${RV_TRACES:-yes}" == "no" ]] && ASSEMBLER_VARIANT="no_rv"
echo "[step 8 ] $ASSEMBLER_VARIANT/assemble_llm_context_od.py  -> $STEPS_REL/llm_context.txt"
( cd "$LLM_SCRIPTS_DIR" && python3 "$ASSEMBLER_VARIANT/assemble_llm_context_od.py" "$RESULT_CONTAINER" ) >/dev/null

# ============================================================
# STEP 9 — Call LLM (mandatory; backend = $LLM_BACKEND)
# ============================================================
echo "[step 9 ] call_llm.py ($LLM_BACKEND)  -> $STEPS_REL/llm_response.json"
( cd "$LLM_SCRIPTS_DIR" && python3 call_llm.py "$RESULT_CONTAINER" "$LLM_BACKEND" )

# ============================================================
# STEP 9.5 — Snapshot Flaky/ for potential feedback re-apply
#
# The feedback loop (see feedback_loop.sh) restores Flaky/ from this
# snapshot before re-applying a corrected patch. Cheap (~1-3s on SSD)
# and avoids needing to re-unzip from $ZIP_PATH or reverse-apply the
# previous patch. Cleaned up at end-of-run (or by step 0 of the next run).
# ============================================================
echo "[step 9.5] snapshotting Flaky/ → $DATA_DIR/Flaky.pristine (for feedback re-apply)"
rm -rf "$DATA_DIR/Flaky.pristine"
cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Flaky.pristine"

# ============================================================
# verify_victim() — extracted from the inline step-11 block so the
# feedback loop can call it twice (once for iter 1's apply, once for
# iter 2's post-feedback apply) without code duplication.
#
# Reads:  patched /app/work/Flaky/ inside the container
# Writes: $STEPS_OUT_DIR/verify_after_fix.log
# Sets:   VERDICT global to "PASSED" or "FAILED"
#
# Uses the SAME surefire invocation as step 6c (extension +
# SUREFIRE_VERSION + runOrder=testorder) so we run on the
# testorder-capable Surefire fork. TraceMOP attaches harmlessly — we
# don't read its traces here.
#
# -Dsurefire.timeout=180 caps the forked test JVM at 3 minutes. Without
# it, an LLM-patched test that busy-loops or deadlocks (e.g. a
# `while (!cond) {}` retry without bound) hangs the wrapper indefinitely.
# 180s is ~3× the longest known-good OD test wall time; large enough not
# to cause false-failure on slow tests, small enough that runaway patches
# surface within minutes instead of hours.
# ============================================================
verify_victim() {
  local VERIFY_LOG VSUM VTESTS VFAIL VERR MARKERS
  VERIFY_LOG="$STEPS_OUT_DIR/verify_after_fix.log"
  echo "[step 11] Re-running '${POLLUTER},${VICTIM}' against patched Flaky/  -> $STEPS_REL/verify_after_fix.log"

  docker exec "$CONTAINER" bash -c "
    cd /app/work/Flaky
    export SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT
    mvn surefire:test \
      -Dmaven.ext.class.path=$EXT_JAR \
      -pl $MODULE \
      -Dtest='$POLLUTER,$VICTIM' \
      -Dsurefire.runOrder=testorder \
      -Dsurefire.timeout=180 \
      $MVNOPTS 2>&1
  " > "$VERIFY_LOG" 2>&1 || true

  VSUM=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
            "$VERIFY_LOG" 2>/dev/null | tail -1 || true)

  VERDICT="FAILED"
  if [[ -n "$VSUM" ]]; then
    VTESTS=$(   sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p'   <<<"$VSUM")
    VFAIL=$(    sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'    <<<"$VSUM")
    VERR=$(     sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'      <<<"$VSUM")
    VTESTS=${VTESTS:-0}; VFAIL=${VFAIL:-0}; VERR=${VERR:-0}
    if (( VTESTS > 0 && VFAIL == 0 && VERR == 0 )); then
      # Defensive cross-check: even if surefire's summary line claims 0
      # failures, scan the log for per-test failure markers. Discrepancy
      # would indicate the summary is unreliable (rare but possible with
      # surefire forks, custom providers, or accounting bugs). Treat any
      # discrepancy as FAILED — false-positive PASS is the worst outcome.
      MARKERS=$(grep -cE '<<< FAILURE!|<<< ERROR!' "$VERIFY_LOG" 2>/dev/null || true)
      MARKERS=${MARKERS:-0}
      if (( MARKERS > 0 )); then
        echo "[step 11] WARNING: summary claims 0 failures but log has $MARKERS per-test"
        echo "          failure marker(s) (<<< FAILURE! / <<< ERROR!). Summary is unreliable;"
        echo "          treating as FAILED."
      else
        VERDICT="PASSED"
      fi
    fi
    echo "[step 11] ${VSUM#*\] }"
  else
    echo "[step 11] No Surefire summary line in verify log — verdict FAILED."
  fi
}

# ============================================================
# STEP 10 + 11 — apply_fix.py + verify, with optional feedback round
#
# feedback_loop.sh's run_apply_verify_feedback_loop() does:
#   iter 1: apply_fix.py reads llm_response.json, applies onto Flaky/,
#           recompiles. Then verify_victim() runs the OD polluter,victim
#           pair. If PASSED → done. If FAILED + retriable category
#           (compile_failed | test_failed) → feedback round.
#   between iters: snapshot pre-feedback artifacts, build_feedback.py
#           writes feedback_payload.txt, call_llm.py --feedback-from
#           overwrites llm_response.json with the corrected patch,
#           Flaky/ is restored from Flaky.pristine.
#   iter 2: same apply_fix.py + verify_victim, against the corrected patch.
#
# After the loop, $STEPS_OUT_DIR/verify_after_fix.{log,verdict} hold the
# FINAL attempt's results. *_pre_feedback siblings (if feedback fired)
# capture the original attempt for audit + Phase 5 CSV columns.
# ============================================================
# shellcheck source=feedback_loop.sh
source "$SCRIPT_DIR/feedback_loop.sh"
run_apply_verify_feedback_loop

# Cleanup the snapshot. KEEP_SOURCE=1 preserves it for post-run inspection
# (and step 0 will wipe it on the next run anyway). Default behavior
# reclaims the disk now to keep batch runs bounded.
if [[ "${KEEP_SOURCE:-0}" != "1" ]]; then
  rm -rf "$DATA_DIR/Flaky.pristine"
fi

# ============================================================
# Summary
# ============================================================
echo
echo "=========================================="
echo "Done."
echo
echo "Trace dirs:"
for v in fixed flaky; do
  d="$DATA_DIR/traces-$v"
  ut=0; loc=0
  [[ -f "$d/unique-traces.txt" ]] && ut=$(wc -l < "$d/unique-traces.txt" | tr -d ' ')
  [[ -f "$d/locations.txt"     ]] && loc=$(wc -l < "$d/locations.txt"     | tr -d ' ')
  printf "  traces-%-8s  unique-traces=%s  locations=%s\n" "$v" "$ut" "$loc"
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
