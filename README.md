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
✗ 3 differences in 100,000 rows
  10 queries · 18,749 rows downloaded (9.37% of both tables) · 0.6s
  1 only in A · 2 different

  ✓ independent sample check: 10,044 sampled rows (1 in 10) re-compared from raw values - all agree
    risk: with 99.9% confidence fewer than 0.069% of rows (at most 69 of 100,000) could differ in a way the checksums missed; a checksum collision is below 1 in 10^18
  comparing floats and decimals at 6 decimal places

  only in A   key 99999
  different   key 7              columns: status
      status   A NULL   B ''
  different   key 4242           columns: amount
      amount   A 5302.500000   B 5302.510000
```

Exit code `1`. Put it in CI and the build fails until the data agrees.

**Documentation:** [reading the output][output] ·
[troubleshooting][troubleshooting] · [engines, logins and permissions][engines] ·
[limitations][limitations] · [error odds][risk] · [type coverage][coverage]

## How it works

`parity` pushes hash aggregation **down into both engines**. It asks each side
for one checksum per bucket of the key range, compares a handful of integers,
recurses only into the buckets that disagree, and downloads rows only from
ranges already proven to differ. On identical tables it downloads **zero
rows** in **four queries**, whether the table has ten thousand rows or ten
million.

Then it **checks itself**. The checksums are only as right as the SQL that
renders each value to text inside each engine, and a mistake there can make
two different values look alike on both sides — the way every false match
this tool has ever had worked. So after the walk, about 10,000 rows (all of
them, for a table that small) are fetched again from both sides as the
database drivers return them, and compared in plain Python by the documented
rules, without any of that SQL. If the two methods disagree about a single
row, the result is **not identical** and the report names the row. Cost: one
extra pass per side that hashes the key only, plus the sample.

## Install

```bash
pip install "parity-diff[all]"        # every engine
pip install "parity-diff[duckdb]"     # DuckDB only — no other driver pulled in
pip install "parity-diff[postgres]"   # PostgreSQL only
pip install "parity-diff[mysql]"      # MySQL only
pip install "parity-diff[snowflake]"  # Snowflake only
pip install "parity-diff[bigquery]"   # BigQuery only
pip install "parity-diff[mssql]"      # SQL Server / Azure SQL only
pip install "parity-diff[oracle]"     # Oracle only (thin mode: no Oracle client needed)
```

> The PyPI distribution is `parity-diff` — plain `parity` is squatted by an
> empty project. The command you run and the module you import are both
> `parity`.

Python 3.10+. The core has no dependencies; drivers are optional extras and are
imported lazily, so a DuckDB-only user is never made to install `psycopg`.

## Try it in five minutes

No database server needed: two DuckDB files stand in for the legacy system and
the new warehouse.

```bash
pip install "parity-diff[duckdb]"
```

Save this as `make_tables.py` and run `python make_tables.py`. It builds the
same 100,000-row table twice, then plants three differences in the "migrated"
copy — a changed amount, a NULL turned into an empty string, a lost row:

```python
import duckdb

for path in ("old.duckdb", "new.duckdb"):
    con = duckdb.connect(path)
    con.execute("drop table if exists orders")
    con.execute("""
        create table orders as
        select i                                              as order_id,
               round(i * 1.25, 2)::decimal(12, 2)             as amount,
               case when i % 7 = 0 then null else 'shipped' end as status,
               date '2024-01-01' + (i % 365)::int             as ordered_on
        from range(1, 100001) t(i)
    """)
    con.close()

# Plant three differences in the "migrated" copy.
con = duckdb.connect("new.duckdb")
con.execute("update orders set amount = amount + 0.01 where order_id = 4242")
con.execute("update orders set status = '' where order_id = 7")  # was NULL
con.execute("delete from orders where order_id = 99999")
con.close()
print("wrote old.duckdb and new.duckdb")
```

Now compare them:

```bash
parity diff --a duckdb:///old.duckdb --a-table orders \
            --b duckdb:///new.duckdb --b-table orders --key order_id
