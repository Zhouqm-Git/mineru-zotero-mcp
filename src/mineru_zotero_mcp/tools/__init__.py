"""Tools package.

Importing this module imports every tool submodule, which registers all
@mcp.tool definitions via side-effect on the shared `mcp` instance (mirrors
zotero-mcp's tools/__init__.py).
"""

# Order doesn't matter for registration; each module decorates at import time.
from . import anchors, capture, candidates, markdown, parse, quota  # noqa: F401
