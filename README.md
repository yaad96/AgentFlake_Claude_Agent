# AgentFlake Claude Agent

Claude Code CLI pipeline for repairing flaky tests in the AF_Claude_Agent containers.

The tool stages a flaky-test container, reproduces the failure, asks Claude Code
to edit the project inside Docker, captures Claude's patch, verifies it from a
clean baseline, and stores the full run under `AF_Claude_Agent/data/<container>/run_<NN>/`.

## Requirements

The repository can install its Python dependencies and build its Docker images.
Neede two external things:

- Docker installed and running.
- A valid Anthropic API key.


Claude Code CLI is installed inside the project Docker images. The run scripts
build the needed image from the included Dockerfile when the image is missing
or when an existing local image does not contain `claude`.

## One-Command Setup

From the repo root, create a file ".anthropic_api_key" and store your api key there. During the run, the key needed will be accessed from there. It is git-ignored, so its safe.

## Basic Run

Run from the repository root with the venv interpreter:

```bash
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py <container> \
  --runs 1 \
  --models claude \
  --max-iterations 10
```


Model aliases are defined in `AF_Claude_Agent/agentic/agentic_config.py`.

| Alias | Model |
|---|---|
| `claude` | `claude-sonnet-4-6` |
| `sonnet` | `claude-sonnet-4-6` |
| `opus` | `claude-opus-4-7` |
| `haiku` | `claude-haiku-4-5-20251001` |

## Examples

ID example:

```bash
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py \
  incubatorshardingsphereshardingjdbcshardingjdbccored517e5eassertGetDatabaseProductName \
  --runs 1 --models claude --max-iterations 10
```

OD example:

```bash
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py \
  wikidatatoolkitwdtkutil10f9711 \
  --runs 1 --models claude --max-iterations 10
```

NIO example:

```bash
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py \
  quickcheckc1c1 \
  --runs 1 --models claude --max-iterations 10
```

TD example:

```bash
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py \
  BOOKKEEPER-846 \
  --runs 1 --models claude --max-iterations 10
```

Run all four sequentially:

```bash
cd /path/to/AgentFlake_Claude_Agent

.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py incubatorshardingsphereshardingjdbcshardingjdbccored517e5eassertGetDatabaseProductName --runs 1 --models claude --max-iterations 10
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py wikidatatoolkitwdtkutil10f9711 --runs 1 --models claude --max-iterations 10
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py quickcheckc1c1 --runs 1 --models claude --max-iterations 10
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py BOOKKEEPER-846 --runs 1 --models claude --max-iterations 10
```

## Useful Options

| Option/env var | Purpose |
|---|---|
| `--runs N` | Independent runs for pass@k. |
| `--max-iterations N` | Max Claude Code turns per run. |
| `--models claude,opus` | Run one or more Claude models. |
| `AGENTIC_MAX_BUDGET_USD=0.50` | Hard Claude Code spend cap per run. |
| `AGENTIC_CLI_TIMEOUT_S=2400` | Wall-clock cap for Claude Code. |
| `AGENTIC_VERIFY_PASS_RUNS=10` | Extra passing verification runs required after the first pass. |
| `AGENTIC_FORCE_REBUILD_IMAGE=1` | Rebuild the Docker image for a single run. |

## Output

Each run writes:

```text
AF_Claude_Agent/data/<container>/run_<NN>/
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

Completed runs remove the large source folders by default. Set `KEEP_SOURCE=1`
if you need to inspect them.

Summaries are written to:

```text
AF_Claude_Agent/Complete_Containers_Summary.csv
AF_Claude_Agent/data/<container>/summary.csv
```
