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
        return [["flutter", "analyze", "--no-fatal-infos", "--no-fatal-warnings"], ["flutter", "test"]]
    return [[flutter, "analyze", "--no-fatal-infos", "--no-fatal-warnings"], [flutter, "test"]]


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
    """Run independent verification and return command-level diagnostics."""
    commands = detect(repo)
    if not commands:
        return {
            "status": "unavailable",
            "output": "No supported test/build command could be reliably detected in the worktree.",
            "command": None,
            "verification": [],
            "analyzer_errors": None,
            "analyzer_warnings": None,
            "analyzer_infos": None,
        }

    verification: list[dict] = []
    analyzer_counts = {"errors": None, "warnings": None, "infos": None}

    for cmd in commands:
        started = __import__("time").monotonic()
        code, output = _run(repo, cmd)
        duration = round(__import__("time").monotonic() - started, 3)

        executable = Path(cmd[0]).name.lower() if cmd else ""
        is_flutter_analyze = "analyze" in cmd and executable in {"flutter", "flutter.bat", "flutter.cmd"}

        if is_flutter_analyze:
            parsed = _parse_flutter_analyze(output)
            analyzer_counts = {
                "errors": parsed["errors"],
                "warnings": parsed["warnings"],
                "infos": parsed["infos"],
            }
            passed = parsed["errors"] == 0 and code == 0
            verification.append({
                "name": "flutter analyze",
                "command": cmd,
                "status": "passed" if passed else "failed",
                "exit_code": code,
                "duration_s": duration,
                "errors": parsed["errors"],
                "warnings": parsed["warnings"],
                "infos": parsed["infos"],
                "output": output,
            })
            if not passed:
                break
            continue

        name = _verification_name(cmd)
        if code == 127:
            verification.append({
                "name": name,
                "command": cmd,
                "status": "unavailable",
                "exit_code": code,
                "duration_s": duration,
                "output": output,
            })
            break

        passed = code == 0
        verification.append({
            "name": name,
            "command": cmd,
            "status": "passed" if passed else "failed",
            "exit_code": code,
            "duration_s": duration,
            "output": output,
        })
        if not passed:
            break

    overall = "passed"
    if any(item["status"] == "failed" for item in verification):
        overall = "failed"
    elif any(item["status"] == "unavailable" for item in verification):
        overall = "unavailable"

    return {
        "status": overall,
        "output": _format_verification_report(verification),
        "command": [item["command"] for item in verification],
        "verification": verification,
        "analyzer_errors": analyzer_counts["errors"],
        "analyzer_warnings": analyzer_counts["warnings"],
        "analyzer_infos": analyzer_counts["infos"],
    }


def _verification_name(cmd: list[str]) -> str:
    joined = " ".join(cmd).lower()
    if "flutter" in joined and "test" in joined:
        return "flutter test"
    if "flutter" in joined and "build" in joined:
        return "flutter build"
    if "gradle" in joined:
        return "gradle test"
    if "mvn" in joined:
        return "maven test"
    if "npm" in joined:
        return "npm test"
    if "pytest" in joined:
        return "pytest"
    return "verification command"


def _format_verification_report(items: list[dict]) -> str:
    lines = ["Verification", "────────────────────────"]
    for item in items:
        status = item["status"].upper()
        marker = "PASS" if status == "PASSED" else "FAIL" if status == "FAILED" else "SKIP"
        lines.append(f"[{marker}] {item['name']}")
        lines.append(f"       exit code: {item['exit_code']}")
        lines.append(f"       duration: {item['duration_s']}s")

        if item["name"] == "flutter analyze":
            lines.append(f"       errors: {item['errors']}")
            lines.append(f"       warnings: {item['warnings']}")
            lines.append(f"       infos: {item['infos']}")

        if status != "PASSED":
            lines.append("")
            lines.append("Failure:" if status == "FAILED" else "Unavailable:")
            output = (item.get("output") or "").strip()
            lines.append(output if output else "(no output)")

    return "\n".join(lines)


