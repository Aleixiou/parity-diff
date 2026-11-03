# Reading the output

Every run prints its verdict first, then the evidence, then anything that
qualifies the verdict, then the rows. This page goes through every line the
report can print, what it means, and what to do about it. The examples are
real output.

```
✗ 3 differences in 100,000 rows                                    ← verdict
  10 queries · 18,749 rows downloaded (9.37% of both tables) · 0.6s ← cost
  1 only in A · 2 different                                         ← counts by kind

  ✓ independent sample check: 10,044 sampled rows (1 in 10) re-compared from raw values - all agree
    risk: with 99.9% confidence fewer than 0.069% of rows (at most 69 of 100,000) could differ in a way the checksums missed; a checksum collision is below 1 in 10^18
  comparing floats and decimals at 6 decimal places                ← always printed
                                                                    ← warnings (! ...) go here
  only in A   key 99999                                             ← the rows
  different   key 7              columns: status
      status   A NULL   B ''
  different   key 4242           columns: amount
      amount   A 5302.500000   B 5302.510000
```

On a console that cannot print `✗ ✓ · !` (an old Windows code page, some CI
logs) the same report uses `x = - ?` instead.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | identical |
| `1` | not proven identical: rows differ, a column exists on one side only, the checksums disagreed in a way the downloaded rows could not explain, the independent sample check contradicted the checksums, or the walk stopped at `--max-diffs` |
| `2` | error: bad connection string, missing table, unusable key, a refused value, a database error. The message goes to stderr and starts with `parity:` |

An error is never `1`, so a CI job can always tell "the tables differ" from
"the tool could not run".

## The verdict (first line)

Exactly one of these. `R` is the larger of the two sides' row counts.

