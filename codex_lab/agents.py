from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class AgentUnavailable(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_windows_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


@dataclass(frozen=True)
class AgentInfo:
    name: str
    executable: str
    version: str
    help_text: str


class AgentAdapter:
    name = "agent"

    def prepare_workspace(self, worktree: Path) -> None:
        """Perform agent-specific workspace setup before launching the agent."""
        return None

    def inspect(self) -> AgentInfo:
        raise NotImplementedError

    def launch_args(self, info: AgentInfo, prompt: str) -> list[str]:
        raise NotImplementedError

    def collect_telemetry(self, before: dict[str, int], prompt: str) -> dict:
        return {
            "model": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "session_files": [],
            "human_messages": [],
            "usage_status": "unavailable",
            "intervention_status": "unavailable",
            "cost": None,
            "cost_status": "unavailable",
        }

    def snapshot(self) -> dict[str, int]:
        return {}

    def run(self, worktree: Path, prompt: str) -> dict:
        info = self.inspect()
        before = self.snapshot()
        started = time.monotonic()
        started_at = utc_now()
        args = self.launch_args(info, prompt)
        print(f"[{self.name}] starting interactive session in {worktree}")
        try:
            if self.name == "antigravity":
                returncode = _run_antigravity_interactive(info, worktree, prompt)
            else:
                p = subprocess.run(args, cwd=worktree)
                returncode = p.returncode
        except FileNotFoundError as exc:
            raise AgentUnavailable(f"{self.name} executable could not be started: {info.executable}") from exc
        telemetry = self.collect_telemetry(before, prompt)
        return {
            "returncode": returncode,
            "started_at": started_at,
            "ended_at": utc_now(),
            "duration_s": round(time.monotonic() - started, 3),
            **telemetry,
        }


def _inspect(executable_name: str) -> AgentInfo:
    exe = shutil.which(executable_name)
    if not exe:
        raise AgentUnavailable(f"{executable_name} executable not found on PATH")
    version = subprocess.run([exe, "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.strip()
    help_out = subprocess.run([exe, "--help"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout
    return AgentInfo(executable_name, exe, version, help_out)


class CodexAdapter(AgentAdapter):
    name = "codex"

    def inspect(self) -> AgentInfo:
        return _inspect("codex")

    def launch_args(self, info: AgentInfo, prompt: str) -> list[str]:
        args = [info.executable]
        if os.name == "nt" and _is_windows_admin() and "--no-daemon" in info.help_text:
            args.append("--no-daemon")
        args.append(prompt)
        return args

    def _rollouts(self) -> list[Path]:
        root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
        if not root.exists():
            return []
        return sorted(root.glob("*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime_ns)

    def snapshot(self) -> dict[str, int]:
        return {str(p): p.stat().st_mtime_ns for p in self._rollouts()}

    def collect_telemetry(self, before: dict[str, int], prompt: str) -> dict:
        candidates = []
        for p in self._rollouts():
            try:
                mtime = p.stat().st_mtime_ns
            except OSError:
                continue
            if str(p) not in before or mtime > before[str(p)]:
                candidates.append(p)

        model = None
        input_tokens = output_tokens = total_tokens = cached = None
        humans = []
        expected = " ".join(prompt.split())
        initial_consumed = False
        for path in candidates:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "session_meta":
                    model = (record.get("payload") or {}).get("model") or model
                if record.get("type") != "event_msg":
                    continue
                payload = record.get("payload") or {}
                typ = payload.get("type")
                if typ == "token_count":
                    usage = (payload.get("info") or {}).get("total_token_usage") or {}
                    input_tokens = usage.get("input_tokens", input_tokens)
                    output_tokens = usage.get("output_tokens", output_tokens)
                    total_tokens = usage.get("total_tokens", total_tokens)
                    cached = usage.get("cached_input_tokens", cached)
                elif typ == "user_message":
                    message = str(payload.get("message") or "")
                    if not initial_consumed and expected and " ".join(message.split()) == expected:
                        initial_consumed = True
                        continue
                    if not initial_consumed and not expected:
                        initial_consumed = True
                        continue
                    humans.append({"at": record.get("timestamp"), "message": message, "type": "human_input"})
        return {
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cached_input_tokens": cached,
            "session_files": [str(p) for p in candidates],
            "human_messages": humans,
            "usage_status": "available" if total_tokens is not None else "unavailable",
            "intervention_status": "available",
            "cost": None,
            "cost_status": "unavailable",
        }


class ClaudeAdapter(AgentAdapter):
    name = "claude"

    def inspect(self) -> AgentInfo:
        return _inspect("claude")

    def launch_args(self, info: AgentInfo, prompt: str) -> list[str]:
        # Anthropic documents `claude "query"` as starting the interactive REPL.
        return [info.executable, prompt]

def _run_antigravity_interactive(info: AgentInfo, worktree: Path, prompt: str) -> int:
    """Run agy in a real terminal and inject the initial prompt.

    Antigravity's normal `agy` mode is a TUI. A normal subprocess pipe is not
    sufficient because the TUI expects a terminal/console. On Windows use
    pywinpty (ConPTY) so codex-lab can type the initial prompt while the user
    retains the live interactive TUI for subsequent interventions.
    """
    if os.name != "nt":
        # On POSIX, keep a normal attached terminal; stdin/stdout remain interactive.
        p = subprocess.Popen([info.executable], cwd=worktree)
        return p.wait()

    try:
        from winpty import PtyProcess
    except ImportError as exc:
        raise AgentUnavailable(
            "Antigravity interactive prompt injection on Windows requires "
            "the 'pywinpty' package. Reinstall codex-lab with its dependencies."
        ) from exc

    proc = PtyProcess.spawn([info.executable], cwd=str(worktree))
    sent = False
    buffer = ""
    deadline = time.monotonic() + 15.0

    # Wait for the TUI prompt marker, then type the initial prompt + Enter.
    # Keep forwarding terminal output so the user sees the normal Antigravity UI.
    while proc.isalive():
        try:
            chunk = proc.read(4096)
        except EOFError:
            break
        if chunk:
            print(chunk, end="", flush=True)
            if not sent:
                buffer += chunk
                # The prompt box is rendered with a leading ">" by the TUI.
                if "\n>" in buffer or buffer.rstrip().endswith(">") or time.monotonic() > deadline:
                    proc.write(prompt + "\r")
                    sent = True
        elif not sent and time.monotonic() > deadline:
            proc.write(prompt + "\r")
            sent = True
        time.sleep(0.02)

    return proc.exitstatus if proc.exitstatus is not None else 0


class AntigravityAdapter(AgentAdapter):
    name = "antigravity"

    def inspect(self) -> AgentInfo:
        return _inspect("agy")

    def prepare_workspace(self, worktree: Path) -> None:
        """Persist this exact experiment workspace in Antigravity's trust list.

        Antigravity CLI stores its Windows workspace settings in
        ~/.gemini/antigravity-cli/settings.json. We only add the exact worktree
        to the existing `trustedWorkspaces` array; all other settings are kept.
        """
        settings_path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        if settings_path.exists():
            try:
                raw = settings_path.read_text(encoding="utf-8")
                settings = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                raise AgentUnavailable(
                    f"could not read Antigravity settings {settings_path}: {exc}"
                ) from exc
            if not isinstance(settings, dict):
                raise AgentUnavailable(f"Antigravity settings must be a JSON object: {settings_path}")
        else:
            settings = {}

        trusted = settings.get("trustedWorkspaces")
        if trusted is None:
            trusted = []
        if not isinstance(trusted, list):
            raise AgentUnavailable(
                f"Antigravity settings 'trustedWorkspaces' must be an array: {settings_path}"
            )

        workspace = str(worktree.resolve())
        # Compare normalized paths so slash/case differences do not create duplicates on Windows.
        def normalize(path: object) -> str:
            return os.path.normcase(os.path.normpath(str(path)))

        if not any(normalize(item) == normalize(workspace) for item in trusted):
            trusted.append(workspace)
            settings["trustedWorkspaces"] = trusted
            temp = settings_path.with_suffix(".json.tmp")
            try:
                temp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                os.replace(temp, settings_path)
            except OSError as exc:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise AgentUnavailable(
                    f"could not update Antigravity settings {settings_path}: {exc}"
                ) from exc

        print(f"[antigravity] trusted workspace: {workspace}")

    def launch_args(self, info: AgentInfo, prompt: str) -> list[str]:
        # `agy` launches the documented interactive TUI. Its documented -p mode is
        # headless, so do not silently use it: the experiment requires interaction.
        return [info.executable]


ADAPTERS = {
    "codex": CodexAdapter,
    "claude": ClaudeAdapter,
    "antigravity": AntigravityAdapter,
}


def get_adapter(name: str) -> AgentAdapter:
    try:
        return ADAPTERS[name]()
    except KeyError as exc:
        raise AgentUnavailable(f"unsupported agent: {name}; choose from {', '.join(ADAPTERS)}") from exc


def inspect_all() -> dict:
    result = {}
    for name, cls in ADAPTERS.items():
        try:
            info = cls().inspect()
            result[name] = {"available": True, "path": info.executable, "version": info.version}
        except AgentUnavailable as exc:
            result[name] = {"available": False, "reason": str(exc)}
    return result
