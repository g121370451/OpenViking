#!/usr/bin/env python3
"""
Compare link vs non-link benchmark results.
Identifies cases where the link version uses more iterations,
consumes more tokens, or takes longer on retrieval — to analyze
why link performs worse.

Usage:
    python compare_link_effect.py [non_link.json] [link.json]
"""

import json
import re
import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_A = os.path.join(SCRIPT_DIR, "Output/FinanceBench/experiment_test_bot_top_1/qa_eval_detailed_results.json")
DEFAULT_B = os.path.join(SCRIPT_DIR, "Output/FinanceBench/experiment_test_bot_top_1_link_keyword/qa_eval_detailed_results.json")

ABLATION_GROUPS = {
    "baseline": os.path.join(SCRIPT_DIR, "Output/FinanceBench/experiment_test_bot_top_1/qa_eval_detailed_results.json"),
    # "link+none": os.path.join(SCRIPT_DIR, "Output/FinanceBench/experiment_test_bot_top_1_link/qa_eval_detailed_results.json"),
    # "link+vector": os.path.join(SCRIPT_DIR, "Output/FinanceBench/experiment_test_bot_top_1_link_vector/qa_eval_detailed_results.json"),
    "link+keyword": os.path.join(SCRIPT_DIR, "Output/FinanceBench/experiment_test_bot_top_1_link_keyword/qa_eval_detailed_results.json"),
}


def parse_malformed_json(raw_text):
    """
    Fix and parse malformed JSON from LLM tool_calls.
    - args: stack-based brace matching to strip extra quotes
    - reasoning: regex boundary detection, escape inner quotes
    - Wraps in [...] and uses strict=False for raw newlines
    """
    all_keys = ['tool_name', 'args', 'reasoning', 'duration', 'execute_success']
    key_alt = '|'.join(re.escape(k) for k in all_keys)
    boundary_re = re.compile(r'"\s*,\s*"(' + key_alt + r')"|"\s*,\s*\{|"\s*\}')

    result = []
    i = 0
    length = len(raw_text)

    while i < length:
        if raw_text[i:i+6] == '"args"':
            result.append('"args"')
            i += 6
            found_colon = False
            while i < length:
                c = raw_text[i]
                if c in ' \t\n\r':
                    result.append(c); i += 1
                elif c == ':' and not found_colon:
                    found_colon = True; result.append(c); i += 1
                elif c == '"' and found_colon:
                    peek = i + 1
                    while peek < length and raw_text[peek] in ' \t\n\r\\':
                        peek += 1
                    if peek < length and raw_text[peek] == '{':
                        i += 1
                        stack, in_str, esc = 0, False, False
                        while i < length:
                            ch = raw_text[i]
                            if ch == '\\' and not esc:
                                esc = True
                            else:
                                if ch == '"' and not esc: in_str = not in_str
                                if not in_str:
                                    if ch == '{': stack += 1
                                    elif ch == '}': stack -= 1
                                esc = False
                            result.append(ch); i += 1
                            if stack == 0 and ch == '}':
                                while i < length and raw_text[i] in ' \t\n\r':
                                    result.append(raw_text[i]); i += 1
                                if i < length and raw_text[i] == '"': i += 1
                                break
                        break
                    else:
                        result.append(c); i += 1; break
                else:
                    result.append(c); i += 1; break
            continue

        if raw_text[i:i+11] == '"reasoning"':
            result.append('"reasoning"')
            i += 11
            while i < length and raw_text[i] in ' \t\n\r':
                result.append(raw_text[i]); i += 1
            if i < length and raw_text[i] == ':':
                result.append(':'); i += 1
            while i < length and raw_text[i] in ' \t\n\r':
                result.append(raw_text[i]); i += 1
            if i < length and raw_text[i] == '"':
                result.append('"'); i += 1
                m = boundary_re.search(raw_text, i)
                if m:
                    result.append(raw_text[i:m.start()].replace('"', '\\"'))
                    result.append('"')
                    i = m.start() + 1
                else:
                    result.append(raw_text[i:].replace('"', '\\"'))
                    i = length
            continue

        result.append(raw_text[i])
        i += 1

    fixed = ''.join(result)
    if not fixed.strip().startswith('['):
        fixed = '[' + fixed + ']'
    data = json.loads(fixed, strict=False)
    if isinstance(data, str):
        data = json.loads(data, strict=False)
    return data


