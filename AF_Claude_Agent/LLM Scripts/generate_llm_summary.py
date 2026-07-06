#!/usr/bin/env python3
"""
generate_llm_summary.py

Reads step_8_C_official.txt + events_encoding_id.txt + test_config.csv and
produces an LLM-ready trace-diff summary at:
    data/<result_container>/Steps Output Files/llm_trace_summary.txt

What it does NOT do:
  - signal assessment (removed: caller can infer signal strength from raw counts)

What it ADDS over the spec-name-only summary:
  - decoded TOP DISTINCTIVE FLAKY-ONLY trace sequences (signal for OD/TD/ID
    where the failing run executes ADDITIONAL events)
  - decoded TOP DISTINCTIVE CLEAN-ONLY trace sequences (signal for NIO
    where the FIXED variant executes cleanup events the FLAKY variant
    misses — e.g., `Collection.clear()` cycles. Without this section, NIO
    diffs whose only signal is "missing behavior in the broken run" reach
    the LLM as just a count, with no behavioral detail.)
  - decoded TOP FREQUENCY DIFFERENCES (largest |Δ| first)
  - SOURCE-LEVEL LOCATION MISMATCHES (the set of `Class.method(File:line)`
    triples reported by compare-traces — these often point straight at the
    cleanup site or the polluted-state read site, but were previously
    discarded as a count without detail)

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
# Script lives in AF_Claude_Agent/LLM Scripts/ ; data and CSV are one level up
# (in AF_Claude_Agent/), and events_encoding_id.txt is two levels up in scripts/.
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
#
# Note `[^\]]*` (zero-or-more, NOT one-or-more): compare-traces sometimes
# emits an EMPTY trace pattern `[]` for divergent control-flow paths whose
# events were skipped by the upstream "will skip ... event location mismatch"
# pass. Those entries still count and still carry signal (they tell us the
# failing run reached an abnormal-exit path the passing run didn't, even if
# the specific events are not recoverable). The earlier `+` quantifier
# silently dropped these entries from the counts AND from the LLM context.
RE_ACTUAL_ONLY = re.compile(
    r"ERROR:\s*\[(?P<trace>[^\]]*)\].*is in actual\s*\((?P<count>\d+)\s*times?\).*not expected"
)
RE_EXPECTED_ONLY = re.compile(
    r"ERROR:\s*\[(?P<trace>[^\]]*)\].*is in expected\s*\((?P<count>\d+)\s*times?\).*not actual"
)
RE_FREQ_DIFF = re.compile(
    r"WARNING:\s*\[(?P<trace>[^\]]*)\].*frequency is (?P<expected>\d+)\s*in expected,\s*but is (?P<actual>\d+)"
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
        # An empty trace pattern `[]` from compare-traces means TraceMOP
        # recorded a divergent control-flow path whose constituent events
        # were filtered out by the "skip due to location mismatch" upstream
        # pass. The path itself is real signal (something diverged); the
        # specific events are not recoverable.
        return "(empty trace pattern — divergent abnormal-exit path; events suppressed by location-mismatch filter)"

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


# `Class.method(File.java:line)` triples inside the location-mismatch set
# literal. compare-traces emits these as a Python repr of a set, e.g.:
#   {'pkg.Cls.foo(File.java:123)', 'pkg.Cls$Inner.bar(File.java:456)'}
# The class part can include `$` for nested classes; the method part is
# either a normal identifier OR `<init>` (constructor) / `<clinit>` (static
# initializer) — JVM-internal names that appear in stack traces whenever
# the divergent event fires inside a constructor or static block. Without
# the angle-bracket alternation, those locations are silently dropped from
# the SOURCE LOCATION MISMATCHES section.
RE_LOC_TRIPLE = re.compile(
    r"([\w\$\.]+)\.(<init>|<clinit>|\w+)\(([\w\$\.\-]+):(\d+)\)"
)


def parse_step8c_entries(content):
    """
    Parse step_8_C_official.txt into structured entries.

    Returns:
      flaky_only:    list of (count, trace_inner)            sorted by count desc
      clean_only:    list of (count, trace_inner)            sorted by count desc
      freq_diffs:    list of (expected, actual, trace_inner) sorted by |Δ| desc
      loc_mismatch_count: int (count of "Locations don't match" header lines)
      loc_triples:   list of (class_fqn, method, file_basename, line_no:int)
                     parsed from the line(s) immediately following each
                     "Locations don't match" header. Deduplicated, file-then-line
                     sorted. Empty if no location mismatches were reported.
    """
    flaky_only, clean_only, freq_diffs = [], [], []
    loc_mismatch_count = 0
    loc_triples_seen = set()
    loc_triples = []

    raw_lines = content.splitlines()
    for idx, raw in enumerate(raw_lines):
        line = raw.strip()
        if not line:
            continue
        if "Locations don't match" in line:
            loc_mismatch_count += 1
            # The location set literal can appear in one of three layouts:
            #   (a) on the SAME line as the header  ("Locations don't match: {...}")
            #   (b) on the very next non-empty line  ({...})
            #   (c) split across MULTIPLE lines      ({'a',
            #                                         'b',
            #                                         'c'})
            # Python's repr() for a small set fits on one line but for very
            # large sets (or when the file was post-processed) it can wrap.
            # We accumulate lines from the header forward until we either
            # see the closing `}` OR hit a hard delimiter that means we've
            # left the set body (next ERROR/WARNING/will-skip line). Then we
            # extract every triple from the accumulated text in one shot.
            accumulator = raw  # include the header line itself for case (a)
            seen_open_brace = "{" in raw
            seen_close_brace = "}" in raw and seen_open_brace
            j = idx + 1
            while j < len(raw_lines) and not seen_close_brace:
                nxt = raw_lines[j]
                nxt_stripped = nxt.strip()
                if nxt_stripped.startswith(("ERROR:", "WARNING:", "will skip")):
                    break
                accumulator += "\n" + nxt
                if "{" in nxt_stripped:
                    seen_open_brace = True
                if seen_open_brace and "}" in nxt_stripped:
                    seen_close_brace = True
                j += 1
            for m in RE_LOC_TRIPLE.finditer(accumulator):
                cls, method, file_, lineno = m.group(1), m.group(2), m.group(3), int(m.group(4))
                key = (cls, method, file_, lineno)
                if key not in loc_triples_seen:
                    loc_triples_seen.add(key)
                    loc_triples.append(key)
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
    # File then line for stable, human-readable output.
    loc_triples.sort(key=lambda t: (t[2], t[3], t[0], t[1]))
    return flaky_only, clean_only, freq_diffs, loc_mismatch_count, loc_triples


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
def generate_summary(result_container, steps_dir=None, compare_file=None,
                     output_file=None):
    """Decode a compare-traces diff into an LLM-readable summary.

    Defaults reproduce the original non-agentic layout: the diff is read from
    `<data>/<container>/Steps Output Files/step_8_C_official.txt` and the
    summary written next to it. The agentic pipeline overrides these so it can
    point at its own `Steps_Output_Files/rv_trace_diff.log` and write the
    summary where `get_rv_trace_diff` expects it. Overriding the paths does not
    change any parsing behaviour.
    """
    base = os.path.join(DATA_DIR, result_container)
    if steps_dir is None:
        steps_dir = os.path.join(base, "Steps Output Files")
    os.makedirs(steps_dir, exist_ok=True)
    if compare_file is None:
        compare_file = os.path.join(steps_dir, "step_8_C_official.txt")
    if output_file is None:
        output_file = os.path.join(steps_dir, "llm_trace_summary.txt")

    if not os.path.isfile(compare_file):
        print(f"ERROR: {compare_file} not found. Run Step 8C first.", file=sys.stderr)
        sys.exit(1)

    events_map = load_events_encoding(EVENTS_FILE)
    csv_row = load_csv_row(CSV_FILE, result_container)

    content = read_file_auto_encoding(compare_file)
    flaky_only, clean_only, freq_diffs, loc_mismatch, loc_triples = \
        parse_step8c_entries(content)

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

    # --- Top distinctive clean-only trace sequences (decoded) ---
    # These are the events present in the PASSING run but MISSING from the
    # failing run. For OD/TD/ID this is usually less interesting than
    # flaky-only (the failing run did MORE bad things). For NIO with a
    # collection-clear fix, this is THE signal — the missing events are the
    # `Collection.clear()` calls (and the post-clear refill cycle) that the
    # broken variant fails to perform. Without this section, the LLM sees
    # only "Clean-only traces: 4" as a count with no behavioral detail.
    if clean_only:
        shown = min(TOP_N_FLAKY_TRACES, len(clean_only))
        lines.append(f"=== TOP DISTINCTIVE CLEAN-ONLY TRACE SEQUENCES "
                     f"(top {shown} of {len(clean_only)}) ===")
        lines.append("(Decoded event sequences seen ONLY in the passing run, sorted by count.")
        lines.append(" These are events the FIXED variant produces that the FLAKY variant")
        lines.append(" does NOT — typically the cleanup/reset behavior that's missing in")
        lines.append(" the broken version. For NIO bugs whose fix is a `.clear()` call on a")
        lines.append(" static collection, look here for the Collection-related event patterns.)")
        lines.append("")
        for i, (count, trace) in enumerate(clean_only[:TOP_N_FLAKY_TRACES], 1):
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

    # --- Source-level location mismatches ---
    # compare-traces emits a `Locations don't match` header followed by a
    # set literal of `Class.method(File:line)` triples. These are the SOURCE
    # SITES where the runs diverged — frequently the cleanup line in Fixed/
    # that doesn't exist in Flaky/, or the polluted-state read in the
    # second invocation. Counting them ('Location mismatches: N') is not
    # actionable; listing them with file+line+method IS.
    if loc_triples:
        # Group by file for compact output.
        by_file = defaultdict(list)
        for cls, method, file_, lineno in loc_triples:
            by_file[file_].append((lineno, cls, method))
        lines.append(f"=== SOURCE LOCATION MISMATCHES ({len(loc_triples)} sites in {len(by_file)} file(s)) ===")
        lines.append("(Concrete source positions where TraceMOP saw divergent events between")
        lines.append(" the two runs. Each line is `File:line  Class.method`. For NIO bugs the")
        lines.append(" line numbers usually point at either (a) the cleanup site that exists")
        lines.append(" only in Fixed/ or (b) the polluted-state read in the failing iteration.)")
        lines.append("")
        for file_ in sorted(by_file):
            lines.append(f"  {file_}:")
            for lineno, cls, method in sorted(by_file[file_]):
                # Use the simple class name (after the last `.`) for readability;
                # full FQN is unhelpful when every entry shares the same package.
                simple_cls = cls.rsplit(".", 1)[-1]
                lines.append(f"    line {lineno:>4}   {simple_cls}.{method}")
        lines.append("")

    # --- How to read this (replaces the long generic interpretation guide) ---
    lines.append("--- HOW TO READ THIS ---")
    lines.append("Three classes of evidence, in rough priority order:")
    lines.append("  1. SOURCE LOCATION MISMATCHES — concrete File:line sites. Start here.")
    lines.append("  2. TOP DISTINCTIVE FLAKY-ONLY TRACE SEQUENCES — extra events the failing")
    lines.append("     run produces. Strongest signal for OD/TD/ID.")
    lines.append("  3. TOP DISTINCTIVE CLEAN-ONLY TRACE SEQUENCES — events the FIXED variant")
    lines.append("     produces that the FLAKY one misses. Strongest signal for NIO bugs")
    lines.append("     whose fix adds cleanup behavior (e.g., a `.clear()` call).")
    lines.append("Frequency differences are a weaker secondary signal — same code path,")
    lines.append("different repetition count.")
    lines.append("")

    # --- Write ---
    output_text = "\n".join(lines)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(output_text)
    print(output_text)
    print(f"\nSaved to: {output_file}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Decode a compare-traces diff into an LLM-readable summary.")
    ap.add_argument("result_container")
    ap.add_argument("--steps-dir",
                    help="output dir for llm_trace_summary.txt "
                         "(default: <data>/<container>/Steps Output Files)")
    ap.add_argument("--compare-file",
                    help="path to the compare-traces diff to decode "
                         "(default: <steps-dir>/step_8_C_official.txt)")
    ap.add_argument("--out-file",
                    help="explicit output path (default: <steps-dir>/llm_trace_summary.txt)")
    a = ap.parse_args()
    generate_summary(a.result_container, steps_dir=a.steps_dir,
                     compare_file=a.compare_file, output_file=a.out_file)
