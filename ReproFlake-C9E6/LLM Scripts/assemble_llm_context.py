#!/usr/bin/env python3
"""
assemble_llm_context.py

Assembles a single structured context file for LLM-based flaky test patch
generation. Combines RV trace analysis, test source code, failure output,
and production code into one file optimized for API consumption.

Usage:
    python assemble_llm_context.py <result_container>

Output:
    data/<result_container>/llm_context.txt

Design:
    The output is structured for a two-stage LLM prompting strategy:
      Stage 1 (diagnosis): LLM reads trace summary + source → identifies root cause
      Stage 2 (patching):  LLM reads diagnosis + source → generates fix

    The RV trace summary provides the "dynamic runtime evidence" that narrows
    the search space. Without it, the LLM must do pure static analysis (guess).
    With it, the LLM knows:
      - Whether flakiness is confirmed at runtime (not hypothetical)
      - The category of violation (ThreadSafe, Iterator, Collection, etc.)
      - The direction (polluter-caused vs non-deterministic)
      - The magnitude (STRONG/MODERATE/SUBTLE/NO signal)
"""

import csv
import os
import re
import sys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Script lives in ReproFlake-C9E6/LLM Scripts/ ; data and CSV are one level up.
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
CSV_FILE = os.path.join(SCRIPT_DIR, "..", "test_config.csv")


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_csv_row(result_container):
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("result_container", "").strip() == result_container:
                return row
    return None


# ---------------------------------------------------------------------------
# File reading helpers
# ---------------------------------------------------------------------------

def read_file_safe(path, encoding="utf-8"):
    """Read a file, return empty string if missing."""
    if not os.path.isfile(path):
        return ""
    for enc in (encoding, "utf-8-sig", "utf-16-le", "utf-16", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                content = f.read()
            if "\x00" in content and enc == "latin-1":
                continue
            return content
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""


# ---------------------------------------------------------------------------
# Source code extraction
# ---------------------------------------------------------------------------

def fqn_to_path(fqn_with_method):
    """
    Convert 'com.j256.ormlite.logger.LoggerFactoryTest#testSetLogFactory'
    into ('com/j256/ormlite/logger/LoggerFactoryTest.java', 'testSetLogFactory')

    Handles inner classes: if the class part contains '$', strip it.
    """
    if "#" in fqn_with_method:
        class_fqn, method = fqn_with_method.rsplit("#", 1)
    else:
        class_fqn = fqn_with_method
        method = None

    # Strip inner class (e.g., Foo$Bar → Foo)
    if "$" in class_fqn:
        class_fqn = class_fqn[:class_fqn.index("$")]

    rel_path = class_fqn.replace(".", "/") + ".java"
    return rel_path, method


def find_source_file(base_dir, module, rel_path, search_dirs=("src/test/java", "src/main/java")):
    """
    Find a Java source file under the project. Tries:
      1. <base>/Flaky/<module>/<search_dir>/<rel_path>  (multi-module)
      2. <base>/Flaky/<search_dir>/<rel_path>           (single module)
    """
    for search_dir in search_dirs:
        # Multi-module path
        if module and module != ".":
            candidate = os.path.join(base_dir, "Flaky", module, search_dir, rel_path)
            if os.path.isfile(candidate):
                return candidate

        # Single-module path
        candidate = os.path.join(base_dir, "Flaky", search_dir, rel_path)
        if os.path.isfile(candidate):
            return candidate

    return None


def extract_java_method(file_path, method_name, target_line=None):
    """
    Extract a single method from a Java file by matching the method signature
    and tracking brace depth to find the end.

    If target_line is provided (1-indexed), picks the overload whose body
    contains that line. Otherwise picks the first match.

    Returns the method source code as a string, or None.
    """
    if not file_path or not os.path.isfile(file_path):
        return None

    with open(file_path, encoding="utf-8") as f:
        lines = f.readlines()

    # Find ALL method declarations matching method_name
    candidates = []
    for i, line in enumerate(lines):
        if re.search(r'\b' + re.escape(method_name) + r'\s*\(', line):
            # Walk backwards to include annotations
            start = i
            while start > 0 and lines[start - 1].strip().startswith("@"):
                start -= 1
            candidates.append(start)

    if not candidates:
        return None

    # If target_line given and multiple overloads, find the one containing that line
    # target_line is 1-indexed, list is 0-indexed
    method_start = candidates[0]
    if target_line and len(candidates) > 1:
        target_idx = int(target_line) - 1  # convert to 0-indexed
        # Pick the candidate whose start is at or before target_line
        # (the method that contains target_line in its body)
        best = candidates[0]
        for c in candidates:
            if c <= target_idx:
                best = c
            else:
                break
        method_start = best

    # Track brace depth from the opening { to find method end
    brace_depth = 0
    found_open = False
    method_end = None

    for i in range(method_start, len(lines)):
        for ch in lines[i]:
            if ch == '{':
                brace_depth += 1
                found_open = True
            elif ch == '}':
                brace_depth -= 1
                if found_open and brace_depth == 0:
                    method_end = i
                    break
        if method_end is not None:
            break

    if method_end is None:
        # Fallback: return 30 lines from method start
        method_end = min(method_start + 30, len(lines) - 1)

    return "".join(lines[method_start:method_end + 1])


def extract_full_class(file_path, max_lines=200):
    """Read a full class file, capped at max_lines."""
    if not file_path or not os.path.isfile(file_path):
        return None
    with open(file_path, encoding="utf-8") as f:
        lines = f.readlines()
    if len(lines) > max_lines:
        return "".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines truncated)\n"
    return "".join(lines)


