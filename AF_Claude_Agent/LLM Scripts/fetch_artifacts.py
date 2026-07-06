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
  FILE_SKELETON     target: <relative path to .java file>
                    returns: structural view of the file — package + imports
                    + class signature(s) + field declarations + method
                    signatures (NO method bodies). Inner classes are shown
                    structurally. Capped at 300 lines.
  METHOD            target: <FQN>#<methodName>
                    returns: a single method body (annotations + signature
                    + body), capped at 100 lines with a truncation marker.
  AROUND            target: <relative path to .java file>#L<line> (or
                    `#L<start>-<end>` for an explicit range)
                    returns: ±100 lines around the named line, with a
                    leading header showing the absolute line range.
  SPEC_DEFINITION   target: <SpecName>
                    returns: contents of Valg/scripts/props/<SpecName>.mop
                    (falls back to props-track/). The RV trace summary
                    references specs by name; this retriever fetches the
                    formal MOP definition so the LLM can interpret what
                    contract was violated.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Reuse the structural-header extractor from the shared assembler module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from assemble_llm_context import extract_class_header  # type: ignore

# ---------------------------------------------------------------------------
# Paths — this file lives in AF_Claude_Agent/LLM Scripts/
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent              # AF_Claude_Agent/
VALG_DIR = REPROFLAKE_DIR.parent                # AgentFlake_Claude_Agent/
PROPS_DIR = VALG_DIR / "scripts" / "props"
PROPS_TRACK_DIR = VALG_DIR / "scripts" / "props-track"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
ARTIFACT_BLOCK_RE = re.compile(
    r"<ARTIFACTS_REQUESTED>(.*?)</ARTIFACTS_REQUESTED>",
    re.DOTALL | re.IGNORECASE,
)

# Canonical schema (Claude follows this reliably):
#   <artifact type="METHOD" target="x.y.Z#m" reason="..."/>
# The trailing slash is optional — gpt-4o sometimes emits <artifact ...>
# (non-self-closing) which is still unambiguous given the closed enum.
ARTIFACT_ITEM_RE = re.compile(
    r'<artifact\s+type="(?P<type>[A-Za-z_]+)"\s+'
    r'target="(?P<target>[^"]+)"'
    r'(?:\s+reason="(?P<reason>[^"]*)")?\s*/?>',
    re.IGNORECASE,
)

# Closed enum of supported artifact types. Kept in sync with the prompt's
# `Closed enum of supported types` section in each per-type assembler.
_ARTIFACT_TYPES = (
    "IMPORTS_OF",
    "FILE_SKELETON",
    "METHOD",
    "AROUND",
    "SPEC_DEFINITION",
)

