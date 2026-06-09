#!/usr/bin/env bash
# ============================================================
# run_agentic_nio.sh — agentic NIO repair pipeline
#
# Mirrors run_nio_tracemop.sh's setup (steps 1-7 plus the auto-generated
# JUnit wrapper that re-invokes the victim twice in one JVM) but replaces
# steps 8-11 with a call to agentic_orchestrator.py. The orchestrator then
# iterates through context tools and submit_patch attempts up to a bounded
# limit, using the wrapper-based verify command provided by agentic_verify.py.
#
# Usage:  ./run_agentic_nio.sh <result_container>
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
IFS=',' read -r TEST_TYPE _RC ZIP MODULE POLLUTER VICTIM ITERATIONS CONFIG JAVA NONDEX URL <<< "$ROW"

if [[ "$TEST_TYPE" != "nio" ]]; then
  echo "ERROR: this script targets nio only; got '$TEST_TYPE'."; exit 1
fi
if [[ -z "$VICTIM" ]]; then
  echo "ERROR: NIO container '$RESULT_CONTAINER' must have a victim test in CSV."; exit 1
fi

# Derive wrapper identifiers exactly the way run_nio_tracemop.sh does.
VICTIM_CLASS_FULL="${VICTIM%#*}"
VICTIM_METHOD="${VICTIM##*#}"
VICTIM_CLASS_SIMPLE="${VICTIM_CLASS_FULL##*.}"
VICTIM_PKG="${VICTIM_CLASS_FULL%.*}"
VICTIM_PKG_PATH="$(echo "$VICTIM_PKG" | tr '.' '/')"
METHOD_CAP="$(printf '%s' "${VICTIM_METHOD:0:1}" | tr '[:lower:]' '[:upper:]')${VICTIM_METHOD:1}"
WRAPPER_CLASS_SIMPLE="${METHOD_CAP}NioReproTest"
WRAPPER_FQCN="${VICTIM_PKG}.${WRAPPER_CLASS_SIMPLE}"
WRAPPER_PATH_REL="${MODULE}/src/test/java/${VICTIM_PKG_PATH}/${WRAPPER_CLASS_SIMPLE}.java"

case "$JAVA" in
  8)  IMAGE="flaky_base_jdk8" ;;
  11) IMAGE="flaky_base_jdk11" ;;
  17) IMAGE="flaky_base_jdk17" ;;
  *)  echo "ERROR: unsupported java=$JAVA"; exit 1 ;;
esac

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  if [[ "$JAVA" == "8" ]]; then
    echo "[setup] Building image '$IMAGE' from Dockerfile (one-time)"
    docker build -t "$IMAGE" -f "$REPROFLAKE_DIR/Dockerfile" "$REPROFLAKE_DIR"
  else
    echo "ERROR: image '$IMAGE' not found locally and no Dockerfile in repo"; exit 1
  fi
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
[AGENTIC NIO]
result_container : $RESULT_CONTAINER
victim           : $VICTIM
wrapper          : $WRAPPER_FQCN
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
    mkdir -p "$DATA_DIR"
    unzip -o "$ZIP_PATH" -d "$DATA_DIR" >/dev/null
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
fi

# Preflight: victim method must exist in resolved source.
VICTIM_FILE_REL="${MODULE}/src/test/java/${VICTIM_PKG_PATH}/${VICTIM_CLASS_SIMPLE}.java"
VICTIM_FILE_ABS="$DATA_DIR/Fixed/$VICTIM_FILE_REL"
if [[ ! -f "$VICTIM_FILE_ABS" ]]; then
  echo "ERROR: victim source file not found at $VICTIM_FILE_REL"; exit 1
fi
if ! grep -qwF "$VICTIM_METHOD" "$VICTIM_FILE_ABS"; then
  echo "ERROR: victim method '$VICTIM_METHOD' not in $VICTIM_FILE_REL"; exit 1
fi

