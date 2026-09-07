"""parity - prove two tables in two different database engines hold the same data.

The public surface is deliberately small:

    from parity import get_dialect, diff

Everything else is an implementation detail. Note that importing this package
pulls in no database driver; drivers are loaded lazily by ``get_dialect`` so a
DuckDB-only user is never forced to install a PostgreSQL driver.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.2.2"

__all__ = ["__version__", "diff", "get_dialect"]


def __getattr__(name: str) -> Any:
    """Resolve `parity.diff` and `parity.get_dialect` on first use.

    Deferring the import is what keeps `import parity` free of database
    drivers, so a DuckDB-only user is never made to install psycopg.
    """
    # Lazy re-export: keeps `import parity` free of driver imports.
    if name == "get_dialect":
        from parity.dialects.base import get_dialect

        return get_dialect
    if name == "diff":
        from parity.engine import diff

        return diff
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
