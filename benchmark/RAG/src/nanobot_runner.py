#!/usr/bin/env python3

import os
import re
import time
import json
import uuid
import hashlib
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional

sys_path = str(Path(__file__).parent)
import sys
if sys_path not in sys.path:
    sys.path.append(sys_path)

from core.logger import get_logger

logger = get_logger()

_NANOBOT_CONFIG_PATH = str((Path(__file__).parent.parent / "nanobot_config.json").resolve())
_WORKSPACE_PLACEHOLDER = "PLACEHOLDER_WORKSPACE"
_WORKER_SCRIPT = str((Path(__file__).parent / "nanobot_query_worker.py").resolve())


def _generate_temp_nanobot_config(
    template_config_path: str,
    workspace_path: str,
) -> str:
    with open(template_config_path, 'r', encoding='utf-8') as f:
        config_str = f.read()

    config_str = config_str.replace(_WORKSPACE_PLACEHOLDER, workspace_path)

    temp_dir = Path(__file__).parent.parent / ".temp"
    temp_dir.mkdir(exist_ok=True)

    path_hash = hashlib.md5(workspace_path.encode('utf-8')).hexdigest()
    temp_path = str(temp_dir / f"nanobot_{path_hash}.json")

    with open(temp_path, 'w', encoding='utf-8') as f:
        f.write(config_str)

    return temp_path


class NanobotRunner:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.nanobot_config = config.get('nanobot', {})
        self.max_iterations = self.nanobot_config.get('max_iterations', 50)
        self.doc_output_dir = config.get('paths', {}).get('doc_output_dir')
        self.nanobot_config_path = self.nanobot_config.get('config_path', _NANOBOT_CONFIG_PATH)
        self.timeout = self.nanobot_config.get('timeout', 600)

    def _resolve_nanobot_config_path(self) -> str:
        p = Path(self.nanobot_config_path)
        if not p.is_absolute():
            p = Path(__file__).parent.parent / p
        template_path = str(p.resolve())

        if self.doc_output_dir:
            return _generate_temp_nanobot_config(template_path, self.doc_output_dir)

        return template_path

    def generate_answer(
        self,
        question: str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        session_id = session_id or f"eval_{uuid.uuid4().hex}"
        start_time = time.time()

        try:
            input_msg = (
                "Answer this question as briefly as possible. "
                "Use only the information available in the local files. "
                "Do not use web search or any external source. "
                f"\n\nQuestion: {question}"
            )

            resolved_config = self._resolve_nanobot_config_path()

            env = os.environ.copy()
            api_key = self.config.get('llm', {}).get('api_key')
            if api_key:
                env['OPENAI_API_KEY'] = api_key

            cmd = [
                sys.executable,
                _WORKER_SCRIPT,
                "--question", input_msg,
                "--config-path", resolved_config,
                "--session-id", session_id,
                "--timeout", str(self.timeout),
            ]

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            try:
                stdout, stderr = proc.communicate(timeout=self.timeout + 30)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                raise subprocess.TimeoutExpired(cmd, self.timeout)

            if proc.returncode != 0:
                err_msg = (stderr or "").strip()[:500]
                raise RuntimeError(f"Worker exited with code {proc.returncode}: {err_msg}")

            stdout = (stdout or "").strip()
            if not stdout:
                raise RuntimeError("Worker returned empty output")

            result = json.loads(stdout)

            logger.info(f"Nanobot answer generated in {result.get('total_time_sec', 0):.2f}s")
            return result

        except Exception as e:
            logger.error(f"Error generating answer with Nanobot: {e}")
            return {
                "answer": f"[ERROR] {str(e)}",
                "total_time_sec": time.time() - start_time,
                "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "tools_used_names": [],
                "iterations_used": 0,
                "session_id": session_id,
            }


def stop_nanobot_server() -> None:
    pass


def run_nanobot_query(
    question: str,
    config: Dict[str, Any],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    runner = NanobotRunner(config)
    return runner.generate_answer(question, session_id)
