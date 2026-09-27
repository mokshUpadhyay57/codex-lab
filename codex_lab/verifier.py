from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _run(cwd: Path, cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            shell=False,
        )
        return p.returncode, p.stdout
    except FileNotFoundError:
        return 127, f"VERIFICATION COMMAND NOT FOUND: {' '.join(cmd)}\n"
    except subprocess.TimeoutExpired as e:
        output = e.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return 124, output + "\nVERIFICATION TIMEOUT\n"
    except OSError as e:
        return 126, f"VERIFICATION COULD NOT START: {e}\n"


def _flutter_commands(repo: Path) -> list[list[str]] | None:
    if not (repo / "pubspec.yaml").exists():
        return None
    flutter = shutil.which("flutter")
    if not flutter:
        return [["flutter", "analyze"], ["flutter", "test"]]
    return [[flutter, "analyze"], [flutter, "test"]]


def _single_project_command(repo: Path) -> list[str] | None:
    if os.name == "nt":
        if (repo / "gradlew.bat").exists():
            return ["cmd.exe", "/d", "/c", str(repo / "gradlew.bat"), "test"]
        if (repo / "mvnw.cmd").exists():
            return ["cmd.exe", "/d", "/c", str(repo / "mvnw.cmd"), "test"]
    else:
        if (repo / "gradlew").exists():
            return ["./gradlew", "test"]
        if (repo / "mvnw").exists():
            return ["./mvnw", "test"]

    if (repo / "pom.xml").exists() and shutil.which("mvn"):
        return ["mvn", "test"]
    if (repo / "package.json").exists() and shutil.which("npm"):
        return ["npm", "test"]
    if (repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists() or (repo / "tests").is_dir():
        python = shutil.which("python") or shutil.which("python3")
        if python:
            return [python, "-m", "pytest"]
    return None


def detect(repo: Path) -> list[list[str]]:
    flutter = _flutter_commands(repo)
    if flutter:
        return flutter
    command = _single_project_command(repo)
    return [command] if command else []


def verify(repo: Path) -> dict:
    commands = detect(repo)
    if not commands:
        return {
            "status": "unavailable",
            "output": "No supported test/build command could be reliably detected in the worktree.",
            "command": None,
        }

    outputs: list[str] = []
    executed: list[list[str]] = []
    for cmd in commands:
        executed.append(cmd)
        code, output = _run(repo, cmd)
        outputs.append(f"$ {' '.join(cmd)}\n{output}")
        if code == 127:
            return {
                "status": "unavailable",
                "output": "\n".join(outputs) + "\nRequired verification tool is unavailable.",
                "command": executed,
            }
        if code != 0:
            return {
                "status": "failed",
                "output": "\n".join(outputs),
                "command": executed,
            }

    return {
        "status": "passed",
        "output": "\n".join(outputs),
        "command": executed,
    }
