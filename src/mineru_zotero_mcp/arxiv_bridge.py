"""arXiv PDF download + awesome-list parsing.

Mirrors the proven approach from zotero_pdf_pipeline.py (493-paper field test):
  - curl subprocess with 180s timeout (Python urllib3 gets rate-limited harder)
  - 8-thread ThreadPoolExecutor for batches
  - 5KB size floor to catch 0-byte / withdrawn papers
  - skip already-downloaded (content-hash cache)

awesome-list parsing extracts arXiv IDs from GitHub READMEs or local markdown
files, pairing each ID with the nearest preceding heading line as a title.
"""

from __future__ import annotations

import logging
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# Matches: 2605.25874 | arxiv.org/abs/2605.25874 | arxiv.org/pdf/2605.25874v2
# New-style IDs are YYMM.NNNNN; old-style (pre-2007) are archive/0701234 — we
# support both but the common case is the new format.
_ARXIV_NEW = r"\d{4}\.\d{4,5}"
_ARXIV_OLD = r"[a-z\-]+(?:\.[A-Z\-]+)?/\d{7}"
# The prefix alternation ensures we don't match substrings of other URLs.
# Either arxiv.org/abs|pdf/ prefix, or a word-boundary before a bare YYMM.NNNNN.
# The ID itself is one capture group covering both old and new formats.
ARXIV_ID_RE = re.compile(
    rf"(?:(?:arxiv\.org/(?:abs|pdf)/)|(?:^|(?<=\s)))({ _ARXIV_NEW }|{ _ARXIV_OLD })(?:v\d+)?",
    re.IGNORECASE,
)

# Markdown heading or bold text preceding an arXiv link — used to guess a title.
_HEADING_RE = re.compile(r"^\s{0,3}(?:#+\s*|-\s*\*\*)(.+?)(?:\*\*)?\s*$", re.MULTILINE)

MIN_PDF_BYTES = 5000  # <5KB → withdrawn / not-yet-published / error page


@dataclass
class FetchResult:
    arxiv_id: str
    ok: bool
    path: Path | None = None
    cached: bool = False
    error: str | None = None


@dataclass
class ListEntry:
    arxiv_id: str
    title: str | None = None
    source_url: str | None = None


def normalize_arxiv_id(raw: str) -> str | None:
    """Normalize any arXiv reference to a bare ID.

    'https://arxiv.org/abs/2605.25874v2' → '2605.25874'
    'arxiv.org/pdf/0704.0001'             → '0704.0001'
    '2605.25874'                           → '2605.25874'
    Returns None if no valid ID is found.
    """
    m = ARXIV_ID_RE.search(raw.strip())
    if not m:
        return None
    return m.group(1)


def _pdf_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf"


