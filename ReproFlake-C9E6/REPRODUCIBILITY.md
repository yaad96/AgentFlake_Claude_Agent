# Reproducibility Guide

This guide describes the procedure for reproducing any flaky-test container
listed in [test_config.csv](test_config.csv) on a host that has only Docker
installed. Container `jnrposixd9f3f84` is used as a worked example throughout;
the same procedure applies to every other entry in the CSV.

---

## 1. Overview

The pipeline collects runtime-verification (RV) traces from a deterministic-passing
run and a deterministic-failing run of a flaky test, supplies the traces and the
relevant source code to an LLM (Claude and/or OpenAI), applies the patch returned
by the LLM, and re-runs the test to verify the fix. Each `(backend, run)`
combination is executed `N` times to measure the repair success rate.

The CSV row used as the worked example is:

| field | value |
|---|---|
| `result_container` | `jnrposixd9f3f84` |
| `test_type` | `od` (order-dependent) |
| `module` | `.` |
| `polluter` | `jnr.posix.EnvTest#testSetenvOverwrite` |
| `victim`   | `jnr.posix.GroupTest#getgroups` |
| `java` | 8 |
| `url` | `https://zenodo.org/records/18605131/files/jnrposixd9f3f84.zip` (~124 MB) |

The `test_type` field selects the orchestrator script (`od` →
[run_od_tracemop.sh](TraceMop%20Scripts/run_od_tracemop.sh); `td`, `id`, and
`nio` exist for the other types). The `java` field selects the Docker image.
Both are resolved from the CSV automatically by
[run_pass_at_k.py](TraceMop%20Scripts/run_pass_at_k.py); only the
`result_container` value is supplied on the command line.

---

## 2. Prerequisites

### 2.1 Already required

- **Docker** (Docker Desktop on macOS, or `dockerd` on Linux) must be running
  and reachable; `docker info` must succeed without `sudo`.
- **Approximately 15 GB of free disk** is required for the dataset (~124 MB),
  the Docker base image and the in-image Surefire fork build (~3 GB), and the
  per-run archives (~150 MB × `N` runs × number of models).

### 2.2 Also needed on the host

The orchestrator runs on the host in Python and delegates JVM and Maven work
to the container. The host therefore requires the following tools and
credentials:

| Tool | Purpose |
|---|---|
| `python3` (≥ 3.8) with `pip` and `venv` | runs `run_pass_at_k.py` and the LLM scripts |
| `anthropic` PyPI package | Claude backend |
| `openai` PyPI package | OpenAI backend |
| `unzip` | unzips the dataset archive |
| `patch` | applies `Fixed.patch` |
| `curl` *(or `wget`)* | downloads the dataset archive on first use |
| `git` | clones the repository |
| `ANTHROPIC_API_KEY` *and* `OPENAI_API_KEY` | the LLM step is mandatory; the pipeline aborts when the key for the selected backend is unset |

All remaining build-time dependencies — JDK 8/11, Maven 3.8.6, the
TestingResearchIllinois Surefire fork (`3.0.0-M8-SNAPSHOT`), `xmlstarlet`,
`beautifulsoup4`, and `lxml` — are baked into the Docker image and require no
host-side installation.

---

## 3. Install the host tools

Python dependencies are installed into a virtual environment. System-wide
installation via `pip install --user` is not used: Python 3.12 and later
(including Homebrew Python on macOS and the system Python on Ubuntu 24.04+
and Debian 12+) enforce PEP 668 and reject such installations with
`error: externally-managed-environment`.

### 3.1 macOS

`unzip`, `patch`, `curl`, `git`, and `python3` are provided by the Xcode
Command Line Tools. If these are not present, the first command below
triggers an installation prompt:

```bash
xcode-select --install                   # one-time; installs unzip, patch, curl, git, python3
python3 --version                        # verify >= 3.8

python3 -m venv ~/.venvs/reproflake
source ~/.venvs/reproflake/bin/activate
pip install --upgrade pip
pip install anthropic openai
```

Alternatively, install Python via Homebrew:

```bash
brew install python git
python3 -m venv ~/.venvs/reproflake
source ~/.venvs/reproflake/bin/activate
pip install --upgrade pip
pip install anthropic openai
```

### 3.2 Ubuntu / Debian

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv unzip patch curl git

python3 -m venv ~/.venvs/reproflake
source ~/.venvs/reproflake/bin/activate
pip install --upgrade pip
pip install anthropic openai
```

For other Linux distributions, install the equivalent of `python3 python3-pip
python3-venv unzip patch curl git` from the system package manager, then
create and activate the virtual environment as shown above.

> The virtual environment must be active in the current shell each time the
> orchestrator is invoked. If it is not active, the LLM scripts abort with
> `ModuleNotFoundError: No module named 'anthropic'` (or `'openai'`).

### 3.3 API keys

Set both API keys in the current shell:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```

