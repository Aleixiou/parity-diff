"""Snowflake dialect.

Verified against a live Snowflake account (AWS eu-central-2, 2026-09-07): the
hash constant agrees, a full-table checksum matches DuckDB byte-for-byte over
5,000 mixed-type rows, and `tests/test_snowflake.py` passes end to end - the
identical check, a changed decimal, a deleted row, and the NULL-versus-empty
trap. It was first written against Snowflake's documentation; the live run then
turned up the one thing docs could not: Snowflake upper-cases unquoted
identifiers, so the engine had to match keys and columns case-insensitively
(see `_fold_columns` in engine.py) for a Snowflake table to diff against a
lower-casing engine at all.

The Snowflake-specific decisions, each confirmed by that run:

- **No `CONV` and no bit-cast for the hash.** Snowflake has neither, but
  `MD5_NUMBER_UPPER64(x)` returns the top 64 bits of the digest as an unsigned
  number, and the top 60 bits - the first 15 hex characters the other engines
  fold - are `FLOOR(that / 16)`. That should equal 648541476951500027 for
  `'abc'`; a test must pin it.
- **Integer and decimal both report as `NUMBER`.** `information_schema` tells
  them apart only by `numeric_scale` (0 = integer), so `columns()` reads the
  scale rather than trusting `data_type` - otherwise an integer key would be
  rendered as `42.000000` and never match another engine's `42`.
- **The NULL sentinel is built from `CHR(92)`.** Snowflake interprets
  backslash escapes in string literals, so a literal `'\\N'` is unsafe the same
  way it was on MySQL.
- **One-snapshot isolation is not available.** Snowflake offers only READ
  COMMITTED, so unlike PostgreSQL the walk cannot be pinned to a single
  snapshot; a source table mutating mid-diff can produce an inconsistent
  result. This is a genuine limitation, documented rather than hidden, and
  `caveats` prints it on every run.

Changed after that run from the documentation, then verified live on
2026-11-03 (connector 5.1.0, AWS eu-central-2; `tests/test_snowflake.py`):

- **FLOAT follows the canonical float form** (`_float_text`): the old
  `cast(c as number(38, s))` rounded the exact binary value and disagreed
  with every other engine on 215 of 1,565 sample doubles.
- **No telemetry, no cloud-metadata probes.** The connector's telemetry
  switch is off and platform detection is disabled; connector 5.x still
  flushes a session-telemetry batch at logout, which the README states.
- **TIMESTAMP_TZ / TIMESTAMP_LTZ are instants** (`Column.tz_aware`), and a
  TIMESTAMP_TZ is moved to UTC before it is printed: `TO_CHAR` prints it in
  its *stored* offset, which the session TIMEZONE pin does not touch, so two
  instants hours apart could print the same text.
- **Timestamps finer than microseconds get a caveat**, since FF6 is what
  every other engine can compare.
- **VARIANT / OBJECT / ARRAY render as `TYPEOF` plus `TO_JSON`**, which keeps
  a string's quotes: a plain varchar cast prints the number 1 and the string
  "1" alike.
- **`int_div` divides an exact multiple**, as SQL Server's does: NUMBER
  division rounds its quotient to 6 places, so `floor(n / d)` puts a key
  one bucket high.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import unquote, urlparse

from parity.dialects.base import (
    FLOAT_EXACT_INT_FROM,
    FLOAT_MAX_MAGNITUDE_SQL,
    FLOAT_OUT_OF_RANGE_MESSAGE,
    Dialect,
    float_range_where,
    float_tiny_sql,
    float_zero_text,
    sql_literal,
)
from parity.types import Column, KeySpec, LogicalType

#: Query parameters a `snowflake://` URL may carry. Anything else is refused,
#: never ignored: a dropped parameter is a setting the user believes is in
#: force (a role that limits what is read, a key that replaces the password)
#: and is not - the same rule as MySQL, SQL Server and BigQuery.
URL_PARAMS = ("private_key_file", "role", "warehouse")

#: Where a private key's passphrase is read from. Never the URL: a URL lands
#: in shell history, CI logs and the process list, and the passphrase is the
#: only thing protecting the key file. `<name>_A` / `<name>_B` take
#: precedence for one side, so two Snowflake sides can use two keys.
KEY_PASSPHRASE_ENV = "PARITY_SNOWFLAKE_KEY_PASSPHRASE"  # noqa: S105 - a variable name

#: Session settings pinned at connect, after the login: each one, left to a
#: user or account default, changes what the tool reads or how it renders.
SESSION_PINS = (
    # Timestamps render through this; pin it so two accounts in different
    # regions cannot disagree on the same instant.
    "alter session set timezone = 'UTC'",
    # A user or account ROWS_PER_RESULTSET caps every result - column
    # introspection and checksum buckets - so columns and buckets past it
    # went unseen on both sides: identical (RT-G2). 0 is no limit.
    "alter session set rows_per_resultset = 0",
    # With QUOTED_IDENTIFIERS_IGNORE_CASE on, a quoted identifier folds to
    # upper case, so `"orders"` - found exactly in information_schema - is
    # read as the table ORDERS: the columns of one table, the rows of another,
    # and a real difference read identical (RT-HF2, verified live).
    "alter session set quoted_identifiers_ignore_case = false",
    # With STRICT_JSON_OUTPUT on, TO_JSON prints an SQL NULL inside an ARRAY
    # (`undefined`) as JSON `null`, and NaN / Infinity as null too: three
    # distinct VARIANTs one text (RT-HF6, verified live).
    "alter session set strict_json_output = false",
)

_URL_SHAPE = (
    "snowflake://user:password@account/database/schema?warehouse=WH&role=ROLE "
    "or, for key-pair authentication, "
    "snowflake://user@account/database/schema?private_key_file=/path/key.p8"
)


@dataclass
class SnowflakeURL:
    """The parts of a `snowflake://` URL, password included - never print one."""

    account: str
    user: str
    password: str | None = field(default=None, repr=False)
    database: str | None = None
    schema: str | None = None
    warehouse: str | None = None
    role: str | None = None
    #: A PEM private key for key-pair (JWT) authentication; a path, so it may
    #: be printed. Its passphrase comes from `KEY_PASSPHRASE_ENV`.
    private_key_file: str | None = None


def parse_snowflake_url(connection_string: str) -> SnowflakeURL:
    """Parse ``snowflake://user[:password]@account/database/schema?params``.

    Raises ValueError without the side (`connect` adds it), and no message
    below ever contains the userinfo or a parameter's value: a URL is the one
    place a password is written down, and these errors land in CI logs.
    """
    rest = connection_string.split("://", 1)[1] if "://" in connection_string else ""
    # An unencoded `/`, `?` or `#` in a password ends urlparse's authority
    # early: the start of the password becomes the account (or a "port") and
    # the rest of it the database name, which the server would then echo in
    # "database ... does not exist". An `@` after the authority can only come
    # from such a password (an account, a database or a key path never needs
    # one unencoded), so refuse the URL rather than send any of it anywhere.
    ends = [i for i in (rest.find(c) for c in "/?#") if i >= 0]
    authority_end = min(ends, default=len(rest))
    if "@" in rest[authority_end:]:
        raise ValueError(
            "malformed snowflake URL: percent-encode `/ ? # @ %` in the "
            f"password (`/` as %2F, `@` as %40); expected {_URL_SHAPE}"
        )
    try:
        url = urlparse(connection_string)
        account, username, password = url.hostname, url.username, url.password
    except ValueError:
        # urlparse's own messages ("Invalid IPv6 URL") carry no URL text, but
        # say nothing of it either way.
        raise ValueError(f"malformed snowflake URL; expected {_URL_SHAPE}") from None
    if not account:
        raise ValueError(f"snowflake URL needs an account; expected {_URL_SHAPE}")
    if not username:
        raise ValueError(f"snowflake URL needs a user; expected {_URL_SHAPE}")
    parts = [p for p in url.path.split("/") if p]
    if len(parts) > 2:
        raise ValueError(
            f"snowflake URL path is /database/schema, got {len(parts)} parts; "
            f"expected {_URL_SHAPE}"
        )
    out = SnowflakeURL(
        account=account,
        user=unquote(username),
        password=unquote(password) if password else None,
        database=unquote(parts[0]) if parts else None,
        schema=unquote(parts[1]) if len(parts) > 1 else None,
    )
    # Split by hand rather than with parse_qs, whose `+` means a space: a key
    # file path may hold a `+`, and a path quietly changed is a file not found.
    seen: set[str] = set()
    for item in url.query.split("&") if url.query else []:
        name, _, value = item.partition("=")
        name, value = unquote(name), unquote(value)
        if name == "password":
            raise ValueError(
                "snowflake URL: the password goes in the userinfo "
                "(user:password@account), never in a parameter"
            )
        if name not in URL_PARAMS:
            raise ValueError(
                f"unknown snowflake connection parameter {name!r}: a parameter "
                f"is refused rather than ignored. Supported: {', '.join(URL_PARAMS)}"
            )
        if name in seen:
            raise ValueError(f"snowflake connection parameter {name!r} is given twice")
        seen.add(name)
        if not value:
            raise ValueError(f"snowflake connection parameter {name!r} is empty")
        setattr(out, name, value)
    if out.private_key_file and out.password is not None:
        # Which one would the connector use? Refuse rather than let it pick.
        raise ValueError(
            "snowflake URL has both a password and private_key_file; give one: "
            "user:password@account, or user@account/...?private_key_file=..."
        )
    if not out.private_key_file and out.password is None:
        raise ValueError(
            "snowflake URL has no password and no private_key_file: give "
            "user:password@account, or key-pair authentication with "
            "?private_key_file=/path/key.p8 (passphrase in "
            f"{KEY_PASSPHRASE_ENV})"
        )
    if out.private_key_file:
        out.private_key_file = os.path.expanduser(out.private_key_file)
    return out


def key_passphrase(side: str) -> str | None:
    """The private key's passphrase from the environment, or None if unset.

    The per-side variable wins, so A and B can hold different keys. An empty
    value counts as unset: a variable cleared with `set X=` is not a
    passphrase of no characters.
    """
    for name in (f"{KEY_PASSPHRASE_ENV}_{side}", KEY_PASSPHRASE_ENV):
        value = os.environ.get(name)
        if value:
            return value
    return None


def load_private_key(path: str, passphrase: str | None) -> bytes:
    """Read a PEM RSA private key and return it as unencrypted PKCS#8 DER.

    Loaded here rather than by the connector (`private_key_file`) so every
    failure is one message naming the file and what to do, never a driver
    traceback - and never the key or the passphrase. The connector takes DER
    bytes as `private_key` in every 4.x release. Encryption is read from the
    PEM header first, so a missing or superfluous passphrase is named as such
    rather than guessed from a library message.

    A passphrase given for a key that is not encrypted is refused, not
    ignored: the user believes the key on disk is protected, and it is not -
    or the path names a different file than intended.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        raise ValueError(f"private key file not found: {path}") from None
    except OSError as exc:
        raise ValueError(
            f"private key file {path} could not be read: {exc.strerror}"
        ) from None
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

    if b"-----BEGIN" not in data or b"PRIVATE KEY-----" not in data:
        raise ValueError(
            f"private key file {path} is not a PEM private key (expected "
            "-----BEGIN [ENCRYPTED] PRIVATE KEY-----, as `openssl genrsa` + "
            "`openssl pkcs8` write it)"
        )
    encrypted = b"-----BEGIN ENCRYPTED PRIVATE KEY-----" in data or (
        b"Proc-Type: 4,ENCRYPTED" in data
    )
    if encrypted and passphrase is None:
        raise ValueError(
            f"private key file {path} is encrypted; set {KEY_PASSPHRASE_ENV} "
            "to its passphrase"
        )
    if not encrypted and passphrase is not None:
        raise ValueError(
            f"private key file {path} is not encrypted, but {KEY_PASSPHRASE_ENV} "
            "is set: unset it, or check that the path names the key you meant"
        )
    try:
        key = serialization.load_pem_private_key(
            data, password=passphrase.encode("utf-8") if passphrase else None
        )
    except (ValueError, TypeError):
        # The library's text says "Bad decrypt" or "Could not deserialize";
        # neither carries key material, but neither tells the user more.
        reason = (
            f"wrong passphrase in {KEY_PASSPHRASE_ENV}?"
            if encrypted
            else "the PEM data is damaged or not a private key"
        )
        raise ValueError(
            f"private key file {path} could not be loaded: {reason}"
        ) from None
    if not isinstance(key, RSAPrivateKey):
        raise ValueError(
            f"private key file {path} holds a {type(key).__name__}; Snowflake "
            "key-pair authentication takes an RSA key"
        )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def connect_kwargs(url: SnowflakeURL, private_key: bytes | None) -> dict[str, Any]:
    """The keyword arguments for `snowflake.connector.connect`.

    Kept apart from `connect` so the offline tests can pin exactly what
    reaches the driver - the telemetry switches above all, which must ride on
    every login, password or key.
    """
    kwargs: dict[str, Any] = {
        # CLAUDE.md section 6: no telemetry, ever. The connector reports
        # driver usage (imported packages, connection shape) to Snowflake
        # unless told not to, and does so *during* connect - so the session
        # parameter has to ride in the login request; setting it afterwards
        # would be too late. The server does not list this parameter
        # (it is a client one); the connector's own state is False -
        # verified live. Connector 5.x flushes a session-telemetry batch
        # at logout that no switch stops, hence the `<5` pin (README).
        "session_parameters": {"CLIENT_TELEMETRY_ENABLED": False},
        "log_imported_packages_in_telemetry": False,
        "account": url.account,
        "user": url.user,
        "database": url.database,
        "schema": url.schema,
        "warehouse": url.warehouse,
        "role": url.role,
        "autocommit": True,
    }
    if private_key is not None:
        # Key-pair: the connector signs a JWT with the key. No password is
        # passed at all, so nothing can fall back to password sign-in.
        kwargs["authenticator"] = "SNOWFLAKE_JWT"
        kwargs["private_key"] = private_key
    else:
        kwargs["password"] = url.password
    return kwargs


