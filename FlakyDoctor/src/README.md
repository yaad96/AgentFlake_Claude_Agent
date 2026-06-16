# SRC

File structures in `src` are as follows:
```
src/
# commands to run some neccessary tools
├── cmds 
│   ├── checkout_project.sh
│   ├── run_nondex.sh
│   ├── run_surefire.sh
│   └── stash_project.sh
# main code of FlakyDoctor
├── flakydoctor.py 
├── install.sh
├── operate_patch.py
├── parse_nondex.py
├── process_line.py
├── repair_ID.py
├── repair_OD.py
├── run_FlakyDoctor.sh
# driver: run a ReproFlake OD container (test_config.csv) end-to-end with Claude
├── run_reproflake.py
├── setup.sh
├── stitching.py
├── update_pom.py
├── utils
│   ├── java_dependencies.json
│   └── java_standard_libs.json
└── utils.py
```

To run FlakyDoctor, you need to specify the following options:
```
usage: flakydoctor.py [-h] --input-tests-csv INPUT_TESTS_CSV --flakiness-type FLAKINESS_TYPE --projects PROJECTS
                      --api-key OPENAI_KEY --model MODEL [--nondex-times NONDEX_TIMES] --output-dir OUTPUT_DIR
                      --output-result-csv OUTPUT_RESULT_CSV --output-result-json OUTPUT_RESULT_JSON
                      --output-details-json OUTPUT_DETAILS_JSON

options:
  -h, --help            show this help message and exit
  --input-tests-csv INPUT_TESTS_CSV
                        A csv file include flaky tests with consistent format as in IDoFT `pr-data.csv`.
  --flakiness-type FLAKINESS_TYPE
                        Flakiness type to fix, select one from [ID, OD].
  --projects PROJECTS   A directory path where you save all the Java projects.
  --api-key OPENAI_KEY  API key for the selected model (Anthropic key for Claude, OpenAI key for GPT-4).
                        --openai-key is kept as a deprecated alias.
  --model MODEL         LLM model to run, currently we support [GPT-4, MagiCoder, Claude].
  --nondex-times NONDEX_TIMES
                        How many times you want to nondex to rerun.
  --output-dir OUTPUT_DIR
                        A directory to save all the outputs.
  --output-result-csv OUTPUT_RESULT_CSV
                        A csv to save summary of results.
  --output-result-json OUTPUT_RESULT_JSON
                        A json to save summary of results.
  --output-details-json OUTPUT_DETAILS_JSON
                        A json to save details of results.
```

---

# Running a ReproFlake OD container with FlakyDoctor + Claude (Docker / `testorder`)

`flakydoctor.py` above is the repair engine. It expects each project to be **pre-staged
and pre-built** at `projects/<sha>/<project>`, reads its own `OD_inputs.csv` format
(`url,sha,module,victim.dotted,polluter.dotted`), and reproduces order with
`-Dsurefire.runOrder=testorder` — a capability that only exists in the **Illinois fork of
Surefire** (`TestingResearchIllinois/maven-surefire`). It is *not* present in stock Maven.

