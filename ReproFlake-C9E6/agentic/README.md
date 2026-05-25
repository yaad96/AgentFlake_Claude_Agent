# Agentic Flaky-Test Repair Pipeline

Iterative, tool-use-driven repair for flaky tests.  
The agent reads test code, production code, error logs, and RV traces on demand, then proposes patches — retrying up to `MAX_ITERATIONS` times if a patch fails to compile or the test still fails.

## Prerequisites

```bash
# from ReproFlake-C9E6/
python3 -m pip install -r requirements.txt

# set the key for whichever backend you run:
export ANTHROPIC_API_KEY=sk-ant-...     # for claude-* models
export OPENAI_API_KEY=sk-...            # for gpt-* / o-series models
```

`requirements.txt` installs the LLM SDKs (`anthropic`, `openai`) plus
`tree-sitter` + `tree-sitter-java`. The tree-sitter packages are **optional**:
they enable the AST-based patch-application layer in `../LLM Scripts/apply_fix.py`
(more robust method splicing). Without them the applier falls back to its
regex/brace splicer and everything still works. At minimum you need the SDK for
the provider you use (`anthropic` for Claude, `openai` for GPT).

Docker must be running and the per-type image must exist — the `run_agentic_*.sh`
scripts auto-build it from the matching `Dockerfile*` if it isn't present.  
The container under test must have an entry in `test_config.csv`.

---

## Quickstart — central dispatcher

The simplest entry point. It reads the test type from `test_config.csv` automatically and routes to the correct script.

```bash
python3 run_agentic.py <container> [--models claude] [--runs 3] [--max-iterations 10]
```

### Examples

```bash
# Single run with the default model (claude-sonnet-4-6)
python3 run_agentic.py C9E6-apache-commons-collections-od-1

# 5 runs, pass@5 evaluation
python3 run_agentic.py C9E6-apache-commons-collections-od-1 --runs 5

# Compare two models side-by-side
python3 run_agentic.py C9E6-apache-commons-collections-od-1 \
  --models claude,claude-opus --runs 3
```

### Model aliases

| Alias | Resolves to |
|---|---|
| `claude` *(default)* | `claude-sonnet-4-6` |
| `claude-sonnet` | `claude-sonnet-4-6` |
| `claude-opus` | `claude-opus-4-7` |
| `opus` | `claude-opus-4-7` |
| `haiku` | `claude-haiku-4-5-20251001` |
| Any full model ID | passed through unchanged |

### Supported test types

The dispatcher handles all types in `test_config.csv`:

| CSV `test_type` | Script invoked |
|---|---|
| `od` | `run_agentic_od.sh` |
| `td` | `run_agentic_td.sh` |
| `id` | `run_agentic_id.sh` |
| `nio` | `run_agentic_nio.sh` |
| `unclassified` / `Unclassified` | `run_agentic_unclassified.sh` |
| `brittle` / `britle` *(CSV typo)* | `run_agentic_brittle.sh` |

---

## Run a single container directly

If you prefer to call a per-type script yourself:

```bash
./run_agentic_od.sh           <result_container>
./run_agentic_td.sh           <result_container>
./run_agentic_id.sh           <result_container>
./run_agentic_nio.sh          <result_container>
./run_agentic_unclassified.sh <result_container>
./run_agentic_brittle.sh      <result_container>
```

Output lands in `data/<result_container>/Steps Output Files/`.

---

## Pass@k evaluation (multiple runs, one model)

```bash
python3 run_agentic_pass_at_k.py <container> [options]
```

| Flag | Default | Description |
|---|---|---|
| `--runs` | 3 | Number of independent runs (k) |
| `--max-iterations` | 10 | `submit_patch` attempts per run |
| `--model` | `claude-sonnet-4-6` | Anthropic model ID or alias |
| `--keep-workspace` | off | Keep per-run data directories |

---

## Configuration

Edit [`agentic_config.py`](agentic_config.py) to change defaults without touching any script:

