# Changelog

Notable changes, newest first. This project follows [semantic versioning](https://semver.org/),
with the caveat that 0.x means the CLI surface may still move.

## Unreleased

- **Behaviour change: a sample check that cannot run is no longer
  "identical".** If the independent sample check was asked for (the default)
  and its fetch failed, the result used to stay identical with exit 0 and
  only a `!` line. It is now not identical, exit 1, headed `✗ not proven: the
  independent sample check could not run: <error>`, with `identical: false`
  in JSON. `--verify-sample 0` still rests the verdict on the checksums alone
  - deliberately - and the report now says the check was off (`! independent
  sample check off (--verify-sample 0) ...`); JSON gains `verify_sample`.
  Sample-check notes stay non-blocking.
- **Snowflake key-pair authentication**: `snowflake://user@account/db/schema
  ?private_key_file=<path>`, the key's passphrase read from
  `PARITY_SNOWFLAKE_KEY_PASSPHRASE` (or `_A` / `_B` per side), never from the
  URL. parity loads the PEM RSA key and passes it to the connector as DER
  bytes with `authenticator=SNOWFLAKE_JWT` and no password; the telemetry
  switches ride on this login as on a password one. A missing file, a wrong
  or missing passphrase, a passphrase set for an unencrypted key, a non-RSA
  key, or a URL with both a password and a key are refused naming the side
  and the key path - never the key or passphrase. Verified offline against
  connector 4.x (the connector's own key-pair authenticator signs a JWT the
  public key verifies); the live test `tests/test_snowflake_keypair.py` runs
  when `PARITY_TEST_SNOWFLAKE_KEYPAIR` is set.
- **Snowflake refuses unknown URL parameters**, as MySQL, SQL Server and
  BigQuery do: anything but `warehouse`, `role` and `private_key_file` - and
  a repeated or empty one - used to be dropped silently. The error names the
  side and the parameter and lists the accepted ones. A URL whose password
  holds an unencoded `/ ? #` is refused too, before any of it reaches the
  server, and connection errors are scrubbed of the password and passphrase.
  Password redaction is now tested for Snowflake as for PostgreSQL, MySQL and
  SQL Server.
- **The column type-mismatch warning no longer predicts that every row
  differs.** A VARCHAR `'1'` against a BIGINT `1` compares equal (both render
  `1`), so "will very likely all differ" was wrong; the warning now says rows
  match where both types render the same text and differ where they do not,
  and that a match does not make the type change harmless.
- CONTRIBUTING no longer says a dialect is about eighty lines (the smallest
  is about 570).
- **`parity run CONFIG`: a whole migration in one command** (`parity.batch`).
  A TOML (Python 3.11+, or `tomli` via the new `[toml]` extra on 3.10) or
  JSON file names connections once - `${NAME}` reads an environment
  variable, so passwords stay out of the file - plus `[defaults]` and a list
  of `[[pair]]` entries, each with its own `key`, `columns`, `exclude`,
  `map`, `where`/`a_where`/`b_where` and overrides. The whole file is
  validated before any connection (unknown fields refused with a near-miss
  suggestion, duplicates, missing fields, types, unsafe filters, unset
  variables - all at once, each naming file, pair and field). Pairs run one
  after another on fresh connections; a pair's error is recorded and the run
  goes on; `--fail-fast`, `--only NAME[,NAME]`; Ctrl-C still writes the
  summary, marked incomplete. Outputs: a human summary, `--json` (every
  pair's full `parity diff --json` plus totals), `--markdown FILE` (a
  sign-off report with version, date and config SHA-256) and `--junit FILE`.
  Every qualifier of a pair (filtered, truncated, unexplained, schema
  differences, sample-check failure or error, uncovered columns, caveats)
  appears in every format; the run is identical only if every pair is. Exit
  0 all identical, 1 a pair differs, 2 a pair errored, interrupted, or an
  invalid config. `--resume STATE.json` records each pair as it finishes and
  skips, on a re-run, pairs that finished identical with an unchanged config
  entry (settings, tool version, salted PBKDF2 fingerprint of each expanded
  connection), listed as "from an earlier run" with their timestamp. The
  expanded value of a variable never appears in any output, report or the
  state file: tool messages are redacted (data values never are). `parity
  diff` is unchanged.
- **User documentation restructured** for someone who has never seen the
  tool. The README is now install, a five-minute first try on two DuckDB
  files (no server needed), use on real databases, reading the output, CI,
  the Python API, the risk figures and the eight limitations most likely to
  change a verdict. New pages: `docs/output.md` (every line and verdict the
  report can print, exit codes, every JSON field), `docs/troubleshooting.md`
  (the real error messages and their fixes), `docs/engines.md` (per engine:
  connection string, authentication actually supported, minimum read-only
  grants, snapshot behaviour, versions, what a run costs on BigQuery and
  Snowflake, keeping passwords out of logs) and `docs/limitations.md` (the
  full limitations list, moved from the README, nothing removed). Fixed on
  the way: the usage line showed `--verify-sample 1000` (the default is
  10,000); the example output lacked the sample-check and risk lines; the
  Python options omitted `verify_sample`; the session-settings list missed
  round G's pins; a duplicated paragraph; "a dialect is ~80 lines" (the
  smallest is 574). The 10M-row benchmark was re-measured with the sample
  check (exact counts unchanged: 4 queries / 0 rows and 28 queries / 7,628
  rows), and `demo/benchmark.py` now prints the sample check's queries and
  rows.
