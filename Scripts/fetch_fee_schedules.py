"""
fetch_fee_schedules.py
======================

Downloads the current fee schedule for every supported options exchange into a
dated folder, so the Claude fee-schedule skills can then populate the CSV
outputs.

What it does
------------
1. Creates a dated folder, e.g.  <repo>/Fetched/2026-06-18/
2. Downloads "fixed URL" exchanges directly (HTML or PDF at a stable address).
3. Scrapes the MIAX fees page to find the 4 current date-stamped PDF links
   (MIAX Options, Pearl, Sapphire, Emerald) — filenames change every month.
4. Scrapes the BOX fees page to find its current date-stamped PDF link.
5. Writes a manifest.json summarizing every file (exchange, source URL, size,
   status) so an unattended run can tell what succeeded.
"""

import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

# Pretend to be a normal Chrome browser so the sites don't block us.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# STATIC-URL EXCHANGES
# Fee schedule lives at a fixed web address that never changes.
# key = short name (used to name the output file and select the skill later)
# ext = file type to save as (html or pdf)
# ---------------------------------------------------------------------------
STATIC_TARGETS = [
    # Cboe HTML pages
    {"key": "BZX",     "ext": "html", "url": "https://www.cboe.com/us/options/membership/fee_schedule/bzx/"},
    {"key": "C2",      "ext": "html", "url": "https://www.cboe.com/us/options/membership/fee_schedule/ctwo/"},
    {"key": "EDGX",    "ext": "html", "url": "https://www.cboe.com/us/options/membership/fee_schedule/edgx/"},

    # Cboe Options — full fee schedule as a fixed-name PDF on their CDN
    {"key": "CBOE",    "ext": "pdf",  "url": "https://cdn.cboe.com/resources/membership/Cboe_FeeSchedule.pdf"},

    # MEMX — fee schedule rendered inline as HTML
    {"key": "MEMX",    "ext": "html", "url": "https://info.memxtrading.com/us-options-trading-resources/us-options-fee-schedule/"},

    # Nasdaq listing-center pages (HTML rulebook viewer)
    {"key": "NOM",     "ext": "html", "url": "https://listingcenter.nasdaq.com/rulebook/nasdaq/rules/Nasdaq%20Options%207"},
    {"key": "NTX",     "ext": "html", "url": "https://listingcenter.nasdaq.com/rulebook/nasdaqtx/rules/NTX%20Options%207"},
    {"key": "Gemini",  "ext": "html", "url": "https://listingcenter.nasdaq.com/rulebook/gemx/rules/GEMX%20Options%207"},
    {"key": "ISE",     "ext": "html", "url": "https://listingcenter.nasdaq.com/rulebook/ise/rules/ISE%20Options%207"},
    {"key": "PHLX",    "ext": "html", "url": "https://listingcenter.nasdaq.com/rulebook/phlx/rules/Phlx%20Options%207"},
    {"key": "Mercury", "ext": "html", "url": "https://listingcenter.nasdaq.com/rulebook/mrx/rules/MRX%20Options%207"},

    # NYSE — fixed-name PDFs hosted on nyse.com
    {"key": "ARCA",    "ext": "pdf",  "url": "https://www.nyse.com/publicdocs/nyse/markets/arca-options/NYSE_Arca_Options_Fee_Schedule.pdf"},
    {"key": "AMEX",    "ext": "pdf",  "url": "https://www.nyse.com/publicdocs/nyse/markets/american-options/NYSE_American_Options_Fee_Schedule.pdf"},
]

# ---------------------------------------------------------------------------
# MIAX GROUP — date-stamped PDFs discovered by scraping one fees page
# The filenames change every month (e.g. MIAX_Options_Fee_Schedule_06012026.pdf)
# so we read the page fresh each run and match PDFs by a keyword in the name.
# "Pearl", "Sapphire", "Emerald" are checked before the generic "Options"
# (which is the main MIAX file) to avoid a wrong match.
# ---------------------------------------------------------------------------
MIAX_FEES_PAGE = "https://www.miaxglobal.com/markets/us-options/all-options-exchanges/fees"
MIAX_HOST      = "https://www.miaxglobal.com"
MIAX_MAP = [
    ("Pearl",    "Pearl"),
    ("Sapphire", "Sapphire"),
    ("Emerald",  "Emerald"),
    ("Options",  "MIAX"),
]

