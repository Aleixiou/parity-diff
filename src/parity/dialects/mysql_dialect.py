"""MySQL dialect.

A third engine, and the first test of the claim that adding one is a single
small file. Most of the contract is standard SQL; the MySQL-specific decisions,
each of which the encoding tests verify, are:

- **Identifiers quote with backticks**, not double quotes. That is MySQL's
  default; double quotes are string literals unless ANSI_QUOTES is set, which
  we must not depend on.
- **The row hash goes through `CONV`.** MySQL has no `bit(n)::bigint` cast and
  no `0x`-string cast, but `CONV(hex, 16, 10)` converts a hex prefix to an
  integer at 64-bit precision, which a 60-bit value fits exactly. It must
  produce the same integer PostgreSQL and DuckDB do — 648541476951500027 for
  `'abc'` — and a test pins that.
- **`table_schema` is the database.** MySQL has no schema layer inside a
  database, so a `schema.table` name means `database.table`, and the default
  "schema" is whatever database the connection opened.
- **Snapshot and UTC** come from InnoDB's default REPEATABLE READ plus an
  explicit session time zone, the same guarantees the other two dialects set.
  The session is also READ ONLY and its sql_mode pinned. A table on a
  non-transactional engine (MyISAM, MEMORY) has no snapshot, and `caveats`
  says so in the output.
- **Bytes render as hex**, never through `cast(.. as char)`, which turns
  bytes that are not valid UTF-8 into NULL.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote

from parity.dialects.base import (
    FLOAT_EXACT_INT_FROM,
    FLOAT_MAX_MAGNITUDE_SQL,
    FLOAT_OUT_OF_RANGE_MESSAGE,
    HASH_HEX_CHARS,
    Dialect,
    float_range_where,
)
from parity.types import Column, KeySpec, LogicalType

#: MySQL's widest DECIMAL: 65 significant digits, at most 30 after the point.
#: A DECIMAL column can never hold more, which is what makes the out-of-range
#: guard in `_decimal_text` exact.
MYSQL_MAX_DECIMAL_DIGITS = 65
MYSQL_MAX_DECIMAL_SCALE = 30

#: Leads the text a too-wide DECIMAL raises with, so `query` can recognise it.
DECIMAL_RANGE_MARKER = "parity: DECIMAL value "
_DECIMAL_RANGE_ERROR = re.compile(r"'(" + re.escape(DECIMAL_RANGE_MARKER) + r".*)'")

#: Leads the text a FLOAT past 2^116 raises with. Short, because MySQL cuts
#: the quoted UUID_TO_BIN argument at 128 characters and the value and column
#: must survive; `query` expands it to `FLOAT_OUT_OF_RANGE_MESSAGE`.
FLOAT_RANGE_MARKER = "parity: FLOAT value "
_FLOAT_RANGE_ERROR = re.compile(r"'" + re.escape(FLOAT_RANGE_MARKER) + r"(.*)'")

#: Leads the text a value too long to render raises with (see `_binary_text`).
TOO_LONG_MARKER = "parity: too long to render: "
_TOO_LONG_ERROR = re.compile(r"'" + re.escape(TOO_LONG_MARKER) + r"(.*)'")

# The three patterns above end at the *last* quote of the message, not at the
# English " for function": error 1411 is worded in the server's lc_messages,
# and a German server's "... fuer Funktion uuid_to_bin" let the raw error
# through without its explanation (RT-HM10). The quoted argument is ours and
# language independent; `connect` also pins lc_messages to English.

#: Character sets in which every value MySQL can store converts to utf8mb4
#: exactly and one to one, so `cast(c as char)` loses nothing. Verified on
#: 8.0: all 256 latin1 bytes round-trip to 256 distinct characters, and
#: utf16 refuses an unpaired surrogate on insert (error 1300). Every other
#: character set takes the round-trip check in `_string_text` (RT-HM1/HM2).
_LOSSLESS_CHARSETS = frozenset({"utf8mb4", "utf8mb3", "utf8", "utf16", "latin1"})

#: Leads the canonical text of a value whose characters do not all convert to
#: Unicode, followed by its character set and its stored bytes in hex.
UNCONVERTIBLE_MARKER = "parity: not convertible from "

#: The canonical text of MySQL's ENUM error value, index 0 (RT-HM9).
INVALID_ENUM_TEXT = "parity: invalid ENUM value (index 0)"

#: Words that let a row filter lock rows or write a file. The session is READ
#: ONLY, which refuses FOR UPDATE but not a shared locking read, and InnoDB
#: holds those S locks until the diff's transaction ends (RT-HM12).
_FILTER_LOCKING = re.compile(
    r"\b(for\s+share|for\s+update|lock\s+in\s+share\s+mode|into|outfile|dumpfile)\b",
    re.IGNORECASE,
)

#: MySQL's quoted regions - a string literal or a quoted identifier, never a
#: keyword. Backslash escapes before a quote are refused by the shared check
#: before this runs, so a region ends at the next quote of its kind.
_MYSQL_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")

#: MySQL error numbers: access denied for a table / a column.
_ER_TABLEACCESS_DENIED = 1142
_ER_COLUMNACCESS_DENIED = 1143

#: The session sql_mode, pinned at connect: MySQL 8.0's own default, under
#: which every rendering here was verified. A server default is not something
#: the canonical text may depend on - PAD_CHAR_TO_FULL_LENGTH pads every CHAR
#: value to its declared width (RT-D10), NO_BACKSLASH_ESCAPES changes what a
#: string literal means (see `string_literal`). None of these modes changes
#: the DECIMAL clamp `_decimal_text` guards against: MySQL clamps a SELECT's
#: cast in every sql_mode, so the guard holds as it did (CLAUDE.md 4.2).
SQL_MODE = (
    "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION"
)

#: The storage engines that honour the REPEATABLE READ snapshot. Any other -
#: MyISAM, MEMORY, ARCHIVE, CSV - reads the table as it is at each statement.
_SNAPSHOT_ENGINES = frozenset({"innodb"})

#: Binary strings, rendered as hex (see `_binary_text`).
_BINARY_TYPES = frozenset(
    {"binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"}
)
#: Spatial types: stored as a 4-byte SRID plus WKB, rendered as that hex.
_SPATIAL_TYPES = frozenset(
    {
        "geometry",
        "point",
        "linestring",
        "polygon",
        "multipoint",
        "multilinestring",
        "multipolygon",
        "geometrycollection",
        "geomcollection",
    }
)

#: `ssl_mode` as the mysql client spells it, onto mysql-connector arguments.
#: The connector has no ssl_mode of its own; REQUIRED and above are also
#: checked after connecting (`_require_tls`), because the pure-Python
#: connector falls back to plaintext when the server offers no TLS unless
#: certificate verification is on.
_SSL_MODES: dict[str, dict[str, bool]] = {
    "DISABLED": {"ssl_disabled": True},
    "PREFERRED": {},
    "REQUIRED": {"ssl_disabled": False},
    "VERIFY_CA": {"ssl_disabled": False, "ssl_verify_cert": True},
    "VERIFY_IDENTITY": {
        "ssl_disabled": False,
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
    },
}
_SSL_PATH_PARAMS = ("ssl_ca", "ssl_cert", "ssl_key")
_SSL_BOOL_PARAMS = ("ssl_disabled", "ssl_verify_cert", "ssl_verify_identity")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

#: What follows the userinfo `@`: host (or bracketed IPv6), port, database,
#: query. A host never contains `@`, which is how the parser finds where an
#: unencoded password ends.
_AFTER_AT = re.compile(
    r"^(?P<host>\[[^\]]*\]|[^/:?#@\[\]]*)(?::(?P<port>[^/?#]*))?"
    r"(?P<path>/[^?]*)?(?:\?(?P<query>.*))?$",
    re.DOTALL,
)


@dataclass
class MySQLURL:
    """The parts of a `mysql://` URL, password included - never print one."""

    host: str = "127.0.0.1"
    port: int = 3306
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    database: str | None = None
    #: Extra mysql-connector arguments from the query string (TLS only).
    options: dict[str, Any] = field(default_factory=dict)
    #: ssl_mode REQUIRED or stricter: the connection must be encrypted.
    require_tls: bool = False


def parse_mysql_url(connection_string: str) -> MySQLURL:
    """Parse `mysql://user:password@host:port/database?options`.

    Parsed by hand rather than with `urlparse`, for two reasons (RT-F3).
    An unencoded `/` or `#` in a password ends `urlparse`'s authority early,
    so it reads the start of the password as the port and *quotes it* in
    "Port could not be cast to integer value as '<password>'" - a secret in
    an error message, which CI logs keep. Here the userinfo ends at the
    first `@` after which a plain host follows, so such a password simply
    works; and no error raised below ever contains the userinfo.

    The query string carries mysql-connector's TLS options and nothing else
    (RT-F18). It used to be dropped silently, so `?ssl_mode=REQUIRED` - a
    user forbidding a plaintext connection to production - did nothing. An
    option not understood is refused rather than ignored, for the same reason.
    Raises ValueError without the side; `connect` adds it.
    """
    rest = connection_string.split("://", 1)[1] if "://" in connection_string else ""
    user_info: str | None = None
    m = None
    # The first `@` followed by a well-formed host ends the userinfo: a
    # password may hold `/`, `#`, `?` or `:` unencoded, and an `@` in a query
    # value (an ssl_ca path) is not mistaken for one.
    for i in [i for i, ch in enumerate(rest) if ch == "@"]:
        m = _AFTER_AT.match(rest[i + 1 :])
        if m is not None:
            user_info = rest[:i]
            break
    if "@" not in rest:
        m = _AFTER_AT.match(rest)
    if m is None:
        raise ValueError(
            "malformed MySQL URL; expected "
            "mysql://user:password@host:port/database (percent-encode an `@` "
            "in the password as %40)"
        )
    out = MySQLURL()
    if user_info is not None:
        user, sep, password = user_info.partition(":")
        out.user = unquote(user) or None
        out.password = unquote(password) if sep else None
    host = m.group("host")
    if host.startswith("["):
        host = host[1:-1]
    out.host = unquote(host) or "127.0.0.1"
    port = m.group("port")
    if port:
        if not port.isdigit() or not 0 < int(port) < 65536:
            # The port text is not the password here (`_AFTER_AT` follows the
            # `@`), but say nothing of it either way.
            raise ValueError("the port in the MySQL URL is not a number in 1-65535")
        out.port = int(port)
    path = (m.group("path") or "").lstrip("/")
    out.database = unquote(path) or None
    _parse_options(m.group("query") or "", out)
    return out


def _parse_options(query: str, out: MySQLURL) -> None:
    """Map the URL's query string onto mysql-connector TLS arguments."""
    seen: set[str] = set()
    mode: str | None = None
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name in seen:
            raise ValueError(f"MySQL URL option {name!r} is given twice")
        seen.add(name)
        if name == "ssl_mode":
            mode = value.upper()
            if mode not in _SSL_MODES:
                raise ValueError(
                    f"MySQL URL ssl_mode={value!r} is not one of {', '.join(_SSL_MODES)}"
                )
        elif name in _SSL_PATH_PARAMS:
            out.options[name] = value
        elif name in _SSL_BOOL_PARAMS:
            low = value.lower()
            if low not in _TRUE | _FALSE:
                raise ValueError(f"MySQL URL option {name}={value!r} is not true/false")
            out.options[name] = low in _TRUE
        else:
            raise ValueError(
                f"MySQL URL option {name!r} is not supported, and an option is "
                "refused rather than ignored. Supported: ssl_mode, "
                f"{', '.join(_SSL_PATH_PARAMS + _SSL_BOOL_PARAMS)}"
            )
    if mode is None:
        return
    for opt, flag in _SSL_MODES[mode].items():
        if opt in out.options and out.options[opt] != flag:
            raise ValueError(
                f"MySQL URL ssl_mode={mode} contradicts {opt}={out.options[opt]}"
            )
        out.options[opt] = flag
    if mode == "DISABLED" and any(k in out.options for k in _SSL_PATH_PARAMS):
        raise ValueError(
            "MySQL URL ssl_mode=DISABLED contradicts the ssl_ca/cert/key given"
        )
    out.require_tls = mode not in ("DISABLED", "PREFERRED")


