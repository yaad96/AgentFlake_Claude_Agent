#!/usr/bin/env python3
"""
run_agentic_pass_at_k.py — pass@k harness for the agentic pipeline.

Direct counterpart to TraceMop Scripts/run_pass_at_k.py, adapted for:
  - Per-type entry points under `agentic/` (run_agentic_<type>.sh)
  - Both Claude (Messages API) and OpenAI (Chat Completions) models; the
    orchestrator routes to the matching backend based on the model id.
  - Per-run archive layout that includes the agentic conversation
    transcript + per-iteration log produced by agentic_orchestrator.py
  - Reusing the existing CSV writer / pass@k metric so the agentic
    Complete Containers Summary stays joinable with non-agentic runs

Usage:
  ./run_agentic_pass_at_k.py <container> [--runs 3] [--max-iterations 10]
                             [--keep-workspace] [--model claude-sonnet-4-6]

Archives under:
  data/AGENTIC_FULL_RUNS/<container>_runs/Claude/run_<N>/
"""

from __future__ import annotations

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

import agentic_config  # type: ignore  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
DATA_DIR = REPROFLAKE_DIR / "data"
CSV_FILE = REPROFLAKE_DIR / "test_config.csv"

TYPE_TO_SCRIPT = {
    "od":           SCRIPT_DIR / "run_agentic_od.sh",
    "td":           SCRIPT_DIR / "run_agentic_td.sh",
    "id":           SCRIPT_DIR / "run_agentic_id.sh",
    "nio":          SCRIPT_DIR / "run_agentic_nio.sh",
    "unclassified": SCRIPT_DIR / "run_agentic_unclassified.sh",
    "unassigned":   SCRIPT_DIR / "run_agentic_unclassified.sh",
    "brittle":      SCRIPT_DIR / "run_agentic_brittle.sh",
    "britle":       SCRIPT_DIR / "run_agentic_brittle.sh",  # CSV typo alias
}

# Map model IDs (or short aliases) to the Anthropic API key env var.
# Any model starting with "claude" uses ANTHROPIC_API_KEY.
# gpt-*/o-series route to OPENAI_API_KEY; everything else to ANTHROPIC_API_KEY.
def _api_key_var(model_id: str) -> str:
    key = (model_id or "").strip().lower()
    if key.startswith(("gpt", "o1", "o3", "o4")):
        return "OPENAI_API_KEY"
    return "ANTHROPIC_API_KEY"  # default (claude-* and any other)

SENTINEL = ".run_complete"

