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
            [--max-diffs 10000] [--json] [--quiet]
```

**Exit codes** — this is what makes it a CI check:

| Code | Meaning |
|---|---|
| `0` | identical |
| `1` | differences found |
| `2` | error (bad connection string, missing table, unusable key) |

An error is never `1`. A CI job can always tell "the tables differ" from "the
tool could not run".

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
```

Table names may be schema-qualified. Unqualified names default to `public` on
PostgreSQL and `main` on DuckDB. On BigQuery they default to the dataset in the
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

`--json` emits the same content as a machine-readable object, including
`identical`, `truncated`, per-difference values, and the full stats block.

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

a = get_dialect("duckdb:///old.duckdb", side="A")
b = get_dialect("duckdb:///new.duckdb", side="B")
try:
    result = diff(a, b, "main.orders", "main.orders", key="id")
finally:
    a.close()
    b.close()

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
| `identical` | `True` only if nothing differed **and** the whole key space was walked. False whenever `truncated` is set. |
| `truncated` | The walk stopped early, so this is a partial answer. Never read a truncated result as "the rest matched". |
| `diffs` | `RowDiff` objects in key order, each with `key`, `kind` (`only_in_a`, `only_in_b`, `different`), the `columns` that moved, and `values_a` / `values_b` as raw canonical text. |
| `columns` | The columns actually compared, after `columns` and `exclude`. |
| `warnings` | Everything the comparison decided on your behalf: columns skipped, types that differ between sides, timezone-awareness mismatches. Worth surfacing. |
| `float_scale` | The rounding in force, so a caller can state it alongside the verdict. |
| `stats` | `queries`, `rows_downloaded`, `rows_compared_a`, `rows_compared_b`, `segments_checked`, `seconds`. |

`diff` raises `ValueError` for anything it refuses — a non-integer key, a
non-unique or NULL key, a missing table, mismatched float scales. The message
always names which side.

## Limitations — read these before trusting a result

A parity tool that reports a false match is worse than useless, so these are
stated plainly rather than buried.

- **Any key type, but non-integer keys are bucketed by a hash.** A single
  integer column is bisected directly. A `uuid`, a natural string key, or
  several columns together are hashed to 60 bits so the key space can be
  divided — and the run says so. Rows are still matched and reported by their
  real key, so a hash collision can only put two rows in the same bucket; it
  can never merge them.
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
- **Snowflake FLOAT columns are not verified** against that canonical form —
  no account was available when it was defined — and every run comparing one
  prints a warning saying so. Snowflake DECIMAL columns are unaffected.
- **Keys must be unique.** A non-unique key is detected up front and rejected.
  Silently collapsing duplicate rows would hide real differences.
- **`Infinity` and `NaN`** in float columns are compared as those literal
  tokens on both sides.
- **Wide tables are fine.** PostgreSQL caps a function call at 100 arguments,
  so the row concatenation is built as a nested tree; tested to 500 columns.
- **Supported types:** integer, decimal, float, boolean, string, date,
  timestamp. Anything else is compared as raw text and reported as a warning —
  two engines may render the same JSON or array differently for reasons that
  have nothing to do with the data.
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
- Columns present on only one side are skipped with a warning, not treated as
  differences.
- **A live source table is read at one snapshot on PostgreSQL, MySQL and
  BigQuery.** PostgreSQL and MySQL run the whole diff in one REPEATABLE READ
  transaction. BigQuery has no cross-query transaction, so every query reads a
  base table `FOR SYSTEM_TIME AS OF` the instant of the first one. **A BigQuery
  view is not pinned** — BigQuery silently ignores time travel on views — so,
  like every table on Snowflake (READ COMMITTED only), a view that changes
  during the run can produce differences that never existed at one point in
  time. Diff the underlying table, or re-run, if the source is busy.
- **Interrupting a BigQuery run waits for the in-flight query.** Cancelling a
  BigQuery job needs a write scope, and read-only was judged worth more.

## Supported engines

| Engine | Status |
|---|---|
| PostgreSQL | supported (tested against 16 and 18) |
| DuckDB | supported (tested against 1.5) |
| MySQL | supported (tested against 8.0) |
| Snowflake | supported (verified live on AWS; `pip install "parity-diff[snowflake]"`) |
| BigQuery | supported, unreleased (verified live in the EU region; `pip install "parity-diff[bigquery]"`) |
| Redshift, Databricks | not yet — see `CONTRIBUTING.md`, a dialect is ~80 lines |

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
create and drop their own tables in that dataset.

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

Latest release **0.2.3** — test-only: a dedicated campaign against the identical
check (offline logic, every live engine pair, all key types, edge values,
representation changes) that found no abnormality. 0.2.2 added Snowflake, and
0.2.1 the generative suite and the same-content insert-plus-delete fix it
surfaced. BigQuery support is in the `Unreleased` section. See `CHANGELOG.md`
for the full history.

## License

MIT — see `LICENSE`.