# ---------------------------------------------------------------------------
# Failure output extraction
# ---------------------------------------------------------------------------

def extract_failure_from_log(log_path):
    """
    Extract the first test failure block from a Maven/Surefire log.
    Captures: the FAILURE line, the exception, the full stack trace,
    and the error message (which may appear after a blank line).
    """
    content = read_file_safe(log_path)
    if not content:
        return "(no log file found)"

    lines = content.splitlines()

    # Strategy: find the exception line (e.g., "java.lang.AssertionError:")
    # then capture everything until we hit [INFO] or [ERROR] Failures: summary
    exc_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (stripped.startswith("java.lang.") or stripped.startswith("org.")) and (
            "Error" in stripped or "Exception" in stripped
        ) and not stripped.startswith("at "):
            exc_start = i
            break

    if exc_start is None:
        # Fallback: find <<< FAILURE! line
        for i, line in enumerate(lines):
            if "<<< FAILURE!" in line or "<<< ERROR!" in line:
                exc_start = i
                break

    if exc_start is None:
        # Last resort: Tests run: summary
        for line in lines:
            if "Tests run:" in line and "Failures:" in line:
                return line.strip()
        return "(no failure output found in log)"

    # Include 2 context lines before the exception
    start = max(0, exc_start - 2)
    failure_lines = []

    for i in range(start, len(lines)):
        line = lines[i]
        stripped = line.strip()

        # Stop at results summary or next test section
        if stripped.startswith("[INFO] Results:"):
            break
        if stripped.startswith("[ERROR] Failures:"):
            # Include this summary line then stop
            failure_lines.append(line)
            break

        failure_lines.append(line)

        # Safety cap: don't capture more than 40 lines
        if len(failure_lines) > 40:
            break

    if not failure_lines:
        return "(no failure output found in log)"

    return "\n".join(failure_lines).strip()


# ---------------------------------------------------------------------------
# Stack trace → production code extraction
# ---------------------------------------------------------------------------

def extract_production_code_from_stacktrace(failure_text, base_dir, module, project_package):
    """
    Parse the stack trace for project-package classes under src/main/java,
    extract the relevant methods.

    project_package: e.g., "com.j256.ormlite" — derived from victim FQN.
    """
    if not failure_text or not project_package:
        return []

    # Find all "at com.j256.ormlite.something.Class.method(File.java:NNN)" lines
    pattern = re.compile(
        r'at\s+(' + re.escape(project_package) + r'\.[A-Za-z0-9_$.]+)\.(\w+)\((\w+\.java):(\d+)\)'
    )

    seen = set()
    results = []

    for match in pattern.finditer(failure_text):
        class_fqn = match.group(1)
        method_name = match.group(2)
        filename = match.group(3)
        line_no = match.group(4)

        key = f"{class_fqn}#{method_name}"
        if key in seen:
            continue
        seen.add(key)

        # Try to find in src/main/java (production code)
        rel_path, _ = fqn_to_path(f"{class_fqn}#dummy")
        source_file = find_source_file(
            base_dir, module, rel_path,
            search_dirs=("src/main/java",)
        )

        if source_file:
            method_src = extract_java_method(source_file, method_name, target_line=line_no)
            if method_src:
                results.append({
                    "class": class_fqn,
                    "method": method_name,
                    "file": filename,
                    "line": line_no,
                    "source": method_src,
                })

    return results