| Setting | Default | What it controls |
|---|---|---|
| `MAX_ITERATIONS` | 10 | Hard cap on patch attempts per run |
| `MAX_TOOL_TURNS_PER_ITERATION` | 25 | Max context-tool calls per attempt |
| `DEFAULT_MODEL` | `claude-sonnet-4-6` | Anthropic model |
| `MAX_TOKENS` | 8192 | Max completion tokens per API call |
| `TEMPERATURE` | 0.0 | Sampling temperature |
| `TOOL_OUTPUT_MAX_CHARS` | 16000 | Per-tool output truncation limit |

All settings can also be overridden at run time:

```bash
# via the central dispatcher
python3 run_agentic.py <container> --models claude-opus --max-iterations 5

# via environment variables (picked up by the shell scripts)
AGENTIC_MAX_ITERATIONS=5 AGENTIC_MODEL=claude-opus-4-7 ./run_agentic_od.sh <container>

# via CLI flags on the orchestrator directly
python3 agentic_orchestrator.py <container> \
  --docker-container tm_mycontainer \
  --max-iterations 5 \
  --model claude-opus-4-7
```

---

## Unclassified and Brittle tests

### Unclassified

Tests whose root cause is unknown. The pipeline:
- Runs `mvn test -Dtest=<victim>` to capture the failure log (no TraceMOP traces).
- Calls the agent **without** the `get_flaky_example` tool (no category = no exemplar).
- The agent diagnoses from test code, production code, and error logs alone.

### Brittle

Structurally identical to OD tests (a polluter corrupts shared state before the victim). The full TraceMOP trace pipeline runs exactly as in `run_agentic_od.sh`, including the trace diff and `get_flaky_example`. The only difference is the type check accepts `brittle`/`britle`.

---

## Output files

All written to `data/<result_container>/Steps Output Files/`:

| File | Description |
|---|---|
| `verify_after_fix.verdict` | Final result: `PASSED`, `FAILED`, or `INCOMPLETE` |
| `agentic_iterations.jsonl` | One JSON line per patch attempt — verdict, tools used, timing |
| `agentic_conversation.json` | Full agent conversation transcript |
| `llm_response.json` | Last `submit_patch` payload + cumulative token usage |
| `apply_report.json` | Patch application result from `apply_fix.py` |
| `verify_after_fix.log` | Raw Maven output from the final verification run |
| `llm_context.txt` | Initial prompt sent to the agent (for auditing) |
| `step_8_C_official.txt` | TraceMOP trace diff (OD/TD/ID/NIO/Brittle only) |
| `llm_trace_summary.txt` | Human-readable trace summary (OD/TD/ID/NIO/Brittle only) |

Pass@k archive layout:

```
data/AGENTIC_FULL_RUNS/<container> runs/
  <model-id>/
    run 1/
      Steps Output Files/
      pipeline.log
      .run_complete
    run 2/ ...
  summary.csv
```

---

## How it works (short version)

1. **`run_agentic.py`** reads the CSV, resolves the model alias, and calls `run_agentic_pass_at_k.py` once per model.
2. The pass@k harness calls the per-type shell script `--runs` times, archiving each run.
3. Each per-type script runs TraceMOP traces (or a basic test run for unclassified), generates the trace diff, and snapshots `Flaky/` → `Flaky.pristine`.
4. **`agentic_orchestrator.py`** is called with the container and docker container names.
5. The agent receives a minimal initial prompt (category + victim + failure log) and calls read-only tools (`get_test_code`, `get_code`, `get_error_logs`, `get_rv_trace_diff`, and `get_flaky_example` unless excluded) to gather evidence.
6. When ready, it calls `submit_patch` with a unified diff and structured fallback.
7. The orchestrator applies the patch, recompiles, and reruns the failing test inside docker.
8. On failure, `Flaky/` is restored from `Flaky.pristine`, the failure report is fed back into the conversation (full history is preserved), and the agent tries again.
9. The loop ends on `PASSED` or when `MAX_ITERATIONS` is reached.
