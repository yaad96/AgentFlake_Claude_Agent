#!/usr/bin/env python3
"""
run_pass_at_k.py — pass@k harness for ReproFlake containers.

Runs run_<test_type>_tracemop.sh N times for each requested model, archives
per-run artifacts (excluding Flakym2/ and any target/), and emits summary
CSV + Markdown reports under '<container> runs/'.

This is a SMOKE-TEST version: minimal pre-flight, no lock file, no disk
check, no progress ETA. Verify the core loop works, then add hardening.

Usage:
  ./run_pass_at_k.py <container> --rv-traces <yes|no> [--models claude,openai] [--runs 3]

--rv-traces selects the LLM-context variant for the ablation:
  yes -> include the RV TRACE ANALYSIS section; archive under
         data/FULL_RUNS_RV/<container> runs/
  no  -> omit the RV TRACE ANALYSIS section from the LLM context; archive
         under data/FULL_RUNS_NO_RV/<container> runs/

Every invocation re-runs all requested (model, run) combinations end-to-end,
overwriting any prior per-run folder for those combinations.
"""

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
DATA_DIR = REPROFLAKE_DIR / "data"
CSV_FILE = REPROFLAKE_DIR / "test_config.csv"

TYPE_TO_SCRIPT = {
    "od":  SCRIPT_DIR / "run_od_tracemop.sh",
    "td":  SCRIPT_DIR / "run_td_tracemop.sh",
    "id":  SCRIPT_DIR / "run_id_tracemop.sh",
    "nio": SCRIPT_DIR / "run_nio_tracemop.sh",
}
MODEL_API_KEY = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

# Per-run folder name on disk for each model. Was `<container> <model>` (i.e.
# the model name lowercase, with the container name prefixed redundantly);
# now just the model name with canonical capitalization. The lowercase
# `model` token in `MODEL_API_KEY` is still used everywhere else (CLI, env
# var lookup, summary CSV column); this map only governs directory naming.
MODEL_DIR_NAME = {"claude": "Claude", "openai": "OpenAI"}
# Reverse lookup for scanning existing run folders.
DIR_NAME_TO_MODEL = {v: k for k, v in MODEL_DIR_NAME.items()}

# Per-run sentinel file written ONLY after the archive is fully complete.
# Records `exit_code` and `elapsed` (in seconds); parse_run reads `elapsed`
# back out for rows picked up off disk by collect_all_rows_on_disk.
SENTINEL = ".run_complete"

# Cross-invocation per-run log appended at the end of every run_pass_at_k.py
# invocation. Lives at the repo root (outside data/) so it survives container
# cleanups and accumulates across runs. Append-only: header is written on
# first invocation; subsequent invocations only append rows.
#
# Columns are intentionally minimal: container-level metadata that lives in
# test_config.csv (victim FQN, polluter, module, java, nondex seed, zip,
# config, url) is NOT duplicated here — join back to test_config.csv on the
# `container` column. We do keep `test_type` here as a convenience for
# slicing without a join.
COMPLETE_SUMMARY_FILE = REPROFLAKE_DIR / "Complete Containers Summary.csv"
COMPLETE_SUMMARY_COLS = [
    "timestamp", "container", "test_type", "model", "run", "final verdict",
    "turns_taken", "rv_traces_used",
    # Phase 5 feedback-round columns. Pre-feedback rows in the existing CSV
    # leave these empty; csv.DictWriter handles missing keys via
    # extrasaction="ignore" without complaint. Reading consumers (pandas etc)
    # see "" / NaN for old rows and "yes"/"no" for new ones.
    "feedback_used", "verdict_pre_feedback", "feedback_category",
    # Cost/perf columns at the end so the verdict + feedback-status columns
    # are visible without horizontal scroll in spreadsheet viewers.
    "input_tokens", "output_tokens", "total_tokens", "llm_seconds",
]


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def load_csv_row(container):
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("result_container", "").strip() == container:
                return row
    return None