- **Row filters: `--where SQL`, `--a-where SQL`, `--b-where SQL`**
  (library `diff(..., where_a=..., where_b=...)`). Compares only the rows
  a predicate selects - one partition, last week - so a slice of a huge
  warehouse table can be checked without a full pass. The predicate is
  parenthesised and ANDed, first, into every query that reads the table
  (`Dialect.rows_from`: key stats, every checksum level, every download, the
  sample check), and `tests/test_where_sql.py` records every statement on
  all six dialects to prove it. A filtered verdict says so everywhere:
  `(filtered rows only)` in the headline, a `filtered: A where ...; B where
  ...` line, JSON `where_a`/`where_b` and `risk.filtered`, and a caveat on
  every filtered run. Refused before any query: `;`, comments (`--`, `/*`,
  MySQL `#`), a backslash before a quote, triple- and dollar-quoted strings,
  unbalanced quotes or parentheses, and on SQL Server whatever its SELECT
  guard refuses. Unfiltered runs emit byte-identical SQL.
- **The walk checks that each level adds up to the one above.** A range's
  buckets must sum, in rows and in checksum, to what the level above
  measured for it - exact, since the sums are exact and the buckets tile the
  range. Found while testing `--where`: a difference seen at one level could
  leave a non-deterministic filter (or, on an engine without one snapshot,
  be rewritten) before the next level looked, and the run then read
  **identical** with the evidence discarded. Now that range is unexplained,
  never identical. No extra query; a single-snapshot run never trips it.
- **Column renames: `--map a_col=b_col[,...]`** (repeatable; library
  `diff(..., column_map={...})`), for key columns too. Side B's columns are
  re-keyed under side A's names at matching, so they land in the same
  position of the row text; `--key`, `--columns` and `--exclude` take side
  A's names and differences are reported under them, with side B's shown
  (`customer_name (B: cust_nm)`, JSON `column_map` and per-row `b_columns`).
  Refused: a mapped name absent on its side (near misses listed), one A
  column onto two B columns or two onto one, a mapped B column that a
  same-named column on either side would still match by name, a column both
  mapped and excluded, and `--exclude`/`--columns` naming a column by its
  side B name.
- **Bit strings, intervals, times with a time zone, lists, network addresses
  and SQL Server variants have exact rules** (new logical types BITS,
  INTERVAL, TIMETZ, LIST, INET, VARIANT; `tests/test_types_rules2.py`):
  bit strings as their digits at full width (never a boolean); intervals as
  their three stored components, `<months> mon <days> day <µs> us`, so
  `1 day`, `24 hours`, `1 mon` and `30 days` are four values; TIMETZ as
  `HH:MM:SS.ffffff+HH:MM[:SS]`, local time and offset; one-dimensional
  arrays and lists as `[` + each element by its own type's rule, each
  followed by `\,`, + `]`, so `[]`, `['']`, `[NULL]`, `['a,b']` and
  `['a','b']` all differ (a PostgreSQL array's dimensions and lower bounds
  are part of its value; a list cannot be a key); PostgreSQL
  inet/cidr as `address/prefix`; sql_variant as `<base type>:<exact value>`
  with floats as their IEEE bits. DuckDB UHUGEINT is an INTEGER, hashed as a
  key; PostgreSQL `name` is a STRING. Every one is checked by the sample
  check now. Found on the way and fixed: **DuckDB TIMETZ was a false
  match** (COV-7): its text printed the offset +05:00:30 as `+05:30`; and a
  sql_variant float raised "Arithmetic overflow" for -0.0 and negatives
  below 1e-99 (COV-8). **Checksums move** for interval, timetz, array/list
  and sql_variant float/real columns; unchanged for bit strings (PostgreSQL,
  DuckDB, MySQL), inet/cidr, `name`, UHUGEINT and every other sql_variant.
  Still UNKNOWN: BigQuery ARRAY and INTERVAL, arrays of JSON or of unmapped
  types, nested lists, structs, ranges (Snowflake ARRAY stays JSON). Coverage: see
  `docs/coverage.md`; every native type the sweep loads that had no rule now
  has one.
- **Server settings and versions** (red-team round G, `tests/redteam/test_settings.py`,
  `test_versions.py`, against PostgreSQL 9.5-18, MySQL 8.0/8.4/9.7, SQL Server
  2019/2022 at compatibility levels 100-160, DuckDB 1.0-2.0, live BigQuery and
  Snowflake). Eight false matches, all fixed:
  - MySQL `sql_select_limit` and Snowflake `ROWS_PER_RESULTSET` (a GLOBAL,
    user or `init_connect` value) capped every result, so columns and checksum
    buckets past the cap were never compared: now reset per session, and any
    run whose checksums cover fewer rows than a side holds is not identical.
  - MySQL text that passes `max_allowed_packet` once escaped became the NULL
    sentinel: now refused with an error naming side and size.
  - PostgreSQL databases in LATIN1 or WIN1252 hashed text in the server
    encoding: now hashed as UTF-8 (no checksum moves in UTF-8 databases).
  - SQL Server below compatibility level 130 converts floats inexactly: FLOAT
    columns there are refused, naming the level.
  - PostgreSQL row-level security silently hid rows: now a caveat on every run.
  - DuckDB before 1.3 (unquoted list text, pre-epoch nanoseconds, truncated
    DECIMAL): refused at connect; the `[duckdb]` extra needs `duckdb>=1.3`.
  Also fixed: PostgreSQL `quote_all_identifiers` (pinned off), SQL Server
  NUMERIC_ROUNDABORT from 'user options' or a session default (pinned off;
  closes RT-D6), PostgreSQL 9.5 refused naming the minimum (9.6), MySQL 9.7
  without MD5() refused naming `component_classic_hashing`. Verified to hold:
  PostgreSQL 12-17 byte-identical to 18, MySQL 8.4/9.7 to 8.0, SQL Server 2019
  to 2022 and levels 130-160, DuckDB 1.3-2.0 to 1.5, every user-settable
  PostgreSQL setting, MySQL sql_mode/charset/time_zone variants, SQL Server
  collations, Snowflake output-format parameters, BigQuery `und:ci`.
