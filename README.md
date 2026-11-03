# parity

[![tests](https://github.com/Aleixiou/parity-diff/actions/workflows/tests.yml/badge.svg)](https://github.com/Aleixiou/parity-diff/actions/workflows/tests.yml)

**Prove two tables in two different database engines hold the same data —
without moving the data out of either engine.**

Migrations don't fail on translation. They fail at cutover, because nobody can
prove the new pipeline produces the same data as the old one — so the legacy
system runs in parallel "just to be safe", forever, at double the cost.
`parity` is the proof.

```bash
parity diff \
  --a "postgres://user:pw@legacy-host/warehouse" --a-table public.orders \
  --b "duckdb:///./new.duckdb"                   --b-table main.orders \
  --key order_id
```

```
✗ 5 differences in 10,000,000 rows
  28 queries · 7,628 rows downloaded (0.04% of both tables) · 47.9s
  1 only in A · 1 only in B · 3 different

  only in A   key 999999999
  only in B   key 4000
  different   key 13             columns: note
      note   A ''   B NULL
  different   key 6010000        columns: amount
      amount   A 700.010000   B 700.000000
  different   key 8700000        columns: is_refunded
      is_refunded   A NULL   B false

  comparing floats and decimals at 6 decimal places
```

Exit code `1`. Put it in CI and the build fails until the data agrees.

## How it works

`parity` pushes hash aggregation **down into both engines**. It asks each side
for one checksum per bucket of the key range, compares a handful of integers,
recurses only into the buckets that disagree, and downloads rows only from
ranges already proven to differ.

On identical tables it downloads **zero rows** and issues **four queries**,
whether the table has ten thousand rows or ten million.

Then it **checks itself**. The checksums are only as right as the SQL that
renders each value to text inside each engine, and a mistake there can make
two different values look alike on both sides — the way every false match
this tool has ever had worked. So after the walk, about 1,000 rows (all of
them, for a table that small) are fetched again from both sides as the
database drivers return them, and compared in plain Python by the documented
rules, without any of that SQL. If the two methods disagree about a single
row, the result is **not identical** and the report names the row. Cost: one
extra pass per side that hashes the key only, plus the sample.

## Measured

10,000,000 rows per side, PostgreSQL 18.4 ↔ DuckDB 1.5.5, median of five runs
on one developer laptop (`demo/benchmark.py`):

| Scenario | Queries | Rows downloaded | Wall time |
|---|---|---|---|
| identical tables | **4** | **0** (0.0000%) | 25–31s |
| 5 planted differences | **28** | **7,628** (0.0381%) | 48–55s |

**The query counts and row counts are exact and hardware-independent** — they
are properties of the algorithm, and the test suite pins them. Reproduce them
and you should get the same integers.

The wall times are a range across six runs on one developer laptop, and they
vary by about 25% with whatever else that laptop is doing. Treat them as an
order of magnitude, not a specification.

The query count and the rows-downloaded figures are **exact and
hardware-independent** — they are properties of the algorithm, and the test
suite pins them. The wall times are one machine under sustained load; treat
them as an order of magnitude, not a specification.

The five planted differences — a changed decimal, a deleted row, an inserted
row at key 999,999,999, a `NULL` turned into `''`, and a `FALSE` turned into
`NULL` — are all found exactly, with no false positives.

Cost is roughly **one full hash pass per side**. It is CPU-bound on MD5, not
IO-bound on key lookup, which is why **an index on the key column makes no
measurable difference** (measured at 10M: 38.8s without, 37.3s with).
Raising
`--bisection-factor` does not speed up the dominant first pass; it only reduces
round trips on later levels.

## Install

```bash
pip install "parity-diff[all]"        # every engine
pip install "parity-diff[duckdb]"     # DuckDB only — no other driver pulled in
pip install "parity-diff[postgres]"   # PostgreSQL only
pip install "parity-diff[mysql]"      # MySQL only
pip install "parity-diff[snowflake]"  # Snowflake only
pip install "parity-diff[bigquery]"   # BigQuery only
pip install "parity-diff[mssql]"      # SQL Server / Azure SQL only
```

> The PyPI distribution is `parity-diff` — plain `parity` is squatted by an
> empty project. The command you run and the module you import are both
> `parity`.

Python 3.10+. The core has no dependencies; drivers are optional extras and are
imported lazily, so a DuckDB-only user is never made to install `psycopg`.

## Usage

```
parity diff --a CONN --a-table TABLE --b CONN --b-table TABLE --key COL[,COL...]
            [--columns a,b,c] [--exclude x,y]
            [--bisection-factor 32] [--threshold 10000] [--float-scale 6]
            [--max-diffs 10000] [--verify-sample 1000] [--json] [--quiet]
```

**Exit codes** — this is what makes it a CI check:

| Code | Meaning |
|---|---|
| `0` | identical |
| `1` | not proven identical: rows differ, a column exists on one side only, the checksums disagreed in a way the downloaded rows could not explain, or the independent sample check contradicted the checksums |
| `2` | error (bad connection string, missing table, unusable key) |

An error is never `1`. A CI job can always tell "the tables differ" from "the
tool could not run". A column added or dropped on purpose is a schema
difference until you say so: pass it to `--exclude` (or leave it out of
`--columns`) and it becomes a warning instead.

Connection strings:

```
postgres://user:password@host:port/database     (also postgresql://)
mysql://user:password@host:port/database
duckdb:///relative/path.duckdb                  (three slashes = relative)
duckdb:////var/lib/warehouse.duckdb             (four slashes = absolute)
duckdb:///C:/data/warehouse.duckdb              (absolute, Windows)
duckdb:///:memory:
snowflake://user:password@account/database/schema?warehouse=wh&role=r
bigquery://project/dataset                      (optional ?location=EU)
mssql://user:password@host:port/database        (also sqlserver://; port 1433)
mssql://user:password@host/database?instance=SQLEXPRESS&encrypt=strict
mysql://user:password@host/database?ssl_mode=VERIFY_IDENTITY&ssl_ca=/path/ca.pem
```

Connection errors never print the password. On MySQL a password may contain
`/ # ? :` unencoded but must spell `@` as `%40`; the URL takes the TLS options
`ssl_mode` (`DISABLED`, `PREFERRED`, `REQUIRED`, `VERIFY_CA`,
`VERIFY_IDENTITY`), `ssl_ca`, `ssl_cert`, `ssl_key`, `ssl_verify_cert`,
`ssl_verify_identity` and `ssl_disabled`, and refuses any other option rather
than ignore it. With `ssl_mode=REQUIRED` or stricter the run checks that the
session really is encrypted, since the driver would otherwise fall back to
plaintext without saying so. A BigQuery URL refuses user credentials outright.

Table names may be schema-qualified. Unqualified names mean `public` on
PostgreSQL and `main` on DuckDB — always that schema, never whatever the
session's `search_path` finds first — and on Snowflake the URL's schema (else
`PUBLIC`). PostgreSQL materialized views can be compared like tables. On
BigQuery they default to the dataset in the
connection string, and a table may also be given as `dataset.table` or
`project.dataset.table` (to read another project's data while running jobs in
your own).

BigQuery authenticates with Application Default Credentials — a service
account via `GOOGLE_APPLICATION_CREDENTIALS`, `gcloud auth application-default
login`, impersonation, or the VM's identity — so no secret goes in the
connection string. The credentials are requested with the read-only
`bigquery.readonly` scope, under which BigQuery itself refuses any write: the
tool cannot modify your project even by mistake. The account needs BigQuery Job
User on the project the jobs run in, and read access to the dataset.

SQL Server (2019 or newer, and Azure SQL Database / Managed Instance) uses
Microsoft's `mssql-python` driver, which needs no separately installed ODBC
driver. Unqualified tables default to the login's own default schema (normally
`dbo`). The connection string takes SQL authentication only so far, and a fixed
set of options: `encrypt` (`yes`, `no`, `strict`, `mandatory`, `optional`),
`trust_server_certificate` (`yes`/`no`), `application_intent`
(`readonly`/`readwrite`), `login_timeout` (seconds) and `instance` (a named
instance); anything else is refused rather than ignored, and every value is
quoted so a `;` in a password cannot add a keyword. Encryption is on by default
and the certificate is checked: use `trust_server_certificate=yes` only for a
local test server with a self-signed certificate. SQL Server has no read-only
session, so the tool issues only `SELECT`, refuses anything else before it
reaches the server, and never commits — but **use a login that holds only
`db_datareader`** (or `GRANT SELECT` on the tables), which makes the server
itself refuse a write.

`--verify-sample N` sets the size of the independent sample check (default
1,000 rows; `0` skips it). The rows are chosen by a hash of the key, fresh at
every run, so both sides pick the same rows and the sample spreads over the
whole table; the result records the choice (`modulus`, `residue`) so a run can
be repeated. Types outside the check's rules (JSON, binary, arrays and other
types the tool compares as plain text) are listed as not covered rather than
passed.