def preflight(container, models):
    if not CSV_FILE.is_file():
        sys.exit(f"ERROR: CSV not found: {CSV_FILE}")
    row = load_csv_row(container)
    if not row:
        sys.exit(f"ERROR: container '{container}' not in CSV")
    test_type = row["test_type"].strip().lower()
    if test_type not in TYPE_TO_SCRIPT:
        sys.exit(f"ERROR: unsupported test_type '{test_type}'")
    script = TYPE_TO_SCRIPT[test_type]
    if not script.is_file():
        sys.exit(f"ERROR: script not found: {script}")

    for m in models:
        if m not in MODEL_API_KEY:
            sys.exit(f"ERROR: unknown model '{m}'")
    # NB: API-key check is DEFERRED to per-run execution time so that a
    # `--models claude` invocation doesn't fail just because OPENAI_API_KEY
    # is missing (and vice-versa). The check happens inside the per-(model,
    # run) loop, right before the per-type script is called.

    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        sys.exit("ERROR: Docker daemon not reachable")

    return row, test_type, script


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def archive_run(data_dir: Path, per_run_dir: Path):
    """Mirror data/<container>/ into per_run_dir/, excluding Flakym2/ and
    any target/ subdir. Defensive across all 4 test types — copytree silently
    skips missing entries so the same call works for OD/TD/ID/NIO."""
    skip_target = shutil.ignore_patterns("target")

    sources_with_target = [
        ("Fixed",            skip_target),
        ("Flaky",            skip_target),
        ("FlakyCodeChange",  skip_target),  # TD only
    ]
    sources_no_target = [
        ("result",           None),
        ("traces-fixed",     None),
        ("traces-flaky",     None),
        ("traces-flakycc",   None),  # TD
        ("traces-pass",      None),  # ID
        ("traces-fail",      None),  # ID
    ]
    for sub, ignore in sources_with_target + sources_no_target:
        src = data_dir / sub
        if src.is_dir():
            shutil.copytree(src, per_run_dir / sub, symlinks=True, ignore=ignore)

    steps = data_dir / "Steps Output Files"
    if steps.is_dir():
        shutil.copytree(steps, per_run_dir / "Steps Output Files", symlinks=True)

    for f in ["Fixed.patch", "FlakyCodeChange.patch", "FixedCodeChange.patch",
              "flaky_info.txt", "issue_description.txt"]:
        src = data_dir / f
        if src.is_file():
            shutil.copy2(src, per_run_dir / f)


# ---------------------------------------------------------------------------
# Per-run extraction (for the summary)
# ---------------------------------------------------------------------------

