#!/usr/bin/env python3
"""
generate_llm_summary.py

Reads step_8_C_official.txt + events_encoding_id.txt + test_config.csv and
produces an LLM-ready trace-diff summary at:
    data/<result_container>/Steps Output Files/llm_trace_summary.txt

What it does NOT do:
  - signal assessment (removed: caller can infer signal strength from raw counts)
  - 'RV SPECS ONLY IN CLEAN RUN' (removed: distracts the LLM toward the
    passing run's behaviors, which are not the bug signal)

What it ADDS over the spec-name-only summary:
  - decoded TOP DISTINCTIVE FLAKY-ONLY trace sequences
  - decoded TOP FREQUENCY DIFFERENCES (largest |Δ| first)

Usage:
    python generate_llm_summary.py <result_container>
"""

import csv
import os
import re
import sys
from collections import defaultdict


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Script lives in ReproFlake-C9E6/LLM Scripts/ ; data and CSV are one level up
# (in ReproFlake-C9E6/), and events_encoding_id.txt is two levels up in scripts/.
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
EVENTS_FILE = os.path.join(SCRIPT_DIR, "..", "..", "scripts", "events_encoding_id.txt")
CSV_FILE = os.path.join(SCRIPT_DIR, "..", "test_config.csv")

# How many decoded entries to emit in each section.
TOP_N_FLAKY_TRACES = 20
TOP_N_FREQ_DIFFS = 10

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------
# Matches a single token inside a trace pattern: "e116", "e116~507", "e116~507x6"
EVENT_TOKEN = re.compile(r"e(\d+)(?:~(\d+))?(?:x(\d+))?")

# Lines emitted by compare-traces-official.py:
#   ERROR: [pattern] (ID: -1) is in actual (N times) but not expected
#   ERROR: [pattern] (ID: -1) is in expected (N times) but not actual
#   WARNING: [pattern]'s (ID: -1) frequency is X in expected, but is Y (ID: -1) in actual
RE_ACTUAL_ONLY = re.compile(
    r"ERROR:\s*\[(?P<trace>[^\]]+)\].*is in actual\s*\((?P<count>\d+)\s*times?\).*not expected"
)
RE_EXPECTED_ONLY = re.compile(
    r"ERROR:\s*\[(?P<trace>[^\]]+)\].*is in expected\s*\((?P<count>\d+)\s*times?\).*not actual"
)
RE_FREQ_DIFF = re.compile(
    r"WARNING:\s*\[(?P<trace>[^\]]+)\].*frequency is (?P<expected>\d+)\s*in expected,\s*but is (?P<actual>\d+)"
)


# ---------------------------------------------------------------------------
# File reading helpers
# ---------------------------------------------------------------------------
def read_file_auto_encoding(path):
    """Read a file, handling UTF-8/UTF-16 (PowerShell Tee-Object writes UTF-16)."""
    for enc in ("utf-8-sig", "utf-16-le", "utf-16", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                content = f.read()
            if "\x00" in content and enc == "latin-1":
                continue
            return content
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise RuntimeError(f"Cannot decode {path}")


def load_events_encoding(path):
    """Load events_encoding_id.txt -> {event_id_int: (spec_name, event_name)}."""
    mapping = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) >= 3:
                spec_name, event_name = parts[0], parts[1]
                try:
                    eid = int(parts[2])
                    mapping[eid] = (spec_name, event_name)
                except ValueError:
                    continue
    return mapping


def load_csv_row(csv_path, result_container):
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("result_container", "").strip() == result_container:
                return row
    return None


# ---------------------------------------------------------------------------
# Trace-decoding helpers (the main new pieces)
# ---------------------------------------------------------------------------
MAX_DECODED_EVENTS = 30


def decode_trace(trace_inner, events_map, max_events=MAX_DECODED_EVENTS):
    """
    Decode a comma-separated trace pattern into 'Spec.event → ...' form.

    Examples:
        'e116~507, e117~507, e115~507'
            -> 'Iterator_HasNext.next → Iterator_HasNext.hasNext → Iterator_HasNext.hasNextEnd'
        'e39~595, e41~595x5'
            -> 'Map_UnsafeIterator.create → Map_UnsafeIterator.use ×5'
        'e1~5, e1~7, e1~9, e1~11'   (same event, different locations)
            -> 'Closeable_MeaninglessClose.close ×4'  (collapsed)

    Location IDs (~NNN) are dropped because they are non-stable hashes that
    differ between runs. Adjacent identical decoded labels are collapsed into
    a single '×N' multiplier, which prevents location-noise from blowing up
    repetitive traces (e.g., a close()-loop hitting 200 distinct locations
    becomes one entry instead of 200). Output is capped at `max_events`
    distinct (post-collapse) events.
    """
    if not trace_inner:
        return ""

    # Step 1: tokenize to (label, count) pairs.
    pairs = []
    for token in trace_inner.split(","):
        token = token.strip()
        m = EVENT_TOKEN.match(token)
        if not m:
            pairs.append((token, 1))
            continue
        eid = int(m.group(1))
        count = int(m.group(3)) if m.group(3) else 1
        if eid in events_map:
            spec, evt = events_map[eid]
            label = f"{spec}.{evt}"
        else:
            label = f"e{eid}"
        pairs.append((label, count))

    # Step 2: collapse adjacent duplicates by summing their counts.
    collapsed = []
    for label, count in pairs:
        if collapsed and collapsed[-1][0] == label:
            collapsed[-1][1] += count
        else:
            collapsed.append([label, count])

    # Step 3: cap length.
    suffix = ""
    if len(collapsed) > max_events:
        omitted = len(collapsed) - max_events
        collapsed = collapsed[:max_events]
        suffix = f"  …(+{omitted} more event groups)"

    # Step 4: render.
    parts = []
    for label, count in collapsed:
        parts.append(f"{label} ×{count}" if count > 1 else label)
    return " → ".join(parts) + suffix


