#!/usr/bin/env python3
"""
call_llm_simulate.py — replays archived LLM responses for reproducibility.

Used when SIMULATE_FROM env var is set. Instead of calling Claude or OpenAI,
reads canned llm_response_turn{1,2,3,4}.json from a previously-archived run
and writes them out as if the live backend produced them. Backend-agnostic:
the canned files carry whatever shape the original backend wrote (Claude's
cache_* keys, OpenAI's flat usage), preserved verbatim.

Invoked via call_llm.py's dispatcher when SIMULATE_FROM is set; not run
directly by the bash orchestrator.

Required env:
    SIMULATE_FROM=<archive Steps Output Files dir>, e.g.
    data/FULL_RUNS_RV/<container> runs/Claude/run 1/Steps Output Files/

Hard-fails with a clear message if a needed turn file is missing in the
archive — typically because the simulated pipeline diverged from the
original (e.g. simulated verify FAILED so feedback fires, but the archive's
original turn-1 patch passed verify and no turn-3 file exists).
"""

import json
import os
import sys

import fetch_artifacts
from response_parser import parse_response


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")

# Same system prompt both real backends use — the conversation file we
# write must carry it so a hypothetical feedback continuation reads back
# byte-identical context.
SYSTEM_PROMPT = (
    "You are an expert Java developer specializing in flaky test diagnosis and repair. "
    "You will receive a structured context file containing test metadata, runtime verification "
    "traces, failure output, and source code. Follow the output format instructions exactly, "
    "your response will be parsed by an automated script."
)


def _read_canned(archive_dir: str, name: str):
    path = os.path.join(archive_dir, name)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _diverged(message: str):
    print("\nERROR: simulation diverged from original at the LLM step.", file=sys.stderr)
    print(message, file=sys.stderr)
    sys.exit(1)


def _sum_usage(*usages):
    """Sum usage dicts. Handles both Claude (with cache_*) and OpenAI shapes
    by taking the union of keys present across the inputs."""
    keys = set()
    for u in usages:
        keys.update(u.keys())
    return {k: sum(u.get(k, 0) for u in usages) for k in keys}


