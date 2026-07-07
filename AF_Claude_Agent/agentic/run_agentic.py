#!/usr/bin/env python3
"""
run_agentic.py — central dispatcher for the agentic flaky-test repair pipeline.

Reads the test type from test_config.csv and routes to the correct per-type
shell script via run_agentic_pass_at_k.py. Supports multiple models in a
single invocation; each model runs independently and archives to its own
subdirectory.

Usage:
    python3 run_agentic.py <container> [--models claude] [--runs 3]
                                       [--max-iterations 10]

    # multiple models in one shot:
    python3 run_agentic.py <container> --models claude,claude-opus --runs 3

Model aliases are defined in agentic_config.py (CLAUDE_MODELS).
Common aliases:
    claude / claude-sonnet  ->  claude-sonnet-4-6   (default)
    claude-opus / opus      ->  claude-opus-4-7
    haiku                   ->  claude-haiku-4-5-20251001
    Any full model ID is passed through unchanged.

ANTHROPIC_API_KEY is read from the environment first, then AF_Claude_Agent/.anthropic_api_key via agentic_config.py.
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR     = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
CSV_FILE       = REPROFLAKE_DIR / "test_config.csv"
PASS_AT_K      = SCRIPT_DIR / "run_agentic_pass_at_k.py"

sys.path.insert(0, str(SCRIPT_DIR))
import agentic_config  # type: ignore  # noqa: E402

SUPPORTED_TYPES = {"od", "td", "id", "nio"}


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

def resolve_model(alias: str) -> tuple[str, str]:
    """Return (canonical_model_id, provider) for a Claude alias or model ID."""
    key = alias.lower()

    if key in agentic_config.CLAUDE_MODELS:
        return agentic_config.CLAUDE_MODELS[key], "anthropic"
    if key.startswith("claude"):
        return alias, "anthropic"

    sys.exit(
        "ERROR: Claude CLI mode supports only Claude models. "
        f"Got '{alias}'."
    )


def get_api_key(provider: str = "anthropic") -> tuple[str, str]:
    """Return (api_key, source) for the Claude CLI backend."""
    env_val = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    config_val = (agentic_config.ANTHROPIC_API_KEY or "").strip()
    if env_val:
        return env_val, "env"
    if config_val:
        return config_val, "config"
    return "", ""


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_csv_row(container: str) -> dict | None:
    if not CSV_FILE.is_file():
        sys.exit(f"ERROR: CSV not found: {CSV_FILE}")
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("result_container", "").strip() == container:
                return row
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Central dispatcher for the agentic flaky-test repair pipeline.",
    )
    ap.add_argument("container",
                    help="result_container name from test_config.csv")
    ap.add_argument("--models", default="claude",
                    help="comma-separated model names/IDs (default: claude). "
                         "Example: claude,claude-opus")
    ap.add_argument("--runs", type=int, default=3,
                    help="independent runs per model for pass@k (default 3)")
    ap.add_argument("--max-iterations", type=int,
                    default=agentic_config.MAX_ITERATIONS,
                    help=f"Claude Code max turns per run "
                         f"(default from config: {agentic_config.MAX_ITERATIONS})")
    ap.add_argument("--keep-workspace", action="store_true",
                    help="keep the docker container after each batch; run folders are always kept")
    args = ap.parse_args()

    # ---- validate container ----
    row = load_csv_row(args.container)
    if not row:
        sys.exit(f"ERROR: container '{args.container}' not found in {CSV_FILE.name}")
    test_type = row.get("test_type", "").strip().lower()
    if test_type not in SUPPORTED_TYPES:
        sys.exit(f"ERROR: unsupported test_type '{test_type}' for container "
                 f"'{args.container}'.\n"
                 f"       Supported: {', '.join(sorted(SUPPORTED_TYPES))}")

    victim   = row.get("flaky_test", "").strip()
    polluter = row.get("polluter/state setter", "").strip()
    java_ver = row.get("java", "").strip()
    print(f"[dispatcher] container   = {args.container}")
    print(f"[dispatcher] test_type   = {test_type}")
    print(f"[dispatcher] victim      = {victim}")
    if polluter:
        print(f"[dispatcher] polluter    = {polluter}")
    print(f"[dispatcher] java        = {java_ver}")

    # ---- resolve models and check keys ----
    raw_models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not raw_models:
        sys.exit("ERROR: --models cannot be empty")

    resolved: list[tuple[str, str, str]] = []  # (alias, model_id, provider)
    for alias in raw_models:
        model_id, provider = resolve_model(alias)

        api_key, source = get_api_key(provider)
        if not api_key:
            sys.exit(f"ERROR: No ANTHROPIC_API_KEY found for '{model_id}'.\n"
                     "       Set ANTHROPIC_API_KEY in agentic_config.py or export it as "
                     "an environment variable.")

        resolved.append((alias, model_id, provider))
        key_display = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
        print(f"[dispatcher] model       = {alias} -> {model_id}  "
              f"(key from {source}: {key_display})")

    if not resolved:
        sys.exit("ERROR: no valid models to run after resolution.")

    print(f"[dispatcher] runs        = {args.runs}")
    print(f"[dispatcher] max-iters   = {args.max_iterations}")
    print()

    # ---- dispatch once per model ----
    exit_codes: dict[str, int] = {}
    for alias, model_id, provider in resolved:
        print(f"{'='*60}")
        print(f"[dispatcher] Starting model: {model_id}  runs={args.runs}")
        print(f"{'='*60}")

        cmd = [
            sys.executable, str(PASS_AT_K),
            args.container,
            "--runs",           str(args.runs),
            "--max-iterations", str(args.max_iterations),
            "--model",          model_id,
        ]
        if args.keep_workspace:
            cmd.append("--keep-workspace")

        # Inject API key into the subprocess environment so the shell scripts
        # and orchestrator see it even if it was only set in agentic_config.py.
        env = os.environ.copy()
        api_key, _ = get_api_key(provider)
        env["ANTHROPIC_API_KEY"] = api_key

        proc = subprocess.run(cmd, env=env)
        exit_codes[model_id] = proc.returncode
        status = "OK" if proc.returncode == 0 else f"exit={proc.returncode}"
        print(f"\n[dispatcher] {model_id}: {status}\n")

    # ---- final summary ----
    print(f"{'='*60}")
    print("[dispatcher] All models done.")
    for model_id, rc in exit_codes.items():
        status = "PASSED (≥1 run)" if rc == 0 else "no run passed"
        print(f"  {model_id:40s}  {status}")
    print(f"{'='*60}")

    if any(rc != 0 for rc in exit_codes.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
