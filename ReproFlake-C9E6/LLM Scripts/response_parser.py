#!/usr/bin/env python3
"""
response_parser.py

Provider-agnostic parser for the LLM's OUTPUT 0 / OUTPUT A / OUTPUT B
response shape. Imported by both call_llm_claude.py and call_llm_openai.py
so neither backend depends on the other (and neither pulls in the other's
SDK at import time).

Pure-Python, stdlib only — no SDK dependencies.
"""

import re


def parse_response(raw):
    """
    Parse the raw LLM response into structured output_a / output_b fields.
    Returns a dict with the parsed structure.
    """
    parsed = {
        "output_0": {"diagnosis": None},
        "output_a": {"patch": None},
        "output_b": {"root_cause": None, "fix_description": None, "fixed_code": []},
    }

    # --- Split into Output 0, Output A, and Output B ---
    # Extract diagnosis (OUTPUT 0), everything between OUTPUT 0 and OUTPUT A
    diag_match = re.search(
        r'OUTPUT\s*0\s*[—–-]\s*DIAGNOSIS:?\s*\n(.*?)(?=OUTPUT\s*A\s*[—–-]\s*PATCH|$)',
        raw, re.DOTALL
    )
    if diag_match:
        parsed["output_0"]["diagnosis"] = diag_match.group(1).strip()

    # Split the response into the OUTPUT A and OUTPUT B sections. We split
    # on the OUTPUT A header first so that OUTPUT A's diff parser cannot
    # accidentally match ```diff or ```java fenced blocks emitted earlier
    # inside OUTPUT 0's reasoning (e.g. an "Original:"/"Replacement:" draft).
    a_split = re.split(r'OUTPUT\s*A\s*[—–-]\s*PATCH:?', raw, maxsplit=1)
    after_a = a_split[1] if len(a_split) > 1 else raw

    b_split = re.split(r'OUTPUT\s*B\s*[—–-]\s*DEVELOPER\s*GUIDE:?', after_a, maxsplit=1)
    a_section = b_split[0] if len(b_split) > 1 else after_a
    b_section = b_split[1] if len(b_split) > 1 else ""

    # --- Parse Output A: extract diff from a ```diff fenced block ---
    # Require the "diff" tag explicitly (no `(?:diff)?`) so plain ```...```
    # blocks (e.g. ```java teaching aids) cannot match. Take the LAST match
    # in the OUTPUT A section as belt-and-suspenders against the LLM emitting
    # a draft + a final.
    diff_matches = re.findall(r'```diff\s*\n(.*?)```', a_section, re.DOTALL)
    if diff_matches:
        parsed["output_a"]["patch"] = diff_matches[-1].strip()
    else:
        # Fallback 1: the LLM forgot the "diff" tag but the body still looks
        # like a unified diff (starts with --- a/ on the next line).
        plain_diff_matches = re.findall(r'```\s*\n(---\s.*?)```', a_section, re.DOTALL)
        if plain_diff_matches:
            parsed["output_a"]["patch"] = plain_diff_matches[-1].strip()
        else:
            # Fallback 2: everything in the OUTPUT A section.
            a_body = a_section.strip()
            if a_body:
                parsed["output_a"]["patch"] = a_body

    # --- Parse Output B: ROOT_CAUSE ---
    rc_match = re.search(r'###\s*ROOT_CAUSE\s*\n(.*?)(?=###\s*FIX_DESCRIPTION|$)', b_section, re.DOTALL)
    if rc_match:
        parsed["output_b"]["root_cause"] = rc_match.group(1).strip()

    # --- Parse Output B: FIX_DESCRIPTION ---
    fd_match = re.search(r'###\s*FIX_DESCRIPTION\s*\n(.*?)(?=###\s*FIXED_CODE|$)', b_section, re.DOTALL)
    if fd_match:
        parsed["output_b"]["fix_description"] = fd_match.group(1).strip()

    # --- Parse Output B: FIXED_CODE blocks ---
    fc_match = re.search(r'###\s*FIXED_CODE\s*\n(.*)', b_section, re.DOTALL)
    if fc_match:
        fc_section = fc_match.group(1)

        # Split on @@FILE: markers
        file_blocks = re.split(r'@@FILE:\s*', fc_section)[1:]  # skip text before first @@FILE

        for block in file_blocks:
            lines = block.split('\n', 1)
            file_path = lines[0].strip()
            rest = lines[1] if len(lines) > 1 else ""

            # Check for @@IMPORTS before the first @@METHOD
            first_method_pos = rest.find('@@METHOD:')
            imports_text = None
            if first_method_pos > 0:
                pre_method = rest[:first_method_pos]
                imp_match = re.search(r'@@IMPORTS:\s*\n(.*?)(?=@@METHOD:|$)', pre_method, re.DOTALL)
                if imp_match:
                    imports_text = imp_match.group(1).strip() or None

            # Extract all @@METHOD blocks within this @@FILE
            method_blocks = re.split(r'@@METHOD:\s*', rest)
            for mb in method_blocks[1:]:  # skip text before first @@METHOD
                mb_lines = mb.split('\n', 1)
                method_name = mb_lines[0].strip()
                mb_rest = mb_lines[1] if len(mb_lines) > 1 else ""

                # @@OPERATION and @@ANCHOR live between @@METHOD and the
                # ```java fence. Both are optional in the parsed output so
                # responses produced before the schema was extended still
                # parse cleanly; the applier should default operation to
                # 'replace_method' when absent.
                op_match = re.search(r'@@OPERATION:\s*([^\n]+)', mb_rest)
                operation = op_match.group(1).strip() if op_match else None

                anchor_match = re.search(r'@@ANCHOR:\s*([^\n]+)', mb_rest)
                anchor = anchor_match.group(1).strip() if anchor_match else None

                code_match = re.search(r'```java\s*\n(.*?)```', mb_rest, re.DOTALL)
                code = code_match.group(1).strip() if code_match else None

                parsed["output_b"]["fixed_code"].append({
                    "file": file_path,
                    "imports": imports_text,
                    "method": method_name,
                    "operation": operation,
                    "anchor": anchor,
                    "code": code,
                })

    return parsed
