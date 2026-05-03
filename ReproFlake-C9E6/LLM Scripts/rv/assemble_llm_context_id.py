#!/usr/bin/env python3
"""
assemble_llm_context_id.py

ID-specific variant of assemble_llm_context.py. Produces a structured LLM
context file for ID (Implementation-Dependent) flaky tests.

Differences from the OD/TD assembler:
  - No POLLUTER section (ID has no polluter — the failure is caused by JDK
    iteration order shuffled by NonDex on a given seed, not by a preceding
    test).
  - VICTIM TEST SOURCE shows the failing test method (or full class).
  - FAILURE OUTPUT comes from traces-fail/mvn.log (the NonDex-with-failing-
    seed run) rather than traces-flaky/.
  - TASK section nudges toward ID-specific fix patterns (LinkedHashSet/Map,
    sort before iterating, containsExactlyInAnyOrder, TreeMap/TreeSet, etc.).
  - The TWO-TURN PROTOCOL + OUTPUT spec + cross-check items are duplicated
    inline so this file is self-contained; we deliberately do NOT modify
    assemble_llm_context.py (OD/TD baseline must keep working unchanged).

Usage:
    python assemble_llm_context_id.py <result_container>

Output:
    data/<result_container>/Steps Output Files/llm_context.txt
"""

import os
import sys

# Reuse data-extraction helpers from the shared module. We import only pure
# functions; we never invoke its assemble_context() entry point. The shared
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
# Protocol blocks (duplicated from assemble_llm_context.py to keep this file
# self-contained — see module docstring for rationale).
# ---------------------------------------------------------------------------

def _two_turn_protocol_lines():
    """Same TWO-TURN PROTOCOL + OUTPUT spec + CROSS-CHECK that OD/TD uses.
    Duplicated verbatim so a future change to OD/TD doesn't silently shift
    ID behavior. Cross-check items [1]-[6] still apply unchanged for ID
    (operation/anchor schema, artifact types, etc.)."""
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
    out.append("  [2] Your draft fix would touch a Collection/Map/Set whose element-order")
    out.append("      sensitivity is not visible from the test method alone (e.g., the")
    out.append("      production code that produces the iterated collection).")
    out.append("  [3] Your draft fix would change a return type or collection class in")
    out.append("      production code whose downstream consumers you have not seen.")
    out.append("  [4] You would need to write phrases like 'I assume', 'I guess',")
    out.append("      'I don't know', 'I don't have the full file', 'line N is a guess',")
    out.append("      or 'not sure whether this import exists'.")
    out.append("  [5] Your draft fix would add a third-party import or depend on a")
    out.append("      library API whose dependency is not confirmed in the shown context")
    out.append("      (for example AssertJ's containsExactlyInAnyOrder, Guava's")
    out.append("      ImmutableSet, Hamcrest matchers).")
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
    out.append("                  Capped at 300 lines.")
    out.append("  METHOD          target = '<package.Class>#<methodName>'")
    out.append("                  -> we return the named method's annotations + signature + body,")
    out.append("                  capped at 100 lines.")
    out.append("  AROUND          target = '<relative-path>#L<line>' or '#L<start>-<end>'")
    out.append("                  -> we return ±100 lines around the named line(s) with an")
    out.append("                  absolute-line-number header. Use this when the failure line")
    out.append("                  is inside a stream/lambda/iterator pipeline that doesn't")
    out.append("                  map cleanly to a single method name.")
    out.append("  SPEC_DEFINITION target = RV spec name (e.g. 'Map_UnsafeIterator')")
    out.append("                  -> we return the spec's .mop definition (formal rule). Use")
    out.append("                  this when the RV TRACE ANALYSIS names a spec whose contract")
    out.append("                  you need to interpret formally.")
    out.append("")
    out.append("Guidance for choosing artifacts:")
    out.append("  - If your suspected root cause is in production code (e.g. a method that")
    out.append("    returns a HashSet that the test then iterates), ask for METHOD on that")
    out.append("    production source.")
    out.append("  - Use FILE_SKELETON to navigate a large class quickly without dumping")
    out.append("    method bodies; then follow up with METHOD on the specific methods that")
    out.append("    produce or consume the order-dependent collection.")
    out.append("  - Use AROUND when the failure line is inside a lambda, stream pipeline,")
    out.append("    or iterator chain where method-name lookup wouldn't help.")
    out.append("  - If the RV trace names a spec (e.g. Map_UnsafeIterator), ask")
    out.append("    SPEC_DEFINITION to see the formal contract being violated.")
    out.append("  - Prefer 1-3 high-leverage artifacts over 5 marginal ones.")
    out.append("")
    out.append("End of TWO-TURN PROTOCOL. Below is the OUTPUT spec used either in")
    out.append("TURN 1 (path (a)) or TURN 2 (after artifacts are provided).")
    out.append("")
    return out


