#!/usr/bin/env python3
"""
agentic_claude_cli.py — "Claude Code agent" repair driver.

Drop-in alternative to agentic_orchestrator.py for the agentic OD pipeline.
Instead of an API conversation that calls submit_patch, this driver runs the
*Claude Code CLI agent* autonomously inside the already-running tm_<container>
docker container: the agent reproduces the failure, reads/edits the source in
place, and self-verifies by re-running the reproduction command. Its edits are
then captured as a unified diff and scored through the SAME external path the
orchestrator uses (apply_fix.py -> agentic_verify.py), so the result is
directly comparable to ReproFlake/FlakyDoctor.

Invoked by run_agentic_od.sh (with AGENTIC_DRIVER=claude_cli) exactly like the
orchestrator:

    python3 agentic_claude_cli.py <result_container> --docker-container <name>
            [--model claude-sonnet-4-6] [--max-budget-usd N]

Preconditions (all set up by run_agentic_od.sh steps 0-9.5):
    - data/<container>/Flaky/            staged source tree (host bind-mount)
    - data/<container>/Flaky.pristine/   clean snapshot for restore
    - data/<container>/traces-flaky/mvn.log   initial failure log
    - container tm_<container> running, /app/work bound to data/<container>,
      with the Claude Code CLI installed (Dockerfile.od) and ANTHROPIC_API_KEY
      available in the host environment.

Outputs (data/<container>/Steps_Output_Files/ + a per-trial folder):
    prompt_user.txt, prompt_system.txt, trial.ndjson, claude.stderr,
    patch.diff, llm_response.json, apply_report.json,
    verify_after_fix.{log,verdict}, thinking.txt, tool_calls.jsonl, usage.json
    -> all copied into data/claude_agent/<container>/run_<NN>/ with meta.json
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import tempfile
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
LLM_SCRIPTS_DIR = REPROFLAKE_DIR / "LLM Scripts"
APPLY_FIX = LLM_SCRIPTS_DIR / "apply_fix.py"
AGENTIC_VERIFY = SCRIPT_DIR / "agentic_verify.py"

sys.path.insert(0, str(LLM_SCRIPTS_DIR))
from assemble_llm_context import (  # type: ignore  # noqa: E402
    DATA_DIR,
    load_csv_row,
    fqn_to_path,
    find_source_file,
    extract_java_method,
    extract_failure_from_log,
)

# Number of EXTRA passing confirmation runs required after the first PASS —
# the same bar the orchestrator applies (agentic_config.VERIFY_PASS_RUNS), so
# all three systems use one verdict standard. Falls back to 10 if unavailable.
try:
    sys.path.insert(0, str(SCRIPT_DIR))
    from agentic_config import VERIFY_PASS_RUNS  # type: ignore  # noqa: E402
except Exception:
    VERIFY_PASS_RUNS = 10
# Per-run override (defaults to the config value, so parity with the
# orchestrator is untouched unless explicitly set). Useful to cap confirmation
# wall-clock on heavy projects (e.g. AGENTIC_VERIFY_PASS_RUNS=10 for HBase).
VERIFY_PASS_RUNS = int(os.environ.get("AGENTIC_VERIFY_PASS_RUNS", VERIFY_PASS_RUNS))

# Wall-clock cap for the agent run (mvn under emulation is slow).
AGENT_TIMEOUT_S = int(os.environ.get("AGENTIC_CLI_TIMEOUT_S", "2400"))

# Build artefacts we never want inside the captured patch.
GITIGNORE_BODY = "target/\n**/target/\n*.class\n.traces/\ntraces.txt\n.nondex/\n"

PRETTY_TYPE = {
    "od": "Order-Dependent (OD)",
    "td": "Test-Dependent (TD)",
    "id": "Implementation-Dependent (ID)",
    "nio": "Non-Idempotent-Outcome (NIO)",
}

# Reproduction/verification commands — kept in lock-step with
# agentic_verify._build_command per type, so the agent self-verifies on the same
# command that produces the official verdict.
MVNOPTS_OD = ('-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip '
              '-Drat.skip -Denforcer.skip -Dmaven.javadoc.skip')
MVNOPTS_ID = (
    '-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false '
    '-Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip '
    '-Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip '
    '-Dmaven.javadoc.skip -Dfindbugs.skip -Dwarbucks.skip -Dmodernizer.skip '
    '-Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip '
    '-Dcobertura.skip=true -Dspotless.skip=true -Dspotless.check.skip=true '
    '-Dossindex.skip=true -Dmaven.bundle.plugin.skip=true '
    '-Dmaven.parallel.force=false')
MVNOPTS_TD = MVNOPTS_OD
MVNOPTS_NIO = MVNOPTS_ID + ' -Dfindbugs.skip=true'

# Per-type initial-failure log directory written by the launcher.
FAILURE_LOG_DIR = {"od": "traces-flaky", "id": "traces-fail",
                   "td": "traces-flakycc", "nio": "traces-flaky"}
SUPPORTED_TYPES = {"od", "id", "td", "nio"}


def repro_command(test_type: str, module: str, polluter: str, victim: str) -> str:
    # Always recompile FIRST (the test runner executes compiled .class files),
    # then run the type-specific check.
    if test_type == "id":
        seed = os.environ.get("NONDEXSEED", "").strip()
        runs = os.environ.get("NONDEX_RUNS", "").strip() or "1"
        ver = os.environ.get("NONDEX_PLUGIN_VERSION", "2.1.1").strip() or "2.1.1"
        return (
            f"mvn test-compile -pl {module} {MVNOPTS_ID} 2>&1   # 1) recompile your edits\n"
            f"mvn edu.illinois:nondex-maven-plugin:{ver}:nondex "
            f"-DnondexSeed={seed} -DnondexRuns={runs} "
            f"-pl '{module}' -Dtest='{victim}' -Dsurefire.timeout=180 {MVNOPTS_ID} 2>&1   # 2) run NonDex"
        )
    if test_type == "td":
        return (
            f"mvn test-compile -pl {module} {MVNOPTS_TD} 2>&1   # 1) recompile your edits\n"
            f"mvn dependency:properties surefire:test "
            f"-pl {module} -Dtest='{victim}' -Dsurefire.timeout=180 {MVNOPTS_TD} 2>&1   # 2) run the victim on its own"
        )
    if test_type == "nio":
        wrapper = os.environ.get("WRAPPER_FQCN", "").strip()
        ver = os.environ.get("SUREFIRE_VER", "3.0.0-M5").strip() or "3.0.0-M5"
        return (
            f"export SUREFIRE_VERSION={ver}\n"
            f"mvn test-compile -pl {module} -am {MVNOPTS_NIO} 2>&1   # 1) recompile your edits\n"
            f"mvn test -pl {module} -am -Dtest='{wrapper}#runTwice' "
            f"-Dsurefire.timeout=180 {MVNOPTS_NIO} 2>&1   # 2) run the test twice"
        )
    # od
    return (
        "export SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT\n"
        f"mvn test-compile -pl {module} {MVNOPTS_OD} 2>&1   # 1) recompile your edits\n"
        f"mvn dependency:properties surefire:test "
        f"-pl {module} -Dtest='{polluter},{victim}' "
        f"-Dsurefire.runOrder=testorder -Dsurefire.timeout=180 {MVNOPTS_OD} 2>&1   # 2) run"
    )


def type_context(test_type: str):
    """(order_phrase, type_note) injected into the system prompt per type."""
    if test_type == "id":
        seed = os.environ.get("NONDEXSEED", "").strip()
        runs = os.environ.get("NONDEX_RUNS", "").strip() or "1"
        note = (
            "This is an Implementation-Dependent (ID) flaky test: it fails "
            "non-deterministically because it relies on an unspecified "
            "iteration/element order. NonDex re-runs the test under shuffled "
            "orders to expose this. There is NO polluter test — the root cause "
            "is the test (or the code it exercises) assuming an order that is "
            "not guaranteed. Your fix must make it pass regardless of order "
            "(e.g. sort results, use order-stable collections, or drop the "
            "order assumption).\n\n")
        return (f"across the NonDex shuffled run(s) (pinned seed {seed}, "
                f"{runs} run(s))"), note
    if test_type == "td":
        note = (
            "This is a Test-Dependent (TD) flaky test: it fails when run on its "
            "own (not because another test pollutes it). Diagnose why the test "
            "fails in isolation and fix it minimally.\n\n")
        return "when run on its own", note
    if test_type == "nio":
        wrapper = os.environ.get("WRAPPER_FQCN", "").strip()
        note = (
            "This is a Non-Idempotent-Outcome (NIO) flaky test: it passes the "
            "first time but FAILS when run a second time in the same JVM, because "
            "it leaves behind state (static fields, files, singletons, system "
            "properties, registered hooks, etc.). A generated wrapper class "
            f"({wrapper}) runs the victim twice via #runTwice. Make the SECOND run "
            "pass too — typically by resetting/cleaning up the shared state in "
            "setUp/tearDown or making the code idempotent. Do NOT edit the "
            "generated wrapper class.\n\n")
        return "when run twice in a row (the wrapper's #runTwice)", note
    return "deterministically under this test order", ""


SYSTEM_PROMPT_TMPL = """\
You are an expert Java developer who diagnoses and repairs flaky tests, working
directly inside the project's working directory (your current directory).

