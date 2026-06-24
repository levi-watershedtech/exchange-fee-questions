"""
build_current_markdown.py
=========================

Converts the raw fee-schedule downloads for a run (HTML + PDF) into one lean,
faithful Markdown file per exchange under <repo>/Current/.

Why this exists
---------------
The raw downloads in Fetched/<date>/ are bloated: a single Cboe HTML page is
~660 KB, but >90% of that is markup, scripts, nav and CSS — not fee data. A
Claude Project's practical ceiling is its context window, so raw HTML/PDF for
18 exchanges does not fit in one Project. Converting each schedule to clean
Markdown (just the effective date + the fee tables/text, with the page chrome
stripped) typically shrinks it 85-95%, letting all exchanges live in ONE
Project that the team points at the Current/ folder via the GitHub integration.

What it produces
----------------
Current/  — the single folder the Claude Project syncs from. For each exchange
in the run's manifest.json:
  * HTML exchanges -> Current/<EXCHANGE>.md  (converted; a small front-matter
    header with exchange/effective date/source/fetched date, then the schedule)
  * PDF  exchanges -> Current/<EXCHANGE>.pdf (copied as-is)

Why HTML is converted but PDF is copied
---------------------------------------
A Claude Project counts capacity in tokens. Raw HTML is ingested with all its
markup/nav/scripts, so converting it to clean Markdown cuts it ~85-94% — a huge
capacity win. A PDF is ingested as its *extracted text*, so it is already lean;
converting it saves ~nothing, so we keep the original PDF (proven fidelity).

Conversion
----------
  Cboe / MEMX HTML : isolate the main content region, drop chrome, then
                     markdownify (tables preserved).
  Nasdaq HTML      : purpose-built renderer (see nasdaq_html_to_markdown) — the
                     listing-center pages bury rules in nested layout tables.
  PDF              : copied verbatim into Current/.

Usage
-----
    python build_current_markdown.py                 # latest run under Fetched/
    python build_current_markdown.py Fetched/2026-06-23
"""

import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify as html_to_md

REPO_ROOT = Path(__file__).resolve().parent.parent
FETCHED_DIR = REPO_ROOT / "Fetched"
CURRENT_DIR = REPO_ROOT / "Current"

DATED_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Human-readable names for the H1 title (falls back to the key otherwise).
EXCHANGE_NAMES = {
    "BZX": "Cboe BZX Options", "C2": "Cboe C2 Options", "EDGX": "Cboe EDGX Options",
    "CBOE": "Cboe Options (C1)", "MEMX": "MEMX Options",
    "NOM": "Nasdaq Options Market (NOM)", "NTX": "Nasdaq BX (NTX) Options",
    "Gemini": "Nasdaq GEMX", "ISE": "Nasdaq ISE", "PHLX": "Nasdaq PHLX",
    "Mercury": "Nasdaq MRX (Mercury)", "ARCA": "NYSE Arca Options",
    "AMEX": "NYSE American Options", "MIAX": "MIAX Options",
    "Pearl": "MIAX Pearl Options", "Sapphire": "MIAX Sapphire Options",
    "Emerald": "MIAX Emerald Options", "BOX": "BOX Options",
}

# Tags that are never fee content — removed before conversion.
# NOTE: do NOT strip <form>. The Nasdaq listing-center pages are ASP.NET
# WebForms, which wrap the entire page body in one <form runat="server">;
# removing it would delete the whole fee schedule. We still drop the form's
# interactive children (button/input) as noise.
_CHROME_TAGS = ["script", "style", "noscript", "svg", "iframe", "head",
                "nav", "header", "footer", "button", "input", "link", "meta"]

# Nasdaq listing-center exchanges. Their rulebook pages are ASP.NET WebForms
# that wrap the rule text in layers of *layout* tables — markdownify turns those
# into giant garbage pipe-tables, so these get a purpose-built renderer
# (nasdaq_html_to_markdown) that flattens layout but keeps real fee tables as grids.
NASDAQ_KEYS = {"NOM", "NTX", "Gemini", "ISE", "PHLX", "Mercury"}