def parse_step8c_entries(content):
    """
    Parse step_8_C_official.txt into structured entries.

    Returns:
      flaky_only:    list of (count, trace_inner)            sorted by count desc
      clean_only:    list of (count, trace_inner)            (parsed only for spec computation)
      freq_diffs:    list of (expected, actual, trace_inner) sorted by |Δ| desc
      loc_mismatch:  int (count of "Locations don't match" lines)
    """
    flaky_only, clean_only, freq_diffs = [], [], []
    loc_mismatch = 0

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if "Locations don't match" in line:
            loc_mismatch += 1
            continue
        m = RE_ACTUAL_ONLY.match(line)
        if m:
            flaky_only.append((int(m.group("count")), m.group("trace").strip()))
            continue
        m = RE_EXPECTED_ONLY.match(line)
        if m:
            clean_only.append((int(m.group("count")), m.group("trace").strip()))
            continue
        m = RE_FREQ_DIFF.match(line)
        if m:
            freq_diffs.append((int(m.group("expected")), int(m.group("actual")), m.group("trace").strip()))
            continue

    flaky_only.sort(key=lambda x: -x[0])
    clean_only.sort(key=lambda x: -x[0])
    freq_diffs.sort(key=lambda x: -abs(x[1] - x[0]))
    return flaky_only, clean_only, freq_diffs, loc_mismatch


def event_ids_in_traces(entries, trace_index):
    """Collect all event IDs that appear in the given list of entries."""
    ids = set()
    for tup in entries:
        trace = tup[trace_index]
        for m in EVENT_TOKEN.finditer(trace):
            ids.add(int(m.group(1)))
    return ids


