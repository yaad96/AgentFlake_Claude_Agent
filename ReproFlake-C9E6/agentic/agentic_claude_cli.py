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
GITIGNORE_BODY = ("target/\n**/target/\n*.class\n*.jar\n*.war\n*.ear\n*.nar\n"
                  ".traces/\ntraces.txt\n.nondex/\n")


# ---------------------------------------------------------------------------
# TD forced-verify tree transform (Design A, correctness-first).
#
# TD flakiness reproduces deterministically ONLY under the FlakyCodeChange
# forcing (a timing perturbation, e.g. Thread.sleep, injected into the victim's
# hot path). The bare victim PASSES alone on Flaky/, so verifying the fix on
# Flaky/ alone scores an empty/no-op patch as a spurious PASSED. We instead
# verify the fix UNDER the forcing via a host-side native git 3-way merge of
# three FULL trees in an ISOLATED temp repo (NOT the Flaky-local .git):
#     base   = ext_baseline             (pristine Flaky)
#     ours   = data/<c>/FlakyCodeChange (pristine + forcing)
#     theirs = base/Flaky               (pristine + agent fix, after apply_fix)
# The forcing patch is a plain `diff -ruN` with no index lines, so a tree merge
# (not git apply --3way) is the only robust combiner. The merged tree REPLACES
# base/Flaky, then the existing verify path (cd /app/work/Flaky) runs on
# (fix + forcing) with NO change to agentic_verify.py.
#
# CORRECTNESS-FIRST CONFLICT POLICY (proven necessary on the live 812 trees,
# git 2.54.0): a 3-way TEXTUAL merge of the forcing and a fix that edits the
# SAME region has THREE outcomes, only one sound:
#   (a) clean merge of DISJOINT edits  -> forcing survives + fix survives: SOUND.
#   (b) CONFLICT (overlapping edits)   -> NO sound 'fix+forcing' tree exists: a
#       correct fix typically REMOVES the construct the forcing perturbs (812:
#       the fix replaces .save() with .store(), deleting the very timestamp race
#       the Thread.sleep was widening; the forcing's anchor line no longer
#       exists, and re-injecting the sleep would be a no-op). We FAIL CLOSED.
#   (c) clean merge of INCOMPATIBLE adjacent edits -> a tree that keeps the
#       forcing but where the fix neutered the assertion the forcing trips
#       (gaming), or a corrupt tree. Caught by the positive self-check below.
# Trade-off (documented in RESIDUAL LIMITATIONS): a genuine fix that REWRITES
# the victim test's own forced region is scored FAILED (false-negative). That is
# the correct trade vs. the hard constraint that no empty/no-op or oracle-gutting
# patch may EVER be scored PASSED -- the two are mechanically indistinguishable.
#
# Returns (ok, reason). ok=True means 'forced tree built; run verify on it'
# (the empty-fix tree lands here and equals FlakyCodeChange => verify FAILS).
# ok=False means the CALLER must force FAILED (fail-closed). It never raises for
# an expected merge/IO problem; an unexpected exception is caught by the caller
# and also mapped to FAILED.
def _td_build_forced_verify_tree(flaky, ext_baseline, forcing_tree, work_root,
                                 victim_rel):
    flaky = Path(flaky); ext_baseline = Path(ext_baseline)
    forcing_tree = Path(forcing_tree); work_root = Path(work_root)

    repo = work_root / "td_merge.git"
    wt = work_root / "td_merge_wt"
    shutil.rmtree(repo, ignore_errors=True)
    shutil.rmtree(wt, ignore_errors=True)
    wt.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["GIT_DIR"] = str(repo)
    env["GIT_WORK_TREE"] = str(wt)
    env["GIT_CEILING_DIRECTORIES"] = str(work_root.resolve())

    def g(*a):
        return run(["git", "-c", "user.name=agent",
                    "-c", "user.email=agent@local", *a],
                   env=env, check=False, capture_output=True, text=True)

    def _populate(srcdir):
        # Replace worktree contents with srcdir (excluding any .git), then
        # normalize .gitignore to GITIGNORE_BODY so (i) build artifacts (target/,
        # *.class) never enter any commit and (ii) the project's own .gitignore
        # in FlakyCodeChange vs the driver-written one in Flaky never registers
        # as a spurious change/conflict.
        for p in list(wt.iterdir()):
            if p.name == ".git":
                continue
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
        for item in srcdir.iterdir():
            if item.name == ".git":
                continue
            dst = wt / item.name
            if item.is_dir() and not item.is_symlink():
                shutil.copytree(item, dst, symlinks=True)
            else:
                shutil.copy2(item, dst)
        (wt / ".gitignore").write_text(GITIGNORE_BODY, encoding="utf-8")

    def _commit(srcdir, msg):
        _populate(srcdir)
        if g("add", "-A").returncode != 0:
            return ""
        if g("commit", "-q", "--allow-empty", "-m", msg).returncode != 0:
            return ""
        return g("rev-parse", "HEAD").stdout.strip()

    if g("init", "-q").returncode != 0:
        return (False, "td-merge: git init failed")
    base = _commit(ext_baseline, "base(pristine)")
    ours = _commit(forcing_tree, "ours(FlakyCodeChange forcing)")
    if not base or not ours:
        return (False, "td-merge: failed to commit base/ours trees")
    # theirs (the fix) must branch from base so the 3-way base is pristine.
    if g("checkout", "-q", base).returncode != 0:
        return (False, "td-merge: checkout base failed")
    if g("checkout", "-q", "-b", "theirs").returncode != 0:
        return (False, "td-merge: branch theirs failed")
    theirs = _commit(flaky, "theirs(fixed)")
    if not theirs:
        return (False, "td-merge: failed to commit theirs(fixed) tree")

    # ---- 3-way merge of forcing(ours) and fix(theirs). --------------------
    mt = g("merge-tree", "--write-tree", "--name-only", "--messages",
           ours, theirs)
    out_lines = (mt.stdout or "").splitlines()
    forced_tree_oid = out_lines[0].strip() if out_lines else ""
    if mt.returncode != 0:
        # CONFLICT (outcome b): the fix edits the same region the forcing
        # targets. No sound 'fix + meaningful forcing' tree exists. Fail closed.
        conflicted = []
        for ln in out_lines[1:]:
            if ln == "":
                break
            conflicted.append(ln)
        return (False, "td-merge: fix overlaps the forcing region "
                       f"(conflict in {conflicted or '?'}); no sound "
                       "fix+forcing tree exists -> FAILED (fail-closed).")
    if not forced_tree_oid or len(forced_tree_oid) < 40 or not all(
            ch in "0123456789abcdef" for ch in forced_tree_oid):
        return (False, "td-merge: clean merge produced no valid tree oid "
                       f"(got {forced_tree_oid!r})")

    # ---- Materialize the merged tree into a clean worktree (honors add/del).
    if g("read-tree", forced_tree_oid).returncode != 0:
        return (False, "td-merge: read-tree of merged tree failed")
    for p in list(wt.iterdir()):
        if p.name == ".git":
            continue
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink()
    if g("checkout-index", "-f", "-a").returncode != 0:
        return (False, "td-merge: checkout-index of merged tree failed")

    # ---- Positive self-check (guards outcome c): the forcing's added lines AND
    # the pristine victim assertion(s) must BOTH survive in the merged victim
    # test file. If the forcing vanished (fix dropped it) or a pristine assertion
    # vanished (fix gutted the oracle while keeping the forcing), fail closed.
    victim_live = wt / victim_rel
    victim_pristine = ext_baseline / victim_rel
    victim_forcing = forcing_tree / victim_rel
    if not victim_live.is_file():
        return (False, f"td-merge: victim test file missing after merge "
                       f"({victim_rel})")
    try:
        live_txt = victim_live.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return (False, f"td-merge: cannot read merged victim test: {e}")
    if victim_forcing.is_file() and victim_pristine.is_file():
        try:
            f_txt = victim_forcing.read_text(encoding="utf-8", errors="replace")
            p_txt = victim_pristine.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return (False, f"td-merge: cannot read forcing/pristine victim: {e}")
        live_norm = " ".join(live_txt.split())
        # (1) forcing not dropped: every non-comment line the forcing ADDED to
        # the victim test must still be present in the merged file.
        p_lines = set(p_txt.splitlines())
        forcing_added = [ln.strip() for ln in f_txt.splitlines()
                         if ln.strip() and ln not in p_lines
                         and not ln.lstrip().startswith("//")]
        missing = [ln for ln in forcing_added
                   if " ".join(ln.split()) not in live_norm]
        if missing:
            return (False, "td-merge: forcing lines absent from merged victim "
                           "test (the fix dropped/neutralized the forcing) -> "
                           f"FAILED. missing e.g.: {missing[:2]}")
        # (2) oracle not gutted: every pristine assert* statement must survive.
        pristine_asserts = [ln.strip() for ln in p_txt.splitlines()
                            if re.search(r"\bassert\w*\s*\(", ln)]
        gutted = [a for a in pristine_asserts
                  if " ".join(a.split()) not in live_norm]
        if gutted:
            return (False, "td-merge: pristine victim assertion(s) absent from "
                           "merged victim test (oracle gutted by the fix) -> "
                           f"FAILED. e.g.: {gutted[:2]}")

    # ---- Defense in depth: never let conflict markers reach a verify build.
    for jf in wt.rglob("*.java"):
        try:
            t = jf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "<<<<<<< " in t and ">>>>>>> " in t:
            return (False, f"td-merge: conflict markers survived in {jf.name}")

    # ---- Replace base/Flaky with the materialized (fix + forcing) tree. ----
    shutil.rmtree(flaky, ignore_errors=True)
    shutil.copytree(wt, flaky, symlinks=True)
    shutil.rmtree(flaky / ".git", ignore_errors=True)
    log("td-merge: clean merge -> verifying (fix + forcing); forcing and "
        "pristine assertions confirmed present.")
    return (True, "clean-merge: verifying (fix + forcing)")


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
    # Auth for the in-container `claude` CLI. Prefer an explicit env export;
    # otherwise fall back to agentic_config.ANTHROPIC_API_KEY (the same source
    # the orchestrator uses). Without this the agent runs UNAUTHENTICATED
    # (apiKeySource:none -> "Not logged in") and silently emits an empty patch
    # that is then misscored as a FAILED repair. Fail closed with a clear
    # message instead of burning a run.
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        try:
            from agentic_config import ANTHROPIC_API_KEY as _CFG_KEY  # type: ignore  # noqa: E402
            api_key = (_CFG_KEY or "").strip()
        except Exception:
            api_key = ""
    if not api_key:
        sys.exit("ERROR: no ANTHROPIC_API_KEY in the environment or "
                 "agentic_config.py — the Claude Code agent cannot "
                 "authenticate and would emit an empty patch scored as a "
                 "false FAILED. Export ANTHROPIC_API_KEY or set "
                 "agentic_config.ANTHROPIC_API_KEY, then re-run.")
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
    # Hardened: a swallowed git failure here would emit an EMPTY patch, which is
    # re-applied as a no-op and scored as a FAILED repair — masking an infra
    # problem (root-owned files the agent wrote into the bind mount, a stale
    # index lock, etc.) as "the model couldn't fix it". Surface it instead of
    # silently producing an empty diff.
    add = git(flaky, "add", "-A", gitdir=ext_gitdir, check=False)
    if add.returncode != 0:
        sys.exit(f"ERROR: git add -A failed while capturing the agent's patch "
                 f"(rc={add.returncode}): {(add.stderr or '').strip()} — "
                 f"refusing to emit an empty patch that would be misscored as a "
                 f"FAILED repair.")
    res = git(flaky, "diff", "--cached", "HEAD", gitdir=ext_gitdir, check=False)
    if res.returncode != 0:
        sys.exit(f"ERROR: git diff --cached HEAD failed while capturing the "
                 f"agent's patch (rc={res.returncode}): {(res.stderr or '').strip()}")
    diff = res.stdout
    if not diff.strip():
        # Clean add/diff but no net change: a genuine no-fix outcome (correctly
        # scored FAILED). Make it observable rather than silently empty.
        log("NOTE: agent produced no net file changes — empty patch "
            "(genuine no-fix outcome; will be scored FAILED).")
    (steps / "patch.diff").write_text(diff, encoding="utf-8")
    log(f"captured patch.diff ({len(diff)} bytes)")

    # ---- write llm_response.json in the shape apply_fix.py expects ---------
    (steps / "llm_response.json").write_text(json.dumps({
        "response": {
            "output_a": {"patch": diff},
            "output_b": {"fixed_code": []},
        }
    }, indent=2), encoding="utf-8")

    # One verify helper, defined here so BOTH the ID discrimination gate (on the
    # pristine unfixed tree, below) and the post-fix verify share one verdict
    # standard. verify_after_fix.verdict is written by agentic_verify.py.
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

    # ---- ID discrimination gate (fail-closed) ------------------------------
    # NonDex ID flakiness is probabilistic and, for some subjects, not even
    # reproducible at a fixed seed, so a single post-fix verify can PASS on
    # UNFIXED code -> an empty/no-op patch is then scored PASSED (the observed
    # shardingsphere false-PASS). Guard: the SAME verify must FAIL the UNFIXED
    # victim at least once here — on the pristine tree, BEFORE apply_fix lands the
    # patch — otherwise the verify cannot tell a fix from no-fix and any later
    # PASS is meaningless, so we fail closed. Mirrors the TD forced-verify gate.
    # Discriminative subjects (e.g. a fixed-seed order reversal) fail on run 1 and
    # cost a single extra verify; only non-reproducing ones spend the full budget.
    id_discriminative = True
    if test_type == "id":
        log(f"ID gate: does the UNFIXED victim fail the verify? "
            f"(up to {VERIFY_PASS_RUNS} run(s), on the pristine tree)")
        id_discriminative = False
        for i in range(1, VERIFY_PASS_RUNS + 1):
            bv = _verify_once()
            log(f"  gate {i}/{VERIFY_PASS_RUNS}: unfixed victim -> {bv}")
            if bv == "FAILED":
                id_discriminative = True
                log(f"  unfixed victim failed on run {i} -> verify is "
                    f"discriminative; proceeding to apply + verify the fix.")
                break
        if not id_discriminative:
            log(f"  unfixed victim PASSED all {VERIFY_PASS_RUNS} runs -> the NonDex "
                f"verify cannot reproduce this container's flakiness; a fix PASS "
                f"would be meaningless. Fail-closed (verdict FAILED).")

    log("apply_fix.py")
    run([sys.executable, str(APPLY_FIX), container,
         "--docker-container", docker_container], check=False)

    # ---- NIO oracle integrity: restore the pristine generated wrapper --------
    # The NIO verify oracle is a generated wrapper class (#runTwice) that lives
    # in the AGENT-EDITABLE tree, and the captured+re-applied patch.diff can
    # carry an agent edit to it (GITIGNORE_BODY does not exclude the wrapper
    # path). A weakened #runTwice (dropped 2nd-run assert, try/catch, no-op)
    # would be scored a false PASSED. apply_fix has now landed the patch, so the
    # agent's legitimate VICTIM fix is on disk; we overwrite ONLY the wrapper
    # file with pristine source. Verify ("mvn test ... #runTwice") re-runs
    # test-compile from this on-disk source, so the executed oracle is the
    # unmodified one. Last writer before compile/verify => dominates any tamper,
    # independent of how the patch encoded it. Gated on NIO; od/td/id untouched.
    if test_type == "nio":
        wrapper_fqcn = (os.environ.get("WRAPPER_FQCN") or "").strip()
        if not wrapper_fqcn:
            try:  # fallback: persisted by run_agentic_nio.sh
                tc = json.loads((steps / "trace_config.json").read_text(
                    encoding="utf-8"))
                wrapper_fqcn = (tc.get("wrapper_fqcn") or "").strip()
            except Exception:
                wrapper_fqcn = ""
        if not wrapper_fqcn:
            sys.exit("ERROR: NIO run missing WRAPPER_FQCN (env and "
                     "trace_config.json); refusing to verify a possibly-"
                     "tampered oracle.")

        pkg, _, simple = wrapper_fqcn.rpartition(".")  # pkg="" for default pkg
        pkg_path = pkg.replace(".", "/")
        # Mirror run_agentic_nio.sh WRAPPER_PATH_REL: MODULE in (".","") means the
        # shell wrote "./src/..." which the FS normalizes to "src/..." — so omit
        # the module path segment in that case (build via Path(*parts), no ".").
        mod = (module or ".").strip()
        parts = [] if mod in (".", "") else [mod]
        parts += ["src", "test", "java"]
        if pkg_path:
            parts += pkg_path.split("/")
        parts += [simple + ".java"]
        rel = Path(*parts)

        live = flaky / rel
        pristine = ext_baseline / rel
        if not pristine.is_file():
            fixed_alt = base / "Fixed" / rel   # gen_wrapper wrote both trees
            if fixed_alt.is_file():
                pristine = fixed_alt
        if not pristine.is_file():
            sys.exit(f"ERROR: pristine NIO wrapper not found at {rel} in "
                     f"ext_baseline or Fixed/; cannot guarantee oracle integrity "
                     f"— refusing to score this run.")

        try:
            tampered = (not live.is_file()) or (
                live.read_bytes() != pristine.read_bytes())
        except OSError:
            tampered = True
        if tampered:
            log("SECURITY: the captured patch modified/removed the generated NIO "
                f"verify wrapper ({wrapper_fqcn}); restoring the pristine oracle "
                "before verify. The victim fix is preserved; only the wrapper is "
                "overwritten.")
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pristine, live)
        log(f"restored pristine NIO wrapper: {rel}")

        # Defense in depth: an agent could also add a SECOND class with the same
        # FQCN at a different test-source path to shadow/collide with the oracle.
        # Delete any such duplicate under <module>/src/test/java — never the
        # canonical restored file, never main sources or the victim test.
        test_root = flaky.joinpath(
            *([] if mod in (".", "") else [mod]), "src", "test", "java")
        if test_root.is_dir():
            live_resolved = live.resolve()
            pkg_decl = (re.compile(r"(?m)^\s*package\s+" + re.escape(pkg) + r"\s*;")
                        if pkg else None)
            cls_decl = re.compile(r"(?m)\bclass\s+" + re.escape(simple) + r"\b")
            for cand in test_root.rglob(simple + ".java"):
                try:
                    if cand.resolve() == live_resolved:
                        continue
                    txt = cand.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                pkg_ok = (pkg_decl.search(txt) is not None) if pkg else (
                    re.search(r"(?m)^\s*package\s+", txt) is None)
                if pkg_ok and cls_decl.search(txt):
                    log("SECURITY: removing agent-added duplicate of "
                        f"{wrapper_fqcn} at {cand.relative_to(flaky)} "
                        "(would shadow/collide with the pristine oracle).")
                    try:
                        cand.unlink()
                    except OSError:
                        pass

    # ---- TD forced-verify tree (Design A): verify the fix UNDER the
    # FlakyCodeChange forcing, not on the bare victim. apply_fix has landed the
    # fix into base/Flaky; we host-side 3-way merge pristine(base) /
    # FlakyCodeChange(ours) / fixed(theirs) in an isolated temp repo and replace
    # base/Flaky with the result, so the existing verify path runs on
    # (fix + forcing). Empty fix => merged == FlakyCodeChange => victim FAILS =>
    # FAILED. Gated on td; od/id/nio untouched. Fail-closed: any merge/IO problem
    # or a conflict (fix overlaps the forcing region) => deterministic FAILED.
    td_forced_ok = True
    if test_type == "td":
        forcing_tree = base / "FlakyCodeChange"
        # victim_rel = tree-relative path of the victim test file (module-aware).
        victim_fqn = (row.get("flaky_test") or "").strip()
        victim_rel = None
        try:
            rel_path, _m = fqn_to_path(victim_fqn)
            src = find_source_file(str(base), module, rel_path)
            if src:
                victim_rel = Path(src).resolve().relative_to(flaky.resolve())
        except Exception:
            victim_rel = None
        if not forcing_tree.is_dir():
            log("ERROR: TD forced verify requires data/<container>/"
                "FlakyCodeChange (the forcing tree); not found — failing closed.")
            td_forced_ok = False
        elif victim_rel is None:
            log(f"ERROR: TD forced verify could not resolve the victim test file "
                f"for '{victim_fqn}' under Flaky/ — failing closed.")
            td_forced_ok = False
        else:
            try:
                td_forced_ok, td_reason = _td_build_forced_verify_tree(
                    flaky, ext_baseline, forcing_tree, ext, str(victim_rel))
                if not td_forced_ok:
                    log(f"TD forced-verify tree build failed: {td_reason} — "
                        f"failing closed (verdict FAILED).")
            except Exception as exc:
                log(f"ERROR: TD forced-verify tree build crashed: {exc!r} — "
                    f"failing closed (verdict FAILED).")
                td_forced_ok = False

    # Ensure test classes are compiled before verify (OD/TD verify does not
    # compile; apply_fix only recompiles when it lands a patch). For td this
    # compiles the merged (fix + forcing) tree now on disk at base/Flaky.
    if not (test_type == "td" and not td_forced_ok):
        compile_in_container(docker_container, test_type, module)

    # Initial verify, then — matching the orchestrator's bar — require
    # VERIFY_PASS_RUNS additional passing confirmation runs. A fix that passes
    # once but fails a confirmation is non-deterministic -> FAILED. The tree is
    # left applied+compiled across confirmations (no restore between), exactly
    # as the orchestrator does.
    log("agentic_verify.py (initial run)")
    if test_type == "td" and not td_forced_ok:
        # The forced-verify tree could not be built (missing forcing tree,
        # unresolved victim, merge conflict = fix overlaps the forcing region,
        # or a self-check failure = forcing/assertion vanished). Per Design A we
        # must NOT fall back to the non-discriminative bare-victim verify, which
        # would score an empty/no-op patch PASSED. Record FAILED with a clear
        # line and skip the verify command.
        log("TD forced-verify tree unavailable — recording FAILED (fail-closed; "
            "NOT running the non-discriminative bare-victim verify).")
        verdict_path.write_text("FAILED\n", encoding="utf-8")
        verdict = "FAILED"
    elif test_type == "id" and not id_discriminative:
        # The unfixed victim never failed the verify (see the ID gate above), so a
        # pass here could not distinguish a real fix from a no-op patch. Do NOT run
        # the non-discriminative verify; record FAILED (fail-closed).
        log("ID verify is non-discriminative (unfixed victim never failed) — "
            "recording FAILED (fail-closed).")
        verdict_path.write_text("FAILED\n", encoding="utf-8")
        verdict = "FAILED"
    else:
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
        # ID only: did the UNFIXED victim fail the verify (i.e. is the verify
        # discriminative for this container)? False => verdict was fail-closed.
        "id_discriminative": (id_discriminative if test_type == "id" else None),
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
