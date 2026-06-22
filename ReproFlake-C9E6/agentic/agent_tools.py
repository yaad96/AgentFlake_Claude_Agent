#!/usr/bin/env python3
"""
agent_tools.py

Tool implementations for the agentic flaky-test repair pipeline.

Tool surface (all return strings):
    get_test_code(test_name)        — source of the flaky test (victim + polluter for OD)
    get_code(target)                — production class header or method body by FQN
    get_error_logs(log_type)        — surefire/compile/verify logs verbatim
    get_flaky_example(category)     — category-specific successful-repair exemplar
    get_rv_trace_diff(test_name)    — TraceMOP trace-diff summary (computed lazily on first call)

TraceMOP traces are NOT pre-computed before the agent launches. When the agent
first calls get_rv_trace_diff, this module triggers the TraceMOP trace collection
inside the running docker container, then runs compare-traces and
generate_llm_summary to produce the summary. Subsequent calls return the cached
result. This is driven by trace_config.json written by the per-type shell script.

The terminal "submit_patch" action lives in agentic_orchestrator.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# We reuse the existing LLM Scripts helpers verbatim (read_file_safe,
# fqn_to_path, find_source_file, extract_java_method, extract_class_header,
# extract_failure_from_log, derive_project_package, load_csv_row). The
# agentic pipeline must produce the SAME shaped output (llm_response.json)
# that apply_fix.py consumes, so reusing the same extraction primitives
# keeps behaviour identical to the non-agentic pipeline's view of the source.
SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
LLM_SCRIPTS_DIR = REPROFLAKE_DIR / "LLM Scripts"
sys.path.insert(0, str(LLM_SCRIPTS_DIR))
from assemble_llm_context import (  # type: ignore  # noqa: E402
    DATA_DIR,
    load_csv_row,
    read_file_safe,
    fqn_to_path,
    find_source_file,
    extract_java_method,
    extract_class_header,
    extract_failure_from_log,
)

# Per-category example files live alongside this module.
EXAMPLES_DIR = SCRIPT_DIR / "flaky_examples"


# ---------------------------------------------------------------------------
# CSV row + path resolution
# ---------------------------------------------------------------------------

def _container_base(container: str) -> Path:
    return Path(DATA_DIR) / container


def _source_base(container: str, row: dict) -> Path:
    """Source code may live under <container>/Flaky/ OR under <zip>/Flaky/
    when many containers share a zip. Match the per-type assemblers.
    """
    base = _container_base(container)
    zip_name = (row.get("zip") or "").strip()
    if (base / "Flaky" / "src").is_dir():
        return base
    if zip_name and zip_name != container:
        alt = Path(DATA_DIR) / zip_name
        if (alt / "Flaky" / "src").is_dir():
            return alt
    return base


def _method_fallback_marker(reason: str) -> str:
    return f"({reason}. Use get_code with a more specific FQN if needed.)"


# ---------------------------------------------------------------------------
# Tool 1: get_test_code
# ---------------------------------------------------------------------------

def get_test_code(container: str, test_name: str | None = None) -> str:
    """Return source for the flaky-test container's relevant test methods.

    OD: returns both polluter and victim methods (the CSV pins both).
    TD/ID/NIO: returns the victim method only.

    If `test_name` is supplied, the helper still uses the CSV-known FQN for
    that role; passing a third-party test name is rejected with a clear
    marker rather than risk surfacing arbitrary unrelated code.
    """
    row = load_csv_row(container)
    if not row:
        return f"(container '{container}' not in test_config.csv)"
    source_base = _source_base(container, row)
    module = (row.get("module") or ".").strip()
    test_type = (row.get("test_type") or "").strip().lower()
    polluter_fqn = (row.get("polluter/state setter") or "").strip()
    victim_fqn = (row.get("flaky_test") or "").strip()

    # If the agent asked for a specific name, only honor it when it matches
    # the victim or the polluter — otherwise reject. (We intentionally don't
    # offer arbitrary test-source lookup here; that's get_code's job.)
    known = {n for n in (polluter_fqn, victim_fqn) if n}
    if test_name and test_name.strip() and test_name.strip() not in known:
        return (
            f"(test_name '{test_name}' is not the victim or polluter for this "
            f"container. Known names: {sorted(known) or 'none'}. Use get_code "
            f"with the FQN if you want a different class/method.)"
        )

    pieces: list[str] = []

    def _emit(label: str, fqn: str) -> None:
        rel_path, method = fqn_to_path(fqn)
        src_file = find_source_file(str(source_base), module, rel_path)
        pieces.append(f"=== {label}: {fqn} ===")
        if not src_file:
            pieces.append(f"(source file not found for {fqn})")
            pieces.append("")
            return
        pieces.append(f"File: {os.path.relpath(src_file, source_base)}")
        if method:
            body = extract_java_method(src_file, method)
            if body:
                pieces.append(f"Method: {method}")
                pieces.append("")
                pieces.append(body.rstrip())
            else:
                pieces.append(_method_fallback_marker(
                    f"could not extract method {method}"))
        else:
            pieces.append(_method_fallback_marker(
                "FQN has no #methodName component"))
        pieces.append("")

    if test_type == "od" and polluter_fqn:
        _emit("POLLUTER", polluter_fqn)
    if victim_fqn:
        _emit("VICTIM", victim_fqn)

    if not pieces:
        return f"(no test FQNs recorded for {container})"
    return "\n".join(pieces).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Tool 2: get_code — production class header or method
# ---------------------------------------------------------------------------

def get_code(container: str, target: str) -> str:
    """Return source for a class or method by FQN.

    Accepted targets:
      - 'com.foo.Bar'              -> structural class header (no method bodies)
      - 'com.foo.Bar#methodName'   -> annotations + signature + body of method

    Searches src/main/java first, then src/test/java. This is the agent's
    main vehicle for tracing state pollution into production code.
    """
    if not target or not target.strip():
        return "(get_code requires a non-empty target)"
    target = target.strip()

    row = load_csv_row(container)
    if not row:
        return f"(container '{container}' not in test_config.csv)"
    source_base = _source_base(container, row)
    module = (row.get("module") or ".").strip()

    rel_path, method = fqn_to_path(target)
    # src/main/java first (production), then fall back to src/test/java.
    src_file = find_source_file(
        str(source_base), module, rel_path,
        search_dirs=("src/main/java", "src/test/java"),
    )
    if not src_file:
        return f"(no source file found for {target} under Flaky/)"

    rel_src = os.path.relpath(src_file, source_base)
    if method:
        # all_overloads=True: a bare name often matches both a public wrapper
        # and a private overload holding the real logic; show every overload so
        # the agent isn't blind to the body (and assertions) it needs.
        body = extract_java_method(src_file, method, all_overloads=True)
        if body:
            return (
                f"File: {rel_src}\n"
                f"Method: {method}\n\n"
                f"{body.rstrip()}\n"
            )
        return (
            f"File: {rel_src}\n"
            f"{_method_fallback_marker(f'method {method} not found in file')}\n"
        )

    header = extract_class_header(src_file, include_inner_classes=False)
    if header is None:
        return f"(file not readable: {rel_src})"
    return (
        f"File: {rel_src}\n"
        f"(Structural view: package + imports + signatures + fields. "
        f"Method bodies elided — re-call get_code with '#methodName' for one.)\n\n"
        f"{header.rstrip()}\n"
    )


# ---------------------------------------------------------------------------
# Tool 3: get_error_logs
# ---------------------------------------------------------------------------

# The three log kinds the agent can pull. Anything else returns the list.
_LOG_KIND_HINT = (
    "Pass log_type='test_failure' for the original failing surefire run "
    "(the same log the initial prompt summarised), 'compile' for the most "
    "recent Maven recompile errors from the last patch attempt, or 'verify' "
    "for the most recent verify_after_fix.log from the last patch attempt."
)


def _read_first_existing(*paths: Path) -> tuple[str, Path | None]:
    """Return (content, path) for the first path that exists, else ("", None)."""
    for p in paths:
        if p.is_file():
            return read_file_safe(str(p)), p
    return "", None


def get_error_logs(container: str, log_type: str = "test_failure") -> str:
    """Return raw log content for diagnosis.

    log_type:
      'test_failure' — the failure extracted from traces-flakycc/mvn.log
                       (TD), traces-flaky/mvn.log (OD/NIO) or
                       traces-fail/mvn.log (ID). This is the same source
                       the initial prompt summarised; the agent can pull
                       the full block when it needs the stack trace.
      'compile'      — apply_report.json compile/recompile error tail from
                       the most recent submit_patch attempt.
      'verify'       — Steps_Output_Files/verify_after_fix.log tail from
                       the most recent submit_patch attempt.
    """
    log_type = (log_type or "").strip().lower()
    base = _container_base(container)
    row = load_csv_row(container)
    if not row:
        return f"(container '{container}' not in test_config.csv)"
    source_base = _source_base(container, row)
    steps = base / "Steps_Output_Files"

    if log_type == "test_failure":
        # Match the per-type assemblers' probe order so the agent gets the
        # same failure text the non-agentic pipeline would have inlined.
        for cand in ("traces-flakycc", "traces-flaky", "traces-fail",
                     "traces-fixed"):
            text = extract_failure_from_log(
                str(source_base / cand / "mvn.log"))
            if not text.startswith("("):
                return f"(from {cand}/mvn.log)\n\n{text}\n"
        return "(no failure block found in any traces-*/mvn.log)"

    if log_type == "compile":
        # apply_report.json carries the structured compile result; emit its
        # 'recompile' stderr/stdout tail. Fall back to the raw mvn output
        # the orchestrator dumped if apply_report.json is malformed.
        report_path = steps / "apply_report.json"
        if not report_path.is_file():
            return ("(no apply_report.json yet — submit a patch first; this "
                    "log is only populated after submit_patch runs)")
        import json
        try:
            rep = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return f"(apply_report.json malformed: {exc})"
        rc = rep.get("recompile") or {}
        if rc.get("skipped"):
            return "(recompile was skipped on the last attempt)"
        # Maven writes [ERROR]/BUILD FAILURE to stdout; stderr often carries
        # only benign subprocess warnings. Prefer stdout so the real compile
        # diagnostic surfaces instead of the warning.
        tail = rc.get("stdout_tail") or rc.get("stderr_tail") or ""
        if not tail:
            return "(no recompile output recorded on the last apply attempt)"
        ok = "ok" if rc.get("ok") else "failed"
        return f"(recompile {ok}; tail of mvn output:)\n\n{tail.rstrip()}\n"

    if log_type == "verify":
        log_path = steps / "verify_after_fix.log"
        verify_log, _ = _read_first_existing(log_path)
        if not verify_log:
            return ("(no verify_after_fix.log yet — submit a patch first; "
                    "this log is only populated after submit_patch runs)")
        # Prefer the same Surefire failure block (exception + message + stack
        # trace) used for the original failure, so the agent sees the post-patch
        # failure in the same shape. Fall back to the tail when no block is
        # found (e.g. infra/timeout output rather than a test assertion).
        block = extract_failure_from_log(str(log_path))
        if block and not block.startswith("("):
            return f"{block}\n"
        lines = verify_log.splitlines()
        tail = lines[-200:] if len(lines) > 200 else lines
        return "\n".join(tail) + "\n"

    return f"(unknown log_type '{log_type}'. {_LOG_KIND_HINT})"


# ---------------------------------------------------------------------------
# Tool 4: get_flaky_example
# ---------------------------------------------------------------------------

_NO_EXAMPLE_TYPES = {"unclassified", "unassigned"}

_CATEGORY_ALIASES = {
    "od": "od", "order-dependent": "od", "brittle": "od", "britle": "od",
    "td": "td", "timing-dependent": "td",
    "id": "id", "implementation-dependent": "id", "nondex": "id",
    "nio": "nio", "non-idempotent": "nio", "non-idempotent-outcome": "nio",
}


def get_flaky_example(category: str | None = None,
                      container: str | None = None) -> str:
    """Return a category-specific successful-repair exemplar.

    Defaults the category to the container's test_type when unspecified —
    saves the agent a tool call. The exemplar contains a worked example
    fix and category-specific search hints (e.g., method-name patterns
    flakyDoctor used to look for in ID cases).
    """
    canon = None
    if category and category.strip():
        requested = category.strip().lower()
        if requested in _NO_EXAMPLE_TYPES:
            return ("(get_flaky_example is unavailable for Unclassified/"
                    "Unassigned flaky-test types because no category-specific "
                    "exemplar exists. Use get_test_code, get_code, and error "
                    "logs instead.)")
        canon = _CATEGORY_ALIASES.get(requested)
        if not canon:
            return (f"(unknown category '{category}'; supported: "
                    f"OD, TD, ID, NIO)")
    if not canon and container:
        row = load_csv_row(container)
        if row:
            test_type = (row.get("test_type") or "").strip().lower()
            if test_type in _NO_EXAMPLE_TYPES:
                return ("(get_flaky_example is unavailable for Unclassified/"
                        "Unassigned flaky-test types because no category-specific "
                        "exemplar exists. Use get_test_code, get_code, and error "
                        "logs instead.)")
            canon = _CATEGORY_ALIASES.get(test_type)
    if not canon:
        return ("(category required when container is unknown; "
                "supported: OD, TD, ID, NIO)")

    path = EXAMPLES_DIR / f"{canon}.md"
    if not path.is_file():
        return f"(no example file at {path})"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tool 5: get_rv_trace_diff  (lazy TraceMOP computation)
# ---------------------------------------------------------------------------

_TRACEMOP_MVNOPTS = {
    "id": (
        "-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false "
        "-Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip "
        "-Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip "
        "-Dmaven.javadoc.skip -Dfindbugs.skip -Dwarbucks.skip -Dmodernizer.skip "
        "-Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip "
        "-Dcobertura.skip=true -Dspotless.skip=true -Dspotless.check.skip=true "
        "-Dossindex.skip=true -Dmaven.bundle.plugin.skip=true -Dmaven.parallel.force=false"
    ),
    "_default": (
        "-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip "
        "-Drat.skip -Denforcer.skip -Dmaven.javadoc.skip"
    ),
}

_EXT_JAR = "/tmp/ext-build/target/javamop-extension-1.0.jar"


def _docker_exec(docker: str, bash_cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", docker, "bash", "-c", bash_cmd],
        capture_output=True, text=True)


def _tracemop_run(docker: str, variant: str, label: str,
                  module: str, test_spec: str,
                  extra_flags: str, mvnopts: str) -> None:
    cmd = f"""set -e
