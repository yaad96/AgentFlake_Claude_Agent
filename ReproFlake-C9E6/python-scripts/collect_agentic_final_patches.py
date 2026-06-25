#!/usr/bin/env python3
"""
Collect the final patch for each completed agentic container run.

Selection rule per container:
  1. Use the latest completed PASSED run that has patch content.
  2. If none passed, use the latest completed run that has patch content.

The output folder contains one subfolder per container, each with:
  - final.patch
  - metadata.json

If a run's Fixed.patch is missing or empty, the script tries to generate a
unified diff between that run's Flaky/ and Fixed/ directories.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import filecmp
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DEFAULT_RUNS_DIR = DEFAULT_DATA_DIR / "AGENTIC_FULL_RUNS"
DEFAULT_OUTPUT_DIR = DEFAULT_DATA_DIR / "AGENTIC_FINAL_PATCHES"
IGNORED_DIRS = {
    ".git",
    ".idea",
    ".nondex",
    ".gradle",
    ".mvn",
    ".settings",
    "target",
    "build",
    "out",
    "logs",
    "node_modules",
    "__pycache__",
}
IGNORED_FILES = {
    ".DS_Store",
}


@dataclass(frozen=True)
class Candidate:
    container: str
    test_type: str
    model: str
    run: int
    verdict: str
    fail_category: str
    run_dir: Path
    patch_path: Path
    patch_source: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect final patch-only fixes from AGENTIC_FULL_RUNS."
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=DEFAULT_RUNS_DIR,
        help=f"Directory containing *_runs folders. Default: {DEFAULT_RUNS_DIR}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Delete the output directory before writing results.",
    )
    return parser.parse_args()


def run_number(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def is_completed(run_dir: Path) -> bool:
    return (run_dir / ".run_complete").exists()


def has_content(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def should_skip_path(path: Path) -> bool:
    parts = set(path.parts)
    return bool(parts & IGNORED_DIRS) or path.name in IGNORED_FILES


def iter_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in IGNORED_DIRS and not name.endswith(".log")
        ]
        current = Path(dirpath)
        for filename in filenames:
            if filename in IGNORED_FILES or filename.endswith((".class", ".jar", ".log")):
                continue
            path = current / filename
            if path.is_file():
                yield path.relative_to(root)


def read_text_lines(path: Path) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError:
        return []


def build_tree_patch(flaky_dir: Path, fixed_dir: Path, output_patch: Path) -> bool:
    if not flaky_dir.is_dir() or not fixed_dir.is_dir():
        return False

    all_paths = sorted(set(iter_files(flaky_dir)) | set(iter_files(fixed_dir)))
    patch_lines: list[str] = []

    for rel_path in all_paths:
        flaky_file = flaky_dir / rel_path
        fixed_file = fixed_dir / rel_path
        if flaky_file.exists() and fixed_file.exists():
            try:
                same = filecmp.cmp(flaky_file, fixed_file, shallow=False)
            except OSError:
                same = False
            if same:
                continue
            from_lines = read_text_lines(flaky_file)
            to_lines = read_text_lines(fixed_file)
            from_name = f"a/{rel_path.as_posix()}"
            to_name = f"b/{rel_path.as_posix()}"
        elif flaky_file.exists():
            if should_skip_path(rel_path):
                continue
            from_lines = read_text_lines(flaky_file)
            to_lines = []
            from_name = f"a/{rel_path.as_posix()}"
            to_name = "/dev/null"
        else:
            from_lines = []
            to_lines = read_text_lines(fixed_file)
            from_name = "/dev/null"
            to_name = f"b/{rel_path.as_posix()}"

        diff_lines = difflib.unified_diff(
            from_lines,
            to_lines,
            fromfile=from_name,
            tofile=to_name,
            lineterm="",
        )
        patch_lines.extend(f"{line}\n" for line in diff_lines)

    if not patch_lines:
        return False

    output_patch.parent.mkdir(parents=True, exist_ok=True)
    output_patch.write_text("".join(patch_lines), errors="replace")
    return True


def extract_llm_response_patch(run_dir: Path, output_patch: Path) -> bool:
    response_path = run_dir / "Steps_Output_Files" / "llm_response.json"
    if not response_path.is_file():
        return False

    try:
        response = json.loads(response_path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return False

    patch_text = ""
    raw_response = response.get("raw_response")
    if isinstance(raw_response, str):
        try:
            raw_data = json.loads(raw_response)
            patch_text = raw_data.get("patch", "")
        except json.JSONDecodeError:
            patch_text = ""

    if not patch_text and isinstance(response.get("response"), dict):
        for value in response["response"].values():
            if isinstance(value, dict) and isinstance(value.get("patch"), str):
                patch_text = value["patch"]
                break

    if not patch_text.strip():
        return False

    output_patch.parent.mkdir(parents=True, exist_ok=True)
    output_patch.write_text(patch_text.rstrip() + "\n", errors="replace")
    return True


def patch_for_run(run_dir: Path, temp_dir: Path) -> tuple[Path | None, str]:
    fixed_patch = run_dir / "Fixed.patch"
    if has_content(fixed_patch):
        return fixed_patch, "Fixed.patch"

    generated_patch = temp_dir / f"{run_dir.parent.parent.name}_{run_dir.parent.name}_{run_dir.name}.patch"
    if build_tree_patch(run_dir / "Flaky", run_dir / "Fixed", generated_patch):
        return generated_patch, "generated_tree_diff"

    submitted_patch = temp_dir / f"{run_dir.parent.parent.name}_{run_dir.parent.name}_{run_dir.name}_llm_response.patch"
    if extract_llm_response_patch(run_dir, submitted_patch):
        return submitted_patch, "llm_response_patch"

    return None, ""


def load_candidates(runs_dir: Path, temp_dir: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    for summary_path in sorted(runs_dir.glob("*_runs/summary.csv")):
        with summary_path.open(newline="", errors="replace") as csv_file:
            for row in csv.DictReader(csv_file):
                model = row.get("model", "")
                run = run_number(row.get("run", ""))
                if not model or run < 0:
                    continue

                verdict = row.get("verdict", "").upper()
                if verdict not in {"PASSED", "FAILED"}:
                    continue

                run_dir = summary_path.parent / model / f"run_{run}"
                if not is_completed(run_dir):
                    continue

                patch_path, patch_source = patch_for_run(run_dir, temp_dir)
                if patch_path is None:
                    continue

                candidates.append(
                    Candidate(
                        container=row.get("container", summary_path.parent.name.removesuffix("_runs")),
                        test_type=row.get("test_type", ""),
                        model=model,
                        run=run,
                        verdict=verdict,
                        fail_category=row.get("fail_category", ""),
                        run_dir=run_dir,
                        patch_path=patch_path,
                        patch_source=patch_source,
                    )
                )
    return candidates


def choose_final_runs(candidates: Iterable[Candidate]) -> dict[str, Candidate]:
    grouped: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.container, []).append(candidate)

    selected: dict[str, Candidate] = {}
    for container, items in grouped.items():
        passed = [item for item in items if item.verdict == "PASSED"]
        pool = passed or items
        selected[container] = max(
            pool,
            key=lambda item: (
                item.run,
                item.patch_path.stat().st_mtime,
                item.model,
            ),
        )
    return selected


def write_outputs(selected: dict[str, Candidate], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = []

    for container, candidate in sorted(selected.items()):
        container_dir = output_dir / container
        container_dir.mkdir(parents=True, exist_ok=True)
        patch_output = container_dir / "final.patch"
        shutil.copyfile(candidate.patch_path, patch_output)

        metadata = {
            "container": candidate.container,
            "test_type": candidate.test_type,
            "model": candidate.model,
            "run": candidate.run,
            "verdict": candidate.verdict,
            "fail_category": candidate.fail_category,
            "source_run_dir": str(candidate.run_dir),
            "source_patch": str(candidate.patch_path),
            "patch_source": candidate.patch_source,
            "generated_from_flaky_fixed_dirs": candidate.patch_source == "generated_tree_diff",
            "output_patch": str(patch_output),
        }
        (container_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        )
        manifest.append(metadata)

    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )


def main() -> int:
    args = parse_args()
    runs_dir = args.runs_dir.resolve()
    output_dir = args.output_dir.resolve()

    if not runs_dir.is_dir():
        raise SystemExit(f"Runs directory not found: {runs_dir}")

    if args.overwrite and output_dir.exists():
        shutil.rmtree(output_dir)

    temp_dir = output_dir / ".generated_patch_cache"
    temp_dir.mkdir(parents=True, exist_ok=True)

    candidates = load_candidates(runs_dir, temp_dir)
    selected = choose_final_runs(candidates)
    write_outputs(selected, output_dir)

    passed = sum(1 for item in selected.values() if item.verdict == "PASSED")
    failed = len(selected) - passed
    print(f"Wrote {len(selected)} container patches to {output_dir}")
    print(f"Selected passed runs: {passed}")
    print(f"Selected failed/latest runs: {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