class MySQLDialect(Dialect):
    name = "mysql"
    #: MySQL has no schema inside a database, so the connection's database is
    #: the default namespace and `information_schema` calls it table_schema.
    default_schema = ""

    def connect(self, connection_string: str) -> None:
        """Open a read-only, UTC-pinned, single-snapshot connection.

        InnoDB's default isolation is REPEATABLE READ, so a transaction already
        sees one snapshot from its first read - the guarantee PostgreSQL had to
        be told to give. Autocommit goes off once the session is pinned, so
        that snapshot spans the diff.
        """
        try:
            url = parse_mysql_url(connection_string)
        except ValueError as exc:
            # Name the side; the message never carries the userinfo.
            raise self._err(str(exc)) from None
        # Imported only after the URL parses, so a malformed connection string
        # is reported as such even where the driver is not installed.
        import mysql.connector

        self._database = url.database
        if self.default_schema == "" and self._database:
            self.default_schema = self._database
        #: Tables introspected on a storage engine with no snapshot, by name.
        self._no_snapshot: dict[str, str] = {}
        # Stored so `cancel` can open a second connection to KILL this one.
        self._host = url.host
        self._port = url.port
        self._user = url.user
        self._password = url.password
        self._options = url.options
        # Autocommit stays on while the session is configured, so the
        # characteristics below apply to the first transaction the diff opens
        # rather than to one the SET statements might already have begun.
        self._conn = mysql.connector.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            database=self._database,
            autocommit=True,
            connection_timeout=10,
            **self._options,
        )
        # MariaDB speaks the protocol and accepts mysql://, but it is another
        # engine: its uca1400 default collation makes every text REPLACE an
        # "Illegal mix of collations", it has no UUID_TO_BIN (which every
        # DECIMAL, FLOAT and binary guard raises through), and none of the
        # renderings here was verified on it. Refused by name (RT-HM11).
        if "mariadb" in str(getattr(self._conn, "server_info", "") or "").lower():
            self._conn.close()
            raise self._err(
                "this server is MariaDB, not MySQL. parity's MySQL dialect has "
                "not been verified against MariaDB, whose collations, functions "
                "and type renderings differ, so it refuses rather than compare "
                "on guesses"
            )
        cur = self._conn.cursor()
        # Error texts in English, so `query` can explain the dialect's own
        # refusals whatever the server's lc_messages (RT-HM10). A session
        # variable any login may set; if a server refuses it, the patterns in
        # `query` do not depend on the language anyway.
        try:
            cur.execute("set session lc_messages = 'en_US'")
        except Exception:  # noqa: BLE001, S110 - best effort, see above
            pass
        # A timestamp renders through the session time zone; pin it so two
        # servers in different zones cannot report the same instant as different.
        cur.execute("set session time_zone = '+00:00'")
        cur.execute(f"set session sql_mode = '{SQL_MODE}'")
        # A GLOBAL, per-user or init_connect `sql_select_limit` caps the rows of
        # every SELECT without a LIMIT - the column introspection and the
        # checksum buckets alike - so columns past it were never compared and
        # buckets past it read as empty on both sides: identical (RT-G1).
        # Not `= default`: for a session variable that means the GLOBAL value,
        # which is the cap itself. The type's maximum is MySQL's own default.
        cur.execute("set session sql_select_limit = 18446744073709551615")
        # MySQL 9.7 moved MD5() into the optional component_classic_hashing;
        # without it the first checksum died naming nothing useful (RT-G13).
        try:
            cur.execute("select md5('')")
            cur.fetchall()
        except Exception as exc:
            self._conn.close()
            raise self._err(
                "this MySQL server has no MD5() function, which every row hash "
                "needs. MySQL 9.7 moved it into an optional component: a DBA "
                "can add it with INSTALL COMPONENT "
                "'file://component_classic_hashing'"
            ) from exc
        # Belt and braces on top of the InnoDB default - and READ ONLY, so the
        # server itself refuses a write in this session, as it does on
        # PostgreSQL, DuckDB and BigQuery (RT-F11; CLAUDE.md section 6). A
        # read-only InnoDB transaction keeps the same consistent snapshot.
        cur.execute("set session transaction isolation level repeatable read, read only")
        if url.require_tls:
            self._require_tls(cur)
        cur.close()
        # No pool arguments are passed, so this is never a PooledMySQLConnection.
        self._conn.autocommit = False  # type: ignore[union-attr]

    def _require_tls(self, cur: Any) -> None:
        """Refuse a plaintext session when the URL asked for ssl_mode REQUIRED+.

        mysql-connector has no ssl_mode, and without certificate verification
        it quietly continues in plaintext when the server offers no TLS. The
        server knows: `Ssl_cipher` is empty on an unencrypted session.
        """
        cur.execute("show session status like 'Ssl_cipher'")
        rows = cur.fetchall()
        if not rows or not rows[0][1]:
            self._conn.close()
            raise self._err(
                "the URL asks for ssl_mode REQUIRED or stricter, but the "
                "connection to the server is not encrypted; refusing to continue"
            )

    def cancel(self) -> None:
        """Abort the running query from another thread.

        A separate short-lived admin connection issues KILL QUERY against this
        connection's id - mysql-connector cannot interrupt a blocked cursor in
        place, the way psycopg and duckdb can.
        """
        import mysql.connector

        try:
            thread_id = int(self._conn.connection_id or 0)
            admin = mysql.connector.connect(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                connection_timeout=5,
                **self._options,
            )
            try:
                c = admin.cursor()
                c.execute(f"kill query {thread_id}")
                c.close()
            finally:
                admin.close()
        except Exception as exc:  # noqa: BLE001 - cancel is best effort;
            # a failure to KILL must never mask the real error - but it must
            # not be silent either (RT-HM16). A login already at its
            # max_user_connections cannot open the KILL connection, and the
            # running query then goes on to its end: say so, and how to stop it.
            import warnings

            warnings.warn(
                f"[side {self.side}: {self.name}] could not cancel the running "
                f"query: opening a second connection to KILL it failed ({exc}). "
                "It runs on until it finishes; a DBA can stop it with "
                f"KILL QUERY {getattr(self._conn, 'connection_id', '?')}. A "
                "login at its max_user_connections limit cannot be cancelled "
                "from the client",
                RuntimeWarning,
                stacklevel=2,
            )

    def close(self) -> None:
        """Close the connection, ending the snapshot."""
        self._conn.close()

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run `sql` and return every row as a list of tuples."""
        import mysql.connector

        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            return list(cur.fetchall())
        except mysql.connector.Error as exc:
            huge = _FLOAT_RANGE_ERROR.search(str(exc))
            if huge is not None:
                raise self._err(FLOAT_OUT_OF_RANGE_MESSAGE + huge.group(1)) from exc
            long = _TOO_LONG_ERROR.search(str(exc))
            if long is not None:
                raise self._err(
                    f"{long.group(1)}: its canonical text is longer than this "
                    "server's max_allowed_packet, past which MySQL string "
                    "functions return NULL. A NULL would hash as nothing and "
                    "the value could compare equal to any other, so the "
                    "comparison is refused. Exclude the column (--exclude) or "
                    "raise max_allowed_packet on the server."
                ) from exc
            # The out-of-range DECIMAL guard (`_decimal_text`) raises through
            # UUID_TO_BIN; say what actually happened, and on which side.
            found = _DECIMAL_RANGE_ERROR.search(str(exc))
            if found is None:
                raise
            s = self.float_scale
            raise self._err(
                f"{found.group(1)} does not fit DECIMAL({MYSQL_MAX_DECIMAL_DIGITS},"
                f"{s}), the widest type MySQL can render it in at float_scale {s}. "
                "MySQL would clamp it to that type's maximum without an error, "
                "so two different values could compare equal; the comparison is "
                "refused instead. A smaller --float-scale leaves more integer "
                "digits."
            ) from exc
        finally:
            cur.close()

    def columns(self, table: str) -> list[Column]:
        """Introspect columns. `table_schema` is the database in MySQL."""
        schema, name = self.split_table(table, self.default_schema)
        where = (
            f"where table_schema = {self.string_literal(schema)} "
            f"and table_name = {self.string_literal(name)}"
        )
        rows = self.query(
            "select column_name, data_type, numeric_precision, column_type, "
            "character_set_name "
            f"from information_schema.columns {where} order by ordinal_position"
        )
        if not rows:
            raise self._err(self._not_found(table, schema, name))
        self._require_every_column(table, schema, name, [str(r[0]) for r in rows])
        self._note_snapshot(table, schema, name)
        return [_column(str(r[0]), str(r[1]), r[2], str(r[3] or ""), r[4]) for r in rows]

    def _relation(self, schema: str, name: str) -> str:
        """`schema`.`name` quoted, or the bare name without a schema."""
        return f"{self.quote(schema)}.{self.quote(name)}" if schema else self.quote(name)

    def _probe(self, sql: str) -> int | None:
        """Run `sql`; None if it succeeds, else the MySQL error number."""
        import mysql.connector

        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            cur.fetchall()
            return None
        except mysql.connector.Error as exc:
            return int(exc.errno or -1)
        finally:
            cur.close()

    def _require_every_column(
        self, table: str, schema: str, name: str, visible: list[str]
    ) -> None:
        """Refuse a table this login may read only some columns of (RT-HM3).

        With column-level grants (`GRANT SELECT (id, a) ON t`) MySQL lists
        only the granted columns in information_schema.columns, so the others
        were silently never compared: a difference in one of them read
        identical, with no schema difference and no warning. `SELECT *` needs
        every column, so it is refused (error 1142/1143) exactly when some
        column is hidden - the server's own privilege check, roles included.
        """
        err = self._probe(f"select * from {self._relation(schema, name)} limit 0")
        if err in (_ER_TABLEACCESS_DENIED, _ER_COLUMNACCESS_DENIED):
            raise self._err(
                f"this login may read only some columns of table {table} "
                f"(column-level privileges). MySQL then lists only those - "
                f"{visible} - and hides the rest from information_schema, so "
                "the hidden columns would silently go uncompared. Ask for "
                f"SELECT on the whole table {schema}.{name}"
            )

    def _note_snapshot(self, table: str, schema: str, name: str) -> None:
        """Record every non-transactional table this relation reads (RT-D3).

        The storage engine decides whether the snapshot holds at all: only
        InnoDB honours REPEATABLE READ. A view reports no engine (NULL) and
        reads whatever its base tables are, so its base tables are followed
        through information_schema.view_table_usage (8.0.13+), views of views
        included (RT-HM17). A view whose base tables cannot be listed gets
        the caveat too: unknown is not the same as safe.
        """
        todo = [(schema, name, f"table {table}")]
        seen: set[tuple[str, str]] = set()
        while todo:
            s, n, label = todo.pop()
            if (s, n) in seen or len(seen) > 256:
                continue
            seen.add((s, n))
            where = (
                f"where table_schema = {self.string_literal(s)} "
                f"and table_name = {self.string_literal(n)}"
            )
            for kind, engine in self.query(
                f"select table_type, engine from information_schema.tables {where}"
            ):
                if engine is not None and str(engine).lower() not in _SNAPSHOT_ENGINES:
                    self._no_snapshot[label] = str(engine)
                if str(kind).upper() != "VIEW":
                    continue
                try:
                    bases = self.query(
                        "select table_schema, table_name "
                        "from information_schema.view_table_usage "
                        f"where view_schema = {self.string_literal(s)} "
                        f"and view_name = {self.string_literal(n)}"
                    )
                except Exception:  # noqa: BLE001 - before 8.0.13: unknown
                    bases = []
                if not bases:
                    self._no_snapshot[f"view {table}"] = "unknown"
                for bs, bn in bases:
                    todo.append(
                        (str(bs), str(bn), f"view {table} (base table {bs}.{bn})")
                    )

    def _not_found(self, table: str, schema: str, name: str) -> str:
        """Say "not found, or not granted" when MySQL cannot tell which.

        MySQL hides a table the login holds no privilege on from
        information_schema, and answers a query on it with 1142 "SELECT
        command denied" - but it answers a table that does not exist the same
        way, so as not to reveal which names exist. The probe therefore cannot
        prove the table is there, only that the login may not look; the
        message says both instead of sending the user to hunt a typo when a
        GRANT is missing (RT-HM15). A login with privileges on the database
        gets 1146 for a missing table, and the plain message.
        """
        message = super()._not_found(table, schema, name)
        if self._probe(f"select 1 from {self._relation(schema, name)} limit 0") == (
            _ER_TABLEACCESS_DENIED
        ):
            message += (
                ". Or it exists and this login holds no privilege on it: MySQL "
                "answers 'SELECT command denied' for a table it may not see "
                "whether or not the table exists, and lists neither in "
                f"information_schema. If the name is right, ask for SELECT on "
                f"{schema}.{name}"
            )
        return message

    def check_where(self, where: str) -> None:
        """Refuse a row filter that locks rows or writes a file (RT-HM12).

        The shared check keeps the predicate one read. On MySQL a subquery
        can still end in `FOR SHARE` / `LOCK IN SHARE MODE` - which READ ONLY
        does not refuse - and InnoDB then holds shared locks on every row it
        read until the diff's transaction ends: a writer on the live legacy
        table waits the whole run, which the tool promises never to cause
        (CLAUDE.md 4.9). `INTO` (OUTFILE, DUMPFILE, a variable) is no part of
        a predicate either. Matched as words outside quoted regions, so a
        string or a backticked column that spells one is fine.
        """
        super().check_where(where)
        code = _MYSQL_QUOTED.sub(" ", where)
        found = _FILTER_LOCKING.search(code)
        if found:
            raise self._err(
                f"row filter {where!r} contains {found.group(0)!r}: a locking "
                "read would hold row locks on the live table for the whole diff, "
                "and INTO / OUTFILE / DUMPFILE are not part of a predicate"
            )

    def string_literal(self, value: str) -> str:
        r"""Quote a name as a MySQL string literal: `'` doubled and `\` doubled.

        MySQL reads a backslash in a literal as an escape, so `'a\b'` is
        `a<backspace>` and a trailing `\` swallows the closing quote (RT-F17).
        Doubling it is right exactly when NO_BACKSLASH_ESCAPES is off, which
        `connect` makes certain by pinning `SQL_MODE`. Only ever used for
        schema and table names.
        """
        return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"

    def single_precision(self, column: Column) -> bool:
        """MySQL spells float4 `float` (and `real` is a double here only
        under REAL_AS_FLOAT, which the pinned sql_mode does not set)."""
        return column.raw_type.lower().split("(")[0].strip() == "float"

    def sample_select(self, column: Column) -> str:
        """Raw values for the sample check; a float4 widened to double first.

        The text protocol sends a FLOAT with six significant digits -
        16777217 arrived as 16777200.0 - so the raw value is lossy; widening
        in the server is exact and the double travels in full. Verified live.
        """
        if column.logical_type is LogicalType.FLOAT and self.single_precision(column):
            return f"cast({self.quote(column.name)} as double)"
        c = self.quote(column.name)
        if column.logical_type is LogicalType.JSON:
            # The engine's own normalised text, whatever the connector does
            # with a JSON result.
            return f"cast({c} as char)"
        if column.logical_type in (LogicalType.DATE, LogicalType.TIMESTAMP):
            # The connector returns None for any date Python cannot hold -
            # '0000-00-00', '2024-02-31' under ALLOW_INVALID_DATES - which
            # the reference read as NULL, so the walk's correct difference
            # against a real NULL was noted as a false alarm (RT-HM13).
            # MySQL's own text of the value carries it exactly.
            return f"cast({c} as char)"
        if self._string_sampled_as_bytes(column):
            # Facts, not a rendering: the ENUM index, the stored bytes, the
            # bytes after a round trip through utf8mb4 and the utf8mb4 bytes,
            # in hex; `sample_value` applies the rules to them in Python. The
            # connector would hand back a SET as a Python set (RT-HM6), an
            # ENUM error value as '' (RT-HM9) and an unconvertible character
            # as '?' (RT-HM1), each indistinguishable from real text.
            enum = f"{c} + 0" if _base(column) == "enum" else "''"
            cs = self._lossy_charset(column)
            back = (
                f"ifnull(hex(convert(convert({c} using utf8mb4) using {cs})), '-')"
                if cs
                else f"hex({c})"
            )
            parts = [
                enum,
                f"hex({c})",
                back,
                f"ifnull(hex(convert({c} using utf8mb4)), '-')",
            ]
            return f"case when {c} is null then null else concat_ws(',', {', '.join(parts)}) end"
        return super().sample_select(column)

    def sample_value(self, column: Column, value: Any) -> Any:
        """A BIT(n) arrives as an unsigned integer, which has lost the width
        that is part of the value; restore it from the declared `bit(n)`, so
        the reference reads the n digits the column holds. A text column
        `sample_select` read as hex facts becomes its text by the rules."""
        if column.logical_type is LogicalType.BITS and isinstance(value, int):
            width = re.search(r"\((\d+)\)", column.raw_type)
            digits = format(value, "b")
            return digits.rjust(int(width.group(1)), "0") if width else digits
        if value is not None and self._string_sampled_as_bytes(column):
            text = value.decode() if isinstance(value, (bytes, bytearray)) else str(value)
            index, stored, back, utf8 = text.split(",")
            if _base(column) == "enum" and float(index) == 0:
                return INVALID_ENUM_TEXT
            cs = self._lossy_charset(column)
            if cs and back != stored:
                return f"{UNCONVERTIBLE_MARKER}{cs} 0x{stored.upper()}"
            return bytes.fromhex(utf8).decode("utf-8")
        return value

    def _lossy_charset(self, column: Column) -> str | None:
        """The column's character set, if it is one whose conversion to
        utf8mb4 can lose characters; None for the lossless ones and for any
        column that is not text."""
        if column.logical_type is not LogicalType.STRING or not column.charset:
            return None
        cs = column.charset.lower()
        if cs in _LOSSLESS_CHARSETS:
            return None
        if not re.fullmatch(r"[a-z0-9_]+", cs):
            # Pasted into SQL as a name; information_schema never reports
            # anything else, but say so rather than quote it.
            raise self._err(
                f"column {column.name!r} has an unexpected character set {cs!r}"
            )
        return cs

    def _string_sampled_as_bytes(self, column: Column) -> bool:
        """Whether the sample check reads this text column as hex facts."""
        return column.logical_type is LogicalType.STRING and (
            _base(column) in ("enum", "set") or self._lossy_charset(column) is not None
        )

    def json_semantics(self, column: Column) -> str:
        """MySQL's JSON normal form."""
        return (
            "MySQL JSON: object keys ordered by length then bytes, the last "
            "duplicate key kept, integers exact to 64 bits and every other number "
            "a double (1.50 becomes 1.5, 1e2 becomes 100.0)"
        )

    def caveats(self, columns: Sequence[Column]) -> list[str]:
        """A table on a non-transactional engine is not read at one snapshot.

        MyISAM and MEMORY ignore the transaction entirely: each checksum query
        sees the table as it is when that query runs. A write landing between
        two levels can repair one bucket the walk is descending into and break
        another it already proved equal, and the run then reads identical
        though the tables differed throughout (RT-D3). Said in the output,
        as SQL Server's dialect does for READ COMMITTED.
        """
        tail = (
            "so the walk is not one snapshot: each query sees the table as of "
            "its own start, and rows written during the diff can be reported "
            "as differences that never existed, or missed entirely. Diff a "
            "quiet copy for a single-snapshot result"
        )
        out = []
        for label, engine in sorted(getattr(self, "_no_snapshot", {}).items()):
            if engine == "unknown":
                out.append(
                    f"{label}: its base tables could not be listed "
                    "(information_schema.view_table_usage, MySQL 8.0.13+), so "
                    "whether they all honour the transaction is unknown and the "
                    f"walk may not be one snapshot. If any is not InnoDB, {tail}"
                )
            else:
                out.append(
                    f"{label} uses the {engine} storage engine, which has no "
                    f"transactions, {tail}"
                )
        return out

    def quote(self, identifier: str) -> str:
        """Wrap an identifier in backticks, doubling any it contains.

        MySQL's default identifier quote is the backtick; a double quote is a
        string literal unless ANSI_QUOTES is set, which we must not rely on.
        This is the injection boundary - names arrive from the command line.
        """
        return "`" + identifier.replace("`", "``") + "`"

    # ----------------------------------------------------------- rendering

    def separator_sql(self) -> str:
        """`char(31 using utf8mb4)` - MySQL has no `chr`, and a bare `char(31)`
        is a binary string that would coerce the join to bytes."""
        return "char(31 using utf8mb4)"

    def null_sentinel_sql(self) -> str:
        r"""Build the sentinel from CHAR(92) rather than a backslash literal.

        MySQL processes `\` as an escape inside a string literal, and whether
        it does is governed by `sql_mode` (NO_BACKSLASH_ESCAPES), so `'\N'` is
        not portable across servers. CHAR(92) is a backslash unconditionally.
        Verified to hash to the same value DuckDB and PostgreSQL produce for the
        sentinel.
        """
        return "convert(0x5c4e using utf8mb4)"

    def normalize(self, column: Column) -> str:
        """Render one column as canonical text, null-safe.

        DECIMAL casts to DECIMAL(65, scale) then to text, so `1.5` becomes
        `'1.500000'`, and raises rather than clamp a value that does not fit
        (see `_decimal_text`); FLOAT follows the shared canonical float
        form (see the branch). Every branch ends inside the coalesce, so NULL
        always becomes the sentinel.
        """
        c = self.quote(column.name)
        t = column.logical_type
        raw = column.raw_type.lower().split("(")[0].strip()
        if t is LogicalType.INTEGER:
            # `+ 0` drops what only the display carries (RT-HM4, RT-HM7):
            # a ZEROFILL int(8) 5 casts to '00000005' and the YEAR 0 to
            # '0000', though their values - as the driver and every other
            # engine read them - are 5 and 0. Only those columns change SQL,
            # and only YEAR 0 and ZEROFILL values change text.
            expr = (
                f"cast({c} + 0 as char)"
                if _display_padded(column)
                else f"cast({c} as char)"
            )
        elif t is LogicalType.DECIMAL:
            expr = self._decimal_text(column, c)
        elif t is LogicalType.FLOAT:
            # Widen first: a single-precision column (`real`, FLOAT) is cast to
            # double, which is exact on every engine, so float4 and float8 share
            # one canonical form. Rendered as float4 directly, PostgreSQL and
            # DuckDB print different shortest reprs (-4088288.25 as `-4088288.2`
            # against `-4088288.25`) and MySQL prints only 6 digits. For a
            # double column the cast is a no-op.
            c = f"cast({c} as double)"
            # Finite values follow `canonical_float_text` (base.py). Below 2^53
            # MySQL's double -> DECIMAL cast already is that: it converts
            # through the shortest round-trip decimal (dtoa) and rounds half
            # away from zero - verified identical to the Python spec on every
            # value of the adversarial sample, so these checksums do not move.
            # From 2^53 every double is an integer, rebuilt exactly from two
            # BIGINTs, because PostgreSQL cannot match the shortest repr there.
            # Past 2^116 the high half overflows BIGINT, and MySQL's CAST would
            # *clamp* it silently - two different huge values comparing equal.
            # MySQL has no error() and a SELECT turns most errors into
            # warnings, so that branch raises through UUID_TO_BIN, exactly as
            # the DECIMAL guard does (see `_decimal_text`), and `query` names
            # the side, the column and the value. It used to be exp() overflow
            # (1690), which said only "DOUBLE value is out of range in
            # 'exp(...)'" - nothing a user could act on (RT-E10).
            # MySQL DOUBLE cannot hold Infinity or NaN, so no tokens here.
            s = self.float_scale_for(column)
            hi = f"truncate({c} / {FLOAT_EXACT_INT_FROM}, 0)"
            expr = (
                f"case when abs({c}) < {FLOAT_EXACT_INT_FROM} "
                f"then cast(cast({c} as decimal(38,{s})) as char) "
                f"when abs({c}) < cast('{FLOAT_MAX_MAGNITUDE_SQL}' as double) "
                f"then cast(cast(cast({hi} as signed) as decimal(65,0)) "
                f"* {FLOAT_EXACT_INT_FROM} "
                f"+ cast({c} - {hi} * {FLOAT_EXACT_INT_FROM} as signed) "
                f"as decimal(65,{s})) "
                f"when {c} is not null then cast(uuid_to_bin(concat("
                f"'{FLOAT_RANGE_MARKER}', cast({c} as char), "
                f"convert(0x{float_range_where(column).encode().hex()} using utf8mb4)"
                f")) as char) end"
            )
        elif t is LogicalType.BOOLEAN:
            # MySQL BOOLEAN is an alias for TINYINT(1): 0 or 1, never a real
            # boolean. `when not c` still routes NULL to the sentinel.
            expr = f"case when {c} then 'true' when not {c} then 'false' end"
        elif t is LogicalType.DATE:
            expr = f"date_format({c}, '%Y-%m-%d')"
        elif t is LogicalType.TIMESTAMP:
            # %f is microseconds, six digits, matching the other two engines.
            expr = f"date_format({c}, '%Y-%m-%d %H:%i:%s.%f')"
        elif t is LogicalType.TIME:
            # TIME is a duration here, -838:59:59 .. 838:59:59; time_format's
            # %H runs past 23 and keeps the sign (verified: `-00:00:00.500000`,
            # `100:00:00.500000`), which is `canonical_time_text` exactly.
            # Six digits whatever the column's fsp; the char cast printed
            # none for time(0), so only time(6) checksums stay put.
            expr = f"time_format({c}, '%H:%i:%s.%f')"
        elif t is LogicalType.BINARY:
            # PostgreSQL's bytea text form (bytea_output = hex, its default):
            # a backslash, `x`, then lower-case hex - BINARY_PREFIX + hex.
            expr = self._binary_text(column, c, prefix=True)
        elif t is LogicalType.JSON:
            # MySQL stores JSON normalised - keys ordered by length then
            # bytes, the last duplicate kept - and prints it in exactly the
            # canonical layout and string escaping (verified against
            # `canonical_json_text`), so the char cast is the canonical text:
            # the SQL the unmapped fallback ran, and no checksum moves.
            # Laying out compact JSON here would need REGEXP_REPLACE, whose
            # global-only regexp_time_limit failed on a 400 kB document.
            expr = self.escape_text(f"cast({c} as char)")
        elif raw in _SPATIAL_TYPES:
            # The stored bytes - SRID, then WKB - in full: exact, and
            # MySQL-to-MySQL only. ST_AsText would round the coordinates.
            expr = self._binary_text(column, c, prefix=False)
        elif t is LogicalType.BITS:
            expr = self._bit_text(column, c)
        elif raw == "vector":
            # MySQL 9's VECTOR refuses `cast(.. as char)` (error 1210), which
            # killed the first checksum naming neither column nor type
            # (RT-HM14). Its stored bytes - little-endian float4s - as hex:
            # exact, and MySQL-to-MySQL only, like the spatial types.
            expr = self._binary_text(column, c, prefix=False)
        elif t is LogicalType.STRING:
            expr = self.escape_text(self._string_text(column, c))
        else:
            # STRING, and every unmapped type (json, time, ...) through its
            # text cast: arbitrary text, so escaped.
            expr = self.escape_text(f"cast({c} as char)")
        return f"coalesce({expr}, {self.null_sentinel_sql()})"

    def _string_text(self, column: Column, c: str) -> str:
        """A text column as utf8mb4 text, never losing a character.

        `cast(c as char)` converts the column's character set to utf8mb4 and
        replaces every character with no Unicode mapping by `?` (or U+FFFD) -
        no error, in any sql_mode - and a ucs2 value holding a surrogate
        becomes SQL NULL entirely. So two different Shift-JIS user-defined
        characters, a real `?` and (for ucs2) a NULL all rendered alike: false
        matches, MySQL to MySQL and against every engine (RT-HM1, RT-HM2).
        Measured on 8.0: sjis loses 1,448 storable characters this way, euckr
        1,792, gbk 712.

        For a character set outside `_LOSSLESS_CHARSETS` the value converts
        back to its own character set and is compared byte for byte with what
        is stored. Equal means the conversion lost nothing and it renders
        exactly as before - so no checksum of convertible text moves. Not
        equal (or NULL) means characters were lost, and it renders as
        `UNCONVERTIBLE_MARKER`, the character set and the stored bytes in hex:
        distinct for distinct values, and equal to no text another engine
        holds unless that text spells the marker itself. Only those columns
        pay for the round trip.

        An ENUM's index 0 is MySQL's error value - what an invalid value
        becomes when written outside strict mode - and prints '', exactly as
        a real '' member does (RT-HM9). It renders as `INVALID_ENUM_TEXT`.
        """
        text = f"cast({c} as char)"
        cs = self._lossy_charset(column)
        if cs:
            stored = f"cast({c} as binary)"
            back = f"cast(convert(convert({c} using utf8mb4) using {cs}) as binary)"
            text = (
                f"case when {back} = {stored} then {text} "
                f"when {c} is not null "
                f"then concat('{UNCONVERTIBLE_MARKER}{cs} 0x', hex({c})) end"
            )
        if _base(column) == "enum":
            text = f"case when {c} + 0 = 0 then '{INVALID_ENUM_TEXT}' else {text} end"
        return text

    def _binary_text(self, column: Column, c: str, *, prefix: bool) -> str:
        r"""Render bytes as lower-case hex, never as text.

        These columns used to fall through to `cast(c as char)`, which reads
        the bytes as utf8mb4 and returns NULL - with only warning 1300 - for
        any that are not valid UTF-8. The coalesce then made them the NULL
        sentinel, so every such value equalled every other and a real NULL
        (RT-A8, RT-B12, RT-C4): two different binary(16) UUIDs matched, and a
        table keyed on them collapsed into a few identities and was refused
        as non-unique. Hex is exact for every byte string.

        Checksums move for binary columns whose bytes *were* valid UTF-8,
        which used to render as that text; no other column's text changes.
        With `prefix` the form is PostgreSQL's bytea text as escaped text.

        Hex doubles the length, and MySQL string functions return NULL (again
        with only a warning) once a result passes max_allowed_packet. A NULL
        from a non-NULL value must never become the sentinel, so it raises
        through UUID_TO_BIN like the DECIMAL guard, and `query` names the
        side, the column and the size.
        """
        text = f"lower(hex({c}))"
        if prefix:
            # PostgreSQL renders bytea as the text `\x0aff`, and that text goes
            # through `escape_text` like every unmapped type, which doubles the
            # backslash. So the canonical form is `\\x0aff` (0x5c5c78), the
            # escaped spelling of exactly what PostgreSQL prints.
            text = f"concat(convert(0x5c5c78 using utf8mb4), {text})"
        where = f" bytes in column {column.name!r}".encode()
        refuse = (
            f"cast(uuid_to_bin(concat('{TOO_LONG_MARKER}', length({c}), "
            f"convert(0x{where.hex()} using utf8mb4))) as char)"
        )
        return f"case when {c} is null then null else coalesce({text}, {refuse}) end"

    def escape_text(self, expr: str) -> str:
        """Escape text, refusing a value whose escaped text MySQL cannot hold.

        Escaping lengthens text, and past max_allowed_packet MySQL's REPLACE
        returns NULL with only warning 1301. The coalesce then made it the NULL
        sentinel, so a long text holding one backslash equalled a NULL on the
        other side - a false match against every engine (RT-G3). As for binary
        (`_binary_text`), a NULL from a non-NULL value raises through
        UUID_TO_BIN instead, and `query` names the side and the size. Text
        that fits renders exactly as before: no checksum moves.
        """
        escaped = super().escape_text(expr)
        note = b" characters of text, past max_allowed_packet once escaped"
        refuse = (
            f"cast(uuid_to_bin(concat('{TOO_LONG_MARKER}', char_length({expr}), "
            f"convert(0x{note.hex()} using utf8mb4))) as char)"
        )
        return (
            f"case when ({expr}) is null then null else coalesce({escaped}, {refuse}) end"
        )

    def _bit_text(self, column: Column, c: str) -> str:
        """Render BIT(n) as its n binary digits, PostgreSQL's `bit(n)` text.

        `cast(bit as char)` is the raw bytes, which are rarely valid UTF-8 -
        the same NULL as binary strings (RT-A8, RT-C4). `c + 0` is the
        unsigned integer value; `bin` spells it in binary and `lpad` restores
        the leading zeros the width implies, so b'00000101' in a bit(8) reads
        `00000101` - exactly the stored bits, and what PostgreSQL prints.
        """
        width = re.search(r"\((\d+)\)", column.raw_type)
        digits = f"bin({c} + 0)"
        return f"lpad({digits}, {width.group(1)}, '0')" if width else digits

    def hash_parts(self, parts: list[str]) -> str:
        """The row hash, with a fallback for a row too wide to join.

        MySQL's `concat_ws` returns NULL - warning 1301 only - once the joined
        row passes max_allowed_packet (64 MiB by default), though each value
        fits. md5(NULL) is NULL and `sum()` skips it, so the row vanished from
        its bucket's checksum while still counted: two different wide rows
        checksummed equal, a false match MySQL to MySQL (RT-A17).

        Such a row now hashes the md5 of each column's canonical text instead,
        joined behind a marker no ordinary row text starts with - 32 bytes a
        column, far under any packet limit. Equal rows still hash alike on two
        MySQL servers and different ones differ. Against another engine the
        fallback hash never matches its real one, so the bucket is fetched and
        compared value by value: a slower path, never a false match. Rows
        that fit render exactly as before - the coalesce only fires on NULL -
        so no checksum moves. This overrides `hash_parts`, not `row_hash`, so
        the segment checksum - which folds the key text in - is guarded too.
        """
        primary = super().hash_parts(parts)
        fallback = ["'parity: wide row'", *(f"md5({p})" for p in parts)]
        return f"coalesce({primary}, {self.hash_expr(self._concat(fallback))})"

    def backslash_sql(self) -> str:
        r"""`char(92 using utf8mb4)`: a backslash whatever sql_mode says about
        `\` in literals (NO_BACKSLASH_ESCAPES), and text, not a binary string."""
        return "char(92 using utf8mb4)"

    def concat_sql(self, left: str, right: str) -> str:
        """`concat()`: in MySQL `||` is logical OR unless PIPES_AS_CONCAT."""
        return f"concat({left}, {right})"

    def decimal_key_text(self, c: str, column: Column) -> str:
        """DECIMAL's char cast is exact and plain, at the declared scale; drop
        the trailing fractional zeros that scale adds. Zero is tested first:
        MySQL can hold a negative zero, which would render `-0`. A ZEROFILL
        column's display padding is dropped by `+ 0` (RT-HM5)."""
        t = f"cast({c} + 0 as char)" if _display_padded(column) else f"cast({c} as char)"
        return (
            f"case when {c} = 0 then '0' "
            f"when locate('.', {t}) > 0 "
            f"then trim(trailing '.' from trim(trailing '0' from {t})) "
            f"else {t} end"
        )

    def _decimal_text(self, column: Column, c: str) -> str:
        """Render a DECIMAL column at the float scale, exactly or not at all.

        MySQL's `CAST(x AS DECIMAL(p,s))` does not raise when `x` does not fit:
        inside a SELECT, whatever the sql_mode, it *clamps* to the type's
        largest value and leaves only warning 1264. The original
        `decimal(38,6)` therefore turned every DECIMAL(65,0) value of 1e32 or
        more into `99999999999999999999999999999999.999999` (and the negative
        twin), so 1e32 and 5e40 compared equal - to each other, and to a
        PostgreSQL or DuckDB row really holding that maximum. Verified as a
        false match end to end against all three, and again at float_scale 20,
        where decimal(38,20) clamps from 1e18.

        Two changes close it. The cast widens to `decimal(65,s)`, MySQL's
        widest, so values up to 65 - s integer digits now render exactly;
        rendered text does not depend on the cast's precision, so every value
        the old cast handled renders byte-identically and no checksum moves.
        And a value that still does not fit raises instead of clamping. The
        guard compares against the cast's own maximum, 10^(65-s) - 10^-s,
        which is exact: a value under it could only round past it with 66
        significant digits, and a value over it could only round down into it
        with 66, and no MySQL DECIMAL holds more than 65.

        MySQL has no `error()` and a SELECT downgrades most errors to warnings,
        but UUID_TO_BIN on a non-UUID is a hard error (1411) that quotes its
        argument. MySQL cuts that quote at 128 characters, so the argument
        leads with the value (at most 67) and then the column; `query` turns
        the error into one naming the side and explaining the refusal. The
        column name goes in as a hex literal, like the NULL sentinel, so no
        name can break out of it whatever sql_mode does to backslashes.
        """
        s = self.float_scale
        if s > MYSQL_MAX_DECIMAL_SCALE:
            # MySQL itself refuses with error 1425; say so before any query,
            # naming the side and column rather than a generated expression.
            raise self._err(
                f"DECIMAL column {column.name!r} cannot be compared at "
                f"float_scale {s}; MySQL's maximum DECIMAL scale is "
                f"{MYSQL_MAX_DECIMAL_SCALE}"
            )
        widest = "9" * (MYSQL_MAX_DECIMAL_DIGITS - s) + ("." + "9" * s if s else "")
        where = f" in column {column.name!r}".encode()
        return (
            f"case when abs({c}) > {widest} "
            f"then cast(uuid_to_bin(concat('{DECIMAL_RANGE_MARKER}', {c}, "
            f"convert(0x{where.hex()} using utf8mb4))) as char) "
            f"else cast(cast({c} as decimal({MYSQL_MAX_DECIMAL_DIGITS},{s})) "
            f"as char) end"
        )

    def count_distinct_key(self, key: KeySpec, identity: str) -> str:
        """Count distinct keys by their bytes, never under a collation.

        A hashed key's identity is `concat_ws(char(31), ...)` text in the
        connection collation, utf8mb4_0900_ai_ci by default. That collation is
        case- and accent-blind ('a' = 'A', 'e' = 'é') and treats control
        characters as ignorable, so the separator vanishes and the integer keys
        (5, 944) and (59, 44) both compare as '5944'. Counted under it, unique
        keys were refused as duplicates (RT-E5, RT-E8). The rows themselves are
        matched in Python by exact text, so the count is the only place that
        equality leaked in. A binary string compares byte for byte, with no
        padding. A single integer key is not text and stays as it is.
        """
        if not key.hashed:
            return super().count_distinct_key(key, identity)
        return f"count(distinct cast({identity} as binary))"

    def hash_expr(self, text_expr: str) -> str:
        """Fold canonical text into a positive 60-bit integer.

        `CONV(hex, 16, 10)` works at 64-bit precision, so 15 hex digits (60
        bits) convert exactly, and the result equals PostgreSQL's bit-cast and
        DuckDB's hex-cast for the same input.
        """
        return (
            f"cast(conv(substr(md5({text_expr}), 1, {HASH_HEX_CHARS}), 16, 10) "
            f"as unsigned)"
        )

    def int_div(self, numerator: str, denominator: str) -> str:
        """Truncating integer division. `DIV` is MySQL's, and stays exact on
        the DECIMAL operands `wide_int` produces, unlike `/` which returns a
        scaled result."""
        return f"(({numerator}) div ({denominator}))"

    def wide_int(self, expr: str) -> str:
        """Widen past 64 bits before arithmetic. DECIMAL holds up to 65 digits,
        which the key offset cannot overflow."""
        return f"cast(({expr}) as decimal(65,0))"

    def sum_wide(self, expr: str) -> str:
        """Sum row hashes without overflowing, and return 0 for an empty group.

        DECIMAL(65,0) is MySQL's widest, far beyond any row count times 2^60.
        """
        return f"coalesce(sum(cast(({expr}) as decimal(65,0))), 0)"