`--json` emits the same content as a machine-readable object, including
`identical`, `truncated`, `schema_differences`, `unexplained`, `verification`, per-difference
values, and the full stats block. Values in JSON are the exact canonical text
the engines compared, in which a backslash is doubled and an ASCII Unit
Separator (chr 31) is written `\s`; the human report shows them decoded, with
text quoted and a real NULL printed bare as `NULL`.

### In CI

```yaml
- name: prove the migration is complete
  run: |
    parity diff \
      --a "$LEGACY_URL"     --a-table public.orders \
      --b "$WAREHOUSE_URL"  --b-table analytics.orders \
      --key order_id --quiet
```

## Using it from Python

The CLI is a thin wrapper. `diff` returns the whole result, so you can act on
it rather than parse output. Importing `parity` pulls in no database driver —
they load when `get_dialect` needs one.

```python
from parity import diff, get_dialect

with get_dialect("duckdb:///old.duckdb", side="A") as a, \
     get_dialect("duckdb:///new.duckdb", side="B") as b:
    result = diff(a, b, "main.orders", "main.orders", key="id")

if result.identical:
    print("the tables match")
else:
    for d in result.diffs:
        print(d.kind, d.key, d.columns, d.values_a, d.values_b)

print(f"{result.stats.rows_downloaded} rows crossed the network")
print(f"truncated: {result.truncated}")
```