def _three_outputs_spec_lines():
    """OUTPUT 0 (diagnosis) + OUTPUT A (unified diff) + OUTPUT B (developer
    guide with @@OPERATION/@@ANCHOR schema). Same format as OD/TD so
    apply_fix.py and call_llm.py work without any ID-specific changes."""
    out = []
    out.append("Provide THREE outputs. Your response will be parsed by an automated")
    out.append("script — use the exact headers and fencing shown below. Do not")
    out.append("paraphrase, reorder, or omit any of them.")
    out.append("")
    out.append("## OUTPUT 0 — DIAGNOSIS")
    out.append("Free-form chain-of-thought (3-8 sentences). Cover:")
    out.append("  1. Which collection / API call is the source of the unspecified-order")
    out.append("     dependency.")
    out.append("  2. How NonDex's shuffling under the recorded seed flips the test from")
    out.append("     pass to fail (which iteration produces what assertion outcome).")
    out.append("  3. The location (file + method) where the dependency lives — the test")
    out.append("     itself, or production code that produces the order-sensitive value.")
    out.append("  4. The smallest mechanism that removes the dependency.")
    out.append("  5. Self-verify: confirm the file path you'll patch exists, your patch")
    out.append("     compiles (no missing imports), and the change is order-agnostic.")
    out.append("")
    out.append("## OUTPUT A — PATCH")
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
    out.append("## OUTPUT B — DEVELOPER GUIDE")
    out.append("A structured, redundant description of the same fix. Use the EXACT")
    out.append("section headers shown below — they are parsed by an automated script.")
    out.append("")
    out.append("### ROOT_CAUSE")
    out.append("<2-4 sentences naming the unspecified-order dependency and how the")
    out.append(" failing seed exposes it.>")
    out.append("")
    out.append("### FIX_DESCRIPTION")
    out.append("<2-4 sentences describing the patch and why it removes the order")
    out.append(" dependency.>")
    out.append("")
    out.append("### FIXED_CODE")
    out.append("Use this exact format for each modified file:")
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
    out.append("- Always include the FULL method body — never use ellipsis or '// ... unchanged'.")
    out.append("")
    out.append("CROSS-CHECK BEFORE FINALISING (mandatory before you stop generating):")
    out.append("Verify the following BEFORE emitting your response. If any check fails,")
    out.append("rewrite the relevant output until all checks pass.")
    out.append("  [1] Both OUTPUT A and OUTPUT B touch the SAME set of files (same relative")
    out.append("      paths). No file appears in only one of the two.")
    out.append("  [2] For each file, the SAME set of methods is changed in both outputs.")
    out.append("  [3] The semantic edit in each method is identical between OUTPUT A")
    out.append("      (lines added/removed) and OUTPUT B (the body of the ```java block).")
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