- **TIME, BINARY and JSON have exact rules** (new logical types): TIME as
  `HH:MM:SS.ffffff` (24:00:00 kept; MySQL's duration TIME only matches MySQL;
  sub-µs digits as for timestamps), BINARY as `\\x` + lower hex of the bytes,
  JSON as the engine's normalised document in one canonical layout and string
  escaping - exact same-engine, exact across engines where the normal forms
  coincide, with a warning naming both forms where they do not. PostgreSQL
  `money` compares as DECIMAL (it rendered locale text), enums as STRING.
  Found on the way: BigQuery `TO_JSON_STRING(NULL)` is the text `null`, so a
  SQL NULL equalled JSON null - a false match, fixed. **Checksums move** for
  TIME, BINARY and JSON columns on most engines (PostgreSQL json, time,
  money; MySQL time(0-5); DuckDB TIME/BLOB/JSON; SQL Server time and binary
  types; BigQuery TIME/BYTES/JSON; Snowflake TIME/BINARY/VARIANT/OBJECT/ARRAY);
  unchanged for PostgreSQL bytea/jsonb, enums, and MySQL binary/json/time(6).
  Coverage: 3,717 of 3,743 ruled cells exact (the rest required refusals);
  UNKNOWN-mapped types down from 34 to 14 (arrays, interval, inet, bit
  strings, timetz, sql_variant, UHUGEINT remain).

- **Every run states its own residual risk** (README "Risk figures"): the
  checksum-collision bound (2^-60 per differing range) and, from the sample
  check, the most rows that could hide a difference at 99.9% confidence - the
  exact binomial bound, 0 when every row was compared. In the report and in
  JSON `risk`.
- **Coverage map** (`docs/coverage.md`, `tests/conformance/`): about 500 hard
  values per type on all six engines, each rendered by the engine and checked
  against the rules in Python and across engines - 3,187 of 3,213 cells at
  scale 6 exact, the rest required refusals. It found and these fixes close:
  - **Snowflake TIME was a false match** (COV-6): its varchar cast dropped
    fractional seconds, so 12:34:56.5 equalled 12:34:56. **Checksums of
    Snowflake TIME columns change.**
  - DuckDB TIMESTAMP_S / TIMESTAMP_MS outside 1677-2262 crashed the diff
    (COV-1).
  - Sub-microsecond digits follow one rule (COV-2): compared when *both*
    sides hold them (DuckDB TIMESTAMP_NS, SQL Server datetime2(7), Snowflake
    TIMESTAMP(7-9)), truncated with a caveat otherwise. DuckDB used to keep
    them against every engine, so the same instant on DuckDB and SQL Server
    read as different. **Checksums move for those columns.**
  - The sample check read DuckDB's infinite, BC and nanosecond values wrongly
    (COV-3..5).