rm -rf /app/work/traces-{label}
mkdir -p /app/work/traces-{label}
export TRACEDB_CONFIG_PATH=/tmp/.trace-db.config
printf 'db=memory\\ndumpDB=false\\n' > $TRACEDB_CONFIG_PATH
export RVMLOGGINGLEVEL=UNIQUE
export TRACEDB_PATH=/app/work/traces-{label}
cd /app/work/{variant}
mvn install -Dmaven.test.skip=true -pl '{module}' -am -q {mvnopts}
mvn surefire:test -Dmaven.ext.class.path={_EXT_JAR} \\
  -pl '{module}' -Dtest='{test_spec}' {extra_flags} \\
  {mvnopts} 2>&1 | tee /app/work/traces-{label}/mvn.log || true"""
    r = _docker_exec(docker, cmd)
    if r.returncode != 0:
        print(f"[rv_trace] WARNING: tracemop run '{label}' exit={r.returncode}",
              file=sys.stderr)


def _compute_rv_traces_lazy(container: str, base: Path) -> str:
    """Trigger TraceMOP trace collection on demand. Returns the summary text."""
    steps = base / "Steps_Output_Files"
    config_path = steps / "trace_config.json"

    if not config_path.is_file():
        return ("(trace_config.json not found in Steps_Output_Files/ — "
                "TraceMOP infrastructure was not set up by the shell script)")
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"(trace_config.json unreadable: {exc})"

    if not cfg.get("tracemop_ready"):
        return ("(TraceMOP is not available for this test type — "
                "no trace diff can be computed)")

    docker       = cfg.get("docker_container", "")
    test_type    = cfg.get("test_type", "").lower().replace("britle", "brittle")
    module       = cfg.get("module", ".") or "."
    victim       = cfg.get("victim", "")
    polluter     = cfg.get("polluter", "")
    nondex_seed  = cfg.get("nondex_seed", "")
    nondex_runs  = int(cfg.get("nondex_runs", 10) or 10)
    wrapper_fqcn = cfg.get("wrapper_fqcn", "")
    surefire_ver = cfg.get("surefire_version", "")

    mvnopts = _TRACEMOP_MVNOPTS.get(test_type, _TRACEMOP_MVNOPTS["_default"])
    print(f"[rv_trace] computing TraceMOP traces ({test_type}) …", file=sys.stderr)

    if test_type in ("od", "brittle"):
        _tracemop_run(docker, "Fixed", "fixed",
                      module, victim, "-Dsurefire.runOrder=testorder", mvnopts)
        _tracemop_run(docker, "Flaky", "flaky",
                      module, f"{polluter},{victim}",
                      "-Dsurefire.runOrder=testorder", mvnopts)
        actual, expected = "traces-flaky", "traces-fixed"

    elif test_type == "td":
        _tracemop_run(docker, "Fixed",           "fixed",   module, victim, "", mvnopts)
        _tracemop_run(docker, "FlakyCodeChange", "flakycc", module, victim, "", mvnopts)
        actual, expected = "traces-flakycc", "traces-fixed"

    elif test_type == "id":
        pass_cmd = f"""set -e
