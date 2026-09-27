from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
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
                stream = _run_antigravity_stream(info, worktree, prompt)
                returncode = stream["returncode"]
            else:
                stream = {}
                p = subprocess.run(args, cwd=worktree)
                returncode = p.returncode
        except FileNotFoundError as exc:
            raise AgentUnavailable(f"{self.name} executable could not be started: {info.executable}") from exc
        telemetry = self.collect_telemetry(before, prompt)
        if self.name == "antigravity":
            telemetry.update({
                "model": stream.get("model"),
                "input_tokens": stream.get("input_tokens"),
                "output_tokens": stream.get("output_tokens"),
                "total_tokens": stream.get("total_tokens"),
                "cached_input_tokens": stream.get("cached_input_tokens"),
                "session_files": [],
                "human_messages": [],
                "usage_status": "available" if stream.get("total_tokens") is not None else "unavailable",
                "intervention_status": "not_applicable_automated",
                "cost": None,
                "cost_status": "unavailable",
                "result_status": stream.get("result_status"),
                "result_response": stream.get("result_response"),
                "result_error": stream.get("result_error"),
                "conversation_id": stream.get("conversation_id"),
                "stream_events": stream.get("stream_events", 0),
                "tool_events": stream.get("tool_events", 0),
            })
        return {
            "returncode": returncode,
            "started_at": started_at,
            "ended_at": utc_now(),
            "duration_s": stream.get("duration_s", round(time.monotonic() - started, 3)) if self.name == "antigravity" else round(time.monotonic() - started, 3),
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

def _ag_debug(message: str) -> None:
    """Write low-overhead Antigravity bridge diagnostics to stderr and a log file."""
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    line = f"[antigravity-debug {stamp}] {message}\n"
    print(line, end="", file=__import__("sys").stderr, flush=True)
    try:
        log_dir = Path.home() / ".codex-lab" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "antigravity-input.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def _strip_ansi(text: str) -> str:
    import re
    return re.sub(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text)


def _forward_windows_console_input(proc, stop_event) -> None:
    """Forward user key events from the real Windows console into the PTY."""
    if os.name != "nt":
        return
    import msvcrt
    # This lightweight fallback handles normal typing plus Enter/Backspace.
    # Arrow/function keys are translated to common ANSI sequences.
    special = {
        "H": "\x1b[A", "P": "\x1b[B", "K": "\x1b[D", "M": "\x1b[C",
        "G": "\x1b[H", "O": "\x1b[F", "I": "\x1b[5~", "Q": "\x1b[6~",
    }
    while not stop_event.is_set() and proc.isalive():
        try:
            if not msvcrt.kbhit():
                time.sleep(0.005)
                continue
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                code = msvcrt.getwch()
                proc.write(special.get(code, ""))
            elif ch == "\r":
                proc.write("\r")
            elif ch == "\x03":
                proc.write("\x03")
            elif ch == "\x08":
                proc.write("\x7f")
            elif ch:
                proc.write(ch)
        except (EOFError, OSError, Exception):
            break


def _run_antigravity_stream(info: AgentInfo, worktree: Path, prompt: str) -> dict:
    """Run one Antigravity turn through the documented machine-readable stream."""
    args = [
        info.executable,
        "--print",
        prompt,
        "--output-format",
        "stream-json",
        "--dangerously-skip-permissions",
    ]
    _ag_debug(
        f"launching automated stream session cwd={str(worktree)!r} "
        f"prompt_chars={len(prompt)}"
    )
    _ag_debug(
        "agy command flags: --print --output-format stream-json "
        "--dangerously-skip-permissions"
    )
    started = time.monotonic()
    p = subprocess.Popen(
        args,
        cwd=worktree,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    result = None
    model = None
    total_usage = None
    conversation_id = None
    session_events = 0
    tool_events = 0
    stderr_lines: list[str] = []

    def read_stderr() -> None:
        if p.stderr is None:
            return
        for line in p.stderr:
            line = line.rstrip("\r\n")
            if line:
                stderr_lines.append(line)
                _ag_debug(f"agy stderr: {line[:1000]}")

    stderr_thread = threading.Thread(target=read_stderr, name="antigravity-stderr", daemon=True)
    stderr_thread.start()
    if p.stdout is not None:
        for raw_line in p.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                _ag_debug(f"non-JSON stdout line ignored: {line[:500]}")
                continue
            session_events += 1
            event_type = event.get("event")
            if event_type == "init":
                init = event.get("init") or {}
                conversation_id = event.get("conversation_id") or conversation_id
                model = init.get("model") or model
                _ag_debug(
                    f"stream init conversation_id={conversation_id!r} model={model!r} "
                    f"permission_mode={init.get('permission_mode')!r}"
                )
            elif event_type == "step_update":
                step = event.get("step_update") or {}
                if step.get("step_type") == "tool":
                    tool_events += 1
            elif event_type == "result":
                result = event.get("result") or {}
                conversation_id = result.get("conversation_id") or conversation_id
                total_usage = result.get("usage") or total_usage
                _ag_debug(
                    f"result event detected status={result.get('status')!r} "
                    f"turns={result.get('num_turns')!r} duration={result.get('duration_seconds')!r}"
                )
    returncode = p.wait()
    stderr_thread.join(timeout=1)
    elapsed = round(time.monotonic() - started, 3)
    status = (result or {}).get("status") or ("ERROR" if returncode else "UNKNOWN")
    stderr_text = "\n".join(stderr_lines)
    permission_blocked = (
        "required the \"command\" permission" in stderr_text.lower()
        or "headless mode cannot prompt" in stderr_text.lower()
        or "permission" in str((result or {}).get("error") or "").lower()
           and "command" in str((result or {}).get("error") or "").lower()
    )
    if permission_blocked:
        status = "PERMISSION_BLOCKED"
        _ag_debug("permission-blocked tool detected; overriding AGY SUCCESS status")
    if returncode != 0 and status == "SUCCESS":
        status = "ERROR"
    _ag_debug(
        f"automated stream process exited returncode={returncode} status={status!r} "
        f"elapsed={elapsed}s events={session_events} tools={tool_events}"
    )
    return {
        "returncode": returncode,
        "result_status": status,
        "result_response": (result or {}).get("response"),
        "result_error": (result or {}).get("error"),
        "model": model,
        "conversation_id": conversation_id,
        "input_tokens": (total_usage or {}).get("input_tokens"),
        "output_tokens": (total_usage or {}).get("output_tokens"),
        "total_tokens": (total_usage or {}).get("total_tokens"),
        "cached_input_tokens": (total_usage or {}).get("cache_read_tokens"),
        "stream_events": session_events,
        "tool_events": tool_events,
        "stderr": stderr_text,
        "permission_blocked": permission_blocked,
        "duration_s": elapsed,
    }


def _run_antigravity_interactive(info: AgentInfo, worktree: Path, prompt: str) -> int:
    """Run agy in a ConPTY, inject the initial prompt through the PTY, then proxy I/O."""
    if os.name != "nt":
        p = subprocess.Popen([info.executable], cwd=worktree)
        return p.wait()

    try:
        from winpty import PtyProcess
    except ImportError as exc:
        raise AgentUnavailable("Antigravity interactive mode requires pywinpty on Windows") from exc

    _ag_debug(f"launching agy executable={info.executable!r} cwd={str(worktree)!r}")
    proc = PtyProcess.spawn([info.executable], cwd=str(worktree))
    _ag_debug(f"PTY started pid={getattr(proc, 'pid', 'unknown')}")

    import threading
    import sys

    stop_event = threading.Event()
    prompt_sent = threading.Event()
    ready_event = threading.Event()
    output_buffer = ""
    output_lock = threading.Lock()
    max_ready_wait = float(os.environ.get("CODEX_LAB_ANTIGRAVITY_READY_TIMEOUT", "20"))
    delay = float(os.environ.get("CODEX_LAB_ANTIGRAVITY_PROMPT_DELAY", "0"))
    deadline = time.monotonic() + max_ready_wait

    def inject_when_ready() -> None:
        nonlocal output_buffer
        if delay > 0:
            _ag_debug(f"configured prompt delay={delay}s")
            time.sleep(delay)
        while proc.isalive() and not prompt_sent.is_set():
            if time.monotonic() >= deadline:
                _ag_debug("readiness timeout reached; injecting prompt anyway")
                break
            if ready_event.wait(0.05):
                break
        if not proc.isalive() or prompt_sent.is_set():
            return
        try:
            # Newline is the Enter key for the PTY; this is the actual input path to agy.
            data = prompt + "\r"
            _ag_debug(f"injecting initial prompt via PTY.write chars={len(prompt)} enter=True")
            proc.write(data)
            prompt_sent.set()
            _ag_debug("initial prompt PTY.write completed")
        except Exception as exc:
            _ag_debug(f"initial prompt PTY.write FAILED: {type(exc).__name__}: {exc}")
            print("[antigravity] automatic prompt injection failed; enter the initial prompt in the TUI.")

    def reader() -> None:
        nonlocal output_buffer
        while proc.isalive() and not stop_event.is_set():
            try:
                chunk = proc.read(16384)
            except EOFError:
                break
            except Exception as exc:
                _ag_debug(f"PTY.read FAILED: {type(exc).__name__}: {exc}")
                break
            if not chunk:
                continue
            sys.stdout.write(chunk)
            sys.stdout.flush()
            with output_lock:
                output_buffer = (output_buffer + chunk)[-12000:]
                clean = _strip_ansi(output_buffer)
            # Antigravity's interactive prompt normally ends with a > input marker.
            if not ready_event.is_set() and ("\n>" in clean or clean.rstrip().endswith(">")):
                ready_event.set()
                _ag_debug("detected agy input prompt marker '>'; initial prompt can be injected")

    reader_thread = threading.Thread(target=reader, name="antigravity-pty-reader", daemon=True)
    input_thread = threading.Thread(target=_forward_windows_console_input, args=(proc, stop_event), name="antigravity-console-input", daemon=True)
    injector_thread = threading.Thread(target=inject_when_ready, name="antigravity-prompt-injector", daemon=True)
    reader_thread.start()
    input_thread.start()
    injector_thread.start()

    _ag_debug("interactive bridge started: PTY output -> terminal; console input -> PTY")
    while proc.isalive():
        time.sleep(0.05)
    stop_event.set()
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