def fetch_pdf(
    arxiv_id: str,
    dest_dir: Path,
    timeout: int = 180,
) -> FetchResult:
    """Download a single arXiv PDF via curl subprocess.

    Skips if a valid (>5KB) file already exists at dest_dir/{id}.pdf (cache).
    Returns FetchResult with path on success, error on failure.
    """
    aid = normalize_arxiv_id(arxiv_id)
    if not aid:
        return FetchResult(arxiv_id=arxiv_id, ok=False, error=f"invalid arXiv id: {arxiv_id}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = dest_dir / f"{aid}.pdf"

    # Cache hit — validate integrity (size + page count) before trusting cache.
    # A truncated curl download can leave a file that starts with %PDF but has
    # 0 parseable pages (missing %%EOF), which Zotero opens as a blank page.
    if pdf_path.is_file() and pdf_path.stat().st_size > MIN_PDF_BYTES:
        if _is_valid_pdf(pdf_path):
            return FetchResult(arxiv_id=aid, ok=True, path=pdf_path, cached=True)
        logger.warning("cached PDF for %s is corrupt (0 pages); re-downloading", aid)
        pdf_path.unlink(missing_ok=True)

    url = _pdf_url(aid)
    try:
        subprocess.run(
            [
                "curl", "-sL", "-o", str(pdf_path),
                "--connect-timeout", "15",
                "--max-time", str(timeout),
                "-H", "User-Agent: Mozilla/5.0",
                url,
            ],
            timeout=timeout + 20,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        return FetchResult(arxiv_id=aid, ok=False, error=f"timeout after {timeout}s")
    except FileNotFoundError:
        return FetchResult(arxiv_id=aid, ok=False, error="curl not found (install curl)")
    except Exception as e:  # noqa: BLE001
        return FetchResult(arxiv_id=aid, ok=False, error=str(e)[:120])

    size = pdf_path.stat().st_size if pdf_path.is_file() else 0
    if size < MIN_PDF_BYTES:
        # Clean up tiny invalid files so re-runs retry
        pdf_path.unlink(missing_ok=True)
        return FetchResult(
            arxiv_id=aid,
            ok=False,
            error=f"PDF too small ({size} bytes) — withdrawn or not yet published",
        )
    # Validate page count — a truncated download passes the size check but
    # produces a 0-page PDF that Zotero renders as blank.
    if not _is_valid_pdf(pdf_path):
        pdf_path.unlink(missing_ok=True)
        return FetchResult(
            arxiv_id=aid,
            ok=False,
            error=f"PDF corrupt (0 pages after {size} bytes) — download truncated, retry",
        )
    return FetchResult(arxiv_id=aid, ok=True, path=pdf_path)


def _is_valid_pdf(path: Path) -> bool:
    """Check that a PDF has at least 1 parseable page.

    Uses PyMuPDF (fitz) which is already a dependency via zotero-mcp[pdf].
    A truncated curl download can produce a file with a valid %PDF header
    but no %%EOF marker, yielding 0 pages. This catches that case so the
    caller can retry instead of caching a corrupt file.
    """
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(path))
        ok = doc.page_count > 0
        doc.close()
        return ok
    except Exception:  # noqa: BLE001
        return False


def fetch_batch(
    arxiv_ids: list[str],
    dest_dir: Path,
    parallel: int = 8,
    timeout: int = 180,
) -> list[FetchResult]:
    """Download many PDFs concurrently. Returns one FetchResult per input ID."""
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for raw in arxiv_ids:
        aid = normalize_arxiv_id(raw)
        if aid and aid not in seen:
            seen.add(aid)
            unique.append(aid)

    results: dict[str, FetchResult] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(unique)))) as pool:
        futures = {pool.submit(fetch_pdf, aid, dest_dir, timeout): aid for aid in unique}
        for fut in as_completed(futures):
            r = fut.result()
            results[r.arxiv_id] = r

    # Return in original input order
    return [results.get(aid, FetchResult(arxiv_id=aid, ok=False, error="missing result"))
            for aid in unique]


def parse_awesome_list(source: str) -> list[ListEntry]:
    """Extract arXiv IDs + titles from a GitHub URL or local markdown/text file.

    GitHub: fetches the raw README.md, then scans line-by-line. Each arXiv ID
    is paired with the nearest preceding markdown heading or bold line as its
    title (awesome-lists convention: papers are listed under section headings).

    Local: reads the file directly (supports .md/.txt/.bib).

    Returns entries in document order, deduplicated by arXiv ID.
    """
    text = _read_list_source(source)
    if not text:
        return []

    entries: list[ListEntry] = []
    seen: set[str] = set()
    current_title: str | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # Track the most recent heading/bold line as a candidate title
        heading_match = _HEADING_RE.match(line)
        if heading_match and not ARXIV_ID_RE.search(stripped):
            cleaned_heading = _clean_title(heading_match.group(1))
            if cleaned_heading:
                current_title = cleaned_heading

        # Find arXiv IDs on this line
        for m in ARXIV_ID_RE.finditer(stripped):
            aid = m.group(1)
            if aid in seen:
                continue
            seen.add(aid)
            # If the line itself has a title (common: "- **Title** [link](url)")
            line_title = _extract_inline_title(stripped, aid) or current_title
            entries.append(ListEntry(arxiv_id=aid, title=line_title, source_url=source))

    return entries


