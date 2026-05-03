#!/usr/bin/env python3
"""
call_llm_openai.py

Sends the assembled LLM context to OpenAI (gpt-4o) and saves the response.

Usage:
    python call_llm_openai.py <result_container>

Requires:
    - pip install openai
    - OPENAI_API_KEY in the environment (or entered at the prompt)

Output:
    data/<result_container>/Steps Output Files/llm_response.json
    (same filename as call_llm.py — overwriting any previous run)
"""

import json
import os
import sys
import time

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai package not installed. Run: py -3 -m pip install openai", file=sys.stderr)
    sys.exit(1)

import fetch_artifacts                       # local — provider-neutral
from response_parser import parse_response   # local — provider-neutral


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")

MODEL = "gpt-4o"
MAX_TOKENS = 16384
TEMPERATURE = 0.2


def _send(client, system_prompt, messages, model=MODEL):
    """Single API call with our standard max_tokens/temperature.

    `model` defaults to module-level MODEL; feedback round passes the
    saved model from llm_conversation.json so the prefix replays
    byte-identically (which is required for OpenAI's automatic prompt
    caching to hit).

    OpenAI puts the system prompt as the first message (role='system'),
    unlike Anthropic's separate `system=` kwarg.
    """
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    return client.chat.completions.create(
        model=model,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        messages=full_messages,
    )


def _usage_dict(response):
    """Standard usage dict from an OpenAI response."""
    u = response.usage
    return {
        "input_tokens": u.prompt_tokens,
        "output_tokens": u.completion_tokens,
        "total_tokens": u.total_tokens,
    }


def _sum_usage(*usages):
    keys = ("input_tokens", "output_tokens", "total_tokens")
    return {k: sum(u.get(k, 0) for u in usages) for k in keys}


