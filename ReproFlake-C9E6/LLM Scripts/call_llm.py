#!/usr/bin/env python3
"""
call_llm.py — backend dispatcher

Picks call_llm_claude.py or call_llm_openai.py based on the second
positional argument. The shell scripts call this; users can also call
the per-backend scripts directly for debugging.

Usage:
    python call_llm.py <result_container> <claude|openai>
    python call_llm.py <result_container> <claude|openai> --feedback-from <file>

The chosen backend's main() runs in-process (no subprocess), so its
prints and exit code propagate directly. Extra trailing args (e.g.
`--feedback-from <file>`) are forwarded to the backend verbatim.
"""

import sys


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <result_container> <claude|openai> "
              f"[--feedback-from <feedback_file>]", file=sys.stderr)
        sys.exit(1)

    result_container = sys.argv[1]
    backend = sys.argv[2]
    extra_args = sys.argv[3:]   # forwarded to the backend (e.g. --feedback-from <file>)

    if backend == "claude":
        import call_llm_claude as backend_mod
        script_name = "call_llm_claude.py"
    elif backend == "openai":
        import call_llm_openai as backend_mod
        script_name = "call_llm_openai.py"
    else:
        print(f"ERROR: backend must be 'claude' or 'openai', got '{backend}'", file=sys.stderr)
        sys.exit(1)

    # Each backend's main() reads sys.argv[1] for the container, with
    # optional --feedback-from <file> at sys.argv[2:4]. Rewrite argv so
    # backend_mod.main() sees the per-backend shape it expects.
    sys.argv = [script_name, result_container] + extra_args
    backend_mod.main()


if __name__ == "__main__":
    main()
