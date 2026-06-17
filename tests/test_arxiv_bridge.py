"""Tests for arxiv_bridge: ID normalization, awesome-list parsing, GitHub URL conversion."""

from __future__ import annotations

from pathlib import Path

from mineru_zotero_mcp.arxiv_bridge import (
    _github_to_raw,
    normalize_arxiv_id,
    parse_awesome_list,
)


class Fail(SystemExit):
    pass


def assert_eq(label, expected, actual):
    if expected != actual:
        raise Fail(f"FAIL {label}: expected {expected!r}, got {actual!r}")
    print(f"OK   {label}")


# ── normalize_arxiv_id ───────────────────────────────────────────────────────

def test_normalize_bare_id():
    assert_eq("bare new-style id", "2605.25874", normalize_arxiv_id("2605.25874"))


def test_normalize_abs_url():
    assert_eq("abs url", "2605.25874",
              normalize_arxiv_id("https://arxiv.org/abs/2605.25874"))


def test_normalize_pdf_url_with_version():
    assert_eq("pdf url v2", "2605.25874",
              normalize_arxiv_id("https://arxiv.org/pdf/2605.25874v2"))


def test_normalize_old_style_id():
    assert_eq("old-style id", "cs.AI/0701234",
              normalize_arxiv_id("cs.AI/0701234"))


def test_normalize_invalid():
    assert_eq("garbage → None", None, normalize_arxiv_id("not-an-arxiv-id"))


def test_normalize_embedded_in_text():
    assert_eq("embedded in sentence", "2605.25874",
              normalize_arxiv_id("see paper arxiv.org/abs/2605.25874 for details"))


# ── _github_to_raw ───────────────────────────────────────────────────────────

def test_github_repo_root_to_raw():
    url = _github_to_raw("https://github.com/knightnemo/Awesome-World-Models")
    assert_eq("repo root → raw main README",
              "https://raw.githubusercontent.com/knightnemo/Awesome-World-Models/main/README.md",
              url)


def test_github_blob_url_to_raw():
    url = _github_to_raw("https://github.com/user/repo/blob/main/docs/papers.md")
    assert_eq("blob url → raw file",
              "https://raw.githubusercontent.com/user/repo/main/docs/papers.md",
              url)


def test_github_with_git_suffix():
    url = _github_to_raw("https://github.com/user/repo.git")
    assert_eq(".git suffix stripped",
              "https://raw.githubusercontent.com/user/repo/main/README.md",
              url)


def test_non_github_url():
    assert_eq("non-github → None", None, _github_to_raw("https://example.com/list.md"))


# ── parse_awesome_list (local file) ──────────────────────────────────────────

def test_parse_local_markdown(tmp_path: Path):
    md = tmp_path / "list.md"
    md.write_text(
        "# Awesome Papers\n\n"
        "## Navigation\n\n"
        "- **Cool Nav Paper** [link](https://arxiv.org/abs/2605.25874)\n"
        "- Another paper arxiv.org/pdf/2501.00001v1\n\n"
        "## Generation\n\n"
        "See also 2301.12345 for older work.\n",
        encoding="utf-8",
    )
    entries = parse_awesome_list(str(md))
    assert_eq("found 3 entries", 3, len(entries))
    assert_eq("first arxiv id", "2605.25874", entries[0].arxiv_id)
    assert_eq("first title from bold", "Cool Nav Paper", entries[0].title)
    assert_eq("third arxiv id", "2301.12345", entries[2].arxiv_id)
    # "See also" is the inline text before the bare arXiv ID — closer than the
    # "Generation" section heading, so it wins as the title.
    assert_eq("inline text beats section heading", "See also", entries[2].title)


def test_parse_deduplicates(tmp_path: Path):
    md = tmp_path / "dup.md"
    md.write_text(
        "arxiv.org/abs/2605.25874\n"
        "also see 2605.25874 again\n",
        encoding="utf-8",
    )
    entries = parse_awesome_list(str(md))
    assert_eq("deduped to 1", 1, len(entries))


def test_parse_empty_file(tmp_path: Path):
    md = tmp_path / "empty.md"
    md.write_text("# No papers here\n\nJust text.\n", encoding="utf-8")
    entries = parse_awesome_list(str(md))
    assert_eq("no arxiv ids → empty", 0, len(entries))


def test_parse_nonexistent_file():
    entries = parse_awesome_list("/nonexistent/path/to/file.md")
    assert_eq("missing file → empty", 0, len(entries))


def main():
    import tempfile
    print("=== test_arxiv_bridge.py ===")
    test_normalize_bare_id()
    test_normalize_abs_url()
    test_normalize_pdf_url_with_version()
    test_normalize_old_style_id()
    test_normalize_invalid()
    test_normalize_embedded_in_text()
    test_github_repo_root_to_raw()
    test_github_blob_url_to_raw()
    test_github_with_git_suffix()
    test_non_github_url()
    with tempfile.TemporaryDirectory() as tmp:
        test_parse_local_markdown(Path(tmp))
        test_parse_deduplicates(Path(tmp))
        test_parse_empty_file(Path(tmp))
    test_parse_nonexistent_file()
    print("\nAll arxiv_bridge tests passed.")


if __name__ == "__main__":
    main()
