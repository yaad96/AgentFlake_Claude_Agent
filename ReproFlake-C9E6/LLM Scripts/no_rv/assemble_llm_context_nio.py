#!/usr/bin/env python3
"""
assemble_llm_context_nio.py

NIO-specific variant of assemble_llm_context.py. Produces a structured LLM
context file for NIO (Non-Idempotent-Outcome) flaky tests.

Differences from OD/TD:
  - No POLLUTER section. NIO has no separate polluter; the test IS its own
    polluter on its second invocation (Wei et al., ICSE 2022).
  - Includes a "WRAPPER DRIVER" section that shows the auto-generated
    JUnitCore-based runTwice wrapper used to deterministically reproduce
    the NIO failure (the wrapper invokes the victim test method twice in
    the same forked JVM with full @Before/@After lifecycle each time).
  - FAILURE OUTPUT comes from the wrapper-twice run against Flaky/.
  - TASK section is NIO-framed: "this test self-pollutes via static state;
    add a cleanup at the end of the test method to reset that state".
  - Explicitly forbids modifying the wrapper class (it is the driver, not
    the fix target).

Usage:
    python assemble_llm_context_nio.py <result_container>

Output:
    data/<result_container>/Steps Output Files/llm_context.txt
"""

import os
import sys

# Reuse data-extraction helpers from the shared module (pure functions only).
# The shared module lives one directory up (in `LLM Scripts/`); this file
# lives in `LLM Scripts/rv/` (or its `no_rv/` sibling), so we add the parent.
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
# Wrapper-class lookup
# ---------------------------------------------------------------------------

def _wrapper_class_simple(victim_method):
    """Mirror run_nio_tracemop.sh's gen_wrapper naming: <MethodCap>NioReproTest."""
    if not victim_method:
        return ""
    return victim_method[:1].upper() + victim_method[1:] + "NioReproTest"


def _synth_wrapper_template(victim_pkg, victim_class_simple, victim_method, wrapper_simple):
    """Synthesize the wrapper text matching run_nio_tracemop.sh's gen_wrapper
    template. The on-disk file is byte-equivalent to this; we synthesize
    rather than read so the assembler doesn't depend on full-file extraction."""
    return (
        f"package {victim_pkg};\n\n"
        f"// AUTO-GENERATED — DO NOT EDIT.\n"
        f"// NIO repro driver: invokes {victim_class_simple}#{victim_method} twice in\n"
        f"// the same JVM (full JUnit lifecycle each time). Fix target is the victim,\n"
        f"// NOT this file.\n\n"
        f"import org.junit.Test;\n"
        f"import org.junit.Assert;\n"
        f"import org.junit.runner.JUnitCore;\n"
        f"import org.junit.runner.Request;\n"
        f"import org.junit.runner.Result;\n\n"
        f"public class {wrapper_simple} {{\n"
        f"    @Test public void runTwice() throws Exception {{\n"
        f"        Request req = Request.method({victim_class_simple}.class, \"{victim_method}\");\n"
        f"        Result r1 = new JUnitCore().run(req);\n"
        f"        Assert.assertTrue(\"first invocation should pass: \" + r1.getFailures(), r1.wasSuccessful());\n"
        f"        Result r2 = new JUnitCore().run(req);\n"
        f"        Assert.assertTrue(\"second invocation should pass (NIO assertion): \" + r2.getFailures(), r2.wasSuccessful());\n"
        f"    }}\n"
        f"}}\n"
    )


# ---------------------------------------------------------------------------
# Protocol blocks (duplicated from assemble_llm_context.py to keep this file
# self-contained — same rationale as assemble_llm_context_id.py: a future
# change to OD/TD must not silently shift NIO behavior).
# ---------------------------------------------------------------------------