GOAL — make the named flaky test pass deterministically with the SMALLEST
correct change. Do NOT rename methods, change unrelated code, modify assertions
to mask a real bug, or refactor the test. Success = the project compiles AND
the victim test passes under the exact reproduction command below.

{type_note}You work with your own tools (there is no submit_patch / get_code tool):
  - Use Read to inspect the victim test, the polluter test (if any), and any
    related source code you need.
  - Use Bash to run the reproduction command and observe the result.
  - Use Edit to make the minimal source change in place. Do NOT print a diff —
    edit the files directly; your change is captured from git afterwards.

Reproduction / self-verification commands (run them from the project root, which
is your current directory). ALWAYS run the recompile step after editing source —
the test runner executes compiled .class files, so without recompiling it would
use stale bytecode and not reflect your change:

{repro}

A run PASSES iff Surefire reports Tests>0, Failures=0, Errors=0 and there are
no "<<< FAILURE" / "<<< ERROR" markers in the output. First reproduce the
failure to see it firsthand, then make the minimal fix, then recompile and re-run
to confirm the victim now passes {order_phrase}. When
the fix is confirmed, end your final message with the single line: DONE.
"""

USER_PROMPT_TMPL = """\
=== AGENTIC FLAKY-TEST REPAIR TASK ===

