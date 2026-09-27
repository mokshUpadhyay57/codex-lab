from __future__ import annotations

import argparse
from pathlib import Path

from . import agents
from .runner import intervene, report, run_experiment


def main() -> None:
    parser = argparse.ArgumentParser(prog="codex-lab")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--repo", required=True)
    run.add_argument("--prompt", required=True)
    run.add_argument("--agent", choices=["codex", "claude", "antigravity"], default="codex")

    rep = sub.add_parser("report")
    rep.add_argument("run_id")
    rep.add_argument("--repo", default=".")  # retained for backwards-compatible invocation

    intr = sub.add_parser("intervene")
    intr.add_argument("run_id")
    intr.add_argument("--reason", required=True)
    intr.add_argument("--repo", default=".")

    check = sub.add_parser("doctor", help="inspect supported coding-agent CLIs without starting a session")

    args = parser.parse_args()
    if args.command == "run":
        run_experiment(Path(args.repo), args.prompt, agent_name=args.agent)
    elif args.command == "report":
        report(Path(args.repo), args.run_id)
    elif args.command == "intervene":
        intervene(Path(args.repo), args.run_id, args.reason)
    elif args.command == "doctor":
        print(agents.inspect_all())