# Detect surefire version pinned by the project (single-property resolution
# only — sufficient for the agentic case, matches run_nio_tracemop.sh's logic
# for the common case).
SUREFIRE_VER=$(awk '
  /<plugin>/,/<\/plugin>/ {
    if (/maven-surefire-plugin/) found=1
    if (found && /<version>/) {
      sub(/.*<version>/, "")
      sub(/<\/version>.*/, "")
      gsub(/[[:space:]]/, "")
      print
      exit
    }
    if (/<\/plugin>/) found=0
  }
' "$DATA_DIR/Flaky/pom.xml" 2>/dev/null)
PROP_RX='^\$\{(.+)\}$'
for _ in 1 2 3; do
  [[ "$SUREFIRE_VER" =~ $PROP_RX ]] || break
  prop_name="${BASH_REMATCH[1]}"
  esc_prop="${prop_name//./\\.}"
  resolved=$(find "$DATA_DIR/Flaky" -maxdepth 8 -name pom.xml -print0 2>/dev/null \
    | xargs -0 grep -h "<$prop_name>" 2>/dev/null \
    | sed -nE "s|.*<${esc_prop}>([^<]+)</${esc_prop}>.*|\1|p" \
    | head -n 1 | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')
  [[ -z "$resolved" ]] && { SUREFIRE_VER=""; break; }
  SUREFIRE_VER="$resolved"
done
[[ -z "$SUREFIRE_VER" ]] && SUREFIRE_VER="3.0.0-M5"
echo "[step 1c] Surefire version: $SUREFIRE_VER"

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

# STEP 4c — generate wrapper class in BOTH Fixed/ and Flaky/
echo "[step 4c] Generating NIO wrapper at $WRAPPER_PATH_REL"
gen_wrapper() {
  local root="$1"
  local out="$root/$WRAPPER_PATH_REL"
  mkdir -p "$(dirname "$out")"
  cat > "$out" <<EOF
package ${VICTIM_PKG};

// AUTO-GENERATED by run_agentic_nio.sh — DO NOT EDIT.
// NIO repro driver: invokes ${VICTIM_CLASS_SIMPLE}#${VICTIM_METHOD} twice in
// the same JVM (full JUnit lifecycle each time). Fix target is the victim,
// NOT this file.

import org.junit.Test;
import org.junit.Assert;
import org.junit.runner.JUnitCore;
import org.junit.runner.Request;
import org.junit.runner.Result;

public class ${WRAPPER_CLASS_SIMPLE} {
    @Test public void runTwice() throws Exception {
        Request req = Request.method(${VICTIM_CLASS_SIMPLE}.class, "${VICTIM_METHOD}");
        Result r1 = new JUnitCore().run(req);
        Assert.assertTrue("first invocation should pass: " + r1.getFailures(), r1.wasSuccessful());
        Result r2 = new JUnitCore().run(req);
        Assert.assertTrue("second invocation should pass (NIO assertion): " + r2.getFailures(), r2.wasSuccessful());
    }
}
EOF
}
gen_wrapper "$DATA_DIR/Fixed"
gen_wrapper "$DATA_DIR/Flaky"

# STEP 4d — Run Fixed+wrapper and Flaky+wrapper to capture logs (no TraceMOP)
# TraceMOP traces are computed on demand via get_rv_trace_diff.
EXT_JAR=/tmp/ext-build/target/javamop-extension-1.0.jar
MVNOPTS='-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false -Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip -Dmaven.javadoc.skip -Dwarbucks.skip -Dmodernizer.skip -Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip -Dcobertura.skip=true -Dfindbugs.skip=true -Dspotless.skip=true -Dspotless.check.skip=true -Dossindex.skip=true -Dmaven.bundle.plugin.skip=true -Dmaven.parallel.force=false'

echo "[step 4d] /app/work/Fixed + wrapper -> /app/work/traces-fixed (sanity; no TraceMOP)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-fixed; mkdir -p /app/work/traces-fixed
  export SUREFIRE_VERSION=$SUREFIRE_VER
  cd /app/work/Fixed
  mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS
  mvn test \
    -pl $MODULE -am \
    -Dtest='${WRAPPER_FQCN}#runTwice' \
    $MVNOPTS 2>&1 | tee /app/work/traces-fixed/mvn.log || true
"

echo "[step 4d] /app/work/Flaky + wrapper -> /app/work/traces-flaky (failure log; no TraceMOP)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-flaky; mkdir -p /app/work/traces-flaky
  export SUREFIRE_VERSION=$SUREFIRE_VER
  cd /app/work/Flaky
  mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS
  mvn test \
    -pl $MODULE -am \
    -Dtest='${WRAPPER_FQCN}#runTwice' \
    $MVNOPTS 2>&1 | tee /app/work/traces-flaky/mvn.log || true
"

# Sanity: Fixed+wrapper PASSED and Flaky+wrapper FAILED.
parse_summary() {
  local sum t f e
  sum=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
          "$1" 2>/dev/null | tail -1 || true)
  if [[ -z "$sum" ]]; then echo "0 0 0"; return; fi
  t=$(sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p' <<<"$sum"); t=${t:-0}
  f=$(sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'  <<<"$sum"); f=${f:-0}
  e=$(sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'    <<<"$sum"); e=${e:-0}
  echo "$t $f $e"
}

read -r FT FF FE <<< "$(parse_summary "$DATA_DIR/traces-fixed/mvn.log")"
echo "[sanity ] Fixed+wrapper:  Tests=$FT Failures=$FF Errors=$FE"
if (( FT < 1 || FF + FE >= 1 )); then
  echo "ERROR: Fixed+wrapper did not pass cleanly — pipeline broken"; exit 1
fi
read -r KT KF KE <<< "$(parse_summary "$DATA_DIR/traces-flaky/mvn.log")"
echo "[sanity ] Flaky+wrapper:  Tests=$KT Failures=$KF Errors=$KE"
if (( KT < 1 || KF + KE < 1 )); then
  echo "ERROR: Flaky+wrapper did not exhibit NIO behaviour — bug not reproduced"; exit 1
fi
echo "[sanity ] OK — Fixed passed, Flaky failed (NIO reproduced)"

# STEP 5 — copy trace-comparison tooling (used by lazy get_rv_trace_diff)
echo "[step 5 ] Preparing trace-comparison tooling"
if [[ ! -f "$COMPARE_TRACES_LOCAL" ]]; then
  if   command -v curl >/dev/null; then curl -fsSL "$COMPARE_TRACES_URL" -o "$COMPARE_TRACES_LOCAL"
  elif command -v wget >/dev/null; then wget -q "$COMPARE_TRACES_URL" -O "$COMPARE_TRACES_LOCAL"
  else echo "ERROR: need curl or wget"; exit 1; fi
fi
python3 "$LLM_SCRIPTS/patch_compare.py" "$COMPARE_TRACES_LOCAL" >/dev/null
docker cp "$COMPARE_TRACES_LOCAL"          "$CONTAINER:/tmp/compare-traces-official.py"
docker cp "$EVENTS_FILE"                   "$CONTAINER:/tmp/events_encoding_id.txt"

mkdir -p "$STEPS_OUT_DIR"

# STEP 9.5 — snapshot
echo "[step 9.5] snapshotting Flaky/ -> Flaky.pristine"
rm -rf "$DATA_DIR/Flaky.pristine"
cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Flaky.pristine"

echo "[step 9.5] Writing trace_config.json"
cat > "$STEPS_OUT_DIR/trace_config.json" <<JSONEOF
{
  "docker_container": "$CONTAINER",
  "test_type": "nio",
  "module": "$MODULE",
  "polluter": "",
  "victim": "$VICTIM",
  "nondex_seed": "",
  "nondex_runs": 0,
  "wrapper_fqcn": "$WRAPPER_FQCN",
  "surefire_version": "$SUREFIRE_VER",
  "tracemop_ready": true
}
JSONEOF

# AGENT — verify_victim for NIO needs WRAPPER_FQCN + SUREFIRE_VER in env;
# agentic_verify.py reads them, mirroring run_nio_tracemop.sh's verify_victim().
export WRAPPER_FQCN SUREFIRE_VER
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
echo "[AGENTIC NIO] Done."
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
