"""mineru_check_quota tool.

Local-only quota estimate. Scans the vault's parsed papers and compares today's
page count against MinerU's documented daily high-priority limit (1000 pages).

MinerU exposes no remote quota API, so this is a LOCAL estimate based on what
this vault has already parsed today. Use it before launching a large batch to
avoid blowing through the daily quota and dropping to low-priority (slow) mode.
"""

from __future__ import annotations

from .._app import mcp
from .._ctx import get_vault_root
from ..quota import estimate_batch_pages, format_quota_advice, scan_quota


@mcp.tool(
    name="mineru_check_quota",
    description=(
        "Estimate remaining MinerU quota by scanning the vault's already-parsed "
        "papers. Reports pages parsed today vs the 1000-page/day high-priority "
        "limit, plus per-file and per-batch limits. Optionally pass a proposed "
        "batch size to check whether it would exceed the daily quota and get "
        "trim advice.\n\n"
        "This is a LOCAL estimate (MinerU has no quota-query API); it counts "
        "what this vault submitted today via meta.json timestamps. Run it before "
        "mineru_parse_batch on a large set."
    ),
)
def check_quota_tool(
    proposed_batch_size: int | None = None,
    avg_pages_per_paper: int | None = None,
) -> str:
    vault = get_vault_root()
    report = scan_quota(vault)

    proposed_pages = None
    if proposed_batch_size is not None and proposed_batch_size > 0:
        proposed_pages = estimate_batch_pages(
            ["x"] * proposed_batch_size, pages_per_paper=avg_pages_per_paper
        )

    return format_quota_advice(report, proposed_pages)
