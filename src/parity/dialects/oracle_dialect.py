"""Oracle dialect (Oracle Database 21c and 23ai; python-oracledb, thin mode).

Verified against a live Oracle Database 23ai Free (23.26) in a container,
2026-11-04: the hash constant agrees, rows render byte-identically to DuckDB,
the FLOAT expression matches `canonical_float_text` on the adversarial sample
and on random doubles, and `tests/test_oracle.py` finds every planted
difference exactly. Nothing below is taken from the documentation alone.

The Oracle-specific decisions, each confirmed live:

- **MD5 of the UTF-8 bytes, which in an AL32UTF8 database the text already
  is.** `STANDARD_HASH(text, 'MD5')` hashes a VARCHAR2's bytes in the
  database character set; every NVARCHAR2/NCHAR/NCLOB value is converted to
  it first (a `||` with one national value would make the whole row
  national, AL16UTF16, and hash different bytes). Databases in any other
  character set are refused at connect, as SQL Server below 2019 is.
- **The 4,000-byte trap.** SQL VARCHAR2 holds 4,000 bytes; a longer
  concatenation raises ORA-01489, and `STANDARD_HASH` takes no CLOB (ORA-00902).
  So each row's text is hashed by `STANDARD_HASH` when an upper bound on its
  length, computed from the raw column lengths, is at most 4,000 bytes, and
  otherwise built as a CLOB and hashed by `parity_md5`, an MD5 written in
  PL/SQL and declared inline in the query (`WITH FUNCTION`), which needs no
  privilege beyond SELECT - `DBMS_CRYPTO` needs an EXECUTE grant a reader
  does not hold. Both paths hash the same text, so a row's hash never depends
  on which one ran; `parity_md5` equals Python's `hashlib.md5` on texts up to
  600 kB with emoji straddling every chunk boundary. A bound that ever came
  out too small would raise ORA-01489, never truncate.
- **Empty string is NULL.** Oracle stores `''` as NULL, so an Oracle NULL
  renders the sentinel and a migrated `''` on the other side is reported as a
  difference - which it is, on the target. `caveats` says so on every run
  comparing a text column. Inside a row, text that is empty after rendering
  (an all-space CHAR, an empty CLOB) is SQL NULL too, and `||` treats NULL as
  empty, so the sentinel is decided by the *column* being NULL, never by the
  rendered text.
- **FLOAT builds the shortest repr from an exact decomposition.** Oracle's
  BINARY_DOUBLE to NUMBER conversion keeps 17 significant digits (3,807 of
  4,000 sample doubles came back inexact), so the exact value is rebuilt as
  `m / 2^s` in NUMBER (m an integer below 2^53, both exact), and the
  BigQuery / SQL Server construction - the first of the 15-, 16- and
  17-digit roundings that converts back - runs on that. NUMBER to
  BINARY_DOUBLE is correctly rounded (3,000 random decimals of 15-38 digits).
- **DATE holds a time of day**, so it is a TIMESTAMP here, rendered with six
  zero fractional digits. BC dates use astronomical years (Oracle has no
  year 0: -0001 is 1 BC, rendered `0000`).
- **Session NLS settings are pinned** (`SESSION_PINS`): every one of them
  changes how a number or a date prints, or how text compares.
- **One snapshot**: `SET TRANSACTION READ ONLY` gives the whole walk one
  read-consistent snapshot, and refuses every write. Nothing is committed;
  `close` rolls back. `query` refuses anything but one SELECT.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from parity.dialects.base import (
    FLOAT_MAX_MAGNITUDE_SQL,
    FLOAT_OUT_OF_RANGE_MESSAGE,
    HASH_HEX_CHARS,
    Dialect,
    float_key_refusal,
    float_range_where,
    sql_literal,
)
from parity.types import Column, KeySpec, LogicalType

#: The longest VARCHAR2 the SQL engine builds under MAX_STRING_SIZE=STANDARD.
#: A row whose text may be longer is hashed as a CLOB (`parity_md5`).
VARCHAR_LIMIT = 4000

#: The first 15 hex digits of md5(''), as an integer: what an empty row text
#: hashes to. Oracle's empty text is NULL, and `STANDARD_HASH(NULL)` is NULL.
EMPTY_HASH = int(hashlib.md5(b"", usedforsecurity=False).hexdigest()[:HASH_HEX_CHARS], 16)

#: Numbers print with a period and no group separator, whatever the session.
NLS_NUMBERS = "'NLS_NUMERIC_CHARACTERS=''.,'''"

#: Query parameters `oracle://` understands, and the python-oracledb argument
#: each maps to. Anything else is refused rather than ignored.
_URL_PARAMS = {"tcp_connect_timeout": "tcp_connect_timeout", "protocol": "protocol"}

#: Every session setting that changes how a value renders, how text compares,
#: or which rows a query sees, pinned at connect (round G). Territory and
#: language first: setting them resets the numeric characters and formats.
SESSION_PINS = (
    "alter session set nls_language = 'AMERICAN'",
    "alter session set nls_territory = 'AMERICA'",
    # A non-Gregorian calendar (Thai Buddha, Japanese Imperial, ...) changes
    # every year `to_char` prints.
    "alter session set nls_calendar = 'GREGORIAN'",
    "alter session set nls_numeric_characters = '.,'",
    "alter session set nls_date_format = 'YYYY-MM-DD HH24:MI:SS'",
    "alter session set nls_date_language = 'AMERICAN'",
    "alter session set nls_timestamp_format = 'YYYY-MM-DD HH24:MI:SS.FF9'",
    "alter session set nls_timestamp_tz_format = 'YYYY-MM-DD HH24:MI:SS.FF9 TZH:TZM'",
    "alter session set nls_sort = 'BINARY'",
    "alter session set nls_comp = 'BINARY'",
    # Inherited from the client otherwise (verified: +01:00 from a Windows
    # laptop); TIMESTAMP WITH LOCAL TIME ZONE reads through it.
    "alter session set time_zone = '+00:00'",
    # A national-character value that does not convert to the database
    # character set raises instead of turning into a replacement character.
    "alter session set nls_nchar_conv_excp = 'TRUE'",
    # FORCE replaces literals with binds, which can change the declared
    # length of a CASE result; EXACT is the default.
    "alter session set cursor_sharing = 'EXACT'",
    # With QUERY_REWRITE_INTEGRITY = STALE_TOLERATED a checksum could be
    # answered from a stale materialized view instead of the table.
    "alter session set query_rewrite_enabled = 'FALSE'",
)

#: Leads the text `parity_refuse` raises with, so `query` can find it.
_REFUSAL = re.compile(r"ORA-20001: (parity: .*)", re.S)

#: Words that never appear in the SQL this dialect generates outside a quoted
#: identifier or a string literal, and that any statement able to write, lock
#: or change the session needs.
_WRITE_WORDS = re.compile(
    r"\b(into|insert|update|delete|merge|drop|alter|create|truncate|grant|revoke"
    r"|lock|commit|rollback|savepoint|execute|exec|call|begin|declare|set"
    r"|purge|flashback|audit|noaudit|rename|comment|analyze|explain)\b",
    re.IGNORECASE,
)

#: A quoted identifier or a string literal (`''` escapes `'`).
_QUOTED = re.compile(r"\"[^\"]*\"|'(?:[^']|'')*'")


def is_single_select(sql: str) -> bool:
    """Whether `sql` is one plain SELECT - no second statement, no write.

    This dialect only runs SQL it generated: one SELECT, no `;`, no comment,
    and none of `_WRITE_WORDS` outside identifiers and literals. The
    transaction is READ ONLY as well, so this is the second line.
    """
    rest = _QUOTED.sub(" q ", sql)
    if '"' in rest or "'" in rest:
        return False
    if not rest.lstrip().lower().startswith("select"):
        return False
    if ";" in rest or "--" in rest or "/*" in rest:
        return False
    return _WRITE_WORDS.search(rest) is None


def parse_oracle_url(connection_string: str) -> dict[str, Any]:
    """Turn ``oracle://user:pw@host:port/service_name?...`` into connect args.

    The path is the *service name* (FREEPDB1, ORCLPDB1, an ADB service),
    never a SID. Parameters are whitelisted (`_URL_PARAMS`); the password is
    never part of an error.
    """
    url = urlparse(connection_string)
    if not url.hostname:
        raise ValueError(
            "oracle connection string needs a host: "
            "oracle://user:password@host[:port]/service_name"
        )
    service = unquote(url.path.lstrip("/"))
    if not service or "/" in service:
        raise ValueError(
            "oracle connection string needs exactly one service name: "
            "oracle://user:password@host[:port]/service_name"
        )
    if not url.username:
        raise ValueError(
            "oracle connection string needs a user: password authentication "
            "(user:password@host) is the only kind verified so far"
        )
    try:
        port = url.port or 1521
    except ValueError as exc:
        raise ValueError("oracle connection string has an invalid port") from exc
    params = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
    unknown = sorted(set(params) - set(_URL_PARAMS))
    if unknown:
        raise ValueError(
            f"unknown oracle connection parameter(s) {unknown}; "
            f"supported: {sorted(_URL_PARAMS)}"
        )
    args: dict[str, Any] = {
        "user": unquote(url.username),
        "password": unquote(url.password or ""),
        "host": url.hostname,
        "port": port,
        "service_name": service,
    }
    if "tcp_connect_timeout" in params:
        raw = params["tcp_connect_timeout"].strip()
        try:
            args["tcp_connect_timeout"] = float(raw)
        except ValueError as exc:
            raise ValueError(
                f"oracle tcp_connect_timeout must be a number of seconds, got {raw!r}"
            ) from exc
    if "protocol" in params:
        proto = params["protocol"].strip().lower()
        if proto not in ("tcp", "tcps"):
            raise ValueError(f"oracle protocol must be tcp or tcps, got {proto!r}")
        args["protocol"] = proto
    return args


# ------------------------------------------------- inline PL/SQL functions
#
# Declared in a query's WITH clause, so they need no CREATE privilege and
# leave nothing behind; `query` prepends each one the statement calls.

#: MD5 of a CLOB's UTF-8 bytes, as 32 upper-case hex digits (RFC 1321). Words
#: are NUMBER, so no 32-bit overflow can occur; OR, XOR and NOT are spelled
#: with BITAND over disjoint bits. The constants are the RFC's table, not
#: computed from SIN, so nothing depends on Oracle's trigonometry.
_MD5_K = (
    "3614090360, 3905402710, 606105819, 3250441966, 4118548399, 1200080426, "
    "2821735955, 4249261313, 1770035416, 2336552879, 4294925233, 2304563134, "
    "1804603682, 4254626195, 2792965006, 1236535329, 4129170786, 3225465664, "
    "643717713, 3921069994, 3593408605, 38016083, 3634488961, 3889429448, "
    "568446438, 3275163606, 4107603335, 1163531501, 2850285829, 4243563512, "
    "1735328473, 2368359562, 4294588738, 2272392833, 1839030562, 4259657740, "
    "2763975236, 1272893353, 4139469664, 3200236656, 681279174, 3936430074, "
    "3572445317, 76029189, 3654602809, 3873151461, 530742520, 3299628645, "
    "4096336452, 1126891415, 2878612391, 4237533241, 1700485571, 2399980690, "
    "4293915773, 2240044497, 1873313359, 4264355552, 2734768916, 1309151649, "
    "4149444226, 3174756917, 718787259, 3951481745"
)

PARITY_MD5 = f"""function parity_md5(t clob) return varchar2 is
  m constant number := 4294967296;
  type ka is varray(64) of number;
  k constant ka := ka({_MD5_K});
  type ia is table of number index by pls_integer;
  x ia;
  a0 number := 1732584193; b0 number := 4023233417;
  c0 number := 2562383102; d0 number := 271733878;
  b blob; n number := 0; pos number := 1; chunk raw(32767); buf raw(32767);
  dest_off integer := 1; src_off integer := 1; ctx integer := 0; warn integer;
  function rot(i pls_integer) return pls_integer is
  begin
    if i < 16 then return case mod(i, 4) when 0 then 7 when 1 then 12 when 2 then 17 else 22 end;
    elsif i < 32 then return case mod(i, 4) when 0 then 5 when 1 then 9 when 2 then 14 else 20 end;
    elsif i < 48 then return case mod(i, 4) when 0 then 4 when 1 then 11 when 2 then 16 else 23 end;
    end if;
    return case mod(i, 4) when 0 then 6 when 1 then 10 when 2 then 15 else 21 end;
  end;
  function rotl(v number, s pls_integer) return number is
  begin return mod(v * power(2, s), m) + trunc(v / power(2, 32 - s)); end;
  function le(v number) return varchar2 is h varchar2(8);
  begin h := to_char(v, 'FM0XXXXXXX');
    return substr(h, 7, 2) || substr(h, 5, 2) || substr(h, 3, 2) || substr(h, 1, 2); end;
  procedure block(p raw, off pls_integer) is
    aa number := a0; bb number := b0; cc number := c0; dd number := d0;
    f number; g pls_integer; tmp number;
  begin
    for i in 0..15 loop
      x(i) := utl_raw.cast_to_binary_integer(utl_raw.substr(p, off + i * 4, 4), 2);
      if x(i) < 0 then x(i) := x(i) + m; end if;
    end loop;
    for i in 0..63 loop
      if i < 16 then f := bitand(bb, cc) + bitand(m - 1 - bb, dd); g := i;
      elsif i < 32 then f := bitand(bb, dd) + bitand(cc, m - 1 - dd); g := mod(5 * i + 1, 16);
      elsif i < 48 then
        f := bb + cc - 2 * bitand(bb, cc); f := f + dd - 2 * bitand(f, dd); g := mod(3 * i + 5, 16);
      else
        tmp := bb + (m - 1 - dd) - bitand(bb, m - 1 - dd);
        f := cc + tmp - 2 * bitand(cc, tmp); g := mod(7 * i, 16);
      end if;
      f := mod(f + aa + k(i + 1) + x(g), m);
      aa := dd; dd := cc; cc := bb; bb := mod(bb + rotl(f, rot(i)), m);
    end loop;
    a0 := mod(a0 + aa, m); b0 := mod(b0 + bb, m);
    c0 := mod(c0 + cc, m); d0 := mod(d0 + dd, m);
  end;
