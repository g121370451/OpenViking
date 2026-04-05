#!/usr/bin/env python3
import json
import re
from pathlib import Path

def _extract_json_payload(output):
    """
    专门解析 VikingBot 输出的解析器 - 简单提取我们需要的字段
    """
    if not output:
        return None
    
    # 第一步：找到第一个 { 的位置
    first_brace_idx = output.find('{')
    if first_brace_idx == -1:
        return None
    
    # 截取从第一个 { 开始
    content = output[first_brace_idx:]
    
    # 第二步：使用括号匹配找到完整的对象边界
    brace_count = 0
    end_idx = -1
    in_string = False
    escape_next = False
    
    for i in range(len(content)):
        char = content[i]
        
        if escape_next:
            escape_next = False
            continue
        
        if char == '\\':
            escape_next = True
            continue
        
        if char == '"' and not escape_next:
            in_string = not in_string
            continue
        
        if not in_string:
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    end_idx = i + 1
                    break
    
    if end_idx == -1:
        return None
    
    obj_str = content[:end_idx]
    
    result = {}
    
    # 第三步：提取我们需要的字段
    
    # 提取 text
    # 找到 "text": 
    text_start = obj_str.find('"text":')
    if text_start != -1:
        # 从 text_start + len('"text":') 开始
        idx = text_start + len('"text":')
        # 跳过空白
        while idx < len(obj_str) and obj_str[idx] in ' \t\n\r':
            idx +=1
        if idx < len(obj_str) and obj_str[idx] == '"':
            # 找到对应的 "
            idx +=1
            start = idx
            in_str = True
            escape = False
            while idx < len(obj_str):
                c = obj_str[idx]
                if escape:
                    escape = False
                elif c == '\\':
                    escape = True
                elif c == '"':
                    break
                idx +=1
            if idx < len(obj_str):
                result['text'] = obj_str[start:idx]
    
    # 提取 token_usage
    tu_match = re.search(r'"token_usage"\s*:\s*(\{[^}]*\})', obj_str)
    if tu_match:
        tu_str = tu_match.group(1)
        # 提取里面的数字
        prompt_tokens = re.search(r'"prompt_tokens"\s*:\s*(\d+)', tu_str)
        completion_tokens = re.search(r'"completion_tokens"\s*:\s*(\d+)', tu_str)
        total_tokens = re.search(r'"total_tokens"\s*:\s*(\d+)', tu_str)
        result['token_usage'] = {
            'prompt_tokens': int(prompt_tokens.group(1)) if prompt_tokens else 0,
            'completion_tokens': int(completion_tokens.group(1)) if completion_tokens else 0,
            'total_tokens': int(total_tokens.group(1)) if total_tokens else 0
        }
    
    # 提取 time_cost
    tc_match = re.search(r'"time_cost"\s*:\s*([\d.]+)', obj_str)
    if tc_match:
        result['time_cost'] = float(tc_match.group(1))
    
    # 提取 iteration
    it_match = re.search(r'"iteration"\s*:\s*(\d+)', obj_str)
    if it_match:
        result['iteration'] = int(it_match.group(1))
    
    # 提取 tools_used_names
    tun_match = re.search(r'"tools_used_names"\s*:\s*(\[[^\]]*\])', obj_str)
    if tun_match:
        tun_str = tun_match.group(1)
        # 提取里面的字符串
        names = re.findall(r'"([^"]+)"', tun_str)
        result['tools_used_names'] = names
    
    # 提取 tools_used
    tu_match = re.search(r'"tools_used"\s*:\s*(\[[^\]]*\])', obj_str)
    if tu_match:
        result['tools_used'] = tu_match.group(1)
    
    print(f"✓ 解析到字段: {list(result.keys())}")
    
    return result

# 读取 sample.txt
with open("/Users/bytedance/PR/sample.txt", 'r', encoding='utf-8') as f:
    output = f.read()

print("=== 开始解析 sample.txt ===")
print(f"总长度: {len(output)} 字符\n")

result = _extract_json_payload(output)

if result:
    print(f"\n=== 解析结果 ===")
    print(f"text: {result.get('text', '')[:100]}...")
    print(f"iteration: {result.get('iteration', 0)}")
    print(f"✓ 测试通过！")
else:
    print("\n=== 解析失败 ===")
