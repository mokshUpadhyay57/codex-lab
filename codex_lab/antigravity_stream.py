"""Antigravity CLI streaming driver.

Keeps one agy process alive and sends the initial prompt through stdin.
This uses Antigravity's documented stream-json input/output interface.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Iterator


def start(worktree: str, prompt: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    proc = subprocess.Popen(
        ["agy", "--input-format", "stream-json", "--output-format", "stream-json"],
        cwd=worktree,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
    )
    if proc.stdin is None:
        raise RuntimeError("Antigravity stdin was not available")
    proc.stdin.write(json.dumps({
        "event": "user",
        "message": {"content": prompt},
    }) + "\n")
    proc.stdin.flush()
    return proc


def events(proc: subprocess.Popen[str]) -> Iterator[dict]:
    if proc.stdout is None:
        return
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue
