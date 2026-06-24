"""
effective_dates.py
===================

Extracts the "effective date" of each exchange's fee schedule from the files
that fetch_fee_schedules.py downloads, and normalizes every date to ISO format
(YYYY-MM-DD) so two runs can be compared regardless of how each source happens
to print its date.

Each exchange advertises its effective date in a different place:

  Cboe HTML  (BZX, C2, EDGX) ... a top-of-page "Effective <Month D, YYYY>" line
  Cboe PDF   (CBOE) ........... a "Fees Schedule - <Month D, YYYY>" header
  MEMX HTML  (MEMX) ........... a "(EFFECTIVE <MONTH D, YYYY>)" banner
  Nasdaq     (NOM, NTX, ...) .. the newest date in the rulebook "Versions:" menu
  NYSE PDF   (ARCA) ........... "Effective Date: <Month D, YYYY>"
             (AMEX) ........... "Effective as of <Month D, YYYY>"
  MIAX PDFs  (MIAX, Pearl, ...) the MMDDYYYY stamp in the PDF's URL/filename
  BOX PDF    (BOX) ............ the "as-of-<Month>-DD-YYYY" stamp in its URL

The public entry point is `extract_dates_for_run(run_dir)`, which reads the
run's manifest.json, extracts a date for every exchange, and returns a dict
{exchange: {"effective_date": "YYYY-MM-DD"|None, "raw": <text>, "source": ...}}.
"""

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup
from pypdf import PdfReader

# ---------------------------------------------------------------------------
# Date parsing / normalization
# ---------------------------------------------------------------------------
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# "May 28, 2026" / "June 1, 2026" / "Sept. 3, 2026" (month name, day, year)
_RE_MONTH_DAY_YEAR = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)


def _iso(year: int, month: int, day: int) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}"


def normalize_date(text: str) -> str | None:
    """Parse the first recognizable date in `text` and return it as YYYY-MM-DD."""
    if not text:
        return None
    text = text.strip()

    # Month name form: "May 28, 2026", "Sept. 3, 2026", "April-24-2026"
    m = _RE_MONTH_DAY_YEAR.search(text.replace("-", " "))
    if m:
        mon = _MONTHS[m.group(1)[:3].lower()]
        return _iso(int(m.group(3)), mon, int(m.group(2)))

    # MM/DD/YYYY  (Nasdaq version menu)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        return _iso(int(m.group(3)), int(m.group(1)), int(m.group(2)))

    # MMDDYYYY    (MIAX filename stamp, exactly 8 digits)
    m = re.search(r"(?<!\d)(\d{2})(\d{2})(\d{4})(?!\d)", text)
    if m:
        return _iso(int(m.group(3)), int(m.group(1)), int(m.group(2)))

    return None


# ---------------------------------------------------------------------------
# File readers
# ---------------------------------------------------------------------------
def _html_text(path: Path) -> str:
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n")


