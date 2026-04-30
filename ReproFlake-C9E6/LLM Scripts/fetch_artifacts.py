#!/usr/bin/env python3
"""
fetch_artifacts.py

Closed-enum artifact retrievers for the multi-turn LLM protocol.

In Turn 1, the LLM responds with an <ARTIFACTS_REQUESTED> block listing up
to 5 artifacts it needs to produce a robust fix. We parse that block, fetch
each artifact from the project's source tree (or the spec library), and
format the results as a single Turn 2 user message.

Supported artifact types (extensible):
  IMPORTS_OF        target: <relative path to .java file>
                    returns: package + import lines from the file's header
  FULL_FILE         target: <relative path to .java file>
                    returns: entire file content (capped at 800 lines)
  METHOD            target: <FQN>#<methodName>
                    returns: a single method body extracted from the file
                    that defines the FQN class
  SPEC_DEFINITION   target: <SpecName>
                    returns: contents of Valg/scripts/props/<SpecName>.mop
                    (falls back to props-track/)
  POM_DEPENDENCY    target: <groupId>:<artifactId>
                    returns: matching <dependency> blocks from any pom.xml
                    in the project tree, with their relative pom path
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths — this file lives in ReproFlake-C9E6/LLM Scripts/
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent              # ReproFlake-C9E6/
VALG_DIR = REPROFLAKE_DIR.parent                # Valg/Valg/
PROPS_DIR = VALG_DIR / "scripts" / "props"
PROPS_TRACK_DIR = VALG_DIR / "scripts" / "props-track"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
ARTIFACT_BLOCK_RE = re.compile(
    r"<ARTIFACTS_REQUESTED>(.*?)</ARTIFACTS_REQUESTED>",
    re.DOTALL | re.IGNORECASE,
)
ARTIFACT_ITEM_RE = re.compile(
    r'<artifact\s+type="(?P<type>[A-Za-z_]+)"\s+'
    r'target="(?P<target>[^"]+)"'
    r'(?:\s+reason="(?P<reason>[^"]*)")?\s*/>',
    re.IGNORECASE,
)


def parse_artifact_block(text: str):
    """
    Parse an <ARTIFACTS_REQUESTED> block out of raw LLM text.

    Returns one of:
        ("NONE",  [])           — explicit NONE; LLM is answering directly
                                (may include checklist confirmation text)
        ("LIST",  [items, ...]) — list of {type, target, reason}
        ("ABSENT", [])          — no block at all (LLM ignored the protocol)
    """
    block_match = ARTIFACT_BLOCK_RE.search(text)
    if not block_match:
        return ("ABSENT", [])

    body = block_match.group(1).strip()
    if body.upper() == "NONE" or body.upper().startswith("NONE -"):
        return ("NONE", [])

    items = []
    for m in ARTIFACT_ITEM_RE.finditer(body):
        items.append({
            "type": m.group("type").upper(),
            "target": m.group("target").strip(),
            "reason": (m.group("reason") or "").strip(),
        })
    if items:
        return ("LIST", items)
    return ("NONE", [])


# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------
def _flaky_root(source_base: str) -> Path:
    return Path(source_base) / "Flaky"


def _resolve_in_flaky(source_base: str, rel_path: str) -> Path | None:
    """Resolve a path target against <source_base>/Flaky/.
    Strips a leading 'a/' or 'b/' if the LLM mimicked unified-diff path syntax.
    """
    rel_path = rel_path.lstrip("/")
    for prefix in ("a/", "b/"):
        if rel_path.startswith(prefix):
            rel_path = rel_path[len(prefix):]
            break
    candidate = _flaky_root(source_base) / rel_path
    return candidate if candidate.is_file() else None


def _fqn_to_relpaths(fqn_with_method: str):
    """com.j256.ormlite.logger.BaseLogger#logIfEnabled
       -> (['src/main/java/com/j256/ormlite/logger/BaseLogger.java',
            'src/test/java/com/j256/ormlite/logger/BaseLogger.java'],
           'logIfEnabled')
    """
    if "#" in fqn_with_method:
        class_fqn, method = fqn_with_method.rsplit("#", 1)
    else:
        class_fqn, method = fqn_with_method, None
    if "$" in class_fqn:
        class_fqn = class_fqn[: class_fqn.index("$")]
    rel = class_fqn.replace(".", "/") + ".java"
    return [f"src/main/java/{rel}", f"src/test/java/{rel}"], method


def _find_class_file(source_base: str, fqn_with_method: str):
    """Walk <Flaky>/ for a .java file matching the FQN. Returns (path, method)
    or (None, method)."""
    candidates, method = _fqn_to_relpaths(fqn_with_method)
    flaky = _flaky_root(source_base)
    if not flaky.is_dir():
        return None, method

    # Single-module case
    for rel in candidates:
        p = flaky / rel
        if p.is_file():
            return p, method

    # Multi-module case: search every immediate subdir
    for sub in flaky.iterdir():
        if not sub.is_dir():
            continue
        for rel in candidates:
            p = sub / rel
            if p.is_file():
                return p, method
        # Two levels deep (e.g. compat-deps/foo)
        for sub2 in sub.iterdir():
            if not sub2.is_dir():
                continue
            for rel in candidates:
                p = sub2 / rel
                if p.is_file():
                    return p, method
    return None, method


# ---------------------------------------------------------------------------
# Retrievers
# ---------------------------------------------------------------------------
def _imports_of(source_base: str, target: str) -> str:
    path = _resolve_in_flaky(source_base, target)
    if not path:
        return f"(file not found: {target})"
    out_lines: list[str] = []
    with path.open(encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if (
                stripped.startswith("package ")
                or stripped.startswith("import ")
                or stripped == ""
                or stripped.startswith("//")
                or stripped.startswith("/*")
                or stripped.startswith("*")
            ):
                out_lines.append(raw.rstrip("\n"))
            else:
                # First real declaration — stop.
                break
    return "\n".join(out_lines).strip() or "(empty header)"


def _full_file(source_base: str, target: str, max_lines: int = 800) -> str:
    path = _resolve_in_flaky(source_base, target)
    if not path:
        return f"(file not found: {target})"
    with path.open(encoding="utf-8") as f:
        lines = f.readlines()
    if len(lines) > max_lines:
        return (
            "".join(lines[:max_lines])
            + f"\n... ({len(lines) - max_lines} more lines truncated)\n"
        )
    return "".join(lines)


def _extract_method(file_path: Path, method_name: str) -> str:
    """Find a method by name and return its source (annotations + signature
    + body up through the matching closing brace)."""
    with file_path.open(encoding="utf-8") as f:
        lines = f.readlines()

    candidates: list[int] = []
    pat = re.compile(r"\b" + re.escape(method_name) + r"\s*\(")
    for i, line in enumerate(lines):
        if pat.search(line):
            start = i
            while start > 0 and lines[start - 1].strip().startswith("@"):
                start -= 1
            candidates.append(start)

    if not candidates:
        return f"(method {method_name} not found in {file_path.name})"

    method_start = candidates[0]

    # Brace-depth track from the first '{' onward.
    depth = 0
    seen_open = False
    end = None
    for i in range(method_start, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                seen_open = True
            elif ch == "}":
                depth -= 1
                if seen_open and depth == 0:
                    end = i
                    break
        if end is not None:
            break

    if end is None:
        end = min(method_start + 60, len(lines) - 1)
    return "".join(lines[method_start : end + 1])


def _method(source_base: str, target: str) -> str:
    if "#" not in target:
        return "(METHOD target must be 'package.Class#methodName')"
    file_path, method = _find_class_file(source_base, target)
    if not file_path:
        return f"(class file not found for {target})"
    if not method:
        return "(METHOD target missing #methodName)"
    return _extract_method(file_path, method)


def _spec_definition(source_base: str, target: str) -> str:
    name = target.strip()
    if not name.endswith(".mop"):
        name_mop = f"{name}.mop"
    else:
        name_mop = name
    for base in (PROPS_DIR, PROPS_TRACK_DIR):
        path = base / name_mop
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return f"(spec not found in props/ or props-track/: {target})"


def _pom_dependency(source_base: str, target: str) -> str:
    if ":" not in target:
        return "(POM_DEPENDENCY target must be 'groupId:artifactId')"
    group_id, artifact_id = target.split(":", 1)
    group_id, artifact_id = group_id.strip(), artifact_id.strip()
    flaky = _flaky_root(source_base)
    found: list[str] = []

    dep_re = re.compile(
        r"<dependency>(?:(?!</dependency>).)*?<groupId>\s*"
        + re.escape(group_id)
        + r"\s*</groupId>(?:(?!</dependency>).)*?<artifactId>\s*"
        + re.escape(artifact_id)
        + r"\s*</artifactId>(?:(?!</dependency>).)*?</dependency>",
        re.DOTALL,
    )

    for root, _, files in os.walk(flaky):
        if "pom.xml" not in files:
            continue
        pom_path = Path(root) / "pom.xml"
        try:
            content = pom_path.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in dep_re.finditer(content):
            rel = pom_path.relative_to(flaky)
            found.append(f"# from {rel}\n{m.group(0)}")

    if not found:
        return f"(no <dependency> matching {target} found in any pom.xml)"
    return "\n\n".join(found)


# ---------------------------------------------------------------------------
# Dispatch + format
# ---------------------------------------------------------------------------
RETRIEVERS = {
    "IMPORTS_OF": _imports_of,
    "FULL_FILE": _full_file,
    "METHOD": _method,
    "SPEC_DEFINITION": _spec_definition,
    "POM_DEPENDENCY": _pom_dependency,
}

MAX_REQUESTS_PER_TURN = 5


def fetch_artifacts(requests, source_base: str):
    """
    For each request, dispatch to the appropriate retriever.
    Caps at MAX_REQUESTS_PER_TURN; subsequent requests are dropped with a note.
    Returns a list of result dicts:
        {type, target, reason, content, satisfied, size_chars}
    A result is 'satisfied' iff the content does not start with '('.
    """
    results = []
    for i, req in enumerate(requests):
        if i >= MAX_REQUESTS_PER_TURN:
            results.append(
                {
                    "type": req.get("type", "?"),
                    "target": req.get("target", "?"),
                    "reason": req.get("reason", ""),
                    "content": f"(skipped — exceeded MAX_REQUESTS_PER_TURN={MAX_REQUESTS_PER_TURN})",
                    "satisfied": False,
                    "size_chars": 0,
                }
            )
            continue

        retriever = RETRIEVERS.get(req["type"])
        if retriever is None:
            content = (
                f"(unsupported artifact type: {req['type']}; "
                f"supported: {', '.join(sorted(RETRIEVERS.keys()))})"
            )
        else:
            try:
                content = retriever(source_base, req["target"])
            except Exception as e:
                content = f"(retriever raised {type(e).__name__}: {e})"

        results.append(
            {
                "type": req["type"],
                "target": req["target"],
                "reason": req.get("reason", ""),
                "content": content,
                "satisfied": not content.startswith("("),
                "size_chars": len(content),
            }
        )
    return results


def format_artifacts_block(results) -> str:
    """Format the fetched artifacts as the Turn 2 user message body."""
    out = ["You requested the following artifacts. Their content is below.\n"]
    for i, r in enumerate(results, 1):
        out.append(f"=== ARTIFACT {i}: {r['type']} {r['target']} ===")
        if r["reason"]:
            out.append(f"(your stated reason: {r['reason']})")
        out.append("")
        out.append(r["content"].rstrip("\n"))
        out.append("")
    out.append(
        "Now produce the final fix per the OUTPUT 0 / OUTPUT A / OUTPUT B"
    )
    out.append(
        "spec from the original prompt (CRITICAL DISCIPLINE, OUTPUT 0,"
    )
    out.append(
        "OUTPUT A, OUTPUT B, CROSS-CHECK rules all still apply)."
    )
    out.append("")
    out.append(
        "Reminder: emit EXACTLY ONE ```diff block in OUTPUT A; emit"
    )
    out.append(
        "EXACTLY ONE ### ROOT_CAUSE / ### FIX_DESCRIPTION / ### FIXED_CODE"
    )
    out.append(
        "section in OUTPUT B; cross-check that A and B describe the IDENTICAL"
    )
    out.append("set of edits before sending.")
    return "\n".join(out)
