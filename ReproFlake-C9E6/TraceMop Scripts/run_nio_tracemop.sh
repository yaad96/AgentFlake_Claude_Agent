#!/usr/bin/env bash
# ============================================================
# run_nio_tracemop.sh — END-TO-END pipeline for NIO flaky tests
#
# Same shape as run_td_tracemop.sh / run_od_tracemop.sh / run_id_tracemop.sh,
# with NIO-specific differences:
#
#   1. Test type must be 'nio'.
#   2. NIO definition (Wei et al., ICSE 2022 — "Preempting Flaky Tests via
#      Non-Idempotent-Outcome Tests"): a test T passes when run alone, but
#      fails when run a second time in the same JVM, because T self-pollutes
#      shared state (typically a static field) that its second invocation
#      then reads. The fix is canonically a cleanup line at the end of T
#      that resets the polluted state (e.g., `Foo.iterations = 0;` or
#      `Foo.values.clear();`).
#   3. Variants: BOTH Fixed/ (deterministic pass when run twice) and Flaky/
#      (deterministic fail when run twice — second invocation triggers the
#      NIO assertion) are materialised. No polluter; the test IS its own
#      polluter on its second invocation.
#   4. Image: flaky_base_jdk{8,11,17}, no NIO-specific image. The vendored
#      testrunner/iDFlakies trees in Flaky/ are NOT used by this pipeline
#      (they don't compose with TraceMOP — the JavaMOP extension hooks
#      Surefire's argLine, not testrunner's; verified empirically 2026-04-30).
#
#   5. NIO REPRODUCER (the load-bearing design choice):
#      We generate a tiny JUnit4 wrapper class at
#         <module>/src/test/java/<victim_pkg>/<MethodCap>NioReproTest.java
#      that uses JUnitCore.run(Request.method(...)) to execute the victim
#      twice, in a single forked JVM, with full @Before/@After/@Rule
#      lifecycle each time. The wrapper asserts BOTH runs pass; if the
#      second run fails (NIO!), the wrapper test fails. We then run the
#      wrapper via plain `mvn test` — TraceMOP attaches normally.
#
#      Why a wrapper instead of `mvn testrunner:testplugin idempotent x2`
#      (the gold-standard NIO reproducer used in nio_statistics_generator.sh)?
#      Probed 2026-04-30: testrunner:testplugin runs the test twice and
#      reproduces the NIO failure deterministically (JSON: run-0 PASS,
#      run-1 ERROR), but the JavaMOP extension does NOT inject -javaagent
#      into testrunner's forked JVM. Result: BUILD SUCCESS on Maven side
#      with the JSON reporting NIO, but ZERO traces and zero [TraceMOP]
#      log lines. The wrapper is the only mechanism we found that gives us
#      BOTH a deterministic NIO failure AND working TraceMOP attachment.
#
#   6. mvn invocation: `mvn test` (NOT `mvn surefire:test`) because
#      Flaky/pom.xml has been mutated by modify_pom_for_coverage.sh to
#      reference ${argLine}. Without the `initialize` lifecycle phase,
#      ${argLine} stays unresolved and gets handed to `java` as a literal
#      class name (`Could not find or load main class ${argLine}`). Same
#      trap as ID — same solution.
#
#      We also pin SUREFIRE_VERSION to whatever the project's pom.xml
#      declares (parsed at runtime; default 3.0.0-M5). The JavaMOP extension
#      otherwise upgrades Surefire to 3.1.2, which mixes incompatibly with
#      the project's pinned 3.0.0-M5 dependencies and produces:
#         NoSuchMethodError: RunOrderParameters.<init>(String, File, Long)
#      Verified empirically 2026-04-30.
#
#   7. Trace pair (step 4d):
#        traces-fixed/  — Fixed/ + wrapper (both invocations pass — clean
#                          baseline, the test is idempotent thanks to the
#                          cleanup line in Fixed.patch)
#        traces-flaky/  — Flaky/ + wrapper (first invocation passes, second
#                          fails with the NIO assertion)
#      Diffing traces-flaky against traces-fixed surfaces the runtime
#      events that differ between the idempotent and non-idempotent versions.
#
#      EMPIRICAL CAVEAT: TraceMOP records control-flow events (collection
#      ops, iterator usage), not data-only writes. NIO failures driven by
#      static collection pollution (e.g., `static List` accumulation,
#      fixed by `.clear()`) DO produce non-empty trace diffs. NIO failures
#      driven by static primitive pollution (e.g., `static int counter`
#      accumulation, fixed by `= 0;`) produce EMPTY trace diffs because
#      neither variant differs in monitored event sequences. This is a
#      structural property of trace-diff for NIO, not a pipeline bug —
#      see probe v5/v6 results in data/quickcheckc1c1/probe/.
#
#   8. Step 11 (verify): re-run the SAME wrapper against the patched Flaky/
#      and assert it now passes both invocations (Tests=1, Failures=0,
#      Errors=0). If the LLM's fix correctly resets the polluted state,
#      the wrapper's two runs both succeed; otherwise the second run still
#      fails and the verdict is FAILED.
#
# Usage:
#   ./run_nio_tracemop.sh <result_container> <claude|openai>
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
#   4c. generate the NIO wrapper class in BOTH Fixed/ and Flaky/
#   4d. run mvn test with TraceMOP on Fixed/+wrapper then Flaky/+wrapper
#       (both with the JavaMOP extension + SUREFIRE_VERSION=<project's value>)
#   sanity. Verify Flaky+wrapper failed AND Fixed+wrapper passed.
#   5.  prepare trace-comparison tooling
#   6.  compare-traces-official.py traces-flaky traces-fixed -> step_8_C_official.txt
#   7.  generate_llm_summary.py          -> llm_trace_summary.txt
#   8.  assemble_llm_context_nio.py      -> llm_context.txt   (must exist;
#                                            mirror assemble_llm_context_id.py
#                                            but with NIO-specific framing —
#                                            "this test self-pollutes via static
#                                            state; identify the field and add
#                                            cleanup at the end of the method")
#   9.  call_llm.py (dispatches to claude or openai) -> llm_response.json
#   10. apply_fix.py                     -> patches Flaky/ + recompiles bytecode
#   11. re-run wrapper against patched Flaky/ -> verify_after_fix.log
#
# Container is left running for iteration.
# ============================================================