These exports apply only to the current terminal session. When the shell is
closed the keys are lost, and subsequent pipeline invocations abort with
`ERROR: ANTHROPIC_API_KEY env var not set`. To make the keys persistent,
append the same export lines to the shell's startup file so that every new
terminal inherits them.

**macOS (zsh, the default shell since Catalina):**

```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.zshrc
echo 'export OPENAI_API_KEY=sk-...'        >> ~/.zshrc
source ~/.zshrc
```

**Linux (bash, the default shell on most distributions):**

```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.bashrc
echo 'export OPENAI_API_KEY=sk-...'        >> ~/.bashrc
source ~/.bashrc
```

To determine the active shell, run `echo $SHELL`: `/bin/zsh` indicates
`~/.zshrc`; `/bin/bash` indicates `~/.bashrc`. Confirm that the keys are
visible to a fresh shell by opening a new terminal and running
`echo "$ANTHROPIC_API_KEY"`; the configured value should be printed.

### 3.4 Sanity check

Verify the host environment with the following four commands:

```bash
docker info >/dev/null && echo "docker OK"
python3 -c "import anthropic, openai; print('python deps OK')"
unzip -v >/dev/null && patch --version >/dev/null && curl --version >/dev/null \
  && echo "shell tools OK"
[[ -n "$ANTHROPIC_API_KEY" ]] && [[ -n "$OPENAI_API_KEY" ]] && echo "API keys OK"
```

Each command must print its `OK` message. The Python check requires the
virtual environment to be active in the current shell.

---

## 4. Clone the repository