def safe_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_run(per_run_dir: Path, container, test_type, model, run_n):
    """Extract a single CSV row's worth of data from a per-run folder."""
    steps = per_run_dir / "Steps Output Files"
    verdict_file = steps / "verify_after_fix.verdict"
    apply_file = steps / "apply_report.json"
    llm_resp = steps / "llm_response.json"
    llm_t1 = steps / "llm_response_turn1.json"
    llm_t2 = steps / "llm_response_turn2.json"
    llm_t3 = steps / "llm_response_turn3.json"
    llm_t4 = steps / "llm_response_turn4.json"
    artifacts_t2 = steps / "llm_artifacts_turn2.txt"
    verify_log = steps / "verify_after_fix.log"
    pipeline = per_run_dir / "pipeline.log"

    # Feedback-round artifacts (only present when feedback fired).
    pre_verdict_file = steps / "verify_after_fix_pre_feedback.verdict"
    fail_category_file = steps / "fail_category.txt"

    verdict = "INCOMPLETE"
    if verdict_file.is_file():
        v = verdict_file.read_text(encoding="utf-8").strip()
        if v in ("PASSED", "FAILED"):
            verdict = v

    apply = safe_json(apply_file) or {}
    resp = safe_json(llm_resp) or {}
    t1 = safe_json(llm_t1) or {}
    t2 = safe_json(llm_t2) or {}
    t3 = safe_json(llm_t3) or {}
    t4 = safe_json(llm_t4) or {}

    # Token shape varies (claude vs openai). Claude with prompt caching
    # splits input across `input_tokens` (uncached new), `cache_creation_input_tokens`
    # (newly cached), and `cache_read_input_tokens` (re-used from cache); the
    # full input charge is the SUM of these three. OpenAI uses `prompt_tokens`.
    # turn3 (when feedback fired) and turn4 (Option B: when feedback's turn 3
    # requested artifacts) use the same shape as turns 1-2 for their backend,
    # so the same key list works for all four.
    def toks(d, *keys):
        u = d.get("usage") or {}
        return sum((u.get(k) or 0) for k in keys)

    INPUT_KEYS = ("input_tokens", "cache_creation_input_tokens",
                  "cache_read_input_tokens", "prompt_tokens")
    OUTPUT_KEYS = ("output_tokens", "completion_tokens")

    in_tokens = (toks(t1, *INPUT_KEYS) + toks(t2, *INPUT_KEYS)
                 + toks(t3, *INPUT_KEYS) + toks(t4, *INPUT_KEYS))
    out_tokens = (toks(t1, *OUTPUT_KEYS) + toks(t2, *OUTPUT_KEYS)
                  + toks(t3, *OUTPUT_KEYS) + toks(t4, *OUTPUT_KEYS))
    total = in_tokens + out_tokens

    # Default turns = count of per-turn files actually on disk. Slots aren't
    # dense: a feedback round after a 1-turn initial leaves t1 + t3 (no t2),
    # so "highest slot present" would over-count by 1. llm_response.json's
    # turns_taken (set by call_llm scripts) takes precedence; the default
    # fires only on malformed prior state.
    default_turns = sum(1 for f in (llm_t1, llm_t2, llm_t3, llm_t4) if f.is_file())
    turns = resp.get("turns_taken", default_turns)
    # `(t4 or t3 or t2 or t1)` picks the latest turn that actually ran for
    # the canonical finish reason — feedback's turn4 (when artifact retrieval
    # fired) supersedes turn3, which supersedes turn2/1.
    finish = ((t4 or t3 or t2 or t1).get("stop_reason")
              or (t4 or t3 or t2 or t1).get("finish_reason") or "")
    elapsed_llm = float(
        (t1.get("elapsed_seconds") or 0)
        + (t2.get("elapsed_seconds") or 0)
        + (t3.get("elapsed_seconds") or 0)
        + (t4.get("elapsed_seconds") or 0)
    )

    artifacts_req = len(t1.get("artifacts_requested") or [])
    artifacts_miss = 0
    if artifacts_t2.is_file():
        artifacts_miss = len(re.findall(r"^\s*-.*\bMISS\b", artifacts_t2.read_text(encoding="utf-8", errors="replace"), re.M))

    # apply_report fields
    layers = apply.get("layers_attempted") or []
    result = apply.get("result") or {}
    apply_layer = result.get("layer") or "none"
    path_rewritten = any(bool(la.get("path_rewritten")) for la in layers)
    imports_inferred = []
    for la in layers:
        for ap in (la.get("applied") or []):
            imports_inferred.extend(ap.get("imports_inferred") or [])
    recompile = apply.get("recompile") or {}
    recompile_ok = recompile.get("ok") if recompile and not recompile.get("skipped") else None
    compile_d = apply.get("compile") or {}
    host_compile_ok = compile_d.get("all_ok") if compile_d and not compile_d.get("skipped") else None

    # Verify log parse
    tests = failures = errors = markers = 0
    fail_snippet = ""
    if verify_log.is_file():
        log = verify_log.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)", log):
            tests, failures, errors = int(m.group(1)), int(m.group(2)), int(m.group(3))
        markers = len(re.findall(r"<<< (?:FAILURE|ERROR)!", log))
        if markers > 0:
            for line in log.splitlines():
                if "<<< FAILURE!" in line or "<<< ERROR!" in line:
                    fail_snippet = line.strip()[:200]
                    break

    # Recover elapsed_total_seconds from the sentinel for runs parsed off
    # disk that weren't executed in this invocation (e.g., picked up by
    # collect_all_rows_on_disk from a prior batch with a different --models
    # selection). The sentinel was written as `elapsed=<float>`; without
    # this, avg wall time in the summary collapses to 0 for those rows.
    elapsed_total = 0.0
    sentinel_file = per_run_dir / SENTINEL
    if sentinel_file.is_file():
        for line in sentinel_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("elapsed="):
                try:
                    elapsed_total = float(line.split("=", 1)[1])
                except ValueError:
                    pass
                break

    # Categorise the failure
    cat = classify(verdict, apply, recompile_ok, failures, errors, markers, pipeline)

    if not fail_snippet and verdict != "PASSED":
        # Pull from apply_report's reason if no test-level snippet
        for la in layers:
            r = la.get("reason") or ""
            if r:
                fail_snippet = r.replace("\n", " | ")[:200]
                break
        if not fail_snippet:
            fail_snippet = result.get("reason", "")[:200]

    # Feedback-round status. Existence of llm_response_turn3.json is the
    # canonical signal — only _feedback_main writes that file, and the
    # call_llm scripts' stale-cleanup at top of main() removes it from a
    # prior run. We previously gated on verify_after_fix_pre_feedback.verdict,
    # but that file is written by feedback_loop.sh's snapshot step (which
    # also doesn't get cleaned by the orchestrator's step 0), so a clean
    # iter-1 PASS in run N+1 would inherit run N's pre_feedback files and
    # be falsely tagged feedback_used=yes. (Note: turns_taken alone can't
    # disambiguate — a feedback round with no-artifacts-in-turn-1 and
    # no-artifacts-in-turn-3 produces turns_taken=2, indistinguishable from
    # a non-feedback turn-1+turn-2 run; only the t3 file presence is reliable.)
    if llm_t3.is_file():
        feedback_used = "yes"
        if pre_verdict_file.is_file():
            pv = pre_verdict_file.read_text(encoding="utf-8", errors="replace").strip()
            verdict_pre_feedback = pv if pv in ("PASSED", "FAILED", "INCOMPLETE") else "INCOMPLETE"
        else:
            verdict_pre_feedback = "INCOMPLETE"
        feedback_category = (
            fail_category_file.read_text(encoding="utf-8", errors="replace").strip()
            if fail_category_file.is_file() else ""
        )
    else:
        feedback_used = "no"
        verdict_pre_feedback = verdict
        feedback_category = ""

    return {
        "container": container,
        "test_type": test_type,
        "model": model,
        "run": run_n,
        "verdict": verdict,
        "fail_category": cat,
        "turns_taken": turns,
        "artifacts_requested": artifacts_req,
        "artifacts_miss": artifacts_miss,
        "input_tokens_total": in_tokens,
        "output_tokens_total": out_tokens,
        "total_tokens": total,
        "llm_finish_reason": finish,
        "elapsed_llm_seconds": elapsed_llm,
        "apply_layer": apply_layer,
        "apply_path_rewritten": path_rewritten,
        "apply_imports_inferred": ";".join(imports_inferred),
        "recompile_ok": recompile_ok,
        "host_compile_ok": host_compile_ok,
        # Renamed from step_13_* — the harness reads from `verify_after_fix.log`,
        # so the column name should match the source artifact, not the
        # orchestrator's step numbering (which has gaps and could drift).
        "verify_tests": tests,
        "verify_failures": failures,
        "verify_errors": errors,
        "failure_markers": markers,
        "fail_snippet": fail_snippet,
        "elapsed_total_seconds": round(elapsed_total, 1),
        # Phase 5 feedback-round columns. Populated only when feedback fired
        # (presence of verify_after_fix_pre_feedback.verdict is the signal).
        # For older runs that predate the feedback loop, feedback_used stays
        # "no" and verdict_pre_feedback == verdict.
        "feedback_used": feedback_used,
        "verdict_pre_feedback": verdict_pre_feedback,
        "feedback_category": feedback_category,
    }


