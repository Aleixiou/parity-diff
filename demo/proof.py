"""End-to-end proof: identical 200k-row tables in DuckDB and Postgres,
then the same with planted differences."""

import sys
import time

sys.path.insert(0, "src")
import os

import duckdb
import psycopg

from parity.dialects.base import get_dialect
from parity.engine import bucket_bounds, diff

N = 200_000
# CLAUDE.md section 7 documents the Docker container on port 55432. This machine
# runs a native PostgreSQL install on 5432, so the endpoint is overridable.
PG = os.environ.get("PARITY_TEST_PG", "postgres://parity:parity@127.0.0.1:5432/parity")
# A table name of this script's own: demo/generate.py owns `orders` in the
# same database, and this script drops and recreates whatever it is given.
TABLE = "parity_proof_orders"
DUCK = "duckdb:///./e2e.duckdb"

if os.path.exists("e2e.duckdb"):
    os.remove("e2e.duckdb")

SELECT = """
select i::bigint as id,
       (i % 977)::integer as customer_id,
       ((i * 7 % 100000) / 100.0)::decimal(12,2) as amount,
       case when i % 3 = 0 then 'paid' when i % 3 = 1 then 'open' else 'void' end as status,
       (i % 11 = 0) as is_refunded,
       (timestamp '2024-01-01 00:00:00' + (i % 86400) * interval '1 second') as created_at,
       case when i % 13 = 0 then null else 'note ' || i::varchar end as note
from {series}
"""
pg_sql = SELECT.format(series=f"generate_series(1,{N}) as s(i)")
dck_sql = SELECT.format(series=f"generate_series(1,{N}) as s(i)")

t0 = time.perf_counter()
d = duckdb.connect("e2e.duckdb")
d.execute(f"create table {TABLE} as {dck_sql}")
d.close()
pg = psycopg.connect(PG)
with pg.cursor() as c:
    c.execute(f"drop table if exists {TABLE}")
    c.execute(f"create table {TABLE} as {pg_sql}")
    c.execute(f"alter table {TABLE} add primary key (id)")  # index matters, see below
pg.commit()
print(f"loaded {N:,} rows into both engines in {time.perf_counter() - t0:.1f}s")


def run(label):
    """Run one diff, print what it cost, and hand back the result."""
    a = get_dialect(PG)
    b = get_dialect(DUCK)
    r = diff(a, b, f"public.{TABLE}", f"main.{TABLE}", "id")
    s = r.stats
    pct = 100 * s.rows_downloaded / max(N, 1)
    print(f"\n--- {label} ---")
    print(
        f"  diffs={len(r.diffs)}  queries={s.queries}  segments={s.segments_checked}"
        f"  rows_downloaded={s.rows_downloaded:,} ({pct:.3f}%)  {s.seconds:.2f}s"
    )
    for dd in r.diffs[:6]:
        print(
            f"    {dd.kind:<11} key={dd.key:<8} cols={dd.columns} A={dd.values_a} B={dd.values_b}"
        )
    for w in r.warnings:
        print("    warn:", w)
    a.close()
    b.close()
    return r


r1 = run("IDENTICAL TABLES (must find 0, must download 0)")
assert r1.identical, "FALSE POSITIVE: reported differences on identical tables"
assert r1.stats.rows_downloaded == 0, "downloaded rows despite a clean match"

# plant differences
pg2 = psycopg.connect(PG)
with pg2.cursor() as c:
    c.execute(
        f"update {TABLE} set amount = amount + 0.01 where id = 120455"
    )  # changed value
    c.execute(f"delete from {TABLE} where id = 47")  # only in B
    c.execute(
        f"insert into {TABLE} values (999999999, 1, 1.00, 'paid', false, timestamp '2024-01-01', 'extra')"
    )  # only in A
    c.execute(f"update {TABLE} set note = '' where id = 13")  # NULL vs '' trap
pg2.commit()

r2 = run("PLANTED DIFFERENCES (must find exactly 4)")
kinds = sorted((d.key, d.kind) for d in r2.diffs)
print("\n  found:", kinds)
expected = sorted(
    [
        (47, "only_in_b"),
        (13, "different"),
        (120455, "different"),
        (999999999, "only_in_a"),
    ]
)
assert kinds == expected, f"MISMATCH\n  expected {expected}\n  got      {kinds}"
print("\n*** exact match on all planted differences ***")

# bucket_bounds must invert the SQL bucket expression for every key
print("\n--- bucket_bounds vs SQL bucket assignment ---")
import random

a = get_dialect(PG)
for trial in range(5):
    lo = random.randint(1, 1000)
    hi = lo + random.randint(50, 5000)
    n = random.choice([2, 7, 32, 100])
    n = min(n, hi - lo)
    rows = a.query(
        f"select i, ((i - {lo}) * {n}) / ({hi} - {lo}) as seg "
        f"from generate_series({lo},{hi - 1}) as s(i)"
    )
    for i in range(n):
        b_lo, b_hi = bucket_bounds(i, lo, hi, n)
        sql_keys = {k for k, seg in rows if seg == i}
        py_keys = set(range(b_lo, b_hi))
        assert sql_keys == py_keys, f"bucket {i} mismatch lo={lo} hi={hi} n={n}"
    print(
        f"  lo={lo:<5} hi={hi:<6} n={n:<4} all {hi - lo} keys land in the same bucket both ways  OK"
    )
a.close()
print("\nALL CHECKS PASSED")
