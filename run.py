#!/usr/bin/env python
"""
Entrypoint for Windows Task Scheduler.

Usage:
    python run.py --env prod
    python run.py --env dev

Exit codes:
    0 = run completed (including "resume-only" and "no changed records" runs)
    1 = run raised an unhandled/unexpected error (Task Scheduler should alert
        on this; check logs/<env>/all_runs_errors.log for detail)

Scheduling note: this script does not itself prevent overlapping invocations
at the OS level. Rely on Task Scheduler's own "do not start a new instance if
already running" setting in addition to the application-level IN_PROGRESS-job
check in orchestrator.run() (§8 step 1 / TC9), which blocks a second run from
doing new work even if it were launched.
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from demandbase_sync.config import load_config
from demandbase_sync.orchestrator import run


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Demandbase Opportunity sync.")
    parser.add_argument(
        "--env",
        choices=["dev", "test", "prod"],
        default=None,
        help="Environment to run in. Falls back to DEMANDBASE_SYNC_ENV, then 'dev'.",
    )
    args = parser.parse_args()

    config = load_config(args.env)

    try:
        run_id = run(config)
        print(f"Run {run_id} completed. See logs under {config.paths.run_log_dir}.")
        return 0
    except Exception:
        logging.getLogger(__name__).exception("Unhandled error in sync run")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
