"""Vault path conventions, atomic writes, and content-hash cache helpers.

Layout (vault-relative):
    .raw/lib-<libraryID>/<item_key>/<citekey>.md
    .raw/<doc_id>/anchors.json
    .raw/<doc_id>/content.json
    .raw/<doc_id>/meta.json
    attachments/papers/<doc_id>/<image>.png   (figures + fresh captures)
    wiki/sources/zotero/<collection_path>/<citekey>.md  (paper source page; citekey is filename)

`.raw/` is an internal source/cache layer. User-facing paper knowledge lives in
the Zotero-aware wiki tree and embeds only visible attachments under
attachments/papers/<doc_id>/.

Atomic writes use temp-file + os.replace (POSIX-atomic), matching vspdf fs-utils.
The cache key is a content hash of the PDF (md5 of first 1 MB) instead of CiteFlow's
mtime, because mtime lies after rename/copy operations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Keep BBT-safe chars (letters, digits, _ - + .) but collapse every other char,
# then strip path-traversal sequences. A single "." is fine (some citekeys have
# one), but ".." or leading "." would let a malicious name escape its directory.
_UNSAFE = re.compile(r"[^A-Za-z0-9_\-+.]")
_DOT_RUN = re.compile(r"\.{2,}")


def sanitize_citekey(citekey: str) -> str:
    """Make a citekey safe for use as a directory/file name.

    - Replace any non-[A-Za-z0-9_\\-+.] with underscore.
    - Collapse ".."+ to "_" (prevents path traversal inside vault storage).
    - Strip leading/trailing dots and underscores.
    """
    s = _UNSAFE.sub("_", citekey)
    s = _DOT_RUN.sub("_", s)
    s = s.strip("._")
    return s or "unknown"


def sanitize_doc_id(doc_id: str) -> str:
    """Sanitize a vault-relative document id while preserving path hierarchy."""
    parts = [sanitize_citekey(part) for part in str(doc_id).split("/") if part]
    return "/".join(parts) or "unknown"


def make_doc_id(library_id: int | str | None, item_key: str) -> str:
    """Build the canonical internal id for a Zotero item parse.

    The citekey is bibliographic and can collide across libraries. The Zotero
    item key is library-scoped. The internal parse identity is therefore
    library + item, with citekey kept only as human-readable metadata.
    """
    lib = f"lib-{library_id}" if library_id is not None else "lib-unknown"
    return f"{lib}/{sanitize_citekey(item_key)}"


def raw_dir(vault_root: str | Path, doc_id: str) -> Path:
    return Path(vault_root) / ".raw" / sanitize_doc_id(doc_id)


def attachments_dir(vault_root: str | Path) -> Path:
    return Path(vault_root) / "attachments"


def paper_attachments_dir(vault_root: str | Path, doc_id: str) -> Path:
    return attachments_dir(vault_root) / "papers" / sanitize_doc_id(doc_id)


def md_path(vault_root: str | Path, doc_id: str, citekey: str | None = None) -> Path:
    name = sanitize_citekey(citekey or Path(sanitize_doc_id(doc_id)).name)
    return raw_dir(vault_root, doc_id) / f"{name}.md"


def anchors_path(vault_root: str | Path, doc_id: str) -> Path:
    return raw_dir(vault_root, doc_id) / "anchors.json"


def content_path(vault_root: str | Path, doc_id: str) -> Path:
    return raw_dir(vault_root, doc_id) / "content.json"


def meta_path(vault_root: str | Path, doc_id: str) -> Path:
    return raw_dir(vault_root, doc_id) / "meta.json"


def assets_dir(vault_root: str | Path, doc_id: str) -> Path:
    return paper_attachments_dir(vault_root, doc_id)


def to_vault_relative(vault_root: str | Path, abs_path: str | Path) -> str:
    """Return a forward-slash path relative to vault_root."""
    rel = Path(abs_path).relative_to(vault_root)
    return rel.as_posix()


# ─── atomic writes ──────────────────────────────────────────────


def ensure_dir(path: str | Path) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def _write_atomic(path: Path, data: bytes) -> None:
    ensure_dir(path.parent)
    # Named temp in the same dir so os.replace stays on one filesystem.
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path: str | Path, content: str) -> None:
    _write_atomic(Path(path), content.encode("utf-8"))


def write_json(path: str | Path, data: object) -> None:
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


def write_bytes(path: str | Path, data: bytes) -> None:
    _write_atomic(Path(path), data)


# ─── content-hash cache ─────────────────────────────────────────


def pdf_content_hash(pdf_path: str | Path, head_bytes: int = 1024 * 1024) -> str:
    """md5 of the first 1 MB of the PDF. Cheap, stable under rename/copy."""
    h = hashlib.md5()  # noqa: S324 — non-cryptographic cache key
    with open(pdf_path, "rb") as fh:
        h.update(fh.read(head_bytes))
    return h.hexdigest()


def load_meta(vault_root: str | Path, doc_id: str) -> dict | None:
    p = meta_path(vault_root, doc_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def is_cached(
    vault_root: str | Path, doc_id: str, pdf_path: str | Path
) -> dict | None:
    """Return cached meta dict if the PDF content hash matches; else None."""
    meta = load_meta(vault_root, doc_id)
    if not meta:
        return None
    try:
        current_hash = pdf_content_hash(pdf_path)
    except OSError:
        return None
    if meta.get("source_hash") == current_hash:
        return meta
    return None


def now_ms() -> float:
    return time.time() * 1000.0


# ─── manifest (de)serialization ─────────────────────────────────


def load_manifest(vault_root: str | Path, doc_id: str) -> dict | None:
    """Load anchors.json as a raw dict (or None if missing/corrupt)."""
    p = anchors_path(vault_root, doc_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def manifest_from_dict(d: dict):
    """Reconstruct an AnchorManifest object from the persisted dict.

    Centralized here so both capture.py and candidates.py share one builder
    (avoids a circular import between those tool modules).
    """
    from .types import Anchor, AnchorManifest, PageDimension  # local import: types has no deps

    anchors = [
        Anchor(
            anchorId=a["anchorId"],
            kind=a["kind"],
            page=a["page"],
            bbox=tuple(a["bbox"]),
            bboxRaw=tuple(a.get("bboxRaw", a["bbox"])),
            contentIndex=a.get("contentIndex", 0),
            textPreview=a.get("textPreview"),
            textLevel=a.get("textLevel"),
            imagePath=a.get("imagePath"),
            caption=a.get("caption"),
            tableBodyHtml=a.get("tableBodyHtml"),
            markdownTable=a.get("markdownTable"),
            textFormat=a.get("textFormat"),
            listItemCount=a.get("listItemCount"),
        )
        for a in d.get("anchors", [])
    ]
    page_dims = [
        PageDimension(pageIdx=p["pageIdx"], width=p["width"], height=p["height"])
        for p in d.get("pageDimensions", [])
    ]
    return AnchorManifest(
        docId=d.get("docId", ""),
        sourcePdf=d.get("sourcePdf", ""),
        markdownPath=d.get("markdownPath", ""),
        contentListPath=d.get("contentListPath", ""),
        assetsRoot=d.get("assetsRoot", ""),
        pageDimensions=page_dims,
        anchors=anchors,
    )
