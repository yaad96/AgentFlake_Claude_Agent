#!/usr/bin/env bash
# ============================================================
# run_td_tracemop.sh — END-TO-END pipeline for TD flaky tests
#
# Materialises Fixed/ + FlakyCodeChange/, runs TraceMOP on both,
# diffs the traces, generates the LLM trace summary, assembles the
# final llm_context.txt, and calls the chosen LLM.
#
# Usage:
#   ./run_td_tracemop.sh <result_container> <claude|openai>
#
# Requires:
#   For backend=claude: ANTHROPIC_API_KEY in the environment + pip install anthropic
#   For backend=openai: OPENAI_API_KEY    in the environment + pip install openai
#
# Steps performed (output dir = data/<container>/Steps Output Files/):
#   1.  unzip + apply Fixed.patch / FlakyCodeChange.patch
#   2.  start container with parent data dir mounted
#   3.  copy tracemop.jar
#   4a. build javamop-extension inside container
#   4b. install tracemop.jar into container's local Maven repo
#   4d. run mvn surefire:test with TraceMOP on Fixed/ then FlakyCodeChange/
#   5.  prepare trace-comparison tooling (download upstream compare-traces.py,
#       copy events_encoding_id.txt + patch_compare.py)
#   6.  compare-traces-official.py       -> step_8_C_official.txt
#   7.  generate_llm_summary.py          -> llm_trace_summary.txt
#   8.  assemble_llm_context_td.py       -> llm_context.txt
#   9.  call_llm.py (dispatches to claude or openai) -> llm_response.json
#   10. apply_fix.py                     -> patches Flaky/ + recompiles bytecode
#   11. re-run victim against patched Flaky/ -> verify_after_fix.log
#
# Container is left running for iteration.
# ============================================================

set -euo pipefail

# ----- args -------------------------------------------------
RESULT_CONTAINER="${1:?Usage: $0 <result_container> <claude|openai>   (e.g. BOOKKEEPER-846 claude)}"
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
# Script lives in ReproFlake-C9E6/TraceMop Scripts/
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

if [[ "$TEST_TYPE" != "td" ]]; then
  echo "ERROR: this script targets td only; got '$TEST_TYPE'."
  echo "       (For od/brittle/id, the run shape differs — adapt this script.)"
  exit 1
fi

case "$JAVA" in
  8)  IMAGE="flaky_base_jdk8" ;;
  11) IMAGE="flaky_base_jdk11" ;;
  17) IMAGE="flaky_base_jdk17" ;;
  *)  echo "ERROR: unsupported java=$JAVA"; exit 1 ;;
esac

# Auto-build the JDK 8 base image if missing (the only TD/NIO JDK with a
# Dockerfile in the repo — the bare `Dockerfile`). JDK 11/17 images must be
# preinstalled; if missing, the docker run below will fail with a clear
# Docker-side error.
if [[ "$JAVA" == "8" ]] && ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[setup] Docker image '$IMAGE' not found — building from Dockerfile"
  echo "[setup] (one-time setup, takes a few minutes)"
  docker build -t "$IMAGE" -f "$REPROFLAKE_DIR/Dockerfile" "$REPROFLAKE_DIR"
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
#                     FlakyCodeChange.patch + flaky_info.txt, original .zip.
# REMOVED (every run, unless KEEP_SOURCE=1): Fixed/, Flaky/, FlakyCodeChange/,
#                     Flakym2/, result/. Step 1 re-materialises them from
#                     the zip + patches.
# ============================================================
if [[ "${KEEP_SOURCE:-0}" != "1" ]]; then
  if [[ -d "$DATA_DIR/Fixed" || -d "$DATA_DIR/Flaky" || -d "$DATA_DIR/FlakyCodeChange" || -d "$DATA_DIR/Flakym2" || -d "$DATA_DIR/Flaky.pristine" || -d "$DATA_DIR/result" ]]; then
    echo "[step 0 ] Cleaning mutated source dirs from previous run in $DATA_DIR/"
    echo "          (set KEEP_SOURCE=1 to keep them and resume from existing state)"
    # Flaky.pristine is the snapshot taken in step 9.5 for the feedback
    # round's clean re-apply. Cleaning it here keeps disk bounded across runs.
    rm -rf "$DATA_DIR/Fixed" \
           "$DATA_DIR/FlakyCodeChange" \
           "$DATA_DIR/Flaky" \
           "$DATA_DIR/Flakym2" \
           "$DATA_DIR/Flaky.pristine" \
           "$DATA_DIR/result"
  fi
