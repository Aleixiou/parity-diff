# Troubleshooting

Every error exits with code `2` and prints one line to stderr that starts with
`parity:` and names the side, `[side A: postgres]` or `[side B]`. That prefix
tells you which of the two databases to look at. The messages below are quoted
from the code; `…` marks where your own names and values appear.

- [Table not found](#table-not-found)
- [Key column not found, not unique, or NULL](#key-column-not-found-not-unique-or-null)
- [FLOAT or list key refused](#float-or-list-key-refused)
- [A value or a scale is refused](#a-value-or-a-scale-is-refused)
- [Version or feature refused](#version-or-feature-refused)
- [Connection errors](#connection-errors)
- [Columns refused](#columns-refused)
- [Everything reads as different](#everything-reads-as-different)
- [The run says it is not single-snapshot](#the-run-says-it-is-not-single-snapshot)
- [Slow runs](#slow-runs)

## Table not found

```
parity: [side A: duckdb] table not found: Orders (looked in schema 'main'). Names are matched exactly, including case - did you mean main.orders?
```

Table names are matched **exactly, including case**, because they are quoted
into SQL. Unquoted SQL folds names to lower case on PostgreSQL and DuckDB and
to UPPER case on Snowflake, so the table you created as `Orders` is probably
stored as `orders` (or `ORDERS`). Use the name the error suggests.

Also check the schema in the message: an unqualified table means `public` on
PostgreSQL, `main` on DuckDB, the database's name on MySQL, the URL's schema
(else `PUBLIC`) on Snowflake, the URL's dataset on BigQuery, and the login's
default schema (normally `dbo`) on SQL Server. Pass `schema.table` to be sure.

```
parity: [side A: postgres] table public.orders exists but this role cannot read it. `information_schema` only lists tables you hold privileges on, so a missing GRANT looks exactly like a missing table. Ask for SELECT on public.orders.
```

The table is there; the login lacks `SELECT`. SQL Server cannot tell the two
apart and adds to every not-found message: `If it does exist, this login may
lack SELECT on it (db_datareader, or GRANT SELECT)`. BigQuery says `cannot
read dataset … while looking for …: it does not exist, or this account lacks
access to it`. Grants per engine: [`engines.md`](engines.md).

```
parity: [side A: postgres] table name 'db.public.orders' has 3 dot-separated parts; expected 'table' or 'schema.table'
```

Drop the database part — the database comes from the connection string.
(BigQuery alone accepts `project.dataset.table`.)

## Key column not found, not unique, or NULL

```
parity: [side B] key column(s) ['order_id'] not in orders. Columns are: ['amount', 'id', 'status']
parity: [side A] key column ['k'] in dup is not unique: 100 rows but only 10 distinct keys. Rows cannot be compared one-to-one.
parity: [side A] key column ['status'] in orders contains 14,285 NULL value(s). A row with no key cannot be matched to anything on the other side.
```

`--key` must name columns that identify one row on both sides. Key names are
matched case-insensitively (`id` matches `ID`). If one column is not unique,
give several: `--key customer_id,line_no`. Uniqueness is counted by bytes, so
on a case-insensitive collation `'a'` and `'A'` are two keys, as they should
be. A table without any unique key cannot be compared one-to-one; diff a view
that adds one (for example a row number over a deterministic order) only if
that order really is the same on both sides.

## FLOAT or list key refused

```
parity: [side A: duckdb] in fk: FLOAT column 'k' cannot be used as a key. A key is matched by exact text, and doubles are only compared rounded to the float scale, which would merge distinct keys into one row; no rendering of a double is both exact and identical on every engine. Key by another column, or by a DECIMAL/integer cast of this one in a view
```

Use another key, or a view that casts the float key to an integer or
DECIMAL. Lists and arrays are refused as keys for the same reason (`list
column … cannot be used as a key`).

## A value or a scale is refused

The tool refuses rather than round or clamp a value it cannot render exactly.

```
parity: [side A: postgres] parity: FLOAT value out of the comparable range (|x| >= 2^116, about 8.3e34) - refusing to round it: …
```

A double of magnitude 2^116 or more. Exclude the column (`--exclude`), or
compare a view that filters or rescales those values.

```
parity: [side A: mysql] … does not fit DECIMAL(65,6), the widest type MySQL can render it in at float_scale 6. MySQL would clamp it to that type's maximum without an error, so two different values could compare equal; …
```

A DECIMAL value with more integer digits than the engine can render at the
float scale (MySQL 65 − scale, DuckDB 38 − scale). A smaller `--float-scale`
gives more integer digits.

```
parity: [side A: duckdb] FLOAT column 'x' cannot be compared at float_scale 25; the maximum is 20
parity: [side A: mssql] FLOAT column 'x' cannot be compared at float_scale 19 on SQL Server; the maximum is 18
parity: [side A: mysql] DECIMAL column 'x' cannot be compared at float_scale 31; MySQL's maximum DECIMAL scale is 30
```

Lower `--float-scale`.

```
parity: [side A: mysql] …: its canonical text is longer than this server's max_allowed_packet, past which MySQL string functions return NULL. …
```

A text value too long for the MySQL server's `max_allowed_packet` once
escaped. A DBA can raise `max_allowed_packet`, or exclude the column.

## Version or feature refused

| Message (start) | Fix |
|---|---|
| `PostgreSQL … is not supported: parity needs PostgreSQL 9.6 or newer (12 or newer to compare FLOAT columns)` | Upgrade, or diff a copy on a newer server. |
| `FLOAT column 'x' needs PostgreSQL 12 or newer (server_version_num …): older servers do not print doubles as their shortest round-trip decimal, so they cannot be rendered canonically` | `--exclude` the float columns, or upgrade. |
| `FLOAT column 'x' cannot be compared in a database at compatibility level …: below 130 SQL Server converts floats to decimal inexactly. Raise the level to 130 or more, or leave the column out with --exclude` | As it says. |
| `DuckDB … is not supported: parity needs DuckDB 1.3 or newer (older versions render some values differently, which can hide a difference). Upgrade with: pip install -U duckdb` | As it says. |
| `this MySQL server has no MD5() function, which every row hash needs. MySQL 9.7 moved it into an optional component: a DBA can add it with INSTALL COMPONENT 'file://component_classic_hashing'` | As it says. |
| `this server has no UTF-8 collations (SQL Server 2019 or newer is required) …` | SQL Server 2017 and older are not supported. |
| `unsupported SQL Server engine edition …: … Microsoft Fabric and Azure Synapse differ in isolation and are not supported yet.` | Not supported yet. |
| `the server returned text that is not valid UTF-8 (…); is the database encoding SQL_ASCII?` | A PostgreSQL `SQL_ASCII` database stores bytes unchecked; text that is not UTF-8 cannot be compared with any other engine. |

## Connection errors

```
parity: [side B: duckdb] database file not found: nope.duckdb
parity: [side A: postgres] could not connect: …
parity: [side A] no dialect for scheme 'postgress'. Supported: duckdb, postgres, mysql, snowflake, bigquery, mssql.
```

- **Check the connection string grammar** in [`engines.md`](engines.md). For
  DuckDB the slash count matters: `duckdb:///rel/path.duckdb` is relative,
  `duckdb:////abs/path.duckdb` absolute (POSIX), `duckdb:///C:/path.duckdb`
  absolute (Windows).
- **Special characters in the password.** PostgreSQL and SQL Server URLs need
  reserved characters (`@ : / ? # %`) percent-encoded (`@` → `%40`). MySQL
  accepts `/ # ? :` as they are but needs `@` as `%40` (`malformed MySQL URL;
  expected mysql://user:password@host:port/database (percent-encode an `@` in
  the password as %40)`). Connection errors do not print the password (tested
  for PostgreSQL, MySQL and SQL Server).
- **Unknown options are refused, not ignored**, on MySQL, SQL Server and
  BigQuery: `unknown mssql connection parameter(s) ['encrypted']; supported:
  [...]`, `MySQL URL option 'sslmode' is not supported, and an option is
  refused rather than ignored. …`. The message lists what is accepted.
  Snowflake reads only `warehouse` and `role` and **ignores** any other
  parameter.
- **TLS.** SQL Server encrypts and checks the certificate by default; a local
  test server with a self-signed certificate needs
  `?trust_server_certificate=yes` (never in production). On MySQL,
  `ssl_mode=REQUIRED` or stricter makes the run check the session really is
  encrypted: `the URL asks for ssl_mode REQUIRED or stricter, but the
  connection to the server is not encrypted; refusing to continue`. For
  PostgreSQL, pass libpq's own parameters in the URL, e.g.
  `?sslmode=verify-full&sslrootcert=/path/ca.pem`. A corporate TLS-inspecting
  proxy or antivirus can break certificate checks for the cloud engines; point
  the driver at your organisation's CA bundle (for example
  `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` for BigQuery and Snowflake).
- **Snowflake connector 5.x.** The `[snowflake]` extra pins
  `snowflake-connector-python>=4.0,<5`, because 5.x sends a telemetry batch at
  every logout that no setting stops. If something else in your environment
  upgraded it, reinstall: `pip install "snowflake-connector-python>=4.0,<5"`.
  Snowflake logs in with user and password only (no SSO, MFA prompt or key
  pair yet), so an account policy that requires those for the user blocks it.
- **BigQuery credentials.** BigQuery uses Application Default Credentials;
  a URL holding a user or password is refused (`bigquery connection string
  must not contain credentials …`). Run `gcloud auth application-default
  login`, set `GOOGLE_APPLICATION_CREDENTIALS` to a service-account key, use
  impersonation, or run on a VM with an attached service account. A 403
  usually means the account lacks BigQuery Job User on the URL's project or
  read access to the dataset ([`engines.md`](engines.md#bigquery)).
- **SQL Server login.** Only SQL authentication is supported: `mssql
  connection string needs a user: SQL authentication (user:password@host) is
  the only kind verified so far`. A named instance goes in
  `?instance=SQLEXPRESS`; `?login_timeout=30` waits longer for a slow server.

## Columns refused

```
parity: [side A] orders has two columns that differ only in case: 'Id' and 'ID'. parity matches columns case-insensitively across engines and cannot tell these apart - rename or quote one, or diff a view that does.
parity: --columns named the key column(s) ['id']; the key is how rows are matched up, not something compared between them
parity: --columns named unknown columns: ['x']
parity: --columns named columns present on only one side: ['extra']
parity: --columns and --exclude both name: ['x']
```

As each says. A SQL Server table too wide to compile into one checksum query
asks you to compare fewer columns per run with `--columns` / `--exclude`.

## Everything reads as different

When nearly every row is reported (often with the `at least 10,000
differences … stopped early` headline, and `rows downloaded` near 100%), the
data is rarely that broken. Check, in this order:

1. **The warnings above the rows.** The run names the usual causes itself:
   a key or column whose type differs between the sides, a timezone-aware
   column against a naive one, JSON normalised differently.
2. **Wrong table or environment.** Exactly what a parity check exists to
   catch — compare the row counts in the headline with what you expect.
3. **Wrong key.** If both sides use different keys for the same row (a
   surrogate key regenerated by the migration), every row is only in A and
   only in B. Key by the natural key instead.
4. **Key type changed** (`BIGINT` → `VARCHAR`, `timestamptz` → `timestamp`):
   the same key renders as different text. Compare through a view that casts
   one side.
5. **Timestamps shifted by whole hours**: the target stored local wall-clock
   time where the source stored instants. Sessions are pinned to UTC on both
   sides, so this is a real difference in what was stored.
6. **Decimals differ in the last places**: the target rounded to fewer
   places. Lower `--float-scale` if that rounding is accepted, and say so when
   you report the result.
7. **Text differs only in trailing spaces or case**: these are real
   differences; parity does not follow a collation.

## The run says it is not single-snapshot

```
  ! side A (mssql): database 'legacy' has ALLOW_SNAPSHOT_ISOLATION OFF, so the walk runs at READ COMMITTED ...
```

On Snowflake, SQL Server without snapshot isolation, BigQuery views and MySQL
non-InnoDB tables the walk cannot read one snapshot, so rows written during
the run can show as differences. On SQL Server a DBA can enable it (`ALTER
DATABASE … SET ALLOW_SNAPSHOT_ISOLATION ON`; it does not block writers).
Otherwise diff a quiet replica, a clone (Snowflake zero-copy `CLONE`), or a
base table instead of a view, or re-run and see whether the differences move.

## Slow runs

- **Cost is one full hash pass per side**: every compared column of every row
  is rendered and hashed by MD5 inside the engine. Expect the time of a full
  table scan with a hash per row; on a laptop, 10M rows PostgreSQL ↔ DuckDB
  took about a minute.
- **An index on the key does not help** — the work is CPU-bound hashing,
  not lookup — and `--bisection-factor` does not speed up the first pass.
- **Compare fewer columns** with `--columns` or `--exclude` if some are not
  part of what you need to prove (and say so when you report the result).
- **Many differences cost round trips and downloads.** Each differing range
  is narrowed down and its rows downloaded; thousands of scattered
  differences mean many queries. Fix the systematic cause first
  ([above](#everything-reads-as-different)).
- **The sample check** adds one pass over the key per side and the download
  of about `--verify-sample` rows per side. `--verify-sample 0` skips it, at
  the cost of the risk figure.
- **Cloud warehouses**: a bigger Snowflake warehouse makes the hash pass
  faster; BigQuery bills by bytes scanned, not time (see
  [`engines.md`](engines.md)).
- **Interrupting** (Ctrl-C) cancels the running queries on PostgreSQL, MySQL
  and DuckDB; on Snowflake, BigQuery and SQL Server it waits for the query in
  flight.
