"""
check_fetch_health.py
=====================

Post-run health check for the daily fee-schedule refresh. The pipeline itself
tolerates fetch failures (a failed exchange just keeps its last good Current/
file), which means a broken source can stay silently broken — the Nasdaq
listing-center 403s of 2026-07-31..08-04 went unnoticed for 5 days of green
workflow runs.

This script makes failures loud:

  * Reads the newest Fetched/YYYY-MM-DD/manifest.json.
  * An exchange FAILED if its manifest status is not "ok", or if it is
    missing from the manifest entirely (e.g. the MIAX/BOX page scrape found
    no link for it — previously a fully silent hole).
  * For each failure, walks back through earlier committed runs to count how
    many consecutive runs that exchange has been failing.
  * Single-run blip  -> GitHub ::warning:: annotation, exit stays 0.
    Streak >= 2 runs -> ::error:: annotation and exit 1, so the workflow run
    goes red and GitHub notifies the repo watchers.

Run it AFTER the commit step: the day's data always lands first; this only
decides the color of the run.

Usage:
    python check_fetch_health.py                 # newest run under Fetched/
    python check_fetch_health.py Fetched/2026-08-04
"""

import json
import os
import re
import sys
from pathlib import Path

from effective_dates import EXTRACTORS

REPO_ROOT = Path(__file__).resolve().parent.parent
FETCHED_DIR = REPO_ROOT / "Fetched"

DATED_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Every exchange the fetcher is supposed to deliver each run.
EXPECTED_EXCHANGES = list(EXTRACTORS)

# A failure this many consecutive runs (including today) fails the workflow.
ALERT_STREAK = 2


def list_run_folders() -> list[Path]:
    """All dated run folders under Fetched/ that have a manifest, oldest first."""
    if not FETCHED_DIR.exists():
        return []
    return sorted(
        (p for p in FETCHED_DIR.iterdir()
         if p.is_dir() and DATED_FOLDER_RE.match(p.name) and (p / "manifest.json").exists()),
        key=lambda p: p.name,
    )


def failed_exchanges(run_dir: Path) -> dict[str, str]:
    """{exchange: reason} for every expected exchange not fetched ok in `run_dir`."""
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    by_key = {e["exchange"]: e for e in manifest}
    failures = {}
    for exch in EXPECTED_EXCHANGES:
        entry = by_key.get(exch)
        if entry is None:
            failures[exch] = "missing from manifest (discovery found no link?)"
        elif entry.get("status") != "ok":
            failures[exch] = str(entry.get("status"))
    return failures


def streak_length(exchange: str, runs_newest_first: list[Path]) -> int:
    """Consecutive runs (newest first, starting with today) where `exchange` failed."""
    streak = 0
    for run in runs_newest_first:
        if exchange in failed_exchanges(run):
            streak += 1
        else:
            break
    return streak


def annotate(kind: str, message: str) -> None:
    """Print a GitHub Actions annotation (plain line outside Actions)."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{kind}::{message}")
    else:
        print(f"[{kind.upper()}] {message}")


def write_step_summary(lines: list[str]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def main() -> int:
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        if not (target / "manifest.json").exists():
            print(f"No manifest.json in {target}")
            return 1
    else:
        runs = list_run_folders()
        if not runs:
            print("No run folders with a manifest under Fetched/.")
            return 1
        target = runs[-1]

    older = [r for r in reversed(list_run_folders()) if r.name < target.name]
    failures = failed_exchanges(target)

    print(f"Fetch health for {target.name}: "
          f"{len(EXPECTED_EXCHANGES) - len(failures)}/{len(EXPECTED_EXCHANGES)} exchanges ok")

    if not failures:
        write_step_summary([f"### Fetch health {target.name}: all {len(EXPECTED_EXCHANGES)} exchanges ok"])
        return 0

    summary = [f"### Fetch health {target.name}: {len(failures)} exchange(s) failing", "",
               "| Exchange | Consecutive runs failing | Latest status |",
               "| --- | --- | --- |"]
    alert = False
    for exch, reason in sorted(failures.items()):
        streak = 1 + streak_length(exch, older)
        summary.append(f"| {exch} | {streak} | {reason} |")
        message = f"{exch}: fetch failing {streak} run(s) in a row — {reason}"
        if streak >= ALERT_STREAK:
            alert = True
            annotate("error", message)
        else:
            annotate("warning", message)

    write_step_summary(summary)
    return 1 if alert else 0


if __name__ == "__main__":
    sys.exit(main())