GOAL: Diagnose and fix the flaky test below with the SMALLEST possible
change so that the project compiles and the test passes deterministically
under the reproduction command. Do NOT rename, refactor, or reformat
unrelated code. Do NOT modify assertions or test logic unless the assertion
itself is the root cause.

=== TEST CASE ===
Category:   {pretty_type}
Container:  {container}
{polluter_line}Victim:     {victim_fqn}
Module:     {module}
{java_line}
=== TEST CODE ===
{test_code}

=== INITIAL FAILURE LOG ===
{failure_text}

=== HOW TO PROCEED ===
  1. Run the reproduction command to observe the failure firsthand.
  2. Reason from the test code and failure log above; Read related source only
     as needed.
  3. Make the smallest edit consistent with the evidence.
  4. Re-run the reproduction command to confirm the victim now passes
     deterministically under test order.
  5. End your final message with the single line: DONE.
"""


def log(msg: str) -> None:
    print(f"[claude-cli] {msg}", flush=True)


def run(cmd, **kw):
    """subprocess.run wrapper that streams nothing but returns the result."""
    return subprocess.run(cmd, **kw)


def git(work_tree: Path, *args: str, gitdir: Path = None, check: bool = True):
    """Run git against `work_tree`. With gitdir set, the repo metadata lives at
    an EXTERNAL path (outside the agent's reach); otherwise a work_tree-local
    .git is used. GIT_CEILING_DIRECTORIES forbids git from ever walking up into
    the outer Valg repo (which would otherwise stage our edits there)."""
    env = dict(os.environ)
    env["GIT_CEILING_DIRECTORIES"] = str(Path(work_tree).resolve().parent)
    ident = ["-c", "user.name=agent", "-c", "user.email=agent@local"]
    if gitdir is not None:
        env["GIT_DIR"] = str(gitdir)
        env["GIT_WORK_TREE"] = str(work_tree)
        cmd = ["git", *ident, *args]
    else:
        cmd = ["git", "-C", str(work_tree), *ident, *args]
    return run(cmd, env=env, check=check, capture_output=True, text=True)


def build_test_code(base: Path, module: str, fqns) -> str:
    """Extract the source of each victim/polluter method, concatenated."""
    chunks = []
    for fqn in fqns:
        if not fqn:
            continue
        rel_path, method = fqn_to_path(fqn)
        src = find_source_file(str(base), module, rel_path)
        if not src:
            chunks.append(f"// ({fqn}) — source file not found under Flaky/")
            continue
        body = extract_java_method(src, method) if method else None
        if not body:
            chunks.append(f"// ({fqn}) — method '{method}' not found in {rel_path}")
            continue
        chunks.append(f"// ===== {fqn} ({rel_path}) =====\n{body}")
    return "\n\n".join(chunks) if chunks else "(no test code extracted)"


def assemble_prompts(row: dict, base: Path):
    test_type = (row.get("test_type") or "").strip().lower()
    module = (row.get("module") or ".").strip()
    polluter = (row.get("polluter/state setter") or "").strip()
    victim = (row.get("flaky_test") or "").strip()
    java = (row.get("java") or "").strip()
    container = (row.get("result_container") or "").strip()

    test_code = build_test_code(base, module, [victim, polluter])
    log_dir = FAILURE_LOG_DIR.get(test_type, "traces-flaky")
    failure_text = extract_failure_from_log(str(base / log_dir / "mvn.log"))

    polluter_line = f"Polluter:   {polluter}\n" if polluter else ""
    java_line = f"Java:       {java}\n" if java else ""
    order_phrase, type_note = type_context(test_type)

    user_prompt = USER_PROMPT_TMPL.format(
        pretty_type=PRETTY_TYPE.get(test_type, test_type.upper() or "Flaky"),
        container=container,
        polluter_line=polluter_line,
        victim_fqn=victim,
        module=module,
        java_line=java_line,
        test_code=test_code,
        failure_text=failure_text,
    )
    system_prompt = SYSTEM_PROMPT_TMPL.format(
        type_note=type_note,
        order_phrase=order_phrase,
        repro="  " + repro_command(
            test_type, module, polluter, victim).replace("\n", "\n  "))
    return user_prompt, system_prompt


def run_agent_in_container(docker_container: str, model: str,
                           max_budget_usd, steps_rel: str) -> int:
    """docker exec the Claude Code agent inside /app/work/Flaky. Reads the
    prompt files from the bind-mounted Steps_Output_Files dir, writes the
    stream-json log back to the same dir. Returns the agent's exit code."""
    budget = (f"--max-budget-usd {max_budget_usd} " if max_budget_usd else "")
    inner = f"""
set -o pipefail
export PATH="/root/.local/bin:$PATH"
export CLAUDE_CONFIG_DIR="$(mktemp -d)"
cd /app/work/Flaky
timeout -k 30s {AGENT_TIMEOUT_S}s claude -p "$(cat /app/work/{steps_rel}/prompt_user.txt)" \
  --model {model} \
  --append-system-prompt "$(cat /app/work/{steps_rel}/prompt_system.txt)" \
  --permission-mode bypassPermissions \
  --bare \
  --output-format stream-json --verbose --include-partial-messages \
  {budget}> /app/work/{steps_rel}/trial.ndjson 2> /app/work/{steps_rel}/claude.stderr
"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    # IS_SANDBOX=1 lets --permission-mode bypassPermissions run as root inside
    # the container (claude otherwise refuses bypass under root/sudo).
    cmd = ["docker", "exec",
           "-e", f"ANTHROPIC_API_KEY={api_key}",
           "-e", "IS_SANDBOX=1",
           docker_container, "bash", "-c", inner]
    log(f"running Claude Code agent in {docker_container} (model={model}, "
        f"timeout={AGENT_TIMEOUT_S}s)")
    try:
        # Host-side timeout is a backstop only — the container-side `timeout`
        # above is the real one (it actually kills claude inside the container,
        # whereas killing the `docker exec` client would orphan it). Give the
        # host a grace margin so the in-container kill fires first.
        proc = run(cmd, timeout=AGENT_TIMEOUT_S + 120)
        return proc.returncode
    except subprocess.TimeoutExpired:
        log(f"agent exceeded {AGENT_TIMEOUT_S + 120}s host wall-clock — killed")
        return 124


def compile_in_container(docker_container: str, test_type: str, module: str):
    """Ensure test classes are compiled before agentic_verify runs.

    apply_fix.py only recompiles when it APPLIES a patch, and the OD/TD verify
    command (surefire:test) does not compile. So a run where the agent correctly
    submits no patch (the test already passes) would otherwise be verified
    against absent/stale test classes -> Tests=0 -> a spurious FAILED, and a
    patch-submitting system would unfairly recompile-and-pass where a no-patch
    one fails. Compiling here makes the verdict reflect reality for both.

    ID (the NonDex goal) and NIO (`mvn test`) compile on their own, so they are
    skipped.
    """
    if test_type not in ("od", "td"):
        return
    pre = "export SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT\n" if test_type == "od" else ""
    cmd = f"{pre}cd /app/work/Flaky && mvn test-compile -pl {module} {MVNOPTS_OD} 2>&1"
    log("compiling test classes in container before verify")
    run(["docker", "exec", docker_container, "bash", "-c", cmd], check=False)


def parse_stream(ndjson_path: Path, steps: Path):
    """Split the stream-json log into thinking / tool-call / usage views."""
    thinking, tool_calls, usage = [], [], None
    if ndjson_path.is_file():
        for line in ndjson_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type")
            # incremental thinking deltas
            ev = rec.get("event") or {}
            delta = ev.get("delta") or {}
            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                thinking.append(delta["thinking"])
            # complete assistant message blocks (thinking + tool_use)
            if rtype == "assistant":
                for blk in (rec.get("message", {}).get("content") or []):
                    if blk.get("type") == "thinking" and blk.get("thinking"):
                        thinking.append(blk["thinking"])
                    elif blk.get("type") == "tool_use":
                        tool_calls.append({"name": blk.get("name"),
                                           "input": blk.get("input")})
            if rtype == "result":
                usage = {
                    "usage": rec.get("usage"),
                    "total_cost_usd": rec.get("total_cost_usd"),
                    "modelUsage": rec.get("modelUsage"),
                    "num_turns": rec.get("num_turns"),
                    "duration_ms": rec.get("duration_ms"),
                    "is_error": rec.get("is_error"),
                    "subtype": rec.get("subtype"),
                }
    (steps / "thinking.txt").write_text("".join(thinking), encoding="utf-8")
    with (steps / "tool_calls.jsonl").open("w", encoding="utf-8") as f:
        for tc in tool_calls:
            f.write(json.dumps(tc) + "\n")
    (steps / "usage.json").write_text(
        json.dumps(usage or {}, indent=2), encoding="utf-8")
    return len(thinking), len(tool_calls), usage


def next_experiment_dir(exp_root: Path) -> Path:
    """Per-run folder at data/claude_agent/<container>/run_<NN>/."""
    n = 0
    if exp_root.is_dir():
        for d in exp_root.glob("run_*"):
            m = re.match(r"run_(\d+)$", d.name)
            if m:
                n = max(n, int(m.group(1)))
    exp_root.mkdir(parents=True, exist_ok=True)
    return exp_root / f"run_{n + 1:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("container")
    ap.add_argument("--docker-container")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--max-budget-usd", default=None)
    ap.add_argument("--max-iterations", default=None,
                    help="accepted for orchestrator compatibility; ignored")
    args = ap.parse_args()

    container = args.container
    docker_container = args.docker_container or (
        "tm_" + re.sub(r"[^a-zA-Z0-9]", "_", container))

    row = load_csv_row(container)
    if not row:
        sys.exit(f"ERROR: container '{container}' not in test_config.csv")
    test_type = (row.get("test_type") or "").strip().lower()
    if test_type not in SUPPORTED_TYPES:
        sys.exit(f"ERROR: agentic_claude_cli supports {sorted(SUPPORTED_TYPES)} "
                 f"only (got '{test_type}').")
    module = (row.get("module") or ".").strip()

    base = Path(DATA_DIR) / container
    flaky = base / "Flaky"
    steps = base / "Steps_Output_Files"
    steps.mkdir(parents=True, exist_ok=True)
    steps_rel = "Steps_Output_Files"

    log_dir = FAILURE_LOG_DIR.get(test_type, "traces-flaky")
    for p in (flaky, base / log_dir / "mvn.log"):
        if not p.exists():
            sys.exit(f"ERROR: expected '{p}' (the launcher must run first)")

    # External workspace OUTSIDE the /app/work bind mount. The agent runs with
    # bypassPermissions inside /app/work, so anything kept there (a Flaky-local
    # .git, the launcher's Flaky.pristine) can be deleted by the agent. Keeping
    # the git metadata and a baseline copy here makes capture + restore robust
    # no matter what the agent does to /app/work.
    ext = Path(tempfile.mkdtemp(prefix=f"agentcli_{container}_"))
    ext_gitdir = ext / "flaky.git"
    ext_baseline = ext / "baseline"
    # Guarantee the external workspace (a full source-tree copy) is removed on
    # EVERY exit path — exception, sys.exit, or Ctrl-C — not just the success
    # path below. Otherwise a batch run leaks a project copy into /tmp per crash.
    atexit.register(lambda: shutil.rmtree(ext, ignore_errors=True))

    # ---- assemble prompts --------------------------------------------------
    log("assembling prompts")
    user_prompt, system_prompt = assemble_prompts(row, base)
    (steps / "prompt_user.txt").write_text(user_prompt, encoding="utf-8")
    (steps / "prompt_system.txt").write_text(system_prompt, encoding="utf-8")

    # ---- protected baseline (external git-dir + copy) ----------------------
    log(f"snapshotting protected baseline at {ext}")
    shutil.rmtree(flaky / ".git", ignore_errors=True)
    (flaky / ".gitignore").write_text(GITIGNORE_BODY, encoding="utf-8")
    shutil.copytree(flaky, ext_baseline)
    git(flaky, "init", "-q", gitdir=ext_gitdir)
    git(flaky, "add", "-A", gitdir=ext_gitdir)
    git(flaky, "commit", "-q", "-m", "baseline", gitdir=ext_gitdir)

    # ---- run the agent -----------------------------------------------------
    agent_rc = run_agent_in_container(
        docker_container, args.model, args.max_budget_usd, steps_rel)
    log(f"agent exit code: {agent_rc}")

    # ---- capture the patch (external git-dir; never the outer repo) --------
    git(flaky, "add", "-A", gitdir=ext_gitdir, check=False)
    diff = git(flaky, "diff", "--cached", "HEAD", gitdir=ext_gitdir, check=False).stdout
    (steps / "patch.diff").write_text(diff, encoding="utf-8")
    log(f"captured patch.diff ({len(diff)} bytes)")

    # ---- write llm_response.json in the shape apply_fix.py expects ---------
    (steps / "llm_response.json").write_text(json.dumps({
        "response": {
            "output_a": {"patch": diff},
            "output_b": {"fixed_code": []},
        }
    }, indent=2), encoding="utf-8")

    # ---- restore the protected baseline, then re-apply via the applier -----
    log("restoring Flaky/ from the protected baseline")
    shutil.rmtree(flaky, ignore_errors=True)
    shutil.copytree(ext_baseline, flaky)
    # A Flaky-local .git so apply_fix's `git apply` uses THIS tree (the outer
    # Valg repo gitignores data/**/Flaky, which makes git apply silently skip).
    # Safe now — the agent is no longer running. GIT_CEILING (in git()) keeps
    # it from escaping upward even if this .git is somehow absent.
    git(flaky, "init", "-q")
    git(flaky, "add", "-A")
    git(flaky, "commit", "-q", "-m", "baseline")

    log("apply_fix.py")
    run([sys.executable, str(APPLY_FIX), container,
         "--docker-container", docker_container], check=False)

    # Ensure test classes are compiled before verify (OD/TD verify does not
    # compile; apply_fix only recompiles when it lands a patch).
    compile_in_container(docker_container, test_type, module)

    verdict_path = steps / "verify_after_fix.verdict"

    def _verify_once() -> str:
        # Clear any prior verdict first so a verify that crashes BEFORE writing
        # one cannot leave us reading a STALE verdict (e.g. a previous PASSED) —
        # that would be a fabricated PASS, violating the PASSED/FAILED-only rule.
        verdict_path.unlink(missing_ok=True)
        rc = run([sys.executable, str(AGENTIC_VERIFY), container,
                  "--docker-container", docker_container], check=False).returncode
        if verdict_path.is_file():
            return verdict_path.read_text(encoding="utf-8").strip()
        # No verdict written -> verify itself crashed (container gone, missing
        # env, ...). Not a real test result: fail closed and surface it.
        log(f"WARNING: agentic_verify wrote no verdict (rc={rc}); treating as FAILED")
        return "FAILED"

    # Initial verify, then — matching the orchestrator's bar — require
    # VERIFY_PASS_RUNS additional passing confirmation runs. A fix that passes
    # once but fails a confirmation is non-deterministic -> FAILED. The tree is
    # left applied+compiled across confirmations (no restore between), exactly
    # as the orchestrator does.
    log("agentic_verify.py (initial run)")
    verdict = _verify_once()
    confirm_runs: list = []
    if verdict == "PASSED":
        for i in range(1, VERIFY_PASS_RUNS + 1):
            c = _verify_once()
            confirm_runs.append({"run": i, "verdict": c})
            log(f"confirmation {i}/{VERIFY_PASS_RUNS}: {c}")
            if c != "PASSED":
                verdict = "FAILED"   # CONFIRM_FAILED: fix is still flaky
                break
    # Ensure the canonical verdict file reflects the all-runs-must-pass result.
    verdict_path.write_text(verdict + "\n", encoding="utf-8")
    log(f"verdict after {1 + len(confirm_runs)} verify run(s): {verdict}")

    # ---- parse logs --------------------------------------------------------
    n_think, n_tools, usage = parse_stream(steps / "trial.ndjson", steps)
    log(f"parsed stream: thinking_chunks={n_think} tool_calls={n_tools}")

    # ---- assemble the per-run folder: data/claude_agent/<container>/run_NN/
    exp_root = Path(DATA_DIR) / "claude_agent" / container
    exp = next_experiment_dir(exp_root)
    exp.mkdir()
    artifacts = ["prompt_user.txt", "prompt_system.txt", "trial.ndjson",
                 "claude.stderr", "patch.diff", "llm_response.json",
                 "apply_report.json", "verify_after_fix.log",
                 "verify_after_fix.verdict", "thinking.txt",
                 "tool_calls.jsonl", "usage.json"]
    for name in artifacts:
        src = steps / name
        if src.is_file():
            shutil.copy2(src, exp / name)
    (exp / "meta.json").write_text(json.dumps({
        "container": container,
        "docker_container": docker_container,
        "model": args.model,
        "test_type": test_type,
        "module": row.get("module"),
        "polluter": row.get("polluter/state setter"),
        "victim": row.get("flaky_test"),
        "agent_exit_code": agent_rc,
        "verdict": verdict,
        "verify_pass_runs": VERIFY_PASS_RUNS,
        "confirm_runs": confirm_runs,
        "patch_bytes": len(diff),
        "usage": usage,
    }, indent=2), encoding="utf-8")

    shutil.rmtree(ext, ignore_errors=True)
    log(f"verdict: {verdict}")
    log(f"experiment folder: {exp}")
    sys.exit(0 if verdict == "PASSED" else 1)


if __name__ == "__main__":
    main()