def _two_turn_protocol_lines():
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
    out.append("  [1] The victim test method's body, the inner test classes it spawns")
    out.append("      (via `testResult(SomeClass.class)` or similar), or the static fields")
    out.append("      it touches are NOT fully shown above.")
    out.append("  [2] You cannot tell from the shown context which static field accumulates")
    out.append("      between invocations (the field whose value differs between run #1 and")
    out.append("      run #2 in the same JVM).")
    out.append("  [3] Your draft fix would touch a static collection or registry whose")
    out.append("      type or initialization isn't visible (you cannot tell whether `.clear()`")
    out.append("      is the right reset, vs `= new ArrayList<>()`, vs reset to a fixed")
    out.append("      initial state).")
    out.append("  [4] You would need to write phrases like 'I assume', 'I guess',")
    out.append("      'I don't know', 'I don't have the full file', 'line N is a guess',")
    out.append("      or 'not sure whether this import exists'.")
    out.append("  [5] Your draft fix would add a third-party import or depend on a")
    out.append("      library API whose dependency is not confirmed in the shown context.")
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
    out.append("                  absolute-line-number header. Use this when the polluted")
    out.append("                  static is mutated inside a lambda, field initializer, or")
    out.append("                  static block where there's no method name to ask for.")
    out.append("")
    out.append("Guidance for choosing artifacts (NIO-specific):")
    out.append("  - The TEST CLASS HEADER above already shows inner-class structure")
    out.append("    (signatures + static fields) for NIO. If you need to see HOW an inner")
    out.append("    @Property method mutates a static field, ask for METHOD on that inner")
    out.append("    class's property/test method.")
    out.append("  - Use FILE_SKELETON only if the TEST CLASS HEADER didn't fully render")
    out.append("    (rare — only happens for very large files past the 400-line cap).")
    out.append("  - Use AROUND when the failure line points at a static initializer, field")
    out.append("    initializer, or lambda rather than a clean method body.")
    out.append("  - Prefer 1-3 high-leverage artifacts over 5 marginal ones.")
    out.append("")
    out.append("End of TWO-TURN PROTOCOL. Below is the OUTPUT spec used either in")
    out.append("TURN 1 (path (a)) or TURN 2 (after artifacts are provided).")
    out.append("")
    return out


def _three_outputs_spec_lines():
    """OUTPUT 0 (diagnosis) + OUTPUT A (unified diff) + OUTPUT B (developer
    guide with @@OPERATION/@@ANCHOR schema). Same parser-facing format as
    OD/TD/ID so apply_fix.py and call_llm.py work without NIO-specific
    changes. Long-format scaffolding (CRITICAL DISCIPLINE preamble,
    step-by-step OUTPUT 0 with hunk-discipline self-verify checklist,
    line-for-line CROSS-CHECK [2], imports-parity CROSS-CHECK [3]) is
    shared with OD/TD; the per-step diagnosis bullets, the OUTPUT A
    wrapper-driver guard, the ROOT_CAUSE/FIX_DESCRIPTION wording, the
    insert_method @After example, the type-matching cleanup item (f),
    and CROSS-CHECK [7] are NIO-specific."""
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
    out.append("  1. Which static field is the pollution source? Name the field and the")
    out.append("     declaring class.")
    out.append("  2. WHERE in the test method is the field mutated — directly, or via an")
    out.append("     inner @Property class spawned by `testResult(Foo.class)`?")
    out.append("  3. WHY does the second invocation's assertion observe a different value")
    out.append("     than the first? Cite concrete numbers from the failure stack trace,")
    out.append("     e.g. the `expected:<N> but was:<2N>` pattern.")
    out.append("  4. The smallest cleanup statement that resets the field — append it to")
    out.append("     the END of the victim test method, AFTER the existing assertion(s).")
    out.append("     If the polluted state is shared by multiple test methods in the same")
    out.append("     class, an @After/@AfterEach hook on the outer test class is the")
    out.append("     equivalent shape.")
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
    out.append("       (f) The cleanup matches the polluted field's declared type — `= 0;`")
    out.append("           for an int, `= false;` for a boolean, `.clear();` for a collection")
    out.append("           reused in place, `= new ArrayList<>();` for a non-final collection")
    out.append("           field, etc.")
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
    out.append("CRITICAL: the fix target is the VICTIM source file. Do NOT modify the")
    out.append("WRAPPER DRIVER (the auto-generated <Method>NioReproTest.java shown in")
    out.append("its own section above) — it is the test driver that reproduces NIO, not")
    out.append("the bug site. A patch that touches the wrapper will be rejected.")
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
    out.append("<2-4 sentences naming the polluted static field, where it accumulates,")
    out.append(" and how the second invocation observes the accumulated value. NOT a")
    out.append(" restatement of the diff — name the underlying defect.>")
    out.append("")
    out.append("### FIX_DESCRIPTION")
    out.append("<2-4 sentences describing the cleanup line you append (or the @After")
    out.append(" hook you add) and WHY it resets the polluted state in time for the")
    out.append(" next invocation. A justification, not a diff replay.>")
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
    out.append("      original file (the typical NIO fix: rewrite the victim method body to")
    out.append("      add the trailing cleanup line).")
    out.append("    * 'insert_method' if the method is NEW (not present in the original) —")
    out.append("      e.g. adding an @After/@AfterEach hook on the outer test class.")
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
    out.append("  [7] Neither OUTPUT touches the WRAPPER DRIVER file (the auto-generated")
    out.append("      <Method>NioReproTest.java shown in its own section above).")
    out.append("If any of [1]-[7] disagree, RECONCILE both outputs (regenerate them in")
    out.append("OUTPUT 0's reasoning, then re-emit) before sending. The two outputs MUST")
    out.append("describe the IDENTICAL set of edits.")
    return out


