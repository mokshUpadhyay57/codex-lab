from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    worktree TEXT NOT NULL,
    prompt TEXT NOT NULL,
    agent TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    codex_started_at TEXT,
    codex_ended_at TEXT,
    agent_started_at TEXT,
    agent_ended_at TEXT,
    total_duration_s REAL,
    implementation_duration_s REAL,
    failures INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    recovery_attempts INTEGER NOT NULL DEFAULT 0,
    tests_status TEXT,
    tests_output TEXT,
    analyzer_errors INTEGER,
    analyzer_warnings INTEGER,
    analyzer_infos INTEGER,
    files_changed INTEGER,
    lines_added INTEGER,
    lines_deleted INTEGER,
    commits INTEGER,
    commit_shas TEXT,
    diff_stat TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cost REAL,
    cached_input_tokens INTEGER,
    usage_status TEXT,
    cost_status TEXT,
    session_files TEXT,
    notes TEXT,
    base_commit TEXT
);

CREATE TABLE IF NOT EXISTS interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    at TEXT NOT NULL,
    reason TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'human_input',
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Lightweight forward migration for databases created by v0.1.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    migrations = {
        "agent": "TEXT",
        "agent_started_at": "TEXT",
        "agent_ended_at": "TEXT",
        "intervention_status": "TEXT",
        "cached_input_tokens": "INTEGER",
        "usage_status": "TEXT",
        "cost_status": "TEXT",
        "session_files": "TEXT",
        "analyzer_errors": "INTEGER",
        "analyzer_warnings": "INTEGER",
        "analyzer_infos": "INTEGER",
    }
    for name, kind in migrations.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")
    intervention_cols = {row[1] for row in conn.execute("PRAGMA table_info(interventions)").fetchall()}
    if "type" not in intervention_cols:
        conn.execute("ALTER TABLE interventions ADD COLUMN type TEXT NOT NULL DEFAULT 'human_input'")
    conn.commit()
    return conn


def insert_run(conn, run: dict) -> None:
    cols = ", ".join(run)
    placeholders = ", ".join("?" for _ in run)
    conn.execute(f"INSERT INTO runs ({cols}) VALUES ({placeholders})", list(run.values()))
    conn.commit()


def update_run(conn, run_id: str, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE runs SET {assignments} WHERE run_id = ?", [*fields.values(), run_id])
    conn.commit()


def add_intervention(conn, run_id: str, at: str, reason: str, type: str = "human_input") -> None:
    conn.execute("INSERT INTO interventions(run_id, at, reason, type) VALUES (?, ?, ?, ?)", (run_id, at, reason, type))
    conn.commit()


def get_run(conn, run_id: str):
    return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()


def get_interventions(conn, run_id: str):
    return conn.execute("SELECT at, reason, type FROM interventions WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
