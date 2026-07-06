#!/usr/bin/env python3
"""
assemble_llm_context_od.py

OD (Order-Dependent) and BRITTLE variant of the LLM context assembler.
Produces a structured context file when the failure is caused by a polluter
test that runs immediately before the victim and corrupts shared state.

Differences from the other type-specific assemblers:
  - INCLUDES a POLLUTER SOURCE CODE section (the test that sets up the bad
    state). Neither ID, NIO, nor TD have this.
  - VICTIM TEST SOURCE CODE is method-scoped (since the polluter pinpoints
    which method to focus on).
  - FAILURE OUTPUT comes from traces-flakycc/mvn.log first (TD-style fix
    eval ordering), then traces-flaky/mvn.log (the polluter→victim run).
  - TASK section is OD-framed: cleanup in polluter / setup in victim /
    defensive check in production.
  - The TWO-TURN PROTOCOL + OUTPUT spec + cross-check items are duplicated
    inline so this file is self-contained, matching the per-type style of
    assemble_llm_context_id.py and assemble_llm_context_nio.py.

Usage:
    python assemble_llm_context_od.py <result_container>

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
    name is available. Points the LLM at the TEST CLASS HEADER and TURN 2
    METHOD instead of dumping a full class."""
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
    Same shape as the ID/NIO/TD assemblers; checklist items are tuned for
    OD/brittle (touching shared static state, reset-to-default patterns)."""
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
    out.append("                  (e.g. 'src/test/java/com/example/FooTest.java')")
    out.append("                  -> we return the file's package + import block.")
    out.append("  FILE_SKELETON   target = relative path to a .java file")
    out.append("                  -> we return a STRUCTURAL view: package + imports +")
    out.append("                  class signature(s) + field declarations + method signatures")
    out.append("                  (NO method bodies). Inner classes shown structurally too.")
    out.append("                  Capped at 300 lines. Use this to navigate large files; then")
    out.append("                  follow up with METHOD or AROUND for specific bodies/lines.")
    out.append("  METHOD          target = '<package.Class>#<methodName>'")
    out.append("                  -> we return the named method's annotations + signature + body,")
    out.append("                  capped at 100 lines (searches src/main/java first, then")
    out.append("                  src/test/java). Truncated methods get a marker; ask AROUND")
    out.append("                  for the specific lines you still need.")
    out.append("  AROUND          target = '<relative-path>#L<line>' or '#L<start>-<end>'")
    out.append("                  -> we return ±100 lines around the named line(s), with an")
    out.append("                  absolute-line-number header. Use this when you have a line")
    out.append("                  number from the stack trace, or when the pollution site is")
    out.append("                  in a static initializer / field initializer / lambda where")
    out.append("                  there's no clean method name to ask for.")
    out.append("  SPEC_DEFINITION target = RV spec name (e.g. 'Map_UnsafeIterator')")
    out.append("                  -> we return the spec's .mop definition (formal rule). Use")
    out.append("                  this when the RV TRACE ANALYSIS names a spec whose contract")
    out.append("                  you need to interpret formally.")
    out.append("")
    out.append("Guidance for choosing artifacts:")
    out.append("  - Prefer METHOD for surgical access. Use FILE_SKELETON to discover what")
    out.append("    methods/fields exist in a class without dumping implementation. Use")
    out.append("    AROUND when the failure stack trace gives a line number whose context")
    out.append("    spans method boundaries (field decls + initializer + method body).")
    out.append("  - If the failing path goes through production code that is NOT in")
    out.append("    the failure stack trace (e.g. logger internals, singleton getInstance,")
    out.append("    null-safety checks), ask for METHOD on those production methods.")
    out.append("    A naïve fix that doesn't see the production-side dereference can")
    out.append("    introduce a regression NPE.")
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
    without OD-specific changes."""
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
    out.append("  1. What shared state does the polluter modify or corrupt?")
    out.append("  2. What state does the victim assume or expect?")
    out.append("  3. Where exactly is the mismatch (which field, singleton, static, or global)?")
    out.append("  4. Which fix strategy from the list above is the smallest change that breaks the dependency?")
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
    out.append("           VICTIM TEST SOURCE / PRODUCTION CODE shown above. No fictitious files.")
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
    out.append("  Prefer 'before_method=' anchored on the polluter or related setup, so the")
    out.append("  new method sits with its logical neighbours.")
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