- **The tool now checks itself.** After the checksum walk, about 10,000 rows
  (every row of a smaller table) are fetched again from both sides as the
  drivers return them and compared in Python by the documented rules
  (`parity.reference`), with none of the per-engine SQL the checksums depend
  on. A false match needs that SQL to render two different values alike; the
  second method cannot be fooled the same way, so any row on which the two
  disagree makes the result **not identical** (exit 1) and is named in the
  report. `--verify-sample N` sets the size, `0` skips it; `diff(...,
  verify_sample=)` in the library. New `DiffResult.verification`, JSON
  `verification`, and `stats.sample_queries` / `stats.rows_sampled` (the
  walk's `queries` and `rows_downloaded` are unchanged). Columns of types
  outside the rules (JSON, binary, ...) are listed as not covered. Each
  driver's lossy spots are worked around and verified live: MySQL sends
  FLOAT with six digits, SQL Server drops trailing NULs and rounds
  datetime2(7), psycopg cannot load infinite or BC dates, DuckDB needs pytz
  for TIMESTAMPTZ.

- **Snowflake FLOAT now follows the canonical float form** every other engine
  shares (the shortest round-trip decimal, rounded half away from zero). It
  used to round the double's exact binary value, which disagreed with the
  other engines on 215 of the 1,565 adversarial sample doubles at the default
  scale (8 at scale 2) — false differences, and in principle two different
  doubles rendering alike. Verified live against Snowflake on 6,565 doubles at
  scales 0, 2, 6, 9 and 20; Infinity and NaN render as the shared tokens, and
  values past 2^116 are refused naming the column. The "Snowflake FLOAT not
  verified" warning is gone. **Checksums of Snowflake FLOAT columns change.**
- **Every Snowflake change in 0.4.0 is now verified live** (AWS eu-central-2,
  2026-11-03): UTC rendering of `TIMESTAMP_TZ` and `TIMESTAMP_LTZ`, timezone
  awareness, the nanosecond caveat, nanosecond timestamp keys, exact DECIMAL
  keys, `VARIANT` keeping its type, schema-qualified tables, the exact bucket
  division (plain `/` was confirmed to round 9999999.99… up to 10000000) and
  escaped text, including under a case-insensitive collation.
- **Snowflake: connector 4.x only, and no telemetry or cloud-metadata
  probes.** Connector 5.x flushes a telemetry batch at every logout that no
  switch stops, so the `[snowflake]` extra now requires
  `snowflake-connector-python>=4.0,<5`. Connectors probe the AWS, Azure and
  GCP metadata services at login to report their platform; the tool sets
  `SNOWFLAKE_DISABLE_PLATFORM_DETECTION` (an explicit setting wins). Verified
  live on 4.8.1: a session contacts only the account's login, query and
  logout endpoints, and a test fails if that changes.

## 0.4.0 — 2026-11-03

> **Upgrade from any earlier version, and re-run comparisons that touched any
> of the cases below.** A red-team campaign (about 600 adversarial tests across
> every engine pair, 9,700 differential fuzz cases and mutation testing of the
> suite itself) found inputs on which 0.3.0 and earlier **reported identical
> tables that were not**:
>
> - a text value that is literally `\N` against a NULL, and an ASCII Unit
>   Separator (chr 31) inside a text value or a text key, which could shift a
>   field boundary;
> - a column present on one side only (exit code 0, "no differences");
> - PostgreSQL `infinity` / `-infinity` dates and timestamps (they rendered as
>   NULL) and BC dates (the era was dropped);
> - an unqualified PostgreSQL table, introspected in `public` but read through
>   the session's `search_path` (Snowflake had the same gap);
> - MySQL binary, blob, `bit(n)` and spatial values that are not valid UTF-8
>   (they rendered as NULL), MySQL rows wider than `max_allowed_packet`, and
>   MySQL non-transactional (MyISAM, MEMORY) tables written during the run;
> - DECIMAL and FLOAT keys, which were matched at the 6-place float scale, and
>   timestamp keys or DuckDB `TIMESTAMP_NS` values, cut to microseconds;
> - SQL Server `sql_variant` (lossy, and its type dropped), SQL Server text with
>   unpaired surrogates or trailing NUL characters, DuckDB `UNION` / `VARIANT`
>   and Snowflake `VARIANT` (type dropped);
> - PostgreSQL sessions with `standard_conforming_strings = off`, or a function
>   on `search_path` shadowing a built-in.
>
> Every one is fixed below and pinned by a test that plants exactly that
> difference. Checksums of ordinary data do not change; those of the affected
> values do, which matters only if you stored checksums.

### Fixed

- **Text and key encoding.** Text and unmapped-type values are escaped before
  hashing (`\` → `\\`, chr(31) → `\s`, the backslash always built from its code
  point, under a binary collation on every engine — BigQuery's `REPLACE` under
  a case-insensitive collation was verified to skip chr(31) otherwise). Keys
  render exactly: DECIMAL keys as their minimal exact text, sub-microsecond
  timestamp keys with all their digits; FLOAT keys and NULLs in any key column
  are refused.
- **Verdicts.** A column present on one side only is a schema difference: not
  identical, exit code 1, unless set aside with `--exclude` / `--columns`. A
  checksum mismatch that the downloaded rows cannot explain is reported as
  unverified, never as identical. A key whose type differs between the sides is
  warned about.
- **PostgreSQL:** infinite and BC dates render distinctly; unqualified tables
  always mean `public`; materialized views are supported; session settings
  (`standard_conforming_strings`, `DateStyle`, `IntervalStyle`,
  `bytea_output`, `lc_monetary`, client encoding, `search_path`, idle and
  transaction timeouts) are pinned; keys under a nondeterministic collation are
  counted by bytes; connection errors no longer echo the password.
- **MySQL:** binary columns render as `\x` + hex and `bit(n)` as its bits; wide
  rows fall back to a per-column hash; non-InnoDB tables get a no-snapshot
  warning; the session is READ ONLY with `sql_mode` pinned; TLS options in the
  URL are honoured (and unknown options refused); passwords containing `/` or
  `#` no longer leak through URL-parsing errors; backslashes in table names are
  escaped; `TIMESTAMP` is treated as an instant. Composite and text keys are
  counted distinct by bytes, so unique keys such as `(5, 944)` / `(59, 44)` or
  `'a'` / `'A'` are no longer refused as duplicates.
- **SQL Server:** unpaired surrogates render distinctly instead of as U+FFFD;
  downloads carry the exact UTF-8 bytes (the driver dropped trailing NULs);
  `sql_variant` renders as `<base type>:<exact value>`; the SELECT-only guard
  is a strict single-statement check; `login_timeout` works; tables past ~300
  columns no longer hit the 8,060-byte row limit.
- **DuckDB:** `TIMESTAMP_NS` keeps its nanoseconds; `UNION` / `VARIANT` keep
  their tag; HUGEINT keys spanning more than 2^127 are bucketed exactly;
  extension autoinstall and autoload are off.
- **Snowflake** (unverified live — no account was available): instants are
  rendered in UTC and marked timezone-aware, precisions above microseconds and
  READ COMMITTED are warned about, `VARIANT` keeps its type, tables are always
  schema-qualified, the bucket division is exact, client telemetry is off.
- **BigQuery:** credentials in the URL are refused; views print a
  not-one-snapshot warning.
- **Errors** raised during the walk name the side and the table. A FLOAT value
  past 2^116 is refused naming the side and the column on every engine.
- **Output:** a value the console cannot encode no longer crashes the report
  on Windows; text values and keys are quoted and control characters escaped,
  so a value cannot forge a line of the report, `'NULL'` cannot pass for NULL,
  and a real `\N` string prints as itself.

### Changed

- **Exit code 1** now also means "a column exists on one side only" or
  "checksums disagreed unexplained". JSON gains `schema_differences` and
  `unexplained`; one-sided columns moved there from `warnings`.
- The human report lists warnings before the rows, quotes text values, and
  prints unencodable characters as `\uXXXX`.
- `--max-diffs` below 0 is a usage error (exit 2); `--key ID,id` names one
  column, not two.
- Errors from the walk are a `ValueError` naming the side and the table, with
  the driver's exception as `__cause__`. Dialects are context managers.
- **Library users:** a PostgreSQL dialect's session pins `search_path` to
  `pg_catalog`, so raw SQL passed to `dialect.query()` must schema-qualify its
  tables (`dialect.qualify("orders")` gives `"public"."orders"`). The tool's
  own queries always did.
- Checksums change for: text containing `\` or chr(31); DECIMAL- and
  sub-microsecond-timestamp-keyed tables; MySQL binary-family, `bit` and
  spatial columns; infinite and BC dates on PostgreSQL; DuckDB `TIMESTAMP_NS`
  values with nanoseconds and `UNION` / `VARIANT`; SQL Server `sql_variant`;
  Snowflake non-UTC `TIMESTAMP_TZ` and `VARIANT`.

### Added

- **SQL Server support** — SQL Server 2019+ and Azure SQL Database / Managed
  Instance (`pip install "parity-diff[mssql]"`,
  `mssql://user:password@host:port/database`, also `sqlserver://`), through
  Microsoft's `mssql-python` driver, which needs no system ODBC install.
  Verified end to end against SQL Server 2022 in a UTF-8, a Latin1 (code page
  1252) and a case-sensitive database: the hash constant agrees for `varchar`
  and `nvarchar`, text hashes as the same UTF-8 bytes as every other engine
  (accents, CJK, emoji), every row of a 5,000-row mixed-type table renders
  byte-identically to DuckDB and the full-table checksum matches with zero rows
  downloaded, and the changed-decimal, deleted-row, inserted-row, NULL-vs-`''`
  and NULL-vs-FALSE checks each find exactly the planted row — also through the
  CLI against PostgreSQL. FLOAT matches the canonical form on the adversarial
  sample plus 10,000 random doubles.
- **Three SQL Server false-match traps closed, each pinned by a planted
  difference**: `CONCAT_WS` silently truncates to 4,000 characters unless an
  argument is `nvarchar(max)` (every value is now one; a change at character
  9,500 of the row text is found); text keys equal under the collation
  (`'a'`, `'A'`, `'a '`) are counted distinct by SHA-256, not refused as
  duplicates; and the bucket division divides an exact multiple rather than
  relying on decimal `/`, checked at every boundary of ranges up to 2^64 wide.
- **One snapshot on SQL Server when the database allows it**
  (`ALLOW_SNAPSHOT_ISOLATION ON`); otherwise READ COMMITTED with a warning in
  every run's output. Never REPEATABLE READ or NOLOCK.
- **Dialect caveats reach the output.** A new `Dialect.caveats()` hook lets a
  dialect report limitations of this particular comparison — SQL Server's
  missing snapshot, or a `datetime2(7)` column compared at microseconds — and
  the engine prints each as a warning naming the side (CLAUDE.md §8).

### Changed

- The shared checksum and key-statistics queries spell `GROUP BY`, `COUNT` and
  the distinct-key count through three new dialect hooks (`group_by`, `count`,
  `count_distinct_key`), because T-SQL accepts no `GROUP BY 1` and its `COUNT`
  overflows past 2^31 − 1 rows. Their defaults emit exactly the old SQL: the
  generated queries of PostgreSQL, DuckDB, MySQL, Snowflake and BigQuery are
  byte-identical to 0.3.0's, so no checksum moves.

## 0.3.0 — 2026-11-02

> **Upgrade from any earlier version.** Versions up to 0.2.3 could report two
> tables as identical when they were not, in two cases: floating-point (double)
> columns holding values with about 15 or more significant digits, or near a
> rounding boundary, compared across engines (for example PostgreSQL
> `1e15 + 0.5` against DuckDB `1e15`); and MySQL DECIMAL values of about 1e32 or
> more (more than 38 − scale integer digits), which MySQL silently clamped to
> its maximum. Both are fixed below and pinned by tests that plant exactly these
> differences. Re-run any comparison made with an earlier version that involved
> such columns. Because float rendering changed, checksums of affected values
> differ from 0.2.x; both sides of a diff always use the same version, so this
> only matters if you stored checksums.

### Added

- **BigQuery support** (`pip install "parity-diff[bigquery]"`,
  `bigquery://project/dataset[?location=EU]`, auth by Application Default
  Credentials), verified end to end against a live project in the EU region:
  the hash constant agrees, every row of a 5,000-row mixed-type table renders
  byte-identically to DuckDB and the full-table checksum matches, a 130-column
  table and keys at both ends of INT64 compare clean, and the changed-decimal,
  deleted-row, inserted-row, NULL-vs-`''`, NULL-vs-FALSE and text-key checks
  each find exactly the planted row. Also diffed live against PostgreSQL
  through the CLI. The BigQuery-specific SQL: `ARRAY_TO_STRING` stands in for
  `concat_ws`; the hash is a `0x`-hex INT64 cast of `TO_HEX(MD5(x))`; decimals
  are `ROUND`ed before `FORMAT('%.6f')` (alone it prints `-0.000000`); floats
  follow the canonical float form below (built from `CAST(x AS BIGNUMERIC)`,
  since neither `CAST(x AS STRING)` nor `FORMAT('%.17g')` gives the shortest
  repr); dates use `%E4Y` so year 999 is `0999`; `TIMESTAMP` is rendered in
  UTC and `DATETIME` as naive; sums and the bucket expression run in
  `BIGNUMERIC`.
- **Read-only by construction on BigQuery**: the client requests only the
  `bigquery.readonly` OAuth scope, under which the server refuses INSERT,
  DELETE, CREATE, DROP and scripts (verified).
- **One snapshot on BigQuery**: every query reads a base table `FOR SYSTEM_TIME
  AS OF` the instant of the first metadata query. Views are not pinned —
  BigQuery silently ignores time travel on a view, verified — and this is
  documented as a limitation.

### Fixed

- **A MySQL DECIMAL value of 1e32 or more can no longer compare equal to a
  different one.** MySQL rendered DECIMAL through `CAST(c AS DECIMAL(38,6))`,
  and inside a SELECT MySQL does not raise when a value does not fit — in
  every sql_mode it returns the type's maximum and leaves a warning. So every
  value from 1e32 up rendered `99999999999999999999999999999999.999999`: 1e32
  and 5e40 on MySQL diffed as identical to each other, and to a PostgreSQL or
  DuckDB row really holding that maximum (reproduced end to end against all
  three; at `--float-scale 20` the clamp started at 1e18). The cast is now
  `DECIMAL(65,s)`, MySQL's widest, so values up to 65 − s integer digits
  render exactly; past that a guard raises error 1411 through `UUID_TO_BIN`,
  re-raised as an error naming the side, the column and the value. MySQL's
  maximum DECIMAL scale, 30, is refused up front for DECIMAL columns. Text for
  every value the old cast could hold is byte-identical, so no checksum moves.
  DuckDB, PostgreSQL and BigQuery were checked for the same bug and do not
  clamp: they raise, or render exactly.
- **FLOAT columns now render identically on every verified engine, and a real
  change at the last compared decimal can no longer be hidden.** PostgreSQL
  rendered a double through `float8::numeric` (15 significant digits) and
  DuckDB through `cast(double as decimal)` (the double times 10^scale, in double
  precision), so on a 1,565-value adversarial sample they disagreed on 347
  doubles: rounding-only false alarms such as `1000000000000000.500000` against
  `1000000000000000.524288`. Worse, PostgreSQL's truncation could make two
  *different* values compare equal — 1e15+0.5 on PostgreSQL and 1e15 on DuckDB
  both rendered `1000000000000000.000000`, and 123456789.1234565 against
  123456789.123456 likewise — so a changed value was reported as a match.
  (The report that prompted this measured 49 of 727 sample doubles and
  believed it could never hide a difference; it could.) There is now one
  definition, `canonical_float_text` in `parity.dialects.base`, and every engine
  computes it: below 2^53 the shortest round-trip decimal rounded half away
  from zero to the float scale, from 2^53 the exact integer. PostgreSQL,
  DuckDB, MySQL and BigQuery agree with it on all 1,565 adversarial values at
  scales 0, 2, 6 and 9 and on 20,000 further random doubles (10,000 on
  BigQuery); before, every pair except DuckDB↔BigQuery disagreed on 169-347 of
  them. `demo/float_agreement.py` reproduces the before/after counts.
- Single-precision (`real` / `FLOAT`) columns are widened to double before
  rendering. Rendered directly, PostgreSQL, DuckDB and MySQL disagreed on
  ~630 of 1,500 random float4 values; widened, on none.

### Changed (behaviour)

- **FLOAT canonical text, and therefore FLOAT row checksums, change** for the
  values the engines used to disagree on: doubles with more than ~15
  significant digits, near a rounding boundary, or at or above 2^53. Both sides
  of a diff always run the same version, so this changes verdicts only where
  the old ones were wrong; on the sample, none of the 1,217 values on which all
  four engines already agreed moved. MySQL's text is unchanged below 2^53 (its
  DECIMAL cast already computed the canonical form).
- **Doubles of magnitude 2^116 (~8.3e34) or more are refused** on PostgreSQL,
  DuckDB, MySQL and BigQuery, with an error naming the value. Previously
  DuckDB refused from 1e32 and PostgreSQL rendered any magnitude through 15
  digits; DuckDB now accepts up to 2^116, PostgreSQL no longer renders huge
  doubles approximately.
- PostgreSQL pins `extra_float_digits = 1` at connect, as it pins the timezone,
  and refuses FLOAT columns on servers older than 12 (which cannot print the
  shortest round-trip decimal) instead of comparing them approximately.
- **FLOAT columns refuse `--float-scale` above 20** (`FLOAT_MAX_SCALE`). Past
  it MySQL's DECIMAL cast clamps silently (2e15 at 23 places became
  `999999999999999.999...`) and BigQuery's BIGNUMERIC runs out of places;
  scale 20 itself was verified on all four engines. DECIMAL columns are not
  affected.
