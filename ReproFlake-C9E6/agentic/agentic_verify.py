#!/usr/bin/env python3
"""
agentic_verify.py

Type-aware verification helper for the agentic pipeline. Runs the appropriate
post-patch surefire (or NonDex) command inside the running docker container,
captures stdout/stderr, parses the Surefire summary line, and writes:

    data/<container>/Steps_Output_Files/verify_after_fix.log
    data/<container>/Steps_Output_Files/verify_after_fix.verdict   (PASSED|FAILED)

Exits 0 on PASSED, 1 on FAILED. This mirrors the verify_victim() shell
functions in TraceMop Scripts/run_<type>_tracemop.sh so the agentic
orchestrator does not need per-type docker logic inlined.

Usage:
    python3 agentic_verify.py <result_container> [--docker-container NAME]

Requires:
    - The container `tm_<sanitized>` to already be running with the data dir
      bind-mounted, the JavaMOP extension built, and tracemop.jar installed.
    - For NIO: WRAPPER_FQCN env var (set by run_agentic_nio.sh; equal to the
      auto-generated wrapper class's FQN).
    - For ID:  NONDEXSEED and NONDEX_RUNS env vars (set by run_agentic_id.sh).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
LLM_SCRIPTS_DIR = REPROFLAKE_DIR / "LLM Scripts"
sys.path.insert(0, str(LLM_SCRIPTS_DIR))
from assemble_llm_context import DATA_DIR, load_csv_row  # type: ignore  # noqa: E402

# Surefire summary line emitted at the end of every test invocation. We sum
# Tests/Failures/Errors across all summary lines (NonDex emits N of them, one
# per iteration; surefire:test emits one). PASSED iff all aggregates show
# Tests>0, Failures=0, Errors=0, AND there are no <<< FAILURE!/<<< ERROR!
# markers anywhere in the log (defense in depth against unreliable summary
# lines).
SUMMARY_RE = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)")
MARKER_RE = re.compile(r"<<< (?:FAILURE|ERROR)!")

# Per-type Maven option set. Kept aligned with the shell scripts so test
# behaviour matches the non-agentic pipeline byte-for-byte where possible.
MVNOPTS_OD = ('-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip '
              '-Drat.skip -Denforcer.skip -Dmaven.javadoc.skip')
MVNOPTS_TD = MVNOPTS_OD
MVNOPTS_ID = (
    '-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false '
    '-Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip '
    '-Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip '
    '-Dmaven.javadoc.skip -Dfindbugs.skip -Dwarbucks.skip -Dmodernizer.skip '
    '-Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip '
    '-Dcobertura.skip=true -Dspotless.skip=true -Dspotless.check.skip=true '
    '-Dossindex.skip=true -Dmaven.bundle.plugin.skip=true '
    '-Dmaven.parallel.force=false')
# NIO MVNOPTS adds the additional skips that the NIO shell script uses; the
# extra flags are no-ops on projects that don't define the relevant plugins.
MVNOPTS_NIO = MVNOPTS_ID + ' -Dfindbugs.skip=true'

EXT_JAR = "/tmp/ext-build/target/javamop-extension-1.0.jar"


def _run_in_container(docker_container: str, command: str) -> str:
    """Execute `bash -c command` inside the running docker container and
    return combined stdout/stderr. Tolerates non-zero exit (we WANT the
    output of failing test runs)."""
    # Prepend the JDK Maven already uses to PATH so any build plugin that
    # spawns a bare `java`/tool during the verify build (codegen, antrun,
    # JavaCC/jute, protoc) can find it. Guarded: no-op if JAVA_HOME is unset,
    # and only prepends the in-use JDK — cannot break a subject that already
    # had java on PATH. Mirrors the same hardening in apply_fix recompile.
    command = 'export PATH="${JAVA_HOME:+$JAVA_HOME/bin:}$PATH"\n' + command
    proc = subprocess.run(
        ["docker", "exec", docker_container, "bash", "-c", command],
        capture_output=True, text=True,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def _build_command(test_type: str, row: dict) -> str:
    """Construct the in-container `mvn ... ` command for the given test
    type. The shell scripts' verify_victim() functions are the canonical
    source — keep this in lock-step with them.
    """
    module = (row.get("module") or ".").strip()
    victim = (row.get("flaky_test") or "").strip()
    polluter = (row.get("polluter/state setter") or "").strip()
    timeout = "-Dsurefire.timeout=180"

    if test_type == "od":
        # Pair-of-tests with -Dsurefire.runOrder=testorder (TestingResearch-
        # Illinois fork), pinned via SUREFIRE_VERSION.
        # NOTE: deliberately NO `-Dmaven.ext.class.path={EXT_JAR}` here, for the
        # same reason as the TD/ID branches. The OD gate only needs to rerun the
        # polluter->victim pair in declared order and check the victim now
        # passes; it does NOT need JavaMOP/TraceMOP instrumentation. Worse, the
        # ext jar weaves AspectJ monitors whose static initializer opens
        # .traces/traces.txt, which does not exist in the standalone verify
        # working dir -> the monitor throws FileNotFoundException inside class
        # init before the victim assertion ever runs, so verify reports FAILED
        # regardless of the patch (the verify log is then identical across all
        # attempts). This also makes the gate use the same un-instrumented
        # command that originally reproduced the flake. Trace collection still
        # uses the ext jar in _compute_rv_traces_lazy.
        # Run `dependency:properties` before the standalone surefire:test goal.
        # Some poms (e.g. ZooKeeper) put `-javaagent:${groupId:artifactId:type}`
        # in surefire's argLine; that property is published by the
        # maven-dependency-plugin:properties goal, which is bound into the
        # lifecycle and so is SKIPPED when surefire:test is invoked directly.
        # Without it the placeholder stays literal, the forked test JVM fails to
        # start ("forked VM terminated without properly saying goodbye"), and
        # verify reports Tests run: 0 regardless of the patch. It is a harmless
        # no-op for projects whose argLine references no such property.
        return (
            "cd /app/work/Flaky\n"
            "export SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT\n"
            f"mvn dependency:properties surefire:test "
            f"-pl {module} -Dtest='{polluter},{victim}' "
            f"-Dsurefire.runOrder=testorder {timeout} {MVNOPTS_OD} 2>&1"
        )
    if test_type == "td":
        # NOTE: deliberately NO `-Dmaven.ext.class.path={EXT_JAR}` here, for
        # the same reason as the ID branch below. TD verify just reruns the
        # failing test and checks if it now passes; it doesn't need JavaMOP/
        # TraceMOP instrumentation. Passing the ext jar perturbs the effective
        # surefire version, which breaks on projects whose poms pin an older
        # surefire (e.g. HBASE-27051's pom uses 3.0.0-M6 -> ext jar bumps to
        # 3.1.2 -> NoClassDefFoundError on
        # org.apache.maven.surefire.api.util.TempFileManager -> "Tests run: 0"
        # -> _interpret() defaults to FAILED even when the agent's fix is
        # correct). Trace collection still uses the ext jar in
        # _compute_rv_traces_lazy.
        # `dependency:properties` first — same reason as the OD branch: it
        # publishes the dependency-path properties that some poms reference in
        # surefire's argLine (e.g. -javaagent:${org.jmockit:jmockit:jar}), which
        # the standalone surefire:test goal would otherwise leave unresolved,
        # crashing the forked test JVM (Tests run: 0). No-op when unused.
        return (
            "cd /app/work/Flaky\n"
            f"mvn dependency:properties surefire:test "
            f"-pl {module} -Dtest='{victim}' {timeout} {MVNOPTS_TD} 2>&1"
        )
    if test_type == "id":
        # NonDex iteration verify. Reads NONDEXSEED + NONDEX_RUNS from env
        # — populated by run_agentic_id.sh from the CSV row.
        seed = os.environ.get("NONDEXSEED", "").strip()
        runs = os.environ.get("NONDEX_RUNS", "").strip()
        if not seed or not runs:
            sys.exit("ERROR: ID verify requires NONDEXSEED + NONDEX_RUNS env "
                     "vars set by the per-type orchestrator.")
        # NOTE: deliberately NO `-Dmaven.ext.class.path={EXT_JAR}` here.
        # NonDex only needs to shuffle iteration orders and run surefire; it
        # doesn't need the JavaMOP/TraceMOP instrumentation. Passing the ext
        # jar perturbs the effective surefire version, which breaks on projects
        # whose poms use parameters newer surefire removed (e.g. dubbo's
        # <forkMode> -> "Cannot find 'forkMode'" -> 0 surefire summary lines
        # -> _interpret() defaults to FAILED even when no test actually failed).
        # Trace collection still uses the ext jar in _compute_rv_traces_lazy.
        nondex_plugin_version = os.environ.get(
            "NONDEX_PLUGIN_VERSION", "2.1.1").strip() or "2.1.1"
        return (
            "cd /app/work/Flaky\n"
            f"mvn edu.illinois:nondex-maven-plugin:{nondex_plugin_version}:nondex "
            f"-DnondexSeed={seed} -DnondexRuns={runs} "
            f"-pl '{module}' "
            f"-Dtest='{victim}' {timeout} {MVNOPTS_ID} 2>&1"
        )
    if test_type == "nio":
        # NIO wrapper-class verify. WRAPPER_FQCN is generated and stashed in
        # the env by run_agentic_nio.sh.
        wrapper = os.environ.get("WRAPPER_FQCN", "").strip()
        if not wrapper:
            sys.exit("ERROR: NIO verify requires WRAPPER_FQCN env var (set "
                     "by run_agentic_nio.sh after wrapper generation).")
        surefire_ver = os.environ.get("SUREFIRE_VER", "3.0.0-M5").strip()
        # NOTE: deliberately NO `-Dmaven.ext.class.path={EXT_JAR}` here, for the
        # same reason as the OD/TD/ID branches. The NIO gate only needs to run
        # the wrapper's runTwice() and check the second invocation passes; it
        # does NOT need JavaMOP/TraceMOP instrumentation, which is for trace
        # collection only and can crash the standalone verify run (missing
        # .traces dir) or perturb the effective surefire version. Keeps all
        # four verify branches instrumentation-free and consistent. Trace
        # collection still uses the ext jar in _compute_rv_traces_lazy.
        return (
            "cd /app/work/Flaky\n"
            f"export SUREFIRE_VERSION={surefire_ver}\n"
            f"mvn test -pl {module} -am "
            f"-Dtest='{wrapper}#runTwice' {timeout} {MVNOPTS_NIO} 2>&1"
        )
    if test_type in ("unclassified", "unassigned"):
        return (
            "cd /app/work/Flaky\n"
            f"mvn test -pl '{module}' -Dtest='{victim}' "
            f"{timeout} {MVNOPTS_TD} 2>&1"
        )
    sys.exit(f"ERROR: unsupported test_type '{test_type}' for agentic verify.")


def _interpret(log_text: str) -> tuple[str, dict]:
    """Aggregate Tests/Failures/Errors across all summary lines in the log
    and report a single verdict. Same logic as the shell verify_victim
    functions: Tests>0, Failures=0, Errors=0 across every iteration AND
    zero per-test FAILURE/ERROR markers."""
    tests = fails = errs = 0
    n_summaries = 0
    for m in SUMMARY_RE.finditer(log_text):
        n_summaries += 1
        tests += int(m.group(1))
        fails += int(m.group(2))
        errs += int(m.group(3))
    markers = len(MARKER_RE.findall(log_text))
    # A clean Maven run ends in BUILD SUCCESS. A "BUILD FAILURE" with no per-test
    # failure markers means the run aborted for a non-test reason (NonDex bailing
    # out mid-reproduction, a plugin/infra error); the iterations that *did* run
    # may all show passing summaries, so this guard prevents a fabricated PASS.
    # (Verified against all existing PASSED logs: none contain BUILD FAILURE, so
    # this can only ever turn a spurious PASS into FAILED — never flip a result.)
    build_failure = "BUILD FAILURE" in log_text
    verdict = "FAILED"
    if (n_summaries > 0 and tests > 0 and fails == 0 and errs == 0
            and markers == 0 and not build_failure):
        verdict = "PASSED"
    return verdict, {
        "summary_lines": n_summaries,
        "tests": tests,
        "failures": fails,
        "errors": errs,
        "failure_markers": markers,
        "build_failure": build_failure,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("container")
    ap.add_argument("--docker-container",
                    help="docker container name (default: tm_<container_sanitized>)")
    args = ap.parse_args()

    row = load_csv_row(args.container)
    if not row:
        sys.exit(f"ERROR: container '{args.container}' not in test_config.csv")
    test_type = (row.get("test_type") or "").strip().lower()
    if test_type not in {"od", "td", "id", "nio", "unclassified", "unassigned"}:
        sys.exit(f"ERROR: unsupported test_type '{test_type}'")

    docker_container = args.docker_container or (
        "tm_" + re.sub(r"[^a-zA-Z0-9]", "_", args.container))

    base = Path(DATA_DIR) / args.container
    steps_dir = base / "Steps_Output_Files"
    steps_dir.mkdir(parents=True, exist_ok=True)
    log_path = steps_dir / "verify_after_fix.log"
    verdict_path = steps_dir / "verify_after_fix.verdict"

    cmd = _build_command(test_type, row)
    print(f"[verify] test_type={test_type}  container={docker_container}")
    log_text = _run_in_container(docker_container, cmd)
    log_path.write_text(log_text, encoding="utf-8")

    verdict, stats = _interpret(log_text)
    verdict_path.write_text(verdict + "\n", encoding="utf-8")

    print(f"[verify] summary lines={stats['summary_lines']}  "
          f"Tests={stats['tests']}  Failures={stats['failures']}  "
          f"Errors={stats['errors']}  markers={stats['failure_markers']}  "
          f"build_failure={stats['build_failure']}")
    print(f"[verify] verdict: {verdict}")
    sys.exit(0 if verdict == "PASSED" else 1)


if __name__ == "__main__":
    main()
