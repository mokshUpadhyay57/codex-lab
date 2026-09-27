from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def run(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and p.returncode:
        raise GitError(p.stdout.strip() or f"git exited {p.returncode}")
    return p.stdout.strip()


def ensure_clean(repo: Path) -> None:
    if run(repo, "status", "--porcelain"):
        raise GitError("repository has uncommitted changes; refusing to create an experiment from a dirty tree")


def create_worktree(repo: Path, worktree: Path, branch: str) -> None:
    run(repo, "worktree", "add", "-b", branch, str(worktree))


def diff_metrics(repo: Path, base_commit: str) -> dict:
    # Compare the final worktree against the exact baseline, including committed changes.
    changed = run(repo, "diff", "--name-only", base_commit, "HEAD", check=False)
    numstat = run(repo, "diff", "--numstat", base_commit, "HEAD", check=False)
    untracked = run(repo, "ls-files", "--others", "--exclude-standard", check=False)

    files = {line for line in changed.splitlines() if line}
    files.update(line for line in untracked.splitlines() if line)
    added = deleted = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            if parts[0].isdigit():
                added += int(parts[0])
            if parts[1].isdigit():
                deleted += int(parts[1])

    # Count untracked file lines because they are part of the experiment's final diff.
    for rel in untracked.splitlines():
        path = repo / rel
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            added += len(lines)
        except OSError:
            pass

    commits = run(repo, "rev-list", "--count", f"{base_commit}..HEAD", check=False)
    shas = run(repo, "log", f"{base_commit}..HEAD", "--pretty=format:%H", check=False)
    stat = run(repo, "diff", "--stat", base_commit, "HEAD", check=False)
    if untracked:
        stat = (stat + "\nUntracked files:\n" + untracked).strip()

    return {
        "files_changed": len(files),
        "lines_added": added,
        "lines_deleted": deleted,
        "commits": int(commits) if commits.isdigit() else None,
        "commit_shas": shas,
        "diff_stat": stat,
    }
