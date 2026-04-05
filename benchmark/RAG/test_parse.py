#!/usr/bin/env python3
import json
from typing import Dict, Any, Optional

def _extract_json_payload(output: str) -> Optional[Dict[str, Any]]:
    if not output:
        return None
    
    # 清理输出，移除 "Loading config file" 等日志行
    lines = output.split('\n')
    cleaned_lines = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith('Loading config file:'):
            cleaned_lines.append(line)
    output = '\n'.join(cleaned_lines)
    
    # 方法：优先尝试最后一个 {，因为 VikingBot 的最终输出通常在最后
    last_brace_idx = output.rfind('{')
    if last_brace_idx != -1:
        # 从最后一个 { 开始，找到匹配的 }
        brace_count = 0
        end_idx = -1
        in_string = False
        escape_next = False
        
        for i in range(last_brace_idx, len(output)):
            char = output[i]
            
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
        
        if end_idx != -1:
            json_str = output[last_brace_idx:end_idx]
            try:
                obj = json.loads(json_str, strict=False)
                if isinstance(obj, dict) and "text" in obj:
                    return obj
            except Exception as e:
                print(f"解析错误: {e}")
                print(f"尝试解析的字符串: {repr(json_str[:200])}...")
    
    # 如果最后一个 { 解析失败，再尝试所有可能的 {
    idx = 0
    while True:
        idx = output.find('{', idx)
        if idx == -1:
            break
        
        # 从这个 { 开始，找到匹配的 }
        brace_count = 0
        end_idx = -1
        in_string = False
        escape_next = False
        
        for i in range(idx, len(output)):
            char = output[i]
            
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
        
        if end_idx != -1:
            json_str = output[idx:end_idx]
            try:
                obj = json.loads(json_str, strict=False)
                if isinstance(obj, dict) and "text" in obj:
                    return obj
            except Exception as e:
                print(f"解析错误 (idx={idx}): {e}")
                pass
        
        idx += 1
    
    return None

# 从 sample.json 读取测试输入
with open("/Users/bytedance/PR/sample.json", 'r', encoding='utf-8') as f:
    test_output = f.read()

print("=== 测试输入 ===")
print(repr(test_output))
print("\n=== 开始解析 ===")

result = _extract_json_payload(test_output)

print("\n=== 解析结果 ===")
if result:
    print("解析成功！")
    print(f"text: {result.get('text', '')[:100]}...")
    print(f"iteration: {result.get('iteration', 0)}")
    print(f"tools_used: {len(result.get('tools_used', []))}")
else:
    print("解析失败！")