- A new `Dialect.float_caveat()` hook (default `None`) lets a dialect declare
  its FLOAT rendering unverified; the engine then warns on every FLOAT column
  compared on that side. Snowflake uses it: its FLOAT path was defined before
  the canonical form and has not been re-verified live.

### Changed

- `Column` gained `tz_aware`, so a dialect can state whether a timestamp column
  is an instant instead of the engine inferring it from `with time zone` in the
  type name. BigQuery needs it: its instant type is plainly `TIMESTAMP`, which
  is the *naive* type's name elsewhere, so PostgreSQL `timestamptz` ↔ BigQuery
  `TIMESTAMP` would have warned falsely and `timestamp` ↔ `TIMESTAMP` not at
  all. Unset everywhere else, so no existing behaviour moves.
- Three small `Dialect` hooks, each defaulting to the old behaviour so no
  other engine's SQL changes: `int_literal` (BigQuery cannot parse an integer
  literal past INT64, and the key span of a full-range key is 2^64),
  `string_literal` (BigQuery escapes with backslashes, not doubled quotes), and
  `_columns_catalog` (BigQuery's `INFORMATION_SCHEMA` is per dataset, and the
  "did you mean" hint has to look there).

## 0.2.3 — 2026-09-18

### Internal

A dedicated campaign to harden the **identical check** — a false match being the
one unforgivable failure for a diff tool — added ~20 tests across every angle it
could be attacked from. No abnormality was found.

