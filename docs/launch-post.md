# Both databases agreed. Both were wrong.

*Building a cross-engine data diff, and the ten bugs that only showed up because
every test had to plant a difference.*

---

Data migrations don't fail on translation. SQLGlot and LLMs largely solved
turning old SQL into new SQL. They fail at **cutover**, because nobody can prove
the new pipeline produces the same data as the old one. So the legacy system
runs in parallel "just to be safe", forever, at double the cost.

Datafold sunset its open-source `data-diff` in May 2024 to push people toward a
paid cloud product. What's left is an unmaintained fork, SQLMesh's `table_diff`
(which means adopting SQLMesh), and pandas-scale tools that die on real
warehouse volumes.

So I built [`parity`](https://github.com/Aleixiou/parity-diff): point it at a table
in each of two engines, and it tells you exactly which rows and columns differ —
without moving the data out of either one. PostgreSQL, DuckDB, MySQL, and Snowflake today.

```bash
pip install "parity-diff[all]"

parity diff \
  --a "postgres://user:pw@legacy-host/warehouse" --a-table public.orders \
  --b "duckdb:///./new.duckdb"                   --b-table main.orders \
  --key order_id
```

On 10,000,000 rows per side it proves two identical tables match in **4 queries
with zero rows downloaded**. With five differences planted, it finds exactly
those five in 28 queries, having moved 0.0381% of the data.

Those counts are properties of the algorithm, so you should reproduce them
exactly. The wall clock on my laptop is 25-31s and 48-55s respectively, and
that spread is my machine, not the tool.

That part is just engineering: hash each row inside the engine, compare one
checksum per bucket of the key range, recurse only into buckets that disagree,
download only from ranges already proven to differ. Roughly one full hash pass
per side.

The interesting part is what went wrong.

## The rule that found everything

There is one rule this project cares about more than any feature:

> **Every test must plant a difference and assert the tool finds exactly it.**
> Never only assert that identical tables match.

It sounds pedantic. It is not. It is *trivially easy* to write a diff tool that
says "identical" for everything, and a test suite that only compares matching
tables will pass for that tool with a green tick. A parity tool that reports a
false match is worse than useless — it doesn't just fail, it actively tells you
to switch off a system you shouldn't.

So every positive assertion ("these two agree") is paired with a **negative
control** ("and the harness notices when they genuinely don't").

Here is what that caught.

## 1. A NULL boolean compared equal to FALSE

Both engines render a boolean the same way:

```sql
case when c then 'true' else 'false' end
```

Look at it for a second. `when NULL` is not true, so a NULL boolean falls into
the `else` branch and renders `'false'` — identical to a real `FALSE`. The
`coalesce(..., '\N')` NULL sentinel wrapped around it never fires, because the
CASE already returned a non-NULL string.

I reproduced it end to end: side A has `NULL`, side B has `FALSE`, and the tool
reported **IDENTICAL**.

What makes this the most instructive bug in the project is *why* no ordinary
test could find it. **Both engines produced the same wrong answer.** Every
cross-engine agreement test passed, because they genuinely agreed. Only a test
that planted a NULL-versus-FALSE difference and demanded the tool find it could
possibly have caught it.

The fix is one clause:

```sql
case when c then 'true' when not c then 'false' end
```

Now NULL falls through to the sentinel. It's the same bug class as NULL versus
empty string — the trap this category of tool is supposed to be *good* at.

## 2. Timezones made every row look different

`timestamptz` renders through the **session** timezone. Two servers in different
zones turn the same instant into different text — so a table that had migrated
perfectly reported every single row as changed.

On a real table that is a false positive indistinguishable from catastrophic
data loss. It needs no misconfiguration at all, just two servers in two places,
which is the normal shape of a migration.

Both sessions are now pinned to UTC. A `timestamptz` is an instant; comparing
instants is the correct semantics.

## 3. The table changed underneath the walk

PostgreSQL's default READ COMMITTED gives **every statement its own snapshot**.
The bisection issues one checksum query per level, so the checksums at one level
describe a different table than the level below.

Verified: `key_stats` returned 1,000 rows, another connection inserted one, and
the same object then returned 1,001. The tool could report a difference that
never existed at any single point in time.

During a migration the source side is live *by definition*. This wasn't an edge
case; it was the normal case. Now REPEATABLE READ for the whole diff.

## 4. Windows hid a bug from me for 248 tests

`lstrip("/")` on the connection string stripped *every* leading slash, so
`duckdb:////var/lib/w.duckdb` — the standard four-slash spelling of an absolute
POSIX path — became the relative `var/lib/w.duckdb`. The tool then reported
"database file not found" for a file that was plainly there.

248 tests passed locally. Every demo passed. Then CI ran on Linux for the first
time and four of five jobs failed instantly.

Windows paths begin with a drive letter, so they carry only one leading slash to
start with. My entire dev environment was structurally incapable of seeing it.
That's the argument for a CI matrix in one paragraph.

## 5. Any table wider than 99 columns simply failed

PostgreSQL's `max_function_args` is 100 and fixed at compile time, so
`concat_ws(sep, c1, ..., cN)` raises *"cannot pass more than 100 arguments to a
function"* the moment a table has that many comparable columns. Denormalised
warehouse fact tables routinely do.

DuckDB accepted 150 without complaint, which made the failure **asymmetric** —
the same table worked on one side and not the other.

The concatenation is now a nested tree. That's exact rather than approximate:
`concat_ws` skips only NULLs, and every argument has already been through
`coalesce`, so `concat_ws(s, concat_ws(s, a, b), c)` is byte-identical to
`concat_ws(s, a, b, c)`.

## 6. Two unrelated tables exhausted memory instead of answering

Each reported difference costs about 715 bytes, and the count is linear. Two
tables that share nothing therefore need roughly **8.5 GB at ten million rows**.

I found this by accident — a benchmark against a mismatched pair climbed to 8 GB
before I killed it.

Pointing the tool at the wrong table, the wrong schema or the wrong environment
is *precisely* the situation a parity check exists to catch. It has to survive
doing so. It now stops at 10,000 differences, flags the result `truncated`, and
says "at least N" rather than being killed by the kernel.

## And four more

- **The overflow fix caught one of three overflows.** Widening the *result* of
  `key - lo` does nothing — the subtraction already happened in the column's own
  type. `hi - lo` overflowed too, and `hi` is `max_key + 1`, one past what bigint
  can hold. A key range that wide is exactly what hashing a key produces.
- **`Infinity` and `NaN` killed the run** on DuckDB, which cannot cast them to
  DECIMAL. Any division by zero makes one.
- **A NULL key was reported as a duplicate key**, because `count(distinct)`
  ignores NULLs — sending you hunting for duplicates that don't exist.
- **A missing `GRANT` was reported as a missing table.** `information_schema`
  only lists tables your role holds privileges on, and a restricted account is
  exactly what you point at production.

## What I'd take from this

Three things, none of them about databases.

**A test suite that only checks the happy path passes for a broken tool.** The
NULL boolean bug had been sitting in code that was already "verified end to end
against real PostgreSQL and DuckDB." It was verified against the case where
things work.

**When two independent systems agree, that is not proof they're right.** It's
the single most seductive signal in cross-engine work, and bug #1 is what it
looks like when it lies to you.

**Your dev environment has a shape, and it hides things.** Windows hid a bug
that broke every Linux and macOS user, through 248 passing tests. The first CI
run found it in ninety seconds.

---

`parity` is MIT, `pip install parity-diff`, PostgreSQL, DuckDB, MySQL, and Snowflake. Adding
an engine is one file of about eighty lines — MySQL was the first added after
release, and it was exactly that — the bisection knows nothing about
SQL and the dialects know nothing about bisection, and there's a test that fails
if that ever stops being true.

If you're mid-migration and can't get sign-off to switch the old system off,
I'd genuinely like to hear what would make this useful to you.