def derive_project_package(fqn, depth=3):
    """
    Derive the project base package from a FQN.
    'com.j256.ormlite.table.SchemaUtilsTest#testCreateSchemaDao' → 'com.j256.ormlite'
    """
    class_part = fqn.split("#")[0] if "#" in fqn else fqn
    parts = class_part.split(".")
    if len(parts) >= depth:
        return ".".join(parts[:depth])
    return class_part


# ---------------------------------------------------------------------------
# Main assembly
# ---------------------------------------------------------------------------

def assemble_context(result_container):
    base = os.path.join(DATA_DIR, result_container)
    csv_row = load_csv_row(result_container)

    if not csv_row:
        print(f"ERROR: {result_container} not found in test_config.csv", file=sys.stderr)
        sys.exit(1)

    # The CSV has separate 'result_container' and 'zip' columns.
    # Multiple rows can share the same zip (same codebase) but have different
    # result_containers (different polluter/victim pairs). Source code and logs
    # live under the zip directory via Docker bind mount, while analysis files
    # (step_8_C_official.txt, llm_trace_summary.txt) live under result_container.
    zip_name = csv_row.get("zip", "").strip()
    zip_base = os.path.join(DATA_DIR, zip_name) if zip_name and zip_name != result_container else None

    # Determine the source base: prefer result_container, fall back to zip
    if os.path.isdir(os.path.join(base, "Flaky", "src")):
        source_base = base
    elif zip_base and os.path.isdir(os.path.join(zip_base, "Flaky", "src")):
        source_base = zip_base
    else:
        source_base = base  # fallback, may produce "not found" messages

    test_type = csv_row.get("test_type", "unknown").strip().lower()
    polluter_fqn = csv_row.get("polluter/state setter", "").strip()
    victim_fqn = csv_row.get("flaky_test", "").strip()
    module = csv_row.get("module", ".").strip()
    java_ver = csv_row.get("java", "").strip()
    has_polluter = test_type in ("od", "britle") and polluter_fqn != ""

    type_labels = {
        "od": "OD (order-dependent)",
        "id": "ID (implementation-dependent / non-deterministic)",
        "td": "TD (test-dependency)",
        "britle": "BRITTLE (fragile test interaction)",
        "unclassified": "UNCLASSIFIED",
    }

    # --- Section A: Metadata ---
    out = []
    out.append("=" * 60)
    out.append("LLM CONTEXT FOR FLAKY TEST PATCH GENERATION")
    out.append("=" * 60)
    out.append("")
    out.append("=== TEST METADATA ===")
    out.append(f"Test type:      {type_labels.get(test_type, test_type.upper())}")
    if has_polluter:
        out.append(f"Polluter:       {polluter_fqn}")
    out.append(f"Victim:         {victim_fqn}")
    out.append(f"Module:         {module}")
    out.append(f"Java:           {java_ver}")
    out.append("")

    # --- Section B: Source code ---
    # Source code first — LLM needs to read the code before seeing the evidence

    # Polluter source code (OD/brittle only)
    if has_polluter and polluter_fqn:
        rel_path, method_name = fqn_to_path(polluter_fqn)
        source_file = find_source_file(source_base, module, rel_path)

        out.append("=== POLLUTER SOURCE CODE ===")
        if source_file and method_name:
            method_src = extract_java_method(source_file, method_name)
            if method_src:
                out.append(f"File: {os.path.basename(source_file)}")
                out.append(f"Method: {method_name}")
                out.append("")
                out.append(method_src)
            else:
                out.append(f"(Could not extract method {method_name} — full class below)")
                out.append("")
                out.append(extract_full_class(source_file) or "(file not readable)")
        elif source_file:
            out.append(f"File: {os.path.basename(source_file)}")
            out.append("")
            out.append(extract_full_class(source_file) or "(file not readable)")
        else:
            out.append(f"(Source file not found for {polluter_fqn})")
        out.append("")

    # Victim source code
    rel_path, method_name = fqn_to_path(victim_fqn)
    source_file = find_source_file(source_base, module, rel_path)

    out.append("=== VICTIM SOURCE CODE ===")
    if source_file:
        if has_polluter and method_name:
            method_src = extract_java_method(source_file, method_name)
            if method_src:
                out.append(f"File: {os.path.basename(source_file)}")
                out.append(f"Method: {method_name}")
                out.append("")
                out.append(method_src)
            else:
                out.append(f"(Could not extract method {method_name} — full class below)")
                out.append("")
                out.append(extract_full_class(source_file) or "(file not readable)")
        else:
            out.append(f"File: {os.path.basename(source_file)}")
            if method_name:
                out.append(f"Failing method: {method_name}")
            out.append("")
            out.append(extract_full_class(source_file) or "(file not readable)")
    else:
        out.append(f"(Source file not found for {victim_fqn})")
    out.append("")

    # Production code from stack trace
    # (extracted later after failure_text is available, appended below)

    # --- Section C: Evidence (failure + traces, placed near the task for recency) ---

    # Failure output. The orchestrators write the failing-run mvn log to:
    #   traces-flakycc/mvn.log  (TD: FlakyCodeChange variant)
    #   traces-flaky/mvn.log    (OD: Flaky/ source with polluter→victim order)
    # Probe both, then fall back to traces-fixed/mvn.log as a last resort.
    failure_text = "(no log file found)"
    for candidate in ("traces-flakycc", "traces-flaky", "traces-fixed"):
        text = extract_failure_from_log(
            os.path.join(source_base, candidate, "mvn.log")
        )
        if not text.startswith("("):
            failure_text = text
            break
    out.append("=== FAILURE OUTPUT ===")
    out.append("(The actual error when running polluter → victim)" if has_polluter
               else "(The actual error during the failing execution)")
    out.append("")
    out.append(failure_text)
    out.append("")

    # Production code referenced in stack trace
    project_pkg = derive_project_package(victim_fqn)
    prod_code = extract_production_code_from_stacktrace(
        failure_text, source_base, module, project_pkg
    )

    if prod_code:
        out.append("=== PRODUCTION CODE REFERENCED IN STACK TRACE ===")
        out.append("(Methods from the project's main source that appear in the failure)")
        out.append("")
        for entry in prod_code:
            out.append(f"--- {entry['class']}.{entry['method']}() [{entry['file']}:{entry['line']}] ---")
            out.append(entry["source"])
            out.append("")

    # [PRUNED] TEST RESULTS section removed: with the deterministic
    # Fixed-vs-FlakyCodeChange pair, pass/fail counts collapse to 1/1 — the
    # same boolean is implicit in the failure stack trace.

    # RV Trace Analysis — placed last before task so it's freshest in context
    trace_summary = read_file_safe(os.path.join(base, "Steps Output Files", "llm_trace_summary.txt"))
    if trace_summary.strip():
        out.append("=== RV TRACE ANALYSIS ===")
        out.append("(Generated by TraceMOP runtime verification. These traces capture")
        out.append("behavioral differences between the flaky run and clean run at the")
        out.append("JVM level. Spec names identify which API contracts are violated.)")
        out.append("")
        for line in trace_summary.strip().splitlines():
            out.append(line)
        out.append("")
    else:
        out.append("=== RV TRACE ANALYSIS ===")
        out.append("(llm_trace_summary.txt not found — run generate_llm_summary.py first)")
        out.append("")

    # --- Section D: Task instruction for the LLM ---
    out.append("=== TASK ===")

    # Goal framing: test-driven ("make this test pass")
    if has_polluter:
        out.append(f"GOAL: Make the victim test ({victim_fqn.split('#')[-1]}) pass")
        out.append(f"when run immediately after the polluter ({polluter_fqn.split('#')[-1]}).")
    elif test_type == "td":
        out.append(f"GOAL: Make the test ({victim_fqn.split('#')[-1]}) pass deterministically.")
        out.append("The test currently fails on the codebase shown above. Identify the root")
        out.append("cause from the failure stack trace and the RV trace evidence, and produce")
        out.append("the smallest patch that makes the test pass on every execution.")
    else:
        out.append(f"GOAL: Make the test ({victim_fqn.split('#')[-1]}) pass deterministically")
        out.append("on every execution, regardless of timing or platform.")
    out.append("")

    # Fix strategy hints
    if has_polluter:
        out.append("Possible fix categories (pick whichever fits the evidence):")
        out.append("  1. Add cleanup in the polluter (@After/@AfterEach) to restore shared state")
        out.append("  2. Add setup in the victim (@Before/@BeforeEach) to initialize required state")
        out.append("  3. Add a defensive state check in the production code")
    elif test_type == "td":
        out.append("Possible fix categories (pick whichever fits the evidence — do NOT")
        out.append("force a strategy if the evidence does not point at it):")
        out.append("  1. Timing — adjust @Test(timeout = ...) if the test exceeds a too-tight bound,")
        out.append("     or replace short ad-hoc waits with proper synchronization.")
        out.append("  2. Asynchrony — add explicit waits / retries for asynchronous prerequisites")
        out.append("     (e.g., wait for state to converge before asserting on it).")
        out.append("  3. Determinism — replace unstable orderings (e.g., HashMap iteration) or")
        out.append("     non-deterministic API calls with stable / deterministic alternatives.")
        out.append("  4. Implicit shared state — initialize state the test implicitly depends on")
        out.append("     in @Before/@BeforeEach instead of inheriting it from prior runs.")
    else:
        out.append("Possible fix categories (pick whichever fits the evidence):")
        out.append("  1. Replace non-deterministic calls with deterministic alternatives")
        out.append("  2. Add proper synchronization or waiting mechanisms")
        out.append("  3. Mock or control the source of non-determinism")
        out.append("  4. Fix incorrect assumptions about execution order")
    out.append("")

    # Minimal change constraint
    out.append("CONSTRAINTS:")
    out.append("- Make the SMALLEST possible change that fixes the flakiness.")
    out.append("- Do NOT rename variables, methods, or classes.")
    out.append("- Do NOT refactor or restructure unrelated code.")
    out.append("- Do NOT add logging, print statements, or debug output.")
    out.append("- Do NOT change test assertions, expected values, or test logic")
    out.append("  unless the assertion itself is the root cause.")
    out.append("- Do NOT modify method signatures or class hierarchy.")
    out.append("- Preserve the original code style (indentation, naming conventions).")
    out.append("")

    # ---- Two-turn protocol: artifact request first, fix second ----
    # The LLM must first run a mandatory sufficiency checklist. If any checklist
    # item is true, it must request artifacts and wait for TURN 2 before patching.
    out.append("=== TWO-TURN PROTOCOL (read before responding) ===")
    out.append("You will work in up to TWO turns.")
    out.append("")
    out.append("TURN 1 (this message). Before writing any diagnosis or patch, decide")
    out.append("whether the context above is enough to produce a robust, correct,")
    out.append("buildable patch that does not introduce regressions.")
    out.append("")
    out.append("Mandatory artifact-request checklist:")
    out.append("  If ANY item below is true, you MUST request artifacts. Do not emit")
    out.append("  NONE and do not produce OUTPUT 0/A/B in TURN 1.")
    out.append("  [1] The failure stack trace or your suspected root cause touches a")
    out.append("      method, constructor, field initializer, or class body whose source")
    out.append("      is NOT shown in the context above.")
    out.append("  [2] Your draft fix would touch, reset, or depend on a static singleton,")
    out.append("      factory, cache, registry, global logger, system property, or any")
    out.append("      other shared mutable global state.")
    out.append("  [3] Your draft fix would call setX(null), clear(), reset(), restore a")
    out.append("      default value, or use any other 'reset to default' pattern whose")
    out.append("      production-side behavior is not fully shown.")
    out.append("  [4] You would need to write phrases like 'I assume', 'I guess',")
    out.append("      'I don't know', 'I don't have the full file', 'line N is a guess',")
    out.append("      or 'not sure whether this import exists'.")
    out.append("  [5] Your draft fix would add a third-party import or depend on a")
    out.append("      library API whose dependency is not confirmed in the shown context")
    out.append("      (for example ReflectionUtils, Awaitility, Mockito utilities, or")
    out.append("      framework-specific test helpers).")
    out.append("")
    out.append("After applying the checklist, pick ONE path:")
    out.append("")
    out.append("  (a) Checklist passes: NO additional artifacts needed. Begin your")
    out.append("      response with the single line:")
    out.append("        <ARTIFACTS_REQUESTED>NONE - confirmed checklist above passes</ARTIFACTS_REQUESTED>")
    out.append("      Then immediately proceed to OUTPUT 0 / OUTPUT A / OUTPUT B per")
    out.append("      the spec further below.")
    out.append("")
    out.append("  (b) Checklist fails: YOU NEED additional artifacts. Begin your response")
    out.append("      with an <ARTIFACTS_REQUESTED> block listing up to 5 artifacts,")
    out.append("      then STOP — do NOT produce OUTPUT 0/A/B in this turn. We will")
    out.append("      fulfil your request and ask for OUTPUT 0/A/B in TURN 2.")
    out.append("")
    out.append("Format for option (b):")
    out.append("  <ARTIFACTS_REQUESTED>")
    out.append('    <artifact type="<TYPE>" target="<TARGET>" reason="<short reason>"/>')
    out.append("    ... up to 5 ...")
    out.append("  </ARTIFACTS_REQUESTED>")
    out.append("")
    out.append("Closed enum of supported types and target syntaxes:")
    out.append("  IMPORTS_OF      target = relative path to a .java file")
    out.append("                  (e.g. 'src/test/java/com/example/FooTest.java'")
    out.append("                  or 'bookkeeper-server/src/test/java/.../FooTest.java')")
    out.append("                  -> we return the file's package + import block.")
    out.append("  FULL_FILE       target = relative path to a .java file (capped at 800 lines)")
    out.append("                  -> we return the entire file content.")
    out.append("  METHOD          target = '<package.Class>#<methodName>'")
    out.append("                  -> we return the named method's annotations + signature + body")
    out.append("                  (searches src/main/java first, then src/test/java).")
    out.append("  SPEC_DEFINITION target = RV spec name (e.g. 'Map_UnsafeIterator')")
    out.append("                  -> we return the spec's .mop definition (formal rule).")
    out.append("  POM_DEPENDENCY  target = '<groupId>:<artifactId>'")
    out.append("                  -> we return matching <dependency> blocks from any pom.xml")
    out.append("                  in the project (so you can confirm a library is on classpath).")
    out.append("")
    out.append("Guidance for choosing artifacts:")
    out.append("  - If you're uncertain about line numbers, imports, or the precise")
    out.append("    declaration order in a file, ask for IMPORTS_OF or FULL_FILE.")
    out.append("  - If the failing path goes through production code that is NOT in")
    out.append("    the failure stack trace (e.g. logger internals, singleton getInstance,")
    out.append("    null-safety checks), ask for METHOD on those production methods.")
    out.append("    A naïve fix that doesn't see the production-side dereference can")
    out.append("    introduce a regression NPE.")
    out.append("  - If you'd like to suggest using a library API (e.g. ReflectionUtils,")
    out.append("    Awaitility), ask for POM_DEPENDENCY first to confirm it's available.")
    out.append("  - Prefer 1-3 high-leverage artifacts over 5 marginal ones.")
    out.append("  - Write a one-sentence reason for each — it helps you commit and")
    out.append("    helps the human auditor.")
    out.append("")
    out.append("End of TWO-TURN PROTOCOL. Below is the OUTPUT spec used either in")
    out.append("TURN 1 (path (a)) or TURN 2 (after artifacts are provided).")
    out.append("")

    # Three outputs: diagnosis (CoT) + patch (diff) + developer guide (structured).
    # Output B is REQUIRED — both as redundancy against a malformed Output A diff
    # and as a structured form for corpus extraction / human review.
    out.append("Provide THREE outputs. Your response will be parsed by an automated")
    out.append("script — use the exact headers and fencing shown below. Do not")
    out.append("paraphrase, reorder, or omit any of them.")
    out.append("")
    out.append("CRITICAL DISCIPLINE — read carefully:")
    out.append("  - Complete ALL reasoning, exploration, and self-correction inside")
    out.append("    OUTPUT 0. By the time you write OUTPUT A, the patch shown there is")
    out.append("    your FINAL answer.")
    out.append("  - Do NOT write phrases like 'wait, let me redo this', 'actually,",)
    out.append("    let me reconsider', 'on second thought', or any second-attempt")
    out.append("    diff in OUTPUT A or OUTPUT B. If mid-writing you realise the patch")
    out.append("    is wrong, STOP, return to OUTPUT 0 to extend the reasoning, and")
    out.append("    only then start OUTPUT A clean.")
    out.append("  - OUTPUT A must contain EXACTLY ONE ```diff fenced block. Multiple")
    out.append("    diff blocks break the parser — the parser uses the first one.")
    out.append("  - OUTPUT B must contain EXACTLY ONE ### ROOT_CAUSE, ONE")
    out.append("    ### FIX_DESCRIPTION, and ONE ### FIXED_CODE. Each modified file")
    out.append("    appears once; each modified method appears once.")
    out.append("")

    # OUTPUT 0 — Diagnosis (Chain of Thought) + draft + self-verify
    out.append("OUTPUT 0 — DIAGNOSIS:")
    out.append("Reason step-by-step through ALL of the following before writing")
    out.append("any patch. The OUTPUT 0 section is where ALL exploration happens.")
    if has_polluter:
        out.append("  1. What shared state does the polluter modify or corrupt?")
        out.append("  2. What state does the victim assume or expect?")
        out.append("  3. Where exactly is the mismatch (which field, singleton, static, or global)?")
        out.append("  4. Which fix strategy from the list above is the smallest change that breaks the dependency?")
    elif test_type == "td":
        out.append("  1. What does the failure stack trace point at? Which method, which line, which API call?")
        out.append("  2. What do the TOP DISTINCTIVE FLAKY-ONLY trace sequences and TOP FREQUENCY")
        out.append("     DIFFERENCES tell you about *what* the failing run is doing differently?")
        out.append("  3. Is this a timing bound, an asynchrony issue, a non-deterministic ordering,")
        out.append("     an implicit-state assumption, or something else? Justify with evidence above.")
        out.append("  4. Which fix category is the smallest change that addresses the identified cause?")
    else:
        out.append("  1. What is the source of non-determinism (timing, threads, platform, random, etc.)?")
        out.append("  2. Which specific line(s) in the test or production code are affected?")
        out.append("  3. Why does this cause intermittent failure?")
        out.append("  4. Which fix strategy from the list above is the smallest change that eliminates the non-determinism?")
    out.append("  5. DRAFT the patch mentally. For each changed line, write down both the")
    out.append("     ORIGINAL line (to be removed) and the REPLACEMENT line (to be added).")
    out.append("  6. SELF-VERIFY the drafted patch against this checklist. Each item is a")
    out.append("     bug we have seen LLMs make on this prompt. If any item fails, fix the")
    out.append("     draft inside OUTPUT 0 — do NOT 'redo' it inside OUTPUT A.")
    out.append("       (a) Replacing a line requires BOTH a '-' for the original AND a '+'")
    out.append("           for the new version. A '+' without a matching '-' on a CHANGED")
    out.append("           line produces duplicate code (e.g. two @Test annotations stacked,")
    out.append("           which is a Java compile error).")
    out.append("       (b) Each hunk header is @@ -A,B +C,D @@ where")
    out.append("              B = (context lines) + ('-' lines)")
    out.append("              D = (context lines) + ('+' lines)")
    out.append("           Recount carefully. Wrong counts cause patch(1) to fail or fuzzy-match.")
    out.append("       (c) Mentally apply the diff to the original file and read the result:")
    out.append("           is it valid Java? No duplicate annotations, no unmatched braces,")
    out.append("           no orphaned imports, no broken signatures, no half-written stmts.")
    out.append("       (d) The diff contains ONLY the changes implied by your diagnosis —")
    out.append("           no collateral edits, no whitespace-only churn, no comment additions,")
    out.append("           no reformatting of nearby code.")
    out.append("       (e) Paths in the diff are relative to the project root and exist in the")
    out.append("           VICTIM SOURCE / PRODUCTION CODE shown above. No fictitious files.")
    out.append("")
    out.append("This section is for you (the LLM) to think aloud. After OUTPUT 0 ends,")
    out.append("OUTPUT A must be FINAL — no further reasoning, retries, or redos belong")
    out.append("inside OUTPUT A or OUTPUT B.")
    out.append("")

    # OUTPUT A — Patch
    out.append("OUTPUT A — PATCH:")
    out.append("The unified diff that implements the fix you finalised in OUTPUT 0.")
    out.append("Emit EXACTLY ONE ```diff fenced block. No prose before or after the")
    out.append("block, no second attempt.")
    out.append("```diff")
    out.append("<unified diff applicable via `patch -p1`, paths relative to project root>")
    out.append("```")
    out.append("")

    # OUTPUT B — Developer Guide (structured, redundant with OUTPUT A)
    out.append("OUTPUT B — DEVELOPER GUIDE:")
    out.append("Output B is REQUIRED. It serves two purposes: (i) a structured,")
    out.append("redundant representation of the fix that survives if OUTPUT A's diff")
    out.append("is malformed, and (ii) human-readable justification + exemplar code")
    out.append("suitable for corpus extraction.")
    out.append("")
    out.append("### ROOT_CAUSE")
    out.append("<2-4 sentences in plain English: what causes the test to fail. NOT a")
    out.append(" restatement of the diff — name the underlying defect.>")
    out.append("")
    out.append("### FIX_DESCRIPTION")
    out.append("<2-4 sentences: which file(s) you edit, what you add/remove/change, and")
    out.append(" WHY that addresses the root cause. A justification, not a diff replay.>")
    out.append("")
    out.append("### FIXED_CODE")
    out.append("For EACH modified file, emit ONE block in this exact format:")
    out.append("")
    out.append("@@FILE: <path relative to project root, e.g. src/test/java/com/example/FooTest.java>")
    out.append("@@IMPORTS:")
    out.append("<any NEW import statements to add, one per line; omit @@IMPORTS: entirely if none>")
    out.append("@@METHOD: <method name, e.g. testFoo>")
    out.append("```java")
    out.append("<complete fixed method including annotations, signature, full body, closing brace>")
    out.append("```")
    out.append("")
    out.append("Rules for FIXED_CODE:")
    out.append("- Use exactly these markers: '@@FILE: ', '@@IMPORTS:' (on its own line),")
    out.append("  '@@METHOD: ' — same prefixes, same colons, same spacing.")
    out.append("- Repeat @@METHOD + ```java block for each method that changes IN THE SAME FILE.")
    out.append("- Repeat the full @@FILE block for each ADDITIONAL file.")
    out.append("- @@IMPORTS lists ONLY new imports not already present. Omit the marker line")
    out.append("  entirely if no new imports are needed.")
    out.append("- Always include the FULL method body — never use ellipsis or '// ... unchanged'.")
    out.append("")
    out.append("CROSS-CHECK BEFORE FINALISING (mandatory before you stop generating):")
    out.append("Verify all of the following against your own draft:")
    out.append("  [1] Number of methods changed by OUTPUT A's diff equals the number of")
    out.append("      @@METHOD blocks in OUTPUT B's FIXED_CODE.")
    out.append("  [2] For EACH changed method, the result of applying OUTPUT A's diff")
    out.append("      (i.e. take the original method, drop '-' lines, add '+' lines) is")
    out.append("      LINE-FOR-LINE equivalent to the @@METHOD block in OUTPUT B —")
    out.append("      same annotations, same signature, same body, same closing brace.")
    out.append("  [3] Every NEW import line added by OUTPUT A's diff appears under")
    out.append("      @@IMPORTS in OUTPUT B for the same file (and vice versa).")
    out.append("  [4] OUTPUT A contains exactly ONE ```diff block. OUTPUT B contains")
    out.append("      exactly ONE ### ROOT_CAUSE section, ONE ### FIX_DESCRIPTION section,")
    out.append("      and ONE ### FIXED_CODE section.")
    out.append("If any of [1]-[4] disagree, RECONCILE both outputs (regenerate them in")
    out.append("OUTPUT 0's reasoning, then re-emit) before sending. The two outputs MUST")
    out.append("describe the IDENTICAL set of edits.")

    # --- Write output ---
    output_text = "\n".join(out)
    steps_dir = os.path.join(base, "Steps Output Files")
    os.makedirs(steps_dir, exist_ok=True)
    output_file = os.path.join(steps_dir, "llm_context.txt")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(output_text)

    try:
        print(output_text)
    except UnicodeEncodeError:
        print(output_text.encode("ascii", errors="replace").decode("ascii"))
    print(f"\nSaved to: {output_file}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <result_container>")
        sys.exit(1)

    assemble_context(sys.argv[1])
