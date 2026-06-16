#!/usr/bin/env bash
# Route B driver: run one ReproFlake OD container through FlakyDoctor + Claude
# inside the Illinois `testorder` Surefire environment, so even same-class OD
# pairs reproduce deterministically.
#
# Usage (from the FlakyDoctor root):
#   docker/run_in_container.sh <result_container> [extra run_reproflake args...]
#
# Examples:
#   docker/run_in_container.sh ormlitecore59309e5
#   docker/run_in_container.sh shardingsphereelasticjobelasticjoblitecore4b9afa4   # same-class — works under testorder
#   docker/run_in_container.sh ormlitecore59309e5 --skip-repair                    # reproduce only, no API cost
#
# What it does:
#   1. looks up the row in ../ReproFlake-C9E6/test_config.csv to read its Java version
#   2. builds the matching image (flakydoctor-od8 / flakydoctor-od11) once
#   3. runs the container as your host UID/GID with the repo bind-mounted, and
#      invokes  src/run_reproflake.py --testorder --container <id> --model Claude
set -euo pipefail

CONTAINER="${1:-}"
shift || true
if [[ -z "$CONTAINER" ]]; then
    echo "usage: $0 <result_container> [extra run_reproflake args...]" >&2
    exit 1
fi

# Resolve paths: this script lives in FlakyDoctor/docker/.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLAKYDOCTOR_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$FLAKYDOCTOR_DIR")"
TEST_CONFIG="${TEST_CONFIG:-$REPO_ROOT/ReproFlake-C9E6/test_config.csv}"
API_KEY_FILE="${API_KEY_FILE:-$HOME/.anthropic_api_key}"

[[ -f "$TEST_CONFIG" ]]   || { echo "test_config.csv not found at $TEST_CONFIG" >&2; exit 1; }
[[ -f "$API_KEY_FILE" ]]  || { echo "API key file not found at $API_KEY_FILE (set API_KEY_FILE=...)" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "docker not installed — see docker/README_routeB.md" >&2; exit 1; }

# Read the row's Java version (col 9) for the chosen od container (col 2).
JAVA_VER="$(awk -F, -v c="$CONTAINER" 'tolower($1)=="od" && $2==c {print $9; exit}' "$TEST_CONFIG")"
if [[ -z "$JAVA_VER" ]]; then
    echo "no od row with result_container == $CONTAINER in $TEST_CONFIG" >&2
    echo "tip: python3 src/run_reproflake.py --test-config '$TEST_CONFIG' --list" >&2
    exit 1
fi

case "$JAVA_VER" in
    11) IMAGE="flakydoctor-od11"; BASE="maven:3.8.6-openjdk-11" ;;
    8)  IMAGE="flakydoctor-od8";  BASE="maven:3.8.6-openjdk-8"  ;;
    *)  echo "row asks for Java $JAVA_VER; only 8 and 11 have a prebuilt image." >&2
        echo "build one: docker build -t flakydoctor-od$JAVA_VER --build-arg BASE=maven:3.8.6-openjdk-$JAVA_VER -f docker/Dockerfile.flakydoctor_od ." >&2
        exit 1 ;;
esac

echo "[routeB] container=$CONTAINER  java=$JAVA_VER  image=$IMAGE"

# Build the image once (idempotent: skip if it already exists).
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "[routeB] building $IMAGE from $BASE (one-time; clones + builds the Illinois Surefire) ..."
    docker build -t "$IMAGE" --build-arg "BASE=$BASE" \
        -f "$FLAKYDOCTOR_DIR/docker/Dockerfile.flakydoctor_od" "$FLAKYDOCTOR_DIR"
fi

# Run as the host user so projects/ and outputs/ are owned by you (not root),
# and so git inside the bind-mounted repo sees a matching owner (no "dubious
# ownership"). HOME is set to a writable tmp dir for the non-root user.
docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp/fdhome \
    -e ANTHROPIC_API_KEY="$(cat "$API_KEY_FILE")" \
    -v "$REPO_ROOT":/work \
    -w /work/FlakyDoctor \
    "$IMAGE" \
    bash -lc 'mkdir -p /tmp/fdhome && python3 -u src/run_reproflake.py \
        --test-config "'"$(realpath --relative-to="$FLAKYDOCTOR_DIR" "$TEST_CONFIG")"'" \
        --container "'"$CONTAINER"'" \
        --testorder \
        --model Claude \
        --api-key "$ANTHROPIC_API_KEY" '"$*"
