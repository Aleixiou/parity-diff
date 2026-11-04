# Engines

One section per engine: the connection string, what to install, how the tool
logs in, the least the login needs, how a live table is read, the versions
supported, what a run costs, and the engine's own caveats.

Common to all of them: the tool issues `SELECT` only, never DDL or DML, and
opens every session read-only where the engine has such a thing. Each session
is pinned to UTC and to the settings that affect how values render (the full
list is in [`limitations.md`](limitations.md#session-settings)). Use a login
that can only read anyway: then the server itself refuses a write.

- [PostgreSQL](#postgresql) · [MySQL](#mysql) · [DuckDB](#duckdb) ·
  [SQL Server / Azure SQL](#sql-server--azure-sql) · [Snowflake](#snowflake) ·
  [BigQuery](#bigquery) · [Oracle](#oracle)
- [Keeping passwords out of logs](#keeping-passwords-out-of-logs)

## Summary

| Engine | Install extra | Single snapshot | Versions |
|---|---|---|---|
| PostgreSQL | `postgres` | yes (REPEATABLE READ, read-only) | 9.6+; 12+ for FLOAT columns; 12–18 verified identical |
| MySQL | `mysql` | yes for InnoDB (REPEATABLE READ, READ ONLY); no for MyISAM/MEMORY | 8.0, 8.4 LTS, 9.x verified identical |
| DuckDB | `duckdb` | yes (file opened read-only) | 1.3+; 1.3–2.0 verified identical |
| SQL Server / Azure SQL | `mssql` | only with `ALLOW_SNAPSHOT_ISOLATION ON` | 2019+; 2019 and 2022 verified; FLOAT needs compatibility level 130+ |
| Snowflake | `snowflake` | no (READ COMMITTED only) | verified live on AWS; connector 4.x only |
| BigQuery | `bigquery` | base tables and clones yes (time travel); views no | verified live in the EU region |
| Oracle | `oracle` | yes (READ ONLY transaction) | 19c+ (19c not yet verified); 21c and 23ai verified; AL32UTF8 databases |

Every run that lacks a single snapshot says so in a caveat. Redshift,
Databricks and Fabric are not supported yet.

## PostgreSQL

```
postgres://user:password@host:5432/database      (also postgresql://)
postgres://user:password@host/database?sslmode=verify-full&sslrootcert=/path/ca.pem
```

`pip install "parity-diff[postgres]"` (psycopg 3). The URL goes to libpq as
it is, so libpq's own parameters work (`sslmode`, `sslrootcert`,
`application_name`, …), and a password left out of the URL is taken from
`PGPASSWORD` or `~/.pgpass` as for `psql`.

**Tables.** Unqualified names mean `public` — always that schema, never
whatever the session's `search_path` finds first. Materialized views can be
compared like tables.

**Minimum permissions.** `CONNECT` on the database, `USAGE` on the schema and
`SELECT` on the table:

```sql
create role parity_reader login password '…';
grant connect on database warehouse to parity_reader;
grant usage on schema public to parity_reader;
grant select on public.orders to parity_reader;
```

The tool also reads `information_schema.columns` and `pg_catalog`
(`pg_class`, `pg_attribute`, `pg_type`, `pg_namespace`, `pg_roles`) to learn
types and detect row-level security; both are readable by every role.

**Consistency.** The whole diff runs in one read-only REPEATABLE READ
transaction: one snapshot, taken at the first query. The snapshot is held for
the length of the run, which delays vacuum the way any long analytical query
does. Idle and transaction timeouts are switched off for this session only,
so a role default cannot kill it mid-run.

**Caveats.**

- **Row-level security**: rows the role's policies hide are not compared; the
  run names the table - or the view, and the RLS tables it reads at any
  depth, when the policies apply to the reader or to a view's owner. Diff as
  a role with `BYPASSRLS` for a complete result.
- **Column-level grants**: a column the login lacks SELECT on is not compared
  and the run is not identical (exit 1), naming it; `--exclude` it to make
  the gap deliberate.
- **Inheritance**: a parent table's rows include its children's (as any
  query on it reads them); the run names the children.
- **money** is read with the decimal places of the login's own `lc_monetary`
  (its role, database or server default, before parity pins `C`): `12345`
  stored under a yen locale is 12345, not 123.45. A locale with other than
  two places draws a caveat; money written under a session-only locale that
  differs from the login's default cannot be detected.
- **Extension types** are compared by their text where it is known exact
  (citext, hstore, ltree, isn, cube, PostGIS, pgvector), others by their text
  and their binary send form - or refused, naming the column, when they have
  none or sit inside an array, composite or range.
- **Connecting** gives up after 30 seconds unless the URL sets
  `connect_timeout`.
- **`--where`** may not call functions that change the session or act outside
  the read (`set_config`, advisory locks, `pg_sleep`, sequences, large
  objects, `dblink`, `query_to_xml`, ...). Every pinned setting is re-read
  after the walk; one that moved makes the run unexplained, never identical.
- The database should be UTF-8; text in a `LATIN1`/`WIN1252` database is
  hashed as UTF-8, and a `SQL_ASCII` database holding non-UTF-8 bytes is
  refused.
- Servers older than 9.6 are refused; FLOAT columns need 12 or newer.

## MySQL

```
mysql://user:password@host:3306/database
mysql://user:password@host/database?ssl_mode=VERIFY_IDENTITY&ssl_ca=/path/ca.pem
```

`pip install "parity-diff[mysql]"` (mysql-connector-python). A password may
contain `/ # ? :` unencoded but must spell `@` as `%40`. The URL takes the TLS
options `ssl_mode` (`DISABLED`, `PREFERRED`, `REQUIRED`, `VERIFY_CA`,
`VERIFY_IDENTITY`), `ssl_ca`, `ssl_cert`, `ssl_key`, `ssl_verify_cert`,
`ssl_verify_identity` and `ssl_disabled`, and refuses any other option rather
than ignore it. With `ssl_mode=REQUIRED` or stricter the run checks that the
session really is encrypted, since the driver would otherwise fall back to
plaintext without saying so. Password authentication only.

**Tables.** Unqualified names mean the URL's database.

**Minimum permissions.** `SELECT` on the whole table:

```sql
create user 'parity_reader'@'%' identified by '…';
grant select on warehouse.orders to 'parity_reader'@'%';
```

Column-level grants (`grant select (id, a) on …`) are refused: MySQL then
lists only the granted columns in `information_schema`, and the others would
silently go uncompared. The tool also reads `information_schema.columns`,
`information_schema.tables` (to learn each table's storage engine) and, for a
view, `information_schema.view_table_usage`, which list what the user may
see. To interrupt a running query on Ctrl-C it opens a second connection as
the same user and issues `KILL QUERY` on its own session, which needs no extra
privilege - but needs a free connection: a login at its
`max_user_connections` cannot be cancelled, and the run says so with the
`KILL QUERY` a DBA can issue instead.

**Consistency.** One REPEATABLE READ, READ ONLY transaction — one snapshot for
InnoDB tables. A table on a non-transactional engine (MyISAM, MEMORY, ARCHIVE,
CSV) has no snapshot, and the run says so - for a view too, when any of its
base tables is one, or when its base tables cannot be listed (before 8.0.13).

**MariaDB** is refused by name: it accepts `mysql://` but differs in
collations, functions and renderings, and nothing here was verified on it.

**Caveats.**

- MySQL 9.7+ needs a DBA to `INSTALL COMPONENT 'file://component_classic_hashing'`
  for `MD5()`.
- `sql_select_limit`, which would silently cap results, is reset for the
  session; `sql_mode` and the time zone are pinned.
- DECIMAL values with more than 65 − scale integer digits are refused (MySQL
  would clamp them silently); `--float-scale` is capped at 30 for DECIMAL
  columns.
- A row wider than `max_allowed_packet` is hashed column by column; a single
  value past it once escaped is refused.
- Text in a legacy character set (sjis, euckr, gbk, big5, cp1251, ucs2, …)
  renders as its Unicode text when every character converts; a value holding
  a character with no Unicode mapping (a Shift-JIS user-defined character, a
  ucs2 surrogate) renders as `parity: not convertible from <charset> 0x<bytes>`,
  so it is never equal to a `?`, a NULL or another such value - and never
  equal to anything on another engine.
- ZEROFILL and YEAR render their values (`5`, `0`), not their display
  padding (`00000005`, `0000`). ENUM's error value (index 0, what an invalid
  value becomes outside strict mode) renders `parity: invalid ENUM value
  (index 0)`, not `''`.
- MySQL 9 `VECTOR` renders as the hex of its stored floats: exact MySQL to
  MySQL, equal to nothing on another engine.
- A `--where` with `FOR SHARE`, `FOR UPDATE`, `LOCK IN SHARE MODE` or `INTO`
  is refused: a locking read would hold row locks on the live table for the
  whole diff.
- JSON values of MySQL's own scalar types (DATE, TIME, DATETIME, binary,
  made with `cast(... as json)`) print as JSON strings and compare equal to
  the string of the same text - see [limitations](limitations.md).

## DuckDB

```
duckdb:///relative/path.duckdb                  (three slashes = relative)
duckdb:////var/lib/warehouse.duckdb             (four slashes = absolute, POSIX)
duckdb:///C:/data/warehouse.duckdb              (absolute, Windows)
duckdb:///:memory:
```

`pip install "parity-diff[duckdb]"` (needs `duckdb>=1.3`). The file is
opened read-only; it must exist (`database file not found: …`). Exactly one
leading slash is removed from the path, as in SQLite and SQLAlchemy URLs.
A file that is not a DuckDB database (a SQLite file, say) is refused before
DuckDB opens it, so no extension is ever loaded to read it. MotherDuck
(`md:`, `motherduck:`) is not supported.

**Tables.** Unqualified names mean `main`. Tables are read as
`"<catalog>"."<schema>"."<table>"`, so a schema named like the file is fine.
A view is described as it returns rows now: if the catalog's entry is stale
(its base table was altered after the view was created) the run warns and
compares the live columns.

**Refused files.** A database that defines a macro or a type named like a
DuckDB built-in (`md5`, `replace`, `json`, ...) is refused: DuckDB calls the
stored macro before the built-in, so parity's checksums would be computed by
it. Compare a copy without those macros.

**Permissions.** Read access to the file. Because it is opened read-only,
another process may hold it open read-only too, but not for writing.

**Consistency.** A read-only file does not change during the run.

**Caveats.** Extension auto-install and auto-load are switched off, so the
tool never downloads anything. `TIMESTAMP_NS` keeps its nanoseconds only
against an engine that holds them too; otherwise a caveat says they are
truncated. Versions before 1.3 are refused.

## SQL Server / Azure SQL

```
mssql://user:password@host:1433/database                  (also sqlserver://)
mssql://user:password@host/database?instance=SQLEXPRESS&encrypt=strict
mssql://user:password@localhost/database?trust_server_certificate=yes   (local test server only)
```

`pip install "parity-diff[mssql]"` (Microsoft's `mssql-python`, which needs no
separately installed ODBC driver). SQL Server 2019 or newer, Azure SQL
Database and Azure SQL Managed Instance.

**Authentication.** SQL authentication only (user and password in the URL);
Windows and Entra ID authentication are not supported yet. Options, all
others refused: `encrypt` (`yes`, `no`, `strict`, `mandatory`, `optional`),
`trust_server_certificate` (`yes`/`no`), `application_intent`
(`readonly`/`readwrite`), `login_timeout` (seconds) and `instance` (a named
instance). Every value is quoted, so a `;` in a password cannot add a keyword.
Encryption is on by default and the certificate is checked: use
`trust_server_certificate=yes` only for a local test server with a
self-signed certificate.

**Tables.** Unqualified names mean the login's default schema (normally
`dbo`). A name holding a dot is bracketed: `dbo.[orders.2024]` (`]]` inside
brackets is a `]`). A synonym for an object in the same database is read
through its base table's columns; one pointing into another database or
server is refused, naming its target.

**Minimum permissions.** SQL Server has no read-only session, so the tool
issues only `SELECT`, refuses anything else before it reaches the server, and
never commits — but **use a login that holds only `db_datareader`** (or
`GRANT SELECT` on the tables), which makes the server itself refuse a write:

```sql
create login parity_reader with password = '…';
-- in the database:
create user parity_reader for login parity_reader;
alter role db_datareader add member parity_reader;   -- or: grant select on dbo.orders to parity_reader;
grant view definition to parity_reader;            -- metadata only: lets the masking and row-level security checks see policies and view sources
grant unmask to parity_reader;                       -- only where dynamic data masking is used
```

- **Dynamic data masking.** A login without `UNMASK` reads a masked column
  as its mask (`aXXX@XXXX.com`, `0`), and every expression over it too, so
  two tables whose masked values differ would compare equal. A compared
  column that is masked for this login is therefore **refused**, naming the
  side, table and column: `GRANT UNMASK` (database-wide, or on 2022 per
  schema, table or column), or leave it out with `--exclude`. A view reading
  a masked column the login cannot unmask is refused as a whole (which view
  column carries it is not in the catalog), and so is a view the login lacks
  `VIEW DEFINITION` on, in a database holding such columns.
- **Row-level security.** A security policy's filter predicate silently
  hides rows from the reading user, and those rows are not compared. When
  an enabled filter predicate applies to the table (or to a table a view
  reads) every run's output says so. Policies are visible only with
  `VIEW DEFINITION` on the database (on the table is not enough); without it the output says RLS cannot be
  ruled out. SQL Server has no BYPASSRLS: diff as a user the predicates let
  read every row.

The tool also reads `INFORMATION_SCHEMA.COLUMNS`, `sys.databases` (the current
database's snapshot setting and compatibility level), `sys.fn_helpcollations()`,
`SERVERPROPERTY('EngineEdition')`, `sys.objects`, `sys.tables`,
`sys.synonyms`, `sys.masked_columns`, `sys.security_policies`,
`sys.security_predicates` and `sys.sql_expression_dependencies` (each shows
what this login may see) and `HAS_PERMS_BY_NAME`.

**Consistency.** With `ALLOW_SNAPSHOT_ISOLATION ON` the diff runs in one
SNAPSHOT transaction and does not block writers. Without it the run stays at
READ COMMITTED and prints a warning on every run. It never uses REPEATABLE
READ or SERIALIZABLE (their locks would stall writers on a live system for
the whole diff) or NOLOCK (dirty reads invent differences). To enable
snapshots: `ALTER DATABASE … SET ALLOW_SNAPSHOT_ISOLATION ON` (a DBA
decision: it adds row versioning in tempdb). A **memory-optimized** table is
read with the table hint `WITH (SNAPSHOT)` - In-Memory OLTP's own snapshot,
which refuses a SNAPSHOT session - so it too is read at one point in time,
whatever `ALLOW_SNAPSHOT_ISOLATION` says.

**Load on a live server.** The checksum query is one full scan and hash per
side; on a busy legacy server cap it with Resource Governor (a workload group
for the parity login with `MAX_DOP` and `REQUEST_MAX_MEMORY_GRANT_PERCENT`)
rather than per-query hints. Under SNAPSHOT the reads take no row locks, so
the diff does not block writers; at READ COMMITTED a read waits for a
writer's lock as any query does (the driver has no query timeout).

**Caveats.**

- `datetime2(7)` / `datetimeoffset(7)` compare at microseconds against other
  engines (the 7th digit truncated, with a caveat); `datetime` compares as its
  displayed milliseconds; `char(n)` without trailing padding; GUIDs in lower
  case.
- FLOAT columns need compatibility level 130+; `--float-scale` above 18 is
  refused for them.
- `geometry` / `geography` compare as their SRID and exact extended WKB
  (`SRID=4326;0101…`), so a changed reference system is a difference;
  `hierarchyid` as its path. No other engine has them.
- `varchar`/`char`/`text` in a Japanese, Chinese or Korean code page (932,
  936, 949, 950) may hold byte pairs that are no character; such a value
  compares as a marker and its bytes, never as the `?` or `・` the
  conversion makes of it.
- A `decimal(38,0)` with more than 32 integer digits compares exactly at the
  default scale (its own text, padded with zeros).
- `--where` on SQL Server also refuses table and query hints (`with (...)`,
  `option (...)`) and ODBC escape braces (`{fn ...}`) outside literals.
- Ctrl-C waits for the query in flight: the driver cannot cancel one.
- Fabric and Synapse are refused at connect.

## Snowflake

```
snowflake://user:password@account/database/schema?warehouse=WH&role=ROLE
snowflake://user@account/database/schema?warehouse=WH&private_key_file=C:/keys/parity.p8
```

`pip install "parity-diff[snowflake]"`, which installs
`snowflake-connector-python>=4.0,<5`. **Connector 5.x is not supported**: it
flushes a telemetry batch to Snowflake at every logout that no setting stops,
and the tool sends no telemetry. With 4.x it switches the connector's
telemetry and its cloud-metadata probes (`SNOWFLAKE_DISABLE_PLATFORM_DETECTION`)
off.

**Authentication.** User and password, or **key-pair** (Snowflake is phasing
out password-only sign-in). For key-pair, leave the password out of the URL
and add `private_key_file=<path>` to a PEM RSA private key (PKCS#8, as
`openssl genrsa` + `openssl pkcs8` write it; `~` is expanded). If the key is
encrypted, its passphrase is read from the environment variable
`PARITY_SNOWFLAKE_KEY_PASSPHRASE` (or `PARITY_SNOWFLAKE_KEY_PASSPHRASE_A` /
`_B` for one side), **never from the URL**. The key is loaded by parity and
handed to the connector as bytes with `authenticator=SNOWFLAKE_JWT` and no
password; the telemetry switches apply to this login as to any other.
A URL with both a password and a key file, or neither, is refused; so is a
passphrase set for a key that is not encrypted (the key on disk is not
protected the way you think, or the path names another file). Errors print
the key's path, never the key or the passphrase.

The query string accepts `warehouse`, `role` and `private_key_file` and
nothing else: an unknown, repeated or empty parameter is refused with the
list of accepted ones. SSO (`externalbrowser`), OAuth and MFA prompts are not
supported (`authenticator=` is refused): each needs a browser or a token in
the URL, so a user whose account policy requires them cannot log in.

**Tables.** Unqualified names mean the URL's schema, else `PUBLIC`; tables are
always schema-qualified in queries. Snowflake stores unquoted names in UPPER
case, so `--a-table orders` will not find `ORDERS`; columns are matched across
engines case-insensitively, so `id` on PostgreSQL matches `ID` here.

**Minimum permissions.** A role with:

```sql
grant usage on warehouse WH to role PARITY_READER;
grant usage on database DB to role PARITY_READER;
grant usage on schema DB.SCHEMA to role PARITY_READER;
grant select on table DB.SCHEMA.ORDERS to role PARITY_READER;
grant role PARITY_READER to user PARITY;
```

The tool also reads the database's `information_schema.columns`, which lists
what the role may see.

**Consistency.** Snowflake offers only READ COMMITTED: each query sees the
table as of its own start, and every run says so. For a single-snapshot
result diff a quiet table or a zero-copy clone (`create table … clone …`).

**Cost.** The run keeps the warehouse busy for its duration: one full hash
pass over the compared columns, the narrowing queries, and the sample check.
Credits are billed for the time the warehouse runs (with its usual minimum
per resume). A larger warehouse shortens the hash pass.

**Policies.** A masking policy hands the reading role the mask on both
sides, and a row access policy hides rows from both: either makes real
differences invisible. The policies attached to the compared table (from
`POLICY_REFERENCES`) are named in a caveat on every run; for a view, the
caveat says the policies of the tables beneath it are not checked. Diff as a
role the policies do not mask or filter for a complete result.

**VARIANT doubles.** Snowflake prints a DOUBLE inside a VARIANT, OBJECT or
ARRAY with 16 significant digits, and two adjacent doubles can print alike.
A JSON column holding such a double is refused, naming the column, a key and
the path; `--exclude` it. Integers, decimals and doubles whose text reads
back exactly are compared. GEOGRAPHY, GEOMETRY and VECTOR columns compare
as WKB, EWKB and the vector's elements (listed as unmapped: within
Snowflake only).

**Caveats.** `ROWS_PER_RESULTSET`, which would cap results, is reset for the
session, and `QUOTED_IDENTIFIERS_IGNORE_CASE` and `STRICT_JSON_OUTPUT` are
pinned off. A row filter may not contain `//` outside a quoted string
(Snowflake's line comment). `TIMESTAMP(7-9)` columns compare at microseconds against engines
that hold only microseconds, with a caveat. Ctrl-C waits for the query in
flight (the dialect has no cancel). The login itself names the
driver, Python and OS versions, as every Snowflake client's does.

## BigQuery

```
bigquery://project/dataset
bigquery://project/dataset?location=EU
bigquery://project                       (then name tables as dataset.table)
```

`pip install "parity-diff[bigquery]"` (google-cloud-bigquery 3.15+). `location`
is the only option; without it the dataset's own location is used.

**Authentication.** Application Default Credentials — a service account via
`GOOGLE_APPLICATION_CREDENTIALS`, `gcloud auth application-default login`,
service-account impersonation, or the VM's attached identity — so no secret
goes in the connection string, and a URL that holds one is refused. The
credentials are requested with the read-only `bigquery.readonly` scope, under
which BigQuery itself refuses any write: the tool cannot modify your project
even by mistake.

**Tables.** Unqualified names mean the URL's dataset. A table may also be
given as `dataset.table`, or `project.dataset.table` to read another
project's data while the jobs run (and are billed) in the URL's project.

**Minimum permissions.**

- **BigQuery Job User** (`roles/bigquery.jobUser`) on the URL's project — the
  jobs run and are billed there;
- **BigQuery Data Viewer** (`roles/bigquery.dataViewer`) on each dataset read
  (it covers reading the dataset's metadata, its `INFORMATION_SCHEMA.COLUMNS`
  and `TABLES`, and the table data).

**Consistency.** BigQuery has no multi-statement snapshot, so every query
reads a base table `FOR SYSTEM_TIME AS OF` the server time captured by the
first query: one snapshot; a table clone is pinned the same way, and a
table snapshot is immutable. **Views are not pinned** — BigQuery silently
ignores time travel on a view (and on a materialized view or an external
table) — so it is read live at every level, and the run says so.
`current_timestamp()` in `--where` is *not* pinned by the snapshot: every
query has its own clock.

**Cost.** On-demand pricing bills **bytes scanned**, and BigQuery bills every
query for the full columns it reads, whatever its `WHERE` clause (unless the
table is partitioned or clustered on the key). So every run reads the
compared columns of the **whole table** at least twice on each BigQuery side
— the first checksum pass and the sample check — plus the key column for the
key statistics; when differences exist, each narrowing level and each row
download reads them again. Estimate an identical-tables run at about twice
the size of the compared columns, and more when there are differences;
`--columns` / `--exclude` reduce it, and a dry run of `select <columns> from
<table>` in the console shows that size. Capacity (slot) pricing bills slot
time instead.

**Caveats.**

- ARRAY follows the shared list rule (so it matches PostgreSQL arrays and
  DuckDB lists); a BigQuery table stores a NULL array as empty, so a NULL
  array elsewhere reads as different. STRUCT (each field by its own rule,
  named), GEOGRAPHY (its WKB), INTERVAL and RANGE have no cross-engine rule:
  they are compared as text that is injective on BigQuery, listed as not
  covered, and carry no risk figure.
- Hidden pseudo-columns (`_PARTITIONTIME` of an ingestion-time partitioned
  table) are not data and are not compared.
- Table names may be written as the console copies them, in backticks
  (`` `project.dataset.table` `` or per part), and a domain-scoped project
  (`example.com:project.dataset.table`) is read whole.
- A dataset in another location than the connection's jobs is reported with
  its location and the `?location=` that reaches it.
- `TIMESTAMP` on BigQuery is an instant (its naive type is `DATETIME`), and
  the tool treats it so.
- Ctrl-C waits for the query in flight: cancelling a job needs a write scope.

## Oracle

```
oracle://user:password@host:1521/service_name
oracle://user:password@host/FREEPDB1?tcp_connect_timeout=5
oracle://user:password@host:2484/service_name?protocol=tcps
```

`pip install "parity-diff[oracle]"` (python-oracledb in its default thin
mode: pure Python, no Oracle client or Instant Client to install). The path is
the **service name** (a PDB such as `ORCLPDB1`, `FREEPDB1`, an Autonomous
Database service), not a SID. Verified on Oracle Database 23ai (Free 23.26)
and 21c (XE 21.3), where the whole live suite passes; 19c is accepted but
not yet verified, and older servers are refused.

**Authentication.** User and password only, for now (no wallet, Kerberos or
OS authentication). Options, all others refused: `tcp_connect_timeout`
(seconds) and `protocol` (`tcp` or `tcps`).

**Tables.** Oracle stores unquoted names in upper case, and table names are
matched exactly: `--a-table ORDERS`, not `orders` (the "table not found" error
names the near miss). Key and column names match case-insensitively, so
`--key id` finds `ID`. Unqualified names mean the session's current schema —
the login user, unless a logon trigger moved it — and every query is
schema-qualified, so the rows read always come from the table introspected.

**Minimum permissions.** `CREATE SESSION` and `SELECT` on the compared tables
— nothing else, verified with exactly that login:

```sql
create user parity_reader identified by "…";
grant create session to parity_reader;
grant select on app.orders to parity_reader;
```

The tool reads `ALL_TAB_COLUMNS` (which lists only tables the login can
read), `ALL_POLICIES` and `NLS_DATABASE_PARAMETERS`, all readable by any
login. Long rows are hashed by an MD5 written in PL/SQL and declared inside
the query (`WITH FUNCTION`), which needs no `CREATE PROCEDURE` and leaves
nothing behind — and no `EXECUTE` on `DBMS_CRYPTO`, which a reader does not
hold.

**Consistency.** The session runs one `SET TRANSACTION READ ONLY`
transaction: every query of the walk reads the database as of its first
statement, and the server refuses any write. It is never committed. Two
Oracle specifics:

- A table created, altered or truncated during the diff, or within a few
  seconds before it (even the first insert into a table created without a
  segment counts; measured: always at once, sometimes 1.1 s later, never
  after 1.6 s), cannot be read in that snapshot: the run stops with
  ORA-01466, naming the side. Run it again.
- A very long diff on a busy source can outlive the undo retention and stop
  with ORA-01555 (snapshot too old). Loud, never a wrong answer; raise
  `UNDO_RETENTION` or diff a quieter copy.

**Database character set.** The database must be AL32UTF8 (the default since
12.2); others are refused at connect, because Oracle's `STANDARD_HASH` hashes
VARCHAR2 bytes in the database character set and the tool hashes UTF-8.
NVARCHAR2, NCHAR and NCLOB values are converted to it first; a value that
does not convert raises (`NLS_NCHAR_CONV_EXCP` is pinned on).

**Speed.** Measured on Oracle Database 23ai Free (which caps itself at two
CPU threads) in a container on a laptop, against DuckDB, 1,000,000 rows of
five columns (integer key, NUMBER(12,2), VARCHAR2, BINARY_DOUBLE,
TIMESTAMP): identical tables in 51 s (4 queries, no rows downloaded), one
changed row found in 63 s (8 queries, 1,952 rows downloaded), the sample
check included. A licensed server with more cores is faster; the work is
one hash pass per side, as on every engine.

**Session settings pinned at connect:** `NLS_LANGUAGE`, `NLS_TERRITORY`,
`NLS_CALENDAR` (Gregorian), `NLS_NUMERIC_CHARACTERS`, `NLS_DATE_FORMAT`,
`NLS_TIMESTAMP_FORMAT`, `NLS_TIMESTAMP_TZ_FORMAT`, `NLS_DATE_LANGUAGE`,
`NLS_SORT` and `NLS_COMP` (BINARY), `TIME_ZONE` (+00:00), `NLS_NCHAR_CONV_EXCP`,
`CURSOR_SHARING` (EXACT) and `QUERY_REWRITE_ENABLED` (off, so a stale
materialized view can never answer for the table). A logon trigger setting
every one of them to a hostile value was verified not to move a single row's
text.

**Types.**

| Oracle | Compared as |
|---|---|
| `NUMBER(p,0)`, `NUMBER(*,0)`, `INTEGER` | integer, exact (38 digits); a key wider than 18 digits is hashed, never bucketed in rounding arithmetic |
| `NUMBER`, `NUMBER(p,s)`, `FLOAT(p)` | decimal at the float scale, half away from zero; `FLOAT(p)` is a decimal NUMBER, not IEEE; a value of 10^38 or more is refused |
| `BINARY_DOUBLE`, `BINARY_FLOAT` | the canonical FLOAT form (shortest round-trip decimal), Infinity and NaN as tokens |
| `VARCHAR2`, `NVARCHAR2`, `CHAR`, `NCHAR`, `CLOB`, `NCLOB` | text, exactly; `CHAR`/`NCHAR` without trailing padding |
| `DATE` | a **timestamp** (it holds a time of day): `2024-01-31 00:00:00.000000` |
| `TIMESTAMP(p)` | timestamp at microseconds; p > 6 truncated, with a caveat |
| `TIMESTAMP WITH [LOCAL] TIME ZONE` | the UTC instant |
| `INTERVAL YEAR TO MONTH`, `INTERVAL DAY TO SECOND` | months / days and microseconds (`canonical_interval_text`) |
| `RAW`, `BLOB` | bytes, exactly |
| `BOOLEAN` (23ai) | boolean |
| `JSON` (23ai), `ROWID`, `UROWID` | not covered: compared as their text, and listed as such |
| `LONG`, `LONG RAW`, `XMLTYPE`, `VECTOR`, `BFILE`, object types | refused, naming the column: leave them out with `--exclude` |

**Caveats.**

- **Oracle stores the empty string as NULL.** An Oracle NULL is compared as
  NULL, so a row the other side holds as `''` is reported as `different` —
  which it is, on the target. Every run comparing a text column with an
  Oracle side prints this caveat. If the migration deliberately turned
  Oracle's NULLs into `''` (or the reverse), those rows are reported, not
  hidden; there is no mode that equates them.
- **DATE against a date-only column differs on every row**, since Oracle's
  DATE carries a time and is rendered with it. The run warns about the type
  mismatch; compare it against a timestamp column, or through a view.
- Oracle dates before 1582-10-15 are Julian-calendar dates, as Oracle stores
  them; they are compared by their year, month and day labels, which is how
  every driver and export carries them. Date *arithmetic* there is Julian
  too: `date '0999-12-25' + 66` is 1000-02-29 on Oracle and 1000-03-01 on
  DuckDB or PostgreSQL (verified), so data generated by arithmetic before
  1582 differs between them - and is reported, label by label.
- Rows whose text may pass 4,000 bytes (estimated from the raw column
  lengths and the declared number widths) are hashed as a CLOB: natively by
  `DBMS_CRYPTO.HASH` when the login may call it, otherwise by the PL/SQL MD5
  at roughly 0.5 MB/s per session. An ordinary row is hashed by
  `STANDARD_HASH` in SQL. Both give the same hash; granting `EXECUTE ON
  SYS.DBMS_CRYPTO` only makes wide or long tables faster.
- Ctrl-C cancels the query in flight (python-oracledb's `cancel`).
- Virtual Private Database policies can hide rows from the login; a table
  carrying one gets a caveat. Oracle Data Redaction cannot be detected
  without privileges: diff as a login it does not apply to.

## Keeping passwords out of logs

The CLI takes connection strings as arguments only; it has no flag or
environment variable of its own for them. Keep them out of your shell history
and CI logs by putting each in an environment variable and quoting it:

```bash
export LEGACY_URL='postgres://parity_reader:…@legacy-host/warehouse'
export WAREHOUSE_URL='snowflake://PARITY:…@myaccount/DB/ANALYTICS?warehouse=WH'
parity diff --a "$LEGACY_URL" --a-table public.orders \
            --b "$WAREHOUSE_URL" --b-table ANALYTICS.ORDERS --key order_id
```

```powershell
$env:LEGACY_URL = 'postgres://parity_reader:…@legacy-host/warehouse'
parity diff --a $env:LEGACY_URL --a-table public.orders --b $env:WAREHOUSE_URL --b-table ANALYTICS.ORDERS --key order_id
```

In CI, store each URL as a secret and map it to an environment variable (see
the README's CI example); GitHub Actions masks secret values in logs.

What the tool prints: the report names tables, columns, keys and values,
never a connection string. Connection errors are written so they do not
contain the password (tested for PostgreSQL, MySQL, SQL Server and
Snowflake); BigQuery takes no secret in its URL at all, and a Snowflake key's
passphrase is read from the environment, never the URL. What it cannot hide: the expanded
arguments are visible in the process list to other users of the same machine
while it runs, so use a dedicated read-only login. On PostgreSQL you can leave
the password out of the URL entirely and let libpq read `PGPASSWORD` or
`~/.pgpass`.
