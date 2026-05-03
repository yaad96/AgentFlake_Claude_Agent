#!/usr/bin/env python3
"""
assemble_llm_context_td.py

TD (Test-Dependency) variant of the LLM context assembler. Produces a
structured context file for tests whose flakiness is rooted in the test's
own assumptions about timing, asynchrony, ordering, or implicit shared
state — i.e. there is NO separate polluter test.

Differences from the other type-specific assemblers:
  - No POLLUTER section. TD has no polluter; the test fails on its own
    against a known-flaky codebase commit.
  - VICTIM SOURCE CODE shows the FULL CLASS (no polluter to focus the
    extraction onto a specific method).
  - FAILURE OUTPUT comes from traces-flakycc/mvn.log first (the TD repro
    against the FlakyCodeChange snapshot), then traces-flaky/, then
    traces-fixed/ as a last resort.
  - TASK section is TD-framed: timing / asynchrony / determinism / implicit
    shared-state initialization.
  - The TWO-TURN PROTOCOL + OUTPUT spec + cross-check items are duplicated
    inline so this file is self-contained, matching the per-type style of
    the other assemblers.

Usage:
    python assemble_llm_context_td.py <result_container>

Output:
    data/<result_container>/Steps Output Files/llm_context.txt
"""

import os
import sys

# Reuse data-extraction helpers from the shared module. We only import pure
# functions; the shared module is no longer a runnable assembler. The shared
# module lives one directory up (in `LLM Scripts/`); this file lives in
# `LLM Scripts/rv/` (or its `no_rv/` sibling), so we add the parent dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from assemble_llm_context import (  # type: ignore
    DATA_DIR,
    load_csv_row,
    read_file_safe,
    fqn_to_path,
    find_source_file,
    extract_java_method,
    extract_class_header,
    extract_failure_from_log,
    extract_production_code_from_stacktrace,
    derive_project_package,
)


def _method_fallback_marker(reason):
    """Standard marker emitted when method extraction fails or no method
    name is available. Points the LLM at TEST CLASS HEADER + TURN 2 METHOD."""
    return (
        f"({reason}. See TEST CLASS HEADER above for the file's structure;\n"
        f"request METHOD via TURN 2 with a specific method name if needed.)"
    )


# ---------------------------------------------------------------------------
# Protocol blocks (duplicated from the other per-type assemblers — see module
# docstring for rationale).
# ---------------------------------------------------------------------------

