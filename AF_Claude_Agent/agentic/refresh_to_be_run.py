#!/usr/bin/env python3
"""
refresh_to_be_run.py — regenerate the per-type "still to run" lists.

    to_be_run_<type>.csv  =  cohorts/<type>.csv  −  containers already run

The cohort files are hand-curated inputs (which containers belong to this
experiment); test_config.csv holds far more containers than any cohort, so the
cohort cannot be re-derived from it. Everything else is derived, so this script
is safe to re-run at any time — after every batch, or from a cron.

Evidence that a container has been run comes from either:

  summary  Complete_Containers_Summary.csv — the cross-invocation ledger that
           run_agentic_pass_at_k.py appends to after every single run.
  data     data/<container>/run_NN/.run_complete sentinel files on disk.

Default is `both` (union), so a container still counts as done if one source
was lost — the ledger is gitignored and the data/ tree is prunable.

On top of those, an optional per-type override file

    cohorts/<type>.done.csv

lists containers that were run before the ledger existed, or on another
machine. They are treated as complete regardless of --min-runs. This is the
only place a completed run can be recorded by hand; the ledger and data/ tree
are otherwise the sole authority.

Usage:
    ./refresh_to_be_run.py                       # refresh all four types
    ./refresh_to_be_run.py --types td,nio
    ./refresh_to_be_run.py --min-runs 3          # done only at >=3 runs
    ./refresh_to_be_run.py --require-terminal    # INCOMPLETE runs don't count
    ./refresh_to_be_run.py --dry-run             # report, write nothing
    ./refresh_to_be_run.py --seed-cohort --types od   # create a missing cohort
                                                      # from all test_config rows
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
CSV_FILE = REPROFLAKE_DIR / "test_config.csv"
COHORT_DIR = REPROFLAKE_DIR / "cohorts"
DATA_DIR = REPROFLAKE_DIR / "data"
SUMMARY_FILE = REPROFLAKE_DIR / "Complete_Containers_Summary.csv"

ALL_TYPES = ["id", "od", "nio", "td"]
TERMINAL = {"PASSED", "FAILED"}
SENTINEL = ".run_complete"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def load_test_config() -> dict[str, str]:
    """result_container -> test_type."""
    if not CSV_FILE.is_file():
        sys.exit(f"ERROR: {CSV_FILE} not found")
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        return {r["result_container"].strip(): r["test_type"].strip().lower()
                for r in csv.DictReader(f)
                if r.get("result_container", "").strip()}


def read_container_column(path: Path) -> list[str]:
    """Read a single-column container CSV, preserving order and dropping dupes."""
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    # Accept either `container` or test_config's `result_container` as the key.
    key = "container" if "container" in rows[0] else "result_container"
    if key not in rows[0]:
        sys.exit(f"ERROR: {path} has no 'container' column "
                 f"(found: {', '.join(rows[0].keys())})")
    return list(dict.fromkeys(
        r[key].strip() for r in rows if r.get(key, "").strip()))


def load_cohort(test_type: str, cfg: dict[str, str], seed: bool) -> list[str]:
    path = COHORT_DIR / f"{test_type}.csv"
    if not path.is_file():
        if not seed:
            sys.exit(
                f"ERROR: cohort file not found: {path}\n"
                f"       Create it (one container per line under a 'container'\n"
                f"       header), or pass --seed-cohort to populate it with all\n"
                f"       {test_type} containers in test_config.csv.")
        cohort = [c for c, t in cfg.items() if t == test_type]
        COHORT_DIR.mkdir(parents=True, exist_ok=True)
        write_container_csv(path, cohort)
        print(f"[seed] wrote {path.relative_to(REPROFLAKE_DIR)} "
              f"with {len(cohort)} {test_type} containers from test_config.csv")
        return cohort
    return read_container_column(path)


def load_manual_done(test_type: str) -> set[str]:
    """Containers recorded by hand as already run (ledger-independent)."""
    path = COHORT_DIR / f"{test_type}.done.csv"
    return set(read_container_column(path)) if path.is_file() else set()


# ---------------------------------------------------------------------------
# Completion evidence
# ---------------------------------------------------------------------------

def completed_from_summary(require_terminal: bool) -> dict[str, set[str]]:
    """container -> set of run labels recorded in the shared ledger."""
    done: dict[str, set[str]] = {}
    if not SUMMARY_FILE.is_file() or SUMMARY_FILE.stat().st_size == 0:
        return done
    with open(SUMMARY_FILE, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            container = (r.get("container") or "").strip()
            if not container:
                continue
            # Header drifted over time: `verdict` was renamed `final verdict`.
            verdict = (r.get("final verdict") or r.get("verdict") or "").strip()
            if require_terminal and verdict not in TERMINAL:
                continue
            run = (r.get("run") or "").strip()
            # Count distinct run labels, so a duplicated append can't inflate.
            done.setdefault(container, set()).add(run or f"_row{len(done)}")
    return done


def completed_from_data(require_terminal: bool) -> dict[str, set[str]]:
    """container -> set of run_NN dirs on disk carrying a completion sentinel."""
    done: dict[str, set[str]] = {}
    if not DATA_DIR.is_dir():
        return done
    for container_dir in DATA_DIR.iterdir():
        if not container_dir.is_dir():
            continue
        for run_dir in container_dir.iterdir():
            if not run_dir.is_dir() or not re.fullmatch(r"run_\d+", run_dir.name):
                continue
            if not (run_dir / SENTINEL).is_file():
                continue
            if require_terminal and read_verdict(run_dir) not in TERMINAL:
                continue
            done.setdefault(container_dir.name, set()).add(run_dir.name)
    return done


def read_verdict(run_dir: Path) -> str:
    """Mirror parse_run(): prefer run_verdict.txt, fall back to the binary file."""
    steps = run_dir / "claude_outputs"
    for name in ("run_verdict.txt", "verify_after_fix.verdict"):
        path = steps / name
        if path.is_file():
            v = path.read_text(encoding="utf-8", errors="replace").strip()
            if v in ("PASSED", "FAILED", "INCOMPLETE"):
                return v
    return "INCOMPLETE"


def merge(*sources: dict[str, set[str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for src in sources:
        for container, runs in src.items():
            out.setdefault(container, set()).update(runs)
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_container_csv(path: Path, containers: list[str]) -> None:
    # lineterminator="\n": csv defaults to RFC-4180 CRLF, which leaves a
    # trailing \r on every name when these files are consumed from the shell
    # (`while read c` → container name ends in \r → lookup fails).
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["container"])
        for c in containers:
            w.writerow([c])


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Regenerate to_be_run_<type>.csv from cohorts minus completed runs.")
    ap.add_argument("--types", default=",".join(ALL_TYPES),
                    help=f"comma-separated types (default: {','.join(ALL_TYPES)})")
    ap.add_argument("--min-runs", type=int, default=1,
                    help="runs required before a container counts as done (default 1)")
    ap.add_argument("--require-terminal", action="store_true",
                    help="only count runs whose verdict is PASSED or FAILED; "
                         "INCOMPLETE runs leave the container pending")
    ap.add_argument("--source", choices=["summary", "data", "both"], default="both",
                    help="where completion evidence comes from (default: both)")
    ap.add_argument("--out-dir", type=Path, default=REPROFLAKE_DIR,
                    help="directory for to_be_run_<type>.csv (default: AF_Claude_Agent/)")
    ap.add_argument("--seed-cohort", action="store_true",
                    help="if a cohort file is missing, create it from every "
                         "container of that type in test_config.csv")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only; write nothing")
    args = ap.parse_args()

    types = [t.strip().lower() for t in args.types.split(",") if t.strip()]
    unknown = [t for t in types if t not in ALL_TYPES]
    if unknown:
        sys.exit(f"ERROR: unknown type(s): {', '.join(unknown)} "
                 f"(supported: {', '.join(ALL_TYPES)})")
    if args.min_runs < 1:
        sys.exit("ERROR: --min-runs must be >= 1")

    cfg = load_test_config()

    sources = []
    if args.source in ("summary", "both"):
        sources.append(completed_from_summary(args.require_terminal))
    if args.source in ("data", "both"):
        sources.append(completed_from_data(args.require_terminal))
    done = merge(*sources)

    if not SUMMARY_FILE.is_file() and args.source in ("summary", "both"):
        print(f"[note] {SUMMARY_FILE.name} does not exist yet — "
              f"no runs recorded in the ledger.")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{'type':<5} {'cohort':>7} {'done':>6} {'pending':>8}   output")
    print("-" * 62)

    total_pending = 0
    for t in types:
        cohort = load_cohort(t, cfg, args.seed_cohort)

        stray = [c for c in cohort if c not in cfg]
        if stray:
            print(f"[warn] {t}: {len(stray)} cohort container(s) not in "
                  f"test_config.csv: {', '.join(stray[:3])}"
                  f"{' ...' if len(stray) > 3 else ''}")
        mistyped = [c for c in cohort if c in cfg and cfg[c] != t]
        if mistyped:
            print(f"[warn] {t}: {len(mistyped)} cohort container(s) have a "
                  f"different test_type: {', '.join(mistyped[:3])}"
                  f"{' ...' if len(mistyped) > 3 else ''}")

        manual = load_manual_done(t)
        outside = manual - set(cohort)
        if outside:
            print(f"[warn] {t}: {len(outside)} entry(ies) in {t}.done.csv are "
                  f"not in the cohort: {', '.join(sorted(outside)[:3])}"
                  f"{' ...' if len(outside) > 3 else ''}")

        pending = [c for c in cohort
                   if c not in manual and len(done.get(c, ())) < args.min_runs]
        total_pending += len(pending)

        out = args.out_dir / f"to_be_run_{t}.csv"
        if not args.dry_run:
            write_container_csv(out, pending)
        label = out.name + ("  (dry-run, not written)" if args.dry_run else "")
        print(f"{t:<5} {len(cohort):>7} {len(cohort) - len(pending):>6} "
              f"{len(pending):>8}   {label}")

    print("-" * 62)
    print(f"{'':<5} {'':>7} {'':>6} {total_pending:>8}   total pending")


if __name__ == "__main__":
    main()
