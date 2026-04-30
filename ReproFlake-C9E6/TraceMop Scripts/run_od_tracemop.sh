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
#   ./run_od_tracemop.sh <result_container>
#
# Requires:
#   ANTHROPIC_API_KEY in the environment (step 11 is mandatory).
#   pip install anthropic
#
# Steps performed (output dir = data/<container>/Steps Output Files/):
#   1.  unzip + apply Fixed.patch
#   2.  start container with parent data dir mounted
#   5.  copy tracemop.jar
#   6a. build javamop-extension inside container
#   6b. install tracemop.jar into container's local Maven repo
#   6c. run mvn surefire:test with TraceMOP on Fixed/ then Flaky/
#       (both with the JavaMOP extension + SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT
#        + -Dtest=POLLUTER,VICTIM + -Dsurefire.runOrder=testorder)
#   7.  prepare trace-comparison tooling
#   8C. compare-traces-official.py       -> step_8_C_official.txt
#   9.  generate_llm_summary.py          -> llm_trace_summary.txt
#   10. assemble_llm_context.py          -> llm_context.txt
#   11. call_llm.py                      -> llm_response.json
#
# Container is left running for iteration.
# ============================================================

set -euo pipefail

# ----- args -------------------------------------------------
RESULT_CONTAINER="${1:?Usage: $0 <result_container>   (e.g. shardingsphereelasticjobelasticjoblitecore4b9afa4)}"

# ----- fail fast on missing API key (step 11 is mandatory) ---
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ERROR: ANTHROPIC_API_KEY is not set. Step 11 (call_llm.py) requires it."
  echo "       export ANTHROPIC_API_KEY=sk-ant-...   then re-run."
  exit 1
fi

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
  8)  IMAGE="flaky_base_jdk8_od_cov" ;;
  11) IMAGE="flaky_base_jdk11_od_cov" ;;
  *)  echo "ERROR: OD with java=$JAVA is not supported by this pipeline."
      echo "       test_config.csv has OD rows for java=8 and java=11 only."
      exit 1 ;;
esac

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "ERROR: required Docker image '$IMAGE' was not found locally."
  if [[ "$JAVA" == "11" ]]; then
    echo "       Java 11 OD rows need an OD-specific JDK 11 image that includes"
    echo "       the TestingResearchIllinois Surefire fork for runOrder=testorder."
    echo "       Build/tag that image as: $IMAGE"
    echo "       Command: docker build -t $IMAGE -f '$REPROFLAKE_DIR/Dockerfile.od11' '$REPROFLAKE_DIR'"
  else
    echo "       Build/tag the OD image as: $IMAGE"
    echo "       Command: docker build -t $IMAGE -f '$REPROFLAKE_DIR/Dockerfile.od' '$REPROFLAKE_DIR'"
  fi
  exit 1
fi

CONTAINER="tm_${RESULT_CONTAINER//[^a-zA-Z0-9]/_}"

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
  echo "[step 1] Fixed/, Flaky/, Flakym2/ already present — skipping."
fi

for d in Fixed Flaky Flakym2; do
  [[ -d "$DATA_DIR/$d" ]] || { echo "ERROR: $DATA_DIR/$d missing after step 1"; exit 1; }
done

# ============================================================
# STEP 2 — Start container with PARENT dir mounted
# ============================================================
echo "[step 2] Starting container '$CONTAINER' from image '$IMAGE'"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  --mount type=bind,source="$DATA_DIR",target=/app/work \
  --mount type=bind,source="$DATA_DIR/Flakym2/.m2",target=/root/.m2 \
  "$IMAGE" tail -f /dev/null >/dev/null

# ============================================================
# STEP 5 — Copy tracemop.jar into container
# ============================================================
echo "[step 5] Copying tracemop.jar"
[[ -f "$TRACEMOP_JAR" ]] || { echo "ERROR: $TRACEMOP_JAR not found"; exit 1; }
docker cp "$TRACEMOP_JAR" "$CONTAINER:/tmp/tracemop.jar"