class SnowflakeDialect(Dialect):
    name = "snowflake"
    #: Set from the connection's schema segment. Snowflake folds unquoted names
    #: to upper case, so an unqualified table is looked up in the connected
    #: schema as stored.
    default_schema = "PUBLIC"

    def connect(self, connection_string: str) -> None:
        """Open a connection, pinned to UTC.

        URL grammar mirrors SQLAlchemy's Snowflake dialect:
        ``snowflake://user:password@account/database/schema?warehouse=wh&role=r``,
        or ``snowflake://user@account/...?private_key_file=/path/key.p8`` for
        key-pair authentication (`parse_snowflake_url`).

        Note the honest gap: Snowflake supports only READ COMMITTED, so there
        is no equivalent of PostgreSQL's REPEATABLE READ - the diff cannot hold
        one snapshot across the walk. UTC is still pinned so timestamps render
        deterministically.
        """
        passphrase = None
        try:
            url = parse_snowflake_url(connection_string)
            private_key = None
            if url.private_key_file:
                passphrase = key_passphrase(self.side)
                private_key = load_private_key(url.private_key_file, passphrase)
        except ValueError as exc:
            # `get_dialect` passes a ValueError through untouched, trusting it
            # to name the side already - so it has to.
            raise self._err(str(exc)) from None

        # CLAUDE.md section 6: no calls anywhere but the warehouse. Connector
        # 5.x probes the AWS, Azure and GCP metadata services at every login
        # (169.254.169.254, metadata.google.internal) to report the platform
        # it runs on; this switch stops it - verified live on 5.1.0, the
        # probes vanish from the driver's log. An explicit setting wins.
        os.environ.setdefault("SNOWFLAKE_DISABLE_PLATFORM_DETECTION", "true")
        import snowflake.connector

        if url.schema:
            self.default_schema = url.schema

        try:
            self._conn = snowflake.connector.connect(**connect_kwargs(url, private_key))
        except Exception as exc:  # noqa: BLE001 - any driver error, redacted
            # The connector's messages are not known to echo a password, but
            # nothing promises they never will, and these errors land in CI
            # logs: strip every secret this connection carries, and drop the
            # chained driver exception (which holds the same text) whenever
            # anything had to go.
            text = str(exc).strip()
            clean = text
            for secret in (url.password, passphrase):
                if secret:
                    clean = clean.replace(secret, "***")
            raise self._err(f"could not connect: {clean}") from (
                exc if clean == text else None
            )
        # Belt and braces: the client-side switch, for anything logged later.
        self._conn.telemetry_enabled = False
        cur = self._conn.cursor()
        try:
            for pin in SESSION_PINS:
                cur.execute(pin)
        finally:
            cur.close()

    def close(self) -> None:  # pragma: no cover
        """Close the connection."""
        self._conn.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:  # pragma: no cover
        """Run `sql` and return every row as a list of tuples."""
        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            # The connector's stubs (5.x) type fetchall() as tuples *or* dicts,
            # because a DictCursor returns dicts. We always use the default
            # cursor, which returns tuples, so this is a type-only narrowing.
            return cast("list[tuple[Any, ...]]", list(cur.fetchall()))
        finally:
            cur.close()

    def qualify(self, table: str) -> str:
        """Quote a table name, always schema-qualified.

        `columns()` introspects an unqualified name in `default_schema` (the
        URL's schema, else PUBLIC). Left unqualified in the data queries,
        Snowflake resolved it in the session's current schema - the user's
        DEFAULT_NAMESPACE - so the columns of one table and the rows of
        another could be compared, and a real difference read identical
        (RT-D2, the Snowflake form of PostgreSQL's search_path gap). Naming
        the schema makes the rows come from the table that was introspected.
        Verified live 2026-11-03: a planted change in PUBLIC.SAME is found
        while the URL's schema holds an identical ENC.SAME.
        """
        schema, name = self.split_table(table, self.default_schema)
        return f"{self.quote(schema)}.{self.quote(name)}"

    def columns(self, table: str) -> list[Column]:
        """Introspect columns, reading numeric_scale to split NUMBER.

        Snowflake reports every integer and decimal as `NUMBER`; only the scale
        distinguishes them, so this cannot use the shared `columns()`.
        """
        schema, name = self.split_table(table, self.default_schema)
        rows = self.query(
            "select column_name, data_type, numeric_scale, datetime_precision "
            "from information_schema.columns "
            f"where table_schema = {sql_literal(schema)} "
            f"and table_name = {sql_literal(name)} "
            "order by ordinal_position"
        )
        if not rows:
            raise self._err(self._not_found(table, schema, name))
        self._note_policies(table, schema, name)
        return [_column(str(r[0]), str(r[1]), r[2], r[3]) for r in rows]

    def _note_policies(self, table: str, schema: str, name: str) -> None:
        """Remember the masking and row access policies on `table`.

        Both are invisible in the data: a masking policy hands the reading
        role the mask (`***`) on both sides, so values that differ compare
        equal, and a row access policy hides rows from both sides, so a
        difference in them is never seen - each read identical with no word
        said (RT-HF4, RT-HF5, verified live). Like PostgreSQL row-level
        security, each becomes a caveat on every run: the tool cannot tell
        whether a policy actually masks or filters for this role (most are
        conditional on the role), only that one is attached. Aggregation and
        projection policies need nothing - Snowflake refuses the tool's
        queries outright. POLICY_REFERENCES lists only the policies on the
        object itself, so for a view those of the tables beneath it are not
        seen, and that is a caveat too; a role that may not read the policy
        list gets a caveat saying the check did not run.
        """
        current = self.query("select current_database()")
        database = current[0][0] if current else None
        if database is None:
            self._policy_notes.append(
                f"{table}: masking and row access policies could not be checked "
                "(no current database)"
            )
            return
        ref = ".".join(self.quote(p) for p in (str(database), schema, name))
        kind = self.query(
            "select table_type from information_schema.tables "
            f"where table_schema = {sql_literal(schema)} "
            f"and table_name = {sql_literal(name)}"
        )
        is_view = bool(kind) and "VIEW" in str(kind[0][0]).upper()
        try:
            rows = self.query(
                "select policy_kind, policy_name, ref_column_name "
                "from table(information_schema.policy_references("
                f"ref_entity_name => {sql_literal(ref)}, "
                f"ref_entity_domain => {sql_literal('view' if is_view else 'table')}))"
            )
        except Exception as exc:  # noqa: BLE001 - any refusal: say so, never guess
            reason = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
            self._policy_notes.append(
                f"{table}: masking and row access policies could not be checked "
                f"({type(exc).__name__}: {reason}). A policy masks values or hides "
                "rows from both sides alike, and the differences it covers are "
                "not seen; check SHOW MASKING POLICIES / POLICY_REFERENCES as an "
                "administrator"
            )
            return
        for policy_kind, policy_name, column in rows:
            kind_text = str(policy_kind).upper()
            if kind_text == "MASKING_POLICY" and column is not None:
                self._masked.append((table, str(column), str(policy_name)))
            elif kind_text == "ROW_ACCESS_POLICY":
                self._policy_notes.append(
                    f"row access policy {policy_name} filters {table}: rows it "
                    "hides from this role are not compared at all, so a "
                    "difference there cannot be seen. Diff as a role the policy "
                    "does not filter for a complete result"
                )
        if is_view:
            self._policy_notes.append(
                f"{table} is a view: masking and row access policies on the "
                "tables it reads are not checked, and one that masks values or "
                "hides rows from this role hides their differences too"
            )

    #: Policy findings per side, filled by `columns` and printed by `caveats`.
    _policy_notes: list[str]
    _masked: list[tuple[str, str, str]]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._policy_notes = []
        self._masked = []
        #: (table, where, columns) whose VARIANT doubles were checked.
        self._doubles_checked: set[tuple[str, str | None, tuple[str, ...]]] = set()

    def quote(self, identifier: str) -> str:
        """Wrap an identifier in double quotes, doubling any it contains.

        The injection boundary - names arrive from the command line. Snowflake
        stores unquoted names upper-cased, so a lower-case `"id"` will not match
        a column created as `ID`; the tool sidesteps this by quoting the exact
        names `columns()` read back.
        """
        return '"' + identifier.replace('"', '""') + '"'

    def check_where(self, where: str) -> None:
        """The shared row-filter check, plus Snowflake's `//` line comment.

        Snowflake reads `//` to the end of the line as a comment, which no
        other engine does, so the shared scan counted the parentheses inside
        it as code: `id < 40 // (` ... closed the wrapper the tool puts round
        the predicate, and the bucket bounds were no longer ANDed in (RT-HF10,
        verified live). Refused outside a quoted region only - the shared
        check has already refused every quoting it cannot read the way
        Snowflake does - so `v <> 'http://x'` still works.
        """
        super().check_where(where)
        if "//" in _unquoted(where):
            raise self._err(
                f"row filter {where!r} contains '//' outside a quoted string "
                "(Snowflake reads it as a comment to the end of the line)"
            )

    def caveats(self, columns: Sequence[Column]) -> list[str]:
        """What makes this side's comparison less than exact, for the output.

        CLAUDE.md section 8: the gaps this module's docstring admits to must
        reach the output too, as SQL Server's dialect does for the same two.
        """
        out = [
            (
                "Snowflake offers only READ COMMITTED, so each query of the walk "
                "sees the table as of its own start, not one snapshot: rows written "
                "during the diff can be reported as differences that never existed "
                "at one point in time, or missed. Diff a quiet table or a clone for "
                "a single-snapshot result"
            )
        ]
        compared = {c.name.casefold() for c in columns}
        for table, column, policy in self._masked:
            if column.casefold() in compared:
                out.append(
                    f"masking policy {policy} is set on column {column!r} of "
                    f"{table}: if it masks values for this role, both sides "
                    "compare the mask and a difference in that column cannot be "
                    "seen. Diff as a role the policy does not mask, or leave the "
                    "column out with --exclude"
                )
        out.extend(self._policy_notes)
        fine = sorted(
            c.name for c in columns if self.holds_sub_micro(c) and not c.sub_micro
        )
        if fine:
            out.append(
                f"columns {fine} can hold nanoseconds; they are compared at "
                "microseconds (FF6: the digits past the sixth are dropped), so a "
                "difference below one microsecond is not seen"
            )
        return out

    # ----------------------------------------------------------- rendering

    def null_sentinel_sql(self) -> str:
        r"""Build the sentinel from CHR(92), not a `\N` literal.

        Snowflake processes backslash escapes in string literals, so a literal
        is unsafe; CHR(92) is a backslash unconditionally and CHR returns a
        varchar (not binary), so the coalesce stays text.
        """
        return "(chr(92) || 'N')"

    def normalize(self, column: Column) -> str:
        """Render one column as canonical text, null-safe.

        DECIMAL casts to NUMBER(38, scale) then to text - so `1.5` becomes
        `'1.500000'`. FLOAT follows the canonical float form every engine
        shares (`_float_text`), verified live.
        """
        c = self.quote(column.name)
        t = column.logical_type
        if t is LogicalType.INTEGER:
            expr = f"cast({c} as varchar)"
        elif t is LogicalType.FLOAT:
            expr = self._float_text(c, self.float_scale_for(column), column)
        elif t is LogicalType.DECIMAL:
            expr = self._decimal_text(c, self.float_scale)
        elif t is LogicalType.BOOLEAN:
            expr = f"case when {c} then 'true' when not {c} then 'false' end"
        elif t is LogicalType.DATE:
            expr = f"to_char({c}, 'YYYY-MM-DD')"
        elif t is LogicalType.TIMESTAMP:
            # FF6 is microseconds, matching the other engines' six digits;
            # `caveats` says so when the column holds more.
            if _base(column.raw_type) == "TIMESTAMP_TZ":
                # A TIMESTAMP_TZ prints in its own stored offset, which the
                # session TIMEZONE pin does not move (it governs LTZ only), so
                # 12:00 +00:00 and 12:00 +05:00 - instants five hours apart -
                # would print alike. Move it to UTC first; the two-argument
                # form converts an instant. Verified live against DuckDB.
                c = f"convert_timezone('UTC', {c})"
            expr = f"to_char({c}, 'YYYY-MM-DD HH24:MI:SS.FF6')"
            if column.sub_micro:
                # Both sides hold sub-microsecond digits (COV-2): nine digits
                # when there are any, as DuckDB TIMESTAMP_NS prints them.
                raw_c = self.quote(column.name)
                nine = f"to_char({c}, 'YYYY-MM-DD HH24:MI:SS.FF9')"
                expr = (
                    f"case when date_part(nanosecond, {raw_c}) % 1000 = 0 "
                    f"then {expr} else {nine} end"
                )
        elif t is LogicalType.TIME:
            # The varchar cast follows TIME_OUTPUT_FORMAT (HH24:MI:SS by
            # default), which drops the fraction: 12:34:56.5 and 12:34:56
            # printed alike - a false match (COV-6). FF6 is six digits always,
            # the rest *truncated* (verified: 23:59:59.9999999 prints
            # `.999999`), as for timestamps; `caveats` says so for TIME(7-9).
            expr = f"to_char({c}, 'HH24:MI:SS.FF6')"
            if column.sub_micro:
                # Both sides hold nanoseconds (COV-2): nine digits when any
                # are past the microsecond, as for timestamps.
                expr = (
                    f"case when date_part(nanosecond, {c}) % 1000 = 0 then {expr} "
                    f"else to_char({c}, 'HH24:MI:SS.FF9') end"
                )
        elif t is LogicalType.BINARY:
            # BINARY_PREFIX + lower-case hex. The varchar cast follows
            # BINARY_OUTPUT_FORMAT (upper-case hex by default, or base64).
            expr = f"{self.binary_prefix_sql()} || lower(hex_encode({c}))"
        elif t is LogicalType.JSON:
            expr = self._json_text(c)
        elif _base(column.raw_type) == "GEOGRAPHY":
            # No varchar cast exists for these three (RT-HF7: the walk died
            # in its first checksum query). WKB carries every coordinate as
            # its exact double; a GEOGRAPHY is always WGS 84.
            expr = f"{self.binary_prefix_sql()} || lower(hex_encode(st_aswkb({c})))"
        elif _base(column.raw_type) == "GEOMETRY":
            # EWKB: the SRID too, so one shape in two systems is a difference.
            expr = f"{self.binary_prefix_sql()} || lower(hex_encode(st_asewkb({c})))"
        elif _base(column.raw_type) == "VECTOR":
            # Its elements as a JSON array. A FLOAT vector holds float32s,
            # and TO_JSON's 16 significant digits tell any two float32s apart
            # (they need 9) - unlike the doubles of RT-HF1.
            expr = self.escape_text(f"to_json(cast({c} as array))")
        else:
            # STRING, and every unmapped type through its varchar cast:
            # arbitrary text, so escaped. Verified live against DuckDB, also
            # under an `en-ci` collation, which REPLACE ignores here.
            expr = self.escape_text(f"cast({c} as varchar)")
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    def _decimal_text(self, c: str, s: int) -> str:
        """A NUMBER rounded half away from zero to `s` places, `s` digits shown.

        It used to be `cast(c as number(38, s))`, which holds only 38 - s
        integer digits: a NUMBER(38,2) value of 36 digits raised "Number out
        of representable range" at the default scale, and a NUMBER(30,2) one
        of 19 at scale 20 - nothing could be compared (RT-HF8). `ROUND(c, s)`
        never widens: its result keeps min(scale, s) places in the column's
        own precision (verified live), and a carry fits in the integer digit
        that dropping places frees. The places it does not print are zeros,
        appended here. Same rounding (half away from zero), same text for
        every value the cast held - `0.000000` for a negative value that
        rounds to zero included - so no checksum moved.
        """
        r = f"cast(round({c}, {s}) as varchar)"
        if s == 0:
            return r
        return (
            f"case when contains({r}, '.') "
            f"then {r} || repeat('0', {s} - (length({r}) - position('.', {r}))) "
            f"else {r} || '.{'0' * s}' end"
        )

    #: VARIANT types whose TO_JSON is the value itself. Any other (DATE, TIME,
    #: TIMESTAMP_*, BINARY) prints as a JSON *string*, which a VARCHAR
    #: spelling it would match, so those keep their type name in front.
    _JSON_NATIVE = (
        "'OBJECT', 'ARRAY', 'VARCHAR', 'INTEGER', 'DECIMAL', 'DOUBLE', "
        "'BOOLEAN', 'NULL_VALUE'"
    )

    def _json_text(self, c: str) -> str:
        """A VARIANT, OBJECT or ARRAY as canonical JSON text.

        A plain varchar cast prints a string without its JSON quotes, so the
        number 1 and the string "1" rendered alike - a false match on the
        very type semi-structured data lands in. TO_JSON keeps the quotes and
        is Snowflake's normal form: keys by code point, compact, numbers as
        stored. Its escaping is canonical except for U+007F, which it writes
        as `\\u007f` (verified live), so that escape is undone first - with
        every escaped backslash parked on chr(2) meanwhile, so a literal
        backslash followed by `u007f` is never mistaken for it. A DATE, TIME,
        TIMESTAMP or BINARY at the top level is not JSON and prints as a
        string, so it keeps its TYPEOF tag, as it did before: never equal to
        a VARCHAR, nor to any other engine's JSON, which cannot hold one.
        Inside an OBJECT or ARRAY such a value is its JSON string, as every
        engine's JSON holds it (documented).
        """
        # Backslashes from code points (RT-D1), never in a literal.
        bs = self.spell_sql("\\\\")
        fixed = (
            f"replace(replace(replace(to_json({c}), {bs}, chr(2)), "
            f"{self.spell_sql(chr(92) + 'u007f')}, chr(127)), chr(2), {bs})"
        )
        canonical = self.escape_text(self.json_compact_layout(fixed))
        return (
            f"case when typeof({c}) in ({self._JSON_NATIVE}) then {canonical} "
            f"when {c} is not null then typeof({c}) || ':' || {self.escape_text(f'to_json({c})')} end"
        )

    def json_compact_layout(self, expr: str) -> str:
        """The three steps of the base docstring. Snowflake's regular
        expressions have no non-capturing group, so the separator is group 3.
        Verified live."""
        pattern = self.spell_sql(r'("(\\.|[^"\\])*"|[^",:]*)([,:]?)')
        marked = (
            f"regexp_replace({expr}, {pattern}, "
            f"{self.spell_sql(chr(92) + '1' + chr(92) + '3' + chr(1))})"
        )
        return (
            f"replace(replace(replace({marked}, ',' || chr(1), ', '), "
            f"':' || chr(1), ': '), chr(1), '')"
        )

    def json_semantics(self, column: Column) -> str:
        """Snowflake's VARIANT normal form."""
        return (
            "Snowflake VARIANT: object keys ordered by code point, duplicate keys "
            "refused on load, integers and decimals exact (1.0 becomes 1, 1.50 "
            "becomes 1.5), doubles in exponent form (1e2 becomes "
            "1.000000000000000e+02)"
        )

    def _float_text(self, c: str, s: int, column: Column) -> str:
        """The canonical text of a double (`canonical_float_text`, base.py).

        Below 2^53 that is the shortest round-trip decimal, rounded half away
        from zero to the scale. Snowflake prints no shortest repr: a varchar
        cast is 15 significant digits and `TO_VARCHAR(x, 'TME')` 16, matching
        it on 444 and 1,039 of the 1,565 sample doubles; the old
        `cast(x as number(38, s))` rounds the exact binary value and disagreed
        on 215. But, verified live on every sample value, `cast(x as
        number(38, q))` *is* the exact binary value rounded half up, and
        `ROUND` takes a per-row scale and a `'HALF_TO_EVEN'` mode. So the
        shortest repr is the first of the 15-, 16- and 17-significant-digit
        roundings that converts back to the same double - the BigQuery and
        SQL Server construction - with ties to even, as Ryu and dtoa break
        them, done by `ROUND` itself.

        Two bands, because NUMBER holds 38 digits: below 1 the exact value to
        37 places; from 1 to 2^53, to 21 (16 integer digits). The decimal
        exponent e is read off the exact value's text, never a float log10.
        Values under 10^-(s+1) are zero at this scale and short-circuit, which
        keeps a 17-digit rounding inside 37 places up to the maximum scale 20.
        From 2^53 every double is an integer and `number(38, 0)` holds it
        exactly; past 2^116 it is refused by a failing cast whose message
        carries the value and the column. Infinity and NaN (Snowflake FLOAT
        holds both, and `NaN = NaN` is true) become the shared tokens.
        """
        x = c
        where = float_range_where(column).replace("'", "''")

        def shortest(q: int, exponent: str) -> str:
            """The shortest repr from the exact value to q places; `exponent`
            computes the decimal exponent e from `{d}`."""
            d = f"cast({x} as number(38,{q}))"
            e = exponent.format(d=d)
            c15, c16, c17 = (
                f"round({d}, {n - 1} - {e}, 'HALF_TO_EVEN')" for n in (15, 16, 17)
            )
            return (
                f"(case when cast({c15} as float) = {x} then {c15} "
                f"when cast({c16} as float) = {x} then {c16} else {c17} end)"
            )

        # Below 1: '0.000123...' - e is 2 minus the position of the first
        # non-zero digit. From 1: the length of the integer part, minus one.
        small = shortest(37, "(2 - regexp_instr(cast(abs({d}) as varchar), '[1-9]'))")
        large = shortest(21, "(length(cast(floor(abs({d})) as varchar)) - 1)")
        frac = "." + "0" * s if s else ""
        return (
            f"case when {x} = 'inf'::float then 'Infinity' "
            f"when {x} = '-inf'::float then '-Infinity' "
            f"when {x} = 'nan'::float then 'NaN' "
            f"when abs({x}) < {float_tiny_sql(s)} then '{float_zero_text(s)}' "
            f"when abs({x}) < 1 "
            f"then cast(cast(round({small}, {s}) as number(38,{s})) as varchar) "
            f"when abs({x}) < {FLOAT_EXACT_INT_FROM} "
            f"then cast(cast(round({large}, {s}) as number(38,{s})) as varchar) "
            f"when abs({x}) < '{FLOAT_MAX_MAGNITUDE_SQL}'::float "
            f"then cast(cast({x} as number(38,0)) as varchar) || '{frac}' "
            f"when {x} is not null then cast(cast('{FLOAT_OUT_OF_RANGE_MESSAGE}' "
            f"|| cast({x} as varchar) || '{where}' as number) as varchar) end"
        )

    def holds_sub_micro(self, column: Column) -> bool:
        """A Snowflake timestamp or time of precision above 6 (the default
        is 9)."""
        return column.logical_type in (
            LogicalType.TIMESTAMP,
            LogicalType.TIME,
        ) and _sub_micro(column.raw_type)

    def sample_select(self, column: Column) -> str:
        """Raw values for the sample check; a nanosecond timestamp as text.

        The connector returns a Python datetime, which holds microseconds
        only, so a timestamp compared past them is read as ISO text with all
        nine digits and its offset instead.
        """
        c = self.quote(column.name)
        if column.logical_type is LogicalType.TIME:
            # All nine digits as text; the reference truncates them unless
            # the column is compared past microseconds.
            return f"to_char({c}, 'HH24:MI:SS.FF9')"
        if column.sub_micro:
            if _base(column.raw_type) in ("TIMESTAMP_TZ", "TIMESTAMP_LTZ"):
                c = f"convert_timezone('UTC', {c})"
                return f"to_char({c}, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF9TZH:TZM')"
            return f"to_char({c}, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF9')"
        if column.logical_type is LogicalType.JSON:
            # The connector returns a VARIANT pretty-printed; TO_JSON is the
            # stored value, behind its TYPEOF so `sample_value` can tell a
            # JSON value from a DATE or BINARY one.
            return f"typeof({c}) || ':' || to_json({c})"
        return super().sample_select(column)

    def sample_value(self, column: Column, value: Any) -> Any:
        """A JSON-native VARIANT's text without its tag; any other VARIANT
        (a DATE, a BINARY, ...) as the tagged text, which is not JSON and so
        stays outside the reference rules - not checked, never guessed."""
        if column.logical_type is LogicalType.JSON and isinstance(value, str):
            tag, _, body = value.partition(":")
            if f"'{tag}'" in self._JSON_NATIVE:
                return body
            return ("not json", value)
        return value

    def decimal_key_text(self, c: str, column: Column) -> str:
        """NUMBER's varchar cast is exact at the declared scale; drop the
        trailing fractional zeros it adds. Verified live against DuckDB."""
        t = f"cast({c} as varchar)"
        return (
            f"case when {c} = 0 then '0' "
            f"when contains({t}, '.') then rtrim(rtrim({t}, '0'), '.') "
            f"else {t} end"
        )

    def normalize_key(self, column: Column) -> str:
        """Exact key text; a TIMESTAMP key keeps any sub-microsecond digits.

        Snowflake timestamps hold nanoseconds and render at FF6, which as a
        key merges distinct keys. One with a sub-microsecond part renders FF9
        instead (the nine digits DuckDB prints for a TIMESTAMP_NS); any other
        renders exactly as before. Verified live against DuckDB TIMESTAMP_NS.
        """
        if column.logical_type is LogicalType.TIMESTAMP:
            c = self.quote(column.name)
            # The instant, as `normalize` prints it: a TIMESTAMP_TZ to_char
            # prints its stored offset's wall clock, so without the move to
            # UTC 12:00:00.000000001 +00:00 and +05:00 were one key, and one
            # instant at two offsets two keys (RT-HF3). LTZ prints in the
            # session zone, pinned to UTC.
            shown = (
                f"convert_timezone('UTC', {c})"
                if _base(column.raw_type) == "TIMESTAMP_TZ"
                else c
            )
            return (
                f"case when {c} is null or date_part(nanosecond, {c}) % 1000 = 0 "
                f"then {self.normalize(column)} "
                f"else to_char({shown}, 'YYYY-MM-DD HH24:MI:SS.FF9') end"
            )
        if column.logical_type is LogicalType.TIME:
            # The same for TIME: a key's nanoseconds are kept.
            c = self.quote(column.name)
            return (
                f"case when {c} is null or date_part(nanosecond, {c}) % 1000 = 0 "
                f"then {self.normalize(column)} "
                f"else to_char({c}, 'HH24:MI:SS.FF9') end"
            )
        return super().normalize_key(column)

    def hash_expr(self, text_expr: str) -> str:
        """Fold canonical text into a positive 60-bit integer.

        `MD5_NUMBER_UPPER64` is the top 64 bits of the digest as an unsigned
        number; the top 60 bits - the first 15 hex characters the other engines
        take - are that floor-divided by 16.
        """
        return f"floor(md5_number_upper64({text_expr}) / 16)"

    def int_div(self, numerator: str, denominator: str) -> str:
        """Exact truncating division on NUMBER operands - never a bare `/`.

        Snowflake gives a NUMBER(38,0) / NUMBER(38,0) quotient six decimal
        places and *rounds* it (`2/3` is 0.666667), so `floor(n / d)` turns a
        quotient of q.9999996 into q + 1 and the row lands in the next bucket
        on this side only - the flaw SQL Server's `int_div` had. `n - mod(n,
        d)` is an exact multiple of `d`, so its quotient is an integer and
        nothing can round; the operands are non-negative, so this is floor.
        Verified live: plain `/` gave 10000000 for 9999999.99999999...
        """
        n, d = f"({numerator})", f"({denominator})"
        return f"cast(({n} - mod({n}, {d})) / {d} as number(38,0))"

    def segment_checksums(
        self,
        table: str,
        key: KeySpec,
        columns: Sequence[Column],
        lo: int,
        hi: int,
        n_segments: int,
        where: str | None = None,
    ) -> dict[int, tuple[int, int]]:
        """The shared checksums, after the VARIANT double check."""
        self._check_variant_doubles(table, key, columns, where)
        return super().segment_checksums(table, key, columns, lo, hi, n_segments, where)

    def fetch_range(
        self,
        table: str,
        key: KeySpec,
        columns: Sequence[Column],
        lo: int,
        hi: int,
        where: str | None = None,
    ) -> dict[int | str, tuple[str, ...]]:
        """The shared download, after the VARIANT double check."""
        self._check_variant_doubles(table, key, columns, where)
        return super().fetch_range(table, key, columns, lo, hi, where)

    def _check_variant_doubles(
        self, table: str, key: KeySpec, columns: Sequence[Column], where: str | None
    ) -> None:
        """Refuse a VARIANT holding a double its JSON text does not pin down.

        TO_JSON prints a DOUBLE inside a VARIANT, OBJECT or ARRAY with 16
        significant digits (`1.000000000000000e+00`), and a double needs 17
        to round-trip: 1 and 1.0000000000000002, or 0.3 and 0.1 + 0.2, print
        alike, so rows differing only there compared identical - and the
        sample check reads the same text (RT-HF1, verified live). Snowflake
        has no exact printing of a double inside a document, so a document
        holding such a double is refused, naming the column, a key and the
        path: once per table, column set and filter, one scan per JSON
        column, before the first checksum. The test is exact without a
        correctly rounded parse: if two doubles print one text, that text
        parses back to at most one of them, so at least one is caught. A
        double whose text parses back to itself has a text no other double
        has, and is compared as before - as are integers and decimals, which
        a VARIANT holds exactly.
        """
        json_columns = [c for c in columns if c.logical_type is LogicalType.JSON]
        memo = (table, where, tuple(c.name for c in json_columns))
        if not json_columns or memo in self._doubles_checked:
            return
        identity = self.key_identity(key)
        for col in json_columns:
            c = self.quote(col.name)
            inexact = (
                "typeof({v}) = 'DOUBLE' "
                "and cast(to_json({v}) as float) <> cast({v} as float)"
            )
            sql = (
                f"select k, p from (select {identity} as k, '' as p "
                f"{self.rows_from(table, where, inexact.format(v=c))} limit 1) "
                "union all "
                f"select k, p from (select t.k, f.path as p from (select {identity} "
                f"as k, to_variant({c}) as v {self.rows_from(table, where)}) t, "
                "lateral flatten(input => t.v, recursive => true) f "
                f"where {inexact.format(v='f.value')} limit 1) "
                "limit 1"
            )
            found = self.query(sql)
            if found:
                k, path = found[0]
                where_in = f" at {path}" if path else ""
                raise self._err(
                    f"VARIANT column {col.name!r} of {table} holds a double"
                    f"{where_in} (key {k!r}) whose JSON text Snowflake prints with "
                    "16 significant digits, which another double shares: two "
                    "different values would compare identical. It cannot be "
                    "compared exactly; leave it out with --exclude"
                )
        self._doubles_checked.add(memo)

    def bucket_expr(self, key: KeySpec, k: str, lo: int, hi: int, n_segments: int) -> str:
        """The shared bucket expression, unless it would pass 38 digits.

        NUMBER(38,0) is the only integer Snowflake has - `int` is NUMBER(38,0)
        - and it holds keys up to 10^38 - 1. The shared expression forms
        `(k - lo) * n` and writes the span as a literal, so keys 0 to 10^37
        died at 32 segments ("Number out of representable range"), and a span
        past 10^38 cannot even be written (RT-HF9). For such a span the bucket
        is read off its boundaries instead: bucket q starts at lo + ceil(q *
        span / n) - exactly what `engine.bucket_bounds` inverts - so the key
        is compared with n - 1 literals, each inside [lo, hi), with no
        arithmetic at all. Every narrower span (all of BIGINT's) keeps the
        shared SQL byte for byte, so no checksum moved.
        """
        span = hi - lo
        if key.hashed or span * n_segments < 10**38:
            return super().bucket_expr(key, k, lo, hi, n_segments)
        whens = " ".join(
            f"when {k} >= {self.int_literal(lo - (-(q * span) // n_segments))} then {q}"
            for q in range(n_segments - 1, 0, -1)
        )
        return f"(case {whens} else 0 end)"

    def wide_int(self, expr: str) -> str:
        """Widen past 64 bits before arithmetic. NUMBER(38,0) holds 38 digits,
        which the key offset cannot overflow."""
        return f"cast(({expr}) as number(38,0))"

    def sum_wide(self, expr: str) -> str:
        """Sum row hashes without overflowing, and return 0 for an empty group.

        NUMBER(38,0) is far beyond any row count times 2^60.
        """
        return f"coalesce(sum(cast(({expr}) as number(38,0))), 0)"


