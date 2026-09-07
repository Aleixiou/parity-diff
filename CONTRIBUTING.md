# Contributing

The most valuable contribution is **a new dialect**. The architecture exists to
make that a single file of roughly eighty lines that touches nothing else.

## Adding a dialect

The bisection engine knows nothing about SQL. It talks to two `Dialect` objects
through a contract, so adding Snowflake or BigQuery means writing one new file
in `src/parity/dialects/` and registering it in `get_dialect()`. If you find
yourself editing `engine.py`, the abstraction is wrong — say so in an issue
rather than working around it.

### What you implement

Nine methods, all abstract:

```python
class Dialect(ABC):
    name: str
    default_schema: str                           # "public", "main", ...

    def connect(self, connection_string: str) -> None: ...
    def close(self) -> None: ...
    def query(self, sql: str) -> list[tuple]: ...
    def quote(self, identifier: str) -> str: ...
    def normalize(self, column: Column) -> str:   # canonical text, null-safe
    def hash_expr(self, text_expr: str) -> str:   # -> 60-bit integer
    def int_div(self, num: str, den: str) -> str: # truncating division
    def sum_wide(self, expr: str) -> str:         # overflow-safe sum
    def wide_int(self, expr: str) -> str:         # widen past 64 bits
```

Everything else is inherited and you should not need to touch it: the base
class introspects columns from `information_schema` (override `columns` only if
your engine has no such view), builds the row text, the per-segment checksum
query and the small-range fetch, and splits `schema.table`. Two optional hooks
have sensible defaults — `cancel()` for aborting an in-flight query from
another thread, and `_exists_but_unreadable()` for engines whose catalog hides
tables the current role lacks privileges on.

That comes to roughly 70–85 lines. Read
`src/parity/dialects/duckdb_dialect.py` first — it is the shortest complete
example.

### The things that will bite you

Each of these cost real time to discover. They are documented at length in
`CLAUDE.md` §4; the short version:

1. **The hash must be exactly 60 bits.** 15 hex characters of an MD5 digest.
   That is the widest prefix both PostgreSQL and DuckDB render as the same
   *positive* signed 64-bit integer. At 64 bits PostgreSQL wraps negative and
   the engines disagree. Your dialect must produce `648541476951500027` for
   input `'abc'` — there is a test that checks exactly this.

2. **The sum must be widened.** Row hashes reach 2^60, so summing a few million
   overflows a 64-bit accumulator. Aggregate in `numeric`, `decimal(38,0)`, or
   whatever your engine's arbitrary-precision type is. And wrap it in
   `coalesce(..., 0)`: an empty segment returns SQL `NULL` from `sum()`, and an
   empty bucket on one side must compare equal to an empty bucket on the other
   or the walker recurses into nothing.

3. **`NULL` must render as the literal `\N`, never SQL NULL.** An un-coalesced
   NULL poisons the whole concatenation and silently masks differences. Watch
   `CASE` expressions especially: `case when c then 'true' else 'false' end`
   sends NULL down the `else` branch, so a NULL boolean renders `'false'` and
   compares equal to a real FALSE. Both engines agreed on that wrong answer for
   a while. Use `case when c then 'true' when not c then 'false' end`.

4. **`/` is not portable.** PostgreSQL truncates on integer operands, DuckDB
   promotes to double. That is what `int_div` is for. Do not reach for
   `floor(a/b)` — double precision silently breaks on large key ranges.

5. **Prefer summing to XOR.** `bit_xor` exists in most engines and silently
   cancels duplicate rows, which is precisely the difference you need to see.

6. **`wide_int` has to widen the key *before* the arithmetic, not after.** The
   bucket expression computes `(key - lo) * n_segments`, and a key range as
   wide as bigint overflows in three separate places. `wide_int(k - lo)` looks
   right and does nothing, because the subtraction already happened in the
   column's own type. Return something that survives 128 bits — `hugeint`,
   `numeric`, `NUMERIC(38,0)` — and check `int_div` still truncates on that
   type rather than producing a scaled or rounded result.

MySQL, added after v0.1.0, turned up two more that a warehouse dialect may hit:

7. **Not every engine has `chr`, and `char(n)` may be binary.** MySQL spells
   the separator `char(31)`, not `chr(31)`, and a bare `char(31)` is a *binary*
   string that coerces the whole `concat_ws` to bytes - which then comes back
   from `fetch_range` as `bytes`, not `str`, and every row reads as different.
   The separator and the NULL sentinel are dialect hooks (`separator_sql`,
   `null_sentinel_sql`) for exactly this reason.