# ---------------------------------------------------------------------------
# BOX — single date-stamped PDF discovered by scraping its fees page
# ---------------------------------------------------------------------------
BOX_FEES_PAGE = "https://boxexchange.com/regulatory/fees/"
BOX_HOST      = "https://boxexchange.com"


def download(url: str, out_file: Path, tries: int = 3) -> None:
    """Download `url` to `out_file`, retrying a few times on network hiccups."""
    for attempt in range(1, tries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=60, allow_redirects=True)
            resp.raise_for_status()
            out_file.write_bytes(resp.content)
            return
        except Exception:
            if attempt == tries:
                raise
            time.sleep(2 * attempt)


def scrape_pdf_links(page_url: str, host: str, pattern: str) -> list[str]:
    """
    Fetch `page_url`, find all href values matching `pattern` (a regex),
    make them absolute using `host` if needed, and return de-duplicated list.
    """
    resp = requests.get(page_url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    hrefs = re.findall(pattern, resp.text, flags=re.IGNORECASE)
    hrefs = list(dict.fromkeys(hrefs))  # de-duplicate, keep order
    return [h if h.startswith("http") else host + h for h in hrefs]


def discover_miax_pdfs() -> list[dict]:
    """Scrape the MIAX fees page and return target entries for the 4 PDFs."""
    targets = []
    print("Scraping MIAX fees page for current PDF links...")
    hrefs = scrape_pdf_links(
        MIAX_FEES_PAGE, MIAX_HOST,
        r'href\s*=\s*["\']([^"\']*fee_schedule-files/[^"\']+\.pdf)["\']',
    )
    for keyword, exch in MIAX_MAP:
        match = next((h for h in hrefs if keyword.lower() in h.lower()), None)
        if match:
            targets.append({"key": exch, "ext": "pdf", "url": match})
            hrefs.remove(match)  # don't reuse the same link for two exchanges
        else:
            print(f"  WARNING: no PDF link found for {exch} (keyword '{keyword}')")
    return targets


def discover_box_pdf() -> list[dict]:
    """Scrape the BOX fees page and return a target entry for its PDF."""
    print("Scraping BOX fees page for current PDF link...")
    hrefs = scrape_pdf_links(
        BOX_FEES_PAGE, BOX_HOST,
        r'href\s*=\s*["\']([^"\']*\.pdf)["\']',
    )
    if hrefs:
        return [{"key": "BOX", "ext": "pdf", "url": hrefs[0]}]
    print("  WARNING: no PDF link found for BOX")
    return []


def main() -> None:
    # Output goes to <repo>/Fetched/<today>/
    # (repo root = this script's parent folder's parent)
    repo_root = Path(__file__).resolve().parent.parent
    dest_dir  = repo_root / "Fetched" / date.today().isoformat()
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"Saving fee schedules to: {dest_dir}\n")

    targets = list(STATIC_TARGETS)

    try:
        targets += discover_miax_pdfs()
    except Exception as exc:
        print(f"  WARNING: MIAX scrape failed: {exc}")

    try:
        targets += discover_box_pdf()
    except Exception as exc:
        print(f"  WARNING: BOX scrape failed: {exc}")

    manifest = []
    for t in targets:
        out_file = dest_dir / f"{t['key']}_Fee_Schedule.{t['ext']}"
        try:
            download(t["url"], out_file)
            size   = out_file.stat().st_size
            status = "ok" if size >= 1024 else "suspect-too-small"
            print(f"  [{t['key']:<8}] {size:>9,} bytes  {status}")
            manifest.append({
                "exchange": t["key"], "url": t["url"],
                "file": out_file.name, "type": t["ext"],
                "bytes": size, "status": status,
            })
        except Exception as exc:
            print(f"  [{t['key']:<8}] FAILED: {exc}")
            manifest.append({
                "exchange": t["key"], "url": t["url"],
                "file": None, "type": t["ext"],
                "bytes": 0, "status": f"error: {exc}",
            })

    manifest_path = dest_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nManifest: {manifest_path}")

    ok  = sum(1 for m in manifest if m["status"] == "ok")
    bad = len(manifest) - ok
    print(f"Done. {ok} ok, {bad} need attention. Folder: {dest_dir}")

    # Print the folder path on the final line so a caller can capture it.
    print(dest_dir)


if __name__ == "__main__":
    sys.exit(main())
