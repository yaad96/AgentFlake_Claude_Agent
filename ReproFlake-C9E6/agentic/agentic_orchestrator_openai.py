#!/usr/bin/env python3
"""
agentic_orchestrator_openai.py — OpenAI backend for the agentic loop.

Runs the bounded-iteration tool-use loop against OpenAI's Chat Completions
API. All provider-neutral logic (prompt construction, tool implementations,
patch apply/verify, failure formatting, run-summary, setup/teardown) lives in
orchestrator_common; this file adds only the OpenAI-specific pieces:

  1. client + API call      OpenAI.chat.completions.create
  2. tool schema format     {name, description, input_schema}
                            -> {type:function, function:{name, description, parameters}}
  3. message / tool plumbing assistant.tool_calls + role:"tool" messages keyed by id
  4. usage + stop reason    usage.prompt_tokens/completion_tokens + finish_reason

Normally invoked via the parent dispatcher agentic_orchestrator.py. Can also
be run standalone:

    python3 agentic_orchestrator_openai.py <container> --model gpt-4o

Requires:
    OPENAI_API_KEY in env or agentic_config.OPENAI_API_KEY
    pip install openai
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai package not installed. Run: "
          "python3 -m pip install openai", file=sys.stderr)
    sys.exit(1)

import agentic_config         # type: ignore  # noqa: E402
import agent_tools            # type: ignore  # noqa: E402
import orchestrator_common as common  # type: ignore  # noqa: E402

DEFAULT_MODEL                = "gpt-4o"
MAX_TOKENS                   = common.MAX_TOKENS
TEMPERATURE                  = common.TEMPERATURE
MAX_TOOL_TURNS_PER_ITERATION = common.MAX_TOOL_TURNS_PER_ITERATION
DEFAULT_MAX_ITERATIONS       = common.DEFAULT_MAX_ITERATIONS
TOOL_OUTPUT_MAX_CHARS        = common.TOOL_OUTPUT_MAX_CHARS
VERIFY_PASS_RUNS             = common.VERIFY_PASS_RUNS
SYSTEM_PROMPT                = common.SYSTEM_PROMPT


def is_openai_model(model: str) -> bool:
    """True if `model` should be handled by the OpenAI backend."""
    key = (model or "").strip().lower()
    if not key:
        return False
    if key in {v.lower() for v in agentic_config.OPENAI_MODELS.values()}:
        return True
    if key in {k.lower() for k in agentic_config.OPENAI_MODELS}:
        return True
    return key.startswith(("gpt", "o1", "o3", "o4"))


# ---------------------------------------------------------------------------
# OpenAI-shaped helpers (the only provider-specific code in this file)
# ---------------------------------------------------------------------------

def _to_openai_tools(anthropic_schemas: list[dict]) -> list[dict]:
    """Translate Anthropic {name, description, input_schema} tool defs into
    OpenAI Chat Completions {type:function, function:{...,parameters}} defs."""
    out = []
    for s in anthropic_schemas:
        out.append({
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["input_schema"],
            },
        })
    return out


def _usage_dict(response) -> dict:
    """Map OpenAI usage onto the neutral usage keys (input/output/total/
    cache_read…) the rest of the pipeline expects."""
    u = getattr(response, "usage", None)
    if u is None:
        return common.zero_usage()
    cached = 0
    details = getattr(u, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
    return {
        "input_tokens":  getattr(u, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(u, "completion_tokens", 0) or 0,
        "total_tokens":  getattr(u, "total_tokens", 0) or 0,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
    }


def _serialize_tool_calls(tool_calls) -> list[dict]:
    """Plain-dict form of the assistant's tool_calls, for re-appending to the
    messages list and for the conversation snapshot."""
    out = []
    for tc in tool_calls:
        out.append({
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.function.name,
                "arguments": tc.function.arguments or "{}",
            },
        })
    return out


def _parse_tool_args(raw: str | None) -> tuple[dict, str | None]:
    """Parse a tool_call's JSON argument string. Returns (args, error)."""
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {}, f"arguments were valid JSON but not an object: {type(parsed).__name__}"
        return parsed, None
    except json.JSONDecodeError as exc:
        return {}, f"arguments were not valid JSON: {exc}"


