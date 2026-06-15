"""MinerU cloud /api/v4 client.

Replaces CiteFlow's self-hosted adapter (vspdf/src/parse-and-persist.ts:66-97).
This talks directly to https://mineru.net per the API doc in mineru.md:

  Single file (local upload):
    POST /api/v4/file-urls/batch  → get upload URL + batch_id
    PUT  <upload url>             → upload the PDF
    GET  /api/v4/extract-results/batch/{batch_id}  → poll until done

  Single file (remote URL):
    POST /api/v4/extract/task     → get task_id
    GET  /api/v4/extract/task/{task_id}  → poll until done

  Batch (remote URLs):
    POST /api/v4/extract/task/batch  → batch_id
    GET  /api/v4/extract-results/batch/{batch_id}

Defaults reflect the table-as-markdown policy: model_version="vlm",
enable_table=True (see reference-notes.md §③).
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://mineru.net"
DEFAULT_MODEL = "vlm"
DEFAULT_LANGUAGE = "ch"
DEFAULT_POLL_INTERVAL = 3.0
DEFAULT_TIMEOUT_S = 300.0
_BATCH_MAX_FILES = 50


@dataclass
class TaskResult:
    """Outcome of one file in a parse task."""

    state: str  # done | running | pending | failed | converting | ...
    full_zip_url: str | None = None
    markdown_url: str | None = None  # agent lightweight API only
    err_msg: str | None = None
    file_name: str | None = None
    data_id: str | None = None


@dataclass
class ExtractedZip:
    """Contents of a MinerU result zip."""

    full_markdown: str  # full.md content
    content_list: list[dict[str, Any]]  # content_list.json parsed
    images: dict[str, bytes]  # basename → bytes (from images/ prefix)
    raw_names: list[str]  # all entry names, for debugging


class MineruError(RuntimeError):
    """Raised on MinerU API errors that the caller cannot retry around."""


class MineruClient:
    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not token:
            raise MineruError("MINERU_API_TOKEN is required")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    # ─── single file (local PDF) ────────────────────────────────

    def parse_local_file(
        self,
        pdf_path: str | Path,
        *,
        data_id: str | None = None,
        model_version: str = DEFAULT_MODEL,
        enable_table: bool = True,
        enable_formula: bool = True,
        language: str = DEFAULT_LANGUAGE,
        page_ranges: str | None = None,
        callback: str | None = None,
        seed: str | None = None,
        extra_formats: Iterable[str] = (),
    ) -> str:
        """Submit one local PDF via the batch upload endpoint.

        Returns batch_id. Upload happens synchronously inside this call; the
        actual extraction is async (poll with poll_batch).
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.is_file():
            raise MineruError(f"PDF not found: {pdf_path}")

        file_entry: dict[str, Any] = {"name": pdf_path.name}
        if data_id:
            file_entry["data_id"] = data_id
        if page_ranges:
            file_entry["page_ranges"] = page_ranges

        body: dict[str, Any] = {
            "files": [file_entry],
            "model_version": model_version,
            "enable_table": enable_table,
            "enable_formula": enable_formula,
            "language": language,
        }
        if callback:
            body["callback"] = callback
            if seed:
                body["seed"] = seed
        if extra_formats:
            body["extra_formats"] = list(extra_formats)

        url = f"{self._base_url}/api/v4/file-urls/batch"
        resp = self._session.post(url, json=body, timeout=self._timeout_s)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise MineruError(f"Mineru rejected submission: {data.get('msg')}")

        upload_urls: list[str] = data["data"]["file_urls"]
        batch_id: str = data["data"]["batch_id"]
        if not upload_urls:
            raise MineruError("Mineru returned no upload URL")

        # Upload the file bytes (PUT, no Content-Type per mineru.md:215).
        with pdf_path.open("rb") as fh:
            put = requests.put(upload_urls[0], data=fh, timeout=self._timeout_s)
        if put.status_code not in (200, 201):
            raise MineruError(f"Upload failed: HTTP {put.status_code} {put.text[:200]}")

        logger.info("Uploaded %s, batch_id=%s", pdf_path.name, batch_id)
        return batch_id

    # ─── single file (remote URL) ───────────────────────────────

    def parse_remote_url(
        self,
        pdf_url: str,
        *,
        data_id: str | None = None,
        model_version: str = DEFAULT_MODEL,
        enable_table: bool = True,
        enable_formula: bool = True,
        language: str = DEFAULT_LANGUAGE,
        page_ranges: str | None = None,
    ) -> str:
        """Submit a remote PDF URL. Returns task_id."""
        body: dict[str, Any] = {
            "url": pdf_url,
            "model_version": model_version,
            "enable_table": enable_table,
            "enable_formula": enable_formula,
            "language": language,
        }
        if data_id:
            body["data_id"] = data_id
        if page_ranges:
            body["page_ranges"] = page_ranges

        url = f"{self._base_url}/api/v4/extract/task"
        resp = self._session.post(url, json=body, timeout=self._timeout_s)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise MineruError(f"Mineru rejected submission: {data.get('msg')}")
        task_id = data["data"]["task_id"]
        logger.info("Submitted %s, task_id=%s", pdf_url, task_id)
        return task_id

    # ─── batch (remote URLs) ────────────────────────────────────

    def parse_batch_urls(
        self,
        files: list[dict[str, Any]],
        *,
        model_version: str = DEFAULT_MODEL,
        enable_table: bool = True,
        enable_formula: bool = True,
        language: str = DEFAULT_LANGUAGE,
        callback: str | None = None,
        seed: str | None = None,
    ) -> str:
        """Submit ≤50 remote-URL files as one batch. Returns batch_id."""
        if not files:
            raise MineruError("parse_batch_urls requires at least one file")
        if len(files) > _BATCH_MAX_FILES:
            raise MineruError(f"Batch exceeds {_BATCH_MAX_FILES} files: got {len(files)}")

        body: dict[str, Any] = {
            "files": files,
            "model_version": model_version,
            "enable_table": enable_table,
            "enable_formula": enable_formula,
            "language": language,
        }
        if callback:
            body["callback"] = callback
            if seed:
                body["seed"] = seed

        url = f"{self._base_url}/api/v4/extract/task/batch"
        resp = self._session.post(url, json=body, timeout=self._timeout_s)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise MineruError(f"Mineru rejected batch: {data.get('msg')}")
        batch_id = data["data"]["batch_id"]
        logger.info("Submitted batch of %d files, batch_id=%s", len(files), batch_id)
        return batch_id

    # ─── polling ────────────────────────────────────────────────

    def poll_batch(self, batch_id: str) -> list[TaskResult]:
        """One-shot snapshot of every file in a batch."""
        url = f"{self._base_url}/api/v4/extract-results/batch/{batch_id}"
        resp = self._session.get(url, timeout=self._timeout_s)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise MineruError(f"poll_batch failed: {data.get('msg')}")

        items = data.get("data", {}).get("extract_result", []) or []
        results: list[TaskResult] = []
        for it in items:
            results.append(
                TaskResult(
                    state=it.get("state", "unknown"),
                    full_zip_url=it.get("full_zip_url"),
                    err_msg=it.get("err_msg"),
                    file_name=it.get("file_name"),
                    data_id=it.get("data_id"),
                )
            )
        return results

    def wait_for_batch(
        self,
        batch_id: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        interval_s: float = DEFAULT_POLL_INTERVAL,
    ) -> list[TaskResult]:
        """Poll a batch until every file reaches a terminal state (done/failed)."""
        deadline = time.time() + timeout_s
        while True:
            results = self.poll_batch(batch_id)
            if all(r.state in {"done", "failed"} for r in results):
                return results
            if time.time() > deadline:
                raise MineruError(
                    f"Timed out after {timeout_s}s waiting on batch {batch_id}"
                )
            time.sleep(interval_s)

    # ─── zip download + extract ─────────────────────────────────

    def _fetch_zip_bytes(self, zip_url: str) -> bytes:
        """Download the result zip. Tries requests first, falls back to curl.

        Some MinerU CDN endpoints occasionally fail the TLS handshake under
        Python's urllib3 (SSL EOF) while curl succeeds — so we fall back to a
        curl subprocess rather than failing the whole parse.
        """
        try:
            resp = self._session.get(zip_url, timeout=self._timeout_s, stream=True)
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.RequestException as e:
            logger.warning("requests download failed (%s); retrying via curl", e)
            import subprocess
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                subprocess.run(
                    ["curl", "-fsSL", "-o", tmp_path, zip_url],
                    check=True,
                    capture_output=True,
                    timeout=self._timeout_s,
                )
                with open(tmp_path, "rb") as fh:
                    return fh.read()
            except (subprocess.CalledProcessError, FileNotFoundError) as ce:
                raise MineruError(f"zip download failed (requests + curl): {e} / {ce}") from e
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def download_and_extract_zip(self, zip_url: str) -> ExtractedZip:
        """Download a result zip and pull out full.md, content_list.json, images."""
        data = self._fetch_zip_bytes(zip_url)
        buf = io.BytesIO(data)

        full_markdown = ""
        content_list: list[dict[str, Any]] = []
        images: dict[str, bytes] = {}
        raw_names: list[str] = []

        with zipfile.ZipFile(buf) as zf:
            for info in zf.infolist():
                name = info.filename
                raw_names.append(name)
                if info.is_dir():
                    continue
                base = name.rsplit("/", 1)[-1]

                if base == "full.md":
                    full_markdown = zf.read(info).decode("utf-8", errors="replace")
                elif base.endswith("_content_list.json") or base == "content_list.json":
                    try:
                        content_list = json.loads(zf.read(info).decode("utf-8"))
                    except Exception:  # noqa: BLE001 — malformed JSON: leave empty
                        content_list = []
                elif name.startswith("images/") and base.lower().endswith(
                    (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")
                ):
                    images[base] = zf.read(info)

        if not full_markdown:
            logger.warning("Zip %s had no full.md (entries: %s)", zip_url, raw_names[:10])

        return ExtractedZip(
            full_markdown=full_markdown,
            content_list=content_list,
            images=images,
            raw_names=raw_names,
        )
