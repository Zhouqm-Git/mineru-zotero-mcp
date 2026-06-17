"""Tests for recommendations_bridge: URL/path resolution, fetch, formatting."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from mineru_zotero_mcp.recommendations_bridge import (
    RecommendationsConfig,
    _resolve_snapshot,
    fetch_recommendations,
    format_recommendations,
)


class Fail(SystemExit):
    pass


def assert_eq(label, expected, actual):
    if expected != actual:
        raise Fail(f"FAIL {label}: expected {expected!r}, got {actual!r}")
    print(f"OK   {label}")


# ── RecommendationsConfig ────────────────────────────────────────────────────

def test_config_configured_when_url_set():
    assert_eq("configured with url", True, RecommendationsConfig(url="https://x").configured)


def test_config_not_configured_when_empty():
    assert_eq("empty url not configured", False, RecommendationsConfig(url="").configured)


# ── _resolve_snapshot ────────────────────────────────────────────────────────

def test_resolve_url_latest():
    url, fname = _resolve_snapshot("https://raw.githubusercontent.com/u/r/main/data/recommendations", None)
    assert_eq("latest url", "https://raw.githubusercontent.com/u/r/main/data/recommendations/latest.json", url)
    assert_eq("latest fname", "latest.json", fname)


def test_resolve_url_dated():
    url, fname = _resolve_snapshot("https://example.com/recs/", "2026-06-17")
    assert_eq("dated url strips trailing slash", "https://example.com/recs/2026-06-17.json", url)
    assert_eq("dated fname", "2026-06-17.json", fname)


def test_resolve_local_latest(tmp_path: Path):
    loc, fname = _resolve_snapshot(str(tmp_path), None)
    assert_eq("local latest path", tmp_path / "latest.json", loc)
    assert_eq("local latest fname", "latest.json", fname)


def test_resolve_local_dated(tmp_path: Path):
    loc, fname = _resolve_snapshot(str(tmp_path), "2026-06-17")
    assert_eq("local dated path", tmp_path / "2026-06-17.json", loc)


# ── fetch_recommendations ────────────────────────────────────────────────────

def _sample_envelope() -> dict:
    return {
        "date": "2026-06-17",
        "generated_at": "2026-06-17T22:05:00Z",
        "count": 2,
        "sources": ["arxiv"],
        "papers": [
            {"arxiv_id": "2405.14867", "title": "Paper A", "authors": ["X"],
             "abstract": "abs A", "url": "u", "pdf_url": "p", "tldr": "short A",
             "affiliations": [], "score": 8.5, "source": "arxiv"},
            {"arxiv_id": "2605.99999", "title": "Paper B", "authors": ["Y"],
             "abstract": "abs B", "url": "u", "pdf_url": "p", "tldr": "short B",
             "affiliations": [], "score": 6.0, "source": "arxiv"},
        ],
    }


def test_fetch_from_local_file(tmp_path: Path):
    (tmp_path / "latest.json").write_text(json.dumps(_sample_envelope()))
    data = fetch_recommendations(str(tmp_path))
    assert_eq("local fetch count", 2, data["count"])
    assert_eq("local fetch first id", "2405.14867", data["papers"][0]["arxiv_id"])


def test_fetch_missing_local_returns_none(tmp_path: Path):
    assert_eq("missing local -> None", None, fetch_recommendations(str(tmp_path)))


def test_fetch_invalid_json_returns_none(tmp_path: Path):
    (tmp_path / "latest.json").write_text("not json{")
    assert_eq("invalid json -> None", None, fetch_recommendations(str(tmp_path)))


def test_fetch_bad_envelope_returns_none(tmp_path: Path):
    (tmp_path / "latest.json").write_text(json.dumps({"no_papers_key": True}))
    assert_eq("bad envelope -> None", None, fetch_recommendations(str(tmp_path)))


def test_fetch_invalid_date_returns_none(tmp_path: Path):
    # No network call should happen; rejected by the date validator.
    assert_eq("bad date -> None", None, fetch_recommendations(str(tmp_path), "2026/06/17"))


def test_fetch_http_success():
    mock_resp = MagicMock()
    mock_resp.text = json.dumps(_sample_envelope())
    mock_resp.raise_for_status = MagicMock()
    with patch("mineru_zotero_mcp.recommendations_bridge.requests.get", return_value=mock_resp) as m:
        data = fetch_recommendations("https://example.com/recs")
    assert_eq("http fetch called once", 1, m.call_count)
    assert_eq("http fetch url", "https://example.com/recs/latest.json", m.call_args[0][0])
    assert_eq("http fetch count", 2, data["count"])


def test_fetch_http_failure_returns_none():
    import requests as _r
    with patch("mineru_zotero_mcp.recommendations_bridge.requests.get",
               side_effect=_r.RequestException("boom")):
        assert_eq("http error -> None", None, fetch_recommendations("https://example.com/recs"))


# ── format_recommendations ───────────────────────────────────────────────────

def test_format_renders_table_sorted_by_score():
    out = format_recommendations(_sample_envelope(), limit=10)
    # Higher score (A, 8.5) should appear before lower score (B, 6.0).
    idx_a = out.find("Paper A")
    idx_b = out.find("Paper B")
    assert idx_a > 0 and idx_b > 0, "both titles should render"
    assert idx_a < idx_b, "higher-scored paper should come first"
    assert "| Score |" in out, "should have a table header"
    assert "8.50" in out and "6.00" in out, "scores should be formatted"


def test_format_respects_limit():
    out = format_recommendations(_sample_envelope(), limit=1)
    assert "Paper A" in out and "Paper B" not in out, "limit should cap the table"


def test_format_respects_min_score():
    out = format_recommendations(_sample_envelope(), min_score=7.0)
    assert "Paper A" in out and "Paper B" not in out, "min_score should filter"


def test_format_empty_papers_message():
    env = _sample_envelope()
    env["papers"] = []
    env["count"] = 0
    out = format_recommendations(env)
    assert "No papers match" in out, "empty case should show a friendly message"


def test_format_handles_null_score():
    env = _sample_envelope()
    env["papers"][0]["score"] = None
    out = format_recommendations(env, min_score=0.0)
    assert "—" in out, "null score should render as em-dash"
