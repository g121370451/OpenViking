#!/usr/bin/env python3
import os
import sys
import subprocess
import json
from pathlib import Path

sys.path.append(str(Path(__file__).parent / "src"))

from core.logger import get_logger

logger = get_logger()

def main():
    # 使用和正常运行相同的配置
    ov_conf_path = str((Path(__file__).parent / "ov.conf").resolve())
    
    # 构建环境
    env = os.environ.copy()
    env["OPENVIKING_CONFIG_FILE"] = ov_conf_path
    env["NANOBOT_AGENTS__MAX_ITERATIONS"] = "10"
    
    # 设置 ovcli.conf 路径
    original_ov_conf_dir = os.path.dirname(ov_conf_path)
    ovcli_conf_path = os.path.join(original_ov_conf_dir, "ovcli.conf")
    if os.path.exists(ovcli_conf_path):
        env["OPENVIKING_CLI_CONFIG_FILE"] = ovcli_conf_path
        print(f"Set OPENVIKING_CLI_CONFIG_FILE to: {ovcli_conf_path}")
    
    # 简单的测试问题
    input_msg = """Answer this question as briefly as possible. Use only the information available in the database. Do not use web search or any external source. Always search in viking://resources/ path.

Question: Were Scott Derrickson and Ed Wood of the same nationality?"""
    
    cmd = ["vikingbot", "chat", "-m", input_msg, "-e", "-c", ov_conf_path]
    print(f"\nRunning command: {' '.join(cmd)}")
    print(f"\n=== VikingBot FULL OUTPUT START ===")
    
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
    )
    
    print(f"STDOUT:\n{repr(result.stdout)}")
    print(f"\nSTDERR:\n{repr(result.stderr)}")
    print(f"\n=== VikingBot FULL OUTPUT END ===")
    
    # 也保存到文件
    output_file = Path(__file__).parent / "vikingbot_full_output.txt"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("=== STDOUT ===\n")
        f.write(result.stdout)
        f.write("\n=== STDERR ===\n")
        f.write(result.stderr)
    
    print(f"\nFull output saved to: {output_file}")

if __name__ == "__main__":
    main()
