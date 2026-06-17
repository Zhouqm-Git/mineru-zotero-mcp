"""WebDAV upload + Zotero local storage repair.

Encapsulates the two-stage sync from zotero_pdf_pipeline.py +
zotero_local_fix.py (493-paper field-tested):

  Stage 1 (WebDAV):  PDF → ZIP+.prop → WebDAV PUT → API PATCH md5/mtime
  Stage 2 (local):   copy PDF → ~/Zotero/storage/{key}/ → SQLite UPDATE

The two stages are independent: WebDAV ensures multi-device sync, local
ensures the current machine can open the PDF immediately (Zotero's own
download-from-WebDAV is unreliable — syncState gets stuck at 1).

Key gotchas baked in (all from arxiv_pdf_download_guide.md):
  - WebDAV PUT MUST send `User-Agent: Zotero/7.0` or cstcloud.cn 403s
  - API PATCH needs If-Unmodified-Since-Version (optimistic concurrency)
  - SQLite columns differ from API field names:
      API md5   → storageHash
      API mtime → storageModTime
  - Zotero must be closed before SQLite write (DB is locked while running)
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import platform
import shutil
import sqlite3
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

ZOTERO_USER_AGENT = "Zotero/7.0"


@dataclass
class WebDAVConfig:
    url: str          # e.g. https://data.cstcloud.cn/dav/zotero/  (trailing slash)
    user: str
    password: str

    @property
    def configured(self) -> bool:
        return bool(self.url and self.user and self.password)


@dataclass
class ZoteroAPIConfig:
    user_id: str
    api_key: str

    @property
    def configured(self) -> bool:
        return bool(self.user_id and self.api_key)

    @property
    def base_url(self) -> str:
        return f"https://api.zotero.org/users/{self.user_id}"


@dataclass
class UploadResult:
    attachment_key: str
    md5: str
    mtime: str
    webdav_ok: bool
    api_patched: bool
    error: str | None = None


@dataclass
class LocalSyncResult:
    ok: bool
    storage_path: Path | None = None
    db_updated: bool = False
    error: str | None = None


# ── Stage 1: create attachment + WebDAV upload ──────────────────────────────

def find_pdf_attachment(api: ZoteroAPIConfig, parent_item_key: str) -> tuple[str, int] | None:
    """Find an existing PDF attachment under a parent item.

    When zotero_add_by_url adds an arXiv paper but the PDF upload fails (quota
    full), the translator still creates an attachment *entry* (with no file).
    This finds it so we can upload to that key instead of creating a duplicate.

    Returns (attachment_key, version) or None if no PDF attachment exists.
    """
    try:
        r = requests.get(
            f"{api.base_url}/items/{parent_item_key}/children",
            headers={"Zotero-API-Key": api.api_key},
            params={"itemType": "attachment", "format": "json"},
            timeout=30,
        )
        r.raise_for_status()
        for item in r.json():
            d = item.get("data", {})
            if d.get("contentType") == "application/pdf":
                return item["key"], item["version"]
    except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
        logger.warning("find_pdf_attachment failed for %s: %s", parent_item_key, e)
    return None


def upload_to_existing_attachment(
    attachment_key: str,
    version: int,
    pdf_path: Path,
    api: ZoteroAPIConfig,
    webdav: WebDAVConfig,
) -> UploadResult:
    """Upload a PDF to an *existing* attachment key (no new entry created).

    Use this when zotero_add_by_url already created the attachment entry but
    the file upload failed (e.g. quota full). Avoids creating duplicate
    attachments under the same parent item.
    """
    uploaded = upload_to_webdav(attachment_key, pdf_path, webdav)
    if not uploaded:
        return UploadResult(
            attachment_key=attachment_key, md5="", mtime="",
            webdav_ok=False, api_patched=False,
            error="WebDAV upload failed",
        )
    md5, mtime = uploaded
    patched = patch_attachment_md5(api, attachment_key, version, md5, mtime)
    return UploadResult(
        attachment_key=attachment_key, md5=md5, mtime=mtime,
        webdav_ok=True, api_patched=patched,
        error=None if patched else "WebDAV uploaded but API PATCH failed",
    )


def create_attachment_entry(
    api: ZoteroAPIConfig,
    parent_item_key: str,
    arxiv_url: str,
    filename: str,
) -> tuple[str, int] | None:
    """Create an imported_url attachment under parent_item_key via Zotero Web API.

    Returns (attachment_key, version) on success, None on failure.
    """
    payload = json.dumps([{
        "itemType": "attachment",
        "parentItem": parent_item_key,
        "linkMode": "imported_url",
        "title": filename,
        "contentType": "application/pdf",
        "url": arxiv_url,
        "tags": [],
    }])
    try:
        r = requests.post(
            f"{api.base_url}/items",
            headers={
                "Zotero-API-Key": api.api_key,
                "Content-Type": "application/json",
            },
            data=payload,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        item = data["successful"]["0"]
        return item["key"], item["version"]
    except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
        logger.error("create_attachment_entry failed: %s", e)
        return None


def upload_to_webdav(
    attachment_key: str,
    pdf_path: Path,
    webdav: WebDAVConfig,
) -> tuple[str, str] | None:
    """Upload PDF as {key}.zip + {key}.prop to WebDAV. Returns (md5, mtime).

    The ZIP must contain the PDF under its original filename (arcname).
    The .prop is XML with mtime (ms) + md5 hash.
    """
    pdf_bytes = pdf_path.read_bytes()
    md5 = hashlib.md5(pdf_bytes).hexdigest()
    mtime = str(int(time.time() * 1000))

    # Build ZIP (arcname = original filename, not the attachment key)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(pdf_path.name, pdf_bytes)

    prop_xml = (
        f'<properties version="1">'
        f"<mtime>{mtime}</mtime><hash>{md5}</hash>"
        f"</properties>"
    )

    base_url = webdav.url.rstrip("/") + "/"
    headers = {
        "User-Agent": ZOTERO_USER_AGENT,  # cstcloud.cn 403s without this
        "Authorization": _basic_auth(webdav.user, webdav.password),
    }

    # Upload .zip
    try:
        r = requests.put(
            f"{base_url}{attachment_key}.zip",
            headers={**headers, "Content-Type": "application/zip"},
            data=buf.getvalue(),
            timeout=180,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        logger.error("WebDAV .zip upload failed for %s: %s", attachment_key, e)
        return None

    # Upload .prop
    try:
        r = requests.put(
            f"{base_url}{attachment_key}.prop",
            headers={**headers, "Content-Type": "text/xml; charset=utf-8"},
            data=prop_xml.encode("utf-8"),
            timeout=30,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        logger.error("WebDAV .prop upload failed for %s: %s", attachment_key, e)
        return None

    return md5, mtime


def patch_attachment_md5(
    api: ZoteroAPIConfig,
    attachment_key: str,
    version: int,
    md5: str,
    mtime: str,
) -> bool:
    """PATCH the attachment's md5+mtime on Zotero cloud (optimistic concurrency)."""
    try:
        r = requests.patch(
            f"{api.base_url}/items/{attachment_key}",
            headers={
                "Zotero-API-Key": api.api_key,
                "Content-Type": "application/json",
                "If-Unmodified-Since-Version": str(version),
            },
            data=json.dumps({"md5": md5, "mtime": mtime}),
            timeout=30,
        )
        return r.status_code in (200, 204)
    except requests.RequestException as e:
        logger.error("PATCH md5/mtime failed for %s: %s", attachment_key, e)
        return False