set -euo pipefail

# ----- args -------------------------------------------------
RESULT_CONTAINER="${1:?Usage: $0 <result_container> <claude|openai>   (e.g. quickcheckc1c72 claude)}"
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

if [[ "$TEST_TYPE" != "nio" ]]; then
  echo "ERROR: this script targets nio only; got '$TEST_TYPE'."
  echo "       Use run_td/od/id_tracemop.sh for those categories."
  exit 1
fi

if [[ -z "$VICTIM" ]]; then
  echo "ERROR: NIO container '$RESULT_CONTAINER' must have a victim test in CSV."
  exit 1
fi

# ----- derive victim/wrapper identifiers --------------------
# VICTIM is "pkg.SubPkg.ClassName#methodName"
VICTIM_CLASS_FULL="${VICTIM%#*}"            # com.pholser.junit.quickcheck.ShrinkingTest
VICTIM_METHOD="${VICTIM##*#}"               # disablingShrinking
VICTIM_CLASS_SIMPLE="${VICTIM_CLASS_FULL##*.}"   # ShrinkingTest
VICTIM_PKG="${VICTIM_CLASS_FULL%.*}"        # com.pholser.junit.quickcheck
# Use `tr` instead of bash parameter substitution `${VICTIM_PKG//./\/}` —
# the latter emits literal `\/` on bash 3.2 (Apple-shipped /bin/bash on macOS),
# which materialized real `com\/pholser\/...` directories next to the legit
# package tree. Silent for execution (javac follows the package declaration)
# but pollutes the source tree and breaks any tool that maps source paths
# back to package names.
VICTIM_PKG_PATH="$(echo "$VICTIM_PKG" | tr '.' '/')"   # com/pholser/junit/quickcheck