```
different 2 ['status'] {'status': 'ok'} {'status': 'CHANGED'}
2 rows crossed the network
truncated: False
```

`diff` takes the same options as the CLI: `columns`, `exclude`,
`bisection_factor`, `threshold`, `max_diffs`. Pass `max_diffs=None` to lift the
10,000 default, and `float_scale` to `get_dialect` — both sides must agree or
the comparison is refused before it runs.

### What you get back

`DiffResult` carries:

| Attribute | |
|---|---|
| `identical` | `True` only if no row differed, no column exists on one side only, every checksum mismatch was explained by the rows downloaded, **and** the whole key space was walked. False whenever `truncated` is set. |
| `truncated` | The walk stopped early, so this is a partial answer. Never read a truncated result as "the rest matched". |
| `schema_differences` | Columns one side has and the other lacks, that you did not set aside with `columns` / `exclude`. Any entry makes the result not identical. |
| `unexplained` | Key ranges whose checksums differed although the rows downloaded from them compared equal (or a side returned a different row count than its checksum counted). The engines proved a difference the tool could not show you, so the result is not identical. |
| `verification` | The independent sample check: `modulus`/`residue` (which rows), `rows_a`/`rows_b`, `checked` and `unchecked` columns, `failures`, `notes`, `error`. Any failure makes the result not identical. `None` with `verify_sample=0`, or when the walk stopped early. |
| `diffs` | `RowDiff` objects in key order, each with `key`, `kind` (`only_in_a`, `only_in_b`, `different`), the `columns` that moved, and `values_a` / `values_b` as raw canonical text. |
| `columns` | The columns actually compared, after `columns` and `exclude`. |
| `warnings` | Everything the comparison decided on your behalf: columns set aside, types that differ between sides (key types included), timezone-awareness mismatches, engine caveats such as a missing snapshot. Worth surfacing. |
| `float_scale` | The rounding in force, so a caller can state it alongside the verdict. |
| `stats` | `queries`, `rows_downloaded`, `rows_compared_a`, `rows_compared_b`, `segments_checked`, `seconds`, and the sample check's own `sample_queries` and `rows_sampled` (kept apart, so the walk's figures are unchanged). |

`diff` raises `ValueError` for anything it refuses — a FLOAT key, a non-unique
or NULL key, a missing table, mismatched float scales — and for any database
error during the walk, which arrives as a `ValueError` naming the side and the
table, with the driver's own exception as its `__cause__`. The message always
names which side. The caller owns the two dialects: close them (or use `with`,
as above) even when `diff` raises, or a PostgreSQL side keeps its snapshot
transaction open.