rm -rf /app/work/traces-pass && mkdir -p /app/work/traces-pass
export TRACEDB_CONFIG_PATH=/tmp/.trace-db.config
printf 'db=memory\\ndumpDB=false\\n' > $TRACEDB_CONFIG_PATH
export RVMLOGGINGLEVEL=UNIQUE
export TRACEDB_PATH=/app/work/traces-pass
cd /app/work/Flaky
mvn test -Dmaven.ext.class.path={_EXT_JAR} \\
  -pl '{module}' -Dtest='{victim}' {mvnopts} 2>&1 | tee /app/work/traces-pass/mvn.log || true"""
        _docker_exec(docker, pass_cmd)

        nondex_plugin_version = (cfg.get("nondex_plugin_version")
                                 or os.environ.get("NONDEX_PLUGIN_VERSION")
                                 or "2.1.1")
        fail_cmd = f"""set -e
rm -rf /app/work/traces-fail && mkdir -p /app/work/traces-fail
export TRACEDB_CONFIG_PATH=/tmp/.trace-db.config
printf 'db=memory\\ndumpDB=false\\n' > $TRACEDB_CONFIG_PATH
export RVMLOGGINGLEVEL=UNIQUE
export TRACEDB_PATH=/app/work/traces-fail
cd /app/work/Flaky
mvn edu.illinois:nondex-maven-plugin:{nondex_plugin_version}:nondex \\
  -DnondexSeed={nondex_seed} -DnondexRuns={nondex_runs} \\
  -Dmaven.ext.class.path={_EXT_JAR} \\
  -pl '{module}' -Dtest='{victim}' {mvnopts} 2>&1 | tee /app/work/traces-fail/mvn.log || true"""
        _docker_exec(docker, fail_cmd)
        actual, expected = "traces-fail", "traces-pass"

    elif test_type == "nio":
        if not wrapper_fqcn:
            return ("(wrapper_fqcn missing from trace_config.json — "
                    "cannot compute NIO trace diff)")
        sf_flag = f"-Dsurefire.version={surefire_ver}" if surefire_ver else ""
        for variant, label in [("Fixed", "fixed"), ("Flaky", "flaky")]:
            cmd = f"""set -e
