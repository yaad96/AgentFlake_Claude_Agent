# Route B — FlakyDoctor + Claude in the original ReproFlake `testorder` environment

Route A (`src/run_reproflake.py` on the host with stock Maven) reproduces only
**cross-class** OD pairs deterministically; **same-class** pairs are a gamble
because stock Surefire cannot order methods within a class.

Route B runs the whole pipeline inside a Docker image that carries the **Illinois
`testorder` Surefire** (a Maven core extension) — exactly the environment
ReproFlake's `Dockerfile.od` uses. With it, `-Dtest=polluter,victim
-Dsurefire.runOrder=testorder` runs the two tests in that *exact* order, methods
included, so **same-class OD pairs reproduce deterministically** too.

The repair engine is unchanged: the container ends up calling
`src/flakydoctor.py --model Claude` (the original FlakyDoctor OD repair loop).

## One-time: install Docker (needs sudo — run this yourself)

Ubuntu 26.04:

```bash
sudo apt-get update
sudo apt-get install -y docker.io
sudo systemctl enable --now docker
# let your user run docker without sudo (log out / back in, or `newgrp docker`):
sudo usermod -aG docker "$USER"
newgrp docker
docker run --rm hello-world      # verify
```

## Run an OD container

From the FlakyDoctor root:

```bash
# reproduce + repair with Claude (key read from ~/.anthropic_api_key):
docker/run_in_container.sh ormlitecore59309e5

# a same-class pair — deterministic under testorder:
docker/run_in_container.sh shardingsphereelasticjobelasticjoblitecore4b9afa4

# reproduce only, no API cost:
docker/run_in_container.sh ormlitecore59309e5 --skip-repair
```

The wrapper:
1. reads the row's Java version from `../ReproFlake-C9E6/test_config.csv`,
2. builds `flakydoctor-od8` or `flakydoctor-od11` once (clones + builds the
   Illinois Surefire — a few minutes the first time),
3. runs the container **as your host UID/GID** with the repo bind-mounted, so
   `projects/` and `outputs/` end up owned by you, and invokes
   `src/run_reproflake.py --testorder --container <id> --model Claude`.

List the runnable OD rows:

```bash
python3 src/run_reproflake.py --test-config ../ReproFlake-C9E6/test_config.csv --list
```

## Knobs

- `API_KEY_FILE=/path/to/key docker/run_in_container.sh <id>` — different key file.
- `TEST_CONFIG=/path/to/test_config.csv docker/run_in_container.sh <id>` — different config.
- Build the images manually if you prefer:
  ```bash
  docker build -t flakydoctor-od8  --build-arg BASE=maven:3.8.6-openjdk-8  -f docker/Dockerfile.flakydoctor_od .
  docker build -t flakydoctor-od11 --build-arg BASE=maven:3.8.6-openjdk-11 -f docker/Dockerfile.flakydoctor_od .
  ```

## How this differs from Route A internally

`run_reproflake.py --testorder`:
- skips the stock alphabetical/reverse-alphabetical order *detection*,
- reproduces with a single `SUREFIRE_RUN_ORDER=testorder` run (gated on the
  victim failing, so no API spend on a polluter/platform failure),
- pins the project's staged offline `.m2` via **both** `MAVEN_ARGS` (Maven 3.9+)
  and `MAVEN_OPTS` (the image ships Maven 3.8.6, which ignores `MAVEN_ARGS`),
- resolves `JAVA_HOME` to the image's `/usr/local/openjdk-N` when the hardcoded
  Linux JDK path is absent.