begin
  if t is not null and dbms_lob.getlength(t) > 0 then
    dbms_lob.createtemporary(b, true, dbms_lob.call);
    dbms_lob.converttoblob(b, t, dbms_lob.lobmaxsize, dest_off, src_off,
      nls_charset_id('AL32UTF8'), ctx, warn);
    if warn <> 0 then
      raise_application_error(-20001, 'parity: row text does not convert to UTF-8');
    end if;
    n := dbms_lob.getlength(b);
  end if;
  while n > 0 and pos + 16384 <= n + 1 loop
    chunk := dbms_lob.substr(b, 16384, pos);
    for j in 0..255 loop block(chunk, j * 64 + 1); end loop;
    pos := pos + 16384;
  end loop;
  if n >= pos then buf := dbms_lob.substr(b, n - pos + 1, pos); end if;
  while utl_raw.length(buf) >= 64 loop
    block(buf, 1);
    if utl_raw.length(buf) = 64 then buf := null; else buf := utl_raw.substr(buf, 65); end if;
  end loop;
  buf := utl_raw.concat(buf, hextoraw('80'));
  while mod(utl_raw.length(buf), 64) <> 56 loop buf := utl_raw.concat(buf, hextoraw('00')); end loop;
  buf := utl_raw.concat(buf, hextoraw(le(mod(n * 8, m)) || le(trunc(n * 8 / m))));
  block(buf, 1);
  if utl_raw.length(buf) = 128 then block(buf, 65); end if;
  if b is not null then dbms_lob.freetemporary(b); end if;
  return le(a0) || le(b0) || le(c0) || le(d0);