def assemble_context_od(result_container):
    csv_row = load_csv_row(result_container)
    if not csv_row:
        sys.exit(f"ERROR: '{result_container}' not in CSV")

    test_type = csv_row.get("test_type", "").strip().lower()
    if test_type not in ("od", "britle"):
        sys.exit(
            f"ERROR: assemble_llm_context_od.py targets OD/brittle only; "
            f"got test_type='{test_type}'. For TD use assemble_llm_context_td.py, "
            f"for ID use assemble_llm_context_id.py, "
            f"for NIO use assemble_llm_context_nio.py."
        )

    base = os.path.join(DATA_DIR, result_container)

    # Source base: prefer result_container, fall back to zip dir.
    # The CSV has separate 'result_container' and 'zip' columns. Multiple
    # rows can share the same zip (same codebase) but have different
    # result_containers (different polluter/victim pairs). Source code and
    # logs live under the zip dir via Docker bind mount; analysis files
    # (step_8_C_official.txt, llm_trace_summary.txt) live under result_container.
    zip_name = csv_row.get("zip", "").strip()
    zip_base = os.path.join(DATA_DIR, zip_name) if zip_name and zip_name != result_container else None
    if os.path.isdir(os.path.join(base, "Flaky", "src")):
        source_base = base
    elif zip_base and os.path.isdir(os.path.join(zip_base, "Flaky", "src")):
        source_base = zip_base
    else:
        source_base = base

    polluter_fqn = csv_row.get("polluter/state setter", "").strip()
    victim_fqn = csv_row.get("flaky_test", "").strip()
    module = csv_row.get("module", ".").strip()
    java_ver = csv_row.get("java", "").strip()
    has_polluter = polluter_fqn != ""

    type_labels = {
        "od": "OD (order-dependent)",
        "britle": "BRITTLE (fragile test interaction)",
    }

    out = []
    out.append("=" * 60)
    out.append("LLM CONTEXT FOR OD/BRITTLE FLAKY TEST PATCH GENERATION")
    out.append("=" * 60)
    out.append("")

    # --- TEST METADATA ---
    out.append("=== TEST METADATA ===")
    out.append(f"Test type:      {type_labels.get(test_type, test_type.upper())}")
    if has_polluter:
        out.append(f"Polluter:       {polluter_fqn}")
    out.append(f"Victim:         {victim_fqn}")
    out.append(f"Module:         {module}")
    out.append(f"Java:           {java_ver}")
    out.append("")
    out.append("Background — what OD/BRITTLE flakiness is:")
    out.append("  An Order-Dependent (OD) flaky test passes when run in isolation but")
    out.append("  fails when run after another test (the \"polluter\") that mutates")
    out.append("  shared state in a way the victim does not expect — typically a")
    out.append("  static field, singleton, registry, system property, file, or other")
    out.append("  process-global slot. A BRITTLE test follows the same pattern; the")
    out.append("  separate label is used when the dependency is fragile or harder to")
    out.append("  classify cleanly as a hard pollution. Unlike TD, the failure is NOT")
    out.append("  rooted in the test's own assumptions about timing or async. Unlike")
    out.append("  ID, no JVM iteration-order shuffling is involved. Unlike NIO, the")
    out.append("  test does not pollute itself across invocations — pollution comes")
    out.append("  from a separate test that ran earlier.")
    out.append("")
    out.append("  The harness reproduces the failure by running the polluter")
    out.append("  immediately before the victim within the same JVM against the")
    out.append("  FlakyCodeChange snapshot. The failure output below comes from that")
    out.append("  paired execution.")
    out.append("")

    # --- TEST CLASS HEADER ---
    # Structural view of the victim test class: package, imports, class
    # signature, field declarations, method signatures (no bodies). Plus the
    # polluter test class header if it lives in a different file. Lets the
    # LLM see the state surface (fields = potential pollution sites) and
    # available helpers/setUp/tearDown without dumping method bodies.
    victim_rel_path, _ = fqn_to_path(victim_fqn)
    victim_source_file = find_source_file(source_base, module, victim_rel_path)
    victim_class_fqn = victim_fqn.split("#", 1)[0]

    polluter_rel_path = None
    polluter_source_file = None
    polluter_class_fqn = None
    if has_polluter:
        polluter_rel_path, _ = fqn_to_path(polluter_fqn)
        polluter_class_fqn = polluter_fqn.split("#", 1)[0]
        if polluter_class_fqn != victim_class_fqn:
            polluter_source_file = find_source_file(source_base, module, polluter_rel_path)

    if victim_source_file:
        header = extract_class_header(victim_source_file, include_inner_classes=False)
        if header:
            out.append("=== TEST CLASS HEADER ===")
            out.append(f"File: {os.path.basename(victim_source_file)}  ({victim_class_fqn})")
            out.append("(Package + imports + class signature + field declarations +")
            out.append("method signatures. Method bodies are elided — request specific")
            out.append("bodies via TURN 2 METHOD if needed.)")
            out.append("")
            out.append(header.rstrip())
            out.append("")

    if polluter_source_file:
        header = extract_class_header(polluter_source_file, include_inner_classes=False)
        if header:
            out.append("=== POLLUTER CLASS HEADER ===")
            out.append(f"File: {os.path.basename(polluter_source_file)}  ({polluter_class_fqn})")
            out.append("(Polluter lives in a different test class than the victim.)")
            out.append("")
            out.append(header.rstrip())
            out.append("")

    # --- POLLUTER SOURCE CODE ---
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
                out.append(f"File: {os.path.basename(source_file)}")
                out.append(_method_fallback_marker(
                    f"Could not extract method {method_name}"
                ))
        elif source_file:
            out.append(f"File: {os.path.basename(source_file)}")
            out.append(_method_fallback_marker(
                "Polluter FQN has no #methodName"
            ))
        else:
            out.append(f"(Source file not found for {polluter_fqn})")
        out.append("")

    # --- VICTIM TEST SOURCE CODE ---
    rel_path, method_name = fqn_to_path(victim_fqn)
    source_file = find_source_file(source_base, module, rel_path)

    out.append("=== VICTIM TEST SOURCE CODE ===")
    if source_file:
        if method_name:
            method_src = extract_java_method(source_file, method_name)
            if method_src:
                out.append(f"File: {os.path.basename(source_file)}")
                out.append(f"Method: {method_name}")
                out.append("")
                out.append(method_src)
            else:
                out.append(f"File: {os.path.basename(source_file)}")
                out.append(_method_fallback_marker(
                    f"Could not extract method {method_name}"
                ))
        else:
            out.append(f"File: {os.path.basename(source_file)}")
            out.append(_method_fallback_marker(
                "Victim FQN has no #methodName"
            ))
    else:
        out.append(f"(Source file not found for {victim_fqn})")
    out.append("")

    # --- FAILURE OUTPUT ---
    # The orchestrator writes the failing-run mvn log to:
    #   traces-flakycc/mvn.log  (FlakyCodeChange variant; OD eval ordering)
    #   traces-flaky/mvn.log    (Flaky/ source with polluter→victim order)
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

    # --- RV TRACE ANALYSIS ---
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

    # --- TASK ---
    out.append("=== TASK ===")
    if has_polluter:
        out.append(f"GOAL: Make the victim test ({victim_fqn.split('#')[-1]}) pass")
        out.append(f"when run immediately after the polluter ({polluter_fqn.split('#')[-1]}).")
    else:
        out.append(f"GOAL: Make the test ({victim_fqn.split('#')[-1]}) pass deterministically")
        out.append("on every execution, regardless of timing or platform.")
    out.append("")

    if has_polluter:
        out.append("Possible fix categories (pick whichever fits the evidence):")
        out.append("  1. Add cleanup in the polluter (@After/@AfterEach) to restore shared state")
        out.append("  2. Add setup in the victim (@Before/@BeforeEach) to initialize required state")
        out.append("  3. Add a defensive state check in the production code")
    else:
        out.append("Possible fix categories (pick whichever fits the evidence):")
        out.append("  1. Replace non-deterministic calls with deterministic alternatives")
        out.append("  2. Add proper synchronization or waiting mechanisms")
        out.append("  3. Mock or control the source of non-determinism")
        out.append("  4. Fix incorrect assumptions about execution order")
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
    assemble_context_od(sys.argv[1])