- **Offline logic** (`tests/test_identical.py`): at high volume against the
  in-memory dialect, identical tables stay identical under every
  fan-out/threshold and with hostile data (the field separator, NULs, full
  Unicode), independent of column order, deterministic across runs, and
  zero-download at 200k rows; and a single planted change (cell, delete, insert)
  is never smoothed to a match, always matching a brute-force oracle.
- **Live cross-engine** (`tests/test_identical_live.py`, `test_mysql_postgres.py`):
  identical reads identical with zero download for the *hashed* key paths (text,
  composite, and a real `uuid`-typed key); for an edge-value table (bigint
  min/max, `Infinity`/`-Infinity`/`NaN`, a 12-digit decimal, astral-plane emoji,
  empty string, all-NULL row); for a *representation change* (the same values
  typed as integer↔bigint, numeric↔double, varchar↔text); for a 120-column table
  that forces PostgreSQL's nested-`concat_ws` tree; and directly between MySQL
  and PostgreSQL, the pair previously covered only through DuckDB. The exact
  0.2.1 same-content insert+delete bug is reproduced across two real engines and
  confirmed caught — the strongest guard on that fix.
- **A repeatable deep hunt.** `PARITY_DEEP=20 pytest tests/test_identical.py
  tests/test_properties.py` scales the oracle and the false-identical hunter far
  past their fast defaults; ~33,000 generated cases across a dozen seeds turned
  up nothing, and anyone can re-run it. Documented in `CONTRIBUTING.md`.