| Headline | Exit | Meaning and what to do |
|---|---|---|
| `✓ no differences in R rows` | 0 | Every row and every shared column matched, the whole key space was walked, and nothing below contradicts it. Still read the qualifying lines: the float scale, and any `!` caveat such as a missing snapshot or columns not covered. |
| `✗ N differences in R rows` | 1 | Rows differ. They are listed at the bottom (see [The rows](#the-rows)). |
| `! at least N differences in R rows - stopped early, the rest was not checked` | 1 | The walk hit `--max-diffs` (default 10,000) and stopped. **This says nothing about the rest of the table.** Usually it means the wrong table, the wrong key or a type mismatch (see [troubleshooting](troubleshooting.md#everything-reads-as-different)); if the differences are real and you want all of them, raise the limit or pass `--max-diffs 0`. |
| `✗ schemas differ: no row differences in R rows, but not every column could be compared` | 1 | The shared columns match, but a column exists on one side only, so it was never compared. If the column was added or dropped on purpose, pass it to `--exclude` and re-run; it then becomes a warning. |
| `✗ not verified: checksums differ in N key ranges that the downloaded rows cannot explain` | 1 | The engines' checksums prove those ranges differ, but the rows downloaded from them compare equal (or a side returned fewer rows than it counted). Something between the database and the comparison altered or hid values: a driver, a row-capping setting, or the table changing mid-run. Re-run on a quiet table; if it persists, report it. |
| `✗ not verified: the independent sample check found N rows the checksum comparison called equal` | 1 | The second method contradicted the first: the tool's own SQL rendered two different values alike for these data. **Do not trust this comparison**, and please report it with the lines that follow. |

The order matters: a truncated run always says so first, and a run with any
row differences leads with the count even if other problems are listed below.

## The cost line

```
  10 queries · 18,749 rows downloaded (9.37% of both tables) · 0.6s
```

- **queries** — checksum and fetch queries of the walk, both sides together.
  Identical tables take 4. The sample check's two queries are counted apart
  (`stats.sample_queries` in JSON).
- **rows downloaded** — rows fetched to compare in Python, both sides, as a
  share of both tables together. On identical tables it is 0. A figure near
  100% means nearly every key range differed: almost always the wrong key or
  a type that renders differently on each side.
- **seconds** — wall time, including connecting and the sample check.

When more than one kind of difference was found, a third line counts them:
`1 only in A · 1 only in B · 3 different`.

## Qualifying lines

These come after a blank line, before any rows. Lines starting `✗` change
the verdict; lines starting `!` qualify it without changing it.

### Schema and unexplained ranges

```
  ✗ schema: not compared, present only on side A: ['extra']
  ✗ unverified <key range>: the checksums of side A (...) and side B (...) disagree, but the 2 downloaded row(s) compare equal - ...
  ✗ unverified side B (orders) holds 1,000 row(s), but its checksums covered 900: rows were hidden from the comparison ...
```

See the verdicts above. A column set aside with `--exclude` or left out of
`--columns` prints as a warning instead:
`! not compared, present only on side A: ['extra']`.

### The independent sample check

After the walk, about `--verify-sample` rows (default 10,000; every row of a
smaller table) are fetched again from both sides as the drivers return them
and compared in plain Python by the documented rules, with none of the SQL the
checksums depend on. Rows are chosen by a hash of the key with a fresh random
residue each run, so the exact count varies a little around the target.

| Line | Meaning |
|---|---|
| `✓ independent sample check: 10,044 sampled rows (1 in 10) re-compared from raw values - all agree` | Both methods agree on every sampled row. |
| `✓ independent sample check: every row re-compared from raw values - all agree` | The table had no more rows than `--verify-sample`, so every row was compared twice. |
| `✗ independent sample check: ... re-compared from raw values` followed by `✗ sample key ...` lines and `the checksum comparison is not trustworthy for these data; please report this` | A contradiction; the verdict is "not verified". Each `✗ sample` line names the key and what each method said. |
| `! not covered by the sample check (type outside its rules): ['s']` | These columns are of a type the rules do not define (structs, nested lists, geometry, ...). They were compared as raw text by the checksums, but the second method could not check them and **no risk figure covers them**. |
| `! sample: key 42: reported different in ['note'], which the raw values do not show (a false alarm, or a difference outside the reference rules)` | The walk reported a difference the second method does not see. Not a false match; worth a look. |
| `! sample: 3 value(s) of column 'x' are outside the reference rules and were not independently checked` | Some values (not the whole column) could not be checked. |
| `! independent sample check could not run: <error>` | The check failed to fetch its rows. **The verdict then stands on the checksums alone**, with no risk line: the exit code is not changed by this. Fix the cause (usually the message names a driver or permission problem) and re-run if you need the figure. |

The check is skipped with `--verify-sample 0`, and it does not run when the
walk stopped early (`--max-diffs`) or both tables are empty.

### The risk line

Printed under a passing sample check:

```
    risk: with 99.9% confidence fewer than 0.069% of rows (at most 69 of 100,000) could differ in a way the checksums missed; a checksum collision is below 1 in 10^18
    risk: every row was compared by both methods, so no row can hide a rendering error; a checksum collision is below 1 in 10^18
```

Two computed figures: the most rows that could carry a difference that both
the checksums and the sample missed (an exact binomial bound from the sample
size; 0 when every row was compared), and the chance that a key range holding
a real difference still sums to the same checksum (at most 2⁻⁶⁰ per differing
range, for data not crafted to collide). They cover the columns the sample
check covers, and nothing the documented rules ignore on purpose. How to quote
them to someone else: [`risk.md`](risk.md).

### Filtered runs (`--where`, `--a-where`, `--b-where`)

```
✓ no differences in 1,204 rows (filtered rows only)
  filtered: A where created_at >= '2026-10-01'; B where created_at >= '2026-10-01'
```

A filtered verdict covers only the rows the predicates select: nothing is
proven about the rest of the table. The headline says `(filtered rows only)`,
the `filtered:` line names each side's predicate (`whole table` for a side
without one), the risk figures count filtered rows, and JSON carries
`where_a`, `where_b` and `risk.filtered`. A caveat warns that a predicate
which changes during the run (`random()`, `clock_timestamp()`, or `now()`
without one snapshot) can select different rows in different queries; the
run then reports those ranges as unexplained, never identical. A separate
warning appears when only one side is filtered.

### Renamed columns (`--map`)

A renamed column is reported under side A's name with side B's beside it,
`customer_name (B: cust_nm)`, after a `renamed columns:` line; JSON carries
`column_map` and per-row `b_columns`.

### Float scale

```
  comparing floats and decimals at 6 decimal places
```

Always printed. Values that differ only past this place are equal by
definition. Change it with `--float-scale`.

### Warnings and caveats (`!`)

Everything the comparison decided on your behalf. The ones you will meet:

| Warning (start of the text) | What it means | What to do |
|---|---|---|
| `side A (snowflake): Snowflake offers only READ COMMITTED, so each query of the walk sees the table as of its own start, not one snapshot ...` | No single snapshot on this side; rows written during the run can show as differences that never existed, or be missed. Same idea for `database 'x' has ALLOW_SNAPSHOT_ISOLATION OFF ...` (SQL Server), `... is not a base table, and BigQuery ignores time travel on a view ...` and `table t uses the MyISAM storage engine, which has no transactions ...` (MySQL). | Diff a quiet table, a clone or a snapshot; or re-run and see whether the differences move. On SQL Server, turn on `ALLOW_SNAPSHOT_ISOLATION`. |
| `side A (postgres): row-level security filters public.t for this role: rows its policies hide are not compared at all ...` | Rows hidden by RLS were never compared. | Diff as a role with `BYPASSRLS`. |
| `... columns ['ts'] hold nanoseconds and the other side microseconds only; they are compared at microseconds ...` (also `hold 100-nanosecond ticks` on SQL Server, `can hold nanoseconds` on Snowflake) | Differences below one microsecond are not seen. | Nothing, if microseconds are what the target holds. |
| `column 'v' is VARCHAR on side A but BIGINT on side B; values are compared as text and will very likely all differ` | The column's type changed in the migration. Values compare by canonical text, so they match only where both types print the same text (here they did: `'1'` and `1`). | If every row differs, cast one side in a view. |
| `key column 'id' is VARCHAR on side A but BIGINT on side B; ... every row will report as only in A and only in B` | The key cannot match across these types. | Compare through a view that casts one side's key. |
| `column 'ts' is timezone-aware on side A but not on side B; both are read in UTC, so a migration that stored local wall-clock time rather than UTC will show every row as different` | `timestamptz` → `timestamp` is a common migration change. | If every row differs by a whole number of hours, the target stored local time: convert in a view. |
| `non-integer key (VARCHAR): bucketed by a 60-bit hash of ['code']. Rows are still matched and reported by their real key ...` (also `composite key: ...`) | Informational: how the key space is divided. | Nothing. |
| `unmapped types, compared as raw text (may differ across engines for reasons other than the data): ['s']` | No exact rule for these types. | Treat differences there with care, and remember no risk figure covers them; `--exclude` them if they are noise. |
| `JSON columns ['doc'] are compared as each engine's normalised document, and the two engines normalise differently ...` | Equal documents may read as different (never the reverse). | Compare a canonical text form through a view on each side. |
| `--exclude named columns that exist on neither side: ['x']` | Probably a typo. | Fix the name. |
| `no comparable columns: only the presence of each key is checked, not row contents` | Both sides share only the key. | Check `--columns` / `--exclude`. |
| `stopped at the --max-diffs limit of 10000; N further difference(s) or key range(s) were not reported, so this is a partial answer` | See the truncated verdict. | |

## The rows

Listed after the qualifying lines, in this order: only in A, only in B,
different; within each, in key order.

```
  only in A   key 99999
  only in B   key 4000
  different   key 7              columns: status
      status   A NULL   B ''
  different   key 4242           columns: amount
      amount   A 5302.500000   B 5302.510000
```

- **only in A / only in B** — the key exists on one side only.
- **different** — the key exists on both sides and the listed columns differ.
  Each changed column shows both values side by side, or on two lines when
  they are long.
- **Values are shown as compared.** A real NULL prints bare as `NULL`; text is
  quoted, so `''`, `'NULL'` and a trailing space all look different from each
  other and from NULL. Numbers print at the float scale (`5302.500000`), dates
  and timestamps in the canonical form (`2024-01-01 00:00:00.000000`).
- **Composite keys** print their parts separated by ` | `, each quoted.
- At most 100 rows are printed; the rest are counted
  (`... 250 more difference(s) found but not shown here; use --json for the full list`).
  This is a display limit only — the verdict and the JSON cover all of them.

## JSON (`--json`)

The same content as one object. A consumer that reads only `identical` can
never be misled by a partial, schema-incomplete or unverified run: it is false
in every one of those cases.

```json
{
  "identical": false,
  "truncated": false,
  "schema_differences": [],
  "unexplained": [],
  "verification": {
    "modulus": 100, "residue": 67, "rows_a": 1017, "rows_b": 1017,
    "checked": ["amount", "ordered_on", "status"], "unchecked": [],
    "failures": [], "notes": [], "error": null
  },
  "risk": {
    "confidence": 0.999,
    "checksum_collision_bound": 8.673617379884035e-19,
    "max_hidden_fraction": 0.006769270971306307,
    "max_hidden_rows": 677,
    "rows": 100000,
    "covered_columns": ["amount", "ordered_on", "status"],
    "uncovered_columns": []
  },
  "difference_count": 3,
  "float_scale": 6,
  "columns_compared": ["amount", "ordered_on", "status"],
  "warnings": [],
  "stats": {
    "queries": 10, "segments_checked": 1, "rows_downloaded": 18749,
    "rows_a": 100000, "rows_b": 99999, "percent_downloaded": 9.3745,
    "seconds": 0.394, "sample_queries": 2, "rows_sampled": 2034
  },
  "differences": [
    {"key": 7, "kind": "different", "columns": ["status"],
     "a": {"status": "\\N"}, "b": {"status": ""}},
    {"key": 4242, "kind": "different", "columns": ["amount"],
     "a": {"amount": "5302.500000"}, "b": {"amount": "5302.510000"}},
    {"key": 99999, "kind": "only_in_a",
     "columns": ["amount", "ordered_on", "status"],
     "a": {"amount": "124998.750000", "ordered_on": "2024-12-20", "status": "shipped"},
     "b": {}}
  ]
}
```

(Real output of the [quickstart](../README.md#try-it-in-five-minutes) tables
with `--verify-sample 1000`, reflowed.)

| Field | |
|---|---|
| `identical` | `true` only if no row differed, no column exists on one side only, every checksum mismatch was explained, the sample check found no contradiction, and the whole key space was walked. |
| `truncated` | The walk stopped at `--max-diffs`; the result is partial. |
| `schema_differences` | Columns one side lacks that were not set aside. |
| `unexplained` | Key ranges (or row counts) the downloaded rows could not account for. |
| `verification` | The sample check: which rows (`modulus`, `residue` — every row whose key hash ≡ `residue` mod `modulus`), how many per side, the `checked` and `unchecked` columns, `failures`, `notes`, `error`. `null` when skipped or when the walk stopped early. |
| `risk` | The risk line's figures as numbers. `max_hidden_fraction` / `max_hidden_rows` are `null` when the check failed, could not run or was skipped; `0` when every row was compared. |
| `difference_count`, `differences` | Every difference (not capped at 100). `key` is an integer for an integer key, otherwise the key's canonical text, with composite parts joined by chr(31). `a` / `b` hold the canonical text of each side's values: `\N` is NULL, a backslash in text is doubled and chr(31) is written `\s` — the exact bytes the engines hashed. For `only_in_*`, the missing side is `{}`. |
| `float_scale`, `columns_compared`, `warnings` | As in the human report. |
| `stats` | `queries`, `segments_checked`, `rows_downloaded`, `rows_a`, `rows_b`, `percent_downloaded` (of both tables), `seconds`, and the sample check's own `sample_queries` and `rows_sampled` (both sides). |

With `--quiet` nothing is printed at all; rely on the exit code. Errors still
go to stderr.