def _feedback_main(result_container: str, feedback_file: str):
    """Resume an existing conversation by appending one feedback user-turn,
    sending one more API call, and overwriting llm_response.json with the
    new final response so apply_fix.py picks up the corrected patch
    unchanged.

    Reads:
        Steps Output Files/llm_conversation.json   (must exist)
        Steps Output Files/llm_response.json       (must exist; cumulative
                                                    usage is computed against
                                                    its prior `usage` field)
        <feedback_file>                            (the new user turn body)

    Writes:
        Steps Output Files/llm_response_turn3.json  (this turn's raw response)
        Steps Output Files/llm_response.json        (overwritten with new
                                                     final response, cumulative
                                                     usage; turns_taken bumped)
        Steps Output Files/llm_conversation.json    (extended with this turn's
                                                     user + assistant messages)
    """
    base = os.path.join(DATA_DIR, result_container)
    steps_dir = os.path.join(base, "Steps Output Files")
    conversation_path = os.path.join(steps_dir, "llm_conversation.json")
    output_file = os.path.join(steps_dir, "llm_response.json")
    turn3_path = os.path.join(steps_dir, "llm_response_turn3.json")

    for required in (conversation_path, output_file, feedback_file):
        if not os.path.isfile(required):
            print(f"ERROR: required file not found: {required}", file=sys.stderr)
            sys.exit(1)

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        api_key = input("Enter your OpenAI API key: ").strip()
        if not api_key:
            print("ERROR: No API key provided.", file=sys.stderr)
            sys.exit(1)
        os.environ["OPENAI_API_KEY"] = api_key

    with open(conversation_path, encoding="utf-8") as f:
        conversation = json.load(f)
    with open(output_file, encoding="utf-8") as f:
        prior_result = json.load(f)
    with open(feedback_file, encoding="utf-8") as f:
        feedback_text = f.read()

    saved_model = conversation.get("model") or MODEL
    system_prompt = conversation.get("system", "")
    messages = list(conversation.get("messages", []))

    # Sanity: conversation must end with an assistant turn so we can append
    # a user turn after it. OpenAI rejects consecutive user messages with
    # a 400 — fail fast here with a helpful message.
    if not messages:
        print(f"ERROR: {conversation_path} has no messages", file=sys.stderr)
        sys.exit(1)
    if messages[-1].get("role") != "assistant":
        print(f"ERROR: last message in {conversation_path} is "
              f"role={messages[-1].get('role')!r}, expected 'assistant'.",
              file=sys.stderr)
        sys.exit(1)

    for msg in messages:
        if msg.get("content") is None:
            msg["content"] = ""

    messages.append({"role": "user", "content": feedback_text})

    client = OpenAI(api_key=api_key)
    print(f"[turn 3] feedback round → {saved_model} ({len(feedback_text)} chars)")
    t0 = time.time()
    resp = _send(client, system_prompt, messages, model=saved_model)
    elapsed = round(time.time() - t0, 2)
    text3 = resp.choices[0].message.content or ""
    finish3 = resp.choices[0].finish_reason
    usage3 = _usage_dict(resp)
    print(f"[turn 3] {elapsed}s, "
          f"in={usage3['input_tokens']} out={usage3['output_tokens']} finish={finish3}")

    turn3_dict = {
        "turn": 3,
        "model": saved_model,
        "elapsed_seconds": elapsed,
        "prompt_source": os.path.basename(feedback_file),
        "prompt_chars": len(feedback_text),
        "stop_reason": finish3,
        "usage": usage3,
        "response": text3,
        "feedback_used": True,
    }
    with open(turn3_path, "w", encoding="utf-8") as f:
        json.dump(turn3_dict, f, indent=2, ensure_ascii=False)

    # ---- TURN 4 (only if turn 3 requested artifacts) ----
    # Mirrors the Option-B flow in call_llm_claude.py — see longer comment
    # there. If turn 3 emitted an <ARTIFACTS_REQUESTED> block instead of a
    # patch, fetch the artifacts and ask for the corrected fix in turn 4.
    # Bounded to 1 artifact-retrieval round per feedback cycle (turn 4 is
    # final regardless of what it returns).
    turn3_kind, turn3_requested = fetch_artifacts.parse_artifact_block(text3)
    final_text = text3
    final_finish = finish3
    final_usage = _sum_usage(prior_result.get("usage") or {}, usage3)
    final_assistant_text = text3
    turns_added = 1
    extra_paths_to_print = []

    if turn3_kind == "LIST" and turn3_requested:
        print(f"[turn 3] LLM requested {len(turn3_requested)} artifact(s); fetching...")
        results = fetch_artifacts.fetch_artifacts(turn3_requested, base)
        for r in results:
            print(f"  - {r['type']:<16} {r['target']}  "
                  f"{'OK' if r['satisfied'] else 'MISS'} "
                  f"({r['size_chars']} chars)")
        artifacts_log = [
            {k: v for k, v in r.items() if k != "content"}
            for r in results
        ]
        # Append a terminal-turn instruction. See longer comment in
        # call_llm_claude.py — without this the LLM may respond with
        # another <ARTIFACTS_REQUESTED> block, which our bounded recursion
        # silently ignores and apply_fix.py then fails for lack of patch.
        turn4_body = fetch_artifacts.format_artifacts_block(results) + (
            "\n\n=== FINAL TURN — YOU MUST PRODUCE A FIX NOW ===\n"
            "This is the LAST turn of this feedback cycle. You MUST reply with\n"
            "a complete fix in the SAME schema (OUTPUT 0 / A / B) using the\n"
            "artifacts provided above plus everything already in your conversation\n"
            "context. Do NOT request additional artifacts — any further\n"
            "<ARTIFACTS_REQUESTED> block will be IGNORED and the run will be\n"
            "marked FAILED. If you are uncertain, make your best informed attempt\n"
            "with the information you already have.\n"
        )
        artifacts_turn4_path = os.path.join(steps_dir, "llm_artifacts_turn4.txt")
        with open(artifacts_turn4_path, "w", encoding="utf-8") as f:
            f.write(turn4_body)

        messages.append({"role": "assistant", "content": text3})
        messages.append({"role": "user", "content": turn4_body})

        print(f"[turn 4] Sending artifacts ({len(turn4_body)} chars) and asking for the corrected fix...")
        t4 = time.time()
        resp4 = _send(client, system_prompt, messages, model=saved_model)
        elapsed4 = round(time.time() - t4, 2)
        text4 = resp4.choices[0].message.content or ""
        finish4 = resp4.choices[0].finish_reason
        usage4 = _usage_dict(resp4)
        print(f"[turn 4] {elapsed4}s, "
              f"in={usage4['input_tokens']} out={usage4['output_tokens']} finish={finish4}")

        turn4_path = os.path.join(steps_dir, "llm_response_turn4.json")
        with open(turn4_path, "w", encoding="utf-8") as f:
            json.dump({
                "turn": 4,
                "model": saved_model,
                "elapsed_seconds": elapsed4,
                "prompt_source": "llm_artifacts_turn4.txt",
                "prompt_chars": len(turn4_body),
                "stop_reason": finish4,
                "usage": usage4,
                "response": text4,
                "artifacts_satisfied": artifacts_log,
                "feedback_used": True,
            }, f, indent=2, ensure_ascii=False)

        final_text = text4
        final_finish = finish4
        final_usage = _sum_usage(final_usage, usage4)
        final_assistant_text = text4
        elapsed = round(elapsed + elapsed4, 2)
        turns_added = 2
        extra_paths_to_print = [turn4_path, artifacts_turn4_path]

    # Cumulative usage = prior + turn3 (+ turn4 if it ran). `(... or {})` /
    # `(... or 0)` defensively coerce explicit-None values in prior_result
    # so a malformed prior llm_response.json doesn't crash. See longer
    # comment in call_llm_claude.py.
    new_result = dict(prior_result)
    new_result["elapsed_seconds"] = round((prior_result.get("elapsed_seconds") or 0) + elapsed, 2)
    new_result["turns_taken"] = (prior_result.get("turns_taken") or 0) + turns_added
    new_result["stop_reason"] = final_finish
    new_result["usage"] = final_usage
    new_result["raw_response"] = final_text
    new_result["response"] = parse_response(final_text)
    new_result["feedback_used"] = True
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(new_result, f, indent=2, ensure_ascii=False)

    # Extend the conversation with whichever final assistant turn ran. When
    # turn 4 fired, `messages` already has the turn-3 assistant + turn-4 user
    # appended (lines above) — we just need to add the final assistant turn.
    messages.append({"role": "assistant", "content": final_assistant_text})
    conversation["messages"] = messages
    with open(conversation_path, "w", encoding="utf-8") as f:
        json.dump(conversation, f, indent=2, ensure_ascii=False)

    print(f"Updated: {output_file}")
    print(f"  + {turn3_path}")
    for p in extra_paths_to_print:
        print(f"  + {p}")
    print(f"  + {conversation_path}")