def full_webdav_sync(
    parent_item_key: str,
    pdf_path: Path,
    arxiv_url: str,
    api: ZoteroAPIConfig,
    webdav: WebDAVConfig,
) -> UploadResult:
    """End-to-end: create attachment → WebDAV upload → PATCH. Returns UploadResult."""
    filename = pdf_path.name

    att = create_attachment_entry(api, parent_item_key, arxiv_url, filename)
    if not att:
        return UploadResult(
            attachment_key="", md5="", mtime="",
            webdav_ok=False, api_patched=False,
            error="failed to create attachment entry",
        )
    att_key, version = att

    uploaded = upload_to_webdav(att_key, pdf_path, webdav)
    if not uploaded:
        return UploadResult(
            attachment_key=att_key, md5="", mtime="",
            webdav_ok=False, api_patched=False,
            error="WebDAV upload failed",
        )
    md5, mtime = uploaded

    patched = patch_attachment_md5(api, att_key, version, md5, mtime)
    return UploadResult(
        attachment_key=att_key, md5=md5, mtime=mtime,
        webdav_ok=True, api_patched=patched,
        error=None if patched else "WebDAV uploaded but API PATCH failed",
    )


# ── Stage 2: local storage repair (SQLite) ──────────────────────────────────

def find_zotero_db() -> Path | None:
    """Locate zotero.sqlite cross-platform.

    macOS:   ~/Zotero/zotero.sqlite
    Linux:   ~/Zotero/zotero.sqlite
    Windows: %APPDATA%\\Zotero\\Zotero\\zotero.sqlite  (best-effort)
    """
    home = Path.home()
    candidates = [
        home / "Zotero" / "zotero.sqlite",
        home / ".zotero" / "zotero.sqlite",
    ]
    if platform.system() == "Windows":
        appdata = Path.home() / "AppData" / "Roaming"
        candidates.append(appdata / "Zotero" / "Zotero" / "zotero.sqlite")

    for c in candidates:
        if c.is_file():
            return c
    return None


