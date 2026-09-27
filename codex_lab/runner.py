from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import agents, git, verifier
from .database import add_intervention, connect, get_interventions, get_run, insert_run, update_run

MAX_RETRIES = 3
RUN_DB_FILENAME = "runs-v10.sqlite3"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path(repo: Path | None = None) -> Path:
    import os
    configured = os.environ.get("CODEX_LAB_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".codex-lab" / RUN_DB_FILENAME


def next_run_id(conn) -> str:
    rows = conn.execute("SELECT run_id FROM runs ORDER BY rowid").fetchall()
    return f"RUN-{len(rows) + 1:03d}"


def stable_worktree_path(repo: Path, agent_name: str) -> Path:
    """Return a stable workspace path for an agent/repository pair.

    Antigravity treats a newly-created absolute workspace path as a new project
    and can ask for Workspace Trust again. Reusing this exact path means the
    trust decision is made once, while each experiment still gets its own Git
    branch and isolated worktree contents.
    """
    configured = os.environ.get("CODEX_LAB_WORKTREE_ROOT")
    root = Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex-lab" / "workspaces")
    repo_key = hashlib.sha1(str(repo).encode("utf-8")).hexdigest()[:10]
    repo_name = "".join(c if c.isalnum() or c in "-_." else "-" for c in repo.name).strip(".") or "repo"
    return root / f"{repo_name}-{repo_key}" / agent_name


def prepare_worktree_slot(repo: Path, worktree: Path) -> None:
    """Remove a previous experiment worktree occupying the stable slot.

    Git can retain a worktree registration after its directory is manually
    deleted. Prune those registrations before checking/reusing the stable slot.
    """
    worktree = worktree.resolve()
    git.prune_worktrees(repo)
    if git.worktree_registered(repo, worktree):
        git.remove_worktree(repo, worktree)
    elif worktree.exists():
        shutil.rmtree(worktree)
    git.prune_worktrees(repo)
    worktree.parent.mkdir(parents=True, exist_ok=True)


def run_experiment(repo: Path, prompt: str, agent_name: str = "codex") -> str:
    repo = repo.expanduser().resolve()
    if not (repo / ".git").exists():
        raise RuntimeError(f"not a Git repository: {repo}")
    git.ensure_clean(repo)

    conn = connect(db_path())
    agent = agents.get_adapter(agent_name)
    run_id = next_run_id(conn)
    worktree = stable_worktree_path(repo, agent_name)
    prepare_worktree_slot(repo, worktree)
    branch = f"codex-lab/{run_id.lower()}"
    started_at = now()
    base_commit = git.run(repo, "rev-parse", "HEAD")
    git.create_worktree(repo, worktree, branch)
    # Antigravity must trust the exact generated workspace before its interactive
    # TUI starts. This does not alter agent permission rules.
    agent.prepare_workspace(worktree)
    insert_run(conn, {
        "run_id": run_id, "repo": str(repo), "worktree": str(worktree), "prompt": prompt, "agent": agent_name,
        "status": "running", "started_at": started_at, "base_commit": base_commit,
    })

    total_start = time.monotonic()
    failures = retries = recovery = 0
    implementation_duration = 0.0
    usage_total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cached_input_tokens": 0}
    usage_seen = False
    models = []
    session_files = []
    usage_status = "unavailable"
    intervention_count = 0
    cost_status = "unavailable"
    try:
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                retries += 1
                recovery += 1
                recovery_prompt = (
                    "Continue the same task from the current worktree. The independent verifier failed. "
                    "Inspect the failure, fix the implementation, and rerun the relevant tests/build.\n\n"
                    f"Original task:\n{prompt}"
                )
                run_prompt = recovery_prompt
            else:
                run_prompt = prompt

            update_run(conn, run_id, failures=failures, retries=retries, recovery_attempts=recovery)
            try:
                result = agent.run(worktree, run_prompt)
            except agents.AgentUnavailable as e:
                update_run(conn, run_id, status="blocked", ended_at=now(), total_duration_s=round(time.monotonic()-total_start, 3), notes=str(e))
                print(f"{run_id}: blocked — {e}")
                return run_id

            if result["returncode"] != 0:
                failures += 1
                update_run(conn, run_id, failures=failures)

            implementation_duration += result["duration_s"]
            if result.get("model"):
                models.append(result["model"])
            session_files.extend(result.get("session_files", []))
            for human in result.get("human_messages", []):
                add_intervention(conn, run_id, human.get("at") or now(), human.get("message") or "", "codex_user_message")
                intervention_count += 1
            if result.get("intervention_status") == "available":
                pass
            if result.get("total_tokens") is not None:
                usage_seen = True
                for key in usage_total:
                    usage_total[key] += result.get(key) or 0
            usage_status = "available" if usage_seen else "unavailable"

            verification = verifier.verify(worktree)
            update_run(conn, run_id,
                       codex_started_at=result["started_at"], codex_ended_at=result["ended_at"],
                       agent_started_at=result["started_at"], agent_ended_at=result["ended_at"],
                       tests_status=verification["status"], tests_output=verification["output"],
                       analyzer_errors=verification.get("analyzer_errors"),
                       analyzer_warnings=verification.get("analyzer_warnings"),
                       analyzer_infos=verification.get("analyzer_infos"),
                       agent=agent_name,
                       model=models[-1] if models else None, input_tokens=usage_total["input_tokens"] if usage_seen else None,
                       output_tokens=usage_total["output_tokens"] if usage_seen else None,
                       total_tokens=usage_total["total_tokens"] if usage_seen else None,
                       cached_input_tokens=usage_total["cached_input_tokens"] if usage_seen else None,
                       implementation_duration_s=round(implementation_duration, 3),
                       cost=None, usage_status=usage_status, cost_status=cost_status,
                       intervention_status=result.get("intervention_status", "unavailable"),
                       session_files=json.dumps(sorted(set(session_files))))

            if verification["status"] == "passed":
                break
            if verification["status"] == "unavailable":
                break
            if attempt == MAX_RETRIES:
                break

        metrics = git.diff_metrics(worktree, base_commit)
        status = "success" if verification["status"] == "passed" else "failure"
        update_run(conn, run_id, status=status, ended_at=now(), total_duration_s=round(time.monotonic()-total_start, 3), **metrics)
        print(f"{run_id}: {status}; worktree={worktree}")
        return run_id
    finally:
        # Keep the worktree until the experiment is inspected. The user can remove it manually.
        conn.close()


def report(repo: Path, run_id: str) -> None:
    conn = connect(db_path(repo.expanduser().resolve()))
    row = get_run(conn, run_id)
    if not row:
        raise RuntimeError(f"unknown run: {run_id}")
    print(f"Run: {row['run_id']}\nAgent: {row['agent'] or 'unknown'}\nStatus: {row['status']}\nRepo: {row['repo']}\nWorktree: {row['worktree']}")
    print(f"Total duration: {row['total_duration_s']}s\nImplementation: {row['implementation_duration_s']}s")
    print(f"Failures: {row['failures']}  Retries: {row['retries']}  Recovery: {row['recovery_attempts']}")
    print(f"Tests/build: {row['tests_status']}")
    if row["analyzer_errors"] is not None:
        print(f"Flutter analyze: {row['analyzer_errors']} errors, {row['analyzer_warnings']} warnings, {row['analyzer_infos']} infos")
    print(f"Files changed: {row['files_changed']}  +{row['lines_added']} / -{row['lines_deleted']}")
    print(f"Commits: {row['commits']}\nModel: {row['model'] or 'unavailable'}")
    print(f"Tokens: {row['total_tokens'] if row['total_tokens'] is not None else 'unavailable'} ({row['usage_status'] or 'unknown'})")
    print(f"Cost: {row['cost'] if row['cost'] is not None else 'unavailable'} ({row['cost_status'] or 'unknown'})")
    print(f"Human interventions: {len(get_interventions(conn, run_id))} ({row['intervention_status'] or 'unknown'})")
    for item in get_interventions(conn, run_id):
        print(f"  [{item['type']}] {item['at']} — {item['reason']}")
    if row["notes"]:
        print(f"Notes: {row['notes']}")


def intervene(repo: Path, run_id: str, reason: str) -> None:
    conn = connect(db_path(repo.expanduser().resolve()))
    if not get_run(conn, run_id):
        raise RuntimeError(f"unknown run: {run_id}")
    add_intervention(conn, run_id, now(), reason)
    print(f"Recorded intervention for {run_id}")