def _html_raw(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _pdf_text(path: Path, pages: int = 1) -> str:
    reader = PdfReader(str(path))
    n = min(pages, len(reader.pages))
    return "\n".join((reader.pages[i].extract_text() or "") for i in range(n))


def _pdf_moddate(path: Path) -> str | None:
    """Fallback: the PDF's /ModDate metadata, e.g. D:20260616085452 -> 2026-06-16."""
    try:
        meta = PdfReader(str(path)).metadata or {}
    except Exception:
        return None
    raw = meta.get("/ModDate") or meta.get("/CreationDate") or ""
    m = re.search(r"D:(\d{4})(\d{2})(\d{2})", str(raw))
    return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


# ---------------------------------------------------------------------------
# Per-exchange extractors. Each returns the raw matched text (or None).
# ---------------------------------------------------------------------------
def _first_clean_effective(text: str) -> str | None:
    """First standalone 'Effective <Month D, YYYY>' (skip 'through'/'as of' clauses)."""
    for line in text.splitlines():
        line = line.strip().rstrip(".")
        m = re.match(r"Effective\s+(" + _RE_MONTH_DAY_YEAR.pattern + r")$", line, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _extract_cboe_html(path: Path, url: str) -> str | None:
    return _first_clean_effective(_html_text(path))


def _extract_cboe_pdf(path: Path, url: str) -> str | None:
    text = _pdf_text(path, pages=1)
    m = re.search(r"Fees?\s+Schedule\s*[-–]\s*(" + _RE_MONTH_DAY_YEAR.pattern + r")",
                  text, re.IGNORECASE)
    return m.group(1) if m else None


def _extract_memx_html(path: Path, url: str) -> str | None:
    text = _html_text(path)
    # "(EFFECTIVE JUNE 1, 2026)" — first full-date EFFECTIVE that isn't a "through" clause
    for m in re.finditer(r"EFFECTIVE\s+(" + _RE_MONTH_DAY_YEAR.pattern + r")", text, re.IGNORECASE):
        return m.group(1)
    return None


def _extract_nasdaq_html(path: Path, url: str) -> str | None:
    """Newest date in the rulebook 'Versions:' dropdown (first real <option>)."""
    html = _html_raw(path)
    m = re.search(r'id="ctl00_MainContent_uctrlRulebookViewer_ddlVersions".*?</select>',
                  html, re.IGNORECASE | re.DOTALL)
    block = m.group(0) if m else html
    m = re.search(r">(\d{2}/\d{2}/\d{4})<", block)
    return m.group(1) if m else None


def _extract_arca_pdf(path: Path, url: str) -> str | None:
    m = re.search(r"Effective\s+Date:?\s*(" + _RE_MONTH_DAY_YEAR.pattern + r")",
                  _pdf_text(path, pages=1), re.IGNORECASE)
    return m.group(1) if m else None


def _extract_amex_pdf(path: Path, url: str) -> str | None:
    m = re.search(r"Effective\s+as\s+of\s*(" + _RE_MONTH_DAY_YEAR.pattern + r")",
                  _pdf_text(path, pages=1), re.IGNORECASE)
    return m.group(1) if m else None


def _extract_from_url(path: Path, url: str) -> str | None:
    """MIAX (..._MMDDYYYY.pdf) and BOX (...as-of-Month-DD-YYYY...) carry the date in the URL."""
    name = (url or path.name)
    # BOX style: "as-of-April-24-2026"
    m = re.search(r"as-of-([A-Za-z]+)-(\d{1,2})-(\d{4})", name, re.IGNORECASE)
    if m:
        return f"{m.group(1)} {m.group(2)}, {m.group(3)}"
    # MIAX style: an 8-digit MMDDYYYY stamp
    m = re.search(r"(?<!\d)(\d{2}\d{2}\d{4})(?!\d)", name)
    return m.group(1) if m else None


# exchange -> (extractor, human-readable source description)
EXTRACTORS = {
    "BZX":     (_extract_cboe_html,  "Cboe HTML 'Effective' line"),
    "C2":      (_extract_cboe_html,  "Cboe HTML 'Effective' line"),
    "EDGX":    (_extract_cboe_html,  "Cboe HTML 'Effective' line"),
    "CBOE":    (_extract_cboe_pdf,   "Cboe PDF 'Fees Schedule -' header"),
    "MEMX":    (_extract_memx_html,  "MEMX HTML '(EFFECTIVE ...)' banner"),
    "NOM":     (_extract_nasdaq_html, "Nasdaq rulebook Versions menu"),
    "NTX":     (_extract_nasdaq_html, "Nasdaq rulebook Versions menu"),
    "Gemini":  (_extract_nasdaq_html, "Nasdaq rulebook Versions menu"),
    "ISE":     (_extract_nasdaq_html, "Nasdaq rulebook Versions menu"),
    "PHLX":    (_extract_nasdaq_html, "Nasdaq rulebook Versions menu"),
    "Mercury": (_extract_nasdaq_html, "Nasdaq rulebook Versions menu"),
    "ARCA":    (_extract_arca_pdf,   "NYSE Arca PDF 'Effective Date:'"),
    "AMEX":    (_extract_amex_pdf,   "NYSE American PDF 'Effective as of'"),
    "MIAX":    (_extract_from_url,   "MIAX URL date stamp"),
    "Pearl":   (_extract_from_url,   "MIAX URL date stamp"),
    "Sapphire":(_extract_from_url,   "MIAX URL date stamp"),
    "Emerald": (_extract_from_url,   "MIAX URL date stamp"),
    "BOX":     (_extract_from_url,   "BOX URL date stamp"),
}


def extract_one(exchange: str, file_path: Path | None, url: str) -> dict:
    """Extract + normalize the effective date for a single exchange."""
    extractor, source = EXTRACTORS.get(exchange, (None, "unknown"))
    raw = None
    if extractor and file_path and file_path.exists():
        try:
            raw = extractor(file_path, url)
        except Exception as exc:  # never let one bad file abort the whole run
            return {"effective_date": None, "raw": None, "source": source,
                    "error": f"{type(exc).__name__}: {exc}"}
    iso = normalize_date(raw) if raw else None

    # Last-ditch fallback for Cboe PDF: use the document's ModDate metadata.
    if iso is None and exchange == "CBOE" and file_path and file_path.exists():
        iso = _pdf_moddate(file_path)
        if iso:
            source += " (ModDate fallback)"

    return {"effective_date": iso, "raw": raw, "source": source}


def extract_dates_for_run(run_dir: Path) -> dict:
    """
    Read run_dir/manifest.json and extract an effective date for every exchange.
    Returns {exchange: {effective_date, raw, source, file, url}}.
    """
    run_dir = Path(run_dir)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No manifest.json in {run_dir}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results = {}
    for entry in manifest:
        exch = entry["exchange"]
        fname = entry.get("file")
        fpath = (run_dir / fname) if fname else None
        info = extract_one(exch, fpath, entry.get("url", ""))
        info["file"] = fname
        info["url"] = entry.get("url", "")
        results[exch] = info
    return results


def write_dates_file(run_dir: Path) -> Path:
    """Extract dates for `run_dir` and write them to run_dir/effective_dates.json."""
    run_dir = Path(run_dir)
    dates = extract_dates_for_run(run_dir)
    out = run_dir / "effective_dates.json"
    out.write_text(json.dumps(dates, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    # Standalone use:  python effective_dates.py <run_folder>
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if not target or not target.exists():
        print("Usage: python effective_dates.py <Fetched/YYYY-MM-DD folder>")
        sys.exit(1)

    out = write_dates_file(target)
    data = json.loads(out.read_text(encoding="utf-8"))
    print(f"Wrote {out}\n")
    for exch, info in data.items():
        flag = "  " if info["effective_date"] else "??"
        print(f" {flag} {exch:<9} {str(info['effective_date']):<12} "
              f"(raw={info['raw']!r}, {info['source']})")