def _create_kwargs(model: str, messages: list[dict], tools: list[dict]) -> dict:
    """Build chat.completions.create kwargs, accounting for the o-series'
    different param names/constraints (max_completion_tokens, no temperature)."""
    key = model.strip().lower()
    is_o_series = key.startswith(("o1", "o3", "o4")) or key.startswith("gpt-5")
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }
    if is_o_series:
        kwargs["max_completion_tokens"] = MAX_TOKENS
    else:
        kwargs["max_tokens"] = MAX_TOKENS
        kwargs["temperature"] = TEMPERATURE
    return kwargs


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    ctx = common.prepare_run(args)

    # API key: env var takes precedence, then agentic_config.OPENAI_API_KEY.
    api_key = (os.environ.get("OPENAI_API_KEY", "")
               or getattr(agentic_config, "OPENAI_API_KEY", "")).strip()
    if not api_key:
        sys.exit("ERROR: OPENAI_API_KEY is not set.\n"
                 "       Set it in agentic_config.py or export it as an env var.")

    client = OpenAI(api_key=api_key)
    excluded_tools = {t.strip() for t in args.exclude_tools.split(",") if t.strip()}
    schemas = [s for s in common.all_tool_schemas()
               if s["name"] not in excluded_tools]
    tools = _to_openai_tools(schemas)
    if excluded_tools:
        print(f"[init ] excluded tools: {sorted(excluded_tools)}")

    # OpenAI carries the system prompt as the first message in the list.
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": ctx.initial_user},
    ]

    cumulative_usage = common.zero_usage()
    total_elapsed = 0.0
    submit_attempts = 0
    final_verdict = "INCOMPLETE"
    final_category = ""
    iter_summary_rows: list[dict] = []

    print(f"[init ] container={args.container}  test_type={ctx.test_type}  "
          f"provider=openai  model={args.model}  "
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
            response = client.chat.completions.create(
                **_create_kwargs(args.model, messages, tools))
            elapsed = time.time() - t0
            total_elapsed += elapsed
            usage = _usage_dict(response)
            cumulative_usage = common.sum_usage(cumulative_usage, usage)

            choice = response.choices[0]
            msg = choice.message
            finish = choice.finish_reason
            tool_calls = list(msg.tool_calls or [])

            print(f"[iter {attempt}/turn {tool_turn}] {elapsed:.1f}s  "
                  f"in={usage['input_tokens']} out={usage['output_tokens']}  "
                  f"cache_read={usage['cache_read_input_tokens']}  "
                  f"finish={finish}")

            # Re-append the assistant turn. If tool_calls exist, every
            # tool_call_id must be answered before the next assistant message.
            assistant_msg: dict = {"role": "assistant",
                                   "content": msg.content or ""}
            if tool_calls:
                assistant_msg["tool_calls"] = _serialize_tool_calls(tool_calls)
            messages.append(assistant_msg)

            if not tool_calls:
                print(f"[iter {attempt}] assistant returned no tool calls; "
                      f"ending iteration with no submit_patch.")
                break

            submit_tc = next(
                (tc for tc in tool_calls if tc.function.name == "submit_patch"),
                None)

            # ---- pure context-tool turn ---------------------------------
            if submit_tc is None:
                for tc in tool_calls:
                    targs, _err = _parse_tool_args(tc.function.arguments)
                    tools_used_this_iter.append(tc.function.name)
                    result_text = agent_tools.dispatch_tool(
                        args.container, tc.function.name, targs)
                    if len(result_text) > TOOL_OUTPUT_MAX_CHARS:
                        result_text = (
                            result_text[:TOOL_OUTPUT_MAX_CHARS]
                            + f"\n\n(tool output truncated at "
                              f"{TOOL_OUTPUT_MAX_CHARS} chars)\n")
                    messages.append({"role": "tool",
                                     "tool_call_id": tc.id,
                                     "content": result_text})
                remaining = MAX_TOOL_TURNS_PER_ITERATION - tool_turn
                if remaining <= 3:
                    messages.append({"role": "user", "content": (
                        f"[SYSTEM] WARNING: you have {remaining} tool turn(s) "
                        f"left in this iteration (cap = "
                        f"{MAX_TOOL_TURNS_PER_ITERATION}). You MUST call "
                        f"submit_patch within the next {remaining} turn(s) or "
                        f"this iteration will be abandoned as INCOMPLETE. "
                        f"Commit to your best fix now.")})
                continue

            # ---- submit_patch fired -------------------------------------
            # Answer every *other* tool_call_id in this turn (OpenAI demands
            # it) with a stub before processing the terminal submit_patch.
            for tc in tool_calls:
                if tc.id == submit_tc.id:
                    continue
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": "(skipped: a submit_patch in the "
                                            "same turn supersedes this call)"})

            submit_args, arg_err = _parse_tool_args(submit_tc.function.arguments)
            if arg_err:
                print(f"[iter {attempt}] submit_patch {arg_err}; asking for resend.")
                messages.append({"role": "tool", "tool_call_id": submit_tc.id,
                                 "content": (
                                     f"=== submit_patch rejected ===\n"
                                     f"Your submit_patch {arg_err}\n"
                                     f"Resend submit_patch with a valid JSON "
                                     f"object containing diagnosis, root_cause, "
                                     f"fix_description, patch, and fixed_code.")})
                continue

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
                messages.append({"role": "tool", "tool_call_id": submit_tc.id,
                                 "content": (
                                     f"=== submit_patch attempt result: PASSED ===\n"
                                     f"The test passes in the initial run and all "
                                     f"{VERIFY_PASS_RUNS} confirmation runs. "
                                     f"Repair confirmed successful.")})
                break

            # FAILED / CONFIRM_FAILED — restore and feed back.
            common.restore_flaky(ctx.base)
            if confirm_runs:
                confirm_summary = "\n".join(
                    f"  run {r['run']}: {r['verdict']}" for r in confirm_runs)
                failure_report = (
                    f"=== submit_patch attempt result: CONFIRM_FAILED ===\n"
                    f"category:        confirm_failed\n"
                    f"verdict:         {verdict}\n"
                    f"The patch passed the first verification run but failed in a "
                    f"subsequent confirmation run — the fix is still "
                    f"non-deterministic.\n\n"
                    f"Confirmation runs ({VERIFY_PASS_RUNS} total):\n"
                    f"{confirm_summary}\n"
                    f"\n--- last failing verify log (tail) ---\n"
                    f"{verify_tail.rstrip()}\n"
                    f"\nFlaky/ has been restored to its pre-patch state. The fix "
                    f"does not pass consistently. Re-examine the root cause and "
                    f"submit a more robust patch.\n"
                ) + common.restrategy_hint("confirm_failed")
            else:
                failure_report = common.format_failure_report(
                    apply_report, verdict, verify_tail)

            print(f"[iter {attempt}] verdict={verdict} "
                  f"category={final_category} — feeding failure back to agent.")
            messages.append({"role": "tool", "tool_call_id": submit_tc.id,
                             "content": failure_report})
            break  # advance to next attempt

        if final_verdict == "PASSED":
            break

        common.save_conversation(ctx.conv_path, args.model, messages,
                                 provider="openai")

        if not submitted_this_iter:
            print(f"[iter {attempt}] no submit_patch this iteration; aborting.")
            break

    code = common.finalize_run(
        ctx=ctx, container=args.container, model=args.model,
        provider="openai", messages=messages, system=None,
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
                    help=f"OpenAI model ID (default: {DEFAULT_MODEL})")
    ap.add_argument("--exclude-tools", default="",
                    help="comma-separated tool names to remove from the "
                         "agent's toolset (e.g. get_flaky_example for "
                         "unclassified tests)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