# Lenient schema #1: type-as-tag-name (observed from gpt-4o):
#   <METHOD target="x.y.Z#m" reason="..."/>
# We accept this even though it's not the canonical form, because dropping
# the request silently because of formatting drift loses the whole turn-2
# round-trip. The prompt still tells the LLM to use the canonical form.
ARTIFACT_ITEM_TYPED_TAG_RE = re.compile(
    r"<(?P<type>" + "|".join(_ARTIFACT_TYPES) + r")\s+"
    r'target="(?P<target>[^"]+)"'
    r'(?:\s+reason="(?P<reason>[^"]*)")?\s*/?>',
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

    Tolerates two artifact-item schemas:
      1. canonical:   <artifact type="METHOD" target="..." reason="..."/>
      2. type-as-tag: <METHOD target="..." reason="..."/>
    Schema 2 is what gpt-4o tends to emit on its own — accepting it
    prevents a silent drop of the entire turn-2 artifact request.
    """
    # Find ALL <ARTIFACTS_REQUESTED> blocks, not just the first. The LLM
    # sometimes declares "NONE" upfront and then mid-OUTPUT-0 realizes it
    # actually needs source — emitting a second block with a real LIST and
    # then stopping (without writing OUTPUT A/B). Using `.search()` would
    # take the first NONE and silently skip turn 2, leaving apply_fix with
    # no usable patch (observed on elasticjob294, May 2026 — the LLM said
    # NONE then asked for GsonFactory.java a few paragraphs later).
    #
    # Policy: if any block contains parseable artifact items, treat the
    # whole response as a LIST request. Only return NONE when every block
    # is NONE-style. Items are deduped across blocks.
    blocks = list(ARTIFACT_BLOCK_RE.finditer(text))
    if not blocks:
        return ("ABSENT", [])

    items = []
    seen = set()  # dedup if both schemas match the same item

    def _add(type_str, target, reason):
        key = (type_str.upper(), target.strip())
        if key in seen:
            return
        seen.add(key)
        items.append({
            "type": type_str.upper(),
            "target": target.strip(),
            "reason": (reason or "").strip(),
        })

    for b in blocks:
        body = b.group(1).strip()
        for m in ARTIFACT_ITEM_RE.finditer(body):
            _add(m.group("type"), m.group("target"), m.group("reason"))
        for m in ARTIFACT_ITEM_TYPED_TAG_RE.finditer(body):
            _add(m.group("type"), m.group("target"), m.group("reason"))

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


def _file_skeleton(source_base: str, target: str, max_lines: int = 300) -> str:
    """Return a structural view of the file (package + imports + class
    signatures + field declarations + method signatures, no method bodies).
    Reuses extract_class_header from the shared assembler module."""
    path = _resolve_in_flaky(source_base, target)
    if not path:
        return f"(file not found: {target})"
    text = extract_class_header(
        str(path),
        include_inner_classes=True,
        max_lines=max_lines,
    )
    if text is None:
        return f"(file not readable: {target})"
    return text


# Parses line targets out of an AROUND target string. Accepted forms:
#   #L42        single line
#   #L20-25     line range
#   #L20-L25    line range (lenient — same as #L20-25, the trailing 'L' is
#               a common typo and we don't want to silently drop the request)
_AROUND_LINE_RE = re.compile(r"#L(?P<a>\d+)(?:-L?(?P<b>\d+))?\s*$")


def _around(source_base: str, target: str, window: int = 100) -> str:
    """Return a window of lines around the target line (`<path>#L<n>`) or an
    explicit line range (`<path>#L<start>-<end>`). The window adds ±`window`
    lines around the target/range. Always emits an absolute-line-number
    header so the LLM knows where the slice came from."""
    m = _AROUND_LINE_RE.search(target)
    if not m:
        return "(AROUND target must be '<relative-path>#L<line>' or '#L<start>-<end>')"
    path_part = target[: m.start()]
    a = int(m.group("a"))
    b = int(m.group("b")) if m.group("b") else a
    if b < a:
        a, b = b, a

    path = _resolve_in_flaky(source_base, path_part)
    if not path:
        return f"(file not found: {path_part})"
    with path.open(encoding="utf-8") as f:
        lines = f.readlines()

    n = len(lines)
    start = max(1, a - window)         # 1-indexed inclusive
    end = min(n, b + window)           # 1-indexed inclusive
    selected = lines[start - 1 : end]  # slice is 0-indexed half-open

    # Header uses '//' so the result doesn't start with '(' — the dispatch
    # treats '(' as the failure-marker convention.
    target_str = f"L{a}" + (f"-L{b}" if b != a else "")
    header = (
        f"// AROUND: lines {start}-{end} of {path_part} "
        f"(target: {target_str})\n"
    )
    return header + "".join(selected)


def _extract_method(file_path: Path, method_name: str, max_lines: int = 100) -> str:
    """Find a method by name and return its source (annotations + signature
    + body up through the matching closing brace). Truncates at max_lines
    with a marker so very long methods don't blow the artifact budget."""
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

    method_lines = lines[method_start : end + 1]
    if len(method_lines) > max_lines:
        omitted = len(method_lines) - max_lines
        truncated = method_lines[:max_lines]
        truncated.append(
            f"// ... {omitted} more lines truncated; "
            f"use AROUND with #L<line> to see specific lines ...\n"
        )
        return "".join(truncated)
    return "".join(method_lines)


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


# ---------------------------------------------------------------------------
# Dispatch + format
# ---------------------------------------------------------------------------
RETRIEVERS = {
    "IMPORTS_OF": _imports_of,
    "FILE_SKELETON": _file_skeleton,
    "METHOD": _method,
    "AROUND": _around,
    "SPEC_DEFINITION": _spec_definition,
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
    """Format the fetched artifacts as the Turn 2 user message body.

    Critical: this is the FINAL turn. Earlier versions of this prompt did
    not say so explicitly, and observed LLM behaviour on hard cases was to
    emit another <ARTIFACTS_REQUESTED> block on turn 2 instead of committing
    to a fix. The pipeline only handles 2 turns; a third request is silently
    captured by the response parser as 'the patch', which then fails to
    apply because it's prose, not a diff. The framing below makes the
    no-third-turn rule explicit and tells the LLM what to do when it would
    otherwise punt to a third turn (state an ASSUMPTION and commit anyway).
    """
    out = ["You requested the following artifacts. Their content is below.\n"]
    for i, r in enumerate(results, 1):
        out.append(f"=== ARTIFACT {i}: {r['type']} {r['target']} ===")
        if r["reason"]:
            out.append(f"(your stated reason: {r['reason']})")
        out.append("")
        out.append(r["content"].rstrip("\n"))
        out.append("")

    out.append("=" * 60)
    out.append("FINAL TURN — produce OUTPUT 0 / OUTPUT A / OUTPUT B now")
    out.append("=" * 60)
    out.append("")
    out.append("This is your FINAL response. The pipeline does not support a")
    out.append("third turn. You MUST emit OUTPUT 0, OUTPUT A, and OUTPUT B in")
    out.append("this message.")
    out.append("")
    out.append("DO NOT emit another <ARTIFACTS_REQUESTED> block. If you find")
    out.append("you still want information that isn't in the context:")
    out.append("  - State the specific assumption explicitly in OUTPUT 0,")
    out.append("    prefixed with 'ASSUMPTION:'.")
    out.append("  - Make the smallest fix consistent with that assumption.")
    out.append("  - Repeat the assumption in OUTPUT B's ### ROOT_CAUSE so a")
    out.append("    human reviewer can spot it.")
    out.append("A clearly-labelled best-effort fix is far more useful than")
    out.append("another artifact request — the latter will be silently dropped")
    out.append("and your output will be unparseable.")
    out.append("")
    out.append("Output spec recap (full rules in the original prompt):")
    out.append("  - OUTPUT 0 — DIAGNOSIS: free-form chain-of-thought.")
    out.append("  - OUTPUT A — PATCH: EXACTLY ONE ```diff fenced block.")
    out.append("  - OUTPUT B — DEVELOPER GUIDE: EXACTLY ONE each of")
    out.append("    ### ROOT_CAUSE, ### FIX_DESCRIPTION, ### FIXED_CODE.")
    out.append("  - Cross-check items [1]-[6] still apply: OUTPUT A and OUTPUT B")
    out.append("    must describe the IDENTICAL set of edits; every @@METHOD has")
    out.append("    @@OPERATION and (for 'insert_method') @@ANCHOR.")
    out.append("")
    out.append("If your fix touches a file whose full body you have NOT seen")
    out.append("(only its FILE_SKELETON / METHOD / AROUND view), prefer the")
    out.append("structured @@METHOD/@@OPERATION/@@ANCHOR form in OUTPUT B over")
    out.append("relying on exact line numbers in OUTPUT A — the structured form")
    out.append("is robust to line-number guesses; unified diffs are not.")
    return "\n".join(out)
