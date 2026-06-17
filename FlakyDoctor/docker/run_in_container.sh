#!/usr/bin/env bash
# Run one ReproFlake OD container through FlakyDoctor + Claude
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
command -v docker >/dev/null 2>&1 || { echo "docker not installed — see docker/README_reproflake_od.md" >&2; exit 1; }

# Read the row's test_type (col 1) and Java version (col 9) for the container (col 2).
ROW_INFO="$(awk -F, -v c="$CONTAINER" '$2==c {print tolower($1)"|"$9; exit}' "$TEST_CONFIG")"
if [[ -z "$ROW_INFO" ]]; then
    echo "no row with result_container == $CONTAINER in $TEST_CONFIG" >&2
    echo "tip: src/run_reproflake.py --list (OD)  |  src/run_reproflake_id.py --list (ID)" >&2
    exit 1
fi
TEST_TYPE="${ROW_INFO%%|*}"
JAVA_VER="${ROW_INFO##*|}"

# OD and ID share the same image. Pick the driver + mode flags by test_type.
# For ID we must DISABLE the Illinois testorder Surefire extension: it lives in
# /usr/share/maven/lib/ext and forces maven-surefire-plugin:3.0.0-M8-SNAPSHOT into
# every build, which the ID container's offline .m2 doesn't have (ID is built with
# NonDex, not testorder) -> build failure. An empty tmpfs over that dir hides the
# extension so Maven uses the project's own surefire. OD keeps it (testorder needed).
EXTRA_RUN_ARGS=()
case "$TEST_TYPE" in
    od) DRIVER="src/run_reproflake.py";    MODE_ARGS="--testorder" ;;
    id) DRIVER="src/run_reproflake_id.py"; MODE_ARGS=""
        EXTRA_RUN_ARGS+=(--tmpfs /usr/share/maven/lib/ext) ;;
    *)  echo "unsupported test_type '$TEST_TYPE' for $CONTAINER (only od and id are wired)" >&2; exit 1 ;;
esac

# JDK 8/11 use the openjdk base + the testorder Surefire. JDK 17 has no
# maven:3.8.6-openjdk-17 tag and the old Surefire fork doesn't compile on 17, so
# it uses the temurin-17 base built WITHOUT testorder (WITH_TESTORDER=false) —
# fine for ID (NonDex), but OD on 17 would have no testorder (none in scope here).
WITH_TESTORDER="true"
case "$JAVA_VER" in
    17) IMAGE="flakydoctor-od17"; BASE="maven:3.8.6-eclipse-temurin-17"; WITH_TESTORDER="false" ;;
    11) IMAGE="flakydoctor-od11"; BASE="maven:3.8.6-openjdk-11" ;;
    8)  IMAGE="flakydoctor-od8";  BASE="maven:3.8.6-openjdk-8"  ;;
    *)  echo "row asks for Java $JAVA_VER; only 8, 11, 17 are wired." >&2
        exit 1 ;;
esac
if [[ "$TEST_TYPE" == "od" && "$WITH_TESTORDER" == "false" ]]; then
    echo "OD on Java $JAVA_VER needs the testorder Surefire, which isn't built for this JDK." >&2
    exit 1
fi

echo "[reproflake] container=$CONTAINER  type=$TEST_TYPE  java=$JAVA_VER  image=$IMAGE  driver=$DRIVER"

# Build the image once (idempotent: skip if it already exists).
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "[reproflake] building $IMAGE from $BASE (one-time, WITH_TESTORDER=$WITH_TESTORDER) ..."
    docker build -t "$IMAGE" --build-arg "BASE=$BASE" --build-arg "WITH_TESTORDER=$WITH_TESTORDER" \
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
    ${EXTRA_RUN_ARGS[@]+"${EXTRA_RUN_ARGS[@]}"} \
    "$IMAGE" \
    bash -lc 'mkdir -p /tmp/fdhome && python3 -u '"$DRIVER"' \
        --test-config "'"$(realpath --relative-to="$FLAKYDOCTOR_DIR" "$TEST_CONFIG")"'" \
        --container "'"$CONTAINER"'" \
        '"$MODE_ARGS"' \
        --model Claude \
        --api-key "$ANTHROPIC_API_KEY" '"$*"
