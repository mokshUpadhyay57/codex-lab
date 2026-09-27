from __future__ import annotations

import os
import shutil
import re
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


def _parse_flutter_analyze(output: str) -> dict[str, int]:
    """Count Flutter analyzer diagnostics by severity.

    Flutter emits diagnostics in the form:
      info - message - file:line:column - rule
      warning - message - file:line:column - rule
      error - message - file:line:column - rule

    The summary (for example, ``128 issues found``) is deliberately not used
    for pass/fail because it mixes warnings and informational diagnostics.
    """
    counts = {"errors": 0, "warnings": 0, "infos": 0}
    pattern = re.compile(r"^\s*(error|warning|info)\s+-\s+", re.IGNORECASE)
    for line in output.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        severity = match.group(1).lower()
        if severity == "error":
            counts["errors"] += 1
        elif severity == "warning":
            counts["warnings"] += 1
        else:
            counts["infos"] += 1
    return counts


def verify(repo: Path) -> dict:
    commands = detect(repo)
    if not commands:
        return {
            "status": "unavailable",
            "output": "No supported test/build command could be reliably detected in the worktree.",
            "command": None,
            "analyzer_errors": None,
            "analyzer_warnings": None,
            "analyzer_infos": None,
        }

    outputs: list[str] = []
    executed: list[list[str]] = []
    analyzer_counts = {"errors": None, "warnings": None, "infos": None}
    for index, cmd in enumerate(commands):
        executed.append(cmd)
        code, output = _run(repo, cmd)
        outputs.append(f"$ {' '.join(cmd)}\n{output}")

        is_flutter_analyze = (
            len(cmd) >= 2 and cmd[-2:] == ["flutter", "analyze"]
        ) or cmd[-1:] == ["analyze"]
        if is_flutter_analyze:
            parsed = _parse_flutter_analyze(output)
            analyzer_counts = {
                "errors": parsed["errors"],
                "warnings": parsed["warnings"],
                "infos": parsed["infos"],
            }
            # Analyzer diagnostics, not the aggregate "N issues found" count,
            # determine static-analysis failure. Warnings/info are recorded but
            # do not fail the run. A non-zero exit with no analyzer errors is
            # still a verifier/tool failure and therefore remains a failure.
            if parsed["errors"] > 0 or code != 0:
                return {
                    "status": "failed",
                    "output": "\n".join(outputs),
                    "command": executed,
                    "analyzer_errors": parsed["errors"],
                    "analyzer_warnings": parsed["warnings"],
                    "analyzer_infos": parsed["infos"],
                }
            continue

        if code == 127:
            return {
                "status": "unavailable",
                "output": "\n".join(outputs) + "\nRequired verification tool is unavailable.",
                "command": executed,
                "analyzer_errors": analyzer_counts["errors"],
                "analyzer_warnings": analyzer_counts["warnings"],
                "analyzer_infos": analyzer_counts["infos"],
            }
        if code != 0:
            return {
                "status": "failed",
                "output": "\n".join(outputs),
                "command": executed,
                "analyzer_errors": analyzer_counts["errors"],
                "analyzer_warnings": analyzer_counts["warnings"],
                "analyzer_infos": analyzer_counts["infos"],
            }

    return {
        "status": "passed",
        "output": "\n".join(outputs),
        "command": executed,
        "analyzer_errors": analyzer_counts["errors"],
        "analyzer_warnings": analyzer_counts["warnings"],
        "analyzer_infos": analyzer_counts["infos"],
    }
