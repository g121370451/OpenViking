"""Parse messy JSON strings from OpenViking benchmark output.

Handles common issues:
- Actual newlines inside JSON string values (should be \\n)
- Unescaped quotes inside string values
- Nested JSON strings that need re-parsing (e.g. tool_calls is a JSON string)
"""

import json
import re
import sys
from pathlib import Path


def fix_unescaped_newlines_in_strings(text: str) -> str:
    """Replace literal newlines inside JSON string values with \\n."""
    result = []
    in_string = False
    escape_next = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escape_next:
            escape_next = False
            result.append(ch)
        elif ch == '\\':
            escape_next = True
            result.append(ch)
        elif ch == '"':
            in_string = not in_string
            result.append(ch)
        elif in_string and ch == '\n':
            # Replace actual newline inside string with escaped version
            result.append('\\n')
        elif in_string and ch == '\r':
            result.append('\\r')
        elif in_string and ch == '\t':
            result.append('\\t')
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def fix_trailing_commas(text: str) -> str:
    """Remove trailing commas before closing brackets/braces."""
    text = re.sub(r',\s*}', '}', text)
    text = re.sub(r',\s*]', ']', text)
    return text


def fix_single_quotes(text: str) -> str:
    """Replace single quotes used as JSON string delimiters with double quotes.
    Only do this at the top level of key-value pairs, not inside strings."""
    # This is conservative - only fix obvious cases like {'key': 'value'}
    # where the outer structure uses single quotes
    return text


def parse_messy_json(text: str, debug: bool = False) -> dict:
    """Try multiple strategies to parse a messy JSON string."""
    original = text

    # Strategy 1: Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if debug:
            print(f"[Strategy 1] Direct parse failed: {e}")

    # Strategy 2: Fix unescaped newlines in strings
    try:
        fixed = fix_unescaped_newlines_in_strings(text)
        fixed = fix_trailing_commas(fixed)
        return json.loads(fixed)
    except json.JSONDecodeError as e:
        if debug:
            print(f"[Strategy 2] Fix newlines failed: {e}")

    # Strategy 3: More aggressive - try to extract key fields manually
    # and rebuild the structure
    try:
        result = _manual_parse(text)
        if result is not None:
            return result
    except Exception as e:
        if debug:
            print(f"[Strategy 3] Manual parse failed: {e}")

    # Strategy 4: Use ast.literal_eval for Python dict format
    try:
        import ast
        return ast.literal_eval(text)
    except (ValueError, SyntaxError) as e:
        if debug:
            print(f"[Strategy 4] ast.literal_eval failed: {e}")

    raise ValueError(f"All parse strategies failed for JSON starting with: {original[:200]}...")


def _manual_parse(text: str) -> dict | None:
    """Manually extract top-level fields using regex and re-parse nested JSON."""
    result = {}

    # Extract top-level numeric/string fields
    for key in ["iterations", "iterations_used", "total_tool_time",
                "tool_calls_parse_error", "retrieval_iterations",
                "search_iterations", "read_iterations", "relations_hits",
                "total_relations_found", "links_created"]:
        m = re.search(rf'"{key}"\s*:\s*([^,\s}}]+)', text)
        if m:
            val = m.group(1)
            try:
                if val == "null":
                    result[key] = None
                elif val == "true":
                    result[key] = True
                elif val == "false":
                    result[key] = False
                elif '.' in val or 'e' in val.lower():
                    result[key] = float(val)
                else:
                    result[key] = int(val)
            except (ValueError, TypeError):
                result[key] = val

    # Extract token_usage sub-object
    tu_match = re.search(r'"token_usage"\s*:\s*({[^}]+})', text)
    if tu_match:
        try:
            tu_str = tu_match.group(1)
            tu_str = fix_unescaped_newlines_in_strings(tu_str)
            tu_str = fix_trailing_commas(tu_str)
            result["token_usage"] = json.loads(tu_str)
        except json.JSONDecodeError:
            result["token_usage"] = {}

    # Extract relation_edges_hit array
    reh_match = re.search(r'"relation_edges_hit"\s*:\s*(\[[^\]]*\])', text)
    if reh_match:
        try:
            result["relation_edges_hit"] = json.loads(reh_match.group(1))
        except json.JSONDecodeError:
            result["relation_edges_hit"] = []

    return result if result else None


