"""Bridge for reading daily paper recommendations from the zotero-arxiv-daily fork.

The fork's GitHub Action writes ``data/recommendations/{date}.json`` and
``data/recommendations/latest.json`` to its repo (see Part 1 of the
integration). This module fetches a snapshot over HTTP — either from a raw
GitHub URL or a local directory — and renders it as a markdown table that an
agent can triage before pulling papers.

Conventions mirror ``webdav_bridge.py`` (dataclass config with a
``.configured`` property) and ``arxiv_bridge._read_list_source`` (requests +
30s timeout, never raise to the caller).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# Where snapshots live on the fork. The base URL is configurable via the
# RECOMMENDATIONS_URL env var so users can point at a private fork, a local
# checkout, or a mirror.
_DEFAULT_BRANCH = "main"
_DEFAULT_FORK_PATH = "Zhouqm-Git/zotero-arxiv-daily"
_DEFAULT_URL = (
    f"https://raw.githubusercontent.com/{_DEFAULT_FORK_PATH}/{_DEFAULT_BRANCH}/data/recommendations"
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class RecommendationsConfig:
    """Base location of the recommendations JSON snapshots.

    ``url`` may be either an ``http(s)`` directory URL (no trailing filename)
    or a local directory path. Defaults to the public fork's raw GitHub path.
    """

    url: str

    @property
    def configured(self) -> bool:
        return bool(self.url)


def _is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def _resolve_snapshot(base: str, date: str | None) -> tuple[str, str] | tuple[Path, str]:
    """Resolve (location, filename) for a snapshot.

    Returns either an HTTP URL or a local Path plus the JSON filename. When
    ``date`` is ``None`` we read ``latest.json``; otherwise ``{date}.json``.
    """
    filename = "latest.json" if date is None else f"{date}.json"
    base = base.rstrip("/")
    if _is_url(base):
        return f"{base}/{filename}", filename
    return Path(base).expanduser() / filename, filename


def fetch_recommendations(base_url: str, date: str | None = None) -> dict | None:
    """Fetch a recommendation snapshot, returning the parsed JSON or None.

    Never raises — network/parse failures are logged and surfaced as None so
    the tool layer can return a friendly error string (matching the
    convention in ``arxiv_bridge._read_list_source``).
    """
    if date is not None:
        if not _DATE_RE.match(date):
            logger.warning("recommendations: invalid date %r (want YYYY-MM-DD)", date)
            return None

    location, filename = _resolve_snapshot(base_url, date)
    try:
        if isinstance(location, str):  # HTTP
            resp = requests.get(
                location, timeout=30, headers={"User-Agent": "Mozilla/5.0"}
            )
            resp.raise_for_status()
            text = resp.text
        else:  # local file
            if not location.is_file():
                logger.warning("recommendations: not found: %s", location)
                return None
            text = location.read_text(encoding="utf-8", errors="replace")
    except requests.RequestException as e:
        logger.warning("recommendations: fetch failed for %s: %s", location, e)
        return None
    except OSError as e:
        logger.warning("recommendations: read failed for %s: %s", location, e)
        return None

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("recommendations: invalid JSON in %s: %s", location, e)
        return None

    # Sanity-check the envelope shape so callers can rely on the fields.
    if not isinstance(data, dict) or "papers" not in data:
        logger.warning("recommendations: unexpected shape in %s (no 'papers' key)", location)
        return None
    return data


def _truncate(text: str | None, limit: int = 140) -> str:
    if not text:
        return ""
    text = " ".join(text.split())  # collapse whitespace/newlines
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_recommendations(data: dict, limit: int = 10, min_score: float = 0.0) -> str:
    """Render a recommendation snapshot as a markdown table for an agent."""
    date = data.get("date", "?")
    generated_at = data.get("generated_at", "?")
    count_total = data.get("count", len(data.get("papers", [])))
    sources = data.get("sources") or []

    papers = list(data.get("papers", []))
    # Filter by min_score (papers with null score always pass — we don't know them).
    filtered = [p for p in papers if p.get("score") is None or p["score"] >= min_score]
    # Sort by score desc; nulls last.
    filtered.sort(key=lambda p: (p.get("score") is None, -(p.get("score") or 0)))
    top = filtered[: limit if limit and limit > 0 else len(filtered)]

    header = (
        f"**Daily arXiv recommendations** · {date} · generated {generated_at} · "
        f"showing {len(top)} of {count_total} (sources: {', '.join(sources) or 'n/a'})"
    )

    if not top:
        return f"{header}\n\n_No papers match the filter (min_score={min_score})._"

    lines = [
        header,
        "",
        "| # | Score | arXiv ID | Title | TL;DR |",
        "|---|-------|----------|-------|-------|",
    ]
    for i, p in enumerate(top, 1):
        score = p.get("score")
        score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "—"
        arxiv_id = p.get("arxiv_id") or "—"
        title = _truncate(p.get("title"), 90)
        tldr = _truncate(p.get("tldr") or p.get("abstract"), 140)
        lines.append(f"| {i} | {score_str} | `{arxiv_id}` | {title} | {tldr} |")

    lines.append("")
    lines.append(
        "_To pull a paper, call `arxiv_ingest_paper(arxiv_id=..., collections=[...])` "
        "— ask the user about collection organization first (see ingest skill)._"
    )
    return "\n".join(lines)