def _read_list_source(source: str) -> str:
    """Fetch text content from a GitHub URL or read a local file."""
    # GitHub URL → raw README
    if source.startswith("http"):
        parsed = urlparse(source)
        if "github.com" in parsed.netloc:
            raw_url = _github_to_raw(source)
            if raw_url:
                try:
                    resp = requests.get(raw_url, timeout=30,
                                        headers={"User-Agent": "Mozilla/5.0"})
                    resp.raise_for_status()
                    return resp.text
                except requests.RequestException as e:
                    logger.warning("failed to fetch %s: %s", raw_url, e)
                    return ""
        # Direct raw URL or other HTTP source
        try:
            resp = requests.get(source, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            logger.warning("failed to fetch %s: %s", source, e)
            return ""

    # Local file
    p = Path(source).expanduser()
    if p.is_file():
        return p.read_text(encoding="utf-8", errors="replace")
    return ""


def _github_to_raw(url: str) -> str | None:
    """Convert a GitHub repo/tree URL to a raw README.md URL.

    https://github.com/user/repo                         → raw .../main/README.md
    https://github.com/user/repo/blob/main/docs/list.md  → raw .../docs/list.md
    Tries 'main' branch first, falls back to 'master'.
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]

    # /user/repo  → 2 parts
    # /user/repo/blob/branch/path...  → 4+ parts
    if len(parts) < 2:
        return None

    user, repo = parts[0], parts[1]
    repo = repo.removesuffix(".git")

    if len(parts) >= 4 and parts[2] == "blob":
        # Direct file: /user/repo/blob/branch/path/to/file.md
        branch = parts[3]
        filepath = "/".join(parts[4:])
        return f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{filepath}"

    # Repo root → try README.md on main, then master
    return f"https://raw.githubusercontent.com/{user}/{repo}/main/README.md"


def _clean_title(text: str) -> str | None:
    """Clean a raw heading/inline line into a presentable title.

    Strips: markdown image syntax ![alt](url), markdown links [text](url),
    bold markers **, list bullets, trailing badges (⭐️ etc), and truncates
    at the first quote-delimited title if present.
    """
    if not text:
        return None
    # Remove markdown images entirely: ![alt](url)
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    # Remove empty-image remnants: [](url)
    cleaned = re.sub(r"\[\s*\]\([^)]*\)", "", cleaned)
    # Remove bracketed badges/emoji: [⭐️], [★], [🔥]
    cleaned = re.sub(r"\[[⭐️🌟✅❌🔥📌📎★☆✦✱]+\]", "", cleaned)
    # Remove generic-word links: [link](url), [pdf](url), etc
    cleaned = re.sub(
        r"\[(link|pdf|here|code|paper|url|abs)\]\([^)]*\)", "", cleaned,
        flags=re.IGNORECASE,
    )
    # Keep non-generic link text: [Title](url) → Title
    cleaned = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cleaned)
    # Strip bold + bullets + numbering
    cleaned = cleaned.replace("**", "")
    cleaned = re.sub(r"^[-*]\s*", "", cleaned.strip())
    cleaned = re.sub(r"^\d+\.\s*", "", cleaned)
    # If there's a quoted title like `"Actual Title"`, prefer it
    quoted = re.search(r'"([^"]{5,})"', cleaned)
    if quoted:
        return quoted.group(1).strip()
    # Strip trailing emoji badges and whitespace
    cleaned = re.sub(r"[⭐️🌟✅❌🔥📌📎]+\s*$", "", cleaned).strip()
    cleaned = cleaned.rstrip(":.,")
    if len(cleaned) > 5:
        return cleaned
    return None


def _strip_markdown_links(text: str) -> str:
    """Remove all markdown link/image syntax from text, keeping only label text
    for non-generic links. Handles links whose URLs span an arXiv ID (which would
    otherwise be truncated mid-URL by ARXIV_ID_RE splitting)."""
    # Images first: ![alt](url) → ""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    # Empty-image remnants: [](url) → ""
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)
    # Generic-word links: [link](url), [pdf](url) → ""
    text = re.sub(
        r"\[(link|pdf|here|code|paper|url|abs)\]\([^)]*\)", "", text,
        flags=re.IGNORECASE,
    )
    # Keep non-generic link text: [Title](url) → Title
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return text


def _extract_inline_title(line: str, arxiv_id: str) -> str | None:
    """Extract a title from a markdown list line like '- **Title** [link](arxiv...)'.

    Strips markdown links *before* splitting on the arXiv ID, so links whose
    URL contains the ID (e.g. [link](https://arxiv.org/abs/2605.25874)) don't
    leak partial URL fragments into the title.
    """
    cleaned_line = _strip_markdown_links(line)
    m = ARXIV_ID_RE.search(cleaned_line)
    if not m:
        before = cleaned_line
    else:
        before = cleaned_line[:m.start()]
    return _clean_title(before)