def parse_tool_calls(tc_raw):
    """Parse tool_calls from string or list, return list of dicts with duration."""
    if not tc_raw:
        return []
    if isinstance(tc_raw, list):
        return tc_raw
    if isinstance(tc_raw, str):
        try:
            return parse_malformed_json(tc_raw)
        except Exception:
            return []
    return []


def extract_tool_durations(tool_calls):
    """Sum up duration of each tool call, grouped by tool_name."""
    total = 0.0
    by_tool = {}
    for tc in tool_calls:
        name = tc.get('tool_name', '?')
        dur = tc.get('duration', 0.0) or 0.0
        total += dur
        by_tool[name] = by_tool.get(name, 0.0) + dur
    return total, by_tool


def shorten_args(tc):
    """Brief summary of a tool call for display."""
    name = tc.get('tool_name', '?')
    args = tc.get('args', '')
    if isinstance(args, dict):
        args = json.dumps(args, ensure_ascii=False)
    if name == 'openviking_search':
        m = re.search(r'"query"\s*:\s*"([^"]*)"', str(args))
        return f'query="{m.group(1)}"' if m else str(args)[:80]
    if name == 'openviking_link':
        reason = re.search(r'"reason"\s*:\s*"([^"]*)"', str(args))
        return f'reason="{reason.group(1)[:60]}"' if reason else str(args)[:80]
    if name in ('openviking_multi_read', 'openviking_read'):
        uris = re.findall(r'viking://[^"\\,\]\}\s]+', str(args))
        short = [u.rsplit('/', 1)[-1] for u in uris]
        return f'[{", ".join(short)}]'
    return str(args)[:80]


