#!/usr/bin/env python3
"""Compare tool_calls between two benchmark eval result files."""

import json
import sys
import os


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# DEFAULT_A = os.path.join(SCRIPT_DIR, "Output/Locomo/experiment_test_bot_top_1_connection_failures/qa_eval_detailed_results.json")
DEFAULT_A = os.path.join(SCRIPT_DIR, "Output/ClapNQ/experiment_test_bot_top_1/qa_eval_detailed_results.json")
# DEFAULT_A = os.path.join(SCRIPT_DIR, "Output/HotpotQA/experiment_test_bot_top_1/qa_eval_detailed_results.json")
# DEFAULT_B = os.path.join(SCRIPT_DIR, "Output/Locomo/experiment_test_bot_top_1_connection_failures_link/qa_eval_detailed_results.json")
DEFAULT_B = os.path.join(SCRIPT_DIR, "Output/ClapNQ/experiment_test_bot_top_1_link/qa_eval_detailed_results.json")
# DEFAULT_B = os.path.join(SCRIPT_DIR, "Output/HotpotQA/experiment_test_bot_top_1_link/qa_eval_detailed_results.json")

def parse_tool_calls(tc_raw):
    """Parse tool_calls from string or list, return list of {name, args} dicts."""
    if not tc_raw:
        return []
    if isinstance(tc_raw, list):
        result = []
        for tc in tc_raw:
            if not isinstance(tc, dict):
                continue
            name = tc.get('tool_name', '?')
            args = tc.get('args', '')
            # args might be a dict (already parsed) or string
            if isinstance(args, dict):
                args = _shorten_args(name, json.dumps(args, ensure_ascii=False))
            elif isinstance(args, str):
                args = _shorten_args(name, args)
            reasoning = tc.get('reasoning', '')
            if isinstance(reasoning, str):
                reasoning = ' '.join(reasoning.split())[:200]
            result.append({"name": name, "args": args, "reasoning": reasoning})
        return result
    if isinstance(tc_raw, str):
        import re
        entries = []
        # Find each tool_name, then extract args and reasoning
        for m in re.finditer(r'"tool_name"\s*:\s*"([^"]+)"', tc_raw):
            name = m.group(1)
            # Find "args": after this match, then extract the {...} block
            args_start = tc_raw.find('"args"', m.end())
            if args_start == -1:
                entries.append({"name": name, "args": "", "reasoning": ""})
                continue
            # Find the opening { of the args value
            brace_pos = tc_raw.find('{', args_start)
            if brace_pos == -1:
                entries.append({"name": name, "args": "", "reasoning": ""})
                continue
            # Brace-match to find the closing }
            depth = 0
            end = brace_pos
            for i in range(brace_pos, min(brace_pos + 2000, len(tc_raw))):
                if tc_raw[i] == '{':
                    depth += 1
                elif tc_raw[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            args_str = tc_raw[brace_pos:end].replace('\n', ' ').strip()
            short_args = _shorten_args(name, args_str)

            # Extract reasoning after args
            reasoning = ""
            reason_match = re.search(r'"reasoning"\s*:\s*"((?:[^"\\]|\\.)*)"', tc_raw[end:end+2000])
            if reason_match:
                reasoning = reason_match.group(1).replace('\\n', ' ').replace('\\"', '"').strip()
                reasoning = ' '.join(reasoning.split())[:1000]

            entries.append({"name": name, "args": short_args, "reasoning": reasoning})
        return entries
    return []


def _shorten_args(tool_name, args_str):
    """Extract the most relevant param from args for display."""
    import re
    if tool_name in ('openviking_search',):
        m = re.search(r'"query"\s*:\s*"([^"]*)"', args_str)
        return f'query="{m.group(1)}"' if m else args_str[:100]
    if tool_name in ('openviking_multi_read',):
        # URIs may be split across lines with whitespace, clean first
        cleaned = re.sub(r'\s+', '', args_str)
        uris = re.findall(r'viking://[^"\\,\]\}]+', cleaned)
        short = [u.rsplit('/', 1)[-1] for u in uris]
        return f'uris=[{", ".join(short)}]'
    if tool_name in ('openviking_link',):
        reason = re.search(r'"reason"\s*:\s*"([^"]*)"', args_str)
        from_uri = re.search(r'"from_uri"\s*:\s*"([^"]*)"', args_str)
        f = from_uri.group(1).rsplit('/', 1)[-1] if from_uri else '?'
        r = reason.group(1)[:80] if reason else '?'
        return f'from={f}, reason="{r}"'
    if tool_name in ('openviking_relations',):
        m = re.search(r'"uri"\s*:\s*"([^"]*)"', args_str)
        return f'uri={m.group(1).rsplit("/", 1)[-1]}' if m else args_str[:100]
    if tool_name in ('openviking_grep',):
        pattern = re.search(r'"pattern"\s*:\s*"([^"]*)"', args_str)
        uri = re.search(r'"uri"\s*:\s*"([^"]*)"', args_str)
        parts = []
        if uri:
            parts.append(f'uri=...{uri.group(1).rsplit("/", 1)[-1]}')
        if pattern:
            parts.append(f'pattern="{pattern.group(1)}"')
        return ', '.join(parts) if parts else args_str[:100]
    if tool_name in ('openviking_list',):
        uri = re.search(r'"uri"\s*:\s*"([^"]*)"', args_str)
        recursive = re.search(r'"recursive"\s*:\s*(true|false)', args_str)
        parts = []
        if uri:
            parts.append(f'uri=...{uri.group(1).rsplit("/", 1)[-1]}')
        if recursive:
            parts.append(f'recursive={recursive.group(1)}')
        return ', '.join(parts) if parts else args_str[:100]
    if tool_name in ('list_dir',):
        path = re.search(r'"path"\s*:\s*"([^"]*)"', args_str)
        return f'path=...{path.group(1).rsplit("/", 1)[-1]}' if path else args_str[:100]
    return args_str[:120]


def load_results(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    results = data.get('results', data if isinstance(data, list) else [])
    return sorted(results, key=lambda r: r.get('_global_index', 0))


def main():
    path_a = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_A
    path_b = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_B
    label_a = os.path.basename(os.path.dirname(path_a))
    label_b = os.path.basename(os.path.dirname(path_b))

    results_a = load_results(path_a)
    results_b = load_results(path_b)

    map_a = {r['_global_index']: r for r in results_a}
    map_b = {r['_global_index']: r for r in results_b}
    all_ids = sorted(set(map_a.keys()) | set(map_b.keys()))

    # Summary counters
    tool_freq_a = {}
    tool_freq_b = {}

    print(f"A: {label_a}")
    print(f"B: {label_b}")
    print(f"{'='*120}\n")

    for idx in all_ids:
        ra = map_a.get(idx)
        rb = map_b.get(idx)
        question = (ra or rb).get('question', '?')

        vb_a = (ra or {}).get('vikingbot', {})
        vb_b = (rb or {}).get('vikingbot', {})

        iters_a = vb_a.get('iterations_used', '-')
        iters_b = vb_b.get('iterations_used', '-')

        tc_a = parse_tool_calls(vb_a.get('tool_calls', ''))
        tc_b = parse_tool_calls(vb_b.get('tool_calls', ''))

        for t in tc_a:
            tool_freq_a[t["name"]] = tool_freq_a.get(t["name"], 0) + 1
        for t in tc_b:
            tool_freq_b[t["name"]] = tool_freq_b.get(t["name"], 0) + 1

        tc_names_a = [t["name"] for t in tc_a]
        tc_names_b = [t["name"] for t in tc_b]

        print(f"[{idx}] {question}")
        if isinstance(iters_a, int) and isinstance(iters_b, int) and iters_a != iters_b:
            d = iters_b - iters_a
            print(f"  *** iters differ: A={iters_a}, B={iters_b} ({'↑' if d > 0 else '↓'}{abs(d)}) ***")
            print(f"  A tools({len(tc_a)}):")
            for i, t in enumerate(tc_a, 1):
                print(f"    {i}. {t['name']}  {t['args']}")
                if t.get('reasoning'):
                    print(f"       reason: {t['reasoning']}")
            print(f"  B tools({len(tc_b)}):")
            for i, t in enumerate(tc_b, 1):
                print(f"    {i}. {t['name']}  {t['args']}")
                if t.get('reasoning'):
                    print(f"       reason: {t['reasoning']}")
        else:
            print(f"  iters={iters_a}, tools({len(tc_a)}): {' -> '.join(tc_names_a) if tc_names_a else '(none)'}")

        # Show tool diff
        if tc_names_a != tc_names_b:
            only_in_b = [t for t in set(tc_names_b) if t not in set(tc_names_a)]
            only_in_a = [t for t in set(tc_names_a) if t not in set(tc_names_b)]
            if only_in_b:
                print(f"  + B has: {', '.join(only_in_b)}")
            if only_in_a:
                print(f"  - A has: {', '.join(only_in_a)}")
        print()

    # Summary
    all_tools = sorted(set(tool_freq_a.keys()) | set(tool_freq_b.keys()))
    print(f"{'='*120}")
    print(f"\nTool call frequency summary:\n")
    print(f"  {'Tool':<30} {'A':>6} {'B':>6} {'Diff':>8}")
    print(f"  {'-'*30} {'-'*6} {'-'*6} {'-'*8}")
    for t in all_tools:
        ca = tool_freq_a.get(t, 0)
        cb = tool_freq_b.get(t, 0)
        d = cb - ca
        print(f"  {t:<30} {ca:>6} {cb:>6} {d:>+8}")

    total_a = sum(tool_freq_a.values())
    total_b = sum(tool_freq_b.values())
    print(f"  {'TOTAL':<30} {total_a:>6} {total_b:>6} {total_b-total_a:>+8}")

    # Iteration summary
    iters_a_list = [map_a[i]['vikingbot']['iterations_used'] for i in all_ids if i in map_a and 'vikingbot' in map_a[i]]
    iters_b_list = [map_b[i]['vikingbot']['iterations_used'] for i in all_ids if i in map_b and 'vikingbot' in map_b[i]]
    if iters_a_list and iters_b_list:
        print(f"\nIteration summary:")
        print(f"  A: avg={sum(iters_a_list)/len(iters_a_list):.2f}, min={min(iters_a_list)}, max={max(iters_a_list)}")
        print(f"  B: avg={sum(iters_b_list)/len(iters_b_list):.2f}, min={min(iters_b_list)}, max={max(iters_b_list)}")


if __name__ == "__main__":
    main()