# Wrapper class name: capitalize first letter of the method, append NioReproTest.
# Two NIO containers in the same Flaky/ tree would never collide because each
# container's Flaky/ is destroyed and recreated by the cleanup at end-of-run.
METHOD_CAP="$(printf '%s' "${VICTIM_METHOD:0:1}" | tr '[:lower:]' '[:upper:]')${VICTIM_METHOD:1}"
WRAPPER_CLASS_SIMPLE="${METHOD_CAP}NioReproTest"
WRAPPER_FQCN="${VICTIM_PKG}.${WRAPPER_CLASS_SIMPLE}"
WRAPPER_PATH_REL="${MODULE}/src/test/java/${VICTIM_PKG_PATH}/${WRAPPER_CLASS_SIMPLE}.java"

# ----- image selection --------------------------------------
case "$JAVA" in
  8)  IMAGE="flaky_base_jdk8" ;;
  11) IMAGE="flaky_base_jdk11" ;;
  17) IMAGE="flaky_base_jdk17" ;;
  *)  echo "ERROR: NIO with java=$JAVA is not supported by this pipeline."; exit 1 ;;
esac

# Auto-build the JDK 8 base image if missing (the only TD/NIO JDK with a
# Dockerfile in the repo — the bare `Dockerfile`). JDK 11/17 images must be
# preinstalled; if missing for those, exit with a clear error.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  if [[ "$JAVA" == "8" ]]; then
    echo "[setup] Docker image '$IMAGE' not found — building from Dockerfile"
    echo "[setup] (one-time setup, takes a few minutes)"
    docker build -t "$IMAGE" -f "$REPROFLAKE_DIR/Dockerfile" "$REPROFLAKE_DIR"
    echo "[setup] image '$IMAGE' built successfully"
  else
    echo "ERROR: required Docker image '$IMAGE' was not found locally."
    echo "       (No Dockerfile for JDK $JAVA in this repo — supply the image manually.)"
    exit 1
  fi
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
victim class     : $VICTIM_CLASS_FULL
victim method    : $VICTIM_METHOD
wrapper class    : $WRAPPER_FQCN
wrapper path     : $WRAPPER_PATH_REL
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
# run lets you inspect the post-patch Flaky/ (including the auto-generated
# wrapper class), the apply-stage javac errors, the surefire-reports/, etc.
# — invaluable for debugging an LLM patch that compiled but didn't actually
# fix the NIO bug.
#
# Set KEEP_SOURCE=1 to skip this cleanup (e.g., to resume a partial run
# without redoing step 1's unzip + patch).
#
# KEPT (across runs): Steps Output Files/, traces-*/, Fixed.patch +
#                     flaky_info.txt, original .zip.
# REMOVED (every run, unless KEEP_SOURCE=1): Fixed/, Flaky/, FlakyCodeChange/,
#                     Flakym2/, result/. Step 1 re-materialises them from
#                     the zip + patches; step 4c re-generates the wrapper.
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
# STEP 1 — lean materialisation: unzip + apply Fixed.patch
# (no FlakyCodeChange/ for NIO — Fixed/ is the idempotent-fix variant,
#  Flaky/ is the original NIO-flaky version)
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

# ----- detect surefire version pinned by the project --------
# Parse the surefire-plugin <version> from Flaky/pom.xml. If any of the parsing
# fails or the value is empty, fall back to 3.0.0-M5 (the value in this dataset).
#
# Multi-module projects (e.g., shardingsphere/elasticjob) pin surefire via a
# property reference like <version>${maven-surefire-plugin.version}</version>
# rather than a literal. The resolution loop below walks the pom hierarchy's
# <properties> blocks for the named property; without it, the literal
# "${maven-surefire-plugin.version}" string would be passed to JavaMOPExtension
# and Maven would try to download `maven-surefire-plugin-${...}.jar`, which
# fails the build before any tests run (observed on elasticjob294 — May 2026).
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

