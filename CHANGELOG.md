# Changelog

Notable changes, newest first. This project follows [semantic versioning](https://semver.org/),
with the caveat that 0.x means the CLI surface may still move.

## Unreleased

### Internal

A dedicated campaign to harden the **identical check** — a false match being the
one unforgivable failure for a diff tool — added ~20 tests across every angle it
could be attacked from. No abnormality was found.

- **Offline logic** (`tests/test_identical.py`): at high volume against the
  in-memory dialect, identical tables stay identical under every
  fan-out/threshold and with hostile data (the field separator, NULs, full
  Unicode), independent of column order, deterministic across runs, and
  zero-download at 200k rows; and a single planted change (cell, delete, insert)
  is never smoothed to a match, always matching a brute-force oracle.
- **Live cross-engine** (`tests/test_identical_live.py`, `test_mysql_postgres.py`):
  identical reads identical with zero download for the *hashed* key paths (text,
  composite, and a real `uuid`-typed key); for an edge-value table (bigint
  min/max, `Infinity`/`-Infinity`/`NaN`, a 12-digit decimal, astral-plane emoji,
  empty string, all-NULL row); for a *representation change* (the same values
  typed as integer↔bigint, numeric↔double, varchar↔text); for a 120-column table
  that forces PostgreSQL's nested-`concat_ws` tree; and directly between MySQL
  and PostgreSQL, the pair previously covered only through DuckDB. The exact
  0.2.1 same-content insert+delete bug is reproduced across two real engines and
  confirmed caught — the strongest guard on that fix.
- **A repeatable deep hunt.** `PARITY_DEEP=20 pytest tests/test_identical.py
  tests/test_properties.py` scales the oracle and the false-identical hunter far
  past their fast defaults; ~33,000 generated cases across a dozen seeds turned
  up nothing, and anyone can re-run it. Documented in `CONTRIBUTING.md`.
- `CONTRIBUTING.md` records the Snowflake findings (no bit-cast/`CONV` for the
  hash, `NUMBER` split by scale, engine-specific identifier case-folding) as
  traps for the next warehouse dialect.

## 0.2.2 — 2026-09-07

### Added

- **Snowflake support** (`pip install "parity-diff[snowflake]"`), verified end
  to end against a live account (AWS eu-central-2): the hash constant agrees,
  a full-table checksum matches DuckDB byte-for-byte over 5,000 mixed-type
  rows, and the identical / changed / deleted / NULL-vs-empty checks all pass.
  Snowflake has no bit-cast or `CONV`, so the 60-bit row hash goes through
  `floor(md5_number_upper64(x)/16)`; integers and decimals both report as
  `NUMBER` and are split by `numeric_scale`; the NULL sentinel is built from
  `chr(92)`. Snowflake offers only READ COMMITTED, so unlike PostgreSQL the
  walk is not pinned to one snapshot — a documented limitation, not a hidden one.

### Changed

- **Identifiers are now matched case-insensitively across engines.** `--key` is
  one name applied to both sides and the shared-column set is an intersection of
  two schemas, but Snowflake upper-cases unquoted identifiers while
  PostgreSQL/DuckDB lower-case them — so a table stored as `id`, `amount` and its
  migration stored as `ID`, `AMOUNT` previously shared no key and no columns, and
  the diff was impossible. Matching now folds case (each side still quotes its
  own real name in SQL), so a table diffs cleanly against its differently-cased
  migration. Same-case schemas are unaffected and no checksum moves; a table
  with two columns differing only in case is refused rather than folded
  ambiguously. This surfaced only under a live Snowflake run, which is the point
  of the §8 rule.

### Internal

- CI now creates the GitHub release page automatically after a successful PyPI
  publish, from the tag's CHANGELOG section.

## 0.2.1 — 2026-09-06

A round of generative testing — property-based, oracle (differential against a
brute-force reference), metamorphic, and cross-engine fuzzing — added to make
sure the tool actually does what it claims. It immediately earned its place by
turning up a real correctness bug, now fixed.

### Fixed

- **A same-content insert and delete in one bucket could read as identical —
  the cardinal sin for a diff tool.** Each bucket was summarised by its row
  count plus a checksum over the *comparable columns only*. When one row was
  added and another removed inside the same bucket and the two shared identical
  column values — utterly ordinary data: a repeated status, a boolean, an empty
  string — the counts balanced and the content sums matched, so the bucket read
  clean and both differing rows were missed. The per-bucket checksum now folds
  the **key** into every row's hash, so an inserted key and a deleted key hash
  differently and the bucket can no longer cancel. This only ever adds
  sensitivity: a checksum that differs is always re-checked by downloading and
  comparing the real rows, so the change can cost an extra query but never a
  wrong verdict. The oracle test — which compares the segmented walk against a
  brute-force row-by-row diff over thousands of generated tables — found it and
  now guards against its return.
- **Single-row (and other degenerate one-key) tables downloaded their rows
  instead of checksumming first.** An identical one-row table reported itself
  identical but still moved its rows across the network, breaking the "identical
  tables download zero rows" promise for that case. Such a table is now
  checksum-qualified like any other, so an identical one downloads nothing; the
  four-query cost on identical tables is unchanged.

### Added

- **Generative test suite** (`tests/test_properties.py`): an oracle test
  (~400 random table pairs per run checked against a brute-force diff), tuning-
  knob invariance, side-swap symmetry, an exact-change-count property, and
  bucket-tiling properties — ~2,600 generated cases per run across ten
  properties, all against the in-memory dialect so they run with no database.
- **Cross-engine encoding fuzzer** (`tests/test_fuzz_encoding.py`): ~250
  generated values plus hand-picked hostile ones (the field-separator byte, the
  `\N` sentinel spelled as real data, emoji, combining marks, RTL controls,
  bigint extremes, microsecond timestamps) inserted identically into PostgreSQL
  and DuckDB, asserting byte-identical canonical text and matching 60-bit row
  hashes, plus the whole `diff()` walk over that fuzzed data end to end.
- A `test` optional-dependency (`pip install "parity-diff[test]"`) carrying
  Hypothesis, so the generative suite runs in CI on every push. The runtime
  core stays standard-library only — this extra is never pulled in by an engine
  extra.

## 0.2.0 — 2026-09-06

Two additions that widen who the tool can serve, both proving the architecture
holds: adding an engine and a key type each touched one place.

### Added

- **MySQL support** (`pip install "parity-diff[mysql]"`, tested against 8.0).
  The first engine added after release, and the test of the "a dialect is ~80
  lines" claim. It forced three MySQL-specific decisions the encoding tests
  pin: the row hash goes through `CONV(hex,16,10)` rather than a bit-cast; the
  field separator is `char(31 using utf8mb4)` because MySQL has no `chr` and a
  bare `char` is binary; and the NULL sentinel is built from a hex literal
  because MySQL processes backslash escapes in string literals. Verified
  end to end against DuckDB, including the NULL-versus-empty-string trap.
- **Non-integer and composite keys.** A single integer column is still
  bisected directly and emits identical SQL to before. A uuid, a text key, or
  several columns together are hashed to 60 bits for bucketing only — row
  identity stays the real key, so a hash collision can never merge two rows.
  `--key` takes a comma-separated list.

### Verified engine agreement

The hash constant `648541476951500027` for `'abc'` now agrees across three
independent SQL paths — PostgreSQL's bit-cast, DuckDB's hex-cast, and MySQL's
`CONV`.

## 0.1.0 — 2026-09-05

First release. Compares a table in PostgreSQL against a table in DuckDB and
reports exactly which rows and columns differ, without moving the data out of
either engine.

### What it does

- **`parity diff`** with human and JSON output, and CI exit codes: `0`
  identical, `1` differences found, `2` error. An error is never `1`, so a
  pipeline can tell a real disagreement from a bad connection string.
- Pushes hash aggregation into both engines and binary-searches the key space.
  On identical tables it issues **4 queries and downloads zero rows**, whether
  the table holds ten thousand rows or ten million.
- Reports each difference as `only_in_a`, `only_in_b`, or `different` with the
  columns that moved and both values.
- `--columns`, `--exclude`, `--bisection-factor`, `--threshold`,
  `--float-scale`, `--max-diffs`, `--json`, `--quiet`.

### Measured

10,000,000 rows per side, PostgreSQL 18.4 against DuckDB 1.5.5, six runs:

| Scenario | Queries | Rows downloaded | Wall time |
|---|---|---|---|
| identical tables | 4 | 0 (0.0000%) | 25-31s |
| 5 planted differences | 28 | 7,628 (0.0381%) | 48-55s |

Query and row counts are exact and hardware-independent; the times are one
laptop.

### Correctness

Every test plants a difference and asserts the tool finds exactly it. A suite
that only compares matching tables passes trivially for a completely broken
tool, and that discipline caught each of these before release:

- a `NULL` boolean compared **equal** to `FALSE`, because `CASE ... ELSE` swallowed
  the NULL. Both engines produced the same wrong answer, so cross-engine
  agreement could not detect it
- `timestamptz` columns reported **every row as different** when the two servers
  sat in different timezones. Sessions are now pinned to UTC
- the walk saw the table change underneath it, because PostgreSQL's default
  READ COMMITTED gives every statement a fresh snapshot. Now REPEATABLE READ
- absolute DuckDB paths broke on every Linux and macOS machine
- tables wider than 99 columns failed outright on PostgreSQL's 100-argument
  function limit
- two unrelated tables exhausted memory rather than answering
- `Infinity` and `NaN` aborted the run on DuckDB
- a `NULL` key was misreported as a duplicate key, and a missing `GRANT` as a
  missing table
- the bucket arithmetic overflowed int64 on wide key ranges

### Known limitations

Stated plainly, because a parity tool that reports a false match is worse than
useless.

- **Any key type.** A single integer column is bisected directly; a uuid, a
  text key, or several columns together are hashed to 60 bits for bucketing
  only. Identity stays the real key, so a collision cannot merge two rows.
- **Floats and decimals compare at 6 decimal places** by default. Two values
  differing only in the 7th place are reported as equal. `--float-scale`
  changes it, and the scale in force is printed on every run.
- **Keys must be unique**, and must not be NULL. Both are detected and refused.
- **At most 10,000 differences are reported** by default; past that the run is
  flagged `truncated` and `identical` is never true. `--max-diffs 0` lifts it.
- Supported types: integer, decimal, float, boolean, string, date, timestamp.
  Anything else is compared as raw text and warned about.
- A double whose magnitude reaches 1e32 is not supported and fails loudly.

### Engines

PostgreSQL (16 and 18), DuckDB (1.5), and MySQL (8.0). Snowflake, BigQuery
and Redshift are not implemented; see `CONTRIBUTING.md` — a dialect is roughly
80 lines, and MySQL was the first one added after release to prove that.