def _unquoted(predicate: str) -> str:
    """`predicate` with every quoted region (`'...'`, `"..."`) blanked out.

    A doubled quote closes and reopens, which leaves the region's text out
    either way. Only for a predicate the shared check has passed: no
    backslash before a quote, no dollar quoting, every quote closed.
    """
    out: list[str] = []
    close = ""
    for ch in predicate:
        if close:
            if ch == close:
                close = ""
            out.append(" ")
        elif ch in "'\"":
            close = ch
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _base(raw: str) -> str:
    """The type name without parameters, upper case: `TIMESTAMP_TZ(9)` ->
    `TIMESTAMP_TZ`."""
    return raw.upper().split("(")[0].strip()


def _sub_micro(raw: str) -> bool:
    """Whether a timestamp type can hold digits past microseconds.

    `_column` folds the precision in (`TIMESTAMP_NTZ(9)`). Without one the
    answer is yes: Snowflake's default fractional precision is 9.
    """
    inside = raw.partition("(")[2].rstrip(")").strip()
    return not inside.isdigit() or int(inside) > 6


def _column(name: str, raw: str, numeric_scale: Any, precision: Any) -> Column:
    """Build a Column from one information_schema row.

    Marks TIMESTAMP_TZ and TIMESTAMP_LTZ as instants and TIMESTAMP_NTZ as
    naive. None of the three says `with time zone`, which is all the engine
    can otherwise go on, so every Snowflake timestamp read as naive: no warning
    for NTZ against a PostgreSQL timestamptz, a false one for TZ against it.
    A timestamp's fractional precision is folded into `raw_type`, so `caveats`
    can tell a TIMESTAMP(9) from a TIMESTAMP(6).
    """
    base = _base(raw)
    tz: bool | None = None
    if base in ("TIMESTAMP_TZ", "TIMESTAMP_LTZ"):
        tz = True
    elif base in ("TIMESTAMP_NTZ", "DATETIME"):
        tz = False
    if (
        (base.startswith("TIMESTAMP") or base == "TIME")
        and "(" not in raw
        and precision is not None
    ):
        try:
            raw = f"{raw}({int(precision)})"
        except (TypeError, ValueError):
            pass
    return Column(name, map_type_snowflake(raw, numeric_scale), raw, tz_aware=tz)