def find_zotero_storage() -> Path | None:
    """Locate Zotero storage directory (sibling of zotero.sqlite by default)."""
    db = find_zotero_db()
    if db:
        storage = db.parent / "storage"
        if storage.is_dir():
            return storage
    # Fallback guesses
    home = Path.home()
    for c in [home / "Zotero" / "storage", home / ".zotero" / "storage"]:
        if c.is_dir():
            return c
    return None


def fix_local_storage(
    attachment_key: str,
    pdf_path: Path,
    library_id: int = 1,
    close_zotero: bool = True,
) -> LocalSyncResult:
    """Copy PDF into Zotero local storage + update SQLite so Zotero can open it.

    Steps:
      1. Copy PDF → ~/Zotero/storage/{attachment_key}/{filename}.pdf
      2. (optional) Close Zotero via AppleScript (macOS only)
      3. SQLite UPDATE itemAttachments SET path, syncState=0, storageHash, storageModTime
      4. (optional) Reopen Zotero

    Without close_zotero, the SQLite write will fail if Zotero is running (DB locked).
    """
    storage = find_zotero_storage()
    db_path = find_zotero_db()
    if not storage or not db_path:
        return LocalSyncResult(
            ok=False,
            error=f"Zotero storage/db not found (storage={storage}, db={db_path})",
        )

    # Step 1: copy PDF into storage/{key}/
    att_dir = storage / attachment_key
    att_dir.mkdir(parents=True, exist_ok=True)
    dst = att_dir / pdf_path.name
    if not dst.exists():
        shutil.copy2(pdf_path, dst)

    # Compute hash + mtime for SQLite
    md5 = hashlib.md5(dst.read_bytes()).hexdigest()
    mtime_ms = int(dst.stat().st_mtime * 1000)
    storage_path = f"storage:{pdf_path.name}"

    # Step 2: close Zotero (macOS only)
    closed_by_us = False
    if close_zotero and platform.system() == "Darwin":
        subprocess.run(
            ["osascript", "-e", 'tell application "Zotero" to quit'],
            capture_output=True,
        )
        time.sleep(5)
        closed_by_us = True
    elif close_zotero and platform.system() != "Darwin":
        return LocalSyncResult(
            ok=False,
            error=(
                "close_zotero=True but not on macOS — AppleScript unavailable. "
                "Close Zotero manually, then call again with close_zotero=False."
            ),
        )

    # Step 3: SQLite UPDATE
    db_updated = False
    try:
        # Backup
        backup = db_path.with_suffix(f".sqlite.backup_{int(time.time())}")
        shutil.copy2(db_path, backup)

        conn = sqlite3.connect(str(db_path))
        # linkMode=1 (imported_url) is required for Zotero to treat the file as a
        # syncable WebDAV attachment. linkMode=0 (imported_file) causes Zotero to
        # show a blank PDF because it uses a different file-resolution path.
        # This matches what zotero_add_by_url creates on a successful web import.
        conn.execute(
            "UPDATE itemAttachments SET "
            "path = ?, syncState = 0, linkMode = 1, "
            "storageModTime = ?, storageHash = ? "
            "WHERE itemID = (SELECT itemID FROM items "
            "WHERE key = ? AND libraryID = ?)",
            (storage_path, mtime_ms, md5, attachment_key, library_id),
        )
        conn.commit()

        # Verify
        cur = conn.execute(
            "SELECT syncState, linkMode FROM itemAttachments "
            "WHERE itemID = (SELECT itemID FROM items WHERE key = ? AND libraryID = ?)",
            (attachment_key, library_id),
        )
        row = cur.fetchone()
        db_updated = row is not None and row[0] == 0 and row[1] == 1
        conn.close()
    except sqlite3.Error as e:
        return LocalSyncResult(
            ok=False,
            storage_path=dst,
            error=f"SQLite error: {e}. Zotero may still be running (DB locked).",
        )

    # Step 4: reopen Zotero
    if closed_by_us and platform.system() == "Darwin":
        subprocess.run(["open", "-a", "Zotero"], capture_output=True)

    return LocalSyncResult(
        ok=db_updated,
        storage_path=dst,
        db_updated=db_updated,
        error=None if db_updated else "SQLite UPDATE did not affect expected row",
    )


def _basic_auth(user: str, password: str) -> str:
    """Build HTTP Basic Authorization header value."""
    import base64
    credentials = f"{user}:{password}"
    return "Basic " + base64.b64encode(credentials.encode()).decode()