- `CONTRIBUTING.md` records the Snowflake findings (no bit-cast/`CONV` for the
  hash, `NUMBER` split by scale, engine-specific identifier case-folding) as
  traps for the next warehouse dialect.

## 0.2.2 — 2026-09-07

### Added

- **Snowflake support** (`pip install "parity-diff[snowflake]"`), verified end
  to end against a live account (AWS eu-central-2): the hash constant agrees,
  a full-table checksum matches DuckDB byte-for-byte over 5,000 mixed-type
  rows, and the identical / changed / deleted / NULL-vs-empty checks all pass.
  Snowflake has no bit-cast or `CONV`, so the 60-bit row hash goes through
  `floor(md5_number_upper64(x)/16)`; integers and decimals both report as
  `NUMBER` and are split by `numeric_scale`; the NULL sentinel is built from
  `chr(92)`. Snowflake offers only READ COMMITTED, so unlike PostgreSQL the
  walk is not pinned to one snapshot — a documented limitation, not a hidden one.

### Changed

- **Identifiers are now matched case-insensitively across engines.** `--key` is
  one name applied to both sides and the shared-column set is an intersection of
  two schemas, but Snowflake upper-cases unquoted identifiers while
  PostgreSQL/DuckDB lower-case them — so a table stored as `id`, `amount` and its
  migration stored as `ID`, `AMOUNT` previously shared no key and no columns, and
  the diff was impossible. Matching now folds case (each side still quotes its
  own real name in SQL), so a table diffs cleanly against its differently-cased
  migration. Same-case schemas are unaffected and no checksum moves; a table
  with two columns differing only in case is refused rather than folded
  ambiguously. This surfaced only under a live Snowflake run, which is the point
  of the §8 rule.

### Internal

- CI now creates the GitHub release page automatically after a successful PyPI
  publish, from the tag's CHANGELOG section.

## 0.2.1 — 2026-09-06

A round of generative testing — property-based, oracle (differential against a
brute-force reference), metamorphic, and cross-engine fuzzing — added to make
sure the tool actually does what it claims. It immediately earned its place by
turning up a real correctness bug, now fixed.

### Fixed

- **A same-content insert and delete in one bucket could read as identical —
  the cardinal sin for a diff tool.** Each bucket was summarised by its row
  count plus a checksum over the *comparable columns only*. When one row was
  added and another removed inside the same bucket and the two shared identical
  column values — utterly ordinary data: a repeated status, a boolean, an empty
  string — the counts balanced and the content sums matched, so the bucket read
  clean and both differing rows were missed. The per-bucket checksum now folds
  the **key** into every row's hash, so an inserted key and a deleted key hash
  differently and the bucket can no longer cancel. This only ever adds
  sensitivity: a checksum that differs is always re-checked by downloading and
  comparing the real rows, so the change can cost an extra query but never a
  wrong verdict. The oracle test — which compares the segmented walk against a
  brute-force row-by-row diff over thousands of generated tables — found it and
  now guards against its return.
- **Single-row (and other degenerate one-key) tables downloaded their rows
  instead of checksumming first.** An identical one-row table reported itself
  identical but still moved its rows across the network, breaking the "identical
  tables download zero rows" promise for that case. Such a table is now
  checksum-qualified like any other, so an identical one downloads nothing; the
  four-query cost on identical tables is unchanged.

### Added