fi

# ============================================================
# STEP 1 — lean materialisation: unzip + apply patches only
# ============================================================
need_step1=0
for d in Fixed FlakyCodeChange Flakym2; do
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

  apply_patch_variant() {
    local target="$1" patch_file="$2"
    if [[ -d "$DATA_DIR/$target" ]]; then
      echo "[step 1b] $target/ already exists — skipping patch"
      return
    fi
    [[ -f "$DATA_DIR/$patch_file" ]] || { echo "ERROR: $DATA_DIR/$patch_file missing"; exit 1; }
    echo "[step 1b] Creating $target/ = Flaky/ + $patch_file"
    cp -r "$DATA_DIR/Flaky" "$DATA_DIR/$target"
    patch -p1 -d "$DATA_DIR/$target" < "$DATA_DIR/$patch_file" >/dev/null
  }
  apply_patch_variant "Fixed"           "Fixed.patch"
  apply_patch_variant "FlakyCodeChange" "FlakyCodeChange.patch"
else
  echo "[step 1c] Fixed/, FlakyCodeChange/, Flakym2/ already present — skipping."
fi

for d in Fixed FlakyCodeChange Flakym2; do
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
# STEP 4d — Run TraceMOP on each codebase variant
# ============================================================
EXT_JAR=/tmp/ext-build/target/javamop-extension-1.0.jar
MVNOPTS='-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Dmaven.javadoc.skip'

run_with_tracemop() {
  local variant="$1"   # "Fixed" or "FlakyCodeChange"
  local label="$2"     # "fixed"  or "flakycc"

  echo "[step 4d] /app/work/$variant  ->  /app/work/traces-$label"

  docker exec "$CONTAINER" bash -c "
    set -e
    rm -rf /app/work/traces-$label
    mkdir -p /app/work/traces-$label

    export TRACEDB_CONFIG_PATH=/tmp/.trace-db.config
    printf 'db=memory\ndumpDB=false\n' > \$TRACEDB_CONFIG_PATH
    export RVMLOGGINGLEVEL=UNIQUE
    export TRACEDB_PATH=/app/work/traces-$label

    cd /app/work/$variant

    echo '--- pre-build: mvn install -DskipTests -pl $MODULE -am ---'
    mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS

    echo '--- mvn surefire:test with TraceMOP attached ---'
    # Tolerate non-zero exit (FlakyCodeChange will fail the test by design).
    mvn surefire:test \
      -Dmaven.ext.class.path=$EXT_JAR \
      -pl $MODULE \
      -Dtest='$VICTIM' \
      $MVNOPTS 2>&1 | tee /app/work/traces-$label/mvn.log || true
  "
}

run_with_tracemop "Fixed"           "fixed"
run_with_tracemop "FlakyCodeChange" "flakycc"

# ============================================================
# STEP 5 — Prepare trace-comparison tooling
# ============================================================
echo "[step 5 ] Preparing trace-comparison tooling"

# 7a. Cache compare-traces-official.py from upstream (one-time per machine)
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

# 7b. Copy tooling into container
docker cp "$COMPARE_TRACES_LOCAL"             "$CONTAINER:/tmp/compare-traces-official.py"
docker cp "$EVENTS_FILE"                      "$CONTAINER:/tmp/events_encoding_id.txt"
docker cp "$LLM_SCRIPTS_DIR/patch_compare.py" "$CONTAINER:/tmp/patch_compare.py"

# 7c. Patch compare-traces-official.py (idempotent)
docker exec "$CONTAINER" python3 /tmp/patch_compare.py >/dev/null

# Steps 8 onwards write into a dedicated subfolder.
mkdir -p "$STEPS_OUT_DIR"
STEPS_REL="data/$RESULT_CONTAINER/Steps Output Files"