def map_type_snowflake(raw: str, numeric_scale: Any) -> LogicalType:
    """Map a Snowflake type onto a logical category.

    Snowflake reports both integers and decimals as `NUMBER`; only the scale
    tells them apart, so it is passed in. A NULL scale (non-numeric type) is
    treated as not-an-integer.
    """
    t = raw.upper().split("(")[0].strip()
    if t in {"NUMBER", "DECIMAL", "NUMERIC"}:
        try:
            scale = int(numeric_scale) if numeric_scale is not None else 6
        except (TypeError, ValueError):
            scale = 6
        return LogicalType.INTEGER if scale == 0 else LogicalType.DECIMAL
    if t in {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "BYTEINT"}:
        return LogicalType.INTEGER  # aliases that may appear via DATA_TYPE_ALIAS
    if t in {"FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "DOUBLE PRECISION", "REAL"}:
        return LogicalType.FLOAT
    if t == "BOOLEAN":
        return LogicalType.BOOLEAN
    if t == "DATE":
        return LogicalType.DATE
    if t.startswith("TIMESTAMP") or t == "DATETIME":
        return LogicalType.TIMESTAMP
    if t in {"TEXT", "VARCHAR", "CHAR", "CHARACTER", "STRING"}:
        return LogicalType.STRING
    if t == "TIME":
        return LogicalType.TIME
    if t in {"BINARY", "VARBINARY"}:
        return LogicalType.BINARY
    if t in {"VARIANT", "OBJECT", "ARRAY"}:
        return LogicalType.JSON
    return LogicalType.UNKNOWN