def load_results(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    results = data.get('results', data if isinstance(data, list) else [])
    return sorted(results, key=lambda r: r.get('_global_index', 0))


def truncate_reasoning(reasoning, max_len=10000):
    """Clean and truncate reasoning text for display."""
    if not reasoning:
        return ""
    if isinstance(reasoning, str):
        reasoning = reasoning.replace('\\n', ' ').replace('\n', ' ')
        reasoning = ' '.join(reasoning.split())
        if len(reasoning) > max_len:
            reasoning = reasoning[:max_len] + '...'
    return reasoning


def format_tool_call_md(i, tc):
    """Format a single tool call as markdown list item with reasoning."""
    name = tc.get('tool_name', '?')
    dur = tc.get('duration', 0) or 0
    args_short = shorten_args(tc)
    reasoning = truncate_reasoning(tc.get('reasoning', ''))
    lines = [f"   {i}. **{name}** `{args_short}` *[{dur:.1f}s]*"]
    if reasoning:
        lines.append(f"      > {reasoning}")
    return '\n'.join(lines)


def build_report(path_a, path_b, case_filter="all"):
    """Build the full markdown report and return as string."""
    label_a = os.path.basename(os.path.dirname(path_a))
    label_b = os.path.basename(os.path.dirname(path_b))
    dataset = os.path.basename(os.path.dirname(os.path.dirname(path_a)))

    results_a = load_results(path_a)
    results_b = load_results(path_b)

    map_a = {r['_global_index']: r for r in results_a}
    map_b = {r['_global_index']: r for r in results_b}
    all_ids = sorted(set(map_a.keys()) & set(map_b.keys()))

    md = []
    md.append(f"# Link Effect Analysis Report — {dataset}")
    md.append("")
    md.append(f"| | Path | Label |")
    md.append(f"|---|---|---|")
    md.append(f"| A (non-link) | `{path_a}` | {label_a} |")
    md.append(f"| B (link) | `{path_b}` | {label_b} |")
    md.append(f"| Common samples | {len(all_ids)} | |")
    md.append("")

    # --- Collect all case data ---
    all_cases = []
    for idx in all_ids:
        ra, rb = map_a[idx], map_b[idx]
        vb_a = ra.get('vikingbot', {})
        vb_b = rb.get('vikingbot', {})

        iters_a = vb_a.get('iterations_used', 0) or 0
        iters_b = vb_b.get('iterations_used', 0) or 0

        tok_a = vb_a.get('token_usage', {})
        tok_b = vb_b.get('token_usage', {})
        prompt_a = tok_a.get('prompt_tokens', 0) or 0
        prompt_b = tok_b.get('prompt_tokens', 0) or 0
        total_tok_a = tok_a.get('total_tokens', 0) or 0
        total_tok_b = tok_b.get('total_tokens', 0) or 0

        tc_a = parse_tool_calls(vb_a.get('tool_calls', ''))
        tc_b = parse_tool_calls(vb_b.get('tool_calls', ''))
        dur_a, dur_by_tool_a = extract_tool_durations(tc_a)
        dur_b, dur_by_tool_b = extract_tool_durations(tc_b)

        score_a = ra.get('llm_evaluation', {}).get('normalized_score', None)
        score_b = rb.get('llm_evaluation', {}).get('normalized_score', None)

        all_cases.append({
            'idx': idx,
            'question': ra.get('question', '?'),
            'iters_a': iters_a, 'iters_b': iters_b,
            'total_tok_a': total_tok_a, 'total_tok_b': total_tok_b,
            'prompt_a': prompt_a, 'prompt_b': prompt_b,
            'dur_a': dur_a, 'dur_b': dur_b,
            'score_a': score_a, 'score_b': score_b,
            'tc_a': tc_a, 'tc_b': tc_b,
            'dur_by_tool_a': dur_by_tool_a, 'dur_by_tool_b': dur_by_tool_b,
            'search_iters_a': vb_a.get('search_iterations', 0) or 0,
            'search_iters_b': vb_b.get('search_iterations', 0) or 0,
            'relations_hits_a': vb_a.get('relations_hits', 0) or 0,
            'relations_hits_b': vb_b.get('relations_hits', 0) or 0,
            'relations_found_a': vb_a.get('total_relations_found', 0) or 0,
            'relations_found_b': vb_b.get('total_relations_found', 0) or 0,
        })

    # --- Filter out failed cases (either side iterations_used=0) ---
    skipped = [c for c in all_cases if c['iters_a'] == 0 or c['iters_b'] == 0]
    all_cases = [c for c in all_cases if c['iters_a'] > 0 and c['iters_b'] > 0]

    if skipped:
        md.append(f"> **Filtered out {len(skipped)} failed cases** (iterations_used=0 on either side, typically process errors or JSON parse failures):")
        md.append(">")
        for c in skipped:
            side = "A" if c['iters_a'] == 0 else "B" if c['iters_b'] == 0 else "A+B"
            md.append(f"> - [{c['idx']}] {c['question'][:80]} — failed on {side}")
        md.append("")
        md.append(f"Effective samples after filtering: **{len(all_cases)}** / {len(all_ids)}")
        md.append("")

    # ===================== Section 1: Aggregate Summary =====================
    md.append("---")
    md.append("## 1. Aggregate Summary")
    md.append("")

    avg = lambda lst: sum(lst) / len(lst) if lst else 0
    all_iters_a = [c['iters_a'] for c in all_cases]
    all_iters_b = [c['iters_b'] for c in all_cases]
    all_tok_a = [c['total_tok_a'] for c in all_cases]
    all_tok_b = [c['total_tok_b'] for c in all_cases]
    all_prompt_a = [c['prompt_a'] for c in all_cases]
    all_prompt_b = [c['prompt_b'] for c in all_cases]
    all_dur_a = [c['dur_a'] for c in all_cases]
    all_dur_b = [c['dur_b'] for c in all_cases]

    md.append("| Metric | A (non-link) | B (link) | Diff |")
    md.append("|---|---:|---:|---:|")
    md.append(f"| Avg iterations | {avg(all_iters_a):.2f} | {avg(all_iters_b):.2f} | {avg(all_iters_b)-avg(all_iters_a):+.2f} |")
    md.append(f"| Total iterations | {sum(all_iters_a)} | {sum(all_iters_b)} | {sum(all_iters_b)-sum(all_iters_a):+d} |")
    md.append(f"| Avg total tokens | {avg(all_tok_a):.0f} | {avg(all_tok_b):.0f} | {avg(all_tok_b)-avg(all_tok_a):+.0f} |")
    md.append(f"| Total tokens | {sum(all_tok_a)} | {sum(all_tok_b)} | {sum(all_tok_b)-sum(all_tok_a):+d} |")
    md.append(f"| Avg prompt tokens | {avg(all_prompt_a):.0f} | {avg(all_prompt_b):.0f} | {avg(all_prompt_b)-avg(all_prompt_a):+.0f} |")
    md.append(f"| Avg tool time (s) | {avg(all_dur_a):.1f} | {avg(all_dur_b):.1f} | {avg(all_dur_b)-avg(all_dur_a):+.1f} |")
    md.append(f"| Total tool time (s) | {sum(all_dur_a):.1f} | {sum(all_dur_b):.1f} | {sum(all_dur_b)-sum(all_dur_a):+.1f} |")

    all_search_a = [c['search_iters_a'] for c in all_cases]
    all_search_b = [c['search_iters_b'] for c in all_cases]
    all_rel_hits_a = [c['relations_hits_a'] for c in all_cases]
    all_rel_hits_b = [c['relations_hits_b'] for c in all_cases]
    all_rel_found_a = [c['relations_found_a'] for c in all_cases]
    all_rel_found_b = [c['relations_found_b'] for c in all_cases]
    n = len(all_cases) or 1

    md.append(f"| Avg search iterations | {avg(all_search_a):.2f} | {avg(all_search_b):.2f} | {avg(all_search_b)-avg(all_search_a):+.2f} |")
    md.append(f"| Relations hit rate | {sum(1 for h in all_rel_hits_a if h > 0)/n:.1%} | {sum(1 for h in all_rel_hits_b if h > 0)/n:.1%} | |")
    md.append(f"| Avg relations found | {avg(all_rel_found_a):.2f} | {avg(all_rel_found_b):.2f} | {avg(all_rel_found_b)-avg(all_rel_found_a):+.2f} |")

    scores_a = [c['score_a'] for c in all_cases if c['score_a'] is not None and c['score_b'] is not None]
    scores_b = [c['score_b'] for c in all_cases if c['score_a'] is not None and c['score_b'] is not None]
    if scores_a:
        md.append(f"| **Avg score** | **{avg(scores_a):.2f}** | **{avg(scores_b):.2f}** | **{avg(scores_b)-avg(scores_a):+.2f}** |")
    md.append("")

    if scores_a:
        worse_score = sum(1 for a, b in zip(scores_a, scores_b) if b < a)
        better_score = sum(1 for a, b in zip(scores_a, scores_b) if b > a)
        same_score = sum(1 for a, b in zip(scores_a, scores_b) if b == a)
        md.append(f"Score breakdown: B better={better_score}, same={same_score}, B worse={worse_score}")
        md.append("")

    # Tool frequency table
    tool_freq_a, tool_freq_b = {}, {}
    for c in all_cases:
        for t in c['tc_a']: tool_freq_a[t.get('tool_name','?')] = tool_freq_a.get(t.get('tool_name','?'), 0) + 1
        for t in c['tc_b']: tool_freq_b[t.get('tool_name','?')] = tool_freq_b.get(t.get('tool_name','?'), 0) + 1
    all_tools = sorted(set(tool_freq_a.keys()) | set(tool_freq_b.keys()))

    md.append("### Tool Call Frequency")
    md.append("")
    md.append("| Tool | A | B | Diff |")
    md.append("|---|---:|---:|---:|")
    for t in all_tools:
        ca = tool_freq_a.get(t, 0)
        cb = tool_freq_b.get(t, 0)
        md.append(f"| {t} | {ca} | {cb} | {cb-ca:+d} |")
    md.append(f"| **TOTAL** | **{sum(tool_freq_a.values())}** | **{sum(tool_freq_b.values())}** | **{sum(tool_freq_b.values())-sum(tool_freq_a.values()):+d}** |")
    md.append("")

    # ===================== Section 2: Worse cases detail =====================
    worse_cases = [c for c in all_cases if (c['iters_b'] - c['iters_a'] > 0) or (c['total_tok_b'] - c['total_tok_a'] > 0) or (c['dur_b'] - c['dur_a'] > 5)]

    if case_filter in ("all", "worse"):
        md.append("---")
        md.append(f"## 2. Cases Where Link Is Worse ({len(worse_cases)}/{len(all_ids)})")
        md.append("")

        for c in sorted(worse_cases, key=lambda x: -(x['total_tok_b'] - x['total_tok_a'])):
            iter_diff = c['iters_b'] - c['iters_a']
            tok_diff = c['total_tok_b'] - c['total_tok_a']
            dur_diff = c['dur_b'] - c['dur_a']
            score_tag = ""
            if c['score_b'] is not None and c['score_a'] is not None and c['score_b'] < c['score_a']:
                score_tag = " ⚠️ score dropped"

            md.append(f"### [{c['idx']}] {c['question']}")
            md.append("")
            md.append(f"| | A (non-link) | B (link) | Diff |")
            md.append(f"|---|---:|---:|---:|")
            md.append(f"| Score | {c['score_a']} | {c['score_b']} | {(c['score_b'] or 0) - (c['score_a'] or 0):+d}{score_tag} |")
            md.append(f"| Iterations | {c['iters_a']} | {c['iters_b']} | {iter_diff:+d} |")
            md.append(f"| Total tokens | {c['total_tok_a']} | {c['total_tok_b']} | {tok_diff:+d} |")
            md.append(f"| Prompt tokens | {c['prompt_a']} | {c['prompt_b']} | {c['prompt_b']-c['prompt_a']:+d} |")
            md.append(f"| Tool time (s) | {c['dur_a']:.1f} | {c['dur_b']:.1f} | {dur_diff:+.1f} |")
            md.append("")

            md.append("**A tool calls:**")
            md.append("")
            for i, tc in enumerate(c['tc_a'], 1):
                md.append(format_tool_call_md(i, tc))
            md.append("")

            md.append("**B tool calls:**")
            md.append("")
            for i, tc in enumerate(c['tc_b'], 1):
                md.append(format_tool_call_md(i, tc))
            md.append("")

            extra_tools = [tc.get('tool_name', '?') for tc in c['tc_b'] if tc.get('tool_name') not in {t.get('tool_name') for t in c['tc_a']}]
            if extra_tools:
                md.append(f"B extra tools: `{'`, `'.join(set(extra_tools))}`")
                md.append("")

    # ===================== Section 3: Worst of worst =====================
    if case_filter in ("all", "effort_worse"):
        md.append("---")
        md.append("## 3. Worst Cases: More Effort, Worse Result")
        md.append("")

        worst = [c for c in worse_cases if c['score_a'] is not None and c['score_b'] is not None and c['score_b'] < c['score_a']]
        if worst:
            md.append("| idx | Question | Score A→B | Iters A→B | Tokens A→B |")
            md.append("|---:|---|---|---|---|")
            for c in sorted(worst, key=lambda x: (x['score_a'] or 0) - (x['score_b'] or 0), reverse=True):
                q = c['question'][:80]
                md.append(f"| {c['idx']} | {q} | {c['score_a']}→{c['score_b']} | {c['iters_a']}→{c['iters_b']} | {c['total_tok_a']}→{c['total_tok_b']} |")
        else:
            md.append("(none)")
        md.append("")

    # ===================== Section 4: Search iterations increased =====================
    if case_filter in ("all", "search_up"):
        search_up_cases = [c for c in all_cases if c['search_iters_b'] > c['search_iters_a']]
        md.append("---")
        md.append(f"## 4. Cases Where Search Iterations Increased ({len(search_up_cases)}/{len(all_cases)})")
        md.append("")

        if search_up_cases:
            md.append("| idx | Question | Search A→B | Iters A→B | Score A→B | Relations B |")
            md.append("|---:|---|---|---|---|---:|")
            for c in sorted(search_up_cases, key=lambda x: x['search_iters_b'] - x['search_iters_a'], reverse=True):
                q = c['question'][:70]
                md.append(f"| {c['idx']} | {q} | {c['search_iters_a']}→{c['search_iters_b']} | {c['iters_a']}→{c['iters_b']} | {c['score_a']}→{c['score_b']} | {c['relations_found_b']} |")
        else:
            md.append("(none)")
        md.append("")

    return '\n'.join(md)


def main():
    """Two-group comparison.

    Usage:
        python compare_link_effect.py [path_a] [path_b] [--cases=all|worse|effort_worse|search_up]
    """
    # Parse --cases flag
    case_filter = "all"
    args = [a for a in sys.argv[1:] if not a.startswith("--cases") and a != "--ablation"]
    for a in sys.argv[1:]:
        if a.startswith("--cases="):
            case_filter = a.split("=", 1)[1]

    path_a = args[0] if len(args) > 0 else DEFAULT_A
    path_b = args[1] if len(args) > 1 else DEFAULT_B

    output_path = None
    if len(args) > 2:
        output_path = args[2]
    else:
        dataset = os.path.basename(os.path.dirname(os.path.dirname(path_a)))
        output_path = os.path.join(SCRIPT_DIR, f"report_link_effect_{dataset}.md")

    report = build_report(path_a, path_b, case_filter=case_filter)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"Report written to: {output_path}")

    print(report)