def _two_turn_protocol_lines():
    """TWO-TURN PROTOCOL + artifact-request checklist + closed-enum schema.
    Same shape as the OD/ID/NIO assemblers; checklist items are tuned for
    TD (timing bounds, async pre-conditions, implicit-state assumptions)."""
    out = []
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
    out.append("Format for option (b) — use this EXACT XML schema:")
    out.append("  <ARTIFACTS_REQUESTED>")
    out.append('    <artifact type="<TYPE>" target="<TARGET>" reason="<short reason>"/>')
    out.append("    ... up to 5 ...")
    out.append("  </ARTIFACTS_REQUESTED>")
    out.append("")
    out.append("STRICT SCHEMA RULES (the response is parsed by a regex):")
    out.append('  - The element tag MUST be the literal word `artifact` (lowercase).')
    out.append('  - The type goes in the `type=` attribute, NOT as the tag name.')
    out.append("  - Correct example:")
    out.append('      <artifact type="METHOD" target="com.foo.Bar#baz" reason="..."/>')
    out.append("  - Common drift mistakes (the parser is tolerant but please do not make these):")
    out.append('      <METHOD target="com.foo.Bar#baz" reason="..."/>          (type-as-tag-name)')
    out.append('      <Artifact Type="METHOD" Target="..." />                  (capitalized attrs)')
    out.append("")
    out.append("Closed enum of supported types and target syntaxes:")
    out.append("  IMPORTS_OF      target = relative path to a .java file")
    out.append("                  -> we return the file's package + import block.")
    out.append("  FILE_SKELETON   target = relative path to a .java file")
    out.append("                  -> we return a STRUCTURAL view: package + imports +")
    out.append("                  class signature(s) + field declarations + method signatures")
    out.append("                  (NO method bodies). Inner classes shown structurally too.")
    out.append("                  Capped at 300 lines. Use this to navigate large files; then")
    out.append("                  follow up with METHOD or AROUND for specific bodies/lines.")
    out.append("  METHOD          target = '<package.Class>#<methodName>'")
    out.append("                  -> we return the named method's annotations + signature + body,")
    out.append("                  capped at 100 lines.")
    out.append("  AROUND          target = '<relative-path>#L<line>' or '#L<start>-<end>'")
    out.append("                  -> we return ±100 lines around the named line(s) with an")
    out.append("                  absolute-line-number header. Use this for stack-trace line")
    out.append("                  numbers, especially when the line is inside a lambda, async")
    out.append("                  callback, or other scope that lacks a clean method name.")
    out.append("  SPEC_DEFINITION target = RV spec name (e.g. 'Map_UnsafeIterator')")
    out.append("                  -> we return the spec's .mop definition (formal rule). Use")
    out.append("                  this when the RV TRACE ANALYSIS names a spec whose contract")
    out.append("                  you need to interpret formally.")
    out.append("")
    out.append("Guidance for choosing artifacts:")
    out.append("  - Prefer METHOD for surgical access. Use FILE_SKELETON to discover what")
    out.append("    methods/fields exist in a class. Use AROUND when the failure line is")
    out.append("    inside a lambda, async callback, or stream pipeline where there's no")
    out.append("    clean method name to ask for.")
    out.append("  - If the failing path goes through production code that is NOT in")
    out.append("    the failure stack trace (e.g. logger internals, async waits,")
    out.append("    null-safety checks), ask for METHOD on those production methods.")
    out.append("  - If the RV trace names a spec you don't recognize, ask SPEC_DEFINITION")
    out.append("    so you can interpret the violated contract formally.")
    out.append("  - Prefer 1-3 high-leverage artifacts over 5 marginal ones.")
    out.append("  - Write a one-sentence reason for each — it helps you commit and")
    out.append("    helps the human auditor.")
    out.append("")
    out.append("End of TWO-TURN PROTOCOL. Below is the OUTPUT spec used either in")
    out.append("TURN 1 (path (a)) or TURN 2 (after artifacts are provided).")
    out.append("")
    return out


