# AgentFlake Claude Agent

Claude Code CLI pipeline for repairing flaky tests in the AF_Claude_Agent containers.

The tool stages a flaky-test container, reproduces the failure, asks Claude Code
to edit the project inside Docker, captures Claude's patch, verifies it from a
clean baseline, and stores the full run under `AF_Claude_Agent/data/<container>/run_<NN>/`.

## Requirements

The repository can install its Python dependencies and build its Docker images.
A reviewer still needs two external things:

- Docker installed and running.
- A valid Anthropic API key.

Linux is supported. On a fresh Linux machine, install Docker Engine and Python
with venv support first. On Debian/Ubuntu that usually means `docker`, `python3`,
`python3-venv`, and `python3-pip`. The user running the tool must be able to run
`docker` without `sudo`, or the bind-mounted run folders may become root-owned.

Claude Code CLI is installed inside the project Docker images. The run scripts
build the needed image from the included Dockerfile when the image is missing
or when an existing local image does not contain `claude`.

## One-Command Setup

From a fresh clone, run:

```bash
ANTHROPIC_API_KEY=sk-ant-your-key-here bash setup.sh --build-images
```

This command:

- creates `.venv/`;
- installs `AF_Claude_Agent/requirements.txt`;
- stores the key in `AF_Claude_Agent/.anthropic_api_key`;
- checks Docker; and
- prebuilds all Docker images that this repo can build, skipping existing
  images only when they already contain the Claude CLI.

If you do not want to prebuild every image, omit `--build-images`. The first
run of a container will build only the image it needs. If local images are
stale, use `bash setup.sh --build-images --force-rebuild-images` or run a
single container with `AGENTIC_FORCE_REBUILD_IMAGE=1`.

`AF_Claude_Agent/.anthropic_api_key` and `.venv/` are gitignored. You can also
use `export ANTHROPIC_API_KEY=...`; the environment variable wins over the file.

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
| `KEEP_SOURCE=1` | Keep source folders after completion. |
| `KEEP_CONTAINER=1` | Keep the Docker container after completion. |
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

## How It Works

1. `run_agentic.py` reads `AF_Claude_Agent/test_config.csv` and dispatches by
   test type: ID, OD, NIO, or TD.
2. The per-type shell script stages `AF_Claude_Agent/data/<container>/run_<NN>/`
   and reproduces the flaky failure.
3. `agentic_claude_cli.py` runs `claude -p` inside Docker with Bash/Read/Edit
   tools enabled.
4. Claude edits the staged project and self-verifies.
5. The driver captures a patch, restores a clean baseline, applies the patch,
   and runs `agentic_verify.py`.
6. Final verdict is written as `PASSED`, `FAILED`, or `INCOMPLETE`.

For implementation details, see `AF_Claude_Agent/agentic/HOW_TO_USE_CLAUDE_AGENT.md`.
