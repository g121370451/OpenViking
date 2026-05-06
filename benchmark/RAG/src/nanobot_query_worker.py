#!/usr/bin/env python3
"""Worker script for running a single nanobot query in an isolated subprocess.

Called by nanobot_runner.py via subprocess.Popen.
Outputs JSON result to stdout.

Usage:
    python nanobot_query_worker.py --question "..." --config-path "..." --session-id "..."
"""

import argparse
import asyncio
import json
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", required=True)
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    from nanobot.nanobot import Nanobot

    start_time = time.time()

    try:
        bot = Nanobot.from_config(config_path=args.config_path)

        async def _run():
            try:
                result = await asyncio.wait_for(
                    bot.run(args.question, session_key=args.session_id),
                    timeout=args.timeout,
                )
            finally:
                try:
                    await bot._loop.close_mcp()
                except Exception:
                    pass
            return result

        response = asyncio.run(_run())

        total_time_sec = round(time.time() - start_time, 2)

        answer = response.content if response else ""

        usage = dict(bot._loop._last_usage) if hasattr(bot._loop, '_last_usage') else {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

        iteration = bot._loop._last_iteration if hasattr(bot._loop, '_last_iteration') else 0
        tools_used_names = bot._loop._last_tools_used if hasattr(bot._loop, '_last_tools_used') and bot._loop._last_tools_used else []

        output = {
            "answer": answer,
            "total_time_sec": total_time_sec,
            "token_usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            },
            "tools_used_names": tools_used_names,
            "iterations_used": iteration,
            "session_id": args.session_id,
        }

    except Exception as e:
        total_time_sec = round(time.time() - start_time, 2)
        output = {
            "answer": f"[ERROR] {str(e)}",
            "total_time_sec": total_time_sec,
            "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "tools_used_names": [],
            "iterations_used": 0,
            "session_id": args.session_id,
            "error": str(e),
        }

    json_str = json.dumps(output, ensure_ascii=False)
    sys.stdout.write(json_str + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