## Risk figures — what a run can state about itself

[`docs/risk.md`](docs/risk.md) states the error odds of every step of a
comparison, in summary and in detail, and is kept current with the code.

Every report ends its sample-check line with two numbers. They are computed,
not estimated, under the assumptions stated with each:

| Figure | What it bounds | Value | Assumption |
|---|---|---|---|
| Checksum collision | The chance that a key range holding a real difference still sums to the other side's checksum | at most 2⁻⁶⁰ ≈ 8.7 × 10⁻¹⁹ per differing range | the data was not crafted to collide (see the tamper note below) |
| Hidden rows | With *n* sampled rows and no disagreement, the share of rows that could still carry a difference both the checksums and the sample missed | below 1 − 0.01^(1/n) at 99% confidence; **0** when every row was compared | rows enter the sample at random (fresh residue each run, keys spread evenly by MD5) |

The hidden-row bound by sample size, at 99% confidence:

| `--verify-sample` | Share of rows that could hide an error | On 10M rows, at most |
|---|---|---|
| 1,000 (default) | 0.46% | 46,000 |
| 10,000 | 0.046% | 4,600 |
| 100,000 | 0.0046% | 460 |
| 1,000,000 | 0.00046% | 46 |
| ≥ table size | 0 (every row compared twice) | 0 |

A larger sample costs one more pass over the key and the download of the
sampled rows: on 1M rows PostgreSQL ↔ DuckDB, 3.5 s at 1,000 and 5.2 s at
100,000. `--json` carries the same figures under `risk`. They cover the
columns listed as `covered_columns`; a column of a type outside the rules
(`uncovered_columns`) carries no bound. Nothing here bounds what neither
method can see: a table changing during a run on an engine without a single
snapshot (the run's caveats say which), or a difference the documented rules
ignore by design - digits past `--float-scale`, sub-microsecond digits against
an engine that holds microseconds, CHAR padding - which both methods follow.

## Limitations — read these before trusting a result

A parity tool that reports a false match is worse than useless, so these are
stated plainly rather than buried.

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
  engine. A NULL in any key column is refused, as for an integer key. When the
  key's type differs between the sides in a way that cannot match (integer
  against float, a timezone-aware against a naive timestamp), the run warns
  that every row will read as missing on one side.