echo "exit code: $?"
```

You get the report at the top of this page — all three differences, exit
code `1`. Only the sample line varies a little between runs (the sampled rows
are drawn afresh each time) and the time. Compare a table with itself
(`--b duckdb:///old.duckdb`) and you get `✓ no differences in 100,000 rows`,
`4 queries · 0 rows downloaded`, exit code `0`. Add `--json` to see the
machine-readable form.

## Using it on your databases

```
parity diff --a CONN --a-table TABLE --b CONN --b-table TABLE --key COL[,COL...]
            [--columns a,b,c] [--exclude x,y] [--map a_col=b_col[,...]]
            [--where SQL] [--a-where SQL] [--b-where SQL]
            [--bisection-factor 32] [--threshold 10000] [--float-scale 6]
            [--max-diffs 10000] [--verify-sample 10000] [--json] [--quiet]
```

| Option | |
|---|---|
| `--a`, `--b` | connection strings, one per side (below) |
| `--a-table`, `--b-table` | the table on each side; `schema.table`, or unqualified for the engine's default schema. Matched exactly, including case |
| `--key` | the column(s) that identify a row, on both sides (matched case-insensitively). One integer column is bisected directly; a uuid, a text key or several columns are hashed. Must be unique and non-NULL |
| `--columns` / `--exclude` | compare only these columns / skip these. Default: every column both sides share |
| `--map` | pair a renamed column: `a_col=b_col` (side A's name = side B's); see *Renamed columns* below |
| `--where` / `--a-where` / `--b-where` | compare only the rows a predicate selects, on both sides or on one; the verdict then covers those rows only (see *Comparing a slice* below) |
| `--float-scale` | decimal places at which floats and decimals are compared (default 6), always the same on both sides |
| `--max-diffs` | stop after N differences (default 10,000; `0` for no limit). The result is then marked partial |
| `--verify-sample` | rows for the independent sample check (default 10,000; `0` skips it, and the report says so). A check that cannot run makes the result not identical |
| `--bisection-factor`, `--threshold` | buckets per level (32), and the range size at which rows are downloaded instead of bisected further (10,000) |
| `--json` / `--quiet` | machine-readable output / print nothing and rely on the exit code |

Connection strings:

```
postgres://user:password@host:port/database     (also postgresql://)
mysql://user:password@host:port/database
duckdb:///relative/path.duckdb                  (three slashes = relative)
duckdb:////var/lib/warehouse.duckdb             (four slashes = absolute)
duckdb:///C:/data/warehouse.duckdb              (absolute, Windows)
snowflake://user:password@account/database/schema?warehouse=wh&role=r
snowflake://user@account/database/schema?private_key_file=/path/key.p8   (key pair)
bigquery://project/dataset                      (optional ?location=EU)
mssql://user:password@host:port/database        (also sqlserver://; port 1433)
oracle://user:password@host:port/service_name   (port 1521; a service name, not a SID)
```

Each engine's options, authentication, the minimum grants for a read-only
login, and what a run costs on a cloud warehouse are in **[engines.md][engines]**.
Keep passwords out of shell history and logs by passing the URL through an
environment variable — `--a "$LEGACY_URL"` — as described
[there][passwords].

**Exit codes** — this is what makes it a CI check:

| Code | Meaning |
|---|---|
| `0` | identical |
| `1` | not proven identical: rows differ, a column exists on one side only, the checksums disagreed in a way the downloaded rows could not explain, the independent sample check contradicted the checksums or could not run, or the run stopped at `--max-diffs` |
| `2` | error (bad connection string, missing table, unusable key, a refused value) |

An error is never `1`. A CI job can always tell "the tables differ" from "the
tool could not run". A column added or dropped on purpose is a schema
difference until you say so: pass it to `--exclude` (or leave it out of
`--columns`) and it becomes a warning instead.

### Comparing a slice: `--where`

