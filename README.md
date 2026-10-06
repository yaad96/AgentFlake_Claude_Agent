# AgentFlake Claude Agent

AgentFlake Claude Agent repairs ID, OD, NIO and TD flaky Java tests with the
Claude Code CLI. Its containerized runner reproduces a flake inside Docker, asks
Claude Code to edit the project, verifies the captured patch from a clean
baseline, and archives the full run under `AF_Claude_Agent/data/<test>/run_<NN>/`.

## Requirements

- Docker installed and running (all builds and tests happen inside the container).
- An Anthropic API key.
- Linux and macOS are supported. The host needs `bash`, `python3` and `docker`;
  the JDK/Maven toolchain and the Claude Code CLI live in the Docker images, which
  the runner builds on first use.

## Setup

Inside the `AF_Claude_Agent/` directory, create a file `.anthropic_api_key` and
store your API key there. The key is read from that file during a run. The file
is git-ignored, so it is safe. Then run `bash setup.sh` from the repository root
to create `.venv` and install the Python dependencies.

## Basic Run

The runner auto-detects the test type from `test_config.csv`, so the same command
handles all four flaky-test categories. From the repository root, pass the test
name from the `result_container` column:

```bash
.venv/bin/python AF_Claude_Agent/agentic/run_agentic.py <test> --runs 1 --models claude --max-iterations 10
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
  crane4jcrane4jcoreb73311aget \
  --runs 1 --models claude --max-iterations 10
```

Run data for this test is in `AF_Claude_Agent_Data.zip/ID/crane4jcrane4jcoreb73311aget`.

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

| Option | Purpose |
|---|---|
| `--runs N` | Independent runs for pass@k, which counts a test as repaired if at least one of the N independently sampled runs yields a verified fix. |
| `--models claude,opus,haiku` | One or more Claude models. |
| `--max-iterations N` | Max Claude Code turns per run. |
| `--cli-timeout-s 2400` | Wall-clock cap for Claude Code. |
| `--verify-pass-runs 10` | Extra passing verification runs required after the first pass. |

## Output

Each run is archived under the following directory:

```text
AF_Claude_Agent/data/<test>/run_<NN>/
  claude_inputs/              # Claude Code prompts and the test's run configuration
    prompt_user.txt
    prompt_system.txt
    trace_config.json
  claude_outputs/             # Claude Code transcript, patch and verification results
    trial.ndjson
    claude.stderr
    tool_calls.jsonl
    usage.json
    patch.diff
    llm_response.json
    apply_report.json
    verify_after_fix.log
    verify_after_fix.verdict
    meta.json                 # verdict, model, token usage
  pipeline.log                # full run log
  .run_complete
```

The verdict in `verify_after_fix.verdict` is `PASSED` or `FAILED`.

Summaries are written to:

```text
AF_Claude_Agent/data/<test>/summary.csv
AF_Claude_Agent/Complete_Containers_Summary.csv
```

All run data is available in `AF_Claude_Agent_Data.zip`, covering 41 OD tests, 41 ID tests, 41 TD tests and 41 NIO tests.