def _simulated_main(result_container: str, archive_dir: str):
    base = os.path.join(DATA_DIR, result_container)
    steps_dir = os.path.join(base, "Steps Output Files")
    os.makedirs(steps_dir, exist_ok=True)

    output_file = os.path.join(steps_dir, "llm_response.json")
    turn1_path = os.path.join(steps_dir, "llm_response_turn1.json")
    turn2_path = os.path.join(steps_dir, "llm_response_turn2.json")
    turn3_path = os.path.join(steps_dir, "llm_response_turn3.json")
    turn4_path = os.path.join(steps_dir, "llm_response_turn4.json")
    artifacts_dump_path = os.path.join(steps_dir, "llm_artifacts_turn2.txt")
    artifacts_turn4_path = os.path.join(steps_dir, "llm_artifacts_turn4.txt")
    conversation_path = os.path.join(steps_dir, "llm_conversation.json")

    # Same stale-cleanup as real backends so this run's output is unambiguous.
    for stale in (turn2_path, turn3_path, turn4_path,
                  artifacts_dump_path, artifacts_turn4_path):
        if os.path.exists(stale):
            os.remove(stale)

    canned_turn1 = _read_canned(archive_dir, "llm_response_turn1.json")
    if canned_turn1 is None:
        _diverged(f"  archive {archive_dir!r} has no llm_response_turn1.json")

    text1 = canned_turn1.get("response") or ""
    print(f"[sim turn 1] replaying canned response ({len(text1)} chars)")

    # Re-parse the artifact request from the canned text. parse_artifact_block
    # is a pure function of the text, so this matches the original parse.
    kind, requested = fetch_artifacts.parse_artifact_block(text1)

    turn1_dict = dict(canned_turn1)
    turn1_dict["artifacts_requested_kind"] = kind
    turn1_dict["artifacts_requested"] = requested
    with open(turn1_path, "w", encoding="utf-8") as f:
        json.dump(turn1_dict, f, indent=2, ensure_ascii=False)

    context_file = os.path.join(steps_dir, "llm_context.txt")
    if os.path.isfile(context_file):
        with open(context_file, encoding="utf-8") as f:
            context = f.read()
    else:
        context = ""
    messages = [{"role": "user", "content": context}]

    final_text = text1
    final_stop_reason = canned_turn1.get("stop_reason") or ""
    final_usage = canned_turn1.get("usage") or {}

    if kind == "LIST" and requested:
        canned_turn2 = _read_canned(archive_dir, "llm_response_turn2.json")
        if canned_turn2 is None:
            _diverged(
                f"  turn 1 of canned response requested {len(requested)} artifact(s), "
                f"but archive {archive_dir!r} has no llm_response_turn2.json. "
                f"Archive is incomplete — cannot replay turn 2."
            )

        # Regenerate artifacts from the LIVE disk — deterministic given the
        # same source tree, and produces a current `llm_artifacts_turn2.txt`
        # that matches this run's actual disk state.
        print(f"[sim turn 1] LLM requested {len(requested)} artifact(s); fetching from current disk...")
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

        text2 = canned_turn2.get("response") or ""
        print(f"[sim turn 2] replaying canned response ({len(text2)} chars)")

        messages.append({"role": "assistant", "content": text1})
        messages.append({"role": "user", "content": turn2_body})

        # artifacts_satisfied + prompt_chars reflect THIS run's disk
        # (so they match the freshly-written llm_artifacts_turn2.txt);
        # everything else (response, usage, stop_reason) is verbatim from
        # canned since that's the LLM's behaviour we're replaying.
        turn2_dict = dict(canned_turn2)
        turn2_dict["artifacts_satisfied"] = artifacts_log
        turn2_dict["prompt_chars"] = len(turn2_body)
        with open(turn2_path, "w", encoding="utf-8") as f:
            json.dump(turn2_dict, f, indent=2, ensure_ascii=False)

        final_text = text2
        final_stop_reason = canned_turn2.get("stop_reason") or ""
        final_usage = _sum_usage(canned_turn1.get("usage") or {},
                                 canned_turn2.get("usage") or {})
    elif kind == "NONE":
        print("[sim turn 1] LLM declared NONE — turn 1 is final answer.")
    elif kind == "ABSENT":
        print("[sim turn 1] No <ARTIFACTS_REQUESTED> block — turn 1 is final answer.")
    else:
        print(f"[sim turn 1] Unexpected protocol state ({kind}) — treating turn 1 as final answer.")

    turns_taken = 2 if (kind == "LIST" and requested) else 1
    model = canned_turn1.get("model") or "simulated"

    result = {
        "model": model,
        "result_container": result_container,
        "elapsed_seconds": 0.0,
        "turns_taken": turns_taken,
        "artifacts_requested_kind": kind,
        "stop_reason": final_stop_reason,
        "usage": final_usage,
        "raw_response": final_text,
        "response": parse_response(final_text),
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    messages_full = list(messages)
    messages_full.append({"role": "assistant", "content": final_text})
    with open(conversation_path, "w", encoding="utf-8") as f:
        json.dump({
            "model": model,
            "system": SYSTEM_PROMPT,
            "messages": messages_full,
        }, f, indent=2, ensure_ascii=False)

    print(f"[sim] Done. {turns_taken} turn(s), 0s simulated.")
    print(f"Saved: {output_file}")
    print(f"  + {turn1_path}")
    if turns_taken == 2:
        print(f"  + {turn2_path}")
        print(f"  + {artifacts_dump_path}")
    print(f"  + {conversation_path}")


def _simulated_feedback_main(result_container: str, feedback_file: str, archive_dir: str):
    base = os.path.join(DATA_DIR, result_container)
    steps_dir = os.path.join(base, "Steps Output Files")
    conversation_path = os.path.join(steps_dir, "llm_conversation.json")
    output_file = os.path.join(steps_dir, "llm_response.json")
    turn3_path = os.path.join(steps_dir, "llm_response_turn3.json")
    turn4_path = os.path.join(steps_dir, "llm_response_turn4.json")
    artifacts_turn4_path = os.path.join(steps_dir, "llm_artifacts_turn4.txt")

    for required in (conversation_path, output_file, feedback_file):
        if not os.path.isfile(required):
            print(f"ERROR: required file not found: {required}", file=sys.stderr)
            sys.exit(1)

    canned_turn3 = _read_canned(archive_dir, "llm_response_turn3.json")
    if canned_turn3 is None:
        _diverged(
            f"  feedback round fired in this simulated run (verify produced FAILED) "
            f"but archive {archive_dir!r} has no llm_response_turn3.json. "
            f"The original turn-1 patch must have passed verify, so feedback never "
            f"ran in the original. The simulated pipeline has diverged."
        )

    with open(conversation_path, encoding="utf-8") as f:
        conversation = json.load(f)
    with open(output_file, encoding="utf-8") as f:
        prior_result = json.load(f)
    with open(feedback_file, encoding="utf-8") as f:
        feedback_text = f.read()

    saved_model = conversation.get("model") or canned_turn3.get("model") or "simulated"
    messages = list(conversation.get("messages", []))

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

    text3 = canned_turn3.get("response") or ""
    print(f"[sim turn 3] feedback round → replaying canned ({len(text3)} chars)")

    with open(turn3_path, "w", encoding="utf-8") as f:
        json.dump(dict(canned_turn3), f, indent=2, ensure_ascii=False)

    turn3_kind, turn3_requested = fetch_artifacts.parse_artifact_block(text3)
    final_text = text3
    final_stop_reason = canned_turn3.get("stop_reason") or ""
    final_usage = _sum_usage(prior_result.get("usage") or {},
                             canned_turn3.get("usage") or {})
    final_assistant_text = text3
    turns_added = 1
    extra_paths_to_print = []

    if turn3_kind == "LIST" and turn3_requested:
        canned_turn4 = _read_canned(archive_dir, "llm_response_turn4.json")
        if canned_turn4 is None:
            _diverged(
                f"  turn 3 of canned response requested {len(turn3_requested)} artifact(s), "
                f"but archive {archive_dir!r} has no llm_response_turn4.json. "
                f"Archive is incomplete — cannot replay turn 4."
            )

        print(f"[sim turn 3] LLM requested {len(turn3_requested)} artifact(s); fetching from current disk...")
        results = fetch_artifacts.fetch_artifacts(turn3_requested, base)
        for r in results:
            print(f"  - {r['type']:<16} {r['target']}  "
                  f"{'OK' if r['satisfied'] else 'MISS'} "
                  f"({r['size_chars']} chars)")
        artifacts_log = [
            {k: v for k, v in r.items() if k != "content"}
            for r in results
        ]
        # Same trailing instruction as real backends — kept for byte-identity
        # if a future feedback-on-feedback continuation is ever added.
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
        with open(artifacts_turn4_path, "w", encoding="utf-8") as f:
            f.write(turn4_body)

        messages.append({"role": "assistant", "content": text3})
        messages.append({"role": "user", "content": turn4_body})

        text4 = canned_turn4.get("response") or ""
        print(f"[sim turn 4] replaying canned ({len(text4)} chars)")

        turn4_dict = dict(canned_turn4)
        turn4_dict["artifacts_satisfied"] = artifacts_log
        turn4_dict["prompt_chars"] = len(turn4_body)
        with open(turn4_path, "w", encoding="utf-8") as f:
            json.dump(turn4_dict, f, indent=2, ensure_ascii=False)

        final_text = text4
        final_stop_reason = canned_turn4.get("stop_reason") or ""
        final_usage = _sum_usage(final_usage, canned_turn4.get("usage") or {})
        final_assistant_text = text4
        turns_added = 2
        extra_paths_to_print = [turn4_path, artifacts_turn4_path]

    new_result = dict(prior_result)
    new_result["elapsed_seconds"] = prior_result.get("elapsed_seconds") or 0
    new_result["turns_taken"] = (prior_result.get("turns_taken") or 0) + turns_added
    new_result["stop_reason"] = final_stop_reason
    new_result["usage"] = final_usage
    new_result["raw_response"] = final_text
    new_result["response"] = parse_response(final_text)
    new_result["feedback_used"] = True
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(new_result, f, indent=2, ensure_ascii=False)

    messages.append({"role": "assistant", "content": final_assistant_text})
    conversation["messages"] = messages
    conversation["model"] = saved_model
    with open(conversation_path, "w", encoding="utf-8") as f:
        json.dump(conversation, f, indent=2, ensure_ascii=False)

    print(f"[sim feedback] Updated: {output_file}")
    print(f"  + {turn3_path}")
    for p in extra_paths_to_print:
        print(f"  + {p}")
    print(f"  + {conversation_path}")


def main():
    archive_dir = os.environ.get("SIMULATE_FROM")
    if not archive_dir:
        print("ERROR: call_llm_simulate.py requires SIMULATE_FROM env var.", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(archive_dir):
        print(f"ERROR: SIMULATE_FROM={archive_dir!r} is not a directory.", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) == 4 and sys.argv[2] == "--feedback-from":
        return _simulated_feedback_main(sys.argv[1], sys.argv[3], archive_dir)

    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <result_container> "
              f"[--feedback-from <feedback_file>]")
        sys.exit(1)

    return _simulated_main(sys.argv[1], archive_dir)


if __name__ == "__main__":
    main()