end;
"""

#: Raise with a message naming the value and the column: Oracle's own
#: conversion errors quote one character of their input (ORA-01722 printed
#: `'p'`), which names nothing a user could act on.
PARITY_REFUSE = """function parity_refuse(msg varchar2) return varchar2 is
begin
  raise_application_error(-20001, msg);
end;
"""

#: A BLOB's bytes as upper-case hex, as a CLOB: RAWTOHEX takes at most 2,000
#: bytes in SQL.
PARITY_BLOB_HEX = """function parity_blob_hex(b blob) return clob is
  out clob; n number; pos number := 1;
begin
  if b is null then return null; end if;
  n := dbms_lob.getlength(b);
  dbms_lob.createtemporary(out, true, dbms_lob.call);
  while pos <= n loop
    dbms_lob.writeappend(out, least(16000, (n - pos + 1) * 2),
      rawtohex(dbms_lob.substr(b, 8000, pos)));
    pos := pos + 8000;
  end loop;
  return out;
end;
"""

#: The canonical text of a BINARY_DOUBLE (`canonical_float_text`), as an
#: inline function. The same construction written as one SQL expression
#: repeats the exact value hundreds of times - 171 kB per column, and a
#: 500-column checksum query Oracle spent minutes parsing - so it lives here,
#: where every intermediate is a variable. See `OracleDialect._float_text`.
PARITY_FLOAT = f"""function parity_float(x binary_double, s pls_integer, col varchar2) return varchar2 is
  ax binary_double; e number; m number; d number; e10 number;
  sh number; r number; frac varchar2(40); zero varchar2(41);
  function cand(n pls_integer, ties boolean) return number is
    k number := n - 1 - e10; rr number := round(d, k); tt number;
  begin
    if not ties then return rr; end if;
    tt := trunc(d, k);
    if 2 * d = tt + rr and rr <> tt and mod(tt * power(10, k), 2) = 0 then return tt; end if;
    return rr;
  end;
begin
  if x is null then return null; end if;
  if x is nan then return 'NaN'; end if;
  if x = binary_double_infinity then return 'Infinity'; end if;
  if x = -binary_double_infinity then return '-Infinity'; end if;
  if s > 0 then frac := '.' || rpad('0', s, '0'); end if;
  zero := '0' || frac;
  ax := abs(x);
  if ax < to_binary_double(power(10, -(s + 1))) then return zero; end if;
  e := floor(ln(ax) / ln(2d));
  if ax >= power(2d, to_binary_double(e + 1)) then e := e + 1;
  elsif ax < power(2d, to_binary_double(e)) then e := e - 1; end if;
  m := to_number(ax * power(2d, to_binary_double(52 - e)));
  if ax < to_binary_double(9007199254740992) then
    d := m / power(2, 52 - e);
    e10 := floor(log(10, d));
    if d >= power(10, e10 + 1) then e10 := e10 + 1;
    elsif d < power(10, e10) then e10 := e10 - 1; end if;
    sh := cand(15, false);
    if to_binary_double(sh) <> ax then
      sh := cand(16, true);
      if to_binary_double(sh) <> ax then sh := cand(17, true); end if;
    end if;
    r := round(sh, s);
    if r = 0 then return zero; end if;
    return case when x < 0 then '-' end || to_char(r, 'FM9999999999999999990' || frac);
  elsif ax < to_binary_double({FLOAT_MAX_MAGNITUDE_SQL}) then
    return case when x < 0 then '-' end
      || to_char(m * power(2, e - 52), 'FM' || rpad('9', 35, '9') || '0') || frac;
  end if;
  raise_application_error(-20001, '{FLOAT_OUT_OF_RANGE_MESSAGE}' || to_char(x) || col);
end;
"""

#: `round(c, s)` as plain text with exactly `s` places - the DECIMAL rule.
PARITY_DEC = f"""function parity_dec(c number, s pls_integer, col varchar2) return varchar2 is
  t varchar2(200);
begin
  if c is null then return null; end if;
  if abs(c) >= 1e38 then
    raise_application_error(-20001, 'parity: DECIMAL value '
      || to_char(c, 'TM9', {NLS_NUMBERS}) || ' is too large to compare (10^38 or more)' || col);
  end if;
  t := to_char(round(c, s), 'TM9', {NLS_NUMBERS});
  if substr(t, 1, 1) = '.' then t := '0' || t;
  elsif substr(t, 1, 2) = '-.' then t := '-0' || substr(t, 2); end if;
  if s > 0 then
    if instr(t, '.') = 0 then t := t || '.' || rpad('0', s, '0');
    else t := rpad(t, instr(t, '.') + s, '0'); end if;
  end if;
  return t;
