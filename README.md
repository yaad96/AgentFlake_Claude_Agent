# AgentFlake Claude Agent

Claude Code CLI pipeline for repairing flaky Java tests. The tool stages a flaky
test, reproduces the failure inside Docker, asks Claude Code to edit the project,
captures Claude's patch, verifies the patch from a clean baseline, and archives
the full run under `AF_Claude_Agent/data/<test>/run_<NN>/`. The examples below
cover the ID, OD, NIO, and TD flaky-test categories.

## Requirements

- Docker installed and running (all builds and tests happen inside the container).
- An Anthropic API key.

The repository installs its own Python dependencies and builds its own Docker
images. Claude Code CLI is installed inside the project images: the run scripts
build the needed image from the included Dockerfile when the image is missing, or
when an existing local image does not contain `claude`.

## Setup

From the repo root, create a file `.anthropic_api_key` and store your API key
there. The key is read from that file during a run. The file is git-ignored, so
it is safe.

## Basic Run

Run from the repository root with the venv interpreter, passing the test name:

```bash
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py <test> \
  --runs 1 \
  --models claude \
  --max-iterations 10
```

## Model Aliases

Aliases are defined in `AF_Claude_Agent/agentic/agentic_config.py`.

| Alias | Model |
|---|---|
| `claude`, `sonnet` | `claude-sonnet-4-6` |
| `opus` | `claude-opus-4-7` |
| `haiku` | `claude-haiku-4-5-20251001` |

## Examples

### ID

```bash
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py \
  incubatorshardingsphereshardingjdbcshardingjdbccored517e5eassertGetDatabaseProductName \
  --runs 1 --models claude --max-iterations 10
```

Run data for this test is in
`AF_Claude_Agent_Data.zip/ID/incubatorshardingsphereshardingjdbcshardingjdbccored517e5eassertGetDatabaseProductName`.

### OD

```bash
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py \
  wikidatatoolkitwdtkutil10f9711 \
  --runs 1 --models claude --max-iterations 10
```

Run data for this test is in `AF_Claude_Agent_Data.zip/OD/wikidatatoolkitwdtkutil10f9711`.

### NIO

```bash
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py \
  quickcheckc1c1 \
  --runs 1 --models claude --max-iterations 10
```

Run data for this test is in `AF_Claude_Agent_Data.zip/NIO/quickcheckc1c1`.

### TD

```bash
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py \
  BOOKKEEPER-846 \
  --runs 1 --models claude --max-iterations 10
```

Run data for this test is in `AF_Claude_Agent_Data.zip/TD/BOOKKEEPER-846`.

## Options

The values shown below are those defaults.

| Option | Purpose |
|---|---|
| `--runs N` | Independent runs for pass@k, which counts a test as repaired if at least one of the N independently sampled runs yields a verified fix. |
| `--models claude,opus,haiku` | One or more Claude models. |
| `--max-iterations N` | Max Claude Code turns per run. |
| `--max-budget-usd 0.50` | Hard Claude Code spend cap per run. |
| `--cli-timeout-s 2400` | Wall-clock cap for Claude Code. |
| `--verify-pass-runs 10` | Extra passing verification runs required after the first pass. |
| `--force-rebuild-image` | Rebuild the Docker image for a single run. |

## Output

Each run is archived under:

```text
AF_Claude_Agent/data/<test>/run_<NN>/
  claude_inputs/
    prompt_user.txt
    prompt_system.txt
    trace_config.json
  claude_outputs/
    trial.ndjson
    claude.stderr
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

The verdict in `verify_after_fix.verdict` is `PASSED` or `FAILED`.

Summaries are written to:

```text
AF_Claude_Agent/data/<test>/summary.csv
AF_Claude_Agent/Complete_Containers_Summary.csv
```

All run data is available in `AF_Claude_Agent_Data.zip`.