- **Generative test suite** (`tests/test_properties.py`): an oracle test
  (~400 random table pairs per run checked against a brute-force diff), tuning-
  knob invariance, side-swap symmetry, an exact-change-count property, and
  bucket-tiling properties — ~2,600 generated cases per run across ten
  properties, all against the in-memory dialect so they run with no database.
- **Cross-engine encoding fuzzer** (`tests/test_fuzz_encoding.py`): ~250
  generated values plus hand-picked hostile ones (the field-separator byte, the
  `\N` sentinel spelled as real data, emoji, combining marks, RTL controls,
  bigint extremes, microsecond timestamps) inserted identically into PostgreSQL
  and DuckDB, asserting byte-identical canonical text and matching 60-bit row
  hashes, plus the whole `diff()` walk over that fuzzed data end to end.
- A `test` optional-dependency (`pip install "parity-diff[test]"`) carrying
  Hypothesis, so the generative suite runs in CI on every push. The runtime
  core stays standard-library only — this extra is never pulled in by an engine
  extra.

## 0.2.0 — 2026-09-06

Two additions that widen who the tool can serve, both proving the architecture
holds: adding an engine and a key type each touched one place.

### Added

- **MySQL support** (`pip install "parity-diff[mysql]"`, tested against 8.0).
  The first engine added after release, and the test of the "a dialect is ~80
  lines" claim. It forced three MySQL-specific decisions the encoding tests
  pin: the row hash goes through `CONV(hex,16,10)` rather than a bit-cast; the
  field separator is `char(31 using utf8mb4)` because MySQL has no `chr` and a
  bare `char` is binary; and the NULL sentinel is built from a hex literal
  because MySQL processes backslash escapes in string literals. Verified
  end to end against DuckDB, including the NULL-versus-empty-string trap.
- **Non-integer and composite keys.** A single integer column is still
  bisected directly and emits identical SQL to before. A uuid, a text key, or
  several columns together are hashed to 60 bits for bucketing only — row
  identity stays the real key, so a hash collision can never merge two rows.
  `--key` takes a comma-separated list.

### Verified engine agreement

The hash constant `648541476951500027` for `'abc'` now agrees across three
independent SQL paths — PostgreSQL's bit-cast, DuckDB's hex-cast, and MySQL's
`CONV`.

## 0.1.0 — 2026-09-05

First release. Compares a table in PostgreSQL against a table in DuckDB and
reports exactly which rows and columns differ, without moving the data out of
either engine.

### What it does

- **`parity diff`** with human and JSON output, and CI exit codes: `0`
  identical, `1` differences found, `2` error. An error is never `1`, so a
  pipeline can tell a real disagreement from a bad connection string.
- Pushes hash aggregation into both engines and binary-searches the key space.
  On identical tables it issues **4 queries and downloads zero rows**, whether
  the table holds ten thousand rows or ten million.
- Reports each difference as `only_in_a`, `only_in_b`, or `different` with the
  columns that moved and both values.
- `--columns`, `--exclude`, `--bisection-factor`, `--threshold`,
  `--float-scale`, `--max-diffs`, `--json`, `--quiet`.

### Measured

10,000,000 rows per side, PostgreSQL 18.4 against DuckDB 1.5.5, six runs:

| Scenario | Queries | Rows downloaded | Wall time |
|---|---|---|---|
| identical tables | 4 | 0 (0.0000%) | 25-31s |
| 5 planted differences | 28 | 7,628 (0.0381%) | 48-55s |

Query and row counts are exact and hardware-independent; the times are one
laptop.

### Correctness

Every test plants a difference and asserts the tool finds exactly it. A suite
that only compares matching tables passes trivially for a completely broken
tool, and that discipline caught each of these before release:

- a `NULL` boolean compared **equal** to `FALSE`, because `CASE ... ELSE` swallowed
  the NULL. Both engines produced the same wrong answer, so cross-engine
  agreement could not detect it
- `timestamptz` columns reported **every row as different** when the two servers
  sat in different timezones. Sessions are now pinned to UTC
- the walk saw the table change underneath it, because PostgreSQL's default
  READ COMMITTED gives every statement a fresh snapshot. Now REPEATABLE READ
- absolute DuckDB paths broke on every Linux and macOS machine
- tables wider than 99 columns failed outright on PostgreSQL's 100-argument
  function limit
- two unrelated tables exhausted memory rather than answering
- `Infinity` and `NaN` aborted the run on DuckDB
- a `NULL` key was misreported as a duplicate key, and a missing `GRANT` as a
  missing table
- the bucket arithmetic overflowed int64 on wide key ranges

### Known limitations

Stated plainly, because a parity tool that reports a false match is worse than
useless.

- **Any key type.** A single integer column is bisected directly; a uuid, a
  text key, or several columns together are hashed to 60 bits for bucketing
  only. Identity stays the real key, so a collision cannot merge two rows.
- **Floats and decimals compare at 6 decimal places** by default. Two values
  differing only in the 7th place are reported as equal. `--float-scale`
  changes it, and the scale in force is printed on every run.
- **Keys must be unique**, and must not be NULL. Both are detected and refused.
- **At most 10,000 differences are reported** by default; past that the run is
  flagged `truncated` and `identical` is never true. `--max-diffs 0` lifts it.
- Supported types: integer, decimal, float, boolean, string, date, timestamp.
  Anything else is compared as raw text and warned about.
- A double whose magnitude reaches 1e32 is not supported and fails loudly.

### Engines

PostgreSQL (16 and 18), DuckDB (1.5), and MySQL (8.0). Snowflake, BigQuery
and Redshift are not implemented; see `CONTRIBUTING.md` — a dialect is roughly
80 lines, and MySQL was the first one added after release to prove that.