rm -rf /app/work/traces-{label} && mkdir -p /app/work/traces-{label}
export TRACEDB_CONFIG_PATH=/tmp/.trace-db.config
printf 'db=memory\\ndumpDB=false\\n' > $TRACEDB_CONFIG_PATH
export RVMLOGGINGLEVEL=UNIQUE
export TRACEDB_PATH=/app/work/traces-{label}
export SUREFIRE_VERSION={surefire_ver}
cd /app/work/{variant}
mvn install -Dmaven.test.skip=true -pl {module} -am -q {mvnopts}
mvn test -Dmaven.ext.class.path={_EXT_JAR} \\
  -pl {module} -am -Dtest='{wrapper_fqcn}#runTwice' \\
  {sf_flag} {mvnopts} 2>&1 | tee /app/work/traces-{label}/mvn.log || true"""
            _docker_exec(docker, cmd)
        actual, expected = "traces-flaky", "traces-fixed"

    else:
        return f"(test_type '{test_type}' not supported for TraceMOP trace diff)"

    # Run compare-traces → rv_trace_diff.log
    cmp = subprocess.run(
        ["docker", "exec", "-w", "/tmp", docker,
         "python3", "compare-traces-official.py",
         f"/app/work/{actual}", f"/app/work/{expected}", "false"],
        capture_output=True, text=True)
    rv_log = steps / "rv_trace_diff.log"
    rv_log.write_text(cmp.stdout, encoding="utf-8")

    # Run generate_llm_summary → llm_trace_summary.txt. We must pass explicit
    # paths: the agentic pipeline uses "Steps_Output_Files" (underscore) and
    # names the diff "rv_trace_diff.log", whereas the script defaults to the
    # non-agentic "Steps Output Files" (space) + "step_8_C_official.txt". The
    # diff format is identical (both are compare-traces-official.py output), so
    # only the paths differ.
    subprocess.run(
        [sys.executable,
         str(LLM_SCRIPTS_DIR / "generate_llm_summary.py"), container,
         "--steps-dir", str(steps),
         "--compare-file", str(rv_log)],
        cwd=str(LLM_SCRIPTS_DIR), capture_output=True, text=True)

    summary_path = steps / "llm_trace_summary.txt"
    if summary_path.is_file():
        body = read_file_safe(str(summary_path)).strip()
        return (body + "\n") if body else (
            "(TraceMOP trace summary is empty — no spec violations recorded)")
    return cmp.stdout or "(TraceMOP comparison produced no output)"


def get_rv_trace_diff(container: str, test_name: str | None = None) -> str:
    """Return the decoded TraceMOP trace-diff summary for this container.

    On the first call, TraceMOP traces are collected lazily inside the running
    docker container (this takes 1-3 minutes). Subsequent calls return the
    cached llm_trace_summary.txt. Skip this tool when source code and error
    logs are already sufficient to identify the root cause.
    """
    base = _container_base(container)
    steps = base / "Steps_Output_Files"
    summary_path = steps / "llm_trace_summary.txt"
    if summary_path.is_file():
        body = read_file_safe(str(summary_path)).strip()
        if body:
            return body + "\n"
        return ("(llm_trace_summary.txt is empty — no spec violations recorded. "
                "For NIO this can happen when the bug is driven by primitive-field "
                "pollution rather than control-flow events the AspectJ specs monitor.)")
    # Defensive cache guard: if the trace diff was already computed (rv_trace_diff.log
    # present and non-empty) but no decoded summary exists, DO NOT recompute — the
    # TraceMOP collection is very expensive on large projects (e.g. two multi-module
    # Maven builds on Hadoop), and the diff is invariant across iterations. Return
    # the raw diff instead of paying that cost again.
    rv_log = steps / "rv_trace_diff.log"
    if rv_log.is_file() and rv_log.stat().st_size > 0:
        body = read_file_safe(str(rv_log)).strip()
        if body:
            return ("(decoded summary unavailable; returning the raw TraceMOP "
                    "trace diff)\n\n" + body + "\n")
    # Nothing cached yet — compute now (first call only).
    return _compute_rv_traces_lazy(container, base)


# ---------------------------------------------------------------------------
# Tool schemas for the Anthropic tool-use API
#
# Kept here so they live next to their implementations. agentic_orchestrator
# imports both TOOL_SCHEMAS and dispatch_tool.
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "get_test_code",
        "description": (
            "Return the source code for the flaky test in this container "
            "(annotations + signature + body). For OD cases, also returns "
            "the polluter test's source. Use this first to see what the "
            "test is asserting and what helpers/lifecycle hooks exist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "test_name": {
                    "type": "string",
                    "description": (
                        "Optional. The test FQN to fetch (must match the "
                        "victim or polluter for this container). Omit to "
                        "get all relevant tests for the case."
                    ),
                },
            },
        },
    },
    {
        "name": "get_code",
        "description": (
            "Return source for a Java class or method by fully-qualified "
            "name. Pass a class FQN ('com.foo.Bar') for a structural class "
            "header (package + imports + signatures + fields, no method "
            "bodies). Pass an FQN with a method ('com.foo.Bar#baz') for "
            "the named method's annotations + signature + body. Searches "
            "src/main/java first, then src/test/java."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Either 'package.ClassName' (class header) or "
                        "'package.ClassName#methodName' (method body). Copy "
                        "the FQN verbatim from the stack trace, an import, or "
                        "an extends/implements clause — do not guess the "
                        "package. For a nested class, name its enclosing "
                        "top-level class (e.g. 'pkg.Outer.Inner' or "
                        "'pkg.Outer'); the file is located automatically "
                        "regardless of which module it lives in."
                    ),
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "get_error_logs",
        "description": (
            "Return raw failure/compile/verify logs. Use 'test_failure' "
            "for the original failing surefire run's stack trace, "
            "'compile' for Maven recompile errors from the most recent "
            "patch attempt, or 'verify' for the most recent verify_after_fix.log."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "log_type": {
                    "type": "string",
                    "enum": ["test_failure", "compile", "verify"],
                    "description": (
                        "Which log to retrieve. 'compile' and 'verify' are "
                        "empty until you have submitted at least one patch."
                    ),
                },
            },
            "required": ["log_type"],
        },
    },
    {
        "name": "get_flaky_example",
        "description": (
            "Return a category-specific successful-repair exemplar with "
            "fix strategies and search hints (e.g. method-name patterns "
            "for ID like flakyDoctor used). Defaults the category to this "
            "container's test_type if you omit it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["OD", "TD", "ID", "NIO"],
                    "description": (
                        "Flaky-test category. Omit to default to this "
                        "container's recorded type."
                    ),
                },
            },
        },
    },
    {
        "name": "get_rv_trace_diff",
        "description": (
            "Return the decoded TraceMOP trace-diff summary: flaky-only "
            "traces, passing-only traces, frequency differences, and "
            "source-location mismatches between the failing and clean "
            "runs. Optional: useful when source code alone doesn't pin "
            "down the pollution site, can be skipped when other evidence "
            "is already conclusive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "test_name": {
                    "type": "string",
                    "description": (
                        "Accepted for symmetry with get_test_code; there "
                        "is only one trace summary per container."
                    ),
                },
            },
        },
    },
]


def dispatch_tool(container: str, name: str, arguments: dict) -> str:
    """Dispatch a read-only context-tool call by name. Returns a string
    payload suitable to hand back as the tool_result content block.

    Unknown tool names yield an explicit marker; an exception inside any
    tool is caught and returned as a short error string so the agent loop
    can continue rather than crash mid-iteration.
    """
    arguments = arguments or {}
    try:
        if name == "get_test_code":
            return get_test_code(container, arguments.get("test_name"))
        if name == "get_code":
            return get_code(container, arguments.get("target") or "")
        if name == "get_error_logs":
            return get_error_logs(
                container, arguments.get("log_type") or "test_failure")
        if name == "get_flaky_example":
            return get_flaky_example(
                arguments.get("category"), container=container)
        if name == "get_rv_trace_diff":
            return get_rv_trace_diff(container, arguments.get("test_name"))
        return (f"(unknown tool '{name}'. Available: get_test_code, get_code, "
                f"get_error_logs, get_flaky_example, get_rv_trace_diff, "
                f"submit_patch.)")
    except Exception as exc:  # noqa: BLE001
        return f"(tool {name} raised {type(exc).__name__}: {exc})"
