"""
agentic_config.py — central configuration for the agentic repair pipeline.

Edit this file to tune behaviour, set API keys, and choose model versions.
Every value here can also be overridden at run time via CLI flags or env vars
(env vars always take precedence over values set in this file):

  CLI flags:  --max-iterations, --model   on run_agentic.py / agentic_orchestrator.py
  Env vars:   AGENTIC_MAX_ITERATIONS, AGENTIC_MODEL, ANTHROPIC_API_KEY, OPENAI_API_KEY
"""

# ===========================================================================
# API KEYS
# Set your keys here so you don't need to export them in the shell.
# Leave as "" to rely on the corresponding environment variable instead.
# Environment variables always win over values set here.
# ===========================================================================

ANTHROPIC_API_KEY: str = ""   # "sk-ant-..."  — used by all claude-* models
OPENAI_API_KEY: str    = ""   # "sk-..."       — used by all gpt-* / openai models


# ===========================================================================
# MODEL VERSIONS
# Canonical model IDs for each provider. run_agentic.py resolves short
# aliases (e.g. "claude" → CLAUDE_MODELS["claude"]) using these dicts.
# Update the IDs here when a new model version is released.
# ===========================================================================

CLAUDE_MODELS: dict = {
    # short alias          → full Anthropic model ID
    "claude":              "claude-sonnet-4-6",   # default aliasx
    "claude-sonnet":       "claude-sonnet-4-6",
    "sonnet":              "claude-sonnet-4-6",
    "claude-opus":         "claude-opus-4-7",
    "opus":                "claude-opus-4-7",
    "haiku":               "claude-haiku-4-5-20251001",
}

OPENAI_MODELS: dict = {
    # short alias          → full OpenAI model ID
    "openai":              "gpt-4o",              # default OpenAI alias
    "gpt-4o":              "gpt-4o",
    "gpt-4o-mini":         "gpt-4o-mini",
    "gpt-4.1":             "gpt-4.1",
    "gpt-4.1-mini":        "gpt-4.1-mini",
}

# Default model used when --model is not passed on the CLI.
# Must be a key in CLAUDE_MODELS or a full Anthropic model ID.
DEFAULT_MODEL: str = "claude-sonnet-4-6"


# ===========================================================================
# ITERATION LIMITS
# ===========================================================================

MAX_ITERATIONS: int = 5
# Hard cap on submit_patch attempts per container run.
# The agent may call as many read-only context tools as it likes per
# iteration; this only counts the terminal "submit a fix" action.
# Typical range: 3–20.

MAX_TOOL_TURNS_PER_ITERATION: int = 10
# Maximum API round-trips within a single iteration before the agent must
# submit a patch. The last turn is reserved for submit_patch only, so a value
# of 10 means at most 9 exploratory turns before a forced patch turn.
# Guards against a runaway exploration loop.

VERIFY_PASS_RUNS: int = 100
# After a patch first passes, run the verification command this many more
# times before declaring success. All runs must pass — if any fail, Flaky/
# is restored and the agent is told the fix is still non-deterministic.
# Set to 1 to accept the first passing run as sufficient.


# ===========================================================================
# API CALL SETTINGS
# ===========================================================================

MAX_TOKENS: int = 16384
# Maximum completion tokens per API call.
# 16384 gives headroom for a submit_patch that carries a full-method
# replace_method body plus a unified diff plus prose; 8192 risked truncating
# large patch outputs mid-JSON, causing spurious apply failures.
# claude-sonnet-4-6 supports well beyond this, so the cap is safe to raise.

TEMPERATURE: float = 0
# Sampling temperature. 0.0 = deterministic (greedy).
# Values above 0.3 tend to produce noisier patches without quality gains.


# ===========================================================================
# TOOL OUTPUT
# ===========================================================================

TOOL_OUTPUT_MAX_CHARS: int = 32_000
# Per-tool-call output cap in characters. Results beyond this limit are
# truncated and a notice appended. Set to 32k so a full failure stack trace
# (extract_failure_from_log now returns up to ~300 lines) and complete method
# bodies from get_code reach the agent without being clipped. Still bounded so
# a single huge file can't blow the context window.
