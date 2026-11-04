# Limitations — read these before trusting a result

A parity tool that reports a false match is worse than useless, so every
limitation is stated here plainly rather than buried. The short list in the
README names the ones most likely to change a verdict; this page is the whole
list, grouped by topic. The error odds of each step are in
[`risk.md`](risk.md), and what each engine needs in [`engines.md`](engines.md).

- [Numbers: floats and decimals](#numbers-floats-and-decimals)
- [Keys](#keys)
- [Text, NULL and wide rows](#text-null-and-wide-rows)
- [Dates, times and time zones](#dates-times-and-time-zones)
- [Supported types](#supported-types)
- [Schema differences](#schema-differences)
- [Consistency: live tables and snapshots](#consistency-live-tables-and-snapshots)
- [What the reading login cannot see](#what-the-reading-login-cannot-see)
- [Session settings](#session-settings)
- [Result size: `--max-diffs`](#result-size---max-diffs)
- [Tampering](#tampering)
- [Engine-specific](#engine-specific)

## Numbers: floats and decimals

- **Floats and decimals are compared at 6 decimal places** by default. Two
  values differing only in the 7th place are reported as *equal*. This is a
  deliberate cross-engine rounding contract — the engines do not otherwise
  agree on float text. Change it with `--float-scale`; it always applies to
  both sides, and the scale in force is printed on every run.
- **How a double is rounded, exactly.** Every engine renders a finite double
  the same way: below 2^53 (about 9.0e15) its *shortest round-trip decimal* —
  what Python's `repr` prints, and what the user most likely typed — rounded
  half away from zero; from 2^53, where every double is an integer, its exact
  integer value. So `2.0000005` compares as `2.000001` and `1e15+0.5` keeps
  its `.5`; single-precision (`real`) columns are widened to double first.
  PostgreSQL, DuckDB, MySQL and BigQuery agree byte for byte on a 1,565-value
  adversarial sample (`demo/float_agreement.py`) and on 20,000 more random
  doubles (10,000 on BigQuery), at several scales. PostgreSQL needs version
  12 or newer for FLOAT columns (older servers are refused with a clear
  error), and FLOAT columns accept `--float-scale` up to 20.
- **Doubles of magnitude 2^116 (about 8.3e34) or more are refused**: the diff
  stops with an error naming the value rather than rounding it approximately.
  (This replaces the old DuckDB-only limit of 1e32, which PostgreSQL did not
  share.)
- **DECIMAL values too wide to render at the float scale are refused**, never
  rounded or clamped. The limit is the engine's widest decimal minus the
  scale: on MySQL 65 − s integer digits (59 at the default scale; MySQL also
  caps `--float-scale` at 30 for DECIMAL columns), on DuckDB 38 − s, on
  BigQuery the column type's own range. PostgreSQL has no limit. The error
  names the side, the column and the value.
- **`Infinity` and `NaN`** in float columns are compared as those literal
  tokens on both sides.
- **SQL Server's own FLOAT rendering** (`float`, `real`) matches the canonical
  form on the same 1,565-value sample and 10,000 random doubles, at scales
  0–18; `--float-scale` above 18 is refused for FLOAT columns there. FLOAT
  columns need database compatibility level 130 or more.

## Keys

- **Any key type, but non-integer keys are bucketed by a hash.** A single
  integer column is bisected directly. A `uuid`, a natural string key, or
  several columns together are hashed to 60 bits so the key space can be
  divided — and the run says so. Rows are still matched and reported by their
  real key, so a hash collision can only put two rows in the same bucket; it
  can never merge them.
- **Keys are compared exactly — never at the float scale.** A DECIMAL key
  compares by its exact value (`1.50` and `1.5000` are the same key, and an
  integral decimal `42` matches an integer `42`); a timestamp key keeps its
  sub-microsecond digits where the engine holds them. A **FLOAT key is
  refused**: no rendering of a double is both exact and identical on every
  engine. A list column cannot be a key either. A NULL in any key column is
  refused, as for an integer key.
- **When the key's type differs between the sides in a way that cannot
  match** (integer against float, a timezone-aware against a naive
  timestamp), the run warns that every row will read as missing on one side.
- **Keys must be unique.** A non-unique key is detected up front and rejected.
  Silently collapsing duplicate rows would hide real differences. Uniqueness
  is counted by bytes, never under a collation, so a case-insensitive
  collation cannot make two keys count as one.

## Text, NULL and wide rows

- **Text is compared exactly, including the characters the tool uses
  internally.** Values are joined with ASCII Unit Separator and NULL is
  spelled `\N`, so text is escaped first (`\` → `\\`, chr(31) → `\s`): the
  string `\N` never equals NULL and a separator inside a value cannot shift
  where a column ends. Text without either character renders exactly as
  before.
- **NULL is not the empty string**, and a NULL boolean is not `false`: both
  are reported as `different`.
- **Wide tables are fine.** PostgreSQL caps a function call at 100 arguments,
  so the row concatenation is built as a nested tree; tested to 500 columns.
  Long rows are fine too: SQL Server's `CONCAT_WS` silently cuts its result at
  4,000 characters unless an argument is `nvarchar(max)`, so every value is
  joined as one — tested with a difference planted at character 9,500.
- **A row wider than MySQL's `max_allowed_packet`** (64 MiB by default) is
  hashed column by column instead of as one string, so it can never drop out
  of a checksum; against another engine such a row is downloaded and compared
  value by value. A single text value that passes `max_allowed_packet` once
  escaped is refused with an error naming the side and the size.
- **`char(n)` padding is ignored.** SQL Server `char(n)`/`nchar(n)` are
  compared without their trailing padding, as PostgreSQL's `char(n)` and
  MySQL's `CHAR` are: a `char(10)` holding `abc` matches `abc` on the other
  side. Nothing is lost, since the column cannot tell `abc` from `abc ` itself.
  SQL Server GUIDs compare in lower case.

## Dates, times and time zones

- **Timezone-aware timestamps are compared as instants, in UTC.** Both
  sessions are pinned to UTC on connect, so the same instant matches whatever
  the two servers' default timezones are. Without that pin, two sides in
  different zones render every `timestamptz` differently and the tool reports
  the entire table as changed. A timezone-aware column against a naive one is
  named in a warning: if the migration stored local wall-clock time rather
  than UTC, every row will differ.
- **Sub-microsecond digits are compared only when both sides hold them**
  (DuckDB `TIMESTAMP_NS`, SQL Server `datetime2(7)`, Snowflake
  `TIMESTAMP(7-9)`). Against an engine that holds microseconds they are
  truncated, never rounded, and every run comparing such a column says so in a
  caveat. As a *key* such a column is always compared with every stored
  digit.
- **SQL Server `datetime2(7)` and `datetimeoffset(7)` are compared at
  microseconds** against other engines: the seventh digit (100 ns) is
  truncated, never rounded, so `23:59:59.9999995` stays on its day.
  `datetime` (1/300 s ticks) compares as its displayed milliseconds,
  `.003`/`.007`, which is what tools export; a target that stored the exact
  tick (`.003333`) will differ. `datetimeoffset` is compared as an instant in
  UTC.
- **Infinite dates and timestamps** (PostgreSQL's and DuckDB's `infinity`,
  common as a "valid until forever" end date) compare as `infinity` /
  `-infinity`, never as NULL. BC dates use astronomical year numbering (1 BC
  is year `0000`, 44 BC is `-43`), as DuckDB prints them.

## Supported types

Integer (DuckDB `UHUGEINT` included), decimal, float, boolean, string
(PostgreSQL `name` and enums included), date, timestamp, time, binary (`\x` +
hex), JSON (the engine's normalised document), and:

- **bit strings** (PostgreSQL bit/varbit, DuckDB BIT, MySQL BIT(n)) as their
  digits at full width — never a boolean;
- **intervals** (PostgreSQL, DuckDB) as their stored months, days and
  microseconds: `1 day` and `24 hours` are *different* values here, though
  both engines' `=` calls them equal — they print differently and add
  differently across a DST change;
- **time with time zone** (PostgreSQL timetz, DuckDB TIMETZ) as local time and
  offset: `12:00+05:45` and `06:15+00` differ;
- **arrays and lists** (PostgreSQL arrays, DuckDB lists, BigQuery ARRAY) of integers,
  decimals, floats, booleans, text, dates, timestamps, times, binary, bit
  strings, intervals, timetz or inet, element by element by each element's
  rule (`[]`, `['']`, `[NULL]` and `['a,b']` against `['a','b']` all differ).
  A PostgreSQL array's dimensions and lower bounds are part of its value (a
  2x2 array is not its four elements). A list cannot be a key. A BigQuery
  table cannot store a NULL array (it stores an empty one) or a NULL
  element, so a NULL array on another engine reads as different from
  BigQuery's `[]`;
- **inet/cidr** (PostgreSQL) as `address/prefix`: `10.0.0.1/32` is not
  `10.0.0.1/8`;
- **SQL Server sql_variant** as base type and exact value, so `int 1`,
  `bigint 1` and `N'1'` differ (floats as their IEEE bits).

**Anything else** — BigQuery STRUCT/GEOGRAPHY/INTERVAL/RANGE and arrays of
those or of JSON, nested lists, other engines' structs, ranges, geometry,
XML — is compared as text, reported as a warning and listed as not covered
by the sample check: two engines may render it differently for reasons that
have nothing to do with the data, and no risk figure covers it. On BigQuery
that text is injective by construction (a STRUCT is each field by its own
rule, named; GEOGRAPHY its WKB; INTERVAL and RANGE print every component),
so two different stored values never render alike there; it used to be
`FORMAT('%t')`, which printed strings unquoted and NULL fields as `NULL` -
false matches (RT-HB1..HB3). DuckDB `UNION`/`VARIANT` and Snowflake `VARIANT` keep their
type tag, so `1` and `'1'` differ. DuckDB `TIMESTAMP_NS` keeps its nanoseconds
when it has any.

**JSON across two engines** is exact only where the engines' normal forms
coincide (PostgreSQL `jsonb`, MySQL's binary JSON, BigQuery and Snowflake by
code point, DuckDB verbatim text). Where they differ the run names both forms
in a warning; equal documents can then read as different, never the reverse.

**MySQL JSON scalars of MySQL's own types** are the one exception to "never
the reverse", MySQL to MySQL. A JSON value made with `cast(date as json)` (or
TIME, DATETIME, a binary string) is a typed scalar that MySQL's own JSON
comparison tells apart from the string of the same text, but it prints as
that string (`"2024-01-02"`, `"base64:type15:AP8="`), so the two compare
equal here. No other engine has these scalar types, and documents written as
JSON text never contain them (RT-HM8).

**MySQL text in a legacy character set** (sjis, euckr, gbk, big5, cp1251,
ucs2, …) is compared as its Unicode text when every character converts. A
value holding a character with no Unicode mapping - a Shift-JIS or EUC-KR
user-defined character, a byte unassigned in its code page, a surrogate in a
ucs2 column - renders as `parity: not convertible from <charset> 0x<bytes>`:
two such values are equal only when their bytes are, and such a value never
equals anything on another engine (a migration that mapped it to a
private-use character reads as different). MySQL itself would turn it into
`?` or NULL (RT-HM1, RT-HM2).

The per-type, per-engine evidence is [`coverage.md`](coverage.md).

## Schema differences

- **A column present on only one side is a schema difference**: the run is not
  identical and exits `1`, even if every row matches on the columns both sides
  share. `--exclude` it if the change was intended; it is then listed as a
  warning instead.
- **A column whose type differs between the sides** is compared as text and
  named in a warning. Columns are matched case-insensitively (`id` on
  PostgreSQL matches `ID` on Snowflake); a table holding two columns that
  differ only in case is refused.

## Consistency: live tables and snapshots

- **A live source table is read at one snapshot on PostgreSQL, MySQL and
  BigQuery.** PostgreSQL and MySQL run the whole diff in one REPEATABLE READ,
  read-only transaction. BigQuery has no cross-query transaction, so every
  query reads a base table `FOR SYSTEM_TIME AS OF` the instant of the first
  one. DuckDB files are opened read-only.
- **A BigQuery view is not pinned** — BigQuery silently ignores time travel on
  views (and on external tables and materialized views); a table clone is
  pinned and a table snapshot cannot change — and neither is a MySQL table on a non-transactional engine (MyISAM,
  MEMORY); like every table on Snowflake (READ COMMITTED only), such a source
  changing during the run can produce differences that never existed at one
  point in time, or hide ones that did. **Every run says so** when it
  applies. Diff the underlying table, or re-run, if the source is busy.
- **SQL Server reads one snapshot only if the database allows it.** With
  `ALLOW_SNAPSHOT_ISOLATION ON` the diff runs in one SNAPSHOT transaction,
  like PostgreSQL. Without it the run stays at READ COMMITTED and **every run
  prints a warning** that it was not single-snapshot; it never falls back to
  REPEATABLE READ, whose locks would block writers on a live system for the
  whole diff, or to NOLOCK, whose dirty reads invent differences. A table
  created or altered while a snapshot diff runs makes it stop with SQL
  Server's error 3961 rather than read inconsistent data.
- The independent sample check re-reads its rows after the walk, so on an
  engine without one snapshot it fails on any sampled row that changed in
  between — but it cannot bound what changed elsewhere.

## What the reading login cannot see

- **What the reading login cannot see is not compared.** PostgreSQL row-level
  security that filters the reading role is named in the output as a caveat;
  diff as a role with BYPASSRLS for a complete result.
- Settings that cap result rows (MySQL `sql_select_limit`, Snowflake
  `ROWS_PER_RESULTSET`) are reset for the session, and if a side's checksums
  ever cover fewer rows than it holds, the run is not identical.

## Session settings

**Session settings cannot skew the result.** Every setting that changes how a
value renders, or which rows a query returns, is pinned at connect:

| Engine | Pinned at connect |
|---|---|
| PostgreSQL | `TimeZone` UTC, `extra_float_digits = 1`, `standard_conforming_strings`, `IntervalStyle`, `DateStyle`, `bytea_output`, `lc_monetary`, `client_encoding` UTF8, `search_path = pg_catalog` (so no user function can shadow a built-in), `quote_all_identifiers` off, and `idle_in_transaction_session_timeout` / (17+) `transaction_timeout` off so a role default cannot kill the held snapshot mid-run; then a read-only REPEATABLE READ transaction |
| MySQL | `time_zone` UTC, `sql_mode`, `sql_select_limit` (reset to no limit), then REPEATABLE READ, READ ONLY |
| SQL Server | isolation (SNAPSHOT where allowed, else READ COMMITTED), `NUMERIC_ROUNDABORT` off; never commits |
| Snowflake | `TIMEZONE` UTC, `ROWS_PER_RESULTSET = 0` (no limit) |
| DuckDB | `TimeZone` UTC, extension auto-install and auto-load off; the file is opened read-only |
| BigQuery | read-only OAuth scope; base tables read `FOR SYSTEM_TIME AS OF` the first query |

Red-team round G set every user-settable PostgreSQL setting at each value, and
MySQL `sql_mode`, charset, time zone and `init_connect` variants, SQL Server
collations, ANSI options and `user options`, Snowflake output formats and
BigQuery `und:ci`; the six that changed anything are now pinned or refused.

## Result size: `--max-diffs`

- **At most 10,000 differences are reported by default.** Each one costs about
  715 bytes, so two tables that share nothing would need gigabytes rather than
  producing an answer — and pointing the tool at the wrong table or the wrong
  environment is exactly what it exists to catch. Past the limit the run is
  flagged `truncated`, `identical` is never true, and the output says "at
  least N". It does not mean the rest matched. `--max-diffs 0` lifts the
  limit.
- The human report lists at most 100 differences; that is a display limit
  only, and it says how many more were found. `--json` carries all of them.
- When the walk stops early, the independent sample check does not run.

## Tampering

- **The checksum detects accidents, not tampering.** Row hashes are summed per
  key range, and the hash is public, so someone who can write rows on one side
  could craft several changes whose hashes cancel and read as identical. By
  accident this happens with odds of about 1 in 2^60 per differing range.
  Treat a clean run as proof that a migration lost or changed nothing, not as
  proof against a deliberate adversary.

## Engine-specific

- **Snowflake needs connector 4.x** (`snowflake-connector-python>=4.0,<5`,
  which the `[snowflake]` extra installs). Connector 5.x flushes a telemetry
  batch to Snowflake at every logout that no setting stops, and the tool sends
  no telemetry. With 4.x it switches the connector's telemetry and its
  cloud-metadata probes (`SNOWFLAKE_DISABLE_PLATFORM_DETECTION`) off, and a
  live test checks that a session contacts only the account's own login,
  query and logout endpoints. The login itself names the driver, Python and OS
  versions, as every Snowflake client's does.
- **Interrupting a BigQuery run waits for the in-flight query.** Cancelling a
  BigQuery job needs a write scope, and read-only was judged worth more. The
  same is true of SQL Server, whose driver has no way to cancel a query, and
  of Snowflake, whose dialect does not cancel either.
- **Microsoft Fabric and Azure Synapse are not supported yet** and are refused
  at connect, naming the engine edition: both isolate differently (Fabric
  ignores `SET TRANSACTION ISOLATION LEVEL`; Synapse dedicated pools read
  uncommitted by default), and neither has been verified.
- **SQL Server authenticates with SQL logins only** so far (no Windows or
  Entra ID authentication), and **Snowflake with a password or a key pair
  only** (no SSO, OAuth or MFA prompt) — see [`engines.md`](engines.md).
