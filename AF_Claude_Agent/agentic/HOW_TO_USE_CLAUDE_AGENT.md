# Claude Agent Internals

This project uses the Claude Code CLI as the repair agent for flaky-test
containers. The user-facing run manual is the top-level `README.md`; this file
only explains what happens inside one run.

## Entry Point

Run from the repository root:

```bash
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py <container> \
  --runs 1 \
  --models claude \
  --max-iterations 5
```

`run_agentic.py` reads `AF_Claude_Agent/test_config.csv`, detects the test type,
and routes to one of:

| Type | Script |
|---|---|
| `id` | `run_agentic_id.sh` |
| `od` | `run_agentic_od.sh` |
| `nio` | `run_agentic_nio.sh` |
| `td` | `run_agentic_td.sh` |

## API Key

The key is loaded in this order:

1. `ANTHROPIC_API_KEY` from the shell.
2. `AF_Claude_Agent/.anthropic_api_key`.

The key file is ignored by Git.

## Run Layout

Each invocation creates the next run directory:

```text
AF_Claude_Agent/data/<container>/run_<NN>/
  claude_inputs/
    prompt_user.txt
    prompt_system.txt
    trace_config.json
  claude_outputs/
    trial.ndjson
    claude.stderr
    thinking.txt
    tool_calls.jsonl
    usage.json
    patch.diff
    llm_response.json
    apply_report.json
    verify_after_fix.log
    verify_after_fix.verdict
    meta.json
  pipeline.log
  .run_complete
```

Large source folders such as `Flaky/`, `Fixed/`, `Flakym2/`, and
`FlakyCodeChange/` are removed after completed `PASSED` or `FAILED` runs. Use
`KEEP_SOURCE=1` to keep them.

## Pipeline

1. The per-type shell script unzips the target project into
   `data/<container>/run_<NN>/`.
2. It starts the Docker container as `tm_<container>` and bind-mounts the run
   directory at `/app/work`.
3. It reproduces the flaky failure and writes the baseline log.
4. `agentic_claude_cli.py` builds `prompt_user.txt` and `prompt_system.txt`.
5. Claude Code runs inside Docker from `/app/work/Flaky`.
6. The driver captures Claude's in-place edits as `claude_outputs/patch.diff`.
7. The tree is restored from a protected baseline.
8. `apply_fix.py` applies the patch.
9. `agentic_verify.py` runs the official verifier and writes the final verdict.

The final verifier is authoritative. Claude's own self-verification is useful
for search, but it is not the final score.

## Claude Command

The driver runs this shape of command inside Docker:

```bash
claude -p "$(cat /app/work/claude_inputs/prompt_user.txt)" \
  --model claude-sonnet-4-6 \
  --append-system-prompt "$(cat /app/work/claude_inputs/prompt_system.txt)" \
  --permission-mode bypassPermissions \
  --bare \
  --output-format stream-json \
  --verbose \
  --include-partial-messages \
  --max-turns "$AGENTIC_MAX_ITERATIONS" \
  --max-budget-usd "$AGENTIC_MAX_BUDGET_USD"
```

Important details:

- `--bare` keeps the run isolated from local Claude project memory.
- `IS_SANDBOX=1` allows `bypassPermissions` inside the Docker container.
- `CLAUDE_CONFIG_DIR` is set to a temporary directory for each run.
- `AGENTIC_MAX_ITERATIONS` is passed to Claude Code as `--max-turns`.
- `AGENTIC_MAX_BUDGET_USD` is optional but recommended while testing.
- Usage details are stored in `claude_outputs/usage.json` and summarized by
  `run_agentic_pass_at_k.py`.

## Common Debug Flags

```bash
KEEP_SOURCE=1                 # keep source folders after run
KEEP_CONTAINER=1              # keep tm_<container> after run
AGENTIC_CLI_TIMEOUT_S=2400    # Claude Code wall-clock timeout
AGENTIC_VERIFY_PASS_RUNS=10   # extra successful verification runs required
```

## Summaries

After runs finish, summary files are updated:

```text
AF_Claude_Agent/Complete_Containers_Summary.csv
AF_Claude_Agent/data/<container>/summary.csv
```
