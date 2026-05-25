#!/usr/bin/env python3
"""
agentic_orchestrator_anthropic.py — Anthropic backend for the agentic loop.

Runs the bounded-iteration tool-use loop against Anthropic's Messages API.
All provider-neutral logic (prompt construction, tool implementations, patch
apply/verify, failure formatting, run-summary, setup/teardown) lives in
orchestrator_common; this file adds only the Anthropic-specific pieces:
the client, the messages.create call, the tool_use/tool_result content-block
plumbing, and usage accounting.

Normally invoked via the parent dispatcher agentic_orchestrator.py. Can also
be run standalone:

    python3 agentic_orchestrator_anthropic.py <container> --model claude-sonnet-4-6

Requires:
    ANTHROPIC_API_KEY in env or agentic_config.ANTHROPIC_API_KEY
    pip install anthropic
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

try:
    from anthropic import Anthropic
except ImportError:
    print("ERROR: anthropic package not installed. Run: "
          "python3 -m pip install anthropic", file=sys.stderr)
    sys.exit(1)

import agentic_config         # type: ignore  # noqa: E402
import agent_tools            # type: ignore  # noqa: E402
import orchestrator_common as common  # type: ignore  # noqa: E402

DEFAULT_MODEL                = agentic_config.DEFAULT_MODEL
MAX_TOKENS                   = common.MAX_TOKENS
TEMPERATURE                  = common.TEMPERATURE
MAX_TOOL_TURNS_PER_ITERATION = common.MAX_TOOL_TURNS_PER_ITERATION
DEFAULT_MAX_ITERATIONS       = common.DEFAULT_MAX_ITERATIONS
TOOL_OUTPUT_MAX_CHARS        = common.TOOL_OUTPUT_MAX_CHARS
VERIFY_PASS_RUNS             = common.VERIFY_PASS_RUNS
SYSTEM_PROMPT                = common.SYSTEM_PROMPT


def is_anthropic_model(model: str) -> bool:
    """True if `model` should be handled by the Anthropic backend. Used by
    the parent dispatcher (which treats 'not OpenAI' as Anthropic)."""
    key = (model or "").strip().lower()
    if key in {v.lower() for v in agentic_config.CLAUDE_MODELS.values()}:
        return True
    if key in {k.lower() for k in agentic_config.CLAUDE_MODELS}:
        return True
    return key.startswith("claude")


# ---------------------------------------------------------------------------
# Anthropic-shaped helpers
# ---------------------------------------------------------------------------

def _usage_dict(response) -> dict:
    u = response.usage
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "total_tokens": u.input_tokens + u.output_tokens,
        "cache_read_input_tokens":
            getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens":
            getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def _extract_assistant_blocks(response):
    """Return the assistant's content blocks as plain dicts suitable to
    append back into the running messages list."""
    out = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            out.append({"type": "text", "text": block.text})
        elif getattr(block, "type", None) == "tool_use":
            out.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return out


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    ctx = common.prepare_run(args)

    # API key: env var takes precedence, then agentic_config.ANTHROPIC_API_KEY.
    api_key = (os.environ.get("ANTHROPIC_API_KEY", "")
               or getattr(agentic_config, "ANTHROPIC_API_KEY", "")).strip()
    if not api_key:
        sys.exit("ERROR: ANTHROPIC_API_KEY is not set.\n"
                 "       Set it in agentic_config.py or export it as an env var.")

    client = Anthropic(api_key=api_key)
    excluded_tools = {t.strip() for t in args.exclude_tools.split(",") if t.strip()}
    tools = [t for t in common.all_tool_schemas()
             if t["name"] not in excluded_tools]
    if excluded_tools:
        print(f"[init ] excluded tools: {sorted(excluded_tools)}")

    messages = [{"role": "user", "content": ctx.initial_user}]

    cumulative_usage = common.zero_usage()
    total_elapsed = 0.0
    submit_attempts = 0
    final_verdict = "INCOMPLETE"
    final_category = ""
    iter_summary_rows: list[dict] = []

    print(f"[init ] container={args.container}  test_type={ctx.test_type}  "
          f"provider=anthropic  model={args.model}  "
          f"max_iterations={args.max_iterations}")

    for attempt in range(1, args.max_iterations + 1):
        print(f"\n[iter {attempt}/{args.max_iterations}] ============")
        t_iter_start = time.time()
        iter_start_usage = dict(cumulative_usage)
        tool_turn = 0
        submitted_this_iter = False
        tools_used_this_iter: list[str] = []

        while tool_turn < MAX_TOOL_TURNS_PER_ITERATION:
            tool_turn += 1
            t0 = time.time()
            response = client.messages.create(
                model=args.model,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )
            elapsed = time.time() - t0
            total_elapsed += elapsed
            usage = _usage_dict(response)
            cumulative_usage = common.sum_usage(cumulative_usage, usage)

            print(f"[iter {attempt}/turn {tool_turn}] {elapsed:.1f}s  "
                  f"in={usage['input_tokens']} out={usage['output_tokens']}  "
                  f"cache_read={usage['cache_read_input_tokens']}  "
                  f"stop={response.stop_reason}")

            assistant_blocks = _extract_assistant_blocks(response)
            messages.append({"role": "assistant", "content": assistant_blocks})

            tool_uses = [b for b in assistant_blocks if b["type"] == "tool_use"]
            if not tool_uses:
                print(f"[iter {attempt}] assistant returned no tool calls; "
                      f"ending iteration with no submit_patch.")
                break

            # Identify a terminal submit_patch (if any). Anthropic requires a
            # tool_result for EVERY tool_use in the turn, so when submit fires
            # we still stub the other tool_uses rather than dropping them
            # (dropping any would 400 the next messages.create call).
            submit_tu = next(
                (tu for tu in tool_uses if tu["name"] == "submit_patch"), None)

            if submit_tu is None:
                tool_results_block: list[dict] = []
                for tu in tool_uses:
                    tools_used_this_iter.append(tu["name"])
                    result_text = agent_tools.dispatch_tool(
                        args.container, tu["name"], tu["input"] or {})
                    if len(result_text) > TOOL_OUTPUT_MAX_CHARS:
                        result_text = (
                            result_text[:TOOL_OUTPUT_MAX_CHARS]
                            + f"\n\n(tool output truncated at "
                              f"{TOOL_OUTPUT_MAX_CHARS} chars)\n")
                    tool_results_block.append({
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": result_text,
                    })
                remaining = MAX_TOOL_TURNS_PER_ITERATION - tool_turn
                if remaining <= 3:
                    nudge = (
                        f"\n[SYSTEM] WARNING: you have {remaining} tool "
                        f"turn(s) left in this iteration (cap = "
                        f"{MAX_TOOL_TURNS_PER_ITERATION}). You MUST call "
                        f"submit_patch within the next {remaining} turn(s) "
                        f"or this iteration will be abandoned as INCOMPLETE. "
                        f"Commit to your best fix now."
                    )
                    tool_results_block.append({"type": "text", "text": nudge})
                messages.append({"role": "user", "content": tool_results_block})
                continue

            # submit_patch fired. Stub every other tool_use in this turn so the
            # following user message answers all tool_use ids.
            submit_args = submit_tu["input"] or {}
            submit_tool_use_id = submit_tu["id"]
            other_results: list[dict] = [
                {"type": "tool_result", "tool_use_id": tu["id"],
                 "content": "(skipped: a submit_patch in the same turn "
                            "supersedes this call)"}
                for tu in tool_uses if tu["id"] != submit_tu["id"]
            ]
            submit_attempts += 1
            submitted_this_iter = True
            print(f"[iter {attempt}] submit_patch received "
                  f"({len(submit_args.get('patch') or '')} char diff, "
                  f"{len(submit_args.get('fixed_code') or [])} fixed_code entries)")

            common.write_llm_response_json(ctx.steps_dir, args.container,
                                           submit_args, attempt, model=args.model)

            apply_report = common.run_apply_fix(args.container, ctx.docker_container)
            applied_ok = bool((apply_report.get("result") or {}).get("ok"))

            verdict = "FAILED"
            verify_tail = ""
            if applied_ok:
                verdict, verify_tail = common.run_verify(
                    args.container, ctx.docker_container)
            else:
                (ctx.steps_dir / "verify_after_fix.verdict").write_text(
                    "FAILED\n", encoding="utf-8")

            final_category = common.classify_failure(apply_report, verdict)

            confirm_runs: list[dict] = []
            if verdict == "PASSED":
                for confirm_num in range(1, VERIFY_PASS_RUNS + 1):
                    c_verdict, c_tail = common.run_verify(
                        args.container, ctx.docker_container)
                    confirm_runs.append({"run": confirm_num, "verdict": c_verdict})
                    print(f"[confirm {confirm_num}/{VERIFY_PASS_RUNS}] {c_verdict}")
                    if c_verdict != "PASSED":
                        verdict = c_verdict
                        verify_tail = c_tail
                        final_category = common.classify_failure(apply_report, verdict)
                        break

            iter_elapsed = round(time.time() - t_iter_start, 2)
            iter_delta = {
                k: cumulative_usage.get(k, 0) - iter_start_usage.get(k, 0)
                for k in cumulative_usage
            }
            iter_row = {
                "iteration": attempt,
                "tool_turns": tool_turn,
                "tools_used": tools_used_this_iter,
                "verdict": verdict,
                "category": final_category,
                "applied_ok": applied_ok,
                "elapsed_seconds": iter_elapsed,
                "confirm_runs": confirm_runs,
                "tokens_in":  iter_delta.get("input_tokens", 0),
                "tokens_out": iter_delta.get("output_tokens", 0),
                "cache_read": iter_delta.get("cache_read_input_tokens", 0),
                "max_iters":  args.max_iterations,
            }
            with open(ctx.iter_log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(iter_row) + "\n")
            iter_summary_rows.append(iter_row)

            if verdict == "PASSED":
                final_verdict = "PASSED"
                messages.append({
                    "role": "user",
                    "content": other_results + [{
                        "type": "tool_result",
                        "tool_use_id": submit_tool_use_id,
                        "content": (
                            f"=== submit_patch attempt result: PASSED ===\n"
                            f"The test passes in the initial run and all "
                            f"{VERIFY_PASS_RUNS} confirmation runs. "
                            f"Repair confirmed successful."),
                    }],
                })
                break

            # FAILED or CONFIRM_FAILED — restore Flaky/ and feed back.
            common.restore_flaky(ctx.base)
            if confirm_runs:
                confirm_summary = "\n".join(
                    f"  run {r['run']}: {r['verdict']}" for r in confirm_runs)
                failure_report = (
                    f"=== submit_patch attempt result: CONFIRM_FAILED ===\n"
                    f"category:        confirm_failed\n"
                    f"verdict:         {verdict}\n"
                    f"The patch passed the first verification run but failed "
                    f"in a subsequent confirmation run — the fix is still "
                    f"non-deterministic.\n\n"
                    f"Confirmation runs ({VERIFY_PASS_RUNS} total):\n"
                    f"{confirm_summary}\n"
                    f"\n--- last failing verify log (tail) ---\n"
                    f"{verify_tail.rstrip()}\n"
                    f"\nFlaky/ has been restored to its pre-patch state. "
                    f"The fix does not pass consistently. Re-examine the "
                    f"root cause and submit a more robust patch.\n"
                ) + common.restrategy_hint("confirm_failed")
            else:
                failure_report = common.format_failure_report(
                    apply_report, verdict, verify_tail)

            print(f"[iter {attempt}] verdict={verdict} "
                  f"category={final_category} — feeding failure back to agent.")
            messages.append({
                "role": "user",
                "content": other_results + [{
                    "type": "tool_result",
                    "tool_use_id": submit_tool_use_id,
                    "content": failure_report,
                    "is_error": True,
                }],
            })
            break  # advance to next attempt

        if final_verdict == "PASSED":
            break

        common.save_conversation(ctx.conv_path, args.model, messages,
                                 provider="anthropic", system=SYSTEM_PROMPT)

        if not submitted_this_iter:
            print(f"[iter {attempt}] no submit_patch this iteration; aborting.")
            break

    code = common.finalize_run(
        ctx=ctx, container=args.container, model=args.model,
        provider="anthropic", messages=messages, system=SYSTEM_PROMPT,
        final_verdict=final_verdict, final_category=final_category,
        submit_attempts=submit_attempts, total_elapsed=total_elapsed,
        cumulative_usage=cumulative_usage, iter_summary_rows=iter_summary_rows,
        max_iters=args.max_iterations)
    sys.exit(code)


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
                    help=f"Anthropic model ID or alias (default: {DEFAULT_MODEL})")
    ap.add_argument("--exclude-tools", default="",
                    help="comma-separated tool names to remove from the "
                         "agent's toolset (e.g. get_flaky_example for "
                         "unclassified tests)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