def classify(verdict, apply, recompile_ok, failures, errors, markers, pipeline):
    if verdict == "PASSED":
        return "passed"
    if verdict == "INCOMPLETE":
        return "incomplete"
    # verdict == FAILED — figure out why
    if pipeline.is_file():
        log = pipeline.read_text(encoding="utf-8", errors="replace")
        if any(s in log for s in [
            "ERROR: Flaky run had Failures=0",
            "ERROR: Flaky+wrapper passed unexpectedly",
            "ERROR: NonDex run produced 0 failures",
        ]):
            return "sanity_failed"
    result = (apply or {}).get("result") or {}
    if not result.get("ok") and result.get("layer") in (None, "none"):
        return "patch_apply_failed"
    if recompile_ok is False:
        return "compile_failed"
    if failures + errors > 0 or markers > 0:
        return "test_failed"
    return "unknown_failure"


# ---------------------------------------------------------------------------
# pass@k
# ---------------------------------------------------------------------------

def pass_at_k(n, c, k):
    if c == 0:
        return 0.0
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


# ---------------------------------------------------------------------------
# Summary writers
# ---------------------------------------------------------------------------

CSV_COLS = [
    "container", "test_type", "model", "run", "verdict", "fail_category",
    "turns_taken", "artifacts_requested", "artifacts_miss",
    "input_tokens_total", "output_tokens_total", "total_tokens",
    "llm_finish_reason", "elapsed_llm_seconds", "elapsed_total_seconds",
    "apply_layer", "apply_path_rewritten", "apply_imports_inferred",
    "recompile_ok", "host_compile_ok",
    "verify_tests", "verify_failures", "verify_errors", "failure_markers",
    "fail_snippet",
    # Phase 5 feedback-round columns.
    "feedback_used", "verdict_pre_feedback", "feedback_category",
]


def collect_all_rows_on_disk(runs_root: Path, container: str, test_type: str) -> list:
    """Walk runs_root for ALL existing per-run folders (across every model
    that's ever been run) and parse each one. This makes summary.csv/.md
    reflect everything currently on disk — not just the runs executed in
    THIS invocation. Without this, a single-model invocation (say, just
    --models openai) would truncate summary.csv to only openai rows, losing
    a prior claude batch's results.

    Layout expected:  runs_root/<ModelDirName>/run <N>/...
    where <ModelDirName> is one of MODEL_DIR_NAME's values (e.g. 'Claude',
    'OpenAI'). Returns deterministic order: model alphabetical, run
    integer-sorted."""
    rows = []
    if not runs_root.is_dir():
        return rows
    for model_dir in sorted(runs_root.iterdir()):
        if not model_dir.is_dir():
            continue
        model = DIR_NAME_TO_MODEL.get(model_dir.name)
        if model is None:
            continue
        run_dirs = []
        for d in model_dir.iterdir():
            if not d.is_dir():
                continue
            m = re.match(r"run (\d+)$", d.name)
            if m:
                run_dirs.append((int(m.group(1)), d))
        run_dirs.sort()
        for run_n, d in run_dirs:
            rows.append(parse_run(d, container, test_type, model, run_n))
    return rows


