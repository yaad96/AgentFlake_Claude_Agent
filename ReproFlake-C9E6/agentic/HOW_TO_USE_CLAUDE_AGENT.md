# How to Use the Claude Code Agent on a Container to Fix Flakiness

This variant runs the **Claude Code CLI agent** as the repair backend, in place
of the API-based orchestrator (`agentic_orchestrator.py`). It plugs into the
existing ReproFlake-C9E6 OD pipeline ([run_agentic_od.sh](run_agentic_od.sh)):
the same staging, the same `tm_<container>` docker container, and the same
external scorer (`apply_fix.py` → `agentic_verify.py`). Only the repair step is
swapped, so the agent's result is directly comparable to ReproFlake/FlakyDoctor.

## 1. Prerequisites

- A flaky-test row in [test_config.csv](../test_config.csv). The worked example
  is **`jnrposixd9f3f84`** — an Order-Dependent (OD) case on Java 8, polluter
  `jnr.posix.EnvTest#testSetenvOverwrite`, victim `jnr.posix.GroupTest#getgroups`.
- The OD image with the `claude` CLI installed (Phase 1). The launcher builds it
  automatically on first use.
- `ANTHROPIC_API_KEY` exported in the **host** environment (the launcher
  requires it; the driver forwards it into the container via `docker exec -e`).
- Sonnet 4.6 access on that key, referenced through the alias `claude-sonnet-4-6`.

## 2. Quick start (one command)

The whole pipeline — stage, reproduce, run the agent, score — is one command.
`AGENTIC_DRIVER=claude_cli` is what selects the Claude Code agent backend:

```bash
ANTHROPIC_API_KEY="$(cat ~/.anthropic_api_key)" \
AGENTIC_DRIVER=claude_cli \
AGENTIC_MODEL=claude-sonnet-4-6 \
AGENTIC_MAX_BUDGET_USD=5 \
bash ReproFlake-C9E6/agentic/run_agentic_od.sh jnrposixd9f3f84
```

Useful extra env vars: `KEEP_CONTAINER=1` (leave `tm_<container>` running for
inspection), `KEEP_SOURCE=1` (keep `Flaky/`, `Flaky.pristine/` after the run),
`AGENTIC_CLI_TIMEOUT_S` (agent wall-clock cap; default 2400).

The remaining sections explain what that command does, phase by phase.

## 3. The Roadmap

### Phase 1 — Add Claude Code to the image (one-time)
The `claude` CLI is installed into the **Java-8 OD image** via two lines in
[Dockerfile.od](../Dockerfile.od):
```dockerfile
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}"
```
The launcher builds `flaky_base_jdk8_od_cov` from this Dockerfile if the image
is missing (and always rebuilds on Apple-silicon, where it runs under
`--platform linux/amd64`). Build context is the `ReproFlake-C9E6/` directory.

> Only `Dockerfile.od` currently carries these lines. Java-11 OD
> (`Dockerfile.od11`), hadoop OD (`Dockerfile.hadoop`), and the ID/NIO/TD images
> do **not** yet have `claude` — add the same two lines there to support those.

### Phase 2 — Stage the container & capture the baseline failure
Handled by `run_agentic_od.sh` (unchanged except the baseline fix noted below):
it unzips the project to `data/<container>/Flaky/`, starts the long-lived
container `tm_<container>` with `data/<container>` bind-mounted to `/app/work`,
builds the JavaMOP extension, then runs the polluter+victim pair in test order
to produce the initial failure log at `data/<container>/traces-flaky/mvn.log`. A
sanity gate aborts unless that run actually fails. It then snapshots
`Flaky/ → Flaky.pristine/` for a clean restore before scoring.

