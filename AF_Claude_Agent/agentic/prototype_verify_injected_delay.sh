#!/usr/bin/env bash
# =============================================================================
# prototype_verify_injected_delay.sh  (PROTOTYPE — TD timing flakes)
#
# Problem: for timing/timeout flakes (e.g. BOOKKEEPER-709/846) the victim does
# NOT fail when run in isolation on Flaky/ (the race/timeout window is never hit
# on a fast, idle machine), so verifying a fix on Flaky/ is a *hollow* PASS — the
# test would pass with or without any fix.
#
# Idea: the benchmark already ships a deterministic reproducer — FlakyCodeChange/
# is Flaky/ plus an injected `Thread.sleep(...)` that forces the race/timeout.
# So verify the candidate fix on the SLEEP-INJECTED tree instead: a fix that
# makes the victim pass *with the injected delay still present* is a real fix.
#
# Crucial detail: the injected sleep is in test SOURCE, so the tree must be
# test-COMPILED (`-DskipTests`, not `-Dmaven.test.skip`) for the delay to take
# effect. (The current step 4d uses -Dmaven.test.skip and so never recompiles
# the injected test — which is why it often captures an empty failure log.)
#
# Modes:
#   --calibrate            prove the gate discriminates:
#                            FlakyCodeChange (no fix)            -> expect FAILED
#                            FlakyCodeChange + ground-truth fix  -> expect PASSED
#   --patch <host_file>    apply a candidate patch (diff vs Flaky/) to the
#                          sleep-injected tree and report PASSED/FAILED.
#
# Usage: prototype_verify_injected_delay.sh <result_container> [--calibrate | --patch <file>]
# =============================================================================
set -uo pipefail

RC="${1:?Usage: $0 <result_container> [--calibrate | --patch <file>]}"
MODE="${2:---calibrate}"
PATCH_FILE="${3:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPROFLAKE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$REPROFLAKE_DIR/data/$RC"
CSV="$REPROFLAKE_DIR/test_config.csv"

ROW="$(awk -F',' -v rc="$RC" '$2==rc{print; exit}' "$CSV")"
[[ -n "$ROW" ]] || { echo "ERROR: $RC not in $CSV"; exit 1; }
ROW="${ROW%$'\r'}"
IFS=',' read -r TT _RC ZIP MODULE POLLUTER VICTIM ITER CONFIG JAVA NONDEX URL <<< "$ROW"
[[ "$TT" == "td" ]] || { echo "ERROR: prototype targets td only (got $TT)"; exit 1; }

case "$JAVA" in
  8)  IMAGE="flaky_base_jdk8" ;;
  11) IMAGE="flaky_base_jdk11" ;;
  17) IMAGE="flaky_base_jdk17" ;;
  *)  echo "ERROR: unsupported java=$JAVA"; exit 1 ;;
esac

PLAT=()
[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] && PLAT=(--platform linux/amd64)

MVNOPTS='-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Dmaven.javadoc.skip'
C="tm_proto_${RC//[^a-zA-Z0-9]/_}"
FCC=/app/work/FlakyCodeChange
VFILE="bookkeeper-server"   # only used in messages

echo "=========================================="
echo "[proto] container=$RC  victim=$VICTIM"
echo "[proto] module=$MODULE  java=$JAVA  image=$IMAGE  mode=$MODE"
echo "=========================================="

for d in Flaky FlakyCodeChange Fixed; do
  [[ -d "$DATA_DIR/$d" ]] || { echo "ERROR: $DATA_DIR/$d not staged — run the launcher once first"; exit 1; }
done

# ---- container ----
docker rm -f "$C" >/dev/null 2>&1 || true
mkdir -p "$DATA_DIR/Flakym2/.m2"
echo "[proto] starting throwaway container $C"
docker run -d "${PLAT[@]}" --name "$C" \
  --mount type=bind,source="$DATA_DIR",target=/app/work \
  --mount type=bind,source="$DATA_DIR/Flakym2/.m2",target=/root/.m2 \
  "$IMAGE" tail -f /dev/null >/dev/null
