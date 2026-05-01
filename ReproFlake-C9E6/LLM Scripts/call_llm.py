#!/usr/bin/env python3
"""
call_llm.py — backend dispatcher

Picks call_llm_claude.py or call_llm_openai.py based on the second
positional argument. The shell scripts call this; users can also call
the per-backend scripts directly for debugging.

Usage:
    python call_llm.py <result_container> <claude|openai>

The chosen backend's main() runs in-process (no subprocess), so its
prints and exit code propagate directly.
"""

import sys


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <result_container> <claude|openai>", file=sys.stderr)
        sys.exit(1)

    result_container = sys.argv[1]
    backend = sys.argv[2]

    if backend == "claude":
        import call_llm_claude as backend_mod
        script_name = "call_llm_claude.py"
    elif backend == "openai":
        import call_llm_openai as backend_mod
        script_name = "call_llm_openai.py"
    else:
        print(f"ERROR: backend must be 'claude' or 'openai', got '{backend}'", file=sys.stderr)
        sys.exit(1)

    # Each backend's main() reads sys.argv[1] for the container.
    # Rewrite argv so backend_mod.main() sees the per-backend single-arg shape.
    sys.argv = [script_name, result_container]
    backend_mod.main()


if __name__ == "__main__":
    main()