`--where SQL` compares only the rows matching a predicate, on both sides;
`--a-where` / `--b-where` give one side its own predicate and replace
`--where` there. Each is that engine's own SQL over that side's own column
names (a column renamed with `--map` keeps its side B name here). Use it to check last week, or one
partition, without paying for a full pass over a huge BigQuery or Snowflake
table:

```
parity diff ... --key order_id --where "created_at >= '2026-10-01'"
```

The predicate is pasted, in parentheses and ANDed with the tool's own
conditions, into every query that reads the table: the row count and key
checks, every checksum level, every download and the sample check. **A
filtered verdict is about the filtered rows only**: the headline says
`(filtered rows only)`, a `filtered: A where ...; B where ...` line follows,
JSON carries `where_a`/`where_b`, and the risk figures count filtered rows.
Each query evaluates the predicate afresh, so prefer fixed literals: an
expression that changes during the run (`random()`, `clock_timestamp()`,
or `now()` / `current_date` on an engine without one snapshot) can select
different rows in different queries, which the run reports as unverified.
Sessions are pinned to UTC, so a literal compared with a timezone-aware
column is read as UTC; on PostgreSQL `search_path` is pinned to
`pg_catalog`, so qualify any table a subquery names.

It is your own SQL against your own database, so this is not a security
boundary, but it must stay one read: a predicate containing `;`, `--`, `/*`,
`#`, a backslash before a quote, a triple- or dollar-quoted string, or
unbalanced quotes or parentheses is refused before any query (on SQL Server
also `"` and the words the dialect's SELECT guard refuses). The sessions are
read-only besides. From Python: `diff(..., where_a=..., where_b=...)`.

### Renamed columns: `--map`

`--map a_col=b_col` pairs a column the migration renamed: side B's `b_col`
is compared with side A's `a_col`. Repeat it, or comma-separate pairs
(`--map customer_name=cust_nm,amount=amt`). It works for key columns too
(`--key id --map id=order_id`). `--key`, `--columns` and `--exclude` take
side A's names, differences are reported under them, and the report names
both sides (`customer_name (B: cust_nm)`; JSON `column_map` and per-row
`b_columns`). Matching stays case-insensitive. A mapping that would pair a
column twice — two A columns onto one B column, or a B column the map claims
while a same-named column still matches it by name — is refused, as is a
mapped name absent on its side (with near misses listed) and a column both
mapped and excluded. From Python: `diff(..., column_map={"a_col": "b_col"})`.

## Checking a whole migration: `parity run`

A migration is rarely one table. List the pairs once in a config file and
check all of them with one command and one summary:

```toml
# migration.toml
[connections]
legacy    = "${LEGACY_URL}"                       # read from the environment
warehouse = "postgres://checker:${WH_PW}@wh-host/analytics"
local     = "duckdb:///./new.duckdb"

[defaults]                 # optional; the same defaults as `parity diff`
float_scale   = 6
verify_sample = 10000
max_diffs     = 10000      # 0 = no limit
# bisection_factor = 32, threshold = 10000

[[pair]]
name    = "orders"
a       = "legacy"
b       = "warehouse"
a_table = "public.orders"
b_table = "public.orders"
key     = "order_id"

[[pair]]
name    = "customers"
a       = "legacy"
b       = "warehouse"
table   = "public.customers"          # same name on both sides
key     = ["region", "customer_no"]   # or "region,customer_no"
map     = { customer_name = "cust_nm" }   # side A name = side B name
exclude = ["etl_loaded_at"]

[[pair]]
name        = "events-october"
a           = "legacy"
b           = "local"
a_table     = "public.events"
b_table     = "main.events"
key         = "event_id"
where       = "created_at >= '2026-10-01'"  # or a_where / b_where per side
float_scale = 2                             # any default can be overridden
```

```
parity run migration.toml
parity run migration.toml --only orders,customers
parity run migration.toml --fail-fast
parity run migration.toml --markdown signoff.md --junit parity.xml
parity run migration.toml --resume state.json      # skip what already passed
parity run migration.toml --json > run.json
```

```
✓ orders                     1,204,332 rows · no differences · 31.2s · scale 6
✗ customers                     88,410 rows · 2 differences · 3.1s · scale 6
      renamed columns (A = B): customer_name = B cust_nm
✓ events-october                10,882 rows · no differences (filtered rows only) · 0.9s · scale 2
    ! filtered rows only: A where created_at >= '2026-10-01'; B where created_at >= '2026-10-01'
    ! side A (postgres) is filtered by (...): ...

✗ 2 of 3 pairs identical, 1 differ, 0 errors
  ! 1 identical pair(s) carry qualifiers (filters, caveats, uncovered columns, no sample check): read their lines above
  risk, weakest identical pair (orders): with 99.9% confidence fewer than 0.069% of rows ...
  the differing rows are listed by --json and --markdown FILE
  35.4s · exit 1
```

- **Each pair is one `parity diff`** with the same rules, opened on two
  fresh connections and closed before the next pair, so no pair shares a
  snapshot or a session with another. Pairs run one after another.
- **The whole file is validated before anything connects**: an unknown field
  (with a near-miss suggestion), a duplicate name, a missing field, a wrong
  type, an unsafe row filter or an unset environment variable is refused,
  every problem at once, each naming the file, the pair and the field. A
  pair without `name` is called by its `a_table`.
- **`${NAME}`** in a connection string, as the whole value or inside it,
  reads an environment variable, so passwords stay out of the file. Nothing
  else is special (a `$` in a literal password stays a `$`). The expanded
  value is never printed: every error, warning, report and the resume file
  show `${NAME}` in its place.
- **A pair that errors is recorded and the run goes on** to the next one;
  `--fail-fast` stops at the first pair that is not identical or errors.
  **Ctrl-C** stops cleanly: the summary of the pairs that finished is still
  printed and every report is still written, marked incomplete.
- **Exit code**: `0` only if every selected pair is identical; `2` if any
  pair errored, the run was interrupted, or the config is invalid; otherwise
  `1` (a pair differs, or `--fail-fast` stopped after one).
- **The summary is never stronger than its weakest pair.** Every qualifier
  the single-pair report would print — filtered, truncated, unexplained,
  schema differences, a sample-check failure or error, uncovered columns,
  caveats — is printed under that pair in every format, and the run's own
  verdict is just "every pair identical", nothing more.
- **`--resume STATE.json`** records each finished pair as it completes. On a
  re-run with the same file, a pair that finished **identical** with an
  unchanged config entry (its settings, the tool version, and a salted hash
  of both connection strings as expanded) is not run again; it is listed as
  `from an earlier run (<timestamp>)` in every output. Every other pair runs.
- **`--markdown FILE`** writes a sign-off report (tool version, date, the
  config file's SHA-256, then per pair: verdict, tables, key, filters,
  renames, float scale, risk line, caveats, uncovered columns and up to 20
  differences); **`--junit FILE`** writes one test case per pair (a failure
  for differences, an error for an error); **`--json`** prints the whole run
  with each pair's full `parity diff --json` object. See
  [docs/output.md](docs/output.md#parity-run-the-summary).
- TOML needs Python 3.11+, or `pip install "parity-diff[toml]"` on 3.10;
  a `.json` file with the same structure works everywhere. Relative DuckDB
  paths are relative to the directory you run `parity` from, as with
  `parity diff`.

## Reading the output

The report leads with one verdict line, then the cost, then everything that
qualifies the verdict, then the rows:

- `✓ no differences in N rows` — identical (exit 0). Still read the lines
  under it: the float scale and any `!` caveat apply to it.
- `✗ N differences in N rows` — listed as `only in A`, `only in B`, or
  `different` with the changed columns and both values. A NULL prints bare as
  `NULL`, text is quoted, so `NULL` and `''` never look alike.
- `! at least N differences … stopped early` — the `--max-diffs` limit was hit;
  the rest of the table was **not** checked.
- `✗ schemas differ …`, `✗ not verified …`, `✗ not proven …` — a column
  exists on one side only, or the tool could not account for a checksum
  difference, was contradicted by its own sample check, or could not run that
  check. Never identical.
- `! …` lines — caveats: no single snapshot on this side, row-level security,
  sub-microsecond truncation, columns of a type outside the rules, a type
  that differs between the sides.

Every line, with what to do about it, and every JSON field:
**[docs/output.md][output]**. When something goes wrong — table not found, key
not unique, everything reading as different — see
**[docs/troubleshooting.md][troubleshooting]**.

## In CI

```yaml
- name: prove the migration is complete
  env:
    LEGACY_URL: ${{ secrets.LEGACY_URL }}
    WAREHOUSE_URL: ${{ secrets.WAREHOUSE_URL }}
  run: |
    pip install "parity-diff[postgres,snowflake]"
    parity diff \
      --a "$LEGACY_URL"     --a-table public.orders \
      --b "$WAREHOUSE_URL"  --b-table ANALYTICS.ORDERS \
      --key order_id --quiet
```

The step fails with exit code `1` while the tables differ and `2` if the tool
could not run. Drop `--quiet` to keep the report in the log, or use `--json`
and keep it as an artifact.

## Using it from Python

The CLI is a thin wrapper. `diff` returns the whole result, so you can act on
it rather than parse output. Importing `parity` pulls in no database driver —
they load when `get_dialect` needs one.

```python
from parity import diff, get_dialect

with get_dialect("duckdb:///old.duckdb", side="A") as a, \
     get_dialect("duckdb:///new.duckdb", side="B") as b:
    result = diff(a, b, "main.orders", "main.orders", key="order_id")

if result.identical:
    print("the tables match")
else:
    for d in result.diffs:
        print(d.kind, d.key, d.columns, d.values_a, d.values_b)

print(f"{result.stats.rows_downloaded} rows crossed the network")
print(f"truncated: {result.truncated}")
```

On the quickstart tables:

```
different 7 ['status'] {'status': '\\N'} {'status': ''}
different 4242 ['amount'] {'amount': '5302.500000'} {'amount': '5302.510000'}
only_in_a 99999 ['amount', 'ordered_on', 'status'] {'amount': '124998.750000', 'ordered_on': '2024-12-20', 'status': 'shipped'} {}
18749 rows crossed the network
truncated: False
```

`diff(a, b, a_table, b_table, key, columns=None, exclude=(),
bisection_factor=32, threshold=10_000, max_diffs=10_000, verify_sample=10_000)`
takes the same options as the CLI; pass `max_diffs=None` to lift the limit,
`verify_sample=0` to skip the sample check, and `float_scale=` to
`get_dialect` — both sides must agree or the comparison is refused before it
runs. Values are the raw canonical text the engines compared (`\N` is NULL).

`DiffResult` carries `identical`, `truncated`, `schema_differences`,
`unexplained`, `verification` (the sample check), `verify_sample`, `diffs` (`RowDiff` with
`key`, `kind`, `columns`, `values_a`, `values_b`), `columns`, `warnings`,
`float_scale` and `stats`. `identical` is `True` only if no row differed, no
column exists on one side only, every checksum mismatch was explained, the
sample check (unless `verify_sample=0`) ran and found no contradiction, **and**
the whole key space was walked.
The fields are described with their JSON counterparts in
[docs/output.md][output-json].

`diff` raises `ValueError` for anything it refuses — a FLOAT key, a non-unique
or NULL key, a missing table, mismatched float scales — and for any database
error during the walk, which arrives as a `ValueError` naming the side and the
table, with the driver's own exception as its `__cause__`. The caller owns the
two dialects: close them (or use `with`, as above) even when `diff` raises, or
a PostgreSQL side keeps its snapshot transaction open.

## How far a result can be trusted

Every passing run prints two computed figures in its `risk:` line (and in
JSON `risk`):

| Figure | What it bounds | Value |
|---|---|---|
| Checksum collision | that a key range holding a real difference still sums to the other side's checksum | at most 2⁻⁶⁰ ≈ 8.7 × 10⁻¹⁹ per differing range, for data not crafted to collide |
| Hidden rows | the share of rows that could carry a difference both the checksums and the sample missed | below 1 − 0.001^(1/n) at 99.9% confidence for n sampled rows — 0.069% at the default 10,000; **0** when every row was compared |

| `--verify-sample` | Share of rows that could hide an error | On 10M rows, at most |
|---|---|---|
| 1,000 | 0.69% | 68,840 |
| 10,000 (default) | 0.069% | 6,905 |
| 100,000 | 0.0069% | 691 |
| ≥ table size | 0 (every row compared twice) | 0 |

A larger sample costs one more pass over the key and the download of the
sampled rows: on 1M rows PostgreSQL ↔ DuckDB, 3.5 s at 1,000 and 5.2 s at
100,000. The rows are chosen by a hash of the key, fresh at every run, so both
sides pick the same rows and the sample spreads over the whole table; the
result records the choice (`modulus`, `residue`) so a run can be repeated.
The figures cover the columns the sample check covers (listed in JSON as
`covered_columns`); a column of a type outside the rules carries no bound.
Nothing bounds a table changing during a run on an engine without a single
snapshot, or a difference the rules ignore by design. **[docs/risk.md][risk]**
states the error odds of every step of a comparison and is kept current with
the code; it is what to show someone who asks how sure you are.

## Limitations you must know

These can change a verdict. Each run prints the ones that apply to it; the
full list, with every engine-specific detail, is
**[docs/limitations.md][limitations]**.

1. **Floats and decimals are compared at 6 decimal places** by default: values
   differing only in the 7th place are *equal*. Change it with
   `--float-scale`; the scale is printed on every run.
2. **Types outside the rules are compared as raw text** — BigQuery
   ARRAY/STRUCT/INTERVAL, nested lists, structs, ranges, geometry, XML. They
   are named in a warning, not covered by the sample check, and carry no risk
   figure.
3. **No single snapshot on Snowflake, BigQuery views, MySQL non-InnoDB tables,
   or SQL Server without `ALLOW_SNAPSHOT_ISOLATION`.** A source written during
   the run can show differences that never existed, or hide real ones. Every
   such run says so.
4. **What the reading login cannot see is not compared** — PostgreSQL
   row-level security is named in a caveat; diff as a `BYPASSRLS` role.
5. **The checksum detects accidents, not tampering.** Someone who can write
   rows could craft changes whose hashes cancel. Accidentally the odds are
   about 2⁻⁶⁰ per differing range.
6. **At most 10,000 differences are reported by default.** Past it the run is
   marked partial and says "at least N"; it does not mean the rest matched.
   `--max-diffs 0` lifts the limit.
7. **Keys must be unique and non-NULL**, and FLOAT keys are refused. Keys are
   compared exactly, never at the float scale.
8. **Sub-microsecond digits** (DuckDB `TIMESTAMP_NS`, SQL Server
   `datetime2(7)`, Snowflake `TIMESTAMP(9)`) are truncated, not compared,
   against an engine that holds only microseconds — with a caveat.

Also by design: timezone-aware timestamps compare as instants in UTC;
`char(n)` padding is ignored; NULL is never equal to `''` or to `false`; a
column on one side only is a schema difference (exit 1).

## Supported engines

| Engine | Status |
|---|---|
| PostgreSQL | supported: 9.6 or newer (12 or newer for FLOAT columns); every version 12-18 verified identical; the database must be UTF-8 or its text is hashed as UTF-8 |
| DuckDB | supported: 1.3 or newer (1.3-2.0 verified identical; older versions are refused, as they render some values ambiguously) |
| MySQL | supported: 8.0, 8.4 LTS and 9.x verified identical; 9.7+ needs `INSTALL COMPONENT 'file://component_classic_hashing'` (MD5) |
| Snowflake | supported (verified live on AWS); connector 4.x, password or key-pair login |
| BigQuery | supported (verified live in the EU region); Application Default Credentials |
| SQL Server / Azure SQL | supported (SQL Server 2019+; 2019 and 2022 verified identical; FLOAT columns need database compatibility level 130 or more); SQL authentication |
| Oracle | supported (19c+; 21c and 23ai verified identical; AL32UTF8 databases); password login; stores `''` as NULL, which the output says |
| Redshift, Databricks, Fabric | not yet — a new engine is one dialect file, see `CONTRIBUTING.md` |

Per-engine details: [engines.md][engines]. Which value of which type was
verified on which engine: [coverage.md][coverage].

## Measured

10,000,000 rows per side, PostgreSQL 18.4 ↔ DuckDB 1.5.5, on one developer
laptop (`demo/benchmark.py`), with the default 10,000-row sample check:

| Scenario | Queries | Rows downloaded | Sample check | Wall time |
|---|---|---|---|---|
| identical tables | **4** | **0** (0.0000%) | 2 queries, ~20,000 rows (both sides) | 61–68s |
| 5 planted differences | **28** | **7,628** (0.0381%) | 2 queries, ~20,000 rows | 94–101s |

**The query counts and row counts are exact and hardware-independent** —
properties of the algorithm, pinned by the test suite; reproduce them and you
should get the same integers. The sample's row count varies by a few percent
between runs, because the sampled rows are drawn afresh each time. The wall
times are a range over two or three runs on a laptop that was busy with other
work at the time, and they vary a lot with load (the same machine measured
25–31s and 48–55s before the sample check existed, on a quieter day); treat
them as an order of magnitude, not a specification.

The five planted differences — a changed decimal, a deleted row, an inserted
row at key 999,999,999, a `NULL` turned into `''`, and a `FALSE` turned into
`NULL` — are all found exactly, with no false positives.

Cost is roughly **one full hash pass per side**. It is CPU-bound on MD5, not
IO-bound on key lookup, which is why **an index on the key column makes no
measurable difference** (measured at 10M before the sample check existed:
38.8s without, 37.3s with).
Raising `--bisection-factor` does not speed up the dominant first pass; it
only reduces round trips on later levels.

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
(`bigquery://<project>/<dataset>`) point at a real account (Snowflake's
key-pair test additionally needs `PARITY_TEST_SNOWFLAKE_KEYPAIR`, a key-pair
URL); the BigQuery tests
create and drop their own tables in that dataset. SQL Server's live suite runs
when `PARITY_TEST_MSSQL` names a login that may create databases — the
Developer-edition container works; see the top of `tests/test_mssql.py` — and
`PARITY_TEST_MSSQL_RO`, if set, adds the read-only-login checks. Oracle's
runs when `PARITY_TEST_ORACLE` is a URL, or `1` for the Oracle Free container
described at the top of `tests/test_oracle.py`.

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

<!-- Absolute links: PyPI renders this README alone, without the docs/ folder. -->
[output]: https://github.com/Aleixiou/parity-diff/blob/main/docs/output.md
[output-json]: https://github.com/Aleixiou/parity-diff/blob/main/docs/output.md#json---json
[troubleshooting]: https://github.com/Aleixiou/parity-diff/blob/main/docs/troubleshooting.md
[engines]: https://github.com/Aleixiou/parity-diff/blob/main/docs/engines.md
[passwords]: https://github.com/Aleixiou/parity-diff/blob/main/docs/engines.md#keeping-passwords-out-of-logs
[limitations]: https://github.com/Aleixiou/parity-diff/blob/main/docs/limitations.md
[risk]: https://github.com/Aleixiou/parity-diff/blob/main/docs/risk.md
[coverage]: https://github.com/Aleixiou/parity-diff/blob/main/docs/coverage.md
