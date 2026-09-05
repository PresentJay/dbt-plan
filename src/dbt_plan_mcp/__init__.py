"""MCP server for dbt-plan.

Deliberately a separate package from `dbt_plan`. The analysis core promises to be
offline and synchronous -- `tests/test_invariants.py` fails the build if anything
under `src/dbt_plan/` imports asyncio or a network module -- and an MCP server is
neither. Keeping them apart is what makes that guarantee provable rather than
aspirational.
"""

__all__ = ["__version__"]

from dbt_plan import __version__