def _base(column: Column) -> str:
    """The bare MySQL type name: `enum`, `int`, `year`."""
    return column.raw_type.lower().split("(")[0].strip()


def _display_padded(column: Column) -> bool:
    """Whether the char cast carries display padding the value does not:
    ZEROFILL (`int(6) unsigned zerofill`) and YEAR (0 prints `0000`)."""
    return "zerofill" in column.declared.lower() or _base(column) == "year"


def _column(
    name: str,
    data_type: str,
    precision: Any,
    declared: str = "",
    charset: Any = None,
) -> Column:
    """Build a Column from one `information_schema.columns` row.

    TIMESTAMP is an instant: MySQL stores it as UTC and converts it through
    the session time zone, which `connect` pins to +00:00, so it renders the
    same text as a PostgreSQL timestamptz holding that instant. DATETIME is
    naive wall-clock time. The name says neither, so `tz_aware` is set
    explicitly, as BigQuery does - otherwise TIMESTAMP against timestamptz
    warned of a timezone mismatch that is not there (RT-A20, RT-C12).

    BIT carries its width in the raw type, `bit(8)`, which `_bit_text`
    needs to keep leading zeros; `data_type` alone says only `bit`.
    """
    low = data_type.lower()
    raw = f"{data_type}({precision})" if low == "bit" and precision else data_type
    tz = {"timestamp": True, "datetime": False}.get(low)
    return Column(
        name,
        map_type_mysql(data_type),
        raw,
        tz_aware=tz,
        declared=declared,
        charset=str(charset) if charset else None,
    )