end;
"""

_FUNCTIONS = {
    "parity_md5(": PARITY_MD5,
    "parity_refuse(": PARITY_REFUSE,
    "parity_blob_hex(": PARITY_BLOB_HEX,
    "parity_float(": PARITY_FLOAT,
    "parity_dec(": PARITY_DEC,
}


def _base(raw: str) -> str:
    """The type name without parameters, upper case: `TIMESTAMP(6)` ->
    `TIMESTAMP`, `TIMESTAMP(6) WITH TIME ZONE` -> `TIMESTAMP WITH TIME ZONE`."""
    return re.sub(r"\(.*?\)", "", raw.upper()).strip()


def _precision(raw: str) -> int | None:
    """The first parenthesised number of a raw type, or None."""
    m = re.search(r"\((\d+)", raw)
    return int(m.group(1)) if m else None


def map_type_oracle(raw: str) -> LogicalType:
    """Map an Oracle type (as `_column` spells it) onto a logical category.

    `NUMBER(p,0)` and `NUMBER(*,0)` hold integers; `NUMBER` without a scale
    and `FLOAT(p)` - a *decimal* NUMBER with binary precision, not IEEE -
    hold decimals. DATE holds a time of day, so it is a TIMESTAMP.
    """
    t = _base(raw)
    if t == "NUMBER":
        m = re.search(r",\s*(-?\d+)\)", raw)
        if m and int(m.group(1)) <= 0:
            return LogicalType.INTEGER
        return LogicalType.DECIMAL
    if t == "FLOAT":
        return LogicalType.DECIMAL
    if t in ("BINARY_FLOAT", "BINARY_DOUBLE"):
        return LogicalType.FLOAT
    if t in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "CLOB", "NCLOB"):
        return LogicalType.STRING
    if t == "DATE" or t.startswith("TIMESTAMP"):
        return LogicalType.TIMESTAMP
    if t.startswith("INTERVAL"):
        return LogicalType.INTERVAL
    if t in ("RAW", "BLOB"):
        return LogicalType.BINARY
    if t == "BOOLEAN":
        return LogicalType.BOOLEAN
    return LogicalType.UNKNOWN


def _column(name: str, data_type: str, precision: Any, scale: Any) -> Column:
    """Build a Column from one ALL_TAB_COLUMNS row.

    A NUMBER's precision and scale are folded into `raw_type`
    (`NUMBER(10,0)`, `NUMBER(*,0)`, `NUMBER`), since only the scale tells an
    integer from a decimal. Timestamps already carry their precision.
    """
    raw = data_type
    if data_type == "NUMBER" and scale is not None:
        p = "*" if precision is None else str(int(precision))
        raw = f"NUMBER({p},{int(scale)})"
    elif data_type == "FLOAT" and precision is not None:
        raw = f"FLOAT({int(precision)})"
    t = _base(raw)
    tz: bool | None = None
    if t in ("TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE"):
        tz = True
    elif t in ("DATE", "TIMESTAMP"):
        tz = False
    return Column(name, map_type_oracle(raw), raw, tz_aware=tz)


class OracleDialect(Dialect):
    name = "oracle"
    #: Replaced at connect by the session's current schema (the login user,
    #: unless a logon trigger moved it).
    default_schema = "PARITY"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Per-instance state; nothing touches the network until connect."""
        super().__init__(*args, **kwargs)
        self._conn: Any = None
        #: `normalize` output -> (SQL upper bound on its UTF-8 length or None
        #: for unbounded, whether the expression is a CLOB). `hash_parts`
        #: reads it to choose between STANDARD_HASH and `parity_md5`.
        self._parts: dict[str, tuple[str | None, bool]] = {}
        #: Tables carrying an enabled VPD policy, found by `columns`.
        self._policies: set[str] = set()
        self.version = ""
        #: Whether this login may call DBMS_CRYPTO.HASH, found at connect.
        #: Then long rows are hashed by it, natively; otherwise by
        #: `parity_md5`. Both are MD5 of the row's UTF-8 bytes.
        self.crypto = False

    # ---------------------------------------------------------------- setup

    def connect(self, connection_string: str) -> None:
        """Open a thin-mode session, pin it, and start one READ ONLY transaction."""
        try:
            args = parse_oracle_url(connection_string)
        except ValueError as exc:
            raise self._err(str(exc)) from exc

        import oracledb

        self._conn = oracledb.connect(**args)
        self._conn.outputtypehandler = _output_type_handler
        self.version = str(self._conn.version)
        major = int(self.version.split(".")[0])
        if major < 19:
            raise self._err(
                f"Oracle {self.version} is not supported: this dialect is "
                "verified on 21c and 23ai, and needs 19c or newer"
            )
        cur = self._conn.cursor()
        try:
            for pin in SESSION_PINS:
                cur.execute(pin)
            cur.execute(
                "select value from nls_database_parameters "
                "where parameter = 'NLS_CHARACTERSET'"
            )
            charset = str(cur.fetchone()[0])
            cur.execute("select sys_context('userenv', 'current_schema') from dual")
            self.default_schema = str(cur.fetchone()[0])
            try:
                # 2 is DBMS_CRYPTO.HASH_MD5. A reader without EXECUTE on it
                # gets ORA-00904 here, which leaves the session usable.
                cur.execute(
                    "select rawtohex(dbms_crypto.hash(to_clob('abc'), 2)) from dual"
                )
                self.crypto = (
                    str(cur.fetchone()[0]).lower()
                    == hashlib.md5(b"abc", usedforsecurity=False).hexdigest()
                )
            except Exception:  # noqa: BLE001 - absent is the expected case
                self.crypto = False
            # The snapshot: a READ ONLY transaction reads the database as of
            # its first statement, for every query that follows, and refuses
            # any write. It has to be the first statement of the transaction,
            # so it is issued after the pins (ALTER SESSION commits) and the
            # two probes (rolled back first).
            self._conn.rollback()
            cur.execute("set transaction read only")
        finally:
            cur.close()
        if charset != "AL32UTF8":
            raise self._err(
                f"database character set {charset} is not supported: rows are "
                "hashed as the UTF-8 bytes every other engine hashes, which "
                "Oracle's VARCHAR2 holds only in an AL32UTF8 database"
            )

    def close(self) -> None:
        """End the transaction by rolling back - never committing - and close.

        After `cancel` the rollback itself can report the cancelled call
        (ORA-01013); nothing was ever written, so that is not worth raising
        over the error that caused the cancel.
        """
        try:
            self._conn.rollback()
        except Exception:  # noqa: BLE001, S110 - see the docstring
            pass
        finally:
            self._conn.close()

    def cancel(self) -> None:
        """Interrupt the running call (python-oracledb's `Connection.cancel`)."""
        if self._conn is not None:
            self._conn.cancel()

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run one SELECT, with the inline functions it calls declared first."""
        if not is_single_select(sql):
            raise self._err(f"refusing to run anything but a single SELECT: {sql[:60]!r}")
        decls = [body for marker, body in _FUNCTIONS.items() if marker in sql]
        text = f"with {''.join(decls)}{sql}" if decls else sql
        cur = self._conn.cursor()
        try:
            cur.execute(text)
            return [tuple(row) for row in cur.fetchall()]
        except Exception as exc:
            if "ORA-01466" in str(exc):
                # A READ ONLY transaction cannot read a table whose definition
                # (creation, ALTER, TRUNCATE, partition maintenance, even the
                # first insert into a table created without a segment) is
                # newer than its snapshot, by a coarse clock (verified: a
                # dialect opened at once after create + insert always fails,
                # 1.1 s later sometimes, 1.6 s or more later never).
                raise self._err(
                    "the table's definition changed after this diff's snapshot "
                    "began (ORA-01466): it was created, altered or truncated "
                    "during the diff or within a few seconds before it. "
                    "Run the diff again"
                ) from exc
            found = _REFUSAL.search(str(exc))
            if found is None:
                raise
            message = found.group(1).split("\nORA-", 1)[0].strip()
            raise self._err(message) from exc
        finally:
            cur.close()

    # ------------------------------------------------------------ metadata

    def columns(self, table: str) -> list[Column]:
        """Introspect columns from ALL_TAB_COLUMNS, which lists only tables the
        session may SELECT - no catalog privilege is needed."""
        schema, name = self.split_table(table, self.default_schema)
        rows = self.query(
            "select column_name, data_type, data_precision, data_scale "
            "from all_tab_columns "
            f"where owner = {sql_literal(schema)} and table_name = {sql_literal(name)} "
            "order by column_id"
        )
        if not rows:
            raise self._err(self._not_found(table, schema, name))
        try:
            policies = self.query(
                "select count(*) from all_policies "
                f"where object_owner = {sql_literal(schema)} "
                f"and object_name = {sql_literal(name)} and enable = 'YES'"
            )
            if policies and int(policies[0][0]) > 0:
                self._policies.add(f"{schema}.{name}")
        except Exception:  # noqa: BLE001, S110 - a caveat probe must never stop the diff
            pass
        return [_column(str(r[0]), str(r[1]), r[2], r[3]) for r in rows]

    def _not_found(self, table: str, schema: str, name: str) -> str:
        """A missing table: near misses by case, and the GRANT hint."""
        message = f"table not found: {table} (looked in schema {schema!r})"
        try:
            near = self.query(
                "select distinct owner, table_name from all_tab_columns "
                f"where upper(table_name) = upper({sql_literal(name)})"
            )
        except Exception:  # noqa: BLE001 - diagnosing an error must never replace it
            near = []
        others = sorted(f"{s}.{t}" for s, t in near if (s, t) != (schema, name))
        if others:
            message += (
                ". Names are matched exactly, including case (Oracle stores "
                f"unquoted names in upper case) - did you mean {' or '.join(others)}?"
            )
        return (
            message + ". If it does exist, this login may lack SELECT on it: "
            "ALL_TAB_COLUMNS lists only tables the session can read"
        )

    def quote(self, identifier: str) -> str:
        """Double-quote an identifier. Oracle has no escape for `"` inside a
        quoted identifier and forbids it in names, so one is refused."""
        if '"' in identifier or "\x00" in identifier:
            raise self._err(f"identifier {identifier!r} cannot be quoted on Oracle")
        return f'"{identifier}"'

    def qualify(self, table: str) -> str:
        """Always schema-qualified: `columns` introspected `default_schema`,
        so the rows must come from there too, whatever CURRENT_SCHEMA is."""
        schema, name = self.split_table(table, self.default_schema)
        return f"{self.quote(schema)}.{self.quote(name)}"

    def caveats(self, columns: Sequence[Column]) -> list[str]:
        """What makes this side's comparison less than exact, for the output."""
        out: list[str] = []
        text = sorted(c.name for c in columns if c.logical_type is LogicalType.STRING)
        if text:
            out.append(
                "Oracle stores an empty string as NULL, so text columns "
                f"{text[:5]}{' ...' if len(text) > 5 else ''} cannot hold '': "
                "a row the other side holds as '' is reported as NULL against "
                "''. That is a real difference on the target, but it may be "
                "how the migration chose to carry Oracle's NULLs"
            )
        fine = sorted(
            c.name for c in columns if self.holds_sub_micro(c) and not c.sub_micro
        )
        fine += sorted(
            c.name
            for c in columns
            if c.logical_type is LogicalType.INTERVAL and (_frac_digits(c.raw_type) > 6)
        )
        if fine:
            out.append(
                f"columns {fine} hold digits past the microsecond; they are "
                "compared at microseconds (the rest truncated), so a "
                "difference below one microsecond is not seen"
            )
        if self._policies:
            out.append(
                f"{sorted(self._policies)} carry an enabled Virtual Private "
                "Database policy, which can hide rows from this login: the "
                "comparison sees only the rows the policy lets it read"
            )
        return out

    # --------------------------------------------------- SQL spelling hooks

    def group_by(self, position: int, expr: str) -> str:
        """The expression itself: GROUP BY takes no ordinal by default."""
        return expr

    def count_distinct_key(self, key: KeySpec, identity: str) -> str:
        """Distinct keys by SHA-256 of their bytes, never by text equality,
        which a column collation (MAX_STRING_SIZE=EXTENDED) could fold. An
        empty identity is NULL in Oracle, so it gets a one-byte stand-in no
        32-byte digest can equal."""
        if not key.hashed:
            return f"count(distinct {identity})"
        return f"count(distinct nvl(standard_hash({identity}, 'SHA256'), hextoraw('00')))"

    def escape_text(self, expr: str) -> str:
        """The shared escape, under the binary collation whatever the column's
        own: a linguistic collation (MAX_STRING_SIZE=EXTENDED databases can
        declare one per column) may treat chr(31) as ignorable. The result is
        handed back under USING_NLS_COMP (BINARY, pinned): text carrying an
        explicit collation cannot be appended to a CLOB (ORA-43912), which
        the long-row hash path does."""
        escaped = super().escape_text(f"({expr} collate binary)")
        return f"({escaped} collate using_nls_comp)"

    def _escape_lob(self, expr: str) -> str:
        """The shared escape on a CLOB, which takes no COLLATE clause (ORA-00932)
        and has no collation of its own: REPLACE on a LOB is binary."""
        return super().escape_text(expr)

    def concat_sql(self, left: str, right: str) -> str:
        """`||` - which treats NULL as the empty string, as row text needs."""
        return f"({left} || {right})"

    def _concat(self, parts: list[str]) -> str:
        """Join with chr(31) and `||`: Oracle has no `concat_ws`, and `||`
        takes any number of operands, so there is nothing to nest."""
        if len(parts) == 1:
            return parts[0]
        return "(" + " || chr(31) || ".join(parts) + ")"

    def hash_expr(self, text_expr: str) -> str:
        """The first 15 hex digits of `STANDARD_HASH(text, 'MD5')`, as NUMBER.

        For VARCHAR2 text of at most 4,000 bytes; NULL text is the empty text.
        Rows that may be longer go through `hash_parts`.
        """
        hexed = (
            f"substr(rawtohex(standard_hash({text_expr}, 'MD5')), 1, {HASH_HEX_CHARS})"
        )
        return f"nvl(to_number({hexed}, '{'X' * HASH_HEX_CHARS}'), {EMPTY_HASH})"

    def _clob_hash(self, clob_expr: str) -> str:
        """The same 60 bits, of a CLOB: by DBMS_CRYPTO.HASH where the login may
        call it (it hashes a CLOB as AL32UTF8, natively - verified equal to
        `parity_md5`), else by `parity_md5`, about 0.5 MB/s."""
        if self.crypto:
            digest = f"rawtohex(dbms_crypto.hash({clob_expr}, 2))"
        else:
            digest = f"parity_md5({clob_expr})"
        hexed = f"substr({digest}, 1, {HASH_HEX_CHARS})"
        # An all-empty row text is a NULL CLOB, whose DBMS_CRYPTO hash is
        # NULL - and a NULL would drop the row out of the sum.
        return f"nvl(to_number({hexed}, '{'X' * HASH_HEX_CHARS}'), {EMPTY_HASH})"

    def hash_parts(self, parts: list[str]) -> str:
        """Hash a row: STANDARD_HASH when it fits a VARCHAR2, else `parity_md5`.

        Each part's upper bound comes from `normalize`; the CASE picks per
        row. The two branches hash the same text, so the choice never moves
        a checksum. A part of unknown origin has no bound and sends every row
        down the CLOB path, which is slower and exact.
        """
        infos = [self._parts.get(p, (None, True)) for p in parts]
        short = self._concat(
            [
                f"to_char({p})" if lob else p
                for p, (_, lob) in zip(parts, infos, strict=True)
            ]
        )
        long = (
            self._concat([f"to_clob({parts[0]})", *parts[1:]])
            if len(parts) > 1
            else f"to_clob({parts[0]})"
        )
        bounds = [b for b, _ in infos]
        if any(b is None for b in bounds):
            return self._clob_hash(long)
        static = [int(b) for b in bounds if b is not None and b.isdigit()]
        dynamic = [b for b in bounds if b is not None and not b.isdigit()]
        total = sum(static) + len(parts) - 1
        if not dynamic and total <= VARCHAR_LIMIT:
            return self.hash_expr(short)
        bound = " + ".join([str(total), *dynamic])
        return (
            f"case when {bound} <= {VARCHAR_LIMIT} then {self.hash_expr(short)} "
            f"else {self._clob_hash(long)} end"
        )

    def int_div(self, numerator: str, denominator: str) -> str:
        """Exact truncating division: `n - mod(n, d)` is an exact multiple of
        `d`, so the quotient is an integer and nothing rounds. Plain NUMBER
        `/` rounds its quotient to 38-40 digits. Operands are non-negative."""
        n, d = f"({numerator})", f"({denominator})"
        return f"(({n} - mod({n}, {d})) / {d})"

    def wide_int(self, expr: str) -> str:
        """NUMBER already holds 38 digits: a key offset times the bucket count
        stays exact for every key this dialect bisects natively."""
        return f"({expr})"

    def sum_wide(self, expr: str) -> str:
        """NUMBER sums are exact to 38 digits: about 8.6e19 rows of 2^60."""
        return f"coalesce(sum({expr}), 0)"

    def bisects_natively(self, column: Column) -> bool:
        """Only integer columns that fit 64 bits are bucketed as themselves.

        A NUMBER(38,0) key spans up to 10^38, and the bucket expression's
        `(k - lo) * n` would then pass NUMBER's 38 digits and round silently.
        Wider integer keys are hashed - their exact text, as any text key -
        which costs a hash per row and never rounds.
        """
        p = _precision(column.raw_type)
        return p is not None and p <= 18

    # ----------------------------------------------------------- rendering

    def null_sentinel_sql(self) -> str:
        r"""`\N`, the backslash from its code point."""
        return "(chr(92) || 'N')"

    def _part(
        self, column: Column, expr: str, bound: str | None, lob: bool = False
    ) -> str:
        """Wrap a rendering in the NULL test and record its length bound.

        The sentinel is chosen by the *column* being NULL. The rendered text
        can itself be NULL for a value that is not - an all-space CHAR after
        rtrim, an empty CLOB - because Oracle's empty text is NULL; `||`
        treats it as empty, which is that value's canonical text.
        """
        c = self.quote(column.name)
        sentinel = self.null_sentinel_sql()
        if lob:
            sentinel = f"to_clob({sentinel})"
        sql = f"(case when {c} is null then {sentinel} else {expr} end)"
        self._parts[sql] = (bound, lob)
        return sql

    def holds_sub_micro(self, column: Column) -> bool:
        """TIMESTAMP(7..9), with or without a time zone."""
        return (
            column.logical_type is LogicalType.TIMESTAMP
            and _base(column.raw_type) != "DATE"
            and (_precision(column.raw_type) or 6) > 6
        )

    def single_precision(self, column: Column) -> bool:
        """BINARY_FLOAT is float4."""
        return _base(column.raw_type) == "BINARY_FLOAT"

    def padded_char(self, column: Column) -> bool:
        """CHAR and NCHAR are blank-padded."""
        return _base(column.raw_type) in ("CHAR", "NCHAR")

    def normalize(self, column: Column) -> str:
        """Render one column as canonical text, null-safe."""
        c = self.quote(column.name)
        t = column.logical_type
        base = _base(column.raw_type)
        if t is LogicalType.INTEGER:
            return self._part(
                column, f"to_char({c}, 'TM9', {NLS_NUMBERS})", str(_integer_width(column))
            )
        if t is LogicalType.DECIMAL:
            s = self.float_scale
            if s > 38:
                raise self._err(
                    f"DECIMAL column {column.name!r} cannot be compared at "
                    f"float_scale {s} on Oracle; the maximum is 38"
                )
            return self._part(
                column, self._decimal_text(c, s, column), str(_decimal_width(column) + s)
            )
        if t is LogicalType.FLOAT:
            s = self.float_scale_for(column)
            x = f"cast({c} as binary_double)" if base == "BINARY_FLOAT" else c
            # Sign, at most 35 integer digits (below 2^116), point, places.
            return self._part(column, self._float_text(x, s, column), str(37 + s))
        if t is LogicalType.BOOLEAN:
            return self._part(
                column, f"case when {c} then 'true' when not {c} then 'false' end", "5"
            )
        if t is LogicalType.TIMESTAMP:
            # `-4712-01-01 00:00:00.123456789` is 30 characters.
            return self._part(column, self._timestamp_text(c, column), "30")
        if t is LogicalType.STRING:
            return self._string_part(column, c, base)
        if t is LogicalType.BINARY:
            prefix = self.binary_prefix_sql()
            if base == "BLOB":
                expr = f"to_clob({prefix}) || lower(parity_blob_hex({c}))"
                bound = f"(3 + 2 * nvl(dbms_lob.getlength({c}), 0))"
            else:
                # RAW(2000) is 4,000 hex digits, so with its prefix it is
                # past a VARCHAR2: always a CLOB.
                expr = f"to_clob({prefix}) || lower(rawtohex({c}))"
                bound = f"(3 + 2 * nvl(utl_raw.length({c}), 0))"
            return self._part(column, expr, bound, lob=True)
        if t is LogicalType.INTERVAL:
            return self._part(column, self._interval_text(c, base), "60")
        return self._unknown_part(column, c, base)

    def _string_part(self, column: Column, c: str, base: str) -> str:
        """Text, escaped. National types are converted to the database
        character set first; CHAR loses its padding (as PostgreSQL's bpchar,
        MySQL's CHAR and SQL Server's char do)."""
        if base == "VARCHAR2":
            return self._part(column, self.escape_text(c), f"(2 * nvl(lengthb({c}), 0))")
        if base == "NVARCHAR2":
            return self._part(
                column, self.escape_text(f"to_char({c})"), f"(3 * nvl(lengthb({c}), 0))"
            )
        if base == "CHAR":
            return self._part(
                column, self.escape_text(f"rtrim({c})"), f"(2 * nvl(lengthb({c}), 0))"
            )
        if base == "NCHAR":
            return self._part(
                column,
                self.escape_text(f"rtrim(to_char({c}))"),
                f"(3 * nvl(lengthb({c}), 0))",
            )
        # CLOB and NCLOB: REPLACE works on a CLOB and returns one. Each UTF-16
        # unit is at most three UTF-8 bytes, and escaping at most doubles a
        # one-byte character.
        v = f"to_clob({c})" if base == "NCLOB" else c
        return self._part(
            column,
            self._escape_lob(v),
            f"(3 * nvl(dbms_lob.getlength({c}), 0))",
            lob=True,
        )

    def _unknown_part(self, column: Column, c: str, base: str) -> str:
        """A type with no rule: its text, escaped, where Oracle has one."""
        if base in ("ROWID", "UROWID"):
            return self._part(column, self.escape_text(f"rowidtochar({c})"), "8000")
        if base == "JSON":
            return self._part(
                column,
                self._escape_lob(f"json_serialize({c} returning clob)"),
                None,
                lob=True,
            )
        raise self._err(
            f"column {column.name!r} has type {column.raw_type}, which has no "
            "text form this dialect can compare (LONG, XMLTYPE, VECTOR, object "
            "types and BFILE are not supported yet); leave it out with --exclude"
        )

    def _decimal_text(self, c: str, s: int, column: Column) -> str:
        """`round(c, s)` as plain text with exactly `s` places (`parity_dec`).

        ROUND on NUMBER is half away from zero. TM9 prints the minimal plain
        form (`.5`, `-.125`, `42`), so the leading zero is restored and the
        fraction padded. Past 10^38 TM9 could switch to exponent notation,
        and no other engine holds such a value exactly, so it is refused.
        """
        return f"parity_dec({c}, {s}, {self._where(column)})"

    def _where(self, column: Column) -> str:
        """`float_range_where` as an Oracle string literal."""
        return sql_literal(float_range_where(column))

    def decimal_key_text(self, c: str, column: Column) -> str:
        """A DECIMAL key's exact minimal text: TM9 is exact and minimal, once
        its leading zero is restored. Exponent notation is refused."""
        tm = f"to_char({c}, 'TM9', {NLS_NUMBERS})"
        where = float_range_where(column).replace("'", "''")
        return (
            f"case when instr({tm}, 'E') > 0 then parity_refuse('parity: DECIMAL key "
            f"' || {tm} || ' has no plain exact text{where}') "
            f"when substr({tm}, 1, 1) = '.' then '0' || {tm} "
            f"when substr({tm}, 1, 2) = '-.' then '-0' || substr({tm}, 2) else {tm} end"
        )

    def normalize_key(self, column: Column) -> str:
        """Exact key text; a TIMESTAMP(7-9) key keeps its sub-microsecond
        digits (nine of them, as DuckDB TIMESTAMP_NS prints) when it has any."""
        c = self.quote(column.name)
        base = _base(column.raw_type)
        if column.logical_type is LogicalType.FLOAT:
            raise self._err(float_key_refusal(column))
        if base in ("CLOB", "NCLOB", "BLOB"):
            # A key is hashed and matched as VARCHAR2 text; a LOB key would
            # need the CLOB path in every key expression. Refused, loudly.
            raise self._err(
                f"{base} column {column.name!r} cannot be used as a key on Oracle; "
                "key by another column, or by a VARCHAR2 cast of this one in a view"
            )
        if column.logical_type is LogicalType.DECIMAL:
            return self._part(column, self.decimal_key_text(c, column), "140")
        if column.logical_type is LogicalType.BINARY:
            # As VARCHAR2, not the CLOB a RAW value renders as: a key of more
            # than 1,998 bytes then raises ORA-01489 rather than truncating.
            hexed = f"{self.binary_prefix_sql()} || lower(rawtohex({c}))"
            return self._part(column, hexed, f"(3 + 2 * nvl(utl_raw.length({c}), 0))")
        if column.logical_type is LogicalType.TIMESTAMP and self.holds_sub_micro(column):
            nine = dataclasses.replace(column, sub_micro=True)
            return self._part(column, self._timestamp_text(c, nine), "30")
        return self.normalize(column)

    def _timestamp_text(self, c: str, column: Column) -> str:
        """`YYYY-MM-DD HH24:MI:SS.ffffff`; instants in UTC; BC years astronomical.

        FF6 truncates the digits past the microsecond (verified:
        23:59:59.9999999 prints .999999). DATE has no fractional seconds.
        Oracle has no year 0: year -1 is 1 BC, astronomical 0000, and -44 is
        astronomical -43, which is how DuckDB and PostgreSQL's rule print it.
        """
        base = _base(column.raw_type)
        v = c
        if base == "TIMESTAMP WITH TIME ZONE":
            v = f"sys_extract_utc({c})"
        elif base == "TIMESTAMP WITH LOCAL TIME ZONE":
            v = f"sys_extract_utc(cast({c} as timestamp with time zone))"
        if base == "DATE":
            clock = f"to_char({v}, 'MM-DD HH24:MI:SS') || '.000000'"
        elif column.sub_micro:
            clock = (
                f"case when substr(to_char({v}, 'FF9'), 7, 3) = '000' "
                f"then to_char({v}, 'MM-DD HH24:MI:SS.FF6') "
                f"else to_char({v}, 'MM-DD HH24:MI:SS.FF9') end"
            )
        else:
            clock = f"to_char({v}, 'MM-DD HH24:MI:SS.FF6')"
        y = f"extract(year from {v})"
        where = float_range_where(column).replace("'", "''")
        # Past 9999 `FM0000` prints `#####` - the same text for every such
        # value. Oracle holds none, but a TIMESTAMP WITH LOCAL TIME ZONE at
        # 9999-12-31 23:59:59.999999 UTC reads as year 10000 internally
        # (verified: TO_CHAR itself raises ORA-01877 on it), so the guard is
        # reachable and refuses instead.
        year = (
            f"case when {y} > 9999 then parity_refuse('parity: TIMESTAMP value "
            f"past year 9999 cannot be rendered{where}') "
            f"when {y} > 0 then to_char({y}, 'FM0000') when {y} = -1 then '0000' "
            f"else '-' || to_char(-{y} - 1, 'FM00') end"
        )
        return f"{year} || '-' || {clock}"

    def _interval_text(self, c: str, base: str) -> str:
        """`<months> mon <days> day <microseconds> us` - `canonical_interval_text`.

        YEAR TO MONTH is months only; DAY TO SECOND is days and the clock in
        microseconds. Every component of an Oracle interval carries the
        interval's sign, as PostgreSQL's do for a value typed in one sign,
        and EXTRACT returns them signed. Digits past the microsecond are
        truncated toward zero (`caveats` says so for DAY TO SECOND(7-9)).
        """
        if base.startswith("INTERVAL YEAR"):
            months = f"(extract(year from {c}) * 12 + extract(month from {c}))"
            return f"to_char({months}, 'TM9') || ' mon 0 day 0 us'"
        # The total in microseconds, then split into whole days and the rest,
        # both with the total's sign. An interval Oracle builds itself has one
        # sign throughout, but a driver can store days and seconds of opposite
        # signs (python-oracledb binds timedelta(microseconds=-1) as -1 day
        # +23:59:59.999999, verified); that is the same duration, and splitting
        # the total renders it as such. 86,400,000,000 has a factor 27, so a
        # quotient that is not whole is never within 10^-38 of a whole number:
        # TRUNC of the NUMBER quotient is exact.
        total = (
            f"((extract(day from {c}) * 86400 + extract(hour from {c}) * 3600 "
            f"+ extract(minute from {c}) * 60) * 1000000 "
            f"+ trunc(extract(second from {c}) * 1000000))"
        )
        days = f"trunc({total} / 86400000000)"
        return (
            f"'0 mon ' || to_char({days}, 'TM9') || ' day ' "
            f"|| to_char({total} - {days} * 86400000000, 'TM9') || ' us'"
        )

    def _float_text(self, x: str, s: int, column: Column) -> str:
        """The canonical text of a BINARY_DOUBLE (`canonical_float_text`).

        Oracle prints no shortest repr, and `cast(x as number)` keeps only 17
        significant digits. The exact value is rebuilt instead (`parity_float`):
        with e the binary exponent (from LN, then corrected by exact
        power-of-two comparisons), `m = |x| * 2^(52 - e)` is an integer below
        2^53 - exact in BINARY_DOUBLE, and exact as NUMBER - and
        `m / 2^(52 - e)` in NUMBER is the exact value to 38-40 significant
        digits (2^s is exact in NUMBER up to s = 130, verified). On that, the
        shortest repr is the first of the 15-, 16- and 17-digit roundings that
        converts back to the same double (NUMBER -> BINARY_DOUBLE is correctly
        rounded, verified on 3,000 random decimals), ties to even built by hand
        at 16 and 17 digits as on SQL Server, since ROUND is half away from
        zero. Then ROUND to the scale.

        The exponent must stay NUMBER: a BINARY_DOUBLE operand anywhere makes
        the whole expression BINARY_DOUBLE, and then every step runs in double
        precision (the first draft did, and disagreed on 593 sample values).
        From 2^53 every double is an integer, `m * 2^(e - 52)` exactly, up to
        2^116; past that it is refused with the value and the column.
        Infinity and NaN, which BINARY_DOUBLE holds, render as the tokens.
        """
        return f"parity_float({x}, {s}, {self._where(column)})"

    # ------------------------------------------------- downloads and samples

    def fetched(self, value: Any) -> Any:
        """Rendered text that is empty arrives as None: Oracle's empty text is
        NULL, and every rendering is non-NULL otherwise (the sentinel)."""
        return "" if value is None else value

    def sample_select(self, column: Column) -> str:
        """Raw values for the sample check, past the driver's limits.

        Dates and timestamps travel as text with all nine digits (and their
        offset): Python's datetime holds no BC date and no nanosecond. A
        BC value carries a literal ` BC`, which the reference reads as such.
        """
        c = self.quote(column.name)
        base = _base(column.raw_type)
        if column.logical_type is LogicalType.TIMESTAMP:
            if base == "DATE":
                fmt = "YYYY-MM-DD HH24:MI:SS"
            elif base == "TIMESTAMP":
                fmt = "YYYY-MM-DD HH24:MI:SS.FF9"
            else:
                fmt = "YYYY-MM-DD HH24:MI:SS.FF9TZH:TZM"
                if base == "TIMESTAMP WITH LOCAL TIME ZONE":
                    c = f"cast({c} as timestamp with time zone)"
            # `YYYY` prints a BC year unsigned (44 BC as 0044); the literal
            # era after it is what the reference reads, never NLS-translated.
            return (
                f"case when extract(year from {c}) < 0 "
                f"then to_char({c}, '{fmt}') || ' BC' "
                f"else to_char({c}, '{fmt}') end"
            )
        return super().sample_select(column)

    def sample_value(self, column: Column, value: Any) -> Any:
        """Intervals as their three stored components: the driver hands back
        a timedelta (DAY TO SECOND, whose components share one sign) or an
        IntervalYM (YEAR TO MONTH)."""
        if value is None:
            return None
        if column.logical_type is LogicalType.INTERVAL:
            import datetime as dt

            if isinstance(value, dt.timedelta):
                total = (
                    value.days * 86_400 + value.seconds
                ) * 1_000_000 + value.microseconds
                days, micros = divmod(abs(total), 86_400_000_000)
                sign = -1 if total < 0 else 1
                return (0, sign * days, sign * micros)
            years = getattr(value, "years", None)
            months = getattr(value, "months", None)
            if years is not None and months is not None:
                return (int(years) * 12 + int(months), 0, 0)
            return value
        return value