- **Text is compared exactly, including the characters the tool uses
  internally.** Values are joined with ASCII Unit Separator and NULL is
  spelled `\N`, so text is escaped first (`\` → `\\`, chr(31) → `\s`): the
  string `\N` never equals NULL and a separator inside a value cannot shift
  where a column ends. Text without either character renders exactly as
  before.
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
- **Snowflake needs connector 4.x** (`snowflake-connector-python>=4.0,<5`,
  which the `[snowflake]` extra installs). Connector 5.x flushes a telemetry
  batch to Snowflake at every logout that no setting stops, and the tool
  sends no telemetry. With 4.x it switches the connector's telemetry and its
  cloud-metadata probes (`SNOWFLAKE_DISABLE_PLATFORM_DETECTION`) off, and a
  live test checks that a session contacts only the account's own login,
  query and logout endpoints. The login itself names the driver, Python and
  OS versions, as every Snowflake client's does.
- **Keys must be unique.** A non-unique key is detected up front and rejected.
  Silently collapsing duplicate rows would hide real differences.
- **`Infinity` and `NaN`** in float columns are compared as those literal
  tokens on both sides. Infinite dates and timestamps (PostgreSQL's and
  DuckDB's `infinity`, common as a "valid until forever" end date) compare as
  `infinity` / `-infinity`, never as NULL. BC dates use astronomical year
  numbering (1 BC is year `0000`, 44 BC is `-43`), as DuckDB prints them.
- **Wide tables are fine.** PostgreSQL caps a function call at 100 arguments,
  so the row concatenation is built as a nested tree; tested to 500 columns.
  Long rows are fine too: SQL Server's `CONCAT_WS` silently cuts its result at
  4,000 characters unless an argument is `nvarchar(max)`, so every value is
  joined as one — tested with a difference planted at character 9,500.
- **Supported types:** integer, decimal, float, boolean, string, date,
  timestamp. Anything else is compared as raw text and reported as a warning —
  two engines may render the same JSON or array differently for reasons that
  have nothing to do with the data. A few such types are rendered exactly
  rather than lossily: MySQL binary columns as `\x` + hex (PostgreSQL's
  `bytea` form) and `bit(n)` as its bits; SQL Server `sql_variant`, DuckDB
  `UNION`/`VARIANT` and Snowflake `VARIANT` with their type tag, so `1` and
  `'1'` differ. DuckDB `TIMESTAMP_NS` keeps its nanoseconds when it has any.
- **A row wider than MySQL's `max_allowed_packet`** (64 MiB by default) is
  hashed column by column instead of as one string, so it can never drop out
  of a checksum; against another engine such a row is downloaded and compared
  value by value.
- **At most 10,000 differences are reported by default.** Each one costs about
  715 bytes, so two tables that share nothing would need gigabytes rather than
  producing an answer - and pointing the tool at the wrong table or the wrong
  environment is exactly what it exists to catch. Past the limit the run is
  flagged `truncated`, `identical` is never true, and the output says "at
  least N". It does not mean the rest matched. `--max-diffs 0` lifts the limit.
- **Timezone-aware timestamps are compared as instants, in UTC.** Both
  sessions are pinned to UTC on connect, so the same instant matches whatever
  the two servers' default timezones are. Without that pin, two sides in
  different zones render every `timestamptz` differently and the tool reports
  the entire table as changed.
- **A column present on only one side is a schema difference**: the run is
  not identical and exits `1`, even if every row matches on the columns both
  sides share. `--exclude` it if the change was intended.
- **A live source table is read at one snapshot on PostgreSQL, MySQL and
  BigQuery.** PostgreSQL and MySQL run the whole diff in one REPEATABLE READ,
  read-only transaction. BigQuery has no cross-query transaction, so every
  query reads a base table `FOR SYSTEM_TIME AS OF` the instant of the first
  one. **A BigQuery view is not pinned** — BigQuery silently ignores time
  travel on views — and neither is a MySQL table on a non-transactional engine
  (MyISAM, MEMORY); like every table on Snowflake (READ COMMITTED only), such a
  source changing during the run can produce differences that never existed
  at one point in time. **Every run says so** when it applies. Diff the
  underlying table, or re-run, if the source is busy.
- **Session settings cannot skew the result.** PostgreSQL pins the time zone,
  float digits, `standard_conforming_strings`, `DateStyle`, `IntervalStyle`,
  `bytea_output`, `lc_monetary`, the client encoding and `search_path` (so a
  user function cannot shadow a built-in), and turns off the idle and
  transaction timeouts that would otherwise kill the snapshot mid-run. MySQL
  pins the time zone and `sql_mode`.
- **The checksum detects accidents, not tampering.** Row hashes are summed per
  key range, and the hash is public, so someone who can write rows on one side
  could craft several changes whose hashes cancel and read as identical. By
  accident this happens with odds of about 1 in 2^60 per differing range.
  Treat a clean run as proof that a migration lost or changed nothing, not as
  proof against a deliberate adversary.
- **Interrupting a BigQuery run waits for the in-flight query.** Cancelling a
  BigQuery job needs a write scope, and read-only was judged worth more.
  The same is true of SQL Server, whose driver has no way to cancel a query.
- **SQL Server reads one snapshot only if the database allows it.** With
  `ALLOW_SNAPSHOT_ISOLATION ON` the diff runs in one SNAPSHOT transaction, like
  PostgreSQL. Without it the run stays at READ COMMITTED and **every run prints
  a warning** that it was not single-snapshot; it never falls back to
  REPEATABLE READ, whose locks would block writers on a live system for the
  whole diff, or to NOLOCK, whose dirty reads invent differences. A table
  created or altered while a snapshot diff runs makes it stop with SQL Server's
  error 3961 rather than read inconsistent data.
- **SQL Server `datetime2(7)` and `datetimeoffset(7)` are compared at
  microseconds**, the precision the other engines hold: the seventh digit (100
  ns) is truncated, never rounded, so `23:59:59.9999995` stays on its day.
  Every run comparing such a column says so. As a *key* such a column is
  compared exactly. `datetime` (1/300 s ticks)
  compares as its displayed milliseconds, `.003`/`.007`, which is what tools
  export; a target that stored the exact tick (`.003333`) will differ.
  `datetimeoffset` is compared as an instant in UTC.
- **SQL Server `char(n)`/`nchar(n)` are compared without their trailing
  padding**, as PostgreSQL's `char(n)` and MySQL's `CHAR` are: a `char(10)`
  holding `abc` matches `abc` on the other side. Nothing is lost, since the
  column cannot tell `abc` from `abc ` itself. GUIDs compare in lower case.
- **SQL Server's own FLOAT rendering** (`float`, `real`) matches the canonical
  form on the same 1,565-value sample and 10,000 random doubles, at scales 0–18;
  `--float-scale` above 18 is refused for FLOAT columns there.
- **Microsoft Fabric and Azure Synapse are not supported yet** and are refused
  at connect, naming the engine edition: both isolate differently (Fabric
  ignores `SET TRANSACTION ISOLATION LEVEL`; Synapse dedicated pools read
  uncommitted by default), and neither has been verified.

## Supported engines

| Engine | Status |
|---|---|
| PostgreSQL | supported (tested against 16 and 18) |
| DuckDB | supported (tested against 1.5) |
| MySQL | supported (tested against 8.0) |
| Snowflake | supported (verified live on AWS; `pip install "parity-diff[snowflake]"`) |
| BigQuery | supported (verified live in the EU region; `pip install "parity-diff[bigquery]"`) |
| SQL Server / Azure SQL | supported (SQL Server 2019+; verified live against 2022; `pip install "parity-diff[mssql]"`) |
| Redshift, Databricks, Fabric | not yet — see `CONTRIBUTING.md`, a dialect is ~80 lines |

## Scope

In scope: proving two tables match, finding exactly which rows and columns
don't, doing it cheaply on large tables, running in CI.

Deliberately out of scope: data quality rules, freshness checks, anomaly
detection, lineage, cataloguing, orchestration, transformation, a web UI, a
server, schema migration. If a feature does not help someone answer *"can I
safely switch off the old system?"*, it does not belong here.

## Development

```bash
python -m venv .venv
.venv/Scripts/Activate.ps1        # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[all]" pytest
pytest
```

Tests that need PostgreSQL read `PARITY_TEST_PG` (default
`postgres://parity:parity@127.0.0.1:5432/parity`) and **skip cleanly** when no
server is reachable — the DuckDB and pure-Python suites still run. Snowflake
and BigQuery have no local or CI instance, so their live suites run only when
`PARITY_TEST_SNOWFLAKE` / `PARITY_TEST_BIGQUERY`
(`bigquery://<project>/<dataset>`) point at a real account; the BigQuery tests
create and drop their own tables in that dataset. SQL Server's live suite runs
when `PARITY_TEST_MSSQL` names a login that may create databases — the
Developer-edition container works; see the top of `tests/test_mssql.py` — and
`PARITY_TEST_MSSQL_RO`, if set, adds the read-only-login checks.

A disposable PostgreSQL:

```bash
docker run -d --name parity-pg -p 55432:5432 \
  -e POSTGRES_USER=parity -e POSTGRES_PASSWORD=parity -e POSTGRES_DB=parity \
  postgres:16-alpine
export PARITY_TEST_PG="postgres://parity:parity@127.0.0.1:55432/parity"
```

Reproduce the benchmark:

```bash
python demo/generate.py --rows 10000000
python demo/benchmark.py --expect-clean
python demo/generate.py --rows 10000000 --plant
python demo/benchmark.py --expect-planted
```

`CLAUDE.md` holds the verified cross-engine SQL and why each expression is the
way it is. `ROADMAP.md` is what is done and what comes next.

## Changelog

Latest release **0.4.0** — SQL Server support, and the fixes from a red-team
campaign that found inputs on which earlier versions reported identical tables
that were not (a text value `\N` against NULL, a separator character inside
text, a column dropped on one side, PostgreSQL `infinity` dates, MySQL binary
columns, rounded DECIMAL keys, and more - listed in `CHANGELOG.md`). **Upgrade
from any earlier version** and re-run comparisons that touched those cases.
Exit code `1` now also means a column exists on one side only. 0.3.0 added
BigQuery and fixed two float/decimal false matches; 0.2.3 hardened the identical
check, 0.2.2 added Snowflake. See `CHANGELOG.md` for the full history.

## License

MIT — see `LICENSE`.
