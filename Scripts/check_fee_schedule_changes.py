"""
check_fee_schedule_changes.py
=============================

Orchestrates a fee-schedule refresh and decides whether anything actually
changed since the previous run:

  1. Runs fetch_fee_schedules.py, which downloads every exchange's current fee
     schedule into a new dated folder (Fetched/YYYY-MM-DD/) and writes a
     manifest.json.
  2. Extracts each exchange's *effective date* from those files (see
     effective_dates.py) and stores them in that folder's effective_dates.json.
  3. Finds the most recent PREVIOUS run that has an effective_dates.json.
  4. Compares the two: any exchange whose effective date moved (or is brand
     new) is considered "changed".
  5. If anything changed, writes changes.json and hands the changed exchanges
     to process_changed_schedules() — the hook for the next step in your
     pipeline. If nothing changed, it stops: there is nothing to do.

Exit codes (so a scheduler / batch job can branch on the result):
    0  success, NO changes  -> done
    3  success, changes found -> the next step ran
    1  error
    2  this was the first run (no previous run to compare against)

Usage:
    python check_fee_schedule_changes.py            # fetch, then compare
    python check_fee_schedule_changes.py --no-fetch # compare existing folders only
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import effective_dates

REPO_ROOT = Path(__file__).resolve().parent.parent
FETCHED_DIR = REPO_ROOT / "Fetched"
FETCH_SCRIPT = Path(__file__).resolve().parent / "fetch_fee_schedules.py"

DATED_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Exit codes
EXIT_NO_CHANGES = 0
EXIT_ERROR = 1
EXIT_FIRST_RUN = 2
EXIT_CHANGES = 3


# ---------------------------------------------------------------------------
# Step 1 — run the fetcher
# ---------------------------------------------------------------------------
def run_fetcher(python_exe: str) -> Path:
    """Run fetch_fee_schedules.py and return the dated folder it produced."""
    print(f"Running fetcher: {FETCH_SCRIPT.name} ...\n" + "-" * 60)
    proc = subprocess.run(
        [python_exe, str(FETCH_SCRIPT)],
        capture_output=True, text=True,
    )
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    print("-" * 60)
    if proc.returncode != 0:
        raise RuntimeError(f"Fetcher exited with code {proc.returncode}")

    # The fetcher prints the destination folder on its final line.
    last_line = next((ln.strip() for ln in reversed(proc.stdout.splitlines()) if ln.strip()), "")
    candidate = Path(last_line)
    if candidate.is_dir() and (candidate / "manifest.json").exists():
        return candidate

    # Fallback: today's folder by convention.
    fallback = FETCHED_DIR / date.today().isoformat()
    if (fallback / "manifest.json").exists():
        return fallback
    raise RuntimeError("Could not determine the folder the fetcher wrote to.")


# ---------------------------------------------------------------------------
# Run-folder helpers
# ---------------------------------------------------------------------------
def list_run_folders() -> list[Path]:
    """All dated run folders under Fetched/, oldest first."""
    if not FETCHED_DIR.exists():
        return []
    folders = [p for p in FETCHED_DIR.iterdir() if p.is_dir() and DATED_FOLDER_RE.match(p.name)]
    return sorted(folders, key=lambda p: p.name)


def latest_run_folder() -> Path | None:
    folders = list_run_folders()
    return folders[-1] if folders else None


def previous_run_with_dates(current: Path) -> Path | None:
    """Most recent run strictly older than `current` that has effective_dates.json."""
    for folder in reversed(list_run_folders()):
        if folder.name < current.name and (folder / "effective_dates.json").exists():
            return folder
    return None


def load_dates(run_dir: Path) -> dict:
    return json.loads((run_dir / "effective_dates.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Step 4 — compare two runs
# ---------------------------------------------------------------------------
def compare_runs(new_dates: dict, old_dates: dict) -> dict:
    """
    Return {"changed": [...], "unchanged": [...], "needs_review": [...]}.

    changed       effective date moved, or the exchange is brand new
    needs_review  could not extract a date this run (can't tell if it changed)
    """
    changed, unchanged, needs_review = [], [], []

    for exch, info in new_dates.items():
        new_date = info.get("effective_date")
        old_info = old_dates.get(exch)
        old_date = old_info.get("effective_date") if old_info else None

        if new_date is None:
            needs_review.append({
                "exchange": exch, "reason": "could not extract effective date this run",
                "old_date": old_date, "file": info.get("file"),
            })
            continue

        if old_info is None:
            changed.append({"exchange": exch, "reason": "new exchange (no prior record)",
                            "old_date": None, "new_date": new_date, "file": info.get("file")})
        elif new_date != old_date:
            changed.append({"exchange": exch, "reason": "effective date changed",
                            "old_date": old_date, "new_date": new_date, "file": info.get("file")})
        else:
            unchanged.append(exch)

    return {"changed": changed, "unchanged": unchanged, "needs_review": needs_review}


# ---------------------------------------------------------------------------
# Step 5 — the next step (hook)
# ---------------------------------------------------------------------------
def process_changed_schedules(changed: list[dict], new_dir: Path) -> None:
    """
    Hook for the next stage of the pipeline. Receives the list of exchanges
    whose fee schedule changed, with the path to the freshly-downloaded file
    for each.

    Right now this just lists them. Replace the body with whatever the next
    step is (e.g. kick off the per-exchange CSV-extraction skills, open a PR,
    send a notification, etc.).
    """
    print("\n>>> NEXT STEP: process changed fee schedules")
    for c in changed:
        fpath = new_dir / c["file"] if c.get("file") else None
        print(f"    - {c['exchange']:<9} {c['old_date']} -> {c['new_date']}   {fpath}")
    # TODO: invoke the downstream processing for `changed` here.


# ---------------------------------------------------------------------------
# Reusable detection (called by main() and by the master pipeline script)
# ---------------------------------------------------------------------------
def detect_changes(no_fetch: bool = False, python_exe: str = sys.executable) -> dict:
    """
    Run (optionally) the fetcher, extract + store this run's effective dates,
    compare against the previous run, and return a structured result:

        {
          "new_dir": Path,            # this run's folder
          "prev_dir": Path | None,    # the run it was compared against
          "first_run": bool,          # True if there was no prior run to compare
          "changed": [...],           # exchanges whose effective date moved / are new
          "unchanged": [...],         # exchange keys that didn't change
          "needs_review": [...],      # exchanges whose date couldn't be extracted
        }

    When first_run is True, the comparison lists are empty (baseline only).
    Also writes changes.json into new_dir when anything changed.
    """
    # Step 1: get the new run folder.
    if no_fetch:
        new_dir = latest_run_folder()
        if new_dir is None:
            raise RuntimeError("No run folders found under Fetched/.")
        print(f"--no-fetch: using latest existing run {new_dir.name}")
    else:
        new_dir = run_fetcher(python_exe)

    # Step 2: extract + store this run's effective dates.
    print(f"\nExtracting effective dates for {new_dir.name} ...")
    dates_path = effective_dates.write_dates_file(new_dir)
    new_dates = load_dates(new_dir)
    extracted = sum(1 for d in new_dates.values() if d["effective_date"])
    print(f"  {extracted}/{len(new_dates)} effective dates extracted -> {dates_path.name}")

    # Step 3: find the previous run to compare against.
    prev_dir = previous_run_with_dates(new_dir)
    if prev_dir is None:
        return {"new_dir": new_dir, "prev_dir": None, "first_run": True,
                "changed": [], "unchanged": [], "needs_review": []}

    print(f"\nComparing {new_dir.name}  vs  previous {prev_dir.name}")
    result = compare_runs(new_dates, load_dates(prev_dir))
    result.update({"new_dir": new_dir, "prev_dir": prev_dir, "first_run": False})

    # Persist what changed (so a downstream step / audit can pick it up).
    if result["changed"] or result["needs_review"]:
        changes_record = {
            "new_run": new_dir.name,
            "previous_run": prev_dir.name,
            "changed": result["changed"],
            "needs_review": result["needs_review"],
        }
        (new_dir / "changes.json").write_text(
            json.dumps(changes_record, indent=2), encoding="utf-8")
    return result


def print_summary(result: dict) -> None:
    """Print the unchanged/changed/needs-review counts for a detection result."""
    print(f"\n  unchanged   : {len(result['unchanged'])}")
    print(f"  changed     : {len(result['changed'])}")
    print(f"  needs review: {len(result['needs_review'])}")
    for nr in result["needs_review"]:
        print(f"      ?? {nr['exchange']}: {nr['reason']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip running the fetcher; compare the existing folders.")
    parser.add_argument("--python", default=sys.executable,
                        help="Python interpreter used to run the fetcher (default: this one).")
    args = parser.parse_args()

    result = detect_changes(no_fetch=args.no_fetch, python_exe=args.python)

    if result["first_run"]:
        print("\nNo previous run with effective dates to compare against.")
        print("Baseline stored. Re-run after the next fetch to detect changes.")
        return EXIT_FIRST_RUN

    print_summary(result)

    if not result["changed"] and not result["needs_review"]:
        print("\nNo fee schedules changed since the previous run. Done.")
        return EXIT_NO_CHANGES

    if result["changed"]:
        print(f"\n{len(result['changed'])} fee schedule(s) changed. Wrote changes.json")
        process_changed_schedules(result["changed"], result["new_dir"])

    return EXIT_CHANGES


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"\nERROR: {exc}")
        sys.exit(EXIT_ERROR)
