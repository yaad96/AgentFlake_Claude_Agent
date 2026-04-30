#!/usr/bin/env bash
# ============================================================
# run_td_tracemop.sh — END-TO-END pipeline for TD flaky tests
#
# Materialises Fixed/ + FlakyCodeChange/, runs TraceMOP on both,
# diffs the traces, generates the LLM trace summary, assembles the
# final llm_context.txt, and calls Claude.
#
# Usage:
#   ./run_td_tracemop.sh <result_container>
#
# Requires:
#   ANTHROPIC_API_KEY in the environment (step 11 is mandatory).
#   pip install anthropic (the call_llm.py script's only non-stdlib dep).
#
# Steps performed (output dir = data/<container>/Steps Output Files/):
#   1.  unzip + apply Fixed.patch / FlakyCodeChange.patch
#   2.  start container with parent data dir mounted
#   5.  copy tracemop.jar
#   6a. build javamop-extension inside container
#   6b. install tracemop.jar into container's local Maven repo
#   6c. run mvn surefire:test with TraceMOP on Fixed/ then FlakyCodeChange/
#   7.  prepare trace-comparison tooling (download upstream compare-traces.py,
#       copy events_encoding_id.txt + patch_compare.py)
#   8C. compare-traces-official.py       -> step_8_C_official.txt
#   9.  generate_llm_summary.py          -> llm_trace_summary.txt
#   10. assemble_llm_context.py          -> llm_context.txt
#   11. call_llm.py                      -> llm_response.json
#
# Container is left running for iteration.
# ============================================================

set -euo pipefail

# ----- args -------------------------------------------------
RESULT_CONTAINER="${1:?Usage: $0 <result_container>   (e.g. BOOKKEEPER-846)}"

# ----- fail fast on missing API key (step 11 is mandatory) ---
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ERROR: ANTHROPIC_API_KEY is not set. Step 11 (call_llm.py) requires it."
  echo "       export ANTHROPIC_API_KEY=sk-ant-...   then re-run."
  exit 1
fi

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

CONTAINER="tm_${RESULT_CONTAINER//[^a-zA-Z0-9]/_}"

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
  echo "[step 1] Fixed/, FlakyCodeChange/, Flakym2/ already present — skipping."
fi

for d in Fixed FlakyCodeChange Flakym2; do
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
# STEP 6c — Run TraceMOP on each codebase variant
# ============================================================
EXT_JAR=/tmp/ext-build/target/javamop-extension-1.0.jar
MVNOPTS='-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Dmaven.javadoc.skip'

run_with_tracemop() {
  local variant="$1"   # "Fixed" or "FlakyCodeChange"
  local label="$2"     # "fixed"  or "flakycc"

  echo "[step 6c] /app/work/$variant  ->  /app/work/traces-$label"

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
# STEP 7 — Prepare trace-comparison tooling
# ============================================================
echo "[step 7 ] Preparing trace-comparison tooling"

# 7a. Cache compare-traces-official.py from upstream (one-time per machine)
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
# STEP 8C — Full trace comparison
# (actual=flakycc, expected=fixed → "only in actual" = flakycc-unique)
# ============================================================
echo "[step 8C] compare-traces-official.py  -> $STEPS_REL/step_8_C_official.txt"
docker exec -w /tmp "$CONTAINER" python3 compare-traces-official.py \
  /app/work/traces-flakycc \
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
for v in fixed flakycc; do
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