# ---------------------------------------------------------------------------
# Main assembly
# ---------------------------------------------------------------------------

def assemble_context_nio(result_container):
    csv_row = load_csv_row(result_container)
    if not csv_row:
        sys.exit(f"ERROR: '{result_container}' not in CSV")

    test_type = csv_row.get("test_type", "").strip().lower()
    if test_type != "nio":
        sys.exit(
            f"ERROR: assemble_llm_context_nio.py targets NIO only; "
            f"got test_type='{test_type}'. For OD/brittle use assemble_llm_context_od.py, "
            f"for TD use assemble_llm_context_td.py, "
            f"for ID use assemble_llm_context_id.py."
        )

    base = os.path.join(DATA_DIR, result_container)

    # Source base: prefer result_container, fall back to zip dir (mirrors OD/TD/ID).
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
    # Derive class/method/pkg pieces from the victim FQN.
    if "#" in victim_fqn:
        victim_class_full, victim_method = victim_fqn.rsplit("#", 1)
    else:
        victim_class_full, victim_method = victim_fqn, ""
    victim_class_simple = victim_class_full.rsplit(".", 1)[-1] if "." in victim_class_full else victim_class_full
    victim_pkg = victim_class_full.rsplit(".", 1)[0] if "." in victim_class_full else ""
    wrapper_simple = _wrapper_class_simple(victim_method)
    wrapper_fqcn = (victim_pkg + "." + wrapper_simple) if victim_pkg else wrapper_simple

    out = []
    out.append("=" * 60)
    out.append("LLM CONTEXT FOR NIO FLAKY TEST PATCH GENERATION")
    out.append("=" * 60)
    out.append("")

    # --- TEST METADATA ---
    out.append("=== TEST METADATA ===")
    out.append("Test type:           NIO (Non-Idempotent-Outcome; Wei et al., ICSE 2022)")
    out.append(f"Victim:              {victim_fqn}")
    out.append(f"Module:              {module}")
    out.append(f"Java:                {java_ver}")
    out.append("")
    out.append("Background — what NIO flakiness is:")
    out.append("  A Non-Idempotent-Outcome (NIO) test deterministically changes its")
    out.append("  outcome from PASS to FAIL when run twice in the same execution")
    out.append("  environment — the first invocation passes and a later invocation in")
    out.append("  the same JVM fails (Wei et al., ICSE 2022). By construction, an NIO")
    out.append("  test both reads AND writes some shared state, so it pollutes and")
    out.append("  then observes its own pollution (\"self-pollution\" in the paper). The")
    out.append("  most-reported substrate is the heap reachable from static fields, but")
    out.append("  system properties, file-system or database state, OS resources, and")
    out.append("  global registries / caches all appear in real cases.")
    out.append("")
    out.append("  The harness reproduces the failure by re-invoking the victim test")
    out.append("  method within ONE JVM (see the wrapper driver below). Under a fresh")
    out.append("  JVM the first invocation passes; a later invocation reports the")
    out.append("  failure shown in the failure output.")
    out.append("")

    # --- TEST CLASS HEADER ---
    # Structural view of the victim test class — INCLUDING inner classes —
    # because NIO's polluted static fields live on inner @RunWith static
    # classes spawned via `testResult(Foo.class)`. The header gives the LLM:
    #   - outer-class fields (sometimes the pollution site)
    #   - inner-class names + their static fields (the most common pollution
    #     site for NIO bugs)
    #   - inner-class method signatures (so the LLM knows which @Property
    #     methods exist and can request specific bodies via TURN 2 METHOD)
    # Method bodies are elided — including the inner @Property methods that
    # actually mutate the polluted state. The trace evidence + the field
    # names + the failure stack trace are usually enough to identify the
    # cleanup needed, but the LLM can request specific method bodies if not.
    rel_path, method_name = fqn_to_path(victim_fqn)
    source_file = find_source_file(source_base, module, rel_path)
    victim_class_fqn = victim_fqn.split("#", 1)[0]

    if source_file:
        header = extract_class_header(source_file, include_inner_classes=True)
        if header:
            out.append("=== TEST CLASS HEADER ===")
            out.append(f"File: {os.path.basename(source_file)}  ({victim_class_fqn})")
            out.append("(Package + imports + outer class signature + field declarations +")
            out.append("method signatures + INNER CLASS structure (signatures + fields).")
            out.append("Method bodies are elided. Inner-class static fields are commonly the")
            out.append("NIO pollution sites — request inner-class @Property method bodies via")
            out.append("TURN 2 METHOD if you need to see exactly how the static is mutated.)")
            out.append("")
            out.append(header.rstrip())
            out.append("")

    # --- VICTIM TEST SOURCE ---
    # Method-scoped extraction of the victim test method — the outer-class
    # method that drives the NIO repro and where the cleanup line is added.
    out.append("=== VICTIM TEST SOURCE CODE ===")
    if source_file:
        out.append(f"File: {os.path.basename(source_file)}")
        if method_name:
            method_src = extract_java_method(source_file, method_name)
            if method_src:
                out.append(f"Failing method (the NIO victim): {method_name}")
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

    # --- WRAPPER DRIVER ---
    # Show the auto-generated wrapper verbatim so the LLM understands HOW
    # the NIO failure is being reproduced under TraceMOP. The wrapper is
    # NOT the fix target — it is explicitly off-limits.
    out.append("=== WRAPPER DRIVER (auto-generated, READ-ONLY) ===")
    out.append("This wrapper is generated by run_nio_tracemop.sh to reproduce the NIO")
    out.append("failure deterministically. It uses JUnitCore.run(Request.method(...)) to")
    out.append("invoke the victim TWICE in the SAME forked JVM, with full @Before/@After/")
    out.append("@Rule lifecycle each time. If the second invocation's assertion fails")
    out.append("(NIO!), the wrapper itself fails and surefire reports a failure.")
    out.append("")
    out.append("DO NOT modify this file. The fix target is the victim's test method.")
    out.append("")
    out.append(f"Wrapper class: {wrapper_fqcn}")
    out.append(f"Wrapper file:  {module}/src/test/java/{victim_pkg.replace('.', '/')}/{wrapper_simple}.java")
    out.append("")
    out.append("(Canonical template — regenerated each run by run_nio_tracemop.sh's")
    out.append("gen_wrapper. The on-disk file is byte-equivalent to this template.)")
    out.append("")
    out.append(_synth_wrapper_template(victim_pkg, victim_class_simple, victim_method, wrapper_simple))
    out.append("")

    # --- FAILURE OUTPUT ---
    # The orchestrator (run_nio_tracemop.sh) writes the failing wrapper run's
    # mvn log to traces-flaky/mvn.log.
    failure_text = "(no log file found)"
    for candidate in ("traces-flaky", "traces-fail"):
        text = extract_failure_from_log(
            os.path.join(source_base, candidate, "mvn.log")
        )
        if not text.startswith("("):
            failure_text = text
            break
    out.append("=== FAILURE OUTPUT ===")
    out.append("(The wrapper-twice run against the Flaky/ source. The first invocation")
    out.append("passes, the second invocation triggers the NIO assertion. The trailing")
    out.append("AssertionError comes from the wrapper's `Assert.assertTrue(...wasSuccessful())`")
    out.append("on the second JUnitCore Result — its message includes the inner failure.)")
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
            out.append(
                f"--- {entry['class']}.{entry['method']}() "
                f"[{entry['file']}:{entry['line']}] ---"
            )
            out.append(entry["source"])
            out.append("")

    # RV TRACE ANALYSIS section (and its empty-diff caveat) deliberately
    # omitted (no_rv ablation variant).

    # --- TASK ---
    out.append("=== TASK ===")
    out.append(
        f"GOAL: Make the test ({victim_method or victim_fqn}) pass when invoked"
    )
    out.append("twice in succession in the same JVM (i.e., make it idempotent). The")
    out.append("victim currently passes its first invocation but fails its second")
    out.append("invocation because it leaves polluted static state behind. Identify the")
    out.append("polluted field and add the smallest cleanup that resets it.")
    out.append("")
    out.append("Fix categories (in order of frequency in published NIO fixes — pick")
    out.append("whichever fits the evidence):")
    out.append("  1. Reset a static primitive at the END of the test method.")
    out.append("       Example:   SomeInnerClass.iterations = 0;")
    out.append("                  SomeInnerClass.flag = false;")
    out.append("  2. Clear a static Collection at the END of the test method.")
    out.append("       Example:   SomeInnerClass.values.clear();")
    out.append("                  SomeInnerClass.attempts.clear();")
    out.append("                  SomeInnerClass.foos.clear();")
    out.append("  3. (Less common) Add an @After method to the outer test class that")
    out.append("     performs the cleanup. Use this only when several test methods in")
    out.append("     the same class share the polluted field.")
    out.append("  4. (Rare) Re-initialize the static collection field rather than clearing")
    out.append("     it (e.g. `SomeInnerClass.values = new ArrayList<>();`) — only if the")
    out.append("     field is non-final.")
    out.append("")
    out.append("WHERE TO LOOK for the polluted field:")
    out.append("  - The victim test method typically calls `testResult(Foo.class)` (or a")
    out.append("    similar JUnitQuickcheck/property-based pattern). The static fields on")
    out.append("    `Foo` (which is itself a static inner class in the same .java file)")
    out.append("    are the actual pollution sites. Look at the inner class's static")
    out.append("    fields and the @Property/@Test method that mutates them.")
    out.append("  - The assertion in the victim that FAILS on the second run usually")
    out.append("    references the polluted field directly — e.g.")
    out.append("       assertEquals(defaultPropertyTrialCount(), Foo.iterations);")
    out.append("    means `Foo.iterations` is the field to reset.")
    out.append("")
    out.append("CONSTRAINTS:")
    out.append("- Make the SMALLEST possible change that makes the test idempotent.")
    out.append("- Add the cleanup line AT THE END of the test method, AFTER the existing")
    out.append("  assertion(s). Do not move or rewrite the assertions.")
    out.append("- Do NOT modify the WRAPPER DRIVER class shown above. It is read-only.")
    out.append("- Do NOT rename variables, methods, or classes.")
    out.append("- Do NOT refactor or restructure unrelated code.")
    out.append("- Do NOT add logging, print statements, or debug output.")
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
    assemble_context_nio(sys.argv[1])
