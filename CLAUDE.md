# CLAUDE.md — project context for `parity`

> Read this file **and** `ROADMAP.md` before writing any code.
> This file is durable context: what the project is, what is already proven,
> and the rules you must not break. `ROADMAP.md` is status and direction.

---

## 1. What this is

`parity` proves that **two tables living in two different database engines hold
the same data** — without moving the data out of either engine.

You point it at a table on each side. It pushes hash aggregation down into both
engines, compares a handful of integers, binary-searches the key space to
isolate where they diverge, and downloads only the rows that actually differ.

```
parity diff \
  --a "postgres://user:pw@legacy-host/warehouse" --a-table public.orders \
  --b "duckdb:///./new.duckdb"                   --b-table main.orders \
  --key order_id
```

## 2. Why it exists (do not lose sight of this)

Data migrations do not fail on translation — SQLGlot and LLMs largely solved
turning old SQL into new SQL. They fail at **cutover**, because nobody can prove
the new pipeline produces the same data as the old one. So the legacy system
runs in parallel "just to be safe", forever, at double the cost.

- 25% of data teams name legacy systems as their single biggest bottleneck
- ~74% of legacy modernisation projects fail
- Datafold **sunset its open-source `data-diff` in May 2024** to push users to a
  paid cloud product, vacating the open-source position in a category with
  already-proven demand
- What remains is an unmaintained community fork, SQLMesh's `table_diff` (which
  requires adopting the entire SQLMesh framework), and pandas-scale tools that
  die on real warehouse volumes

The opening is a **standalone, framework-free, warehouse-native parity checker
that an individual engineer can `pip install` without asking procurement.**

## 3. Scope discipline — this is the thing most likely to kill the project

Datafold abandoned its open-source tool because maintaining it was expensive.
The only defence is a brutally narrow scope.

**In scope:** proving two tables match; finding exactly which rows and columns
don't; doing it fast and cheaply on large tables; running in CI.

**Out of scope — refuse these, even when they seem like small additions:**

- data quality rules, freshness checks, anomaly detection, "observability"
- lineage, cataloguing, orchestration, transformation
- a web UI, a server, a database of past runs
- schema migration or DDL generation
- anything that requires adopting a framework to use

If a feature does not help someone answer *"can I safely switch off the old
system?"*, it does not belong here.

## 4. Verified cross-engine facts

**These were empirically verified against DuckDB 1.5.5 and PostgreSQL 16.13.
Do not change any expression below without re-running the verification harness
(`tests/test_encoding.py`, Milestone 1) and confirming every case still agrees.**

### 4.1 The row hash

The foundation of the whole tool: fold canonical row text into an integer that
**every engine computes identically**.

| Engine | Expression |
|---|---|
| PostgreSQL | `('x' \|\| substr(md5(<text>), 1, 15))::bit(60)::bigint` |
| DuckDB | `cast(('0x' \|\| substr(md5(<text>), 1, 15)) as bigint)` |
| MySQL | `cast(conv(substr(md5(<text>), 1, 15), 16, 10) as unsigned)` |
| Snowflake | `floor(md5_number_upper64(<text>) / 16)` |

