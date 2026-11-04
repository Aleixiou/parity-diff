# Error odds — what can go wrong, and how likely

This file states, for every step of a comparison, how a wrong answer could
arise and how likely it is. It is kept current with the code: it is updated
after **every** change to the tool, and `tests/test_risk_doc.py` fails the
suite whenever the source code, a constant below or the coverage map no
longer match it (see *Keeping this file current* at the end).

Every figure is labelled with what kind of number it is:

- **Proven bound** — follows from mathematics under the stated assumption.
- **Measured** — counted from tests that ran; true for what was tested.
- **By design** — a documented rule, not an error; both methods follow it.
- **Not quantifiable** — no honest number exists; what limits it is stated.

The worst error is a **false match**: "identical" for tables that are not.
A **false alarm** (a reported difference that is not real) and a **refusal**
(the tool stops with an error) are inconvenient but never hide a difference.

Source fingerprint: `src-sha256: 305628369a22d49f`

## Summary

For one run that reports **identical**, on columns the rules cover:

| What would have to go wrong | Odds | Kind |
|---|---|---|
| A real difference sums to the same checksum by accident | at most **2⁻⁶⁰ ≈ 8.67 × 10⁻¹⁹** per key range holding a difference | Proven bound |
| A rendering error hides a difference, and the independent sample check misses it too | fewer than **1 − 0.001^(1/n)** of the rows, at 99.9% confidence, for n rows sampled — **0.069%** at the default n = 10,000 (about 1 row in 1,450); **0** when every row was compared | Proven bound |
| An engine renders a value differently from the rules | **0 of 4,180** checks at float scale 6 outside the 26 required refusals (4,154 exact), every type with a rule × 6 engines | Measured |
| Rows lost between the database and the comparison | **0**: the run is not identical when a download cannot explain a checksum difference | By design (enforced) |
| A partial run read as complete | **0**: a run stopped by `--max-diffs` is never identical | By design (enforced) |
| The sample check could not run, and the run reads as checked | **0**: a check that was asked for and failed to fetch its rows makes the run "not proven", never identical (exit 1). Only `--verify-sample 0` rests the verdict on the checksums alone, and the report then says the check was off and computes no risk figure | By design (enforced) |
| A column on one side only, never compared | **0**: a schema difference is never identical | By design (enforced) |
| A renamed column (`--map`) compared against the wrong counterpart | **0**: pairing is exact, ambiguous maps are refused | By design (tested) |
| A row filter (`--where`) missing one of the queries that read the table | **0**: one code path builds every such query; a test records every statement on all six dialects | By design (tested) |
| An "identical" over no rows at all (a mistyped filter, the wrong table) read as a check | **0**: a side that compared 0 rows draws a warning naming it on every run, and when both are empty the headline says `0 rows compared, both sides are empty` | By design (enforced) |
| A filtered "identical" read as one about the whole table | **0**: the headline, a `filtered:` line, JSON and a caveat state the filter; a filtered run **proves nothing about rows outside it** | By design (enforced) |
| A row filter whose result changes during the run (`random()`, `clock_timestamp()`, `now()`/`current_timestamp()` unless the engine evaluates it once per run - BigQuery's snapshot does not pin its clock) | a change between two queries of the same range is **never identical** (row counts and each level's sums are cross-checked); a row entering the filter outside the key range first measured is not compared; otherwise **not bounded** | Not quantifiable |
| A `parity run` summary reading stronger than one of its pairs | **0**: the run is identical only if every pair is; every pair's qualifiers appear in every output format; an error or Ctrl-C is exit 2 | By design (tested) |
| A `--resume`d pair whose tables or settings changed carried over as identical | **0** for the config entry, the tool version and the connection strings as expanded (hashed); **not bounded** for data written since the earlier run — the report names it and its timestamp | By design / Not quantifiable |
| A difference the rules ignore on purpose (past `--float-scale`, sub-µs against a µs engine, CHAR padding) | these are equal **by definition**; the report states the scale and the caveats | By design |
| A table changing during the run on an engine without one snapshot | **not bounded**; the report warns, and the sample check catches what changed in the rows it reads | Not quantifiable |
| A server setting hides rows or columns from the reading session | **0** for every setting found (round G: MySQL `sql_select_limit`, Snowflake `ROWS_PER_RESULTSET` reset per session); any other cap makes a side's checksum counts fall short of its row count, and the run is then not identical | By design (tested) |
| Row-level security hides rows from the reading role (PostgreSQL; SQL Server security policies) | **not bounded**: those rows are not compared; every run names the table in a caveat (on SQL Server, without VIEW DEFINITION on the database, a caveat that RLS cannot be ruled out) | Not quantifiable |
| Dynamic data masking hands the login masks instead of values (SQL Server) | **0** for a table: a compared column masked for the login is refused; a view reading one is refused, and so is any view whose dependencies the login cannot see (no VIEW DEFINITION) in a database holding masked columns it cannot unmask | By design (tested live) |
| Column-level grants hide a column from the reading login (PostgreSQL) | **0**: a column the login lacks SELECT on is a schema difference, never identical, unless `--exclude`d (round H) | By design (tested) |
| A Snowflake row access policy hides them, or a Snowflake masking policy shows both sides the mask | **not bounded**: those rows or values are not compared; every run names the table (and a masked column) in a caveat, and on a Snowflake view says the policies beneath it are not checked | Not quantifiable |
| Data crafted on purpose to collide | **not bounded**: eight crafted rows can cancel in one sum | Not quantifiable |
| Columns of types outside the rules (BigQuery STRUCT/GEOGRAPHY/INTERVAL/RANGE and arrays of them or of JSON, nested lists, other engines' structs, ranges, geometry, XML; listed per run) | **no bound**; named in the report as not covered. Not only false alarms: until round H, BigQuery's text for ARRAY/STRUCT/GEOGRAPHY collided (false matches). Now injective on BigQuery; elsewhere a raw text cast whose injectivity is per engine | Not quantifiable |
| A bug in the tool not yet found | **no number exists**; see the evidence in each section | Not quantifiable |

How to state a run's risk to a client: quote the run's own `risk` line (or
JSON `risk`), the scale and caveats it printed, and the uncovered columns
it listed. Do not quote a probability that the software has no bugs.

---

## 1. Connecting and pinning the session

What it does: opens each side read-only, pins time zone (UTC), float output,
escape handling, search path and isolation (CLAUDE.md 4.8, 4.9, 4.11).

| Risk | Odds | Kind |
|---|---|---|
| Session settings change how values render | **0** for every pinned setting; round G set every user-settable PostgreSQL setting at each value, MySQL sql_mode, charset, time_zone and init_connect variants, SQL Server collations, ANSI options and 'user options', Snowflake output formats and BigQuery `und:ci`: the 6 that changed anything are now pinned or refused | Measured |
| A pinned setting changed mid-walk by the queries themselves (`set_config()` in a `--where` or a view's function, PostgreSQL) | **0**: such filters are refused by name, and every pin is re-read after the walk - one that moved makes the run unexplained, never identical (round H) | By design (tested) |
| PostgreSQL money written under a locale with other decimals than the login's default lc_monetary | **not bounded**: money is an integer of minor units and read at the login's own frac_digits (a caveat names them when not two); a session-only write locale cannot be detected | Not quantifiable |
| Engine version renders differently | **0** within the supported ranges: PostgreSQL 12-17 byte-identical to 18 (9.6-11 for non-FLOAT, with `extra_float_digits = 3` there so floats inside unmapped types print exactly - round H), MySQL 8.4 and 9.7 to 8.0, SQL Server 2019 to 2022 and compatibility levels 130-160, DuckDB 1.3-2.0 to 1.5; versions outside are refused at connect (PostgreSQL < 9.6, DuckDB < 1.3) or for FLOAT (PostgreSQL < 12, SQL Server level < 130) | Measured |
| Rows hidden from the session (row caps, RLS, Snowflake policies) | caps: **0**, pinned and cross-checked against row counts; PostgreSQL RLS and Snowflake masking / row access policies: **not bounded**, named in a caveat (a Snowflake view's underlying policies cannot be listed, and the caveat says so) | By design / Not quantifiable |
| The login method changes what is read (password, or Snowflake key pair) | **0**: authentication selects the user, not the session; both paths go through one set of driver arguments carrying the same telemetry and session pins (pinned offline per path) | By design (tested) |
| Table changes mid-run, single snapshot (PostgreSQL, MySQL InnoDB, SQL Server with snapshot isolation, DuckDB, BigQuery base tables) | **0** — the whole walk reads one version | By design (verified live) |
| A MySQL view over a non-transactional table read as one snapshot | **0** — the view's base tables are followed through `view_table_usage` (views of views included) and any non-InnoDB one gets the caveat below; a view whose base tables cannot be listed gets it too (round H, RT-HM17) | By design (tested) |
| Table changes mid-run, no single snapshot (Snowflake, BigQuery views, MySQL non-InnoDB, SQL Server without snapshot isolation) | **not bounded**; every run prints a caveat naming the side; a change inside a range between two levels of the walk makes that range unexplained (each level must add up, rows and checksum, to the level above); the sample check re-reads rows after the walk and fails on any sampled row that changed | Not quantifiable |

## 2. Matching schemas and columns

| Risk | Odds | Kind |
|---|---|---|
| A column present on one side only is silently skipped | **0** — it is a schema difference, exit 1 | By design (tested) |
| Columns the login may not read are silently left out (MySQL column-level grants, which hide them from `information_schema`) | **0** — `SELECT *` fails exactly when a column is hidden, and the table is then refused naming the visible columns (round H, RT-HM3, a false match before) | By design (verified live) |
| Two columns differing only in case are confused | **0** — such a table is refused | By design (tested) |
| A type mismatch between sides goes unnoticed | **0** — a warning is printed; values compare by canonical text | By design (tested) |
| A renamed column (`--map`) paired with the wrong column | **0** — side B's column is re-keyed under side A's name before any other matching, so the compared list, its order (the row text's field order) and the key follow from A's names alone; exact, so no new rendering or checksum risk. Refused: a mapped name absent on its side, one column mapped twice either way, a mapped B column a same-named column would still match, mapped and excluded. Pinned both ways: swapped columns mapped as a swap match except the planted row, unswapped ones differ on every row | By design (tested) |

### 2b. Row filters (`--where`)

What it does: compares only the rows a SQL predicate selects on each side.
**A filtered run proves nothing about the rows outside its filter**: its
verdict, its row counts and its sample-check bound (section 8) are all of
the filtered rows, and the report says so in the headline, a `filtered:`
line, JSON (`where_a`, `where_b`, `risk.filtered`) and a caveat.

| Risk | Odds | Kind |
|---|---|---|
| One query that reads the table runs unfiltered (a row counted but never checksummed, or sampled but never walked) | **0** — the key stats, every checksum level, every download and the sample all name the table through one builder that adds the predicate; `tests/test_where_sql.py` records every statement on all six dialects | By design (tested) |
| The predicate escapes its parentheses (`1=1) or (1=1`) or ends the statement | **0** for what the check knows: `;`, comments, MySQL `#`, a backslash before a quote, triple- and dollar-quoted strings and unbalanced quotes, brackets or parentheses are refused before any query; on SQL Server the filter is re-scanned as T-SQL tokenises it (`]]` inside brackets), table and query hints and ODBC escape braces are refused, and the SELECT guard also applies. Not a security boundary: it is the user's own SQL, and the sessions are read-only | By design (tested) |
| The predicate selects different rows in different queries (`random()`, `clock_timestamp()`; `now()`/`current_date` where an engine gives each query its own snapshot) | a change between the row count and the first level, or between two levels of one range, is **never identical** (unexplained); a row entering the filter beyond the key range measured first is **not compared**; beyond that **not bounded** — every filtered run's caveat says to prefer fixed literals | Not quantifiable |
| A deterministic predicate inside one snapshot (PostgreSQL, MySQL InnoDB, SQL Server with snapshot isolation, DuckDB, BigQuery base tables; `now()` is stable within PostgreSQL's transaction) | **no new risk**: every query sees the same rows, so the walk is the unfiltered walk over a smaller table | By design (tested) |
| A predicate takes row locks on the live table (MySQL `FOR SHARE` / `LOCK IN SHARE MODE` in a subquery, which READ ONLY allows) | **0** — `FOR SHARE`, `FOR UPDATE`, `LOCK IN SHARE MODE`, `INTO`, `OUTFILE` and `DUMPFILE` outside quotes are refused on MySQL before any query (round H, RT-HM12) | By design (tested) |
| The two sides' predicates select different slices | **not the tool's to know**: rows in one slice only read as only in A / only in B, and a one-sided filter draws a warning | By design |

### 2c. Many pairs in one run (`parity run`)

What it does: runs one ordinary comparison per `[[pair]]` of a config file,
one after another, each on two fresh connections closed before the next.
**A multi-pair verdict is each pair's verdict**: every figure in this file
applies per pair, and the run's own verdict ("every pair identical") is
never stronger than its weakest pair. Quote a run by its pairs: each pair's
risk line, scale, filters and caveats, as the reports print them.

| Risk | Odds | Kind |
|---|---|---|
| A pair's qualifier (filtered, truncated, unexplained, schema differences, sample-check failure or error, uncovered columns, caveats) missing from the summary | **0** — every format renders from the pair's own JSON result through one list of qualifiers; `tests/test_batch.py` plants each qualifier and checks it in the human, JSON, Markdown and JUnit output | By design (tested) |
| One pair's session or snapshot affecting another | **0** — fresh connections per pair, closed before the next | By design (tested) |
| A pair that errored, or was not run (Ctrl-C, `--fail-fast`), counted as identical | **0** — the run is identical only if every selected pair is; an error or an interrupt is exit 2 | By design (tested) |
| A misspelt field silently ignored (e.g. a `where` that never applies) | **0** — unknown fields are refused before any connection | By design (tested) |
| `--resume` carrying over a verdict that no longer holds | only pairs that finished identical are carried over, and only under an unchanged hash of the pair's settings, the tool version and both expanded connection strings; data written to the tables **since** that run is **not bounded** — each carried-over pair is labelled "from an earlier run" with its timestamp in every output | By design / Not quantifiable |
| A password from `${NAME}` in the output | **0** for the tool's own messages, reports and the state file (redacted; connections are stored as a salted PBKDF2 fingerprint); data values are never altered | By design (tested) |

## 3. Checking keys

| Risk | Odds | Kind |
|---|---|---|
| Duplicate or NULL keys collapse rows and hide differences | **0** — refused before the walk, counted by bytes (never under a collation) | By design (tested) |
| Two distinct keys render alike and merge | **0** for INTEGER (DuckDB UHUGEINT hashed by its exact text), DECIMAL (exact text), TIMESTAMP (all stored digits, as the instant in UTC), STRING (escaped), and the exact rules of TIME, BINARY, BITS, INTERVAL, TIMETZ, INET, VARIANT; FLOAT and LIST keys are refused (a list's elements round as values do). This row said 0 for TIMESTAMP before round H too, and was wrong on Snowflake: a TIMESTAMP_TZ key with nanoseconds printed its stored wall clock, so two instants at different offsets merged (RT-HF3, fixed and tested live) | By design (tested) |

## 4. Rendering each value as canonical text (per engine)

What it does: each engine renders every value by the rules of CLAUDE.md 4.2
and 4.3. This is where every past false match lived.

| Risk | Odds | Kind |
|---|---|---|
| Rendering disagrees with the rules, covered types | **0 of 4,180** checks at scale 6 (4,154 exact, 26 required refusals), same at scale 2; see `docs/coverage.md` per type and engine | Measured |
| Rendering disagrees with the rules, values not in the sample | not measured; caught at run time by the sample check (section 8) | Proven bound at run time |
| Types outside the rules: BigQuery STRUCT/GEOGRAPHY/INTERVAL/RANGE and arrays of them or of JSON, nested lists, other engines' structs, maps, ranges, arrays of JSON or of unmapped types, geometry, XML (the sweep loads 4 such native types to watch their text for collisions: none found) | **not covered**: compared by text, named in every report. The real risk is not only false alarms: an engine's text for such a type can collide - BigQuery's `FORMAT('%t')` printed `['a, b']` like `['a', 'b']` and a NULL struct field like `'NULL'` (round H, fixed). On BigQuery every one is now injective by construction (STRUCT by `canonical_struct_text`, GEOGRAPHY as WKB, INTERVAL/RANGE components printed exactly); on other engines the fallback is the engine's own text cast, injective only as far as that cast is | Not quantifiable |
| A PostgreSQL extension type whose text prints two values alike (contrib `seg`) | **0** false matches between PostgreSQL sides: types outside pg_catalog and the known-exact list are compared by their binary send form too, or refused (round H) | By design (tested) |
| JSON across two engines whose normal forms differ (key order, duplicates, number spelling) | **0** false matches; such documents read as different (false alarms) and the run names both forms | By design (tested) |
| INTERVAL `1 day` against `24 hours`, TIMETZ `12:00+05:45` against `06:15+00`, inet `/32` against `/8` | **0** false matches: different stored values, compared as different even where the engine's `=` calls them equal (false alarms by design) | By design (tested) |
| A PostgreSQL array's shape (2x2 against its four elements flattened, `[0:1]={1,2}` against `{1,2}`) | **0** false matches: the dimensions are rendered with the elements, never flattened away | By design (tested) |
| MySQL text in a legacy character set (sjis, euckr, gbk, big5, cp125x, ucs2, …) holding a character with no Unicode mapping | **0** false matches — MySQL's conversion turns such characters into `?` (and ucs2 surrogates into NULL) silently, so each value is converted back and compared byte for byte with what is stored; one that does not survive renders `parity: not convertible from <charset> 0x<bytes>`. Convertible text renders exactly as before (round H, RT-HM1/HM2, false matches before; verified on 16 lossy charsets and MySQL 8.0, 8.4, 9.7) | By design (verified live) |
| MySQL display forms taken for values (ZEROFILL padding, YEAR 0 as `0000`, ENUM's index-0 error value as `''`) | **0** — rendered as their values (`5`, `0`) and the error value as its own marker (round H, RT-HM4/5/7/9) | By design (tested) |
| MySQL JSON scalars of MySQL's own types (`cast(date as json)`, binary) | **not covered**: they print as the JSON string of their text and compare equal to it, MySQL to MySQL only; documented (RT-HM8) | Not quantifiable |
| Values past an engine's range (FLOAT ≥ 2¹¹⁶, DECIMAL wider than the cast) | **0** — refused with an error naming side, column and value (Snowflake renders DECIMAL with `ROUND`, which never overflows, since round H) | By design (tested) |
| A Snowflake VARIANT double that TO_JSON prints with 16 digits, shared by another double | **0** false matches — the column is refused before the first checksum, naming side, column, key and path (one scan per JSON column); a double whose text reads back as itself has a text no other double has | By design (tested live) |

Findings this check produced and fixed: 8 (COV-1 to COV-8), two of them
false matches (Snowflake TIME; DuckDB TIMETZ, whose text printed the offset
+05:00:30 as +05:30) and one a refusal of ordinary values (SQL Server
sql_variant floats, -0.0 and negatives below 1e-99); the type rules found
one more (BigQuery SQL NULL equal to JSON `null`). Red-team round H on BigQuery: 11
findings, 3 of them false matches (ARRAY, STRUCT and GEOGRAPHY text) and one
a `--where` bracket escape in the scanner every engine shares, all fixed;
BigQuery ARRAY now follows the list rule. Red-team round G (settings, versions): 14
problems, 8 of them false matches, all fixed. Round H, MySQL: 18 findings,
4 of them false matches (legacy character sets, ucs2 surrogates, column-level
grants, the ENUM error value), all fixed; open are a Ctrl-C that cannot
cancel at `max_user_connections` (now reported), JSON typed scalars
(documented) and casefold column matching (engine, RT-HM18). Round H on SQL Server: 11
problems, 4 of them false matches (dynamic data masking, row-level security
without a caveat, spatial SRID, invalid double-byte text), all fixed. Round H on Snowflake: 11 problems
(RT-HF1 to HF11), 6 of them false matches, all fixed or refused, plus a
secret leak in the test suite's failure reports. Earlier campaign: about 30
problems, 11 false matches on ordinary data, all fixed (CHANGELOG 0.4.0).

## 5. Hashing rows and summing per key range

What it does: each row's text becomes a 60-bit MD5 prefix; each side sums
them per key range in a wide type and counts rows.

| Risk | Odds | Kind |
|---|---|---|
| A range holding a real difference sums to the same value | at most **2⁻⁶⁰ ≈ 8.67 × 10⁻¹⁹** per differing range: the sums differ by at least one 60-bit hash that is uniform and independent of the rest | Proven bound (data not crafted to collide) |
| Sum overflow | **0** — summed in NUMERIC / DECIMAL(38) / HUGEINT / NUMBER(38) | By design |
| Crafted collision (8 rows chosen to cancel) | **not bounded** — a few seconds of computation, no secret needed | Not quantifiable |

## 6. Walking the key space (bisection)

| Risk | Odds | Kind |
|---|---|---|
| A key falls in no bucket or two | **0** — bucket arithmetic is exact (widened integers, exact division on every engine; a Snowflake NUMBER(38,0) span too wide for 38 digits compares the key with its precomputed boundaries), property-tested against `bucket_bounds` | By design (tested) |
| An empty range on one side is skipped | **0** — empty sums are 0 on both sides | By design (tested) |
| A difference vanishes between two levels (rows changed between the queries) | **0** false matches — each level's buckets must add up, in rows and checksum, to the level above, or the range is unexplained | By design (tested) |

## 7. Downloading differing ranges and comparing rows

| Risk | Odds | Kind |
|---|---|---|
| A driver changes values on download (dropped NULs, rounding) | **0** false matches — a range whose checksums differ but whose rows compare equal is "unexplained", never identical | By design (tested) |
| Rows lost on download | **0** — row counts are checked against the checksum counts | By design (tested) |
| `--max-diffs` stops the walk | **0** — the result is marked partial, never identical | By design (tested) |

## 8. The independent sample check

What it does: after the walk, about `--verify-sample` rows (default 10,000;
all rows of a smaller table), chosen by a hash of the key with a fresh random
residue each run, are fetched as raw driver values on both sides and compared
in Python by the rules (`parity/reference.py`), with none of the engine SQL.
Any disagreement means not identical, and so does a check that could not
run (its fetch failed): the run asked for a second method and did not get
one. With `--verify-sample 0` the check is off by choice, the report says so,
and the rendering-error row of the summary has no bound for that run - only
the checksum figures apply.

Chance that a rendering error affecting a share p of rows is **missed** by
n sampled rows: (1 − p)ⁿ.

| Share of rows affected | n = 1,000 | n = 10,000 (default) | n = 100,000 |
|---|---|---|---|
| 1% | 4.3 × 10⁻⁵ | 2.2 × 10⁻⁴⁴ | below 10⁻⁴³⁶ |
| 0.1% | 0.368 | 4.5 × 10⁻⁵ | 3.5 × 10⁻⁴⁴ |
| 0.01% | 0.905 | 0.368 | 4.5 × 10⁻⁵ |
| 0.001% | 0.990 | 0.905 | 0.368 |

Equivalently, after n rows with no disagreement, fewer than 1 − 0.001^(1/n)
of rows can carry a hidden error, at 99.9% confidence (the default since
2026-11-03, chosen by the project owner; 99% before, with n = 1,000):

| n | share | of 10M rows, at most |
|---|---|---|
| 1,000 | 0.6884% | 68,840 |
| 10,000 (default) | 0.0691% | 6,905 |
| 100,000 | 0.0069% | 691 |
| 1,000,000 | 0.00069% | 69 |
| whole table | 0 | 0 |

With `--where` the sample is drawn from the filtered rows on each side, n
and the bound are of the filtered rows, and nothing is said about rows the
filter left out.

Assumptions: the key hash spreads keys evenly over residues (MD5), the
residue is random (`random.SystemRandom`), and the data is not crafted
against the sample. The bound covers the run's `covered_columns`; columns
listed as uncovered carry none. Driver quirks that would make the check
disagree falsely are worked around per engine and verified live (MySQL
FLOAT and BIT width; MySQL SET, ENUM and legacy-charset text, read as hex
facts because the connector returns a SET as a Python set, the ENUM error
value as `''` and an unconvertible character as `?`; MySQL zero dates, read
as text because the connector returns them as None; SQL Server NULs, 7-digit times and sql_variant base
types, PostgreSQL and DuckDB infinite and BC dates and interval months,
DuckDB TIMETZ offsets). Measured: on the full live suite (1,620 tests, default sample 10,000) the check
raised no false disagreement; every disagreement it raised was a planted
walk defect or a table changed mid-run.

## 9. What no figure covers

- **Bugs not yet found.** Evidence instead of a number: the coverage map
  (section 4), the full live test suite, two red-team campaigns (the
  second on server settings and versions), and the sample check on every
  run.
- **Tables changing during a run** without one snapshot (section 1).
- **Rows outside a `--where` filter**: never compared (section 2b).
- **Data changed since a `--resume`d pair's earlier run** (section 2c).
- **Crafted data** against the checksum or the sample (sections 5, 8).
- **Uncovered column types**, named in each report (section 4).
- **The engines themselves**: a database or driver bug that returns wrong
  data to both methods alike.

---

## Keeping this file current

Update this file after **every** change to the tool, in the same commit:

1. Re-check each section the change touches; change the figures and the
   kind of number where they moved.
2. If the change touches rendering, regenerate `docs/coverage.md`
   (`tests/conformance/report.py`) and copy its summary numbers into
   sections *Summary* and 4.
3. Refresh the fingerprint: `python tests/test_risk_doc.py --stamp`.

`tests/test_risk_doc.py` fails when the fingerprint does not match
`src/parity/`, when a constant quoted here (2⁻⁶⁰, 99.9%, the default sample
size) differs from the code, or when the coverage numbers here differ from
`docs/coverage.md`.
