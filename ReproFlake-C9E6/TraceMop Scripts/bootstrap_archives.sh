#!/usr/bin/env bash
# Downloads and extracts the FULL_RUNS_RV / FULL_RUNS_NO_RV archive bundle
# from Zenodo into ReproFlake-C9E6/data/. Idempotent — re-running is a
# no-op once data/.bundle_extracted exists. Delete that sentinel to force
# re-fetch.
set -euo pipefail

ZENODO_URL="https://zenodo.org/records/20099827/files/18_RV_17_NRV.zip?token=eyJhbGciOiJIUzUxMiJ9.eyJpZCI6IjlhODEwZjgzLWI1ZWEtNDY4ZS1iYjIzLTkzMWEzOWRhNzRkYyIsImRhdGEiOnt9LCJyYW5kb20iOiI1YjJmNjk1ZGM4Y2I1YTBhODI1OTFlYjU3NzUzMDQzNyJ9.SwGk1BCQ9_pyXYx_cPnB8ydG-1B2BoTwi_JwpkoxCobXLkIoIWXvKwzd0ssuQthkU0jubNu7WzYESVU-mUMYAQ&download=1"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA="$ROOT/data"
SENTINEL="$DATA/.bundle_extracted"

if [[ -f "$SENTINEL" ]]; then
  echo "[bootstrap] archives already extracted (rm $SENTINEL to re-fetch)"
  exit 0
fi

for tool in curl unzip; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: '$tool' not found on PATH" >&2
    exit 1
  fi
done

mkdir -p "$DATA"
TMP_ZIP="$DATA/.bundle.zip.partial"
TMP_DIR="$(mktemp -d "$DATA/.bundle.XXXXXX")"
trap 'rm -rf "$TMP_DIR" "$TMP_ZIP"' EXIT

echo "[bootstrap] downloading zipped containers from Zenodo (~1.3 GB)..."
curl -L --fail --progress-bar -o "$TMP_ZIP" "$ZENODO_URL"

echo "[bootstrap] unzipping..."
unzip "$TMP_ZIP" -d "$TMP_DIR" | awk '
  /^  inflating:|^   creating:|^ extracting:/ {
    n++
    if (n % 500 == 0) printf "\r[bootstrap] extracted %d files...", n
  }
  END { printf "\r[bootstrap] extracted %d files total.   \n", n }
'

for name in FULL_RUNS_RV FULL_RUNS_NO_RV; do
  if [[ ! -d "$TMP_DIR/$name" ]]; then
    echo "ERROR: '$name' not found at top level of bundle" >&2
    exit 1
  fi
  mkdir -p "$DATA/$name"
  shopt -s dotglob nullglob
  for child in "$TMP_DIR/$name"/*; do
    mv "$child" "$DATA/$name/"
  done
  shopt -u dotglob nullglob
done

touch "$SENTINEL"
echo "[bootstrap] done. Containers populated under data/FULL_RUNS_RV/ and data/FULL_RUNS_NO_RV/."