All four return `648541476951500027` for input `'abc'` - through four different
paths (bit-cast, hex-cast, CONV, and Snowflake's top-64-bits-then-drop-4), which
is exactly why a test pins the constant on each. Snowflake has neither a bit
cast nor `conv`, but `md5_number_upper64` gives the top 64 bits of the digest as
a number and `floor(.../16)` drops the low 4, leaving the same 60-bit prefix -
verified live, not just from the docs.

**Why 15 hex characters (60 bits) and not 16.** 60 bits is the widest MD5 prefix
that both engines render as the same *positive* signed 64-bit integer. At 16
characters PostgreSQL's `bit(64)::bigint` wraps to negative and the two engines
disagree. Do not widen this.

**Why the sum must be widened.** Row hashes reach 2^60. Summing millions of them
overflows a 64-bit accumulator, so each side must aggregate in a wider type:
PostgreSQL `sum(h::numeric)`, DuckDB `sum(cast(h as decimal(38,0)))`.

### 4.2 Canonical text encoding

Every column is rendered to text before hashing. Two rows are equal **iff** their
canonical text is byte-identical, so these expressions are the correctness
contract. All 17 cases below were verified to agree exactly.

| Logical type | DuckDB | PostgreSQL |
|---|---|---|
| INTEGER | `cast(c as varchar)` | `(c)::text` |
| DECIMAL | `cast(cast(c as decimal(38,6)) as varchar)` | `cast(round((c)::numeric, 6) as text)` |
| FLOAT (finite) | `cast(cast(c as decimal(38,6)) as varchar)` | `cast(round((c)::numeric, 6) as text)` |
| FLOAT (non-finite) | `case when isinf(c) … 'Infinity'/'-Infinity' when isnan(c) then 'NaN'` | `case when c = 'Infinity'::float8 … when c = 'NaN'::float8 then 'NaN'` |
| BOOLEAN | `case when c then 'true' when not c then 'false' end` | `case when c then 'true' when not c then 'false' end` |
| STRING | `cast(c as varchar)` | `(c)::text` |
| DATE | `strftime(c, '%Y-%m-%d')` | `to_char(c, 'YYYY-MM-DD')` |
| TIMESTAMP | `strftime(c, '%Y-%m-%d %H:%M:%S.%f')` | `to_char(c, 'YYYY-MM-DD HH24:MI:SS.US')` |

Verified agreeing values include: `1.5 → "1.500000"`, `-0.125 → "-0.125000"`,
`1/3 → "0.333333"`, `2024-02-29 13:04:05.123456` round-trips exactly,
`2024-01-01 00:00:00 → "2024-01-01 00:00:00.000000"`, and full Unicode strings.

**Infinity and NaN must be special-cased for FLOAT.** DuckDB cannot cast them
to DECIMAL at all — `Could not cast value inf to DECIMAL(38,6)` kills the whole
diff — while PostgreSQL renders them happily. Any division by zero produces one,
so this is ordinary data, not an exotic case. Both dialects now emit the fixed
tokens `Infinity`, `-Infinity`, `NaN`. Note PostgreSQL cannot detect NaN with
`c <> c`: it deliberately treats NaN as equal to itself, unlike IEEE 754, so the
test must be `c = 'NaN'::float8`. DECIMAL needs no guard — PostgreSQL `numeric`
renders NaN as text on its own and DuckDB `DECIMAL` cannot hold one.

**Still unhandled:** a double whose magnitude reaches 1e32 overflows DuckDB's
`decimal(38,6)` and raises. PostgreSQL's arbitrary-precision `numeric` does not.
Rare in warehouse data, and it fails loudly rather than silently.

**Known, deliberate limitation:** floats and decimals are compared at 6 decimal
places. Document this loudly in the README — silent precision assumptions are
exactly how a parity tool loses trust. Make it configurable later
(`--float-scale`), never silently variable.

### 4.3 NULL handling and field separation

- NULL renders as the literal sentinel `\N`, never SQL NULL:
  `coalesce(<expr>, '\N')`. An un-coalesced NULL would poison the whole
  concatenation and silently mask differences.
- **The BOOLEAN expression must use `when not c`, never `else`.** With
  `case when c then 'true' else 'false' end` a NULL boolean falls into the
  `else` branch and renders `'false'`, so the `coalesce` never fires and a NULL
  on one side compares *equal* to a FALSE on the other. Both engines agreed on
  that wrong answer, so a cross-engine equality test could not see it - it was
  caught only by a test that planted a NULL-versus-FALSE difference and
  asserted the tool found it. This is the same bug class as NULL versus `''`.
- Fields join with `concat_ws(chr(31), ...)` — ASCII Unit Separator, which
  effectively never occurs in warehouse string data and is spelled identically
  in both engines.

### 4.4 Engine type names — they do not look alike

`information_schema.columns` exists in both engines but reports wildly
different type names for the same column. Verified output for one identical
table:

| Column DDL | DuckDB reports | PostgreSQL reports |
|---|---|---|
| `bigint` | `BIGINT` | `bigint` |
| `integer` | `INTEGER` | `integer` |
| `decimal(12,2)` | `DECIMAL(12,2)` | `numeric` |
| `double precision` | `DOUBLE` | `double precision` |
| `varchar(20)` | `VARCHAR` | `character varying` |
| `text` | `VARCHAR` | `text` |
| `boolean` | `BOOLEAN` | `boolean` |
| `date` | `DATE` | `date` |
| `timestamp` | `TIMESTAMP` | `timestamp without time zone` |

So `map_type()` must lowercase, strip everything from the first `(`, and match
against a generous alias set — and `timestamp` must match by *prefix*, because
PostgreSQL appends `without time zone`. Default schema differs too: `public`
on PostgreSQL, `main` on DuckDB.

Both engines expose `information_schema.columns` identically enough that
`columns()` lives on the base class; a dialect only declares its
`default_schema`.

**Table lookup is exact and case-sensitive**, because the table name is quoted
into SQL and is a *per-side* argument the user gives for each engine — so it
must match that engine's stored case. Unquoted SQL folds to lower case on
PostgreSQL/DuckDB and to UPPER case on Snowflake, so `--a-table Orders` against
a table stored as `orders` is an easy mistake; the "table not found" error
therefore runs one case-insensitive follow-up query and names the near miss.

**Key and column *matching* is case-insensitive**, and this is not optional.
`--key` is one name applied to both sides, and the shared-column set is the
intersection of two schemas — but a Postgres table stores `id`, `amount` while
its Snowflake migration stores `ID`, `AMOUNT`, and no single `--key` value and
no exact-string intersection can bridge that. So `engine.py` folds every
identifier (`_fold_columns`, `str.casefold`) for *matching* only, while each
side keeps its own `Column` with its real stored name for *quoting* — the
lower-casing never reaches the SQL. Same-case schemas are unaffected (folding
is identity), so no existing checksum moves; a table carrying two columns that
differ only in case (possible only via quoted identifiers) is refused, because
it cannot be folded unambiguously. Differences are reported under side A's
stored spelling.

### 4.5 Portability traps already discovered

- **`/` is not portable.** PostgreSQL truncates on integer operands; DuckDB
  promotes to double. Integer division must go through a dialect method
  (`int_div`): PostgreSQL `div(a::numeric, b::numeric)`, DuckDB `(a // b)`. Do
  **not** work around this with `floor(a/b)` — double precision silently breaks
  on large key ranges. PostgreSQL's plain `/` is correct for two bigints but
  returns a *scaled, rounded* result once either operand is numeric, which is
  exactly the case below; `div()` is the exact integer quotient and truncates
  toward zero, matching DuckDB's `//` for the non-negative operands used here.
- **The bucket expression overflows int64 on wide key ranges.** It computes
  `(key - lo) * n_segments`, and `span * n_segments` passes 2^63 once the span
  is wider than about 2.9e17. That is ordinary for sparse bigint keys and
  guaranteed the moment keys are hashed into the full bigint range — which is
  what the planned non-integer-key support will do. Both engines raise rather
  than wrap (`bigint out of range` / `Overflow in multiplication of INT64`), so
  it surfaces as a crash and not a wrong answer, but the walk still dies on a
  legitimate key space. The key offset is therefore widened before the multiply
  via a dialect method (`wide_int`): PostgreSQL `::numeric`, DuckDB `hugeint`.
  Widening the *result* is not enough, and the first attempt at this fix got it
  wrong. Three separate overflows lurk in the bucket expression, and a key range
  as wide as bigint itself trips all three:

  1. `key - lo` is evaluated in the column's own type **before** any widening
     cast applies, so widen the key first: `wide_int(k) - lo`, never
     `wide_int(k - lo)`.
  2. `hi - lo` overflows if SQL is left to evaluate it. Compute the span in
     Python and emit it as one literal.
  3. `hi` is `max_key + 1`, which for a table holding the largest bigint is one
     past what the type can represent. Bound the range inclusively
     (`k <= hi - 1`) so every literal stays inside the column's own range.

  This is done **unconditionally**. A version that widened only when the span
  required it was measured at 10M rows and saved nothing — 39.3s against
  38.4s, inside run-to-run noise — because the cost is dominated by MD5 over
  every row. One always-correct path is worth two percent in the one function
  whose off-by-one would make the walker skip rows and still report a match.
- **`concat_ws` cannot take more than 99 columns on PostgreSQL.**
  `max_function_args` is 100 and is fixed at compile time, so a flat
  `concat_ws(sep, c1, ..., cN)` raises `cannot pass more than 100 arguments to
  a function` the moment a table has that many comparable columns — which a
  denormalised warehouse fact table routinely does. DuckDB accepted 150 without
  complaint, so the failure was *asymmetric*: the same table worked on one side
  and not the other. `row_text` therefore builds a **tree** of nested
  `concat_ws` calls, at most `MAX_CONCAT_ARGS` (64) values each.

  The nesting is **exact, not an approximation**. `concat_ws` joins its
  arguments with the separator and skips only NULLs, and every argument has
  already been through `coalesce`, so none is ever NULL. Therefore
  `concat_ws(s, concat_ws(s, a, b), c)` is byte-identical to
  `concat_ws(s, a, b, c)` — verified against both engines at 150 columns — and
  a table narrow enough to fit in one call renders exactly the SQL it always
  did, so no existing checksum moves.
- `md5()` returns the identical lowercase hex digest in both engines.
- `concat_ws`, `chr`, `coalesce`, `bit_xor` and ordered `string_agg` all exist
  in both, but prefer summing over XOR: **XOR silently cancels duplicate rows.**
- An empty segment returns SQL NULL from `sum()`, not 0. Both dialects must
  wrap the aggregate in `coalesce(..., 0)` or an empty bucket on one side
  compares unequal to an empty bucket on the other and the walker recurses
  into nothing forever.

### 4.6 Performance — measured, not assumed

Benchmarked at 2,000,000 rows per side, PostgreSQL 16 ↔ DuckDB, 2 vCPU:

```
identical tables      4 queries    0 rows downloaded            0.75s @ 200k
one changed row       8 queries    3,906 downloaded (0.195%)    6.5s  @ 2M
```

Re-measured at **10,000,000 rows per side**, PostgreSQL 18.4 (native, Windows)
↔ DuckDB 1.5.5, via `demo/benchmark.py`:

```
identical tables      4 queries        0 rows downloaded (0.0000%)   25-31s
5 planted diffs      28 queries    7,628 rows downloaded (0.0381%)   48-55s
```

The five planted differences are a changed decimal, a deleted row, an inserted
row at key 999,999,999, a NULL turned into `''`, and a FALSE turned into NULL.
All five are found exactly, with no false positives, while 0.0381% of the two
tables crosses the network.

**Query count and rows downloaded are exact and hardware-independent** — they
are properties of the algorithm, and the test suite pins them. The wall times
are the median of five runs on one developer laptop and drifted from 19.8s/38.4s
to ~25s/~49s across a long session on the same code paths, so treat them as an
order of magnitude. When a change looks like it cost time, A/B it in one sitting
rather than against a number measured hours earlier: the polling interval in
`_gather` looked like a 10% regression until an A/B in a single run put the
no-polling case squarely in the middle of the noise.

**An index on the key column makes no difference** — measured 6.54s without
versus 7.39s with at 2M, and 38.8s without versus 37.3s with at 10M: noise in
both directions. This is counter-intuitive and worth
understanding before anyone "optimises" it: the top-level query must hash
*every row* to produce a full-table checksum, so the cost is CPU-bound on MD5,
not IO-bound on key lookup. The index only helps the final small-range fetches,
which are already trivial.

Consequences for anyone tuning this:

- Total cost ≈ **one full hash pass per side**, plus a negligible tail. Deeper
  bisection levels only scan the surviving segment.
- Raising `--bisection-factor` does *not* speed up the dominant first level. It
  reduces round trips on later levels only.
- The real optimisations later are sampling mode, and pushing the comparison
  into a `WHERE` clause on a watermark column — not indexes.
- Both sides' queries are issued **in parallel** (two threads). The comparison
  is otherwise entirely IO-wait on two independent engines.

### 4.7 Behaviours verified end-to-end

A working reference implementation (see §5) was run against real DuckDB and
PostgreSQL instances. These are confirmed, and the test suite must keep them
confirmed:

- identical 200k-row tables → 0 differences, **0 rows downloaded**, 4 queries
- a changed decimal → reported as `different` on exactly the `amount` column
- a row deleted from one side → `only_in_b`
- a row inserted on one side → `only_in_a`
- **NULL versus empty string → reported as `different`** (`'\N'` vs `''`).
  This is the trap naive implementations fail, and a real migration bug class
- an inserted row with key `999999999` widened the key range from 200k to 1e9,
  and the walk still completed in 18 queries — sparse key spaces cost round
  trips, not scans
- `bucket_bounds()` was verified against the SQL bucket expression for every
  key across randomised `(lo, hi, n)` combinations

### 4.8 Session timezone — pin it to UTC or cry wolf on every row

`timestamptz` renders through the **session** timezone, and
`information_schema` reports it as `timestamp with time zone` on PostgreSQL and
`TIMESTAMP WITH TIME ZONE` on DuckDB, both of which `map_type` folds onto
TIMESTAMP by prefix. So the canonical text for one instant depends on where the
session happens to be.

Verified: two tables holding the identical instants `2024-06-15 12:00:00+00`
and `2024-01-15 12:00:00+00`, with side A's session in `America/New_York` and
side B's in `Asia/Tokyo`, rendered `08:00:00` against `21:00:00` and reported
**every row as different**. On a real table that is a false positive
indistinguishable from catastrophic data loss.

Both dialects therefore pin their session to UTC at connect:

| Engine | Statement |
|---|---|
| PostgreSQL | `set time zone 'UTC'` (while autocommit is still on, so it is a *session* setting and not one a rolled-back transaction discards) |
| DuckDB | `set TimeZone='UTC'` |

A `timestamptz` is an instant, so comparing instants is the correct semantics,
and UTC makes the rendering deterministic. Naive `timestamp` columns carry no
zone and are unaffected — verified against a database whose own default was set
to `Asia/Tokyo` while the other side sat in `Europe/Zurich`.

### 4.9 Transaction isolation — the source table is live

PostgreSQL's default READ COMMITTED gives **every statement its own snapshot**.
The walk issues one checksum query per level, so under the default the level-2
checksums describe a different table than the level-1 checksums did. A row
inserted mid-walk can be counted at one level and missing at the next, and the
tool then reports a difference that never existed at any single point in time.

This is not an edge case. During a migration the legacy side is still serving
traffic — a live source table is the normal case.

Verified: with a 1,000-row table, running `key_stats`, then inserting a row from
another connection, then running `key_stats` again returned 1,000 and then 1,001
from the same dialect object.

So the PostgreSQL dialect sets `isolation_level = REPEATABLE_READ` at connect
time, giving the entire diff one snapshot. DuckDB is opened read-only and needs
no equivalent.

Two consequences worth knowing:

- The snapshot is taken at the dialect's **first query**, not at connect. A
  long-lived `Dialect` object therefore only ever sees the database as of that
  first query — a table created afterwards is invisible to it. The CLI opens
  fresh connections per run, so this only bites library users and tests.
- The snapshot is held for the whole diff, which delays vacuuming dead tuples.
  At tens of seconds that is the cost of any analytical query, and far cheaper
  than a verdict nobody can trust.

### 4.10 Reported differences are bounded, because memory is

A `RowDiff` costs roughly **715 bytes**, measured, and the count is linear.
Two tables that share nothing therefore need about **8.5 GB at ten million
rows** — an out-of-memory kill rather than an answer. This was found by
accident: a benchmark run against a mismatched pair grew to 8 GB before it was
stopped.

Pointing the tool at the wrong table, the wrong schema or the wrong environment
is precisely the situation a parity check exists to catch, so it has to survive
doing so. `diff()` and the CLI therefore default `max_diffs` to
**10,000** (`DEFAULT_MAX_DIFFS`), which is roughly 7 MB. Past it the walk stops,
`truncated` is set, `identical` is never true, and the output reads "at least N
differences — stopped early".

`--max-diffs 0` on the CLI, or `max_diffs=None` in the library, lifts the limit
for anyone who genuinely wants every row and has the memory for it.

## 5. Architecture

The whole tool ships in `src/parity/` — dialects, engine and CLI — with 345
tests, `demo/proof.py` as the end-to-end proof, and `demo/benchmark.py` as the
10M-row one. Read the code before rewriting any of it: several decisions below
encode findings that were expensive to discover.

### Connection string grammar

```
postgres://user:password@host:port/database     (also postgresql://)
duckdb:///relative/path.duckdb                  (three slashes = relative)
duckdb:////var/lib/warehouse.duckdb             (four slashes = absolute, POSIX)
duckdb:///C:/data/warehouse.duckdb              (absolute, Windows)
duckdb:///:memory:
```

The slash count carries meaning, as in sqlite and SQLAlchemy: exactly **one**
leading slash is removed, the one separating the empty authority from the path.
Stripping them all quietly turns every absolute POSIX path into a relative one
and the tool then reports "database file not found" for a file that is plainly
there. Windows hides this completely, because its paths begin with a drive
letter and carry only one leading slash to start with.

The scheme before `://` selects the dialect in `get_dialect()`. Table names may
be schema-qualified (`public.orders`, `main.orders`); unqualified names default
to `public` on PostgreSQL and `main` on DuckDB.

```
src/parity/
  types.py                      # Column, TableRef, Segment, RowDiff, DiffResult
  dialects/
    base.py                     # Dialect ABC + get_dialect() + type mapping
    postgres_dialect.py
    duckdb_dialect.py
  engine.py                     # segmentation + recursive bisection (engine-agnostic)
  cli.py                        # argparse entry point, human + JSON output
tests/
  test_encoding.py              # cross-engine canonical encoding agreement
  test_engine.py                # bisection logic against fixtures
  test_integration.py           # real DuckDB <-> Postgres diffs
demo/
  generate.py                   # build N-row datasets with planted differences
  benchmark.py                  # timing + bytes-transferred proof
```

**The load-bearing separation:** dialects know *only* how to render and hash
values for one engine. The bisection algorithm knows *nothing* about any
engine. Adding Snowflake or BigQuery later must mean writing one new dialect
file and touching nothing else. If you find yourself adding an
engine-specific branch to `engine.py`, the abstraction is wrong — fix it there.

### The `Dialect` contract

```python
class Dialect(ABC):
    name: str
    def connect(self, connection_string: str) -> None: ...
    def close(self) -> None: ...
    def query(self, sql: str) -> list[tuple]: ...
    def columns(self, table: str) -> list[Column]: ...
    def quote(self, identifier: str) -> str: ...
    def normalize(self, column: Column) -> str: ...   # canonical text, null-safe
    def hash_expr(self, text_expr: str) -> str: ...   # -> 60-bit integer
    def int_div(self, num: str, den: str) -> str: ... # truncating division
    def sum_wide(self, expr: str) -> str: ...         # overflow-safe sum
```

Shared, non-abstract helpers on the base class build `concat_ws(...)` row text,
the per-segment checksum query, and the small-range row fetch — so a new
dialect is roughly 80 lines.

## 6. Engineering conventions

- **Python 3.10+**, standard library only for the core. `duckdb` and
  `psycopg[binary]` are **optional extras** (`pip install parity[postgres]`).
  A user who only has DuckDB must never be forced to install a Postgres driver.
- Type hints everywhere. `from __future__ import annotations` at the top of
  every module.
- Dataclasses for value types. No ORM, no SQLAlchemy — we emit dialect SQL
  deliberately, that is the product.
- No network calls, no telemetry, no phoning home. Ever. This tool points at
  production warehouses; trust is the entire distribution strategy.
- Read-only by construction: the tool issues `SELECT` only. Never generate
  DDL or DML against a user's database.
- Comments explain *why*, especially every cross-engine workaround — each one
  is a landmine for the next contributor.
- Errors must name the side and the table. `"table not found"` is useless when
  you are comparing two databases.

## 7. Local development environment (Windows)

DuckDB needs nothing. For PostgreSQL, either:

```powershell
# Option A - Docker Desktop (preferred, disposable)
docker run -d --name parity-pg -e POSTGRES_PASSWORD=parity -e POSTGRES_USER=parity `
  -e POSTGRES_DB=parity -p 55432:5432 postgres:16-alpine

# Option B - native installer from postgresql.org, then create the parity db
```

Connection strings used throughout the tests:

```
postgres://parity:parity@127.0.0.1:55432/parity
duckdb:///./demo/new.duckdb
```

Python setup:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[all]" pytest
```

## 8. How the project stays honest

Two rules that matter more than any feature:

1. **A parity tool that reports a false match is worse than useless.** Every
   test must include a *planted difference* and assert the tool finds exactly
   it — never only assert that identical tables match. It is trivially easy to
   write a diff tool that says "identical" for everything.
2. **Never claim a comparison is exact when it is approximate.** Float scale,
   unsupported types, and sampled modes must surface in the output, not just
   the docs.
