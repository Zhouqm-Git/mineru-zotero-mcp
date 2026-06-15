"""Local MinerU quota estimation.

MinerU's /api/v4 exposes no quota-query endpoint (verified: 12 candidate paths
all return 404/401). So we estimate locally by scanning the vault's parsed
meta.json files and summing page counts, then comparing against the documented
limits from mineru.md:

  - Daily high-priority quota: 1000 pages (mineru.md:42)
  - Per-file: ≤ 200 pages, ≤ 200 MB (mineru.md:30-31)
  - Per batch request: ≤ 50 files (mineru.md:219)

This is an ESTIMATE of local usage, not the cloud-side remaining balance — but
it's enough to warn before a large batch would blow through the daily quota.

The "today" window is defined by the cache_at timestamp in meta.json (UTC date),
since that's when MinerU actually did the work.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Documented limits (mineru.md).
DAILY_HIGH_PRIORITY_PAGES = 1000
MAX_PAGES_PER_FILE = 200
MAX_FILE_SIZE_MB = 200
MAX_FILES_PER_BATCH = 50


@dataclass
class QuotaReport:
    """Result of a local quota scan."""

    # Pages parsed today (UTC), counted only for non-cached runs.
    today_pages: int
    # Pages parsed in the last 7 days (context, not a hard limit).
    week_pages: int
    # Total pages across the whole vault.
    total_pages: int
    # Number of papers in the vault.
    paper_count: int
    # List of (doc_id, pages, cached_at_iso) parsed today.
    today_papers: list[tuple[str, int, str]]
    # Estimated remaining high-priority pages today.
    remaining_today: int
    # Whether a batch of `batch_pages` would exceed the daily quota.
    def would_exceed(self, batch_pages: int) -> bool:
        return self.today_pages + batch_pages > DAILY_HIGH_PRIORITY_PAGES


def _utc_date(ts_ms: float) -> str:
    """YYYY-MM-DD in UTC from a millisecond timestamp."""
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def scan_quota(vault_root: str | Path) -> QuotaReport:
    """Scan the vault's .raw/**/meta.json files and summarize usage.

    A paper counts toward "today" if its meta.json cached_at falls on today's
    UTC date AND it was a fresh parse (not a cache hit we re-reported). We
    approximate by counting every meta.json whose cached_at is today — the
    first parse writes meta.json with cached_at=now; re-parses that hit cache
    don't rewrite meta.json, so they don't double-count.
    """
    vault_root = Path(vault_root)
    raw_dir = vault_root / ".raw"
    today_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    now_ms = datetime.now(tz=timezone.utc).timestamp() * 1000.0
    week_ago_ms = now_ms - 7 * 24 * 3600 * 1000.0

    today_pages = 0
    week_pages = 0
    total_pages = 0
    paper_count = 0
    today_papers: list[tuple[str, int, str]] = []

    if not raw_dir.is_dir():
        return QuotaReport(
            today_pages=0, week_pages=0, total_pages=0, paper_count=0,
            today_papers=[], remaining_today=DAILY_HIGH_PRIORITY_PAGES,
        )

    for meta_file in sorted(raw_dir.rglob("meta.json")):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue

        doc_id = meta.get("doc_id") or meta_file.parent.relative_to(raw_dir).as_posix()
        pages = int(meta.get("page_count", 0) or 0)
        cached_at = float(meta.get("cached_at", 0) or 0)
        paper_count += 1
        total_pages += pages

        if cached_at > 0:
            day = _utc_date(cached_at)
            if day == today_utc:
                today_pages += pages
                today_papers.append((doc_id, pages, day))
            if cached_at >= week_ago_ms:
                week_pages += pages

    return QuotaReport(
        today_pages=today_pages,
        week_pages=week_pages,
        total_pages=total_pages,
        paper_count=paper_count,
        today_papers=today_papers,
        remaining_today=max(0, DAILY_HIGH_PRIORITY_PAGES - today_pages),
    )


def estimate_batch_pages(item_keys: list[str], *, pages_per_paper: int | None = None) -> int:
    """Rough estimate of total pages a batch will consume.

    Without per-PDF page counts up front (we'd need to open each PDF), we use a
    conservative default of 20 pages/paper when `pages_per_paper` is None. The
    caller can pass a better estimate if known.
    """
    if not item_keys:
        return 0
    per = pages_per_paper if pages_per_paper is not None and pages_per_paper > 0 else 20
    return len(item_keys) * per


def format_quota_advice(report: QuotaReport, proposed_batch_pages: int | None = None) -> str:
    """Human-readable quota summary + optional advice for a proposed batch."""
    lines = [
        "# MinerU quota estimate (local)",
        "",
        f"- papers in vault:    {report.paper_count}",
        f"- pages parsed today: {report.today_pages} / {DAILY_HIGH_PRIORITY_PAGES} "
        f"(high-priority daily limit)",
        f"- pages last 7 days:  {report.week_pages}",
        f"- pages all-time:     {report.total_pages}",
        f"- remaining today:    {report.remaining_today}",
        "",
        f"Per-file limits: ≤{MAX_PAGES_PER_FILE} pages, ≤{MAX_FILE_SIZE_MB} MB. "
        f"Per-batch: ≤{MAX_FILES_PER_BATCH} files.",
    ]
    if report.today_papers:
        lines.append("")
        lines.append("Parsed today:")
        for ck, p, day in report.today_papers:
            lines.append(f"  - {ck}: {p} pages ({day})")

    if proposed_batch_pages is not None and proposed_batch_pages > 0:
        lines.append("")
        would = report.would_exceed(proposed_batch_pages)
        after = report.today_pages + proposed_batch_pages
        verb = "WOULD EXCEED" if would else "fits within"
        lines.append(
            f"Proposed batch (~{proposed_batch_pages} pages): {verb} the daily quota "
            f"({after}/{DAILY_HIGH_PRIORITY_PAGES})."
        )
        if would:
            over = after - DAILY_HIGH_PRIORITY_PAGES
            lines.append(
                f"⚠️  Estimated {over} pages over the high-priority limit. Pages beyond "
                f"{DAILY_HIGH_PRIORITY_PAGES}/day run at lower priority (slower) per "
                f"MinerU docs. Consider: split the batch across days, or trim to "
                f"~{max(1, report.remaining_today // 20)} papers at ~20p each."
            )
    return "\n".join(lines)