# Block-level tags that should force a line break when flattening Nasdaq prose.
_BLOCK_TAGS = {"p", "div", "tr", "li", "section", "article",
               "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "br"}


def _latest_run() -> Path | None:
    if not FETCHED_DIR.exists():
        return None
    runs = [p for p in FETCHED_DIR.iterdir()
            if p.is_dir() and DATED_FOLDER_RE.match(p.name)]
    return max(runs, key=lambda p: p.name) if runs else None


def _tidy(md: str) -> str:
    """Collapse runs of blank lines and trim trailing whitespace per line."""
    md = "\n".join(line.rstrip() for line in md.splitlines())
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


def html_to_markdown(path: Path) -> str:
    """Isolate the main content of an HTML page and convert it to Markdown."""
    soup = BeautifulSoup(path.read_bytes(), "lxml")
    for tag in soup(_CHROME_TAGS):
        tag.decompose()

    # Prefer a semantic main-content container; fall back to <body>, then whole doc.
    main = (soup.find("main")
            or soup.find("article")
            or soup.find(attrs={"role": "main"})
            or soup.find(id=re.compile("content|main", re.I))
            or soup.find("body")
            or soup)

    md = html_to_md(str(main), heading_style="ATX", strip=["a", "img"])
    return _tidy(md)


# ---------------------------------------------------------------------------
# Nasdaq listing-center rulebook pages — purpose-built renderer.
# These pages bury the rule text inside nested *layout* tables, which markdownify
# mangles into one enormous pipe-table. Instead we walk the rulebook content
# container ourselves: real fee tables (>=2 rows and >=2 columns) become Markdown
# grids; everything else (layout wrappers + prose) is flattened to clean text.
# ---------------------------------------------------------------------------
def _cell_text(cell) -> str:
    return " ".join(cell.get_text(" ", strip=True).split()).replace("|", "\\|")


def _grid_rows(table) -> list[list[str]]:
    """Rows belonging directly to `table` (cells inside a nested table excluded)."""
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells = [_cell_text(c) for c in tr.find_all(["td", "th"])
                 if c.find_parent("table") is table]
        rows.append(cells)
    return [r for r in rows if any(x.strip() for x in r)]


def _is_data_table(table) -> bool:
    rows = _grid_rows(table)
    return len(rows) >= 2 and max((len(r) for r in rows), default=0) >= 2


def _table_to_grid(table) -> str:
    rows = _grid_rows(table)
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join(["---"] * ncol) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _render_nasdaq(el, out: list[str]) -> None:
    for child in el.children:
        if isinstance(child, NavigableString):
            s = str(child).strip()
            if s:
                out.append(s + " ")
        elif isinstance(child, Tag):
            if child.name in ("script", "style", "noscript"):
                continue
            if child.name == "table" and _is_data_table(child):
                out.append("\n\n" + _table_to_grid(child) + "\n\n")
            else:
                _render_nasdaq(child, out)
                if child.name in _BLOCK_TAGS:
                    out.append("\n")


def nasdaq_html_to_markdown(path: Path) -> str:
    """Render a Nasdaq listing-center rulebook page to clean Markdown."""
    soup = BeautifulSoup(path.read_bytes(), "lxml")
    main = (soup.find("span", id=re.compile("RulebookContent", re.I))
            or soup.find("div", class_="rulebook-rules-container"))
    if main is None:  # fall back to the generic HTML path if the layout changed
        return html_to_markdown(path)
    out: list[str] = []
    _render_nasdaq(main, out)
    return _tidy("".join(out))


def build_current(run_dir: Path) -> list[dict]:
    """Build Current/<EXCHANGE>.md for every exchange in run_dir's manifest."""
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    dates_path = run_dir / "effective_dates.json"
    dates = json.loads(dates_path.read_text(encoding="utf-8")) if dates_path.exists() else {}

    CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    summary = []

    for entry in manifest:
        exch = entry["exchange"]
        fname = entry.get("file")
        if not fname:
            print(f"  [{exch:<8}] SKIP (no file downloaded: {entry.get('status')})")
            continue

        src = run_dir / fname
        if not src.exists():
            print(f"  [{exch:<8}] SKIP (missing {fname})")
            continue

        raw_kb = src.stat().st_size / 1024

        # PDFs are already lean in token terms — copy them verbatim.
        if entry["type"] == "pdf":
            out = CURRENT_DIR / f"{exch}.pdf"
            shutil.copyfile(src, out)
            print(f"  [{exch:<8}] {raw_kb:7.0f} KB    copied (pdf)")
            summary.append({"exchange": exch, "raw_kb": raw_kb,
                            "out_kb": raw_kb, "kind": "pdf"})
            continue

        # HTML exchanges get converted to Markdown.
        try:
            body = (nasdaq_html_to_markdown(src) if exch in NASDAQ_KEYS
                    else html_to_markdown(src))
        except Exception as exc:
            print(f"  [{exch:<8}] FAILED: {type(exc).__name__}: {exc}")
            continue

        eff = (dates.get(exch) or {}).get("effective_date")
        title = EXCHANGE_NAMES.get(exch, exch)
        header = (
            "---\n"
            f"exchange: {exch}\n"
            f"effective_date: {eff or 'unknown'}\n"
            f"source_url: {entry.get('url', '')}\n"
            f"fetched: {run_dir.name}\n"
            "---\n\n"
            f"# {title} — Fee Schedule\n\n"
            f"*Effective {eff or 'unknown'}. Source: {entry.get('url', '')}*\n\n"
        )
        out = CURRENT_DIR / f"{exch}.md"
        out.write_text(header + body, encoding="utf-8")

        md_kb = out.stat().st_size / 1024
        pct = (1 - md_kb / raw_kb) * 100 if raw_kb else 0
        print(f"  [{exch:<8}] {raw_kb:7.0f} KB -> {md_kb:6.0f} KB  ({pct:4.0f}% smaller)")
        summary.append({"exchange": exch, "raw_kb": raw_kb,
                        "out_kb": md_kb, "kind": "md"})

    return summary


def main() -> int:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else _latest_run()
    if not run_dir or not run_dir.exists():
        print("No run folder found. Usage: python build_current_markdown.py [Fetched/YYYY-MM-DD]")
        return 1
    if not (run_dir / "manifest.json").exists():
        print(f"No manifest.json in {run_dir}")
        return 1

    print(f"Building Current/ Markdown from {run_dir.name} ...\n")
    summary = build_current(run_dir)

    if summary:
        n_md = sum(1 for s in summary if s["kind"] == "md")
        n_pdf = sum(1 for s in summary if s["kind"] == "pdf")
        raw = sum(s["raw_kb"] for s in summary)
        out = sum(s["out_kb"] for s in summary)
        print(f"\n  {len(summary)} exchanges  ({n_md} HTML->md, {n_pdf} pdf copied)")
        print(f"  raw total     : {raw/1024:6.2f} MB")
        print(f"  Current/ total: {out/1024:6.2f} MB  ({(1-out/raw)*100:.0f}% smaller)")
        print(f"  output        : {CURRENT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