def _three_outputs_spec_lines():
    """OUTPUT 0 (diagnosis) + OUTPUT A (unified diff) + OUTPUT B (developer
    guide with @@OPERATION/@@ANCHOR schema). Same parser-facing format as
    the other per-type assemblers so apply_fix.py and call_llm.py work
    without TD-specific changes."""
    out = []
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

    out.append("OUTPUT 0 — DIAGNOSIS:")
    out.append("Reason step-by-step through ALL of the following before writing")
    out.append("any patch. The OUTPUT 0 section is where ALL exploration happens.")
    out.append("  1. What does the failure stack trace point at? Which method, which line, which API call?")
    out.append("  2. What do the TOP DISTINCTIVE FLAKY-ONLY trace sequences and TOP FREQUENCY")
    out.append("     DIFFERENCES tell you about *what* the failing run is doing differently?")
    out.append("  3. Is this a timing bound, an asynchrony issue, a non-deterministic ordering,")
    out.append("     an implicit-state assumption, or something else? Justify with evidence above.")
    out.append("  4. Which fix category is the smallest change that addresses the identified cause?")
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

    out.append("OUTPUT A — PATCH:")
    out.append("The unified diff that implements the fix you finalised in OUTPUT 0.")
    out.append("Emit EXACTLY ONE ```diff fenced block. No prose before or after the")
    out.append("block, no second attempt.")
    out.append("")
    out.append("APPLIER NOTE: the diff will be applied with `git apply --recount`,")
    out.append("which RECOMPUTES hunk line counts. This means:")
    out.append("  - You do NOT need to count lines exactly. Off-by-one errors in the")
    out.append("    ',N' fields of '@@ -L,N +L,N @@' will be silently corrected.")
    out.append("  - The L (start line) numbers and the hunk BODY (context/'-'/'+'")
    out.append("    lines) still must be correct: --recount only fixes counts, not")
    out.append("    missing/wrong context.")
    out.append("  - When unsure of the exact start line, prefer the form")
    out.append("    '@@ -L +L @@' (no commas, no counts) — --recount accepts it.")
    out.append("  - DO NOT emit anchorless '@@\\n' headers; --recount cannot find")
    out.append("    the hunk without at least the start line number.")
    out.append("  - Every non-empty hunk-body line MUST start with ' ', '+' or '-'.")
    out.append("    Blank context lines are a single space, never empty.")
    out.append("```diff")
    out.append("<unified diff with absolute paths from project root, headers '@@ -L +L @@'")
    out.append(" or '@@ -L,N +L,N @@', applied via `git apply --recount`>")
    out.append("```")
    out.append("")

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
    out.append("@@OPERATION: replace_method | insert_method")
    out.append("@@ANCHOR: before_method=<name> | after_method=<name> | end_of_class")
    out.append("```java")
    out.append("<complete fixed method including annotations, signature, full body, closing brace>")
    out.append("```")
    out.append("")
    out.append("Rules for FIXED_CODE:")
    out.append("- Use exactly these markers: '@@FILE: ', '@@IMPORTS:' (on its own line),")
    out.append("  '@@METHOD: ', '@@OPERATION: ', '@@ANCHOR: ' — same prefixes, same colons,")
    out.append("  same spacing.")
    out.append("- Repeat @@METHOD + @@OPERATION + (@@ANCHOR if needed) + ```java block for")
    out.append("  each method that changes IN THE SAME FILE.")
    out.append("- Repeat the full @@FILE block for each ADDITIONAL file.")
    out.append("- @@IMPORTS lists ONLY new imports not already present. Omit the marker line")
    out.append("  entirely if no new imports are needed.")
    out.append("- @@OPERATION is REQUIRED on every @@METHOD block:")
    out.append("    * 'replace_method' if a method with this name already exists in the")
    out.append("      original file (the fix rewrites its body or annotations).")
    out.append("    * 'insert_method' if the method is NEW (not present in the original).")
    out.append("- @@ANCHOR is REQUIRED when @@OPERATION is 'insert_method' and FORBIDDEN")
    out.append("  when 'replace_method'. Allowed forms:")
    out.append("    * 'before_method=<name>' — insert immediately before this existing method.")
    out.append("    * 'after_method=<name>' — insert immediately after this existing method.")
    out.append("    * 'end_of_class' — append as the last member of the outer class.")
    out.append("  Prefer 'before_method=' anchored on a related setup method, so the new")
    out.append("  method sits with its logical neighbours.")
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
    out.append("  [5] Every @@METHOD block has an @@OPERATION line. If the named method")
    out.append("      is new (not in the original file), its operation is 'insert_method'")
    out.append("      and it has an @@ANCHOR line; if the named method already exists,")
    out.append("      its operation is 'replace_method' and there is NO @@ANCHOR line.")
    out.append("  [6] @@OPERATION/@@ANCHOR agree with what OUTPUT A's diff actually does:")
    out.append("      a 'replace_method' block corresponds to a hunk that has both '-' and")
    out.append("      '+' lines on the named method; an 'insert_method' block corresponds")
    out.append("      to a hunk that has only '+' lines for the new method, positioned")
    out.append("      consistently with the @@ANCHOR.")
    out.append("If any of [1]-[6] disagree, RECONCILE both outputs (regenerate them in")
    out.append("OUTPUT 0's reasoning, then re-emit) before sending. The two outputs MUST")
    out.append("describe the IDENTICAL set of edits.")
    return out


# ---------------------------------------------------------------------------
# Main assembly
# ---------------------------------------------------------------------------