8. **Backslash in a string literal is not portable.** MySQL processes a
   backslash as an escape inside `'...'`, toggled by `sql_mode`, so the `\N`
   sentinel silently became `N`. Build such bytes from `CHAR`/hex, not a
   literal, and verify by hashing rather than by reading the SQL.

Snowflake, the first warehouse (v0.2.2), added three more — the sort a warehouse
is especially likely to spring:

9. **Your engine may have neither a bit-cast nor `CONV` to reach 60 bits.**
   Snowflake had no `bit(60)::bigint` and no `conv(hex,16,10)`. What it did have
   is `md5_number_upper64(x)`, the top 64 bits of the digest as a number, and
   `floor(that / 16)` drops the low 4 to land on the same 60-bit prefix - the
   fourth distinct path to `648541476951500027`. Find your engine's own route;
   the constant is the contract, not the SQL that reaches it.

10. **Integers and decimals may share one type name.** Snowflake reports both as
    `NUMBER` and only `numeric_scale` (0 = integer) tells them apart, so its
    `columns()` reads the scale instead of trusting `data_type`. Trust the type
    name and an integer key renders as `42.000000` and never matches another
    engine's `42`. If your engine collapses numeric types like this, override
    `columns()`.

11. **Identifier case-folding is the engine's, and it is not universal.**
    Snowflake upper-cases unquoted identifiers where PostgreSQL and DuckDB
    lower-case them. The engine matches keys and columns case-insensitively for
    exactly this reason (`_fold_columns`), but the *table* name is looked up in
    the case the engine stored, so `--a-table orders` against a Snowflake
    `ORDERS` fails with a near-miss hint. And some warehouses (Snowflake among
    them) offer only READ COMMITTED, so unlike PostgreSQL the walk cannot be
    pinned to one snapshot - a real limitation to document, not hide. None of
    these three showed up in the docs; they surfaced only against a live
    account, which is why the rule below is not negotiable.

### Proving it works

A dialect is not done until `tests/test_encoding.py` passes against it. That
file is the correctness contract: it inserts the same literal into your engine
and into a reference engine and asserts the canonical text is byte-identical.

Add your engine to the fixtures there and run:

```bash
pytest tests/test_encoding.py -v
```

**Every test must plant a difference.** A test that only asserts identical
tables match passes trivially for a completely broken tool — this is the single
rule the project cares most about. For each positive assertion ("these agree"),
add the negative control ("and the harness notices when they genuinely don't").
That discipline is what caught the boolean NULL bug in point 3 above.

## Running the checks

```bash
pip install -e ".[all]" pytest mypy ruff
pytest
ruff check src tests demo
mypy src/parity --strict --ignore-missing-imports
```

CI runs the static checks first, because they take seconds where the test
matrix takes minutes. `mypy --strict` is what turns "type hints everywhere"
from an aspiration into a fact, and ruff's `ISC`, `BLE` and `S` rules are on
deliberately: implicit string concatenation inside a collection is the
missing-comma bug class, a blind `except` has to be justified where it sits,
and this tool builds SQL by hand so injection rules earn their place. Where a
rule is knowingly not applicable, the ignore lives in `pyproject.toml` with the
reason next to it rather than being switched off globally.

PostgreSQL-backed tests read `PARITY_TEST_PG` and skip cleanly when nothing is
listening, so the suite is useful with only DuckDB installed.

## Scope

Before proposing a feature, check it against the question the tool exists to
answer: *"can I safely switch off the old system?"* Data quality rules,
freshness checks, lineage, cataloguing, orchestration, a web UI, and schema
migration are all deliberately out of scope. The open-source predecessor in
this category was abandoned because maintaining it grew expensive; a narrow
scope is the only defence.

Non-integer and composite keys, sampling mode, and a dbt integration are
planned and welcome.

## Style

- Python 3.10+, `from __future__ import annotations` at the top of every module.
- Type hints everywhere. Dataclasses for value types. No ORM — emitting dialect
  SQL deliberately is the product.
- No network calls, no telemetry. This tool points at production warehouses;
  trust is the whole distribution strategy.
- Read-only by construction. The tool issues `SELECT` only and never generates
  DDL or DML against a user's database.
- Comments explain *why*, especially for cross-engine workarounds — each one is
  a landmine for the next person.
- Errors must name the side and the table. `"table not found"` is useless when
  two databases are in play.
