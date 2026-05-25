#!/usr/bin/env python3
"""
agentic_orchestrator.py — parent dispatcher for the agentic repair loop.

This is a thin router. It parses the CLI, decides which provider backend to
use based on the --model id, and hands off to that backend's run(args):

    gpt-*, o1/o3/o4-*   -> agentic_orchestrator_openai.run
    claude-* (default)  -> agentic_orchestrator_anthropic.run

Only the chosen backend is imported, so a claude run does not require the
`openai` package and a gpt run does not require `anthropic`.

The actual loop, tool plumbing, and all artifacts are identical across
backends — see:
    orchestrator_common.py            shared, provider-neutral core
    agentic_orchestrator_anthropic.py Anthropic (Messages API) backend
    agentic_orchestrator_openai.py    OpenAI (Chat Completions) backend

The CLI and filename are unchanged from the original single-file orchestrator,
so the per-type run_agentic_*.sh scripts call this script exactly as before.

Usage:
    python3 agentic_orchestrator.py <container>
                                    [--docker-container NAME]
                                    [--max-iterations N]
                                    [--model claude-sonnet-4-6 | gpt-4o | ...]
                                    [--exclude-tools t1,t2]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import agentic_config  # type: ignore  # noqa: E402

DEFAULT_MODEL          = agentic_config.DEFAULT_MODEL
DEFAULT_MAX_ITERATIONS = agentic_config.MAX_ITERATIONS


def _resolve_model(alias: str) -> tuple[str, str]:
    """Resolve a model alias or id to (canonical_model_id, provider).

    Mirrors run_agentic.resolve_model so a direct invocation of this script
    (e.g. --model opus, --model openai) behaves the same as going through the
    dispatcher. Uses only agentic_config — no SDK import needed to decide.
    """
    key = (alias or "").strip().lower()

    # Config alias dicts first (keys and values are lower-case canonical ids).
    if key in agentic_config.CLAUDE_MODELS:
        return agentic_config.CLAUDE_MODELS[key], "anthropic"
    if key in agentic_config.OPENAI_MODELS:
        return agentic_config.OPENAI_MODELS[key], "openai"

    # Otherwise treat it as a full model id; infer provider from the prefix.
    if key.startswith(("gpt", "o1", "o3", "o4")):
        return alias, "openai"
    if key.startswith("claude"):
        return alias, "anthropic"

    # Unknown — assume Anthropic (matches run_agentic.resolve_model).
    return alias, "anthropic"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("container")
    ap.add_argument("--docker-container",
                    help="docker container name (default tm_<container>)")
    ap.add_argument("--max-iterations", type=int,
                    default=DEFAULT_MAX_ITERATIONS,
                    help=f"hard cap on submit_patch attempts "
                         f"(default {DEFAULT_MAX_ITERATIONS})")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"model ID or alias; routes to the matching provider "
                         f"backend (default: {DEFAULT_MODEL})")
    ap.add_argument("--exclude-tools", default="",
                    help="comma-separated tool names to remove from the "
                         "agent's toolset (e.g. get_flaky_example for "
                         "unclassified tests)")
    args = ap.parse_args()

    # Resolve any alias (e.g. "opus", "openai") to a canonical model id so the
    # backend passes a real model to the provider API, and route on the result.
    model_id, provider = _resolve_model(args.model)
    if model_id != args.model:
        print(f"[dispatch] model alias '{args.model}' -> '{model_id}' ({provider})")
    args.model = model_id

    if provider == "openai":
        import agentic_orchestrator_openai as backend  # noqa: E402
    else:
        import agentic_orchestrator_anthropic as backend  # noqa: E402
    backend.run(args)   # calls sys.exit with the verdict-based code


if __name__ == "__main__":
    main()
