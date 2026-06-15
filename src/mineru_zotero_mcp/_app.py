"""FastMCP application instance and server lifecycle.

Mirrors zotero-mcp's _app.py: a single FastMCP instance, tools registered via
side-effect import in server.py.
"""

import logging
import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

_log_level = os.environ.get("MINERU_ZOTERO_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.WARNING),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)

logger = logging.getLogger("mineru_zotero_mcp")


@asynccontextmanager
async def server_lifespan(server: FastMCP):
    """Lifecycle hook. Reserved for future warm-up (e.g. pre-open Zotero DB)."""
    logger.info("Starting mineru-zotero-mcp server...")
    yield {}
    logger.info("Shutting down mineru-zotero-mcp server.")


# Single shared instance. tools/*.py import `mcp` and decorate with @mcp.tool.
mcp = FastMCP("mineru-zotero", lifespan=server_lifespan)