# ============================================================
# STEP 6a — Build the Maven extension (one-time per container)
# ============================================================
echo "[step 6a] Building javamop-extension inside container"
[[ -d "$EXT_SRC_DIR" ]] || { echo "ERROR: $EXT_SRC_DIR not found"; exit 1; }
docker exec "$CONTAINER" mkdir -p /tmp/ext-build
docker cp "$EXT_SRC_DIR/pom.xml" "$CONTAINER:/tmp/ext-build/pom.xml"
docker cp "$EXT_SRC_DIR/src"     "$CONTAINER:/tmp/ext-build/src"
docker exec "$CONTAINER" bash -c "cd /tmp/ext-build && mvn package -DskipTests -q"

# ============================================================
# STEP 6b — Install tracemop.jar into the container's local Maven repo
# ============================================================
echo "[step 6b] Installing tracemop.jar into /root/.m2"
docker exec "$CONTAINER" bash -c "mvn install:install-file \
  -Dfile=/tmp/tracemop.jar \
  -DgroupId=javamop-agent \
  -DartifactId=javamop-agent \
  -Dversion=1.0 \
  -Dpackaging=jar -q"

# ============================================================
# STEP 6c — Run TraceMOP on each codebase variant (polluter → victim order)
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

  echo "[step 6c] /app/work/$variant  ->  /app/work/traces-$label"

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
# Claude prompt. We parse the last Surefire summary line of the Flaky-run
# mvn.log and assert the victim test actually ran AND something failed.
#
# Step 6c uses the JavaMOP extension + SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT
# + -Dsurefire.runOrder=testorder. 'testorder' sequences the two methods
# by their order in -Dtest=. If for any reason the agent didn't attach, or
# testorder isn't actually being honoured by the active Surefire version,
# this guard catches the silent passing-Flaky case before we feed empty
# traces to Claude.
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
  echo "       Step 6c uses -Dsurefire.runOrder=testorder (provided by the"
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
# STEP 7 — Prepare trace-comparison tooling
# ============================================================
echo "[step 7 ] Preparing trace-comparison tooling"

if [[ ! -f "$COMPARE_TRACES_LOCAL" ]]; then
  echo "[step 7a] Downloading compare-traces.py from upstream"
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
# STEP 8C — Full trace comparison
# (actual=flaky, expected=fixed → "only in actual" = flaky-unique)
# ============================================================
echo "[step 8C] compare-traces-official.py  -> $STEPS_REL/step_8_C_official.txt"
docker exec -w /tmp "$CONTAINER" python3 compare-traces-official.py \
  /app/work/traces-flaky \
  /app/work/traces-fixed \
  false > "$STEPS_OUT_DIR/step_8_C_official.txt"

# ============================================================
# STEP 9 — Generate LLM-ready trace summary
# ============================================================
echo "[step 9 ] generate_llm_summary.py     -> $STEPS_REL/llm_trace_summary.txt"
( cd "$LLM_SCRIPTS_DIR" && python3 generate_llm_summary.py "$RESULT_CONTAINER" ) >/dev/null

# ============================================================
# STEP 10 — Assemble LLM context
# ============================================================
echo "[step 10] assemble_llm_context.py     -> $STEPS_REL/llm_context.txt"
( cd "$LLM_SCRIPTS_DIR" && python3 assemble_llm_context.py "$RESULT_CONTAINER" ) >/dev/null

# ============================================================
# STEP 11 — Call Claude (mandatory)
# ============================================================
echo "[step 11] call_llm.py                 -> $STEPS_REL/llm_response.json"
( cd "$LLM_SCRIPTS_DIR" && python3 call_llm.py "$RESULT_CONTAINER" )

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
for f in step_8_C_official.txt llm_trace_summary.txt llm_context.txt llm_response.json; do
  if [[ -f "$STEPS_OUT_DIR/$f" ]]; then
    sz=$(wc -c < "$STEPS_OUT_DIR/$f" | tr -d ' ')
    printf "  %-26s  %s bytes\n" "$f" "$sz"
  fi
done
echo
echo "Container '$CONTAINER' is left running for iteration."
echo "  Open shell : docker exec -it $CONTAINER bash"
echo "  Stop+rm    : docker rm -f $CONTAINER"
echo "=========================================="