_first_append_this_process = True


def append_complete_summary(rows, rv_traces):
    """Append one row per (model, run) from THIS invocation to the
    cross-invocation log at COMPLETE_SUMMARY_FILE.

    The `rv_traces_used` column ('yes'/'no') records whether the ablation
    included the RV TRACE ANALYSIS section in the LLM context for this
    invocation.

    Container-level metadata other than test_type (victim FQN, polluter,
    module, java, nondex seed, etc.) is NOT duplicated here — join back to
    test_config.csv on the `container` column to look it up.

    Visual separation between invocations: the FIRST call in this Python
    process does a full rewrite (also handling any schema migration) and
    inserts a single blank line between the prior content and this run's
    rows. Subsequent calls in the same process append directly to the file,
    so all rows from one run_pass_at_k.py invocation stay in one contiguous
    block, with a blank gap before the next invocation's block. The blank
    line is invisible to pandas (skip_blank_lines=True default) but renders
    as a gap in spreadsheets / cat / less.

    Schema migration: when COMPLETE_SUMMARY_COLS gains new columns (e.g.
    Phase 5 added feedback_used / verdict_pre_feedback / feedback_category)
    a plain append would corrupt the file's column structure. The first-call
    rewrite path handles this; later calls in the same process can safely
    append because schema is stable within a single invocation.
    """
    global _first_append_this_process
    if not rows:
        return
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new_row_dicts = []
    for r in rows:
        new_row_dicts.append({
            "timestamp": timestamp,
            "container": r["container"],
            "test_type": r["test_type"],
            "model": r["model"],
            "run": f"run {r['run']}",
            "final verdict": r["verdict"],
            "turns_taken": r["turns_taken"],
            "rv_traces_used": rv_traces,
            "feedback_used": r.get("feedback_used", "no"),
            "verdict_pre_feedback": r.get("verdict_pre_feedback", r["verdict"]),
            "feedback_category": r.get("feedback_category", ""),
            "input_tokens": r["input_tokens_total"],
            "output_tokens": r["output_tokens_total"],
            "total_tokens": r["total_tokens"],
            "llm_seconds": round(r["elapsed_llm_seconds"], 1),
        })

    if _first_append_this_process:
        _first_append_this_process = False
        # Fast path: if the on-disk header already matches the current schema,
        # just append a blank line + new rows. This preserves blank-line
        # separators from PRIOR invocations exactly — going through DictReader
        # would silently drop them (csv.DictReader skips blank lines).
        # Schema migration (full rewrite) only fires when the header differs.
        existing_header = None
        if COMPLETE_SUMMARY_FILE.is_file() and COMPLETE_SUMMARY_FILE.stat().st_size > 0:
            with open(COMPLETE_SUMMARY_FILE, encoding="utf-8", newline="") as f:
                try:
                    existing_header = next(csv.reader(f))
                except StopIteration:
                    existing_header = None

        if existing_header == COMPLETE_SUMMARY_COLS:
            # Append-with-separator path. File already in current schema.
            with open(COMPLETE_SUMMARY_FILE, "a", encoding="utf-8", newline="") as f:
                f.write("\n")  # blank-line separator between invocations
                w = csv.DictWriter(
                    f, fieldnames=COMPLETE_SUMMARY_COLS,
                    quoting=csv.QUOTE_ALL, extrasaction="ignore",
                )
                for r in new_row_dicts:
                    w.writerow(r)
        else:
            # Schema migration path: full rewrite via tmp+rename. Rare event
            # (only when COMPLETE_SUMMARY_COLS gains/renames columns). Blank
            # line separators from prior invocations are LOST during this
            # rewrite because DictReader skips them; they accumulate again
            # for subsequent invocations. Acceptable for a rare migration.
            existing_rows = []
            if COMPLETE_SUMMARY_FILE.is_file():
                with open(COMPLETE_SUMMARY_FILE, encoding="utf-8", newline="") as f:
                    existing_rows = list(csv.DictReader(f))

            # Schema rename: column "verdict" → "final verdict". Without this,
            # extrasaction="ignore" would silently drop the old column's data
            # leaving "final verdict" empty for every pre-rename row.
            for r in existing_rows:
                if "verdict" in r and "final verdict" not in r:
                    r["final verdict"] = r.pop("verdict")

            tmp_path = COMPLETE_SUMMARY_FILE.with_suffix(COMPLETE_SUMMARY_FILE.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(
                    f, fieldnames=COMPLETE_SUMMARY_COLS,
                    quoting=csv.QUOTE_ALL, extrasaction="ignore",
                )
                w.writeheader()
                for r in existing_rows:
                    w.writerow(r)
                if existing_rows:
                    f.write("\n")  # separator before this invocation's rows
                for r in new_row_dicts:
                    w.writerow(r)
            tmp_path.replace(COMPLETE_SUMMARY_FILE)
    else:
        # Subsequent calls in the same process: plain append, no separator,
        # no rewrite. Schema is stable within one invocation so this is safe.
        with open(COMPLETE_SUMMARY_FILE, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(
                f, fieldnames=COMPLETE_SUMMARY_COLS,
                quoting=csv.QUOTE_ALL, extrasaction="ignore",
            )
            for r in new_row_dicts:
                w.writerow(r)

    print(f"[wrapper] appended {len(rows)} row(s) to "
          f"{COMPLETE_SUMMARY_FILE.name}")


def write_summary(rows, runs_root: Path, container, row_meta, runs_per_model,
                  log_prefix="[wrapper]"):
    csv_path = runs_root / "summary.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, quoting=csv.QUOTE_ALL,
                           extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # summary.md generation disabled — only summary.csv is written.
    # md = [f"# {container} — pass@k report\n"]
    # md.append(f"**Test type**: {row_meta['test_type']}     "
    #           f"**Module**: {row_meta.get('module','')}     "
    #           f"**JDK**: {row_meta.get('java','')}\n")
    # md.append(f"**Polluter**: {row_meta.get('polluter/state setter','') or 'n/a'}\n"
    #           f"**Victim**:   {row_meta.get('flaky_test','')}\n")
    # md.append(f"\n## Aggregate (n={runs_per_model} per model)\n")
    # md.append("### Plausibility (test-passing rate; pass@k Chen et al.)\n")
    # md.append("| Model | runs passed | pass@1 (plausible) | pass@N (plausible) "
    #           "| avg tokens | avg wall time |")
    # md.append("|---|---|---|---|---|---|")
    # for m in sorted(set(r["model"] for r in rows)):
    #     m_rows = [r for r in rows if r["model"] == m]
    #     n = sum(1 for r in m_rows if r["verdict"] in ("PASSED", "FAILED"))
    #     c = sum(1 for r in m_rows if r["verdict"] == "PASSED")
    #     avg_tok = sum(r["total_tokens"] for r in m_rows) // max(1, len(m_rows))
    #     avg_wall = sum(r.get("elapsed_total_seconds", 0) for r in m_rows) / max(1, len(m_rows))
    #     p1 = pass_at_k(n, c, 1) if n else 0.0
    #     pN = pass_at_k(n, c, n) if n else 0.0
    #     md.append(f"| {m} | {c}/{n} | {p1:.0%} | {pN:.0%} | "
    #               f"{avg_tok:,} | {avg_wall:.0f}s |")
    #
    # md.append("\n### Failure breakdown by category\n")
    # cats = ["passed", "incomplete", "sanity_failed", "llm_failed",
    #         "patch_apply_failed", "compile_failed", "test_failed", "unknown_failure"]
    # md.append("| Model | " + " | ".join(cats) + " |")
    # md.append("|" + "---|" * (len(cats) + 1))
    # for m in sorted(set(r["model"] for r in rows)):
    #     cells = []
    #     for c in cats:
    #         n = sum(1 for r in rows if r["model"] == m and r["fail_category"] == c)
    #         cells.append(str(n))
    #     md.append(f"| {m} | " + " | ".join(cells) + " |")
    #
    # md.append("\n## Per-run details\n")
    # for r in rows:
    #     diag = ""
    #     model_dir_name = MODEL_DIR_NAME.get(r["model"], r["model"])
    #     d = safe_json(
    #         runs_root / model_dir_name / f"run {r['run']}"
    #         / "Steps Output Files" / "llm_response.json"
    #     ) or {}
    #     resp = d.get("response") or {}
    #     if isinstance(resp, dict):
    #         # `.get("diagnosis", "")` returns None (not "") when the key exists
    #         # with a None value — common in the parsed-LLM-output shape. Use
    #         # `or ""` to coerce both missing-key and None-value to empty string.
    #         diag = ((resp.get("output_0") or {}).get("diagnosis") or "")[:250]
    #     md.append(f"### {r['model']} / run {r['run']} — {r['verdict']} [{r['fail_category']}]\n")
    #     md.append(f"- **LLM**: {r['turns_taken']} turn(s), "
    #               f"{r['input_tokens_total']:,} in / {r['output_tokens_total']:,} out tokens; "
    #               f"finish={r['llm_finish_reason']}")
    #     md.append(f"- **Apply**: layer=`{r['apply_layer']}` · "
    #               f"path_rewritten={r['apply_path_rewritten']} · "
    #               f"imports_inferred=`{r['apply_imports_inferred'] or '—'}`")
    #     md.append(f"- **Compile**: recompile_ok={r['recompile_ok']} · "
    #               f"host_compile_ok={r['host_compile_ok']}")
    #     md.append(f"- **Verify**: tests={r['verify_tests']} failures={r['verify_failures']} "
    #               f"errors={r['verify_errors']} · markers={r['failure_markers']}")
    #     if diag:
    #         md.append(f"- **Diagnosis (first 250 chars)**:\n  > {diag.replace(chr(10), ' ')}\n")
    #     if r["fail_snippet"]:
    #         md.append(f"- **Fail snippet**: `{r['fail_snippet']}`")
    #     # Markdown link — model dir name has no spaces, only the `run N`
    #     # segment needs URL-encoded spaces. TD test type archives the patched
    #     # variant under FlakyCodeChange/ instead of Flaky/, so the source-link
    #     # leaf name follows the test_type.
    #     link_dir = f"{model_dir_name}/run%20{r['run']}"
    #     src_dir = "FlakyCodeChange" if r["test_type"] == "td" else "Flaky"
    #     md.append(f"- 📁 [pipeline.log]({link_dir}/pipeline.log) · "
    #               f"[Steps Output Files/]({link_dir}/Steps%20Output%20Files/) · "
    #               f"[{src_dir}/]({link_dir}/{src_dir}/)\n")
    #
    # md_path = runs_root / "summary.md"
    # md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"{log_prefix} summary written: {csv_path.name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("container")
    ap.add_argument("--rv-traces", choices=["yes", "no"], required=True,
                    help="ablation switch: 'yes' includes the RV TRACE "
                         "ANALYSIS section in the LLM context and archives "
                         "under data/FULL_RUNS_RV/; 'no' omits the section "
                         "and archives under data/FULL_RUNS_NO_RV/. "
                         "Required, no default.")
    ap.add_argument("--models", default="claude,openai")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--keep-workspace", action="store_true",
                    help="don't clean up data/<container>/ scratch workspace + "
                         "docker container after the batch (default: clean up; "
                         "the per-run folders already have their own snapshots)")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    row, test_type, script = preflight(args.container, models)

    # Place the runs folder INSIDE data/ so it inherits the project's existing
    # `data/` gitignore. Putting it at REPROFLAKE_DIR/ would make every
    # archived source tree show up as untracked (thousands of files) until
    # the user adds a separate gitignore entry. The RV/NO-RV split keeps the
    # two ablation modes' archives in separate trees so they don't collide.
    rv_dir_name = "FULL_RUNS_RV" if args.rv_traces == "yes" else "FULL_RUNS_NO_RV"
    runs_root = DATA_DIR / rv_dir_name / f"{args.container} runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    print(f"[wrapper] container={args.container}  test_type={test_type}  "
          f"models={models}  runs={args.runs}  rv_traces={args.rv_traces}")
    print(f"[wrapper] runs_root={runs_root}")

    data_container_dir = DATA_DIR / args.container

    rows = []
    for model in models:
        for run_n in range(1, args.runs + 1):
            per_run_dir = runs_root / MODEL_DIR_NAME[model] / f"run {run_n}"
            sentinel = per_run_dir / SENTINEL

            if not os.environ.get(MODEL_API_KEY[model]):
                sys.exit(f"ERROR: {MODEL_API_KEY[model]} env var not set "
                         f"(required to execute {model}/run {run_n})")

            if per_run_dir.exists():
                print(f"[wrapper] clearing {per_run_dir} for fresh run")
                shutil.rmtree(per_run_dir, ignore_errors=True)
            per_run_dir.mkdir(parents=True, exist_ok=True)

            # Wipe dynamic outputs from data/<container>/ so this run's
            # artifacts can't be confused with the previous run's. The per-type
            # script's "step 0" only cleans mutated source dirs (Fixed/, Flaky/,
            # etc.) and explicitly KEEPS Steps Output Files/ + traces-*/ +
            # result/ across runs — fine for human debugging, but if the next
            # run fails before regenerating those (e.g. mvn build dies at
            # step 4a), archive_run would copy the prior run's outputs into
            # the current per-run dir and parse_run would tag them as this
            # run's data. The bug: an early-failed openai run picked up the
            # preceding claude run's PASSED verdict + token counts verbatim.
            for stale in ("Steps Output Files", "result",
                          "traces-fixed", "traces-flaky", "traces-flakycc",
                          "traces-pass", "traces-fail"):
                stale_path = data_container_dir / stale
                if stale_path.is_dir():
                    shutil.rmtree(stale_path, ignore_errors=True)

            print(f"[wrapper] === starting {model}/run {run_n} ===")
            t0 = time.time()
            pipeline_log = per_run_dir / "pipeline.log"
            env = os.environ.copy()
            env.pop("KEEP_SOURCE", None)  # don't let user-set KEEP_SOURCE break run independence
            # Tell the orchestrator to leave its container running between
            # runs in this loop. The wrapper's per-(model,run) loop reuses
            # the same container so the ~30s start-up + extension-build
            # overhead is amortized across runs. Container is finally
            # removed by the wrapper's end-of-batch cleanup below.
            env["KEEP_CONTAINER"] = "1"
            # Tells run_<type>_tracemop.sh which assembler subfolder to use
            # (rv/ or no_rv/) for the LLM-context ablation. Read by the bash
            # script as ${RV_TRACES:-yes}, so a missing/empty value defaults
            # to including the RV section.
            env["RV_TRACES"] = args.rv_traces

            with open(pipeline_log, "w", encoding="utf-8") as logf:
                p = subprocess.Popen(
                    [str(script), args.container, model],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env=env, text=True, bufsize=1,
                )
                for line in p.stdout:
                    sys.stdout.write(line)
                    logf.write(line)
                p.wait()
                exit_code = p.returncode

            elapsed = time.time() - t0
            print(f"[wrapper] === finished {model}/run {run_n} (exit={exit_code}, "
                  f"wall={elapsed:.0f}s) ===")

            # Archive (sources first, Steps Output Files last; sentinel last of all)
            archive_run(data_container_dir, per_run_dir)

            # Defense-in-depth: if the per-type script exited non-zero, force
            # INCOMPLETE regardless of what's on disk. The pre-run wipe above
            # already prevents stale-data inheritance from the previous run,
            # but a script that crashes AFTER writing its own verdict.PASSED
            # (e.g., a sanity-check failure later in step 13) would otherwise
            # have its truncated state trusted. exit_code == 0 is the only
            # signal that the full pipeline ran end-to-end.
            v_file = per_run_dir / "Steps Output Files" / "verify_after_fix.verdict"
            if exit_code != 0:
                v_file.parent.mkdir(parents=True, exist_ok=True)
                v_file.write_text("INCOMPLETE\n")
            elif not v_file.is_file():
                # Script exited 0 but no verdict was written (shouldn't happen
                # — verify step always writes one — but guard anyway).
                v_file.parent.mkdir(parents=True, exist_ok=True)
                v_file.write_text("INCOMPLETE\n")

            # Sentinel last
            sentinel.write_text(f"exit_code={exit_code}\nelapsed={elapsed:.1f}\n")

            row_data = parse_run(per_run_dir, args.container, test_type, model, run_n)
            row_data["elapsed_total_seconds"] = round(elapsed, 1)
            rows.append(row_data)

            # Incremental summary write — pull EVERY run on disk (not just
            # the ones from this invocation), so single-model invocations
            # don't truncate the summary and lose prior data.
            all_rows = collect_all_rows_on_disk(runs_root, args.container, test_type)
            write_summary(all_rows, runs_root, args.container, row, args.runs)

            # Append THIS run's row to the cross-invocation log immediately
            # so a mid-batch crash (or Ctrl+C) preserves partial progress and
            # downstream readers can tail the file live. append_complete_summary
            # does an atomic .tmp+rename rewrite, so calling it 6× per batch
            # (typical 2 models × 3 runs) is safe and adds <100ms total on a
            # ~100-row file. Each row gets its own completion-time timestamp
            # — different from the previous batch-shared-timestamp behavior.
            append_complete_summary([row_data], args.rv_traces)

    # Always refresh per-container summary at the end too, picking up any
    # prior on-disk runs (e.g., from earlier invocations with different
    # --models or after extractor changes the user wants reflected in
    # existing runs). Same disk-walk rationale as the incremental write.
    all_rows = collect_all_rows_on_disk(runs_root, args.container, test_type)
    if all_rows:
        write_summary(all_rows, runs_root, args.container, row, args.runs)

    # Clean up the scratch workspace at data/<container>/ — it's just the
    # last run's state, redundant with the archived per-run snapshots.
    # Also stop the docker container the per-type script left running
    # (and that bind-mounts the workspace, so it must die before we wipe).
    if not args.keep_workspace:
        container_name = "tm_" + re.sub(r"[^a-zA-Z0-9]", "_", args.container)
        subprocess.run(["docker", "rm", "-f", container_name],
                       capture_output=True)
        if data_container_dir.is_dir():
            print(f"[wrapper] cleaning workspace {data_container_dir.name}/")
            shutil.rmtree(data_container_dir)

    print(f"[wrapper] DONE. {sum(1 for r in rows if r['verdict']=='PASSED')}/{len(rows)} runs PASSED.")


if __name__ == "__main__":
    main()