# Reuse the cross-invocation log alongside the non-agentic runs, but separate
# by an `agentic` column so the existing reader scripts can filter. We DO use
# the same file path so a single dashboard sees both pipelines side-by-side.
COMPLETE_SUMMARY_FILE = REPROFLAKE_DIR / "Complete_Containers_Summary.csv"
COMPLETE_SUMMARY_COLS = [
    "timestamp", "container", "test_type", "model", "run", "final verdict",
    "rv_traces_used",
    "input_tokens", "output_tokens", "total_tokens", "llm_seconds",
    "validation_runs", "temperature", "tools_used",
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


def preflight(container):
    if not CSV_FILE.is_file():
        sys.exit(f"ERROR: CSV not found: {CSV_FILE}")
    row = load_csv_row(container)
    if not row:
        sys.exit(f"ERROR: container '{container}' not in CSV")
    test_type = row["test_type"].strip().lower()
    if test_type not in TYPE_TO_SCRIPT:
        sys.exit(f"ERROR: unsupported test_type '{test_type}' "
                 f"(supported: {', '.join(sorted(TYPE_TO_SCRIPT))})")
    script = TYPE_TO_SCRIPT[test_type]
    if not script.is_file():
        sys.exit(f"ERROR: agentic per-type script not found: {script}")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        sys.exit("ERROR: Docker daemon not reachable")
    return row, test_type, script


# ---------------------------------------------------------------------------
# Archive (same layout as non-agentic so existing tools work)
# ---------------------------------------------------------------------------

def archive_run(data_dir: Path, per_run_dir: Path):
    skip_target = shutil.ignore_patterns("target")
    sources_with_target = [
        ("Fixed", skip_target), ("Flaky", skip_target),
        ("FlakyCodeChange", skip_target),
    ]
    sources_no_target = [
        ("result", None),
        ("traces-fixed", None), ("traces-flaky", None),
        ("traces-flakycc", None), ("traces-pass", None), ("traces-fail", None),
    ]
    for sub, ignore in sources_with_target + sources_no_target:
        src = data_dir / sub
        if src.is_dir():
            shutil.copytree(src, per_run_dir / sub, symlinks=True, ignore=ignore)
    steps = data_dir / "Steps_Output_Files"
    if steps.is_dir():
        shutil.copytree(steps, per_run_dir / "Steps_Output_Files", symlinks=True)
    for f in ["Fixed.patch", "FlakyCodeChange.patch", "FixedCodeChange.patch",
              "flaky_info.txt", "issue_description.txt"]:
        src = data_dir / f
        if src.is_file():
            shutil.copy2(src, per_run_dir / f)


def docker_image_for_java(java_version: str) -> str:
    return {
        "8": "flaky_base_jdk8",
        "11": "flaky_base_jdk11",
        "17": "flaky_base_jdk17",
    }.get(str(java_version).strip(), "flaky_base_jdk8")


def restore_workspace_owner(container_name: str, data_dir: Path | None = None,
                            image: str | None = None):
    """Return bind-mounted outputs created by Docker root to the host user."""
    if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
        return
    uid, gid = os.getuid(), os.getgid()
    result = subprocess.run(
        ["docker", "exec", "-u", "0", container_name,
         "chown", "-R", f"{uid}:{gid}", "/app/work"],
        capture_output=True,
    )
    if result.returncode == 0 or not data_dir or not image or not data_dir.is_dir():
        return
    subprocess.run(
        ["docker", "run", "--rm",
         "--mount", f"type=bind,source={data_dir},target=/app/work",
         image, "chown", "-R", f"{uid}:{gid}", "/app/work"],
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Per-run parse
# ---------------------------------------------------------------------------

def safe_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_run(per_run_dir: Path, container, test_type, run_n, model="claude"):
    """Extract a single CSV row's worth of data from an agentic per-run
    folder. Returns a dict shaped to match parse_run in the non-agentic
    harness so downstream summary writers don't need to branch.
    """
    steps = per_run_dir / "Steps_Output_Files"
    run_verdict_file = steps / "run_verdict.txt"          # authoritative 3-state
    verdict_file = steps / "verify_after_fix.verdict"     # binary fallback
    apply_file = steps / "apply_report.json"
    llm_resp = steps / "llm_response.json"
    iter_log = steps / "agentic_iterations.jsonl"
    verify_log = steps / "verify_after_fix.log"
    pipeline = per_run_dir / "pipeline.log"

    # Prefer the authoritative run verdict; fall back to the binary verify
    # file for older runs that predate run_verdict.txt.
    verdict = "INCOMPLETE"
    for vf in (run_verdict_file, verdict_file):
        if vf.is_file():
            v = vf.read_text(encoding="utf-8").strip()
            if v in ("PASSED", "FAILED", "INCOMPLETE"):
                verdict = v
                break

    apply_rep = safe_json(apply_file) or {}
    resp = safe_json(llm_resp) or {}

    # Token counts live on llm_response.json["usage"] for agentic runs (the
    # orchestrator's _sum_usage tracks them cumulatively across all turns
    # within the iteration loop).
    usage = resp.get("usage") or {}
    in_tokens = ((usage.get("input_tokens") or 0)
                 + (usage.get("cache_creation_input_tokens") or 0)
                 + (usage.get("cache_read_input_tokens") or 0))
    out_tokens = usage.get("output_tokens") or 0
    total = in_tokens + out_tokens
    elapsed_llm = float(resp.get("elapsed_seconds") or 0)

    # Read per-iteration jsonl tail for finer-grained data if needed.
    iterations = []
    if iter_log.is_file():
        for line in iter_log.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
            try:
                iterations.append(json.loads(line))
            except Exception:
                continue

    # apply_report: layer + compile/recompile state.
    layers = apply_rep.get("layers_attempted") or []
    result = apply_rep.get("result") or {}
    apply_layer = result.get("layer") or "none"
    rc = apply_rep.get("recompile") or {}
    recompile_ok = rc.get("ok") if rc and not rc.get("skipped") else None
    compile_d = apply_rep.get("compile") or {}
    host_compile_ok = (compile_d.get("all_ok") if compile_d
                       and not compile_d.get("skipped") else None)
    path_rewritten = any(bool(la.get("path_rewritten")) for la in layers)
    imports_inferred = []
    for la in layers:
        for ap in (la.get("applied") or []):
            imports_inferred.extend(ap.get("imports_inferred") or [])

    # Verify log parse.
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

    elapsed_total = 0.0
    sentinel_file = per_run_dir / SENTINEL
    if sentinel_file.is_file():
        for line in sentinel_file.read_text(encoding="utf-8",
                                            errors="replace").splitlines():
            if line.startswith("elapsed="):
                try:
                    elapsed_total = float(line.split("=", 1)[1])
                except ValueError:
                    pass
                break

    cat = classify(verdict, apply_rep, recompile_ok, failures, errors,
                   markers, pipeline)
    if not fail_snippet and verdict != "PASSED":
        for la in layers:
            r = la.get("reason") or ""
            if r:
                fail_snippet = r.replace("\n", " | ")[:200]
                break
        if not fail_snippet:
            fail_snippet = result.get("reason", "")[:200]

    # Aggregate tool usage across all iterations of this run into a compact
    # "name:count; name:count" string (sorted by count desc, then name).
    tool_counts = {}
    for it in iterations:
        for t in (it.get("tools_used") or []):
            tool_counts[t] = tool_counts.get(t, 0) + 1
    tools_used_str = "; ".join(
        f"{name}:{cnt}" for name, cnt in
        sorted(tool_counts.items(), key=lambda kv: (-kv[1], kv[0])))

    return {
        "container": container,
        "test_type": test_type,
        "model": model,
        "run": run_n,
        "verdict": verdict,
        "tools_used": tools_used_str,
        "fail_category": cat,
        "input_tokens_total": in_tokens,
        "output_tokens_total": out_tokens,
        "total_tokens": total,
        "llm_finish_reason": resp.get("stop_reason") or "",
        "elapsed_llm_seconds": elapsed_llm,
        "apply_layer": apply_layer,
        "apply_path_rewritten": path_rewritten,
        "apply_imports_inferred": ";".join(imports_inferred),
        "recompile_ok": recompile_ok,
        "host_compile_ok": host_compile_ok,
        "verify_tests": tests,
        "verify_failures": failures,
        "verify_errors": errors,
        "failure_markers": markers,
        "fail_snippet": fail_snippet,
        "elapsed_total_seconds": round(elapsed_total, 1),
        "agentic_iterations": len(iterations),
    }


def classify(verdict, apply_rep, recompile_ok, failures, errors, markers, pipeline):
    if verdict == "PASSED":
        return "passed"
    if verdict == "INCOMPLETE":
        return "incomplete"
    if pipeline.is_file():
        log = pipeline.read_text(encoding="utf-8", errors="replace")
        if any(s in log for s in [
            "ERROR: Flaky run had Failures=0",
            "ERROR: Flaky+wrapper passed unexpectedly",
            "ERROR: NonDex run produced 0 failures",
        ]):
            return "sanity_failed"
    result = (apply_rep or {}).get("result") or {}
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
    "agentic_iterations",
    "input_tokens_total", "output_tokens_total", "total_tokens",
    "llm_finish_reason", "elapsed_llm_seconds", "elapsed_total_seconds",
    "apply_layer", "apply_path_rewritten", "apply_imports_inferred",
    "recompile_ok", "host_compile_ok",
    "verify_tests", "verify_failures", "verify_errors", "failure_markers",
    "fail_snippet",
]


def collect_all_rows_on_disk(runs_root: Path, container: str,
                             test_type: str) -> list:
    """Scan every model-dir/run-N sub-directory under runs_root. The model
    directory name is the model ID (or alias) as passed to --model; any
    subdirectory whose children match 'run N' is treated as a model directory.
    """
    rows = []
    if not runs_root.is_dir():
        return rows
    for model_dir in sorted(runs_root.iterdir()):
        if not model_dir.is_dir():
            continue
        run_dirs = []
        for d in model_dir.iterdir():
            if not d.is_dir():
                continue
            m = re.match(r"run_(\d+)$", d.name)
            if m:
                run_dirs.append((int(m.group(1)), d))
        if not run_dirs:
            continue
        run_dirs.sort()
        for run_n, d in run_dirs:
            rows.append(parse_run(d, container, test_type, run_n,
                                  model=model_dir.name))
    return rows


_first_append_this_process = True


def append_complete_summary(rows):
    """Append per-run rows to the shared Complete Containers Summary.csv.
    Tagged with rv_traces_used='agentic' so the agentic rows are visually
    and machine-distinguishable from the non-agentic pass@k batches.
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
            "run": f"run_{r['run']}",
            "final verdict": r["verdict"],
            "rv_traces_used": "agentic",
            "input_tokens": r["input_tokens_total"],
            "output_tokens": r["output_tokens_total"],
            "total_tokens": r["total_tokens"],
            "llm_seconds": round(r["elapsed_llm_seconds"], 1),
            "validation_runs": agentic_config.VERIFY_PASS_RUNS,
            "temperature": agentic_config.TEMPERATURE,
            "tools_used": r.get("tools_used", ""),
        })

    if _first_append_this_process:
        _first_append_this_process = False
        existing_header = None
        if COMPLETE_SUMMARY_FILE.is_file() and COMPLETE_SUMMARY_FILE.stat().st_size > 0:
            with open(COMPLETE_SUMMARY_FILE, encoding="utf-8", newline="") as f:
                try:
                    existing_header = next(csv.reader(f))
                except StopIteration:
                    existing_header = None
        if existing_header == COMPLETE_SUMMARY_COLS:
            with open(COMPLETE_SUMMARY_FILE, "a", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=COMPLETE_SUMMARY_COLS,
                                   quoting=csv.QUOTE_ALL, extrasaction="ignore")
                for r in new_row_dicts:
                    w.writerow(r)
        else:
            # Header drift / new file path: full rewrite. DictReader skips
            # blank lines, so any pre-existing blank separator rows are dropped
            # here and none are written back.
            existing_rows = []
            if COMPLETE_SUMMARY_FILE.is_file():
                with open(COMPLETE_SUMMARY_FILE, encoding="utf-8", newline="") as f:
                    existing_rows = list(csv.DictReader(f))
            for r in existing_rows:
                if "verdict" in r and "final verdict" not in r:
                    r["final verdict"] = r.pop("verdict")
            tmp = COMPLETE_SUMMARY_FILE.with_suffix(
                COMPLETE_SUMMARY_FILE.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=COMPLETE_SUMMARY_COLS,
                                   quoting=csv.QUOTE_ALL, extrasaction="ignore")
                w.writeheader()
                for r in existing_rows:
                    w.writerow(r)
                for r in new_row_dicts:
                    w.writerow(r)
            tmp.replace(COMPLETE_SUMMARY_FILE)
    else:
        with open(COMPLETE_SUMMARY_FILE, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COMPLETE_SUMMARY_COLS,
                               quoting=csv.QUOTE_ALL, extrasaction="ignore")
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
    print(f"{log_prefix} summary written: {csv_path.name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("container")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-iterations", type=int, default=10,
                    help="hard cap on submit_patch attempts per run (default 10)")
    ap.add_argument("--model", default="claude-sonnet-4-6",
                    help="Anthropic model id passed to agentic_orchestrator.py")
    ap.add_argument("--keep-workspace", action="store_true",
                    help="don't clean data/<container>/ scratch workspace after the batch")
    args = ap.parse_args()

    row, test_type, script = preflight(args.container)

    api_key_var = _api_key_var(args.model)
    if not os.environ.get(api_key_var):
        sys.exit(f"ERROR: {api_key_var} env var not set (required for model '{args.model}')")

    runs_root = DATA_DIR / "AGENTIC_FULL_RUNS" / f"{args.container}_runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    print(f"[wrapper] container={args.container}  test_type={test_type}  "
          f"runs={args.runs}  max_iterations={args.max_iterations}  "
          f"model={args.model}")
    print(f"[wrapper] runs_root={runs_root}")

    data_container_dir = DATA_DIR / args.container
    # Archive directory uses the model ID directly so multiple models can
    # coexist under the same runs_root without overwriting each other.
    model_dir_label = args.model
    container_name = "tm_" + re.sub(r"[^a-zA-Z0-9]", "_", args.container)
    docker_image = docker_image_for_java(row.get("java", "8"))

    rows = []
    for run_n in range(1, args.runs + 1):
        per_run_dir = runs_root / model_dir_label / f"run_{run_n}"
        sentinel = per_run_dir / SENTINEL

        if per_run_dir.exists():
            print(f"[wrapper] clearing {per_run_dir} for fresh run")
            shutil.rmtree(per_run_dir, ignore_errors=True)
        per_run_dir.mkdir(parents=True, exist_ok=True)

        restore_workspace_owner(container_name, data_container_dir, docker_image)

        # Wipe dynamic outputs so this run can't be contaminated by stale
        # artefacts from the previous run (same rationale as the non-agentic
        # harness — see run_pass_at_k.py).
        for stale in ("Steps_Output_Files", "result",
                      "traces-fixed", "traces-flaky", "traces-flakycc",
                      "traces-pass", "traces-fail"):
            stale_path = data_container_dir / stale
            if stale_path.is_dir():
                shutil.rmtree(stale_path, ignore_errors=True)

        print(f"[wrapper] === starting {args.model}/run_{run_n} ===")
        t0 = time.time()
        pipeline_log = per_run_dir / "pipeline.log"
        env = os.environ.copy()
        env.pop("KEEP_SOURCE", None)
        env["KEEP_CONTAINER"] = "1"
        env["AGENTIC_MAX_ITERATIONS"] = str(args.max_iterations)
        env["AGENTIC_MODEL"] = args.model
        # Stream the orchestrator's stdout live instead of block-buffering it
        # through this pipe, so [iter]/[apply]/[verify] lines appear in real time.
        env["PYTHONUNBUFFERED"] = "1"

        with open(pipeline_log, "w", encoding="utf-8") as logf:
            p = subprocess.Popen(
                [str(script), args.container],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, text=True, bufsize=1,
            )
            for line in p.stdout:
                sys.stdout.write(line)
                logf.write(line)
            p.wait()
            exit_code = p.returncode

        restore_workspace_owner(container_name, data_container_dir, docker_image)

        elapsed = time.time() - t0
        print(f"[wrapper] === finished {args.model}/run_{run_n} "
              f"(exit={exit_code}, wall={elapsed:.0f}s) ===")

        archive_run(data_container_dir, per_run_dir)

        # Defense in depth: if per-type script exited non-zero AND the
        # orchestrator did not already write INCOMPLETE/PASSED, force a
        # terminal verdict so parse_run can't misread a stale verdict.
        v_file = per_run_dir / "Steps_Output_Files" / "verify_after_fix.verdict"
        if exit_code != 0 and not v_file.is_file():
            v_file.parent.mkdir(parents=True, exist_ok=True)
            v_file.write_text("INCOMPLETE\n")
        elif not v_file.is_file():
            v_file.parent.mkdir(parents=True, exist_ok=True)
            v_file.write_text("INCOMPLETE\n")

        sentinel.write_text(f"exit_code={exit_code}\nelapsed={elapsed:.1f}\n")

        row_data = parse_run(per_run_dir, args.container, test_type, run_n,
                             model=args.model)
        row_data["elapsed_total_seconds"] = round(elapsed, 1)
        rows.append(row_data)

        all_rows = collect_all_rows_on_disk(runs_root, args.container,
                                            test_type)
        write_summary(all_rows, runs_root, args.container, row, args.runs)
        append_complete_summary([row_data])

    all_rows = collect_all_rows_on_disk(runs_root, args.container, test_type)
    if all_rows:
        write_summary(all_rows, runs_root, args.container, row, args.runs)

    if not args.keep_workspace:
        restore_workspace_owner(container_name, data_container_dir, docker_image)
        subprocess.run(["docker", "rm", "-f", container_name],
                       capture_output=True)
        if data_container_dir.is_dir():
            print(f"[wrapper] cleaning workspace {data_container_dir.name}/")
            shutil.rmtree(data_container_dir)

    n = sum(1 for r in rows if r['verdict'] in ('PASSED', 'FAILED'))
    c = sum(1 for r in rows if r['verdict'] == 'PASSED')
    p1 = pass_at_k(n, c, 1) if n else 0.0
    pN = pass_at_k(n, c, n) if n else 0.0
    print(f"[wrapper] DONE. {c}/{n} runs PASSED  "
          f"pass@1={p1:.0%}  pass@{n}={pN:.0%}")

    # Exit nonzero when no run passed, so the dispatcher (run_agentic.py) and
    # any CI caller reflect the real repair outcome rather than just "the
    # batch completed". c = number of PASSED runs.
    sys.exit(0 if c > 0 else 1)


if __name__ == "__main__":
    main()