def main():
    if len(sys.argv) == 4 and sys.argv[2] == "--feedback-from":
        return _feedback_main(sys.argv[1], sys.argv[3])

    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <result_container> "
              f"[--feedback-from <feedback_file>]")
        sys.exit(1)

    result_container = sys.argv[1]
    base = os.path.join(DATA_DIR, result_container)
    steps_dir = os.path.join(base, "Steps Output Files")
    os.makedirs(steps_dir, exist_ok=True)
    context_file = os.path.join(steps_dir, "llm_context.txt")
    output_file = os.path.join(steps_dir, "llm_response.json")
    turn1_path = os.path.join(steps_dir, "llm_response_turn1.json")
    turn2_path = os.path.join(steps_dir, "llm_response_turn2.json")
    turn3_path = os.path.join(steps_dir, "llm_response_turn3.json")
    turn4_path = os.path.join(steps_dir, "llm_response_turn4.json")
    artifacts_dump_path = os.path.join(steps_dir, "llm_artifacts_turn2.txt")
    artifacts_turn4_path = os.path.join(steps_dir, "llm_artifacts_turn4.txt")

    # Clear stale per-turn files left over from a prior run so this run's
    # output is unambiguous. turn3/turn4 specifically: a prior feedback
    # round leaves them on disk; if this run completes without feedback
    # firing, those stale files would falsely suggest feedback happened.
    for stale in (turn2_path, turn3_path, turn4_path,
                  artifacts_dump_path, artifacts_turn4_path):
        if os.path.exists(stale):
            os.remove(stale)

    if not os.path.isfile(context_file):
        print(f"ERROR: {context_file} not found. Run the per-type assembler first "
              f"(assemble_llm_context_od.py / _td.py / _id.py / _nio.py).", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        print("Using OPENAI_API_KEY from environment.")
    else:
        api_key = input("Enter your OpenAI API key: ").strip()
        if not api_key:
            print("ERROR: No API key provided.", file=sys.stderr)
            sys.exit(1)
        os.environ["OPENAI_API_KEY"] = api_key
        print("API key stored in environment for this session.")

    with open(context_file, encoding="utf-8") as f:
        context = f.read()

    client = OpenAI(api_key=api_key)

    system_prompt = (
        "You are an expert Java developer specializing in flaky test diagnosis and repair. "
        "You will receive a structured context file containing test metadata, runtime verification "
        "traces, failure output, and source code. Follow the output format instructions exactly, "
        "your response will be parsed by an automated script."
    )

    # ---- TURN 1 ----
    messages = [{"role": "user", "content": context}]

    print(f"[turn 1] Sending context to {MODEL} ({len(context)} chars)...")
    t0 = time.time()
    resp1 = _send(client, system_prompt, messages)
    t1 = time.time()
    text1 = resp1.choices[0].message.content
    finish1 = resp1.choices[0].finish_reason
    usage1 = _usage_dict(resp1)
    print(f"[turn 1] {t1 - t0:.1f}s, "
          f"in={usage1['input_tokens']} out={usage1['output_tokens']} "
          f"finish={finish1}")

    kind, requested = fetch_artifacts.parse_artifact_block(text1)

    turn1_dict = {
        "turn": 1,
        "model": MODEL,
        "elapsed_seconds": round(t1 - t0, 2),
        "prompt_source": "llm_context.txt",
        "prompt_chars": len(context),
        "stop_reason": finish1,
        "usage": usage1,
        "response": text1,
        "artifacts_requested_kind": kind,
        "artifacts_requested": requested,
    }
    with open(turn1_path, "w", encoding="utf-8") as f:
        json.dump(turn1_dict, f, indent=2, ensure_ascii=False)

    # ---- TURN 2 (only if the LLM requested artifacts) ----
    final_text = text1
    final_stop_reason = finish1
    final_usage = usage1

    if kind == "LIST" and requested:
        print(f"[turn 1] LLM requested {len(requested)} artifact(s); fetching...")
        results = fetch_artifacts.fetch_artifacts(requested, base)
        for r in results:
            print(f"  - {r['type']:<16} {r['target']}  "
                  f"{'OK' if r['satisfied'] else 'MISS'} "
                  f"({r['size_chars']} chars)")
        artifacts_log = [
            {k: v for k, v in r.items() if k != "content"}
            for r in results
        ]
        turn2_body = fetch_artifacts.format_artifacts_block(results)
        with open(artifacts_dump_path, "w", encoding="utf-8") as f:
            f.write(turn2_body)

        messages.append({"role": "assistant", "content": text1})
        messages.append({"role": "user", "content": turn2_body})

        print(f"[turn 2] Sending artifacts ({len(turn2_body)} chars) and asking for the fix...")
        t2 = time.time()
        resp2 = _send(client, system_prompt, messages)
        t3 = time.time()
        text2 = resp2.choices[0].message.content
        finish2 = resp2.choices[0].finish_reason
        usage2 = _usage_dict(resp2)
        print(f"[turn 2] {t3 - t2:.1f}s, "
              f"in={usage2['input_tokens']} out={usage2['output_tokens']} "
              f"finish={finish2}")

        turn2_dict = {
            "turn": 2,
            "model": MODEL,
            "elapsed_seconds": round(t3 - t2, 2),
            "prompt_source": "llm_artifacts_turn2.txt",
            "prompt_chars": len(turn2_body),
            "stop_reason": finish2,
            "usage": usage2,
            "response": text2,
            "artifacts_satisfied": artifacts_log,
        }
        with open(turn2_path, "w", encoding="utf-8") as f:
            json.dump(turn2_dict, f, indent=2, ensure_ascii=False)

        final_text = text2
        final_stop_reason = finish2
        final_usage = _sum_usage(usage1, usage2)

    elif kind == "NONE":
        print("[turn 1] LLM declared NONE — answering directly in turn 1.")
    elif kind == "ABSENT":
        print("[turn 1] No <ARTIFACTS_REQUESTED> block found — treating turn 1 as final answer.")
    else:
        print(f"[turn 1] Unexpected protocol state ({kind}) — treating turn 1 as final answer.")

    elapsed_seconds = round(time.time() - t0, 2)
    turns_taken = 2 if (kind == "LIST" and requested) else 1

    result = {
        "model": MODEL,
        "result_container": result_container,
        "elapsed_seconds": elapsed_seconds,
        "turns_taken": turns_taken,
        "artifacts_requested_kind": kind,
        "stop_reason": final_stop_reason,
        "usage": final_usage,
        "raw_response": final_text,
        "response": parse_response(final_text),
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    # Persist the full conversation for potential feedback continuation.
    # Includes the final assistant response so a later --feedback-from
    # invocation can resume by replaying these messages and appending a
    # new user turn. OpenAI's automatic prefix caching kicks in on replay
    # since the system prompt + turns 1-2 are byte-identical.
    messages_full = list(messages)
    messages_full.append({"role": "assistant", "content": final_text})
    conversation_path = os.path.join(steps_dir, "llm_conversation.json")
    with open(conversation_path, "w", encoding="utf-8") as f:
        json.dump({
            "model": MODEL,
            "system": system_prompt,
            "messages": messages_full,
        }, f, indent=2, ensure_ascii=False)

    print(f"Done in {elapsed_seconds:.1f}s, {final_usage['total_tokens']} tokens")
    print(f"Saved: {output_file}")
    print(f"  + {turn1_path}")
    if turns_taken == 2:
        print(f"  + {turn2_path}")
        print(f"  + {artifacts_dump_path}")
    print(f"  + {conversation_path}")


if __name__ == "__main__":
    main()
