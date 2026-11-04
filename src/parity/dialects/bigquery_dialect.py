"""BigQuery dialect.

Verified against a live BigQuery project (location EU, 2026-11-02): the hash
constant agrees, a full-table checksum over mixed-type rows matches DuckDB byte
for byte (including a 130-column table), and `tests/test_bigquery.py` passes end
to end - identical tables, a changed decimal, a deleted row, an inserted row,
NULL versus '' and NULL versus FALSE. Every expression below was checked against
DuckDB on the live engine rather than taken from the documentation, and several
of them are not what the documentation suggests.

The BigQuery-specific decisions, each confirmed by that run:

- **The hash.** `MD5` returns BYTES, so `TO_HEX` first; BigQuery then casts a
  `0x`-prefixed hex STRING to INT64, giving the same 60-bit prefix as DuckDB's
  hex-cast - `648541476951500027` for 'abc'.
- **No `concat_ws`.** `ARRAY_TO_STRING([...], CHR(31))` instead. It skips NULL
  elements exactly as `concat_ws` skips NULL arguments, and every element has
  already been through `coalesce`, so a flat join is byte-identical to the
  nested `concat_ws` tree the other engines build.
- **DECIMAL needs `ROUND` before `FORMAT`.** `CAST(numeric AS STRING)` drops
  trailing zeros (`1.5`, not `1.500000`), and `FORMAT('%.6f', x)` pads but
  prints `-0.000000` for a value like -0.0000001 that rounds to zero, where
  DuckDB and PostgreSQL print `0.000000`. `ROUND` to the scale first lands on a
  NUMERIC zero, which has no sign.
- **FLOAT builds the shortest repr, which BigQuery does not print.** The
  canonical float form (`canonical_float_text` in base.py) starts from a
  double's shortest round-trip decimal. `CAST(x AS STRING)` never prints 16
  digits and `FORMAT('%.17g')` is not correctly rounded, but
  `CAST(x AS BIGNUMERIC)` is exact to 38 places, so the shortest repr is the
  first of its 15-, 16- and 17-significant-digit roundings that parses back to
  x. Verified identical to the Python spec, and so to PostgreSQL, DuckDB and
  MySQL, on 1,565 adversarial and 10,000 random doubles. See `normalize`.
- **Years below 1000.** `%Y` renders year 999 as `999`; DuckDB and PostgreSQL
  zero-pad to `0999`. `%E4Y` pads.
- **TIMESTAMP is an instant, DATETIME is naive.** Both map to TIMESTAMP, but the
  former is formatted in UTC explicitly (BigQuery has no session timezone to
  pin) and is marked `tz_aware`, because its name lacks the `with time zone`
  the engine otherwise looks for.
- **Integer literals past INT64 do not parse.** The key span of a range as wide
  as bigint is up to 2^64, so `int_literal` emits those as `BIGNUMERIC '...'`.
- **Identifiers and strings escape with a backslash.** Backtick-quoted names
  and string literals both process `\\` escapes; neither doubles a quote.
- **One snapshot, for base tables.** Every SELECT against a base table reads it
  `FOR SYSTEM_TIME AS OF` the instant of the dialect's first metadata query, so
  the whole walk sees one version of a live table (CLAUDE.md section 4.9).
  Views are *not* pinned: BigQuery silently ignores `FOR SYSTEM_TIME AS OF` on
  a view - verified, it returned the live row count - so a view is read live at
  every level, the same READ COMMITTED limitation Snowflake has. A table
  clone honours the clause and is pinned like a base table (verified, RT-HB5);
  a table snapshot is immutable, so it is read as it is, with no caveat.
- **Read-only by construction.** Credentials are requested with the
  `bigquery.readonly` OAuth scope and queries go through `jobs.query`. Verified:
  SELECT, INFORMATION_SCHEMA and time travel work; INSERT, DELETE, UPDATE,
  MERGE, CREATE, DROP, EXPORT DATA, temp tables, transactions and EXECUTE
  IMMEDIATE are refused by the server ("Need BigQuery or Cloud Platform write
  scope"), inside a multi-statement script as much as alone. A script that
  only reads (DECLARE, a temp function, SELECT) does run - the scope refuses
  writes, not scripts - but the dialect never sends one, and `;` is refused
  in a row filter. Credentials that cannot be down-scoped (a VM's metadata
  server) keep their own scopes; the dialect still issues SELECT only.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs, unquote

from parity.dialects.base import (
    FLOAT_EXACT_INT_FROM,
    FLOAT_MAX_MAGNITUDE_SQL,
    FLOAT_OUT_OF_RANGE_MESSAGE,
    HASH_HEX_CHARS,
    Dialect,
    float_tiny_sql,
    float_zero_text,
)
from parity.types import Column, LogicalType

#: Read-only OAuth scope. Under it the server refuses any DML, DDL or script,
#: so no bug in query building can write to a user's project.
READONLY_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"

#: Table types that honour `FOR SYSTEM_TIME AS OF`: a base table and a table
#: clone (verified live: a row inserted into a clone mid-walk stays invisible).
_PINNABLE = {"BASE TABLE", "CLONE"}

#: Table types that cannot change, so reading them live is one snapshot
#: anyway: a table snapshot is read-only by definition (BigQuery refuses DML
#: on it), and needs neither time travel nor a caveat.
_IMMUTABLE = {"SNAPSHOT"}

#: Query parameters `bigquery://` understands. Anything else is refused rather
#: than ignored: a misspelt `locaton=` silently dropped is a query routed to the
#: wrong region and an error that does not point at the typo.
_URL_PARAMS = {"location"}

_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


def parse_bigquery_url(connection_string: str) -> tuple[str, str, str | None]:
    """Split ``bigquery://project[/dataset][?location=EU]``.

    Returns ``(project, dataset, location)``; dataset is "" when omitted and
    location is None when omitted (then taken from the dataset at connect).
    Parsed by hand rather than with `urlparse`, whose `hostname` lower-cases
    and would mangle a domain-scoped project such as ``example.com:proj``.
    """
    rest = connection_string.split("://", 1)[1] if "://" in connection_string else ""
    path, _, query = rest.partition("?")
    if "@" in path or "%40" in path.lower():
        # `user:password@project` would otherwise become the project id, and
        # the client sends that in a request URL that Google's error echoes
        # back (RT-F4). No project or dataset id can hold `@`, and BigQuery
        # takes no credentials in the URL at all, so refuse - without quoting
        # the string, which holds the secret.
        raise ValueError(
            "bigquery connection string must not contain credentials "
            "(user:password@): BigQuery authenticates through Application "
            "Default Credentials only - bigquery://<project>[/<dataset>]"
        )
    parts = [unquote(p) for p in path.split("/") if p]
    if not parts:
        raise ValueError(
            "bigquery connection string needs a project: "
            "bigquery://<project>[/<dataset>][?location=<region>]"
        )
    if len(parts) > 2:
        raise ValueError(
            f"bigquery connection string has {len(parts)} path parts; "
            "expected bigquery://<project>[/<dataset>]"
        )
    params = parse_qs(query)
    unknown = sorted(set(params) - _URL_PARAMS)
    if unknown:
        raise ValueError(
            f"unknown bigquery connection parameter(s) {unknown}; "
            f"supported: {sorted(_URL_PARAMS)}"
        )
    location = params["location"][0] if "location" in params else None
    return parts[0], parts[1] if len(parts) > 1 else "", location


class BigQueryDialect(Dialect):
    name = "bigquery"
    #: The dataset from the connection string; unqualified tables live there.
    default_schema = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Set up per-instance state; nothing touches the network until connect."""
        super().__init__(*args, **kwargs)
        self.project = ""
        #: The google-cloud-bigquery Client. Typed loosely: the library is an
        #: optional extra and only partly annotated.
        self._client: Any = None
        self._location: str | None = None
        #: The instant every pinned read is taken at, as a literal-ready string.
        #: Set by the first `columns()` call, from the server's own clock.
        self.snapshot: str | None = None
        #: Base tables (project, dataset, table) that read at `snapshot`. Only
        #: base tables: on a view BigQuery ignores time travel without an error.
        self._pinned: set[tuple[str, str, str]] = set()
        #: Tables `columns()` found to be views (or anything else time travel
        #: does not pin), with their table_type: read live at every level,
        #: which `caveats` reports.
        self._live: dict[tuple[str, str, str], str] = {}

    # ---------------------------------------------------------------- setup

    def connect(self, connection_string: str) -> None:  # pragma: no cover - live
        """Open a read-only client for the project.

        Authentication is Application Default Credentials - a service account
        via ``GOOGLE_APPLICATION_CREDENTIALS``, ``gcloud auth
        application-default login``, impersonation, or a VM's identity - so no
        secret ever appears in the connection string. Looking the dataset up
        here proves the credentials work and the dataset exists before any
        diff starts, and supplies its location so every job runs where the
        data is.
        """
        try:
            project, dataset, location = parse_bigquery_url(connection_string)
        except ValueError as exc:
            # `get_dialect` passes a ValueError through untouched, trusting it
            # to name the side already - so it has to.
            raise self._err(str(exc)) from exc

        import google.auth
        from google.cloud import bigquery

        credentials, _ = google.auth.default(scopes=[READONLY_SCOPE])
        self._client = bigquery.Client(
            project=project, credentials=credentials, location=location
        )
        self.project = project
        if dataset:
            self.default_schema = dataset
            found = self._client.get_dataset(f"{project}.{dataset}")
            location = location or found.location
        self._location = location

    def close(self) -> None:  # pragma: no cover - live
        """Close the client's HTTP session."""
        self._client.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run `sql` and return every row as a tuple of Python values.

        `query_and_wait` uses the `jobs.query` endpoint, which is the one that
        accepts the read-only scope; `client.query` goes through `jobs.insert`,
        which refuses it even for a plain SELECT. There is consequently no
        `cancel()`: cancelling a job needs the write scope (verified, 403), and
        read-only is worth more than an interruptible query.
        """
        rows = self._client.query_and_wait(sql, location=self._location)
        return [tuple(row.values()) for row in rows]

    # ------------------------------------------------------------ metadata

    def _resolve(self, table: str) -> tuple[str, str, str]:
        """Split `table`, `dataset.table` or `project.dataset.table`.

        Three parts are accepted here, unlike the other engines, because
        reading another project's dataset (a shared or public one) while
        billing your own is ordinary BigQuery usage. The name may come as the
        console copies it, in backticks - whole (`` `p.d.t` ``) or per part
        (`` `p`.`d`.`t` ``) - and the project may be domain-scoped
        (``example.com:proj``), whose own dot is not a separator (RT-HB7,
        RT-HB10).
        """
        parts = _split_table_name(table)
        if parts is None:
            raise self._err(
                f"table name {table!r} has backticks parity cannot read: quote "
                "the whole name (`project.dataset.table`) or each part "
                "(`project`.`dataset`.`table`), or leave the backticks out"
            )
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2:
            return self.project, parts[0], parts[1]
        if len(parts) == 1:
            if not self.default_schema:
                raise self._err(
                    f"table {table!r} names no dataset, and the connection "
                    "string has none either: use dataset.table, or "
                    "bigquery://<project>/<dataset>"
                )
            return self.project, self.default_schema, parts[0]
        raise self._err(
            f"table name {table!r} has {len(parts)} dot-separated parts; "
            "expected table, dataset.table or project.dataset.table"
        )

    def _columns_catalog(self, schema: str) -> str:
        """The dataset's own INFORMATION_SCHEMA - there is no global one.

        `schema` may arrive as ``project.dataset`` from `columns()` below.
        """
        return f"{self.qualify_parts(*self._split_schema(schema))}.INFORMATION_SCHEMA.COLUMNS"

    def _split_schema(self, schema: str) -> tuple[str, ...]:
        """``dataset`` or ``project.dataset`` into quoted-ready parts."""
        return tuple(schema.split(".")) if "." in schema else (self.project, schema)

    def columns(self, table: str) -> list[Column]:
        """Introspect columns, learn whether the table can be pinned, and fix
        the snapshot instant - all in the one metadata query.

        Joining TABLES gives `table_type`, which decides pinning: only a
        `BASE TABLE` honours `FOR SYSTEM_TIME AS OF`. `current_timestamp()` is
        the server's clock at the start of this job, so the snapshot is "as of
        the first query", the same point PostgreSQL's REPEATABLE READ takes it.
        """
        project, dataset, name = self._resolve(table)
        schema = f"{project}.{dataset}"
        info = self.qualify_parts(project, dataset)
        # `is_hidden`: an ingestion-time partitioned table lists its
        # pseudo-columns (_PARTITIONTIME, _PARTITIONDATE) with is_hidden YES.
        # They are load metadata, not data - a copy of the table elsewhere
        # has no such column, and a reload of the same rows has other values
        # (RT-HB4) - and `select *` leaves them out too.
        sql = (
            "select c.column_name, c.data_type, t.table_type, "
            "format_timestamp('%Y-%m-%d %H:%M:%E6S+00', current_timestamp()) "
            f"from {info}.INFORMATION_SCHEMA.COLUMNS as c "
            f"join {info}.INFORMATION_SCHEMA.TABLES as t "
            "on c.table_name = t.table_name "
            f"where c.table_name = {self.string_literal(name)} "
            "and c.is_hidden = 'NO' "
            "order by c.ordinal_position"
        )
        try:
            rows = self.query(sql)
        except Exception as exc:
            # A missing dataset and a dataset this account cannot see return
            # the same 403 from BigQuery, so the message has to cover both -
            # and so does a dataset in another location than the job (RT-HB8).
            raise self._err(
                f"cannot read dataset {schema} while looking for {table}: it "
                f"does not exist, or this account lacks access to it"
                f"{self._location_hint(project, dataset)} ({exc})"
            ) from exc
        if not rows:
            raise self._err(self._not_found(table, schema, name))
        if self.snapshot is None:
            self.snapshot = str(rows[0][3])
        kind = str(rows[0][2])
        if kind in _PINNABLE:
            self._pinned.add((project, dataset, name))
        elif kind not in _IMMUTABLE:
            self._live[(project, dataset, name)] = kind
        return [_column(str(r[0]), str(r[1])) for r in rows]

    def _location_hint(self, project: str, dataset: str) -> str:  # pragma: no cover - live
        """Name the dataset's location when it is not where jobs run.

        A job runs in one location and cannot read a dataset in another, and
        the server's error is the same "not found or no access" as for a
        missing dataset. Dataset metadata is readable across locations, so ask
        for it and say which `?location=` would work. Best effort: when that
        lookup fails too, the hint says where jobs run and how to change it.
        """
        here = self._location or "the dataset's default location"
        try:
            found = self._client.get_dataset(f"{project}.{dataset}").location
        except Exception:
            return (
                f"; or it is in another location than this connection's jobs "
                f"({here}): add ?location=<its region> to the connection string"
            )
        if found and (self._location or "").lower() != str(found).lower():
            return (
                f"; it is in location {found} and this connection runs jobs in "
                f"{here}: add ?location={found} to the connection string"
            )
        return ""

    def caveats(self, columns: Sequence[Column]) -> list[str]:
        """Say so when the walk is not one snapshot (RT-D7).

        A view ignores `FOR SYSTEM_TIME AS OF` without an error, so it is read
        live at every level - the gap the module docstring documents, which
        CLAUDE.md section 8 says must reach the output, as SQL Server's
        dialect reports its READ COMMITTED fallback.
        """
        if not self._live:
            return []
        names = sorted(
            f"{'.'.join(ref)} ({kind.lower()})" for ref, kind in self._live.items()
        )
        note = (
            f"{' and '.join(names)} is not a base table or a clone, and "
            "BigQuery ignores time travel on it, so it is read live at every "
            "level rather than as one snapshot: rows written during the diff "
            "can be reported as differences that never existed at one point "
            "in time, or missed. Diff the base table, or a snapshot of it, for "
            "a single-snapshot result"
        )
        return [note]

    def quote(self, identifier: str) -> str:
        """Back-quote one identifier, escaping backslash and backtick.

        The injection boundary. Backtick-quoted identifiers process backslash
        escapes, so a name ending in a backslash would otherwise escape the
        closing quote. Verified: a name containing a backtick and SQL parses
        as one identifier (which BigQuery then rejects as an invalid name).
        """
        escaped = identifier.replace("\\", "\\\\").replace("`", "\\`")
        return f"`{escaped}`"

    def qualify_parts(self, *parts: str) -> str:
        """Quote each part of a reference and join them with dots."""
        return ".".join(self.quote(p) for p in parts)

    def qualify(self, table: str) -> str:
        """`project`.`dataset`.`table`, read at the snapshot when it is safe to.

        Appending `FOR SYSTEM_TIME AS OF` here is what pins every checksum and
        fetch to one version without the engine knowing BigQuery exists. A
        table `columns()` never saw is left unpinned - it was not proven to be
        a base table, and on a view the clause is silently ignored.
        """
        ref = self._resolve(table)
        sql = self.qualify_parts(*ref)
        if self.snapshot is not None and ref in self._pinned:
            sql += (
                f" for system_time as of timestamp {self.string_literal(self.snapshot)}"
            )
        return sql

    def string_literal(self, value: str) -> str:
        """A BigQuery string literal: backslash escapes, no quote-doubling.

        `'it''s'` is not an escaped quote in BigQuery, and `\\` starts an escape
        sequence, so the standard-SQL `sql_literal` is wrong here.
        """
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"

    def int_literal(self, value: int) -> str:
        """Digits inside INT64, a BIGNUMERIC literal outside it.

        BigQuery rejects `18446744073709551616` at parse time ("Invalid integer
        literal"), and the key span of a range as wide as bigint is that big.
        """
        if _INT64_MIN <= value <= _INT64_MAX:
            return str(value)
        return f"bignumeric '{value}'"

    # ----------------------------------------------------------- rendering

    def null_sentinel_sql(self) -> str:
        r"""Build `\N` from CHR(92): BigQuery processes backslash escapes in
        string literals, and `'\N'` is an illegal escape (verified)."""
        return "concat(chr(92), 'N')"

    def _concat(self, parts: list[str]) -> str:
        """Join with `ARRAY_TO_STRING` - BigQuery has no `concat_ws`.

        No argument limit, so no nesting. Every part is coalesced and never
        NULL, so this is byte-identical to the nested `concat_ws` tree; verified
        against DuckDB at 130 columns, past the 64-per-call nesting point.
        """
        return f"array_to_string([{', '.join(parts)}], {self.separator_sql()})"

    def normalize(self, column: Column) -> str:
        """Render one column as canonical text, null-safe.

        Every branch ends inside the `coalesce`, so NULL is always the sentinel.
        """
        c = self.quote(column.name)
        t = column.logical_type
        s = self.float_scale
        fmt = f"'%.{s}f'"
        if t is LogicalType.INTEGER:
            expr = f"cast({c} as string)"
        elif t is LogicalType.DECIMAL:
            # ROUND first: FORMAT alone prints -0.0000001 as `-0.000000`, and
            # the other engines print `0.000000`. ROUND is half away from zero,
            # as DuckDB's decimal cast and PostgreSQL's round() are; verified on
            # 312 decimals, halfway cases included.
            expr = f"format({fmt}, round({c}, {s}))"
        elif t is LogicalType.FLOAT:
            # Finite values follow `canonical_float_text` (base.py): below 2^53
            # the shortest round-trip decimal, rounded half away from zero.
            # BigQuery has no shortest-repr function, and neither obvious
            # route builds one: CAST(x AS STRING) is %.15g when that round-
            # trips and %.17g otherwise, never 16 digits (2^45 + 3/128 prints
            # `...832.023`, not `...832.02`), and FORMAT('%.17g') is not even
            # correctly rounded (2^30 + 1/128 printed `...0078124`, exactly
            # `...0078125`; %.15g and %.16g missed 52 and 203 of the sample).
            # What *is* exact is CAST(x AS BIGNUMERIC): x to 38 places, half
            # up, verified on every sample value. So the shortest repr is
            # built from it: round to 15, then 16, then 17 significant digits
            # and keep the first that parses back to x. Each is the closest
            # decimal of its length, and below 2^53 the closest one round-trips
            # whenever any of that length does, so this is exactly the shortest
            # repr. Ties go to even, as Ryu and dtoa break them: 2^50 + 0.25
            # sits exactly between two 17-digit candidates that both round-trip,
            # and every other engine prints `...910.2`, not `...910.3`. Significant digits need the
            # decimal exponent e, read off the exact integer part's length (or
            # the leading zeros below 1) rather than a float log10.
            # Values under 10^-(s+1) are zero at this scale and short-circuit,
            # which also keeps e within BIGNUMERIC's 38 places.
            # From 2^53 every double is an integer, rebuilt exactly from two
            # INT64s; past 2^116 the high half overflows and ERROR() refuses.
            # Inf/NaN cannot reach BIGNUMERIC, so they become the fixed tokens.
            self.float_scale_for(column)  # BIGNUMERIC's 38 places bound it
            xb = f"abs(cast({c} as bignumeric))"
            e = (
                f"(case when {xb} >= 1 then length(cast(trunc({xb}) as string)) - 1 "
                f"else -1 - length(regexp_extract(cast({xb} as string), r'^0[.](0*)')) end)"
            )
            r15, r16, r17 = (
                f"round(cast({c} as bignumeric), {n - 1} - {e}, 'ROUND_HALF_EVEN')"
                for n in (15, 16, 17)
            )
            shortest = (
                f"case when cast({r15} as float64) = {c} then {r15} "
                f"when cast({r16} as float64) = {c} then {r16} else {r17} end"
            )
            two53 = f"{FLOAT_EXACT_INT_FROM}"
            hi = f"trunc({c} / {two53})"
            expr = (
                f"case when is_inf({c}) then (case when {c} > 0 then 'Infinity' "
                f"else '-Infinity' end) "
                f"when is_nan({c}) then 'NaN' "
                f"when abs({c}) < {float_tiny_sql(s)} then '{float_zero_text(s)}' "
                f"when abs({c}) < {two53} "
                f"then format({fmt}, round({shortest}, {s})) "
                f"when abs({c}) < cast('{FLOAT_MAX_MAGNITUDE_SQL}' as float64) "
                f"then format({fmt}, cast(cast({hi} as int64) as bignumeric) * {two53} "
                f"+ cast({c} - {hi} * {two53} as int64)) "
                f"when {c} is not null then error(concat('{FLOAT_OUT_OF_RANGE_MESSAGE}', "
                f"cast({c} as string))) end"
            )
        elif t is LogicalType.BOOLEAN:
            # `when not`, never `else`: an `else` sends NULL to 'false'.
            expr = f"case when {c} then 'true' when not {c} then 'false' end"
        elif t is LogicalType.DATE:
            # %E4Y, not %Y: %Y renders year 999 as `999`, the others `0999`.
            expr = f"format_date('%E4Y-%m-%d', {c})"
        elif t is LogicalType.TIMESTAMP:
            if column.tz_aware is False:
                expr = f"format_datetime('%E4Y-%m-%d %H:%M:%E6S', {c})"
            else:
                # An instant: render in UTC explicitly, since BigQuery has no
                # session timezone to pin (CLAUDE.md section 4.8).
                expr = f"format_timestamp('%E4Y-%m-%d %H:%M:%E6S', {c}, 'UTC')"
        elif t is LogicalType.STRING:
            expr = self.escape_text(f"cast({c} as string)")
        elif t is LogicalType.TIME:
            # %E6S: seconds with six fractional digits always (TIME holds
            # microseconds, no more). '%t' printed `12:34:56.500`.
            expr = f"format_time('%H:%M:%E6S', {c})"
        elif t is LogicalType.BINARY:
            # BINARY_PREFIX + lower-case hex; '%t' printed the bytes as an
            # escaped literal (`\x00`, but `abc` as itself).
            expr = self.concat_sql(self.binary_prefix_sql(), f"to_hex({c})")
        elif t is LogicalType.JSON:
            # TO_JSON_STRING is BigQuery's normal form - keys by code point,
            # the first duplicate kept - compact, with string escaping that is
            # already canonical (verified: lower-case \u00xx, U+007F and `/`
            # raw). Only the layout needs fixing. TO_JSON_STRING of a SQL
            # NULL is the *string* 'null' (found by the conformance sweep), the
            # text of a JSON null - so SQL NULL and JSON null rendered alike, a
            # false match, unless NULL is kept NULL for the coalesce first.
            text = f"if({c} is null, null, to_json_string({c}))"
            expr = self.escape_text(self.json_compact_layout(text))
        else:
            # Anything else (ARRAY, STRUCT, INTERVAL, ...) through
            # FORMAT('%t'), which renders every type - CAST to STRING raises on
            # ARRAY/STRUCT and would kill the diff. But '%t' renders NULL as the
            # *string* 'NULL', which would slip past the coalesce and compare
            # equal to a real 'NULL' on the other side; hence the explicit guard.
            # Arbitrary text (strings inside arrays and structs, JSON), so
            # escaped like STRING.
            expr = self.escape_text(f"if({c} is null, null, format('%t', {c}))")
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    def concat_sql(self, left: str, right: str) -> str:
        """`concat()`, the spelling the sentinel has always used here."""
        return f"concat({left}, {right})"

    def json_compact_layout(self, expr: str) -> str:
        """The three steps of the base docstring, in BigQuery's RE2 spelling;
        REGEXP_REPLACE replaces every match. Verified live."""
        pattern = self.spell_sql(r'(?s)("(?:\\.|[^"\\])*"|[^",:]*)([,:]?)')
        marked = (
            f"regexp_replace({expr}, {pattern}, "
            f"{self.spell_sql(chr(92) + '1' + chr(92) + '2' + chr(1))})"
        )
        return (
            f"replace(replace(replace({marked}, concat(',', chr(1)), ', '), "
            f"concat(':', chr(1)), ': '), chr(1), '')"
        )

    def sample_select(self, column: Column) -> str:
        """Raw values for the sample check; JSON as its own text.

        The client parses a JSON column into Python objects (1.50 becomes the
        float 1.5); TO_JSON_STRING is the stored document, which the
        reference lays out itself - NULL kept NULL, since TO_JSON_STRING
        turns a SQL NULL into the text of a JSON null.
        """
        if column.logical_type is LogicalType.JSON:
            c = self.quote(column.name)
            return f"if({c} is null, null, to_json_string({c}))"
        return super().sample_select(column)

    def json_semantics(self, column: Column) -> str:
        """BigQuery's JSON normal form."""
        return (
            "BigQuery JSON: object keys ordered by code point, the first duplicate "
            "key kept, integers exact to 64 bits and every other number a double "
            "(1.50 becomes 1.5, 1e2 becomes 100.0)"
        )

    def escape_text(self, expr: str) -> str:
        """Escape with the collation stripped first.

        REPLACE honours a column's collation, and under `und:ci` chr(31) is
        ignorable: verified live, `replace(collate(x, 'und:ci'), chr(31), ..)`
        left the chr(31) in place while the backslash was doubled - the
        separator collision again, on exactly the columns a migration marks
        case-insensitive. `collate(x, '')` removes the collation and changes
        no character.
        """
        return super().escape_text(f"collate({expr}, '')")

    def decimal_key_text(self, c: str, column: Column) -> str:
        """NUMERIC/BIGNUMERIC's string cast is already the minimal exact text:
        plain notation, no trailing zeros, whatever the declared scale."""
        return f"cast({c} as string)"

    def hash_expr(self, text_expr: str) -> str:
        """Fold canonical text into a positive 60-bit integer.

        MD5 gives BYTES, so TO_HEX first; a `0x`-prefixed hex STRING then casts
        to INT64. 648541476951500027 for 'abc', verified live.
        """
        return (
            f"cast(concat('0x', substr(to_hex(md5({text_expr})), 1, "
            f"{HASH_HEX_CHARS})) as int64)"
        )

    def int_div(self, numerator: str, denominator: str) -> str:
        """`DIV` truncates toward zero on BIGNUMERIC; `/` would give a float."""
        return f"div({numerator}, {denominator})"

    def wide_int(self, expr: str) -> str:
        """BIGNUMERIC: 76 digits, which the key offset times the bucket count
        cannot overflow even for a key range as wide as bigint."""
        return f"cast(({expr}) as bignumeric)"

    def sum_wide(self, expr: str) -> str:
        """Sum in BIGNUMERIC, 0 for an empty group.

        INT64 raises after ~8 rows of 2^60; NUMERIC would last ~8.6e10 rows,
        BIGNUMERIC ~5e20. Measured at 50M rows the two cost the same, within
        run-to-run noise, so the wider one is free.
        """
        return f"coalesce(sum(cast(({expr}) as bignumeric)), 0)"


def _split_table_name(table: str) -> list[str] | None:
    """A table reference's parts, backticks and domain-scoped projects read.

    Split on dots outside backticks; a backticked part is taken literally,
    its backticks stripped. One backticked part holding the whole name
    (`` `p.d.t` ``, as the console copies it) is read as if unquoted. A bare
    part holding `:` names a domain-scoped project (``example.com:proj``):
    it and the bare parts before it are one project, rejoined by their dots.
    None when a backtick appears anywhere but around a whole part - one is
    never stripped from inside a name.
    """
    parts: list[str] = []
    quoted: list[bool] = []
    i = 0
    while True:
        if table.startswith("`", i):
            end = table.find("`", i + 1)
            if end < 0:
                return None
            parts.append(table[i + 1 : end])
            quoted.append(True)
            i = end + 1
            if i < len(table) and table[i] != ".":
                return None
        else:
            end = table.find(".", i)
            end = len(table) if end < 0 else end
            part = table[i:end]
            if "`" in part:
                return None
            parts.append(part)
            quoted.append(False)
            i = end
        if i >= len(table):
            break
        i += 1  # the dot
    if len(parts) == 1 and quoted[0]:
        return None if "`" in parts[0] else _split_table_name(parts[0])
    for j, part in enumerate(parts):
        if ":" in part and not any(quoted[: j + 1]):
            return [".".join(parts[: j + 1]), *parts[j + 1 :]]
    return parts


def _column(name: str, raw: str) -> Column:
    """Build a Column, marking TIMESTAMP as an instant and DATETIME as naive."""
    t = raw.upper()
    tz: bool | None = None
    if t.startswith("TIMESTAMP"):
        tz = True
    elif t.startswith("DATETIME"):
        tz = False
    return Column(name, map_type_bigquery(raw), raw, tz_aware=tz)


def map_type_bigquery(raw: str) -> LogicalType:
    """Map a BigQuery type name onto a logical category.

    INFORMATION_SCHEMA reports parameterised types as `NUMERIC(12, 2)` and
    `STRING(20)`, and aliases such as SMALLINT as INT64; strip the parameters.
    Composite types (`ARRAY<INT64>`, `STRUCT<...>`) fall through to UNKNOWN.
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
    if t in {"TIMESTAMP", "DATETIME"}:
        return LogicalType.TIMESTAMP
    if t == "STRING":
        return LogicalType.STRING
    if t == "TIME":
        return LogicalType.TIME
    if t == "BYTES":
        return LogicalType.BINARY
    if t == "JSON":
        return LogicalType.JSON
    return LogicalType.UNKNOWN