def parse_tool_calls(tool_calls_str: str, debug: bool = False) -> list[dict]:
    """Parse the tool_calls JSON string (may be a JSON string containing a list)."""
    if not tool_calls_str or tool_calls_str == "[]":
        return []

    # If it's already a list, return directly
    if isinstance(tool_calls_str, list):
        return tool_calls_str

    # Strategy 1: Direct parse
    try:
        return json.loads(tool_calls_str)
    except json.JSONDecodeError as e:
        if debug:
            print(f"[ToolCalls] Direct parse failed: {e}")

    # Strategy 2: Fix unescaped newlines
    try:
        fixed = fix_unescaped_newlines_in_strings(tool_calls_str)
        fixed = fix_trailing_commas(fixed)
        return json.loads(fixed)
    except json.JSONDecodeError as e:
        if debug:
            print(f"[ToolCalls] Fix newlines failed: {e}")

    # Strategy 3: Try to extract individual tool call objects with regex
    try:
        # Find all tool call objects by matching balanced braces
        results = []
        depth = 0
        start = -1
        for i, ch in enumerate(tool_calls_str):
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start >= 0:
                    obj_str = tool_calls_str[start:i + 1]
                    try:
                        obj_str = fix_unescaped_newlines_in_strings(obj_str)
                        obj_str = fix_trailing_commas(obj_str)
                        results.append(json.loads(obj_str))
                    except json.JSONDecodeError:
                        if debug:
                            print(f"[ToolCalls] Failed to parse object: {obj_str[:100]}...")
                    start = -1
        if results:
            return results
    except Exception as e:
        if debug:
            print(f"[ToolCalls] Brace matching failed: {e}")

    raise ValueError(f"Failed to parse tool_calls: {tool_calls_str[:200]}...")


def extract_abstracts(records: list[dict], output_path: str | None = None):
    """Extract document abstracts from parsed tool_calls records."""
    lines = []
    for tc in records:
        if tc.get("tool_name") != "openviking_search":
            continue
        results = tc.get("result", [])
        if isinstance(results, str):
            try:
                results = json.loads(results)
            except json.JSONDecodeError:
                continue
        for r in results:
            uri = r.get("uri", "")
            abstract = r.get("abstract", "")
            reason = r.get("match_reason", "")
            rel_reason = r.get("relation_reason", "")
            score = r.get("score", 0)

            fname = uri.split("/")[-1] if uri else "unknown"
            lines.append(f"\n{'='*80}")
            lines.append(f"Document: {fname}")
            lines.append(f"URI: {uri}")
            lines.append(f"Score: {score:.4f}  Match: {reason}")
            if rel_reason:
                lines.append(f"Relation: {rel_reason}")
            lines.append(f"{'='*80}")
            lines.append(abstract if abstract else "(no abstract)")

    output = "\n".join(lines)
    if output_path:
        Path(output_path).write_text(output, encoding="utf-8")
        print(f"Abstracts written to: {output_path}")
    else:
        print(output)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Parse messy OpenViking JSON")
    parser.add_argument("input", nargs="?", help="JSON string or file path")
    parser.add_argument("--file", "-f", action="store_true", help="Input is a file path")
    parser.add_argument("--debug", "-d", action="store_true", help="Enable debug output")
    parser.add_argument("--extract-abstracts", "-a", type=str, help="Extract abstracts to file")
    parser.add_argument("--stdin", action="store_true", help="Read JSON from stdin")
    args = parser.parse_args()

    # Get input
    if args.stdin:
        text = sys.stdin.read()
    elif args.input:
        if args.file:
            text = Path(args.input).read_text(encoding="utf-8")
        else:
            text = args.input
    else:
        parser.print_help()
        return

    # Parse outer JSON
    print("Parsing outer JSON...")
    data = parse_messy_json(text, debug=args.debug)
    print(f"Parsed top-level keys: {list(data.keys())}")

    # Parse inner tool_calls
    tool_calls_str = data.get("tool_calls", "[]")
    if isinstance(tool_calls_str, list):
        tool_calls = tool_calls_str
    else:
        print(f"Parsing tool_calls (length={len(tool_calls_str)} chars)...")
        tool_calls = parse_tool_calls(tool_calls_str, debug=args.debug)

    print(f"Parsed {len(tool_calls)} tool calls")
    for i, tc in enumerate(tool_calls):
        tn = tc.get("tool_name", "unknown")
        dur = tc.get("duration", 0)
        success = tc.get("execute_success", False)
        rf = tc.get("relations_found", 0)
        n_results = len(tc.get("result", [])) if isinstance(tc.get("result"), list) else 0
        print(f"  [{i}] {tn} | duration={dur:.1f}ms | success={success} | relations={rf} | results={n_results}")

    # Extract abstracts if requested
    if args.extract_abstracts:
        extract_abstracts(tool_calls, args.extract_abstracts)

    # Print summary
    tu = data.get("token_usage", {})
    print(f"\nSummary:")
    print(f"  Iterations: {data.get('iterations_used', '?')}/{data.get('iterations', '?')}")
    print(f"  Tokens: {tu.get('total_tokens', '?')} (prompt={tu.get('prompt_tokens', '?')}, completion={tu.get('completion_tokens', '?')})")
    print(f"  Tool result tokens: {tu.get('tool_result_tokens', {})}")
    print(f"  Reasoning tokens: {tu.get('reasoning_tokens', 0)}")
    print(f"  Relations hits: {data.get('relations_hits', 0)}")
    print(f"  Links created: {data.get('links_created', 0)}")


if __name__ == "__main__":
    main()