# ============================================================
# STEP 6 — Full trace comparison
# (actual=flakycc, expected=fixed → "only in actual" = flakycc-unique)
# ============================================================
echo "[step 6 ] compare-traces-official.py  -> $STEPS_REL/step_8_C_official.txt"
docker exec -w /tmp "$CONTAINER" python3 compare-traces-official.py \
  /app/work/traces-flakycc \
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
echo "[step 8 ] $ASSEMBLER_VARIANT/assemble_llm_context_td.py  -> $STEPS_REL/llm_context.txt"
( cd "$LLM_SCRIPTS_DIR" && python3 "$ASSEMBLER_VARIANT/assemble_llm_context_td.py" "$RESULT_CONTAINER" ) >/dev/null

# ============================================================
# STEP 9 — Call LLM (mandatory; backend = $LLM_BACKEND)
# ============================================================
echo "[step 9 ] call_llm.py ($LLM_BACKEND)  -> $STEPS_REL/llm_response.json"
( cd "$LLM_SCRIPTS_DIR" && python3 call_llm.py "$RESULT_CONTAINER" "$LLM_BACKEND" )

# ============================================================
# STEP 10 — Apply the LLM-proposed fix to Flaky/ + recompile bytecode
#
# apply_fix.py:
#   1. tries `git apply` (then `--recount`) on output_a.patch
#   2. falls back to splicing output_b.fixed_code via the operation/anchor schema
#   3. host-side javac smoke-tests touched .java files
#   4. runs `mvn test-compile -pl <module> -am` INSIDE the container so
#      target/test-classes/ holds the patched bytecode (without this,
#      step 13's surefire run would silently execute stale .class files
#      and report a false negative).
# ============================================================
# ============================================================
# STEP 9.5 — Snapshot Flaky/ for potential feedback re-apply
# ============================================================
echo "[step 9.5] snapshotting Flaky/ → $DATA_DIR/Flaky.pristine (for feedback re-apply)"
rm -rf "$DATA_DIR/Flaky.pristine"
cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Flaky.pristine"

# ============================================================
# verify_victim() — TD: run the victim alone (no polluter, no testorder).
# Same parse-and-set-VERDICT logic as OD, with a single-test invocation.
#
# CAVEAT (preserved from inline step 11): TD failures are non-deterministic,
# so a single passing run is necessary-but-not-sufficient evidence the fix
# worked. For rigorous verification, run the project's NonDex/iteration
# config from the CSV row separately.
# ============================================================
verify_victim() {
  local VERIFY_LOG VSUM VTESTS VFAIL VERR MARKERS
  VERIFY_LOG="$STEPS_OUT_DIR/verify_after_fix.log"
  echo "[step 11] Re-running '${VICTIM}' against patched Flaky/  -> $STEPS_REL/verify_after_fix.log"

  # -Dsurefire.timeout=180 caps the forked test JVM at 3 minutes. Without
  # it, an LLM-patched test that busy-loops or deadlocks hangs the wrapper
  # indefinitely (observed on curator-681). 180s is ~3× the longest
  # known-good TD test wall time.
  docker exec "$CONTAINER" bash -c "
    cd /app/work/Flaky
    mvn surefire:test \
      -Dmaven.ext.class.path=$EXT_JAR \
      -pl $MODULE \
      -Dtest='$VICTIM' \
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
# See feedback_loop.sh and the analogous block in run_od_tracemop.sh for
# the full mechanism. After the loop, $STEPS_OUT_DIR/verify_after_fix.{log,
# verdict} hold the FINAL attempt's results; *_pre_feedback siblings (if
# feedback fired) capture the original attempt for audit + Phase 5 CSV
# columns.
# ============================================================
# shellcheck source=feedback_loop.sh
source "$SCRIPT_DIR/feedback_loop.sh"
run_apply_verify_feedback_loop

# Cleanup the snapshot. KEEP_SOURCE=1 preserves it for post-run inspection.
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
for v in fixed flakycc; do
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
# the top of this script. Leaving Fixed/, Flaky/, FlakyCodeChange/, Flakym2/,
# and result/ in place after the run is intentional: they are the primary
# evidence for debugging an LLM fix that compiled but didn't behave correctly.
# The next invocation of this script will wipe them before STEP 1.)
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