def build_ablation_summary(groups: dict[str, str]) -> str:
    """Build a multi-group ablation comparison summary table."""
    avg = lambda lst: sum(lst) / len(lst) if lst else 0

    loaded = {}
    for name, path in groups.items():
        if not os.path.exists(path):
            continue
        loaded[name] = {r['_global_index']: r for r in load_results(path)}

    if len(loaded) < 2:
        return "Not enough groups with data for ablation comparison."

    all_ids = set()
    for m in loaded.values():
        all_ids |= set(m.keys())
    common_ids = all_ids
    for m in loaded.values():
        common_ids &= set(m.keys())
    common_ids = sorted(common_ids)

    # Filter out failed cases (iterations_used=0)
    valid_ids = []
    for idx in common_ids:
        all_ok = True
        for m in loaded.values():
            vb = m[idx].get('vikingbot', {})
            if (vb.get('iterations_used', 0) or 0) == 0:
                all_ok = False
                break
        if all_ok:
            valid_ids.append(idx)

    md = []
    md.append("# Ablation Comparison Summary")
    md.append("")
    md.append(f"Common valid samples: {len(valid_ids)} (filtered from {len(common_ids)})")
    md.append("")

    headers = ["Metric"] + list(loaded.keys())
    md.append("| " + " | ".join(headers) + " |")
    md.append("|---" + "|---:" * len(loaded) + "|")

    metrics = {}
    for name, m in loaded.items():
        iters = [m[i].get('vikingbot', {}).get('iterations_used', 0) or 0 for i in valid_ids]
        toks = [m[i].get('vikingbot', {}).get('token_usage', {}).get('total_tokens', 0) or 0 for i in valid_ids]
        search_iters = [m[i].get('vikingbot', {}).get('search_iterations', 0) or 0 for i in valid_ids]
        relations_hits = [m[i].get('vikingbot', {}).get('relations_hits', 0) or 0 for i in valid_ids]
        total_relations = [m[i].get('vikingbot', {}).get('total_relations_found', 0) or 0 for i in valid_ids]
        scores = []
        for i in valid_ids:
            s = m[i].get('llm_evaluation', {}).get('normalized_score')
            if s is not None:
                scores.append(s)

        tc_list = [parse_tool_calls(m[i].get('vikingbot', {}).get('tool_calls', '')) for i in valid_ids]
        durs = [extract_tool_durations(tc)[0] for tc in tc_list]
        tool_counts = [len(tc) for tc in tc_list]

        metrics[name] = {
            'avg_iters': avg(iters),
            'avg_tokens': avg(toks),
            'avg_score': avg(scores) if scores else 0,
            'avg_tool_time': avg(durs),
            'avg_tool_calls': avg(tool_counts),
            'avg_search_iters': avg(search_iters),
            'relations_hit_rate': sum(1 for h in relations_hits if h > 0) / len(relations_hits) if relations_hits else 0,
            'avg_relations_found': avg(total_relations),
        }

    rows = [
        ("Avg iterations", 'avg_iters', '.2f'),
        ("Avg search iterations", 'avg_search_iters', '.2f'),
        ("Avg total tokens", 'avg_tokens', '.0f'),
        ("Avg score", 'avg_score', '.2f'),
        ("Avg tool time (s)", 'avg_tool_time', '.1f'),
        ("Avg tool calls", 'avg_tool_calls', '.1f'),
        ("Relations hit rate", 'relations_hit_rate', '.2%'),
        ("Avg relations found", 'avg_relations_found', '.2f'),
    ]
    for label, key, fmt in rows:
        cells = [label]
        for name in loaded:
            cells.append(f"{metrics[name][key]:{fmt}}")
        md.append("| " + " | ".join(cells) + " |")

    md.append("")
    return '\n'.join(md)


def main_ablation():
    """Run multi-group ablation comparison.

    Usage:
        python compare_link_effect.py --ablation                          # use ABLATION_GROUPS defaults
        python compare_link_effect.py --ablation baseline=path1 keyword=path2  # custom groups
        python compare_link_effect.py --ablation path1 path2             # auto-name from directory
    """
    groups = {}
    args = [a for a in sys.argv[1:] if a != "--ablation"]

    if not args:
        groups = ABLATION_GROUPS
    elif '=' in args[0]:
        for arg in args:
            name, path = arg.split('=', 1)
            groups[name] = path
    else:
        for path in args:
            name = os.path.basename(os.path.dirname(path))
            groups[name] = path

    report = build_ablation_summary(groups)

    dataset = "ablation"
    for path in groups.values():
        parts = path.replace("\\", "/").split("/")
        for i, p in enumerate(parts):
            if p == "Output" and i + 1 < len(parts):
                dataset = parts[i + 1]
                break
        break

    output_path = os.path.join(SCRIPT_DIR, f"report_ablation_{dataset}.md")
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"Ablation report written to: {output_path}")
    print(report)


if __name__ == "__main__":
    if "--ablation" in sys.argv:
        sys.argv.remove("--ablation")
        main_ablation()
    else:
        main()