cleanup(){ [[ "${KEEP_CONTAINER:-0}" == "1" ]] || docker rm -f "$C" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# ---- build FlakyCodeChange WITH the injected sleep compiled (-DskipTests, not maven.test.skip) ----
echo "[proto] building FlakyCodeChange (-DskipTests so the injected sleep is COMPILED) ..."
docker exec "$C" bash -c "cd $FCC && mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS" \
  || { echo '[proto] FATAL: FlakyCodeChange build failed'; exit 1; }

# run_victim <tree> -> echoes PASSED|FAILED based on agentic_verify's rule
run_victim() {
  local tree="$1" log="/app/work/proto_victim.log"
  docker exec "$C" bash -c "cd $tree && mvn surefire:test -pl $MODULE -Dtest='$VICTIM' $MVNOPTS 2>&1" >/tmp/proto_out.txt 2>&1
  if grep -qE '<<< (FAILURE|ERROR)' /tmp/proto_out.txt; then echo FAILED; return; fi
  if grep -qE 'Tests run: [1-9][0-9]*, Failures: 0, Errors: 0' /tmp/proto_out.txt \
     && ! grep -qE 'Failures: [1-9]|Errors: [1-9]' /tmp/proto_out.txt; then echo PASSED; else echo FAILED; fi
}

# apply a patch (diff vs Flaky) onto the sleep-injected tree, then recompile tests
apply_and_compile() {
  local patch_host="$1"
  docker cp "$patch_host" "$C:/tmp/candidate.patch"
  docker exec "$C" bash -c "cd $FCC && patch -p1 --forward < /tmp/candidate.patch >/tmp/patch.log 2>&1 || true"
  docker exec "$C" bash -c "cd $FCC && mvn test-compile -pl $MODULE -q $MVNOPTS" \
    || { echo '[proto] FATAL: test-compile after patch failed'; return 1; }
}

if [[ "$MODE" == "--calibrate" ]]; then
  echo "[proto] >>> A) FlakyCodeChange, NO fix (sleep active) — expect FAILED"
  V_NOFIX="$(run_victim "$FCC")"
  echo "[proto]     verdict(no-fix) = $V_NOFIX"

  echo "[proto] >>> B) FlakyCodeChange + ground-truth Fixed.patch — expect PASSED"
  # snapshot the module's source so the staged tree is restored afterward
  # (generic — works regardless of which file the fix/sleep touches)
  docker exec "$C" bash -c "rm -rf /tmp/src_bak && cp -r $FCC/$MODULE/src /tmp/src_bak"
  apply_and_compile "$DATA_DIR/Fixed.patch"
  V_FIX="$(run_victim "$FCC")"
  echo "[proto]     verdict(ground-truth) = $V_FIX"
  docker exec "$C" bash -c "rm -rf $FCC/$MODULE/src && cp -r /tmp/src_bak $FCC/$MODULE/src"

  echo
  echo "=========================== CALIBRATION ==========================="
  printf "  %-42s %s\n" "FlakyCodeChange (sleep, NO fix)"        "$V_NOFIX   (want FAILED)"
  printf "  %-42s %s\n" "FlakyCodeChange (sleep + real fix)"     "$V_FIX   (want PASSED)"
  if [[ "$V_NOFIX" == "FAILED" && "$V_FIX" == "PASSED" ]]; then
    echo "  RESULT: gate DISCRIMINATES ✓  (meaningful, unlike bare Flaky/)"
  else
    echo "  RESULT: gate did NOT discriminate — needs inspection"
  fi
  echo "==================================================================="
elif [[ "$MODE" == "--patch" ]]; then
  [[ -f "$PATCH_FILE" ]] || { echo "ERROR: patch file '$PATCH_FILE' not found"; exit 1; }
  echo "[proto] applying candidate patch $PATCH_FILE onto the sleep-injected tree"
  apply_and_compile "$PATCH_FILE"
  V="$(run_victim "$FCC")"
  echo "[proto] VERDICT (candidate fix vs injected delay) = $V"
else
  echo "ERROR: unknown mode '$MODE' (use --calibrate or --patch <file>)"; exit 1
fi