The ReproFlake dataset (`../ReproFlake-C9E6/test_config.csv`) is in a different shape:
Zenodo zip snapshots, `Class#method` test names, `od` rows. The setup below bridges that gap
and runs the whole thing inside a Docker image that carries the `testorder` Surefire, so that
**even same-class OD pairs reproduce deterministically** (stock Surefire can order classes
but not methods within one class — see `run_reproflake.py`'s docstring).

## Components

| Path | Role |
| --- | --- |
| `src/run_reproflake.py` | Bridges `test_config.csv` → FlakyDoctor's OD pipeline; `--testorder` mode forces the exact polluter→victim order. |
| `docker/Dockerfile.flakydoctor_od` | `maven:3.8.6-openjdk-{8,11}` + Illinois `testorder` Surefire + `git` + the Claude-only Python deps. Symlinks `/usr/lib/jvm/java-1.{8,11}.0-openjdk-amd64` → `/usr/local/openjdk-N` so the **unmodified** `cmds/run_surefire.sh` finds a valid JDK in-container. |
| `docker/run_in_container.sh` | One-command driver: picks the jdk8/jdk11 image from the row's Java version, builds it once, bind-mounts the repo, runs the `--testorder` pipeline. |
| `docker/README_reproflake_od.md` | Short companion guide. |

## One-time setup

Install Docker (needs sudo) and put your Anthropic key at `~/.anthropic_api_key`:

```bash
sudo apt-get update && sudo apt-get install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" && newgrp docker     # run docker without sudo
docker run --rm hello-world                          # verify
```

No host JDK/Maven is required — the toolchain lives in the image.

## How to run

From the **FlakyDoctor root** (`cd ~/Desktop/FlakyRV/FlakyDoctor`):

```bash
# 1. list the OD containers (yes = cross-class/easy, same-class = harder):
python3 src/run_reproflake.py --test-config ../ReproFlake-C9E6/test_config.csv --list

# 2. full Claude repair on one container (spends API tokens on ~/.anthropic_api_key):
docker/run_in_container.sh jnrposixd9f3f84

# 3. reproduce only, zero API cost:
docker/run_in_container.sh jnrposixd9f3f84 --skip-repair
```

If a fresh terminal hasn't picked up the `docker` group yet
(`permission denied ... docker.sock`), prefix the command:
`sg docker -c 'docker/run_in_container.sh jnrposixd9f3f84'`.

Override the defaults with env vars:
`API_KEY_FILE=/path/to/key` and `TEST_CONFIG=/path/to/test_config.csv`.

## What one run does

1. **stage** — download the Zenodo zip, unpack `Flaky/`→`projects/<zipbase>/<project>/`,
   `Flakym2/`, `Fixed.patch` (idempotent: skipped if already staged).
2. **git baseline** — `git init` + one commit (FlakyDoctor rolls files back between rounds).
3. **build** — `mvn install` with the container's offline `.m2` (pinned via `MAVEN_OPTS`
   *and* `MAVEN_ARGS`, because Maven 3.8.6 ignores `MAVEN_ARGS`).
4. **reproduce** — one `SUREFIRE_RUN_ORDER=testorder` run forcing polluter→victim; gated on
   the **victim** failing, so no API tokens are spent on a polluter/platform failure.
5. **repair** — `flakydoctor.py --flakiness-type OD --model Claude`: Claude proposes a patch,
   `mvn test` under `testorder` verifies, repeat for a few rounds.
6. **summary** — rounds, whether the victim reached `test_pass`, and the winning patch.

## Outputs

- `outputs/reproflake_<container>_<timestamp>/` — `results.csv`, `results.json`,
  `details.json` (full per-round Claude prompts/responses), and the patch files.
- `projects/<container>/Fixed.patch` — the developer's reference fix, for comparison.

## Notes & troubleshooting

- These two messages are **harmless** — the Maven base image's entrypoint trying to touch
  `/root/.m2` while the container runs as your non-root UID; it prints `Carrying on ...` and
  proceeds (the real repo is the staged `.m2`):
  ```
  Can not write to /root/.m2/copy_reference_file.log. Wrong volume permissions? Carrying on ...
  mkdir: cannot create directory '/root': Permission denied
  ```
- The container runs as your host UID/GID, so `projects/` and `outputs/` stay owned by you.
- Rows requesting Java other than 8/11 need their own image
  (`docker build -t flakydoctor-od<N> --build-arg BASE=maven:3.8.6-openjdk-<N> -f docker/Dockerfile.flakydoctor_od .`).
- Same-class pairs are the reason for the `testorder` image; cross-class pairs also work and are the
  easier wins. A pair can reproduce yet remain unfixed if the model doesn't converge on the
  right patch within the round budget — that is a model outcome, not a pipeline error.