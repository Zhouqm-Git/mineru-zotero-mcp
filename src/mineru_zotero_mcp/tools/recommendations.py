"""Daily arXiv recommendation discovery tool.

Reads JSON snapshots persisted by the zotero-arxiv-daily fork's GitHub Action
(``data/recommendations/latest.json`` + per-date history) and returns a ranked
markdown table for agent triage. Pair with ``arxiv_ingest_paper`` to pull
chosen papers — ask the user about collection organization first (see the
paper-wiki ingest skill).
"""

from __future__ import annotations

import logging

from .._app import mcp
from .._ctx import get_recommendations_config
from ..recommendations_bridge import fetch_recommendations, format_recommendations

logger = logging.getLogger(__name__)


@mcp.tool(
    name="arxiv_daily_recommendations",
    description=(
        "Fetch today's (or a given day's) arXiv paper recommendations produced by the "
        "zotero-arxiv-daily recommender, which reranks new arXiv papers against your Zotero "
        "library by embedding similarity and adds LLM-generated TL;DRs. Returns a markdown "
        "table (rank, score, arXiv ID, title, TL;DR) sorted by relevance. "
        "Pass date as YYYY-MM-DD for a historical snapshot, or omit for the latest run. "
        "Use min_score to filter weak matches, limit to cap the list length. "
        "After triage, call arxiv_ingest_paper for chosen IDs — ask the user about collection "
        "organization first when no suitable collection exists."
    ),
)
def arxiv_daily_recommendations_tool(
    date: str | None = None,
    limit: int = 10,
    min_score: float = 0.0,
) -> str:
    """Show today's recommended papers (or a specific date's snapshot)."""
    config = get_recommendations_config()
    if not config.configured:
        return (
            "Error: RECOMMENDATIONS_URL is not set. Point it at the base location of your "
            "zotero-arxiv-daily snapshots — e.g. "
            "https://raw.githubusercontent.com/<user>/zotero-arxiv-daily/main/data/recommendations "
            "(HTTP) or a local directory path."
        )

    data = fetch_recommendations(config.url, date)
    if data is None:
        target = date or "latest"
        return (
            f"No recommendations available for {target} at {config.url}.\n"
            "Possible causes: the daily Action hasn't run yet, the date is out of range, "
            "the fork's REPOSITORY/REF vars are misconfigured, or the URL is wrong."
        )

    return format_recommendations(data, limit=limit, min_score=min_score)