def ids_to_specs(event_ids, events_map):
    return sorted({events_map[eid][0] for eid in event_ids if eid in events_map})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def generate_summary(result_container):
    base = os.path.join(DATA_DIR, result_container)
    steps_dir = os.path.join(base, "Steps Output Files")
    os.makedirs(steps_dir, exist_ok=True)
    compare_file = os.path.join(steps_dir, "step_8_C_official.txt")
    output_file = os.path.join(steps_dir, "llm_trace_summary.txt")

    if not os.path.isfile(compare_file):
        print(f"ERROR: {compare_file} not found. Run Step 8C first.", file=sys.stderr)
        sys.exit(1)

    events_map = load_events_encoding(EVENTS_FILE)
    csv_row = load_csv_row(CSV_FILE, result_container)

    content = read_file_auto_encoding(compare_file)
    flaky_only, clean_only, freq_diffs, loc_mismatch = parse_step8c_entries(content)

    # Spec-set computation (used for "ONLY IN FLAKY", "IN BOTH", "FREQ-ONLY" sections)
    flaky_eids = event_ids_in_traces(flaky_only, trace_index=1)
    clean_eids = event_ids_in_traces(clean_only, trace_index=1)
    freq_eids = event_ids_in_traces(freq_diffs, trace_index=2)

    flaky_specs = set(ids_to_specs(flaky_eids, events_map))
    clean_specs = set(ids_to_specs(clean_eids, events_map))
    freq_specs = set(ids_to_specs(freq_eids, events_map))

    only_flaky_specs = sorted(flaky_specs - clean_specs)
    freq_only_specs = sorted(freq_specs - flaky_specs - clean_specs)

    # Spec → events breakdown for the flaky-only specs (so the spec list shows
    # which specific events of each spec triggered the flaky run).
    flaky_spec_events = defaultdict(set)
    for _, trace in flaky_only:
        for m in EVENT_TOKEN.finditer(trace):
            eid = int(m.group(1))
            if eid in events_map:
                spec, event = events_map[eid]
                if spec in only_flaky_specs:
                    flaky_spec_events[spec].add(event)

    # ---------------------------------------------------------------------
    # Build output
    # ---------------------------------------------------------------------
    lines = []
    lines.append("=" * 60)
    lines.append("TRACE DIFF SUMMARY (for LLM consumption)")
    lines.append("=" * 60)
    lines.append("")

    # --- Metadata ---
    if csv_row:
        test_type = csv_row.get("test_type", "unknown").strip().lower()
        polluter = csv_row.get("polluter/state setter", "").strip()
        victim = csv_row.get("flaky_test", "").strip()
        module = csv_row.get("module", "").strip()
        java_ver = csv_row.get("java", "").strip()
        has_polluter = test_type in ("od", "britle") and polluter != ""

        type_labels = {
            "od": "OD (order-dependent)",
            "id": "ID (implementation-dependent / non-deterministic)",
            "td": "TD (test-dependency)",
            "britle": "BRITTLE (fragile test interaction)",
            "unclassified": "UNCLASSIFIED",
        }
        lines.append(f"Test type:  {type_labels.get(test_type, test_type.upper())}")
        if has_polluter:
            lines.append(f"Polluter:   {polluter}")
        lines.append(f"Victim:     {victim}")
        lines.append(f"Module:     {module}")
        lines.append(f"Java:       {java_ver}")
    else:
        lines.append(f"Result container: {result_container}")
        lines.append("(CSV row not found — metadata unavailable)")
    lines.append("")

    # --- Raw counts ---
    lines.append("--- RAW COUNTS ---")
    lines.append(f"Flaky-only traces:   {len(flaky_only)}")
    lines.append(f"Clean-only traces:   {len(clean_only)}")
    lines.append(f"Frequency diffs:     {len(freq_diffs)}")
    lines.append(f"Location mismatches: {loc_mismatch}")
    lines.append("")

    # --- Specs only in flaky run (with their events) ---
    lines.append(f"=== RV SPECS ONLY IN FLAKY RUN ({len(only_flaky_specs)}) ===")
    lines.append("(Spec names whose events appear ONLY in the failing run.)")
    if only_flaky_specs:
        for spec in only_flaky_specs:
            events = sorted(flaky_spec_events.get(spec, []))
            lines.append(f"  - {spec}")
            if events:
                lines.append(f"      Events: {', '.join(events)}")
    else:
        lines.append("  (none — all specs in flaky-only traces also appear in the passing run.")
        lines.append("   The flakiness signal is in the SEQUENCES below, not in spec membership.)")
    lines.append("")

    # --- Top distinctive flaky-only trace sequences (decoded) ---
    if flaky_only:
        shown = min(TOP_N_FLAKY_TRACES, len(flaky_only))
        lines.append(f"=== TOP DISTINCTIVE FLAKY-ONLY TRACE SEQUENCES "
                     f"(top {shown} of {len(flaky_only)}) ===")
        lines.append("(Decoded event sequences seen ONLY in the failing run, sorted by count.")
        lines.append(" Format: SpecName.eventName → ... → ...   (×N = repetition).")
        lines.append(" Location IDs are dropped because they are non-stable across runs.)")
        lines.append("")
        for i, (count, trace) in enumerate(flaky_only[:TOP_N_FLAKY_TRACES], 1):
            decoded = decode_trace(trace, events_map)
            lines.append(f"  [{i}] count={count}")
            lines.append(f"      {decoded}")
        lines.append("")

    # --- Top frequency differences (decoded) ---
    if freq_diffs:
        shown = min(TOP_N_FREQ_DIFFS, len(freq_diffs))
        lines.append(f"=== TOP FREQUENCY DIFFERENCES "
                     f"(top {shown} of {len(freq_diffs)}, sorted by |Δ|) ===")
        lines.append("(Same trace pattern in both runs but at different frequencies.")
        lines.append(" expected = count in passing run, actual = count in failing run.)")
        lines.append("")
        for i, (exp, act, trace) in enumerate(freq_diffs[:TOP_N_FREQ_DIFFS], 1):
            delta = act - exp
            sign = "+" if delta >= 0 else ""
            decoded = decode_trace(trace, events_map)
            lines.append(f"  [{i}] expected={exp}  actual={act}  Δ={sign}{delta}")
            lines.append(f"      {decoded}")
        lines.append("")

    # --- Specs that only appear in frequency diffs ---
    if freq_only_specs:
        lines.append(f"=== SPECS WITH FREQUENCY DIFFERENCES ONLY ({len(freq_only_specs)}) ===")
        lines.append("(Same trace pattern in both, but different occurrence counts.)")
        for spec in freq_only_specs:
            lines.append(f"  - {spec}")
        lines.append("")

    # --- How to read this (replaces the long generic interpretation guide) ---
    lines.append("--- HOW TO READ THIS ---")
    lines.append("Use TOP DISTINCTIVE FLAKY-ONLY TRACE SEQUENCES and TOP FREQUENCY")
    lines.append("DIFFERENCES as concrete behavioral evidence. Each entry names")
    lines.append("the RV monitor (spec) and the ordered events that fired. Combine")
    lines.append("with the failure stack trace to localise the bug.")
    lines.append("")

    # --- Write ---
    output_text = "\n".join(lines)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(output_text)
    print(output_text)
    print(f"\nSaved to: {output_file}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <result_container>")
        sys.exit(1)
    generate_summary(sys.argv[1])
