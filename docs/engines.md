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
  [BigQuery](#bigquery)
- [Keeping passwords out of logs](#keeping-passwords-out-of-logs)

## Summary

| Engine | Install extra | Single snapshot | Versions |
|---|---|---|---|
| PostgreSQL | `postgres` | yes (REPEATABLE READ, read-only) | 9.6+; 12+ for FLOAT columns; 12–18 verified identical |
| MySQL | `mysql` | yes for InnoDB (REPEATABLE READ, READ ONLY); no for MyISAM/MEMORY | 8.0, 8.4 LTS, 9.x verified identical |
| DuckDB | `duckdb` | yes (file opened read-only) | 1.3+; 1.3–2.0 verified identical |
| SQL Server / Azure SQL | `mssql` | only with `ALLOW_SNAPSHOT_ISOLATION ON` | 2019+; 2019 and 2022 verified; FLOAT needs compatibility level 130+ |
| Snowflake | `snowflake` | no (READ COMMITTED only) | verified live on AWS; connector 4.x only |
| BigQuery | `bigquery` | base tables yes (time travel); views no | verified live in the EU region |

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
  run names the table. Diff as a role with `BYPASSRLS` for a complete result.
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

**Minimum permissions.** `SELECT` on the table:

```sql
create user 'parity_reader'@'%' identified by '…';
grant select on warehouse.orders to 'parity_reader'@'%';
```

The tool also reads `information_schema.columns` and `information_schema.tables`
(to learn each table's storage engine), which list what the user may see. To
interrupt a running query on Ctrl-C it opens a second connection as the same
user and issues `KILL QUERY` on its own session, which needs no extra
privilege.

**Consistency.** One REPEATABLE READ, READ ONLY transaction — one snapshot for
InnoDB tables. A table on a non-transactional engine (MyISAM, MEMORY) has no
snapshot, and the run says so.

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

**Tables.** Unqualified names mean `main`.

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
`dbo`).

**Minimum permissions.** SQL Server has no read-only session, so the tool
issues only `SELECT`, refuses anything else before it reaches the server, and
never commits — but **use a login that holds only `db_datareader`** (or
`GRANT SELECT` on the tables), which makes the server itself refuse a write:

```sql
create login parity_reader with password = '…';
-- in the database:
create user parity_reader for login parity_reader;
alter role db_datareader add member parity_reader;   -- or: grant select on dbo.orders to parity_reader;
```

The tool also reads `INFORMATION_SCHEMA.COLUMNS`, `sys.databases` (the current
database's snapshot setting and compatibility level), `sys.fn_helpcollations()`
and `SERVERPROPERTY('EngineEdition')`, all readable by any database user.

**Consistency.** With `ALLOW_SNAPSHOT_ISOLATION ON` the diff runs in one
SNAPSHOT transaction and does not block writers. Without it the run stays at
READ COMMITTED and prints a warning on every run. It never uses REPEATABLE
READ or SERIALIZABLE (their locks would stall writers on a live system for
the whole diff) or NOLOCK (dirty reads invent differences). To enable
snapshots: `ALTER DATABASE … SET ALLOW_SNAPSHOT_ISOLATION ON` (a DBA
decision: it adds row versioning in tempdb).

**Caveats.**

- `datetime2(7)` / `datetimeoffset(7)` compare at microseconds against other
  engines (the 7th digit truncated, with a caveat); `datetime` compares as its
  displayed milliseconds; `char(n)` without trailing padding; GUIDs in lower
  case.
- FLOAT columns need compatibility level 130+; `--float-scale` above 18 is
  refused for them.
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

**Caveats.** `ROWS_PER_RESULTSET`, which would cap results, is reset for the
session. `TIMESTAMP(7-9)` columns compare at microseconds against engines
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
first query: one snapshot. **Views are not pinned** — BigQuery silently
ignores time travel on a view — so a view is read live at every level, and
the run says so.

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

- ARRAY, STRUCT and INTERVAL columns have no exact rule yet: they are compared
  as raw text, listed as not covered, and carry no risk figure.
- `TIMESTAMP` on BigQuery is an instant (its naive type is `DATETIME`), and
  the tool treats it so.
- Ctrl-C waits for the query in flight: cancelling a job needs a write scope.

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
