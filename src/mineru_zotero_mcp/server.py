"""Server entry point.

Thin shell: importing this module triggers `import mineru_zotero_mcp.tools`,
which registers all @mcp.tool definitions via side-effect — exactly the pattern
used by zotero-mcp (see zotero-mcp/src/zotero_mcp/server.py:16).
"""

# Side-effect: import the tools package so every @mcp.tool decorator runs.
import mineru_zotero_mcp.tools  # noqa: F401
from mineru_zotero_mcp._app import mcp


def main() -> None:
    """Console script entry point (declared in pyproject [project.scripts])."""
    mcp.run()


if __name__ == "__main__":
    main()
