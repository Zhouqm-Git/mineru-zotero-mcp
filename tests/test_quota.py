"""Tests for quota: local page-count scan + daily-limit estimation."""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mineru_zotero_mcp.quota import (
    DAILY_HIGH_PRIORITY_PAGES,
    estimate_batch_pages,
    format_quota_advice,
    scan_quota,
)


def _now_ms() -> float:
    return datetime.now(tz=timezone.utc).timestamp() * 1000.0


def _write_meta(vault: Path, doc_id: str, pages: int, cached_at_ms: float) -> None:
    """Write a fake meta.json for one paper."""
    d = vault / ".raw" / doc_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({
        "doc_id": doc_id,
        "citekey": doc_id.rsplit("/", 1)[-1],
        "page_count": pages,
        "cached_at": cached_at_ms,
    }))


def test_empty_vault():
    vault = Path(tempfile.mkdtemp())
    report = scan_quota(vault)
    assert report.today_pages == 0
    assert report.paper_count == 0
    assert report.remaining_today == DAILY_HIGH_PRIORITY_PAGES


def test_today_pages_counted():
    vault = Path(tempfile.mkdtemp())
    now = _now_ms()
    _write_meta(vault, "lib-1/A2024", 100, now)
    _write_meta(vault, "lib-2/B2024", 50, now)
    report = scan_quota(vault)
    assert report.today_pages == 150
    assert report.paper_count == 2
    assert report.total_pages == 150
    assert len(report.today_papers) == 2


def test_old_papers_not_in_today():
    vault = Path(tempfile.mkdtemp())
    now = _now_ms()
    two_days_ago = now - 2 * 24 * 3600 * 1000
    _write_meta(vault, "lib-1/OLD2024", 200, two_days_ago)
    _write_meta(vault, "lib-1/NEW2024", 30, now)
    report = scan_quota(vault)
    assert report.today_pages == 30  # only today's
    assert report.total_pages == 230  # all-time
    assert len(report.today_papers) == 1


def test_week_window():
    vault = Path(tempfile.mkdtemp())
    now = _now_ms()
    three_days_ago = now - 3 * 24 * 3600 * 1000
    ten_days_ago = now - 10 * 24 * 3600 * 1000
    _write_meta(vault, "lib-1/RECENT", 40, three_days_ago)
    _write_meta(vault, "lib-1/ANCIENT", 60, ten_days_ago)
    report = scan_quota(vault)
    assert report.week_pages == 40  # only within 7 days


def test_would_exceed():
    vault = Path(tempfile.mkdtemp())
    now = _now_ms()
    _write_meta(vault, "lib-1/BIG", 950, now)
    report = scan_quota(vault)
    assert not report.would_exceed(40)   # 950+40=990 < 1000
    assert report.would_exceed(60)       # 950+60=1010 > 1000


def test_estimate_batch_pages_default():
    assert estimate_batch_pages([]) == 0
    assert estimate_batch_pages(["a", "b", "c"]) == 60  # 3 * 20 default
    assert estimate_batch_pages(["a", "b"], pages_per_paper=30) == 60


def test_format_advice_no_batch():
    vault = Path(tempfile.mkdtemp())
    now = _now_ms()
    _write_meta(vault, "lib-1/X", 100, now)
    report = scan_quota(vault)
    out = format_quota_advice(report)
    assert "100" in out  # today's pages
    assert "1000" in out  # daily limit
    assert "remaining" in out.lower()


def test_format_advice_within_batch():
    vault = Path(tempfile.mkdtemp())
    report = scan_quota(vault)
    out = format_quota_advice(report, proposed_batch_pages=200)
    assert "fits within" in out


def test_format_advice_exceeds_batch_has_warning():
    vault = Path(tempfile.mkdtemp())
    now = _now_ms()
    _write_meta(vault, "lib-1/BIG", 950, now)
    report = scan_quota(vault)
    out = format_quota_advice(report, proposed_batch_pages=200)
    assert "WOULD EXCEED" in out
    assert "trim" in out.lower() or "split" in out.lower()


def test_malformed_meta_skipped():
    vault = Path(tempfile.mkdtemp())
    d = vault / ".raw" / "lib-1" / "BROKEN"
    d.mkdir(parents=True)
    (d / "meta.json").write_text("not json")
    _write_meta(vault, "lib-1/GOOD", 10, _now_ms())
    report = scan_quota(vault)
    assert report.paper_count == 1  # only the good one