The orchestrator reads three artifacts located one directory above
`ReproFlake-C9E6/`: `../experiments/tracemop.jar`,
`../scripts/javamop-extension/`, and `../scripts/events_encoding_id.txt`
(see [run_od_tracemop.sh:88-90](TraceMop%20Scripts/run_od_tracemop.sh#L88-L90)).
The full repository must therefore be cloned; cloning only the
`ReproFlake-C9E6/` subdirectory is not sufficient.

```bash
git clone <repo-url>
cd <cloned-dir>/ReproFlake-C9E6

# Verify the host-side artifacts read by the orchestrator
ls -l ../experiments/tracemop.jar             # ~19 MB
ls -l ../scripts/javamop-extension/pom.xml
ls -l ../scripts/events_encoding_id.txt
```

All three artifacts are checked into the repository, so a complete clone is
sufficient and no additional downloads are required. If any of the three is
missing, the pipeline aborts at step 3 or 4.

---

## 5. Run a container

From `<cloned-dir>/ReproFlake-C9E6/`, with the virtual environment active,
invoke the orchestrator:

```bash
source ~/.venvs/reproflake/bin/activate    # if not already active in the current shell

# Worked example: jnrposix, both backends, 3 runs each
./TraceMop\ Scripts/run_pass_at_k.py jnrposixd9f3f84 \
    --rv-traces yes \
    --models claude,openai \
    --runs 3
```

To reproduce a different container, substitute its `result_container` value
from `test_config.csv`. All remaining parameters (`test_type`, `module`,
`java`, `polluter`, `victim`, dataset URL) are read from the CSV automatically.

### 5.1 Argument reference

| flag | meaning |
|---|---|
| *(positional)* | the `result_container` value from `test_config.csv` |
| `--rv-traces yes\|no` | required. `yes` runs the full pipeline, including the RV trace section in the LLM prompt; output is archived under `data/FULL RUNS: RV/`. `no` runs the ablation that omits the RV section; output is archived under `data/FULL RUNS: NO RV/`. |
| `--models claude,openai` | comma-separated list of LLM backends to invoke. The default is `claude,openai`. Pass `claude` or `openai` alone to skip the other. |
| `--runs N` | number of runs per backend. The default is 3. |

### 5.2 First-run timing

The first invocation for any given `(test_type, java)` pair triggers a
one-time Docker image build that clones and compiles the Surefire fork
inside the container; this typically takes **5–10 minutes**. Subsequent
invocations reuse the cached image and start within seconds. The Zenodo
dataset archive is also downloaded on first use and cached in `data/`.

A complete `--models claude,openai --runs 3` invocation for `jnrposixd9f3f84`
takes approximately **20–40 minutes** end-to-end after the one-time setup is
complete.

### 5.3 Backends

Two LLM backends are supported:

- `claude` — Anthropic `claude-sonnet-4-6` ([call_llm_claude.py:36](LLM%20Scripts/call_llm_claude.py#L36)). Requires `ANTHROPIC_API_KEY`.
- `openai` — OpenAI `gpt-4o` ([call_llm_openai.py:37](LLM%20Scripts/call_llm_openai.py#L37)). Requires `OPENAI_API_KEY`.

Per-run token counts and wall-clock time are recorded in `summary.csv` and in
the top-level `Complete Containers Summary.csv`.

---

## 6. Result layout

All artifacts produced by an invocation are written under `data/`. The
per-container layout is:

```
ReproFlake-C9E6/
├── Complete Containers Summary.csv               # one row per (model, run); append-only across invocations
└── data/
    └── FULL RUNS: RV/                            # "FULL RUNS: NO RV/" when --rv-traces no is used
        └── jnrposixd9f3f84 runs/
            ├── summary.csv                       # per-run aggregate over every run on disk
            ├── Claude/
            │   ├── run 1/
            │   │   ├── pipeline.log              # complete stdout of the orchestrator
            │   │   ├── Steps Output Files/
            │   │   │   ├── llm_context.txt           # prompt sent to the LLM
            │   │   │   ├── llm_response.json         # diagnosis and patch
            │   │   │   ├── apply_report.json         # patch application report
            │   │   │   ├── verify_after_fix.log      # Surefire output from the post-fix test run
            │   │   │   └── verify_after_fix.verdict  # PASSED, FAILED, or INCOMPLETE
            │   │   ├── Fixed/, Flaky/             # source snapshots (target/ excluded)
            │   │   ├── traces-fixed/, traces-flaky/  # RV traces
            │   │   └── .run_complete              # sentinel containing exit_code and elapsed
            │   ├── run 2/
            │   └── run 3/
            └── OpenAI/
                ├── run 1/
                ├── run 2/
                └── run 3/
```

Three files address the most common questions:

- **Verdict for a single run** — `verify_after_fix.verdict` (one of `PASSED`,
  `FAILED`, or `INCOMPLETE`).
- **Aggregate across runs** — `summary.csv` (one row per run, importable into
  a spreadsheet).
- **Cross-container, cross-invocation log** — `Complete Containers Summary.csv`
  at the `ReproFlake-C9E6/` root. Join back to `test_config.csv` on the
  `container` column to retrieve victim FQN, polluter, and other container
  metadata.

---

## 7. Troubleshooting

The most common failure modes and their resolutions:

| Symptom | Resolution |
|---|---|
| `ERROR: Docker daemon not reachable` | Start Docker Desktop (macOS) or `dockerd` (Linux) and retry. |
| `ERROR: ANTHROPIC_API_KEY env var not set` (or `OPENAI_API_KEY`) | Export the API key for the backend specified in `--models`. |
| `ERROR: container '<name>' not in CSV` | Verify that `result_container` matches the CSV value exactly (for example, `jnrposixd9f3f84`, not `jnrposix`). |
| `ERROR: …/experiments/tracemop.jar not found` | The repository was cloned partially. Re-clone the full repository and run from `<cloned-dir>/ReproFlake-C9E6/`. |
| `ModuleNotFoundError: No module named 'anthropic'` (or `'openai'`) | The virtual environment is not active in the current shell. Run `source ~/.venvs/reproflake/bin/activate` and retry. |
| `error: externally-managed-environment` from `pip install` | The virtual environment was bypassed. Python 3.12 and later prohibit system-wide pip installs. Follow Section 3 to create and activate a virtual environment. |
| `ERROR: Flaky run had Failures=0, Errors=0` | TraceMOP failed to attach, or the Surefire fork was not honoured. Inspect `data/<container>/traces-flaky/mvn.log` for `[TraceMOP]` lines and for any `Changed surefire version to ...` warning. |
| All runs report verdict `INCOMPLETE` | The per-type script exited non-zero before writing a verdict; the orchestrator records `INCOMPLETE` in this case. Consult `pipeline.log` in the affected per-run directory to identify the underlying failure. |
| First invocation appears to hang for ~10 minutes with no output | Expected behaviour. The one-time Docker image build is in progress. Monitor progress with `docker images` from a separate terminal. |
| Pipeline runs but no LLM call is observed | Confirm that the API key was exported in the current shell and that `python3 -c "import anthropic"` (or `openai`) succeeds inside the active virtual environment. |

---

## 8. Summary of commands

The complete command sequence for a fresh host (worked example
`jnrposixd9f3f84`):

```bash
# (one-time) Install host tools
sudo apt-get install -y python3 python3-pip python3-venv unzip patch curl git    # macOS: xcode-select --install
python3 -m venv ~/.venvs/reproflake
source ~/.venvs/reproflake/bin/activate
pip install --upgrade pip
pip install anthropic openai

# (one-time) Clone the repository and export API keys
git clone <repo-url>
cd <cloned-dir>/ReproFlake-C9E6
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...

# Each new shell: re-activate the virtual environment
source ~/.venvs/reproflake/bin/activate

# Reproduce any container from test_config.csv (jnrposix shown)
./TraceMop\ Scripts/run_pass_at_k.py jnrposixd9f3f84 \
    --rv-traces yes --models claude,openai --runs 3

# Inspect the verdict and aggregate
cat "data/FULL RUNS: RV/jnrposixd9f3f84 runs/Claude/run 1/Steps Output Files/verify_after_fix.verdict"
open "data/FULL RUNS: RV/jnrposixd9f3f84 runs/summary.csv"
```