def assemble_context_id(result_container):
    csv_row = load_csv_row(result_container)
    if not csv_row:
        sys.exit(f"ERROR: '{result_container}' not in CSV")

    test_type = csv_row.get("test_type", "").strip().lower()
    if test_type != "id":
        sys.exit(
            f"ERROR: assemble_llm_context_id.py targets ID only; "
            f"got test_type='{test_type}'. For OD/brittle use assemble_llm_context_od.py, "
            f"for TD use assemble_llm_context_td.py, "
            f"for NIO use assemble_llm_context_nio.py."
        )

    base = os.path.join(DATA_DIR, result_container)

    # Source base: prefer result_container, fall back to zip dir (mirrors OD/TD).
    zip_name = csv_row.get("zip", "").strip()
    zip_base = os.path.join(DATA_DIR, zip_name) if zip_name and zip_name != result_container else None
    if os.path.isdir(os.path.join(base, "Flaky", "src")):
        source_base = base
    elif zip_base and os.path.isdir(os.path.join(zip_base, "Flaky", "src")):
        source_base = zip_base
    else:
        source_base = base

    victim_fqn = csv_row.get("flaky_test", "").strip()
    module = (csv_row.get("module", ".").strip() or ".")
    java_ver = csv_row.get("java", "").strip()
    # NonDex seed lives in the 'nondex' column in the CSV header. Be lenient
    # about the exact header capitalization in case CSVs differ.
    nondex_seed = (
        csv_row.get("nondex", "").strip()
        or csv_row.get("nondexSeed", "").strip()
        or csv_row.get("Nondex", "").strip()
    )
    iterations = csv_row.get("iterations", "").strip()

    out = []
    out.append("=" * 60)
    out.append("LLM CONTEXT FOR ID FLAKY TEST PATCH GENERATION")
    out.append("=" * 60)
    out.append("")

    # --- TEST METADATA ---
    out.append("=== TEST METADATA ===")
    out.append("Test type:           ID (Implementation-Dependent)")
    out.append(f"Victim:              {victim_fqn}")
    out.append(f"Module:              {module}")
    out.append(f"Java:                {java_ver}")
    out.append(f"Failing NonDex seed: {nondex_seed or '(unspecified)'}")
    out.append(f"NonDex iterations:   {iterations}")
    out.append("")
    out.append("Background — what ID flakiness is:")
    out.append("  An Implementation-Dependent flaky test depends on Java behavior the")
    out.append("  spec leaves UNSPECIFIED — typically iteration order of HashMap/HashSet,")
    out.append("  default-locale formatters, or similar. Two runs of the same test on the")
    out.append("  same source can pass or fail depending on which unspecified-behavior")
    out.append("  outcome the JVM produces.")
    out.append("")
    out.append("  NonDex (https://github.com/TestingResearchIllinois/NonDex) is a research")
    out.append("  tool that deliberately shuffles those orderings. Its randomization is")
    out.append("  deterministic given a seed. The seed shown above produces an iteration")
    out.append(f"  order under which the test fails; under the natural JDK default (no")
    out.append("  shuffling) the test passes.")
    out.append("")

    # --- TEST CLASS HEADER ---
    # Structural view of the victim test class: package + imports +
    # class signature + field declarations + method signatures (no bodies).
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

    # --- VICTIM TEST SOURCE ---
    out.append("=== VICTIM TEST SOURCE CODE ===")
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
    # The orchestrator (run_id_tracemop.sh) writes the failing run's mvn log
    # to traces-fail/mvn.log. We probe that first; fall back to traces-flaky/
    # for cross-compat with anything that might write under the OD naming.
    failure_text = "(no log file found)"
    for candidate in ("traces-fail", "traces-flaky"):
        text = extract_failure_from_log(
            os.path.join(source_base, candidate, "mvn.log")
        )
        if not text.startswith("("):
            failure_text = text
            break
    out.append("=== FAILURE OUTPUT ===")
    out.append(
        f"(The actual error when running the victim test under NonDex seed "
        f"{nondex_seed or '<unspecified>'})"
    )
    out.append("")
    out.append(failure_text)
    out.append("")

    # --- PRODUCTION CODE REFERENCED ---
    project_pkg = derive_project_package(victim_fqn)
    prod_code = extract_production_code_from_stacktrace(
        failure_text, source_base, module, project_pkg
    )
    if prod_code:
        out.append("=== PRODUCTION CODE REFERENCED IN STACK TRACE ===")
        out.append("(Methods from the project's main source that appear in the failure)")
        out.append("")
        for entry in prod_code:
            out.append(
                f"--- {entry['class']}.{entry['method']}() "
                f"[{entry['file']}:{entry['line']}] ---"
            )
            out.append(entry["source"])
            out.append("")

    # --- RV TRACE ANALYSIS ---
    trace_summary = read_file_safe(
        os.path.join(base, "Steps Output Files", "llm_trace_summary.txt")
    )
    if trace_summary.strip():
        out.append("=== RV TRACE ANALYSIS ===")
        out.append("(Generated by TraceMOP runtime verification. Compares RV traces from a")
        out.append("PASSING run (same Flaky/ source, no NonDex shuffling) against a FAILING")
        out.append("run (same source, NonDex with the recorded seed). Both runs execute the")
        out.append("identical bytecode of the project — only NonDex's iteration-order")
        out.append("shuffling differs. Distinctive trace events should localize the order-")
        out.append("dependent code path.)")
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
    out.append(
        f"GOAL: Make the test ({victim_fqn.split('#')[-1] if '#' in victim_fqn else victim_fqn})"
    )
    out.append("pass deterministically regardless of the iteration order chosen by the JVM")
    out.append("for any unspecified-order API. The test currently fails when NonDex shuffles")
    out.append(f"orderings under seed {nondex_seed or '<unspecified>'}. Identify where the")
    out.append("test depends on an unspecified ordering and produce the smallest patch")
    out.append("that removes that dependency.")
    out.append("")
    out.append("Possible fix categories (pick whichever fits the evidence — do NOT force")
    out.append("a strategy if the evidence does not point at it):")
    out.append("  1. Replace HashSet/HashMap with LinkedHashSet/LinkedHashMap in the test")
    out.append("     or in the production code that flows into the assertion. This is the")
    out.append("     most common ID fix when the test asserts on iteration-order-sensitive")
    out.append("     output (e.g. toString, collected list, serialization).")
    out.append("  2. Sort the collection before iterating or asserting on it.")
    out.append("  3. Replace order-sensitive assertEquals on a collection with a set-")
    out.append("     membership / containsExactlyInAnyOrder / hasItems-style assertion.")
    out.append("  4. Replace iterator-order-sensitive logic with a TreeMap / TreeSet (or")
    out.append("     a Comparator-based ordering) when a stable order is required.")
    out.append("  5. Use a deterministic factory / generator instead of one whose output")
    out.append("     order depends on JDK-internal hashing.")
    out.append("")
    out.append("CONSTRAINTS:")
    out.append("- Make the SMALLEST possible change that fixes the flakiness.")
    out.append("- Do NOT rename variables, methods, or classes.")
    out.append("- Do NOT refactor or restructure unrelated code.")
    out.append("- Do NOT add logging, print statements, or debug output.")
    out.append("- Do NOT weaken assertions just to make them order-agnostic UNLESS the")
    out.append("  assertion's order-sensitivity IS the root cause (the test was over-")
    out.append("  specifying a contract that the API never guaranteed).")
    out.append("- Do NOT modify method signatures or class hierarchy.")
    out.append("- Preserve the original code style.")
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
    assemble_context_id(sys.argv[1])