def _integer_width(column: Column) -> int:
    """Most characters an integer NUMBER prints: its digits and a sign.

    `NUMBER(p,0)` holds p digits; `NUMBER(p,-k)` p digits and k zeros;
    `NUMBER(*,0)` (and INTEGER) 38. These bounds only choose the hash path
    (`hash_parts`); one too small would raise ORA-01489, never truncate.
    """
    m = re.match(r"NUMBER\((\d+|\*),\s*(-?\d+)\)", column.raw_type.upper())
    if not m or m.group(1) == "*":
        return 40
    return int(m.group(1)) + max(0, -int(m.group(2))) + 1


def _decimal_width(column: Column) -> int:
    """Most characters a decimal NUMBER prints before its fractional places:
    sign, integer digits (p - s of them, at least one; 38 when undeclared,
    as larger values are refused), one more for a rounding carry (9.99 at
    scale 0 is 10) and the point."""
    m = re.match(r"NUMBER\((\d+),\s*(\d+)\)", column.raw_type.upper())
    if not m:
        return 41
    return max(int(m.group(1)) - int(m.group(2)), 1) + 3


def _frac_digits(raw: str) -> int:
    """Fractional-second digits of an INTERVAL DAY TO SECOND(p) (6 default)."""
    m = re.search(r"SECOND\((\d+)\)", raw.upper())
    return int(m.group(1)) if m else 6


def _output_type_handler(cursor: Any, metadata: Any) -> Any:
    """Fetch NUMBER as Decimal (the default turns a decimal into a float),
    and CLOB/NCLOB/BLOB as str/bytes rather than LOB locators."""
    import decimal

    import oracledb

    if metadata.type_code is oracledb.DB_TYPE_NUMBER:
        return cursor.var(decimal.Decimal, arraysize=cursor.arraysize)
    if metadata.type_code in (oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_NCLOB):
        return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
    if metadata.type_code is oracledb.DB_TYPE_BLOB:
        return cursor.var(oracledb.DB_TYPE_LONG_RAW, arraysize=cursor.arraysize)
    return None