def map_type_mysql(raw: str) -> LogicalType:
    """Map a MySQL type name onto a logical category.

    MySQL's `information_schema` reports lower-case bare names (`int`, `bigint`,
    `decimal`, `double`, `varchar`, `datetime`, ...). Note that BOOLEAN is an
    alias for `tinyint`, so a boolean column reads as an integer here - correct
    for MySQL-to-MySQL, and a cross-engine trap worth knowing when comparing a
    MySQL tinyint against a real boolean on another engine.
    """
    t = raw.lower().split("(")[0].strip()
    if t in {
        "int",
        "integer",
        "bigint",
        "smallint",
        "mediumint",
        "tinyint",
        "int unsigned",
        "bigint unsigned",
        "year",
    }:
        return LogicalType.INTEGER
    if t in {"decimal", "numeric", "dec", "fixed"}:
        return LogicalType.DECIMAL
    if t in {"float", "double", "double precision", "real"}:
        return LogicalType.FLOAT
    if t in {"bool", "boolean"}:  # rarely seen; MySQL stores these as tinyint
        return LogicalType.BOOLEAN
    if t in {"date"}:
        return LogicalType.DATE
    if t in {"datetime", "timestamp"}:
        return LogicalType.TIMESTAMP
    if t == "time":
        return LogicalType.TIME
    if t in _BINARY_TYPES:
        return LogicalType.BINARY
    if t == "json":
        return LogicalType.JSON
    if t == "bit":
        # BIT(n), a bit string of n digits - not a boolean (MySQL's BOOLEAN
        # is TINYINT(1)), and rendered as PostgreSQL renders bit(n).
        return LogicalType.BITS
    if t in {
        "char",
        "varchar",
        "text",
        "tinytext",
        "mediumtext",
        "longtext",
        "enum",
        "set",
    }:
        return LogicalType.STRING
    return LogicalType.UNKNOWN