def assemble_context_td(result_container):
    csv_row = load_csv_row(result_container)
    if not csv_row:
        sys.exit(f"ERROR: '{result_container}' not in CSV")

    test_type = csv_row.get("test_type", "").strip().lower()
    if test_type != "td":
        sys.exit(
            f"ERROR: assemble_llm_context_td.py targets TD only; "
            f"got test_type='{test_type}'. For OD/brittle use assemble_llm_context_od.py, "
            f"for ID use assemble_llm_context_id.py, "
            f"for NIO use assemble_llm_context_nio.py."
        )

    base = os.path.join(DATA_DIR, result_container)

    # Source base: prefer result_container, fall back to zip dir.
    zip_name = csv_row.get("zip", "").strip()
    zip_base = os.path.join(DATA_DIR, zip_name) if zip_name and zip_name != result_container else None
    if os.path.isdir(os.path.join(base, "Flaky", "src")):
        source_base = base
    elif zip_base and os.path.isdir(os.path.join(zip_base, "Flaky", "src")):
        source_base = zip_base
    else:
        source_base = base

    victim_fqn = csv_row.get("flaky_test", "").strip()
    module = csv_row.get("module", ".").strip()
    java_ver = csv_row.get("java", "").strip()

    out = []
    out.append("=" * 60)
    out.append("LLM CONTEXT FOR FLAKY TEST PATCH GENERATION")
    out.append("=" * 60)
    out.append("")

    # --- TEST METADATA ---
    out.append("=== TEST METADATA ===")
    out.append("Test type:      TD (test-dependency)")
    out.append(f"Victim:         {victim_fqn}")
    out.append(f"Module:         {module}")
    out.append(f"Java:           {java_ver}")
    out.append("")

    # --- TEST CLASS HEADER ---
    # Structural view of the victim test class: package, imports, class
    # signature, field declarations, method signatures (no bodies). Lets the
    # LLM see the state surface and available helpers without dumping bodies.
    rel_path, method_name = fqn_to_path(victim_fqn)
    source_file = find_source_file(source_base, module, rel_path)
    victim_class_fqn = victim_fqn.split("#", 1)[0]

    if source_file:
        header = extract_class_header(source_file, include_inner_classes=False)
        if header:
            out.append("=== TEST CLASS HEADER ===")
            out.append(f"File: {os.path.basename(source_file)}  ({victim_class_fqn})")
            out.append("(Package + imports + class signature + field declarations +")
            out.append("method signatures. Method bodies are elided — request specific")
            out.append("bodies via TURN 2 METHOD if needed.)")
            out.append("")
            out.append(header.rstrip())
            out.append("")

    # --- VICTIM SOURCE CODE ---
    # Method-scoped extraction of the failing test. The LLM can request other
    # methods (helpers, @Before/@After) via TURN 2 METHOD if needed.
    out.append("=== VICTIM SOURCE CODE ===")
    if source_file:
        out.append(f"File: {os.path.basename(source_file)}")
        if method_name:
            method_src = extract_java_method(source_file, method_name)
            if method_src:
                out.append(f"Failing method: {method_name}")
                out.append("")
                out.append(method_src)
            else:
                out.append(_method_fallback_marker(
                    f"Could not extract method {method_name}"
                ))
        else:
            out.append(_method_fallback_marker(
                "Victim FQN has no #methodName"
            ))
    else:
        out.append(f"(Source file not found for {victim_fqn})")
    out.append("")

    # --- FAILURE OUTPUT ---
    # The TD orchestrator writes the failing-run mvn log to traces-flakycc/
    # (the FlakyCodeChange snapshot). Probe traces-flaky/ and traces-fixed/
    # as fallbacks for cross-compat.
    failure_text = "(no log file found)"
    for candidate in ("traces-flakycc", "traces-flaky", "traces-fixed"):
        text = extract_failure_from_log(
            os.path.join(source_base, candidate, "mvn.log")
        )
        if not text.startswith("("):
            failure_text = text
            break
    out.append("=== FAILURE OUTPUT ===")
    out.append("(The actual error during the failing execution)")
    out.append("")
    out.append(failure_text)
    out.append("")

    # --- PRODUCTION CODE REFERENCED IN STACK TRACE ---
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

    # RV TRACE ANALYSIS section deliberately omitted (no_rv ablation variant).

    # --- TASK ---
    out.append("=== TASK ===")
    out.append(f"GOAL: Make the test ({victim_fqn.split('#')[-1]}) pass deterministically.")
    out.append("The test currently fails on the codebase shown above. Identify the root")
    out.append("cause from the failure stack trace, and produce")
    out.append("the smallest patch that makes the test pass on every execution.")
    out.append("")

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
    out.append("")

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

    # --- TWO-TURN PROTOCOL ---
    out.extend(_two_turn_protocol_lines())

    # --- OUTPUT spec + cross-check ---
    out.extend(_three_outputs_spec_lines())

    # --- write ---
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
    assemble_context_td(sys.argv[1])
