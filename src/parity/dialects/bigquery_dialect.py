"""BigQuery dialect.

DRAFT - NOT YET VERIFIED against a live instance. Every supported dialect earns
that word only after `tests/test_encoding.py` (or the equivalent live checksum
comparison) agrees byte-for-byte with another engine on a real account, because
a tool whose whole claim is that it does not lie must not ship an engine nobody
has run. This file was written from Google Standard SQL documentation, not a
real project, so it stays out of the supported list and out of the `all` extra
until someone points it at BigQuery and the encoding harness passes. The
Snowflake dialect was drafted the same way and then a live run turned up the one
thing docs could not (identifier case-folding); expect BigQuery to do likewise.

The BigQuery-specific decisions, each of which a live encoding run must confirm:

- **No `concat_ws`.** GoogleSQL has no `concat_ws`; `ARRAY_TO_STRING([a, b, ...],
  sep)` joins an array with a separator and, given a `null_text` is omitted,
  drops NULLs - but every element here is already `coalesce`d, so none is NULL.
  With no argument limit it needs no nesting, and a flat join of coalesced parts
  is byte-identical to the other engines' `concat_ws` tree, so the canonical
  text agrees. `_concat` is overridden for this.
- **The 60-bit hash.** BigQuery has no bit-cast and no `CONV`, but `MD5(x)`
  returns the digest as BYTES, `TO_HEX` renders it lowercase, and BigQuery casts
  a `0x`-prefixed hex STRING to INT64 - so
  `CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5(text)), 1, 15)) AS INT64)` is the same
  positive 60-bit prefix DuckDB's hex-cast produces. Must equal
  648541476951500027 for `'abc'`; a test pins it.
- **DECIMAL and FLOAT scale.** GoogleSQL's `NUMERIC`/`BIGNUMERIC` render to
  STRING without a fixed number of decimal places, so `CAST(x AS STRING)` gives
  `1.5`, not `1.500000`. `FORMAT('%.6f', x)` pads to the compared scale and
  matches the other engines' `cast(... as decimal(38,6))`. Non-finite floats are
  special-cased to the fixed tokens with `IS_INF`/`IS_NAN`, like every dialect.
- **TIMESTAMP vs DATETIME.** BigQuery `TIMESTAMP` is an instant (rendered through
  a zone) and `DATETIME` is naive; both map to `TIMESTAMP`, so `normalize` reads
  the raw type to pick `FORMAT_TIMESTAMP(..., 'UTC')` for the former and
  `FORMAT_DATETIME` for the latter. UTC is pinned in the format call rather than
  a session setting, which BigQuery does not have the same way.
- **No multi-statement snapshot.** Each BigQuery query is consistent in itself,
  but there is no REPEATABLE READ across the walk, so - like Snowflake and
  unlike PostgreSQL - a source table mutating mid-diff can give an inconsistent
  result. A documented limitation, not a hidden one.
- **A client library, not a DBAPI connection.** BigQuery has no connection
  string; `connect()` builds a `google.cloud.bigquery.Client` for the project
  and authenticates through Application Default Credentials.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from parity.dialects.base import Dialect, sql_literal
from parity.types import Column, LogicalType


class BigQueryDialect(Dialect):
    name = "bigquery"
    #: BigQuery's unit of grouping is a *dataset*, not a schema; an unqualified
    #: table is looked up in the dataset the connection string names.
    default_schema = ""

    def connect(self, connection_string: str) -> None:  # pragma: no cover
        """Open a BigQuery client for the project.

        URL grammar: ``bigquery://<project>/<default_dataset>`` - the host is the
        GCP project and the first path segment, if any, is the dataset used for
        unqualified table names. Authentication is Application Default
        Credentials (a service-account key via ``GOOGLE_APPLICATION_CREDENTIALS``,
        or ``gcloud auth application-default login``); there is no password in
        the URL, which is why this tool never has to handle one for BigQuery.
        """
        from google.cloud import bigquery

        url = urlparse(connection_string)
        self.project = url.hostname or ""
        parts = [p for p in url.path.split("/") if p]
        if parts:
            self.default_schema = parts[0]
        self._client = bigquery.Client(project=self.project)

    def close(self) -> None:  # pragma: no cover
        """Close the client."""
        self._client.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:  # pragma: no cover
        """Run `sql` and return every row as a tuple of Python values."""
        return [tuple(row.values()) for row in self._client.query(sql).result()]

    def columns(self, table: str) -> list[Column]:
        """Introspect columns from the dataset's INFORMATION_SCHEMA.

        BigQuery's `INFORMATION_SCHEMA.COLUMNS` view lives inside each dataset
        (``\\`project.dataset\\`.INFORMATION_SCHEMA.COLUMNS``), not in one global
        catalogue, so this cannot use the shared `columns()`.
        """
        schema, name = self.split_table(table, self.default_schema)
        rows = self.query(
            f"select column_name, data_type from "
            f"{self.quote(self.project)}.{self.quote(schema)}"
            f".INFORMATION_SCHEMA.COLUMNS "
            f"where table_name = {sql_literal(name)} "
            f"order by ordinal_position"
        )
        if not rows:
            raise self._err(self._not_found(table, schema, name))
        return [Column(str(r[0]), map_type_bigquery(str(r[1])), str(r[1])) for r in rows]

    def qualify(self, table: str) -> str:
        """Quote a `dataset.table` (or bare `table`) into `project.dataset.table`.

        BigQuery references are three-part and back-quoted. The project comes
        from the connection, the dataset from the name or the connection default.
        """
        schema, name = self.split_table(table, self.default_schema)
        return f"{self.quote(self.project)}.{self.quote(schema)}.{self.quote(name)}"

    def quote(self, identifier: str) -> str:
        """Back-quote one identifier. BigQuery names cannot contain a backtick,
        so there is nothing to escape - but strip any, defensively, so the quote
        can never be broken out of."""
        return "`" + identifier.replace("`", "") + "`"

    # ----------------------------------------------------------- rendering

    def separator_sql(self) -> str:
        """ASCII Unit Separator (0x1f). BigQuery spells `chr` as `CHR`."""
        return "chr(31)"

    def null_sentinel_sql(self) -> str:
        r"""Build the `\N` sentinel from `CHR(92)`.

        BigQuery processes backslash escapes in string literals and `\N` is not
        a defined escape, so a literal would raise; `CHR(92)` is a backslash
        unconditionally and returns a STRING, keeping the coalesce text.
        """
        return "concat(chr(92), 'N')"

    def _concat(self, parts: list[str]) -> str:
        """Join rendered columns with `ARRAY_TO_STRING` - BigQuery has no
        `concat_ws`. No argument limit, so no nesting is needed; a flat join of
        coalesced (never-NULL) parts is byte-identical to the other engines'
        `concat_ws` output, which is what keeps the canonical text agreeing."""
        return f"array_to_string([{', '.join(parts)}], {self.separator_sql()})"

    def normalize(self, column: Column) -> str:
        """Render one column as canonical text, null-safe.

        DECIMAL/FLOAT go through `FORMAT('%.<scale>f', ...)` so the value is
        padded to the compared scale exactly as `cast(... as decimal(38,scale))`
        does elsewhere. Non-finite floats are the fixed tokens. TIMESTAMP and
        DATETIME use different format functions because BigQuery keeps them as
        different types, distinguished here by the raw type name.
        """
        c = self.quote(column.name)
        t = column.logical_type
        raw = (column.raw_type or "").upper()
        fmt = f"'%.{self.float_scale}f'"
        if t is LogicalType.INTEGER:
            expr = f"cast({c} as string)"
        elif t is LogicalType.DECIMAL:
            expr = f"format({fmt}, {c})"
        elif t is LogicalType.FLOAT:
            expr = (
                f"case when is_inf({c}) then "
                f"(case when {c} > 0 then 'Infinity' else '-Infinity' end) "
                f"when is_nan({c}) then 'NaN' "
                f"else format({fmt}, {c}) end"
            )
        elif t is LogicalType.BOOLEAN:
            expr = f"case when {c} then 'true' when not {c} then 'false' end"
        elif t is LogicalType.DATE:
            expr = f"format_date('%Y-%m-%d', {c})"
        elif t is LogicalType.TIMESTAMP:
            if raw.startswith("DATETIME"):
                expr = f"format_datetime('%Y-%m-%d %H:%M:%E6S', {c})"
            else:
                expr = f"format_timestamp('%Y-%m-%d %H:%M:%E6S', {c}, 'UTC')"
        else:
            expr = f"cast({c} as string)"
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    def hash_expr(self, text_expr: str) -> str:
        """Fold canonical text into a positive 60-bit integer.

        `MD5` gives the digest as BYTES, `TO_HEX` renders it, and BigQuery casts
        a `0x`-prefixed hex STRING to INT64 - so the first 15 hex characters are
        the same 60-bit prefix the other engines take.
        """
        return f"cast(concat('0x', substr(to_hex(md5({text_expr})), 1, 15)) as int64)"

    def int_div(self, numerator: str, denominator: str) -> str:
        """Truncating integer division. `DIV(a, b)` truncates toward zero, which
        matches the other dialects for the non-negative operands used here; `/`
        would return a float."""
        return f"div({numerator}, {denominator})"

    def wide_int(self, expr: str) -> str:
        """Widen the key offset past 64 bits before the bucket multiply.

        `NUMERIC` holds 38 digits, far beyond `span * n_segments` even when the
        key is hashed into the full bigint range.
        """
        return f"cast({expr} as numeric)"

    def sum_wide(self, expr: str) -> str:
        """Sum row hashes without overflow, returning 0 for an empty group.

        Row hashes reach 2^60; summing millions overflows INT64, so aggregate in
        `BIGNUMERIC` (76 digits), which no realistic row count can exceed.
        """
        return f"ifnull(sum(cast({expr} as bignumeric)), 0)"


def map_type_bigquery(raw: str) -> LogicalType:
    """Map a BigQuery (GoogleSQL) type name onto a logical category.

    BigQuery keeps integers (`INT64`) and decimals (`NUMERIC`/`BIGNUMERIC`) as
    distinct types, so unlike Snowflake no scale lookup is needed.
    """
    t = raw.upper().split("(")[0].strip()
    if t in {"INT64", "INTEGER", "INT", "SMALLINT", "BIGINT", "TINYINT", "BYTEINT"}:
        return LogicalType.INTEGER
    if t in {"NUMERIC", "DECIMAL", "BIGNUMERIC", "BIGDECIMAL"}:
        return LogicalType.DECIMAL
    if t in {"FLOAT64", "FLOAT"}:
        return LogicalType.FLOAT
    if t in {"BOOL", "BOOLEAN"}:
        return LogicalType.BOOLEAN
    if t == "DATE":
        return LogicalType.DATE
    if t.startswith(("TIMESTAMP", "DATETIME")):
        return LogicalType.TIMESTAMP
    if t in {"STRING", "VARCHAR", "CHAR", "TEXT"}:
        return LogicalType.STRING
    return LogicalType.UNKNOWN