# Resolve up to 3 levels of ${prop} indirection (e.g., ${a} -> ${b} -> literal).
# Search every pom.xml under Flaky/ for the property's <prop>VALUE</prop>
# definition; take the first match. Maven's property inheritance means a single
# definition anywhere in the hierarchy resolves the reference. Cap at 3 to
# bound any chain — Maven itself permits longer chains, but in practice flaky
# project poms don't go deeper.
PROP_RX='^\$\{(.+)\}$'
for _ in 1 2 3; do
  [[ "$SUREFIRE_VER" =~ $PROP_RX ]] || break
  prop_name="${BASH_REMATCH[1]}"
  echo "[step 1c] surefire version is property reference \${$prop_name} — resolving from pom hierarchy"
  # Escape dots in the property name so sed treats them literally; Maven
  # property names commonly contain `.` (e.g., `maven-surefire-plugin.version`).
  esc_prop="${prop_name//./\\.}"
  resolved=$(find "$DATA_DIR/Flaky" -maxdepth 8 -name pom.xml -print0 2>/dev/null \
    | xargs -0 grep -h "<$prop_name>" 2>/dev/null \
    | sed -nE "s|.*<${esc_prop}>([^<]+)</${esc_prop}>.*|\1|p" \
    | head -n 1 \
    | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')
  if [[ -z "$resolved" ]]; then
    echo "[step 1c] WARNING: could not resolve property \${$prop_name} in any pom.xml under Flaky/"
    SUREFIRE_VER=""
    break
  fi
  echo "[step 1c] Resolved \${$prop_name} = $resolved"
  SUREFIRE_VER="$resolved"
done

if [[ -z "$SUREFIRE_VER" ]]; then
  echo "[step 1c] WARNING: could not parse surefire-plugin version from Flaky/pom.xml — defaulting to 3.0.0-M5"
  SUREFIRE_VER="3.0.0-M5"
else
  echo "[step 1c] Detected surefire version pinned by project: $SUREFIRE_VER"
fi

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
# STEP 4c — Generate the NIO wrapper class in BOTH Fixed/ and Flaky/
#
# The wrapper uses JUnitCore + Request.method(...) to invoke the victim
# twice in the same JVM, going through the FULL JUnit lifecycle each time
# (so @Before/@After/@Rule are honoured). It then asserts both runs were
# successful — if either run had a failure or error, the wrapper test
# itself fails and surefire reports a Failures count >= 1.
#
# Why JUnitCore.run(Request.method(...)) instead of `new VictimClass().method()`?
# Because direct invocation skips JUnit's @Before/@After/@Rule machinery.
# Some NIO victims rely on that lifecycle for their setup/teardown semantics.
# Using JUnitCore mirrors how the gold-standard testrunner:testplugin
# reproducer drives the test, while staying compatible with TraceMOP
# attachment (which testrunner is not).
#
# AUTO-GENERATED note in the wrapper itself: the LLM context script
# (assemble_llm_context_nio.py) must NOT include the wrapper in the LLM
# context, and apply_fix.py must NOT target it — the fix site is the
# victim's source file, not the wrapper.
# ============================================================
echo "[step 4c] Generating NIO wrapper class at $WRAPPER_PATH_REL"

gen_wrapper() {
  local root="$1"
  local out="$root/$WRAPPER_PATH_REL"
  mkdir -p "$(dirname "$out")"
  cat > "$out" <<EOF
package ${VICTIM_PKG};

// AUTO-GENERATED by run_nio_tracemop.sh — DO NOT EDIT.
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

# ============================================================
# STEP 4d — Run TraceMOP on each codebase variant with the wrapper
#
# Final approach (verified empirically 2026-04-30 across probes 1-6):
#
#   - The JavaMOP extension is the only mechanism that reliably attaches
#     TraceMOP to Surefire's forked JVM. Plain `-DargLine='-javaagent:'`
#     produces empty traces.
#   - The extension would normally upgrade Surefire to 3.1.2, which collides
#     with this dataset's pinned 3.0.0-M5 dependencies (NoSuchMethodError on
#     RunOrderParameters.<init>). We pin SUREFIRE_VERSION=$SUREFIRE_VER (the
#     project's own declared version) so the extension keeps Surefire on the
#     compatible release.
#   - We use `mvn test` (not `mvn surefire:test`) because the Flaky/pom.xml
#     has been mutated by modify_pom_for_coverage.sh to reference ${argLine}.
#     `surefire:test` skips the `initialize` phase, so ${argLine} stays
#     unresolved and gets passed to `java` as a literal class name (boom).
# ============================================================
EXT_JAR=/tmp/ext-build/target/javamop-extension-1.0.jar

# Maven flags lifted from the existing OD/ID scripts (silence side-plugins so
# surefire's exit is the only meaningful signal).
MVNOPTS='-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false -Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip -Dmaven.javadoc.skip -Dwarbucks.skip -Dmodernizer.skip -Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip -Dcobertura.skip=true -Dfindbugs.skip=true -Dspotless.skip=true -Dspotless.check.skip=true -Dossindex.skip=true -Dmaven.bundle.plugin.skip=true -Dmaven.parallel.force=false'

run_with_tracemop() {
  local variant="$1"   # "Fixed" or "Flaky"
  local label="$2"     # "fixed"  or "flaky"

  echo "[step 4d] /app/work/$variant + wrapper  ->  /app/work/traces-$label"

  docker exec "$CONTAINER" bash -c "
    set -e
    rm -rf /app/work/traces-$label
    mkdir -p /app/work/traces-$label

    export TRACEDB_CONFIG_PATH=/tmp/.trace-db.config
    printf 'db=memory\ndumpDB=false\n' > \$TRACEDB_CONFIG_PATH
    export RVMLOGGINGLEVEL=UNIQUE
    export TRACEDB_PATH=/app/work/traces-$label

    # Pin Surefire to whatever the project declared — see header comment.
    export SUREFIRE_VERSION=$SUREFIRE_VER

    cd /app/work/$variant

    echo '--- pre-build: mvn install -DskipTests -pl $MODULE -am ---'
    mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS

    echo '--- mvn test with JavaMOP extension + Surefire $SUREFIRE_VER + wrapper ---'
    # Tolerate non-zero exit (Flaky variant will fail by design).
    mvn test \
      -Dmaven.ext.class.path=$EXT_JAR \
      -pl $MODULE \
      -am \
      -Dtest='${WRAPPER_FQCN}#runTwice' \
      $MVNOPTS 2>&1 | tee /app/work/traces-$label/mvn.log || true
  "
}

run_with_tracemop "Fixed" "fixed"
run_with_tracemop "Flaky" "flaky"

# ============================================================
# SANITY CHECK — confirm Fixed+wrapper PASSED and Flaky+wrapper FAILED
#
# Without these guards, two silent failure modes pollute the LLM context:
#   (a) Fixed+wrapper unexpectedly fails -> our pipeline is broken;
#       Fixed.patch may be corrupt, the wrapper may not compile, etc.
#   (b) Flaky+wrapper unexpectedly passes -> the test isn't actually NIO
#       under our reproducer (e.g., the pollution is reset by something
#       else, or the @Before/@After does the cleanup). Either way, the
#       trace diff is meaningless.
# ============================================================
parse_summary() {
  # Print "TESTS FAILURES ERRORS" from the LAST surefire summary in the log.
  local log="$1"
  local sum
  sum=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
          "$log" 2>/dev/null | tail -1 || true)
  if [[ -z "$sum" ]]; then echo "0 0 0"; return; fi
  local t f e
  t=$(sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p'   <<<"$sum"); t=${t:-0}
  f=$(sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'    <<<"$sum"); f=${f:-0}
  e=$(sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'      <<<"$sum"); e=${e:-0}
  echo "$t $f $e"
}

echo "[sanity ] Verifying Fixed+wrapper PASSED"
read -r FT FF FE <<< "$(parse_summary "$DATA_DIR/traces-fixed/mvn.log")"
echo "[sanity ] Fixed+wrapper:  Tests=$FT Failures=$FF Errors=$FE"
if (( FT < 1 )); then
  echo "ERROR: Fixed+wrapper executed 0 tests. The wrapper may not have compiled,"
  echo "       or the -Dtest= filter didn't match. Inspect $DATA_DIR/traces-fixed/mvn.log."
  exit 1
fi
if (( FF + FE >= 1 )); then
  echo "ERROR: Fixed+wrapper had failures/errors — the idempotent variant is supposed"
  echo "       to pass both invocations. Either Fixed.patch did not actually fix the"
  echo "       NIO bug, or the wrapper-twice driver invokes the test in a way that"
  echo "       the cleanup line doesn't cover. Inspect $DATA_DIR/traces-fixed/mvn.log"
  echo "       and the assertion stack trace before continuing."
  exit 1
fi

echo "[sanity ] Verifying Flaky+wrapper FAILED (the NIO failure)"
read -r KT KF KE <<< "$(parse_summary "$DATA_DIR/traces-flaky/mvn.log")"
echo "[sanity ] Flaky+wrapper:  Tests=$KT Failures=$KF Errors=$KE"
if (( KT < 1 )); then
  echo "ERROR: Flaky+wrapper executed 0 tests. The wrapper may not have compiled,"
  echo "       or the -Dtest= filter didn't match. Inspect $DATA_DIR/traces-flaky/mvn.log."
  exit 1
fi
if (( KF + KE < 1 )); then
  echo "ERROR: Flaky+wrapper passed unexpectedly. The test is NOT exhibiting NIO"
  echo "       behaviour under our wrapper-twice driver. Possible causes:"
  echo "         (a) the test's @Before/@After resets the polluted state between"
  echo "             invocations (so the second run starts clean);"
  echo "         (b) the static field is per-class-loader and JUnitCore.run uses"
  echo "             a fresh class loader for each Request (uncommon but possible);"
  echo "         (c) the CSV's victim/method spec doesn't match an actually-NIO test."
  echo "       Inspect $DATA_DIR/traces-flaky/mvn.log."
  exit 1
fi
echo "[sanity ] OK — Fixed passed, Flaky failed (NIO reproduced)."

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

mkdir -p "$STEPS_OUT_DIR"
STEPS_REL="data/$RESULT_CONTAINER/Steps Output Files"

# ============================================================
# STEP 6 — Full trace comparison
# (actual=flaky, expected=fixed → "only in actual" = flaky-unique)
#
# CAVEAT (informational): for NIO victims whose pollution is via a static
# primitive (e.g., `static int counter`), this diff will be empty because
# TraceMOP's monitored event set doesn't include arbitrary static-field
# writes. For NIO victims polluting via a static collection (e.g.,
# `static List`, fixed by `.clear()`), the diff is non-empty. See the
# header comment.
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
# STEP 8 — Assemble LLM context (NIO-specific)
#
# This script must exist. Mirror assemble_llm_context_id.py with NIO framing:
#   - State the NIO definition (test self-pollutes static state across runs).
#   - Show the victim's source code (the test method + any static fields it touches).
#   - Show the wrapper class verbatim (to make the failure mode concrete) AND
#     instruct the LLM that the wrapper is the driver, not the fix target.
#   - Show the assertion stack trace from traces-flaky/mvn.log.
#   - Show the trace diff (may be empty for primitive-pollution NIO — that's OK).
#   - Ask for a fix to the VICTIM's source: a cleanup line at the end of the
#     test method that resets the polluted static state.
# ============================================================
# Pick the assembler variant based on the RV-traces ablation switch set by
# run_pass_at_k.py. ${RV_TRACES:-yes} preserves the historical behavior
# (include the RV TRACE ANALYSIS section) for any direct caller that doesn't
# set the var.
ASSEMBLER_VARIANT="rv"
[[ "${RV_TRACES:-yes}" == "no" ]] && ASSEMBLER_VARIANT="no_rv"
echo "[step 8 ] $ASSEMBLER_VARIANT/assemble_llm_context_nio.py -> $STEPS_REL/llm_context.txt"
( cd "$LLM_SCRIPTS_DIR" && python3 "$ASSEMBLER_VARIANT/assemble_llm_context_nio.py" "$RESULT_CONTAINER" ) >/dev/null

# ============================================================
# STEP 9 — Call LLM
# ============================================================
echo "[step 9 ] call_llm.py ($LLM_BACKEND)  -> $STEPS_REL/llm_response.json"
( cd "$LLM_SCRIPTS_DIR" && python3 call_llm.py "$RESULT_CONTAINER" "$LLM_BACKEND" )

# ============================================================
# STEP 10 — Apply the LLM-proposed fix to Flaky/ + recompile bytecode
# (apply_fix.py handles the patch; recompile is done in-container so step 13
#  doesn't run stale .class files)
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
# STEP 11 — Verify the LLM fix actually removes the NIO failure
#
# Strict binary verdict (preserves the cross-script invariant):
#   PASSED iff step 12 landed AND the patched Flaky/ runs the wrapper with
#          Tests=1, Failures=0, Errors=0 (i.e., both invocations now pass).
#   FAILED in all other cases.
#
# Same surefire invocation as step 6c (extension + SUREFIRE_VERSION + wrapper)
# so the verification mirrors the diff-collection runs exactly.
# ============================================================
VERDICT="FAILED"
if (( STEP12_OK )); then
  VERIFY_LOG="$STEPS_OUT_DIR/verify_after_fix.log"
  echo "[step 11] Re-running wrapper '${WRAPPER_FQCN}#runTwice' against patched Flaky/  -> $STEPS_REL/verify_after_fix.log"

  # -Dsurefire.timeout=180 caps the forked test JVM at 3 minutes. The NIO
  # wrapper runs the victim twice in one JVM; under normal NIO repro, both
  # invocations together finish in well under a minute. A 3-minute cap
  # prevents LLM-patched tests with infinite-loop bugs from hanging the
  # wrapper indefinitely.
  docker exec "$CONTAINER" bash -c "
    cd /app/work/Flaky
    export SUREFIRE_VERSION=$SUREFIRE_VER
    mvn test \
      -Dmaven.ext.class.path=$EXT_JAR \
      -pl $MODULE \
      -am \
      -Dtest='${WRAPPER_FQCN}#runTwice' \
      -Dsurefire.timeout=180 \
      $MVNOPTS 2>&1
  " > "$VERIFY_LOG" 2>&1 || true

  read -r VTESTS VFAIL VERR <<< "$(parse_summary "$VERIFY_LOG")"
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
      VERDICT="FAILED"
    else
      VERDICT="PASSED"
    fi
  fi
  echo "[step 11] Tests=$VTESTS Failures=$VFAIL Errors=$VERR"
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
  echo "  Flaky/                 — LLM-patched source + <Method>NioReproTest wrapper"
  echo "  target/                — recompiled bytecode"
  echo "  surefire-reports/      — verify run output"
  echo "Remove when done: docker rm -f $CONTAINER"
else
  echo "Container '$CONTAINER' will be removed by the cleanup trap."
  echo "(Set KEEP_CONTAINER=1 next time to preserve it for inspection.)"
fi
echo "=========================================="