> **Baseline fix:** the baseline build uses `mvn install -DskipTests …` (compile
> tests, don't run them). It previously used `-Dmaven.test.skip=true`, which
> skips test *compilation*, so `surefire:test` found no tests for any project
> whose zip ships no pre-compiled test classes (jnr-posix is one). `-DskipTests`
> is strictly more robust and also benefits the orchestrator pipeline.

### Phase 3 — Assemble the prompt
Done by [agentic_claude_cli.py](agentic_claude_cli.py). Every agent run is driven
by two pieces of text, as with any LLM call:

- a **system prompt** — standing instructions defining the agent's role and rules; and
- a **user message** — the specific task: *this* test, in *this* project, with *this* failure.

Both are assembled automatically from the CSV row and the Phase-2 baseline log.

**The user message** is produced from the driver's template (`USER_PROMPT_TMPL`,
which mirrors ReproFlake's `INITIAL_USER_TEMPLATE`). For `jnrposixd9f3f84`:

```
=== AGENTIC FLAKY-TEST REPAIR TASK ===

GOAL: Diagnose and fix the flaky test below with the SMALLEST possible
change so that the project compiles and the test passes deterministically
under the reproduction command. Do NOT rename, refactor, or reformat
unrelated code. Do NOT modify assertions or test logic unless the assertion
itself is the root cause.

=== TEST CASE ===
Category:   Order-Dependent (OD)
Container:  jnrposixd9f3f84
Polluter:   jnr.posix.EnvTest#testSetenvOverwrite
Victim:     jnr.posix.GroupTest#getgroups
Module:     .
Java:       8

=== TEST CODE ===
<source of the victim test method, and the polluter method>

=== INITIAL FAILURE LOG ===
<the exception and stack trace from the first failing run>
```

Each field is filled from a known source, so the agent sees the same inputs the
API systems do:

| Field | What it is | Source |
|---|---|---|
| `GOAL` | Task + constraints (minimal change, no refactor, no masking assertions) | Fixed text |
| `Category` | Flakiness type (here OD) | CSV `test_type` |
| `Container` | Container/project id | CSV `result_container` |
| `Polluter` | Test that corrupts shared state when run first | CSV `polluter/state setter` (omitted if none) |
| `Victim` | The flaky test being fixed | CSV `flaky_test` |
| `Module` / `Java` | Maven module + Java version | CSV `module`, `java` |
| `TEST CODE` | Source of the victim (and polluter) methods | `assemble_llm_context` helpers |
| `INITIAL FAILURE LOG` | Exception + stack trace | `traces-flaky/mvn.log` |

**How the assembly works (`assemble_llm_context`).** The driver imports these
helpers and runs them in sequence:

1. **Read the CSV row.** `load_csv_row("jnrposixd9f3f84")` returns the row as a
   dict. Scalar fields are substituted directly; `Polluter`/`Java` lines are
   dropped entirely when empty.
2. **Locate source files.** For the victim and polluter, `fqn_to_path` turns
   `jnr.posix.GroupTest#getgroups` into `("jnr/posix/GroupTest.java", "getgroups")`,
   and `find_source_file` finds it under `data/<container>/Flaky/.../src/test/java/`.
3. **Extract the methods.** `extract_java_method` returns just the named
   method's source (brace-counting, literal/comment-aware). Victim + polluter
   bodies are concatenated into `TEST CODE`.
4. **Extract the failure.** `extract_failure_from_log` pulls the exception +
   stack trace from `traces-flaky/mvn.log` into `INITIAL FAILURE LOG`.
5. **Substitute** into the template to produce the finished user message.

**The system prompt** (`SYSTEM_PROMPT_TMPL`) keeps ReproFlake's persona and
constraints, but rewrites the tool protocol: instead of `submit_patch`/`get_code`,
the agent is told to use Read/Bash/Edit, edit files in place (not print a diff),
re-run the embedded reproduction command until the victim passes deterministically,
and end with `DONE`. It is passed via `--append-system-prompt`. The embedded
reproduction/self-verify command is identical to the official OD verify command
(see `agentic_verify._build_command`):
```bash
export SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT
mvn dependency:properties surefire:test -pl . \
  -Dtest='jnr.posix.EnvTest#testSetenvOverwrite,jnr.posix.GroupTest#getgroups' \
  -Dsurefire.runOrder=testorder -Dsurefire.timeout=180 \
  -DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip \
  -Denforcer.skip -Dmaven.javadoc.skip 2>&1
```

### Phase 4 — Launch the agent
Before launching, the driver records a **protected baseline outside the bind
mount** — a copy of `Flaky/` plus an external git index (`GIT_DIR`) in a host
temp dir. The agent runs with `bypassPermissions` over `/app/work`, so it can
(and does) delete the launcher's `Flaky.pristine` and any in-tree `.git`;
keeping capture + restore state externally makes the run robust regardless. The
agent is then run **inside** `tm_<container>`, cwd `/app/work/Flaky`, reading the
prompts from the bind-mounted `Steps_Output_Files/`:
```bash
docker exec -e ANTHROPIC_API_KEY=… -e IS_SANDBOX=1 tm_<container> bash -c '
  export PATH="/root/.local/bin:$PATH"
  export CLAUDE_CONFIG_DIR="$(mktemp -d)"
  cd /app/work/Flaky
  claude -p "$(cat /app/work/Steps_Output_Files/prompt_user.txt)" \
    --model claude-sonnet-4-6 \
    --append-system-prompt "$(cat /app/work/Steps_Output_Files/prompt_system.txt)" \
    --permission-mode bypassPermissions \
    --bare \
    --output-format stream-json --verbose --include-partial-messages \
    --max-budget-usd 5 \
    > /app/work/Steps_Output_Files/trial.ndjson \
    2> /app/work/Steps_Output_Files/claude.stderr
'
```
A fresh `CLAUDE_CONFIG_DIR` + `--bare` keep each run hermetic; `--effort` is
omitted to match ReproFlake; `bypassPermissions` lets the agent edit and run
tests unattended. Three things are load-bearing: `IS_SANDBOX=1` (lets
`bypassPermissions` run as root inside the container — `claude` otherwise refuses
bypass under root), the explicit `PATH`, and a non-login `bash -c` (a login shell
re-derives `PATH` and would drop `/root/.local/bin`).

### Phase 5 — The agent diagnoses, edits, and self-verifies
During the run the agent works autonomously: it runs the reproduction command to
see the failure, Reads the victim/polluter/related source, Edits the source in
place, then re-runs the command to confirm the victim now passes deterministically
under test order, iterating until satisfied and ending with `DONE`. This
reproduce → fix → verify loop is the agent's distinguishing behavior. Its
self-verification is only its own judgment, though — the authoritative verdict is
produced independently in Phase 7 from a clean baseline.

### Phase 6 — Capture the patch
The agent's in-place edits are captured as a unified diff against the protected
baseline, using the **external git-dir** (with `GIT_CEILING_DIRECTORIES` set so
git can never walk up into the outer Valg repo and stage edits there):
```bash
git --git-dir=<ext>/flaky.git --work-tree=data/<container>/Flaky add -A
git --git-dir=<ext>/flaky.git --work-tree=data/<container>/Flaky diff --cached HEAD > patch.diff
```

### Phase 7 — Score the patch (external verdict)
The diff is wrapped into `llm_response.json` in the shape `apply_fix.py` expects,
the tree is restored from the **protected baseline** (not `Flaky.pristine`, which
the agent may have deleted) and given a fresh Flaky-local `.git`, and the
existing scorers run — the same path the orchestrator uses, so the outcome is
comparable:
```bash
# llm_response.json = {"response": {"output_a": {"patch": "<diff>"}, "output_b": {"fixed_code": []}}}
rm -rf data/<container>/Flaky && cp -r <ext>/baseline data/<container>/Flaky
git -C data/<container>/Flaky init -q && git -C data/<container>/Flaky add -A \
  && git -C data/<container>/Flaky commit -qm baseline   # so apply_fix's `git apply` uses THIS tree
python3 "ReproFlake-C9E6/LLM Scripts/apply_fix.py" <container> --docker-container tm_<container>
python3 ReproFlake-C9E6/agentic/agentic_verify.py   <container> --docker-container tm_<container>
```
`apply_fix.py` reads the patch from `Steps_Output_Files/llm_response.json` (there
is **no** `--patch` flag), applies it with a 4-layer applier, and recompiles
inside the container. `agentic_verify.py` runs the OD verify command and writes
`Steps_Output_Files/verify_after_fix.verdict` — a binary **PASSED / FAILED**.
(The driver always restores-then-applies before verifying; there is no separate
restore-on-failure branch — that was the orchestrator's per-iteration behavior.)
The Flaky-local `.git` matters because the outer Valg repo gitignores
`data/**/Flaky`, which would otherwise make `git apply` silently skip the patch.

### Phase 8 — Collect & parse the logs
The driver parses `trial.ndjson` into per-trial views and assembles the output
folder (Phase: §4). The equivalent `jq` extractions:
```bash
# thinking text (Sonnet 4.6 streams it)
jq -rc 'select(.event.delta.type=="thinking_delta").event.delta.thinking' trial.ndjson > thinking.txt
# tool calls
jq -c 'select(.type=="assistant").message.content[]?|select(.type=="tool_use")|{name,input}' trial.ndjson > tool_calls.jsonl
# token usage + cost
jq -c 'select(.type=="result")|{usage,total_cost_usd,modelUsage}' trial.ndjson > usage.json
```

## 4. Reproducibility & Fairness Checklist

- The model is pinned to `claude-sonnet-4-6`, the same model we call in ReproFlake.
- No `--effort` is set (ReproFlake sets none).
- **Temperature:** the agent is locked at `temperature=1` and this cannot be
  changed. Since Sonnet 4.6 accepts temperature through the API,
  ReproFlake/FlakyDoctor are set to `temperature=1` so all three systems match.
- Memory is kept hermetic with a fresh `CLAUDE_CONFIG_DIR` and `--bare` per trial.
- All three systems are scored with the same external verifier
  (`apply_fix.py` → `agentic_verify.py`).
- Incidental `claude-haiku-4-5` usage (background tasks) is expected in the cost logs.

## 5. Generated Folder

Each run writes `data/claude_agent/<container>/run_<NN>/` (NN auto-increments):
```
data/claude_agent/<container>/run_<NN>/
  prompt_user.txt / prompt_system.txt   # exact prompts used
  trial.ndjson                          # full stream: thinking + tools + usage
  claude.stderr                         # agent stderr
  thinking.txt / tool_calls.jsonl / usage.json   # parsed views
  patch.diff                            # the agent's edits
  llm_response.json                     # diff wrapped for apply_fix.py
  apply_report.json                     # applier result
  verify_after_fix.{log,verdict}        # official PASSED/FAILED
  meta.json                             # model, container, polluter/victim, verdict, usage
```
