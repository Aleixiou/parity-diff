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
| BigQuery | `cast(concat('0x', substr(to_hex(md5(<text>)), 1, 15)) as int64)` |
| SQL Server | `convert(bigint, convert(varbinary(8), '0' + left(convert(varchar(32), hashbytes('MD5', <utf8>), 2), 15), 2))` |

All six return `648541476951500027` for input `'abc'` - through six different
paths (bit-cast, hex-cast, CONV, Snowflake's top-64-bits-then-drop-4,
BigQuery's hex-cast of `to_hex`, since its `md5` returns BYTES, and SQL
Server's style-2 hex read back as 8 bytes behind a `0` nibble), which
is exactly why a test pins the constant on each. On SQL Server `<utf8>` is
`cast(cast(<text> as nvarchar(max)) collate Latin1_General_100_BIN2_UTF8 as
varchar(max))`: SQL Server holds Unicode as UTF-16, and hashing that disagrees
with every other engine on every row; a `varchar` under a `_UTF8` collation
*is* UTF-8. Verified equal to Python's `md5(s.encode())` for accents, CJK,
emoji and combining marks, in a UTF-8 and a Latin1 database alike, for
`varchar` and `nvarchar` input. UTF-8 collations exist from SQL Server 2019, so
older servers are refused. Snowflake has neither a bit
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
| FLOAT (finite) | see **FLOAT canonical form** below | see below |
| FLOAT (non-finite) | `case when isinf(c) … 'Infinity'/'-Infinity' when isnan(c) then 'NaN'` | `case when c = 'Infinity'::float8 … when c = 'NaN'::float8 then 'NaN'` |
| BOOLEAN | `case when c then 'true' when not c then 'false' end` | `case when c then 'true' when not c then 'false' end` |
| STRING | `cast(c as varchar)` | `(c)::text` |
| DATE | `strftime(c, '%Y-%m-%d')` | `to_char(c, 'YYYY-MM-DD')` |
| TIMESTAMP | `strftime(c, '%Y-%m-%d %H:%M:%S.%f')` | `to_char(c, 'YYYY-MM-DD HH24:MI:SS.US')` |

Verified agreeing values include: `1.5 → "1.500000"`, `-0.125 → "-0.125000"`,
`1/3 → "0.333333"`, `2024-02-29 13:04:05.123456` round-trips exactly,
`2024-01-01 00:00:00 → "2024-01-01 00:00:00.000000"`, and full Unicode strings.

**BigQuery** (verified live 2026-11-02 against DuckDB, byte-for-byte over 5,000
mixed-type rows; `tests/test_bigquery.py`). It has no `concat_ws`, and its
`cast(numeric as string)` drops trailing zeros, so it needs its own spellings:

| Logical type | BigQuery |
|---|---|
| DECIMAL | `format('%.6f', round(c, 6))` (round first, or `format` prints `-0.000000`) |
| FLOAT (finite) | see **FLOAT canonical form** below (built from `cast(c as bignumeric)`) |
| DATE | `format_date('%E4Y-%m-%d', c)` (`%Y` prints year 999 as `999`) |
| DATETIME / TIMESTAMP | `format_datetime('%E4Y-%m-%d %H:%M:%E6S', c)` / `format_timestamp(..., c, 'UTC')` |
| Row text | `array_to_string([...], chr(31))`, flat - byte-identical to nested `concat_ws` at 130 columns |
| NULL sentinel | `concat(chr(92), 'N')` |

BigQuery's instant type is plainly `TIMESTAMP` (its naive one is `DATETIME`), the
opposite of what that name means elsewhere, so the dialect sets
`Column.tz_aware` explicitly instead of letting the engine infer it from the name.

**SQL Server** (verified live 2026-11-02 against SQL Server 2022 CU27 in a
Linux container, byte-for-byte with DuckDB over 5,000 mixed-type rows, in a
UTF-8, a Latin1 and a case-sensitive database; `tests/test_mssql.py`). Every
value is `nvarchar(max)` - see the CONCAT trap in 4.5 - and is converted to
UTF-8 only once, at the hash:

| SQL Server type | Expression (inside `coalesce(..., N'\N')`) |
|---|---|
| `tinyint` … `bigint` | `convert(nvarchar(max), c)` |
| `decimal`, `numeric`, `money`, `smallmoney` | `convert(nvarchar(max), cast(c as decimal(38,6)))` - numeric to numeric rounds half away from zero; money converts exactly |
| `float`, `real` | see **FLOAT canonical form** below |
| `bit` | `case when c = 1 then N'true' when c = 0 then N'false' end` |
| `date` | `convert(nvarchar(max), c, 23)` (pads year 0001 / 0999) |
| `datetime2`, `smalldatetime` | `left(convert(nvarchar(max), cast(c as datetime2(7)), 121), 26)` - the 7th digit **truncated** |
| `datetime` | `convert(nvarchar(max), c, 121) + N'000'` - its displayed `.003`/`.007` |
| `datetimeoffset` | as datetime2, of `switchoffset(c, '+00:00')`; `tz_aware=True` |
| `char`, `nchar` | `rtrim(cast(c as nvarchar(max)))` |
| `uniqueidentifier` | `lower(convert(nvarchar(max), c))` |
| other strings | `cast(c as nvarchar(max))` |
| `timestamp` (rowversion), binary | `convert(nvarchar(max), convert(varbinary(max), c), 1)` - UNKNOWN, 0x-hex |
| separator / NULL sentinel | `nchar(31)` / `N'\N'` (T-SQL processes no backslash escapes) |

Decisions, not just spellings:

- **`datetime2(7)` truncates to microseconds.** That is how a microsecond
  reader (Python's datetime, an ODBC export) carries the value, so it is what
  most migrations hold, and it never rolls `23:59:59.9999995` into the next day
  as `cast(... as datetime2(6))` (which rounds) would. A difference below 1 µs is
  therefore invisible, and `caveats()` says so on every run comparing a
  precision-7 column. The boundary is pinned by a test.
- **`datetime` renders its display, not its tick.** It counts 1/300 s;
  `cast(datetime as datetime2)` gives `.0033333` under compatibility level 130+,
  which nobody exports. The displayed milliseconds are independent of the
  compatibility level.
- **`char(n)` is right-trimmed.** PostgreSQL's `bpchar::text` and MySQL's CHAR
  strip the padding, DuckDB has none, and `char(n)` cannot tell `abc` from
  `abc ` itself, so nothing is lost.
- **`timestamp` is rowversion** and `bit` is boolean: `map_type_mssql` exists
  because the shared map would read the first as a point in time, and adding
  `bit` there would turn PostgreSQL's bit strings into booleans.
- SQL Server floats cannot hold Infinity or NaN, so no tokens are emitted.

**Infinity and NaN must be special-cased for FLOAT.** DuckDB cannot cast them
to DECIMAL at all — `Could not cast value inf to DECIMAL(38,6)` kills the whole
diff — while PostgreSQL renders them happily. Any division by zero produces one,
so this is ordinary data, not an exotic case. Both dialects now emit the fixed
tokens `Infinity`, `-Infinity`, `NaN`. Note PostgreSQL cannot detect NaN with
`c <> c`: it deliberately treats NaN as equal to itself, unlike IEEE 754, so the
test must be `c = 'NaN'::float8`. DECIMAL needs no guard — PostgreSQL `numeric`
renders NaN as text on its own and DuckDB `DECIMAL` cannot hold one.

**A DECIMAL too wide for the rendering cast must raise, never clamp**
(verified 2026-11-02 against MySQL 8.0.42, PostgreSQL 18.4, DuckDB 1.5.5 and
live BigQuery; `tests/test_decimal_range.py`). MySQL's `CAST(x AS
DECIMAL(p,s))` does not fail when `x` does not fit: inside a SELECT, in every
sql_mode including `''` and the strict default, it returns the type's maximum
and leaves only warning 1264. MySQL used to render DECIMAL as
`decimal(38,6)`, so every value of 1e32 or more became
`99999999999999999999999999999999.999999` (negatives the mirror image): 1e32
and 5e40 on MySQL, against a PostgreSQL `numeric` or a DuckDB `decimal(38,6)`
really holding that maximum, diffed as **identical** - a false match,
reproduced end to end MySQL↔PostgreSQL, MySQL↔DuckDB and MySQL↔MySQL. At
`--float-scale 20` the same cast clamped from 1e18. The MySQL DECIMAL
expression is now

```
case when abs(c) > 9…9.9…9            -- 10^(65-s) - 10^-s: decimal(65,s)'s maximum
     then cast(uuid_to_bin(concat('parity: DECIMAL value ', c, ' in column …')) as char)
     else cast(cast(c as decimal(65,s)) as char) end
```

- Widening 38 → 65 (MySQL's maximum) renders values up to 65 - s integer
  digits exactly. The text never depended on the cast's precision, so every
  value the old cast held renders byte-identically - verified at scales 0, 6,
  20 and 30 - and **no checksum moves**.
- The guard is exact: no MySQL DECIMAL has more than 65 digits, and a value
  could only round across the bound with 66. So it fires on exactly the
  values the cast would clamp.
- `UUID_TO_BIN` of a non-UUID raises error 1411 even in a SELECT (most errors
  there are downgraded to warnings) and quotes its argument, cut at 128
  characters - hence the value first. `MySQLDialect.query` re-raises it as an
  error naming the side, the column and the exact value.
- MySQL's DECIMAL scale tops out at 30; above it the dialect refuses before any
  query, naming side and column.

The other engines do not clamp. DuckDB's `decimal(38,s)` cast raises `value is
out of range` (and DuckDB columns hold at most 38 digits anyway). PostgreSQL's
`round(numeric, s)` is exact at any magnitude (it caps the scale at 16,383,
past any stored value's scale). BigQuery's `ROUND` raises `numeric overflow`
when rounding carries past NUMERIC's or BIGNUMERIC's maximum, and its `FORMAT`
prints NUMERIC exactly. Snowflake, by its documentation only, raises "Numeric
value out of range" when a cast to `number(38,s)` does not fit.

**FLOAT canonical form** (verified 2026-11-02 against PostgreSQL 18.4, DuckDB
1.5.5, MySQL 8.0.42 and live BigQuery; `tests/test_float_canonical.py`,
`demo/float_agreement.py`). The definition is the Python function
`canonical_float_text` in `dialects/base.py`; every engine must equal it byte
for byte:

- **|x| < 2^53:** the *shortest round-trip decimal* of the double, rounded half
  away from zero to the float scale. A function of the IEEE value alone, and
  the value the user typed, so a DECIMAL `2.0000005` and a DOUBLE `2.0000005`
  both render `2.000001`.
- **2^53 ≤ |x| < 2^116:** every such double is an integer; render it exactly.
  Split as `hi = trunc(x / 2^53)` and `lo = x - hi * 2^53` (both exact in
  double, both fit a signed 64-bit integer) and recombine in a decimal type.
- **|x| ≥ 2^116 (~8.3e34):** refused loudly with an error naming the value.
- Single-precision columns are widened to double first (exact everywhere).
  Rendered as float4, the engines' shortest reprs differ.
- `--float-scale` is honoured up to `FLOAT_MAX_SCALE` (20) for FLOAT columns
  and refused past it: MySQL's DECIMAL cast clamps silently beyond, and
  BigQuery's BIGNUMERIC has 38 places. Verified at scales 0, 2, 6, 9 and 20.

| Engine | Finite FLOAT below 2^53 |
|---|---|
| PostgreSQL | `round(cast(cast(c as text) as numeric), 6)`, with `extra_float_digits = 1` pinned at connect; FLOAT refused on servers < 12 |
| DuckDB | `abs(c) < 1e-7` → `'0.000000'`, else `cast(cast(cast(c as varchar) as decimal(38,6)) as varchar)` |
| MySQL | `cast(cast(c as decimal(38,6)) as char)` (unchanged: its DECIMAL cast goes through dtoa's shortest repr) |
| BigQuery | the first of `round(cast(c as bignumeric), n-1-e, 'ROUND_HALF_EVEN')` for n = 15, 16, 17 significant digits that casts back to `c`, then `round(.., 6)` |
| SQL Server | the same construction from `cast(c as decimal(38,37))` below 1 and `cast(c as decimal(38,21))` up to 2^53, with ties to even built by hand (`ROUND` is half away from zero) at 16 and 17 digits; `--float-scale` capped at 18 |
| Snowflake | the same two bands from `cast(c as number(38,q))`, candidates `round(.., n-1-e, 'HALF_TO_EVEN')` |

Every one of these was found by measurement, not by reading the docs:

- **PostgreSQL `float8::numeric` keeps 15 significant digits**, so
  `round(c::numeric, 6)` turned 1e15+0.5 into `1000000000000000.000000` - the
  exact text DuckDB gives 1e15. That is a *false match*, not just a false
  alarm. `float8::text` is the shortest repr only on PostgreSQL 12+ with
  `extra_float_digits > 0`, hence the pin and the version refusal.
- **DuckDB `cast(double as decimal(38,6))` computes `x * 10^6` in double**:
  1e15+0.5 rendered `1000000000000000.524288`. Its string-to-decimal cast is
  correct *except* in exponent notation with a large negative exponent, which
  it rounds on the leading digit alone (`'6.4e-10'` → `0.000001`); values under
  10^-(s+1) are zero at any reading, so they short-circuit before the cast.
- **PostgreSQL's shortest repr (Ryu) excludes the rounding-interval boundaries
  that dtoa (MySQL, DuckDB, Python) includes.** Below 2^53 a boundary always
  has ≥ 17 significant digits, so the two agree; above 2^54 boundaries are
  short integers and they do not (`3.8809635010537424e16` vs
  `3.880963501053742e16`). Hence exact integers from 2^53.
- **BigQuery has no shortest repr.** `CAST(x AS STRING)` is `%.15g` when that
  round-trips and `%.17g` otherwise, never 16 digits; `FORMAT('%.Ng')` is not
  correctly rounded (`%.17g` of 2^30 + 1/128 printed `...0078124`, exactly
  `...0078125`). `CAST(x AS BIGNUMERIC)` *is* exact to 38 places, half up, so
  the shortest repr is built from it. Candidates break ties to even, as Ryu
  and dtoa do: 2^50 + 0.25 sits exactly between two 17-digit candidates that
  both round-trip.
- **MySQL's CAST to an integer type clamps silently** on overflow, so two
  different huge doubles would compare equal; its out-of-range branch raises
  through `UUID_TO_BIN` like the DECIMAL guard, carrying the value and the
  column, and `MySQLDialect.query` re-raises it naming the side. (It used to
  call `exp(abs(c))` - error 1690, which named nothing a user could act on.)
  Every engine's refusal past 2^116 now names side and column (RT-E10).
- **SQL Server prints no shortest repr either** (style 3 is always 17 digits),
  but `cast(float as decimal(38, q))` is the exact binary value rounded half up
  and decimal to float is correctly rounded - both verified on every sample
  value - so the BigQuery construction carries over. Its decimal arithmetic
  shortens the scale of `d + d` by one place, which is why the tie test needs
  the digit past the candidate and why scales above 18 are refused. And **its
  string-to-float parse is not correctly rounded**: `'-5.795195809241054e+16'`
  arrives one ulp off and subnormals arrive as 0, so test doubles must travel
  as bound parameters, never literals.
- **Snowflake prints no shortest repr either** (verified live 2026-11-03): a
  varchar cast is 15 significant digits, `TO_VARCHAR(x, 'TME')` 16, and the old
  `cast(c as number(38,6))` rounded the exact binary value (215 of the 1,565
  sample doubles disagreed). `cast(float as number(38, q))` *is* the exact value
  rounded half up, and `ROUND` takes a per-row scale and a `'HALF_TO_EVEN'`
  mode, so the BigQuery construction carries over with no hand-built tie test.
  NUMBER holds 38 digits, hence SQL Server's two bands (37 places below 1, 21
  up to 2^53). Agrees with the spec on 6,565 doubles at scales 0, 2, 6, 9, 20.

Before this form, PostgreSQL and DuckDB disagreed on 347 of the 1,565 sample
doubles, PostgreSQL and MySQL on 296, DuckDB and MySQL on 289; now every pair
of the four agrees on all of them at scales 0, 2, 6 and 9, and on 20,000
further random doubles (10,000 on BigQuery). None of the 1,217 values on which
all four already agreed changed text.

**Known, deliberate limitation:** floats and decimals are compared at 6 decimal
places. Document this loudly in the README — silent precision assumptions are
exactly how a parity tool loses trust. Make it configurable later
(`--float-scale`), never silently variable.

### 4.3 NULL handling and field separation

- NULL renders as the literal sentinel `\N`, never SQL NULL:
  `coalesce(<expr>, chr(92) || 'N')`. An un-coalesced NULL would poison the
  whole concatenation and silently mask differences. The backslash is always
  built from its code point (`chr(92)`, MySQL `char(92 using utf8mb4)`, SQL
  Server `nchar(92)`), never written as `'\N'`: under PostgreSQL
  `standard_conforming_strings = off` that literal is `'N'` (RT-D1).
- **Text is escaped, or the sentinel and the separator are forgeable**
  (RT-A1–A4, B1–B3). Unescaped, the four bytes of the string `\N` equalled
  NULL, and a chr(31) inside a value moved a field boundary, so `('a\x1fb',
  'c')` equalled `('a', 'b\x1fc')` - false matches on ordinary data, and the
  same collision merged two composite keys. Every STRING value and every
  unmapped-type fallback cast therefore goes through `Dialect.escape_text`:
  double each backslash first, then write chr(31) as `\s`. No field can then
  equal `\N` or contain chr(31), and text without either renders exactly as
  before, so no ordinary checksum moved. The Python spec is
  `escape_canonical_text`. **`replace()` follows collation**: it runs under
  `collate "C"` on PostgreSQL (a nondeterministic ICU collation refuses
  substring search), `Latin1_General_BIN2` on SQL Server (Windows collations
  treat chr(31) as ignorable), and `collate(x, '')` on BigQuery - verified
  live that BigQuery's `REPLACE` under `und:ci` silently leaves chr(31) in
  place.
- **The BOOLEAN expression must use `when not c`, never `else`.** With
  `case when c then 'true' else 'false' end` a NULL boolean falls into the
  `else` branch and renders `'false'`, so the `coalesce` never fires and a NULL
  on one side compares *equal* to a FALSE on the other. Both engines agreed on
  that wrong answer, so a cross-engine equality test could not see it - it was
  caught only by a test that planted a NULL-versus-FALSE difference and
  asserted the tool found it. This is the same bug class as NULL versus `''`.
- Fields join with `concat_ws(chr(31), ...)` — ASCII Unit Separator, spelled
  identically in every engine. "Effectively never occurs" was not good enough:
  it is escaped out of every text value (above), so it cannot occur.

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

**SQL Server's traps** (verified live against SQL Server 2022; each is pinned by
a test that plants the difference the trap would hide):

- **`CONCAT_WS` truncates silently.** Without a `max`-typed argument its result
  is at most 4,000 characters (8,000 bytes): three `nvarchar(4000)` columns
  joined came back as 4,000 characters, not 12,002, so a difference past the
  cut would vanish from the checksum - a false match. Every normalized value
  is `nvarchar(max)`; a change at character 9,500 of a row's text is found.
- **`CONCAT_WS` refuses one value** ("requires 3 to 254 arguments"). One part
  reaches `_concat` for a key-only table, a single-column hashed key, and the
  remainder group of a 65-part row; the dialect returns it as itself.
- **No `GROUP BY 1`, and `COUNT` is 32-bit.** T-SQL accepts neither an ordinal
  nor an alias in GROUP BY, and `count` raises past 2^31 - 1 rows. The base
  class spells both through hooks (`group_by`, `count`) whose defaults emit
  the old SQL byte for byte - checked by generating every shared query of all
  five other dialects before and after.
- **Text equality is the collation's.** Case-insensitive by default, and blind
  to trailing spaces under every collation: `'a'`, `'A'`, `'a '` count as one
  distinct value, so a unique text key would be refused as duplicated. The
  uniqueness count is over `hashbytes('SHA2_256', <utf8>)` instead
  (`count_distinct_key` hook). Rows are matched by their text in Python, so
  nothing else depended on server-side text equality.
- **Catalog names follow the database collation.** In a case-sensitive
  database `information_schema.columns` is "Invalid object name"; it must be
  `INFORMATION_SCHEMA.COLUMNS` with upper-case column names, so the dialect
  overrides `columns()` and the near-miss query.
- **`int_div` divides an exact multiple: `(n - n % d) / d`.** The design notes
  feared decimal `/` would *round* a quotient like `q.9999996` up to the next
  bucket. On SQL Server 2022 it truncates at the reduced scale instead
  (`2/3 = 0.666666`), so the naive form placed every boundary key correctly too
  - but that is undocumented behaviour, and the modulo form is exact whatever
  the rounding mode. Pinned at every bucket boundary of ranges up to 2^64 wide.
- An integer literal past bigint (the 2^64 span) parses as `numeric`, so no
  `int_literal` override is needed.

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

BigQuery has no multi-statement snapshot, so every read of a **base table** uses
`FOR SYSTEM_TIME AS OF` the server time captured by the first metadata query -
verified live: a row inserted mid-walk stays invisible. BigQuery silently ignores
time travel on a **view** (verified: it returned the live count), so views are read
live at every level, as on Snowflake (READ COMMITTED only). Documented in the README,
pinned by a live test. The dialect also requests the `bigquery.readonly` OAuth scope,
so the server itself refuses any write - verified for INSERT, DELETE, CREATE, DROP.

SQL Server gives one snapshot only where the database has
`ALLOW_SNAPSHOT_ISOLATION ON`, which the dialect reads from `sys.databases` at
connect. Then it sets SNAPSHOT while autocommit is on and turns autocommit off,
so the walk is one transaction - verified live: a row inserted and another
updated mid-walk stay invisible, a fresh dialect sees both, and the writer is
not blocked. Otherwise it stays at READ COMMITTED and `caveats()` puts a warning
in every run's output. It **never** falls back to REPEATABLE READ or
SERIALIZABLE, whose shared locks are held to the end of the transaction and
would stall writers on a live legacy system for the whole diff, and never to
NOLOCK, whose dirty reads invent differences. Verified: with snapshot off the
session reports isolation level 2 (read committed) and a concurrent UPDATE
completes under a 5 s lock timeout. Two SQL Server specifics: a table created or
altered after the snapshot began is not invisible, as on PostgreSQL - reading it
raises error 3961, loudly; and there is no read-only session
(`ApplicationIntent=ReadOnly` does not enforce one on a primary), so the dialect
refuses any non-SELECT in `query`, never commits, rolls back in `close`, and the
docs require a `db_datareader`-only login - verified live that such a login runs
the diff and is refused a DELETE by the server.

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

### 4.11 Red-team findings and the rules they left (2026-11-03)

A campaign of seven adversarial angles (values, keys, schema, live sessions,
CLI, differential fuzzing, mutation testing; ~600 tests in `tests/redteam/`,
each confirmed bug a strict xfail until fixed, then a regression test) found
the false matches listed in the CHANGELOG. Rules that came out of it:

- **Keys are exact, never at the float scale** (`normalize_key`). DECIMAL keys
  render as their minimal exact text (`1.50` → `1.5`, `42.000` → `42`, so an
  integral decimal key matches an integer one); FLOAT keys are refused (no
  rendering of a double is exact *and* identical everywhere); a sub-µs
  timestamp key renders all its digits only when it has them (DuckDB
  `TIMESTAMP_NS`, SQL Server `datetime2(7)` as 7 digits + `00`); a NULL in any
  hashed-key column is refused, as for an integer key.
- **Key uniqueness is counted by bytes, never under a collation.** MySQL
  `utf8mb4_0900_ai_ci` ignores chr(31) and folds case, so `(5, 944)` and
  `(59, 44)` counted as one key (found by the fuzzer); PostgreSQL ICU
  nondeterministic collations fold case. MySQL counts `cast(identity as
  binary)`, PostgreSQL `collate "C"`, SQL Server SHA-256.
- **A one-sided column is a schema difference** (`DiffResult.schema_differences`,
  exit 1) unless set aside with `--exclude`/`--columns`. It used to be a
  warning under an exit-0 "no differences" headline, which `--quiet` hid.
- **An unexplained checksum mismatch is never identical**
  (`DiffResult.unexplained`). If a bucket's checksums differ but its
  downloaded rows compare equal (SQL Server's driver dropped trailing NULs), the
  fetch path is lossy and the verdict must say so.
- **Every dialect call in the walk names its side and table** (`_on_side` in
  engine.py), with the driver's exception chained as `__cause__`.
- **PostgreSQL:** tables are always schema-qualified (an unqualified name was
  introspected in `public` but read through `search_path`); `search_path` is
  pinned to `pg_catalog` so no user function can shadow a built-in; infinite
  dates render `infinity`/`-infinity` (what DuckDB's `strftime` prints - `to_char`
  returned NULL) and BC dates use DuckDB's astronomical years (`0000`, `-43`);
  the full set of session pins is `SESSION_PINS` in `postgres_dialect.py`,
  including `idle_in_transaction_session_timeout = 0` and (17+)
  `transaction_timeout = 0`, without which a role default kills the held
  snapshot while the other side is still working.
- **MySQL:** binary renders `\x` + lower hex (PostgreSQL's `bytea` text) -
  `cast(binary as char)` was NULL for any non-UTF-8 bytes; the session is
  `READ ONLY` with `sql_mode` pinned; a row past `max_allowed_packet` makes
  `concat_ws` return NULL, so `row_hash` falls back to hashing per-column md5s
  rather than letting the row drop out of `sum()`; non-InnoDB tables get a
  no-snapshot caveat - views too, through `view_table_usage` (8.0.13+), and
  conservatively when the base tables cannot be listed (RT-HM17).
  **Round H (2026-11-04, verified on 8.0.46, 8.0.42 native, 9.7.4):**
  `cast(c as char)` from a legacy character set turns every character with no
  Unicode mapping into `?`/U+FFFD and a ucs2 surrogate into SQL NULL - no
  error, any sql_mode - so different stored characters, a real `?` and NULL
  all compared equal (RT-HM1/HM2, false matches). Outside
  `_LOSSLESS_CHARSETS` (utf8mb4/mb3, utf16, latin1: verified total and
  one-to-one) a value converts back to its own charset and is compared byte
  for byte; equal renders exactly as before (**no checksum of convertible
  text moves**), unequal renders `parity: not convertible from <cs> 0x<HEX>`
  - a marker, not a refusal, like SQL Server's unpaired surrogates, so keys
  stay distinct. Column-level grants hide columns from information_schema;
  `SELECT * ... LIMIT 0` fails exactly then (1142/1143) and the table is
  refused (RT-HM3, false match). A `--where` holding `FOR SHARE`/`FOR
  UPDATE`/`LOCK IN SHARE MODE`/`INTO`/`OUTFILE`/`DUMPFILE` outside quotes is
  refused: READ ONLY does not refuse a shared locking read (RT-HM12).
  ZEROFILL and YEAR render `c + 0` (display padding is not the value;
  checksums move for ZEROFILL columns and YEAR 0 only); ENUM index 0 renders
  `parity: invalid ENUM value (index 0)`, never `''` (RT-HM9); `VECTOR` is
  hex of its bytes. The sample check reads ENUM/SET/legacy-charset text as
  hex facts (index, stored, round-tripped, utf8mb4 bytes) and DATE/TIMESTAMP
  as MySQL's text (the connector returns None for zero dates). MariaDB is
  refused by name; `lc_messages` is pinned to `en_US` and the error patterns
  no longer need English. Open: a cancel at `max_user_connections` cannot
  KILL (the C connector has no socket to interrupt) and now warns instead of
  failing silently (RT-HM16); JSON typed scalars (`cast(date as json)`)
  print as strings (RT-HM8, documented); casefold column matching (RT-HM18,
  engine).
- **SQL Server:** unpaired surrogates render as a marker plus their bytes
  (UTF-8 conversion turned them all into U+FFFD); downloads fetch the canonical
  text as `varbinary` UTF-8 bytes; `sql_variant` renders `<BaseType>:<exact>`;
  the SELECT guard accepts one plain SELECT only.
- **DuckDB:** extension autoinstall/autoload are off (no network, §6);
  HUGEINT key spans past 2^127 bucket exactly in UHUGEINT.
- **Snowflake changes are verified live** (2026-11-03, `tests/test_snowflake.py`):
  UTC rendering of `TIMESTAMP_TZ`/`LTZ`, `tz_aware`, the FF6 caveat, FF9 keys,
  exact DECIMAL keys, `VARIANT` type tag, schema-qualified tables, the exact
  `int_div` (plain `/` rounds the quotient to 6 places - confirmed), escaping
  under an `en-ci` collation. **Telemetry:** `CLIENT_TELEMETRY_ENABLED` is a
  client parameter (the server does not list it); connectors probe cloud
  metadata at login unless `SNOWFLAKE_DISABLE_PLATFORM_DETECTION` is set (the
  dialect sets it). Connector 5.x (a Rust core) flushes a session-telemetry
  batch at logout that no switch in 5.1.0 stops, so the extra pins
  `>=4.0,<5`; on 4.8.1 a live test confirms only login, query and logout
  endpoints are contacted (`test_a_session_contacts_only_the_account_...`).
  URL parameters are whitelisted (`URL_PARAMS`), as on MySQL, SQL Server and
  BigQuery. **Key-pair sign-in** (`?private_key_file=`, passphrase only from
  `PARITY_SNOWFLAKE_KEY_PASSPHRASE[_A|_B]`, never the URL): parity loads the
  PEM itself and passes DER bytes with `authenticator=SNOWFLAKE_JWT` and no
  password, through the same `connect_kwargs` that carries the telemetry pins
  (pinned offline in `test_snowflake_connect_offline.py`; live test gated on
  `PARITY_TEST_SNOWFLAKE_KEYPAIR`). No browser or token authenticator.
- **The walk is checked by a second method** (`engine._verify`,
  `parity/reference.py`). About `verify_sample` rows, chosen by a hash of the
  key text (`fetch_sample`), are fetched as raw driver values and compared in
  Python by the rules - no `normalize` SQL - and must agree with the walk on
  every sampled key; a disagreement is a failure and the result is not
  identical. A check that was asked for and **could not run**
  (`verification.error`) is not identical either (exit 1, "not proven"): only
  `verify_sample=0` rests the verdict on the checksums alone, and the report
  then says the check was off. It compares by canonical text (`canonical_text`), not by typed
  value, because that is the walk's equality. Driver workarounds live in
  `sample_select`/`sample_value` per dialect, never in the reference. The
  test suite's autouse fixture `_sample_check_never_disagrees` fails any test
  whose comparison the check contradicts, so every comparison in the suite
  also tests the check; tests that plant a walk defect opt out with
  `@pytest.mark.sample_check_may_disagree`.
- **The coverage map is the evidence, not a claim** (`tests/conformance/`,
  `docs/coverage.md`, regenerate with `tests/conformance/report.py`): every
  logical type on every engine against `parity.reference`, at scales 6 and 2.
  A new disagreement fails the sweep; a confirmed one is a COV-<n> strict
  xfail until fixed. Saved runs (`tests/conformance/results/`) are not
  committed: they can carry account identifiers.
- **Sub-microsecond rule (COV-2):** `Column.sub_micro` is set by the engine
  only when both sides' `holds_sub_micro` is true; then a value with digits
  past microseconds renders nine digits on every engine, otherwise they are
  truncated and the dialect's caveat says so.
- **TIME, BINARY, JSON** (`canonical_time_text`, `canonical_binary_text`,
  `canonical_json_text` in base.py): TIME `HH:MM:SS.ffffff`; BINARY `\\x` +
  lower hex; JSON the engine's own normal form (jsonb on PostgreSQL, MySQL's
  binary JSON, BigQuery/Snowflake by code point, DuckDB verbatim text) in one
  layout - `{"a": 1, "b": [1, 2]}` - and one string escaping, with every
  backslash in that SQL built by `spell_sql` (RT-D1). Cross-engine JSON is
  exact only where normal forms coincide; `json_semantics` names both forms
  in a warning otherwise. A NULL JSON must render the sentinel, never `null`
  (BigQuery's TO_JSON_STRING(NULL) is `null`).
- **BITS, INTERVAL, TIMETZ, LIST, INET, VARIANT** (`canonical_*_text` in
  base.py, `tests/test_types_rules2.py`; verified live on PostgreSQL 18.4,
  DuckDB 1.5.5, MySQL 8, SQL Server 2022). Rules, and why each is injective:
  - BITS: the digits at full width (`bit(8)` b'101' is `00000101`) - what
    PostgreSQL, DuckDB and MySQL already printed. Never merged with BOOLEAN:
    SQL Server's `bit` keeps its own map (`map_type_mssql`).
  - INTERVAL: `<months> mon <days> day <µs> us`, the three stored components
    (PostgreSQL `extract` year/month, day, hour/minute/microseconds, summed in
    numeric; DuckDB `datepart` in HUGEINT). `1 day` and `24 hours` differ
    though both engines' `=` says equal - they print and add differently. PG
    17's infinite intervals render `infinity`/`-infinity`. The sample reads
    text (IntervalStyle pinned): both drivers fold a month into 30 days.
  - TIMETZ: `canonical_time_text` of the local clock + offset `+HH:MM`, `:SS`
    when it has seconds. **DuckDB's varchar cast of a TIMETZ is not
    injective** (COV-7: `+05:00:30` prints `+05:30`), so DuckDB renders from
    `datepart` and its sample reads the driver's `datetime.time`.
  - LIST: `[` + each element by its own type's `normalize`, each followed by
    `\,` (`LIST_TERMINATOR`), + `]`. Escaped text only ever has `\\`, `\s`
    after a backslash, so no element forges the terminator; the text starts
    with `[`, so it is never the sentinel. The element is rendered through a
    stand-in column (`LIST_ELEMENT_NAME`: an `unnest ... with ordinality`
    alias on PostgreSQL, a list-comprehension variable on DuckDB). PostgreSQL
    reports every array as `ARRAY`, so `_with_arrays` reads the element type
    from the catalog (`integer[]`). Ruled element types are
    `LIST_ELEMENT_TYPES`; anything else (JSON, nested, structs) leaves the
    list UNKNOWN. A PostgreSQL array with more than one dimension or a lower
    bound other than 1 holds its shape in its value and `unnest` would
    flatten it, so its `array_dims` lead, between `\=` marks
    (`LIST_DIMS_MARK`): `[\=[1:2][1:2]\=1\,2\,3\,4\,]` - a plain list
    cannot start `[\=`. psycopg drops lower bounds, so the PostgreSQL sample
    reads the array's text (`parse_pg_array`). Refused loudly: a list of
    DuckDB TIMESTAMP_NS (no caveat could reach an element) and a LIST key
    (elements round as values do).
  - INET: PostgreSQL's `::text` (`text(inet)`: the prefix always written, IPv6
    by BIND's `inet_net_ntop`, reproduced in `canonical_inet_text`).
  - VARIANT (sql_variant): `<base type>:<value>`, each base type in an exact
    style; float/real as `0x` + IEEE bits (-0 as 0), because style 3 raised
    on -0.0 and on negatives below 1e-99 and is not correctly rounded
    (COV-8). mssql-python drops the base type, so the sample reads base type,
    code page and `convert(varbinary)` of the value, decoded by
    `_variant_value` from SQL Server's storage layouts.
  - UHUGEINT is INTEGER, and as a key it is hashed (`bisects_natively`):
    DuckDB silently turns UHUGEINT arithmetic with a signed operand into
    DOUBLE. PostgreSQL `name` is STRING.
  - Not ruled, by choice: BigQuery ARRAY (cannot be NULL or hold a NULL, so a
    rule would only partly meet other engines') and INTERVAL; ranges,
    structs, nested lists, arrays of JSON.
- **Session pins are the contract for server settings** (round G): every
  setting that changes rendering or row visibility is pinned at connect -
  MySQL `sql_select_limit`, Snowflake `rows_per_resultset`, PostgreSQL
  `quote_all_identifiers`, SQL Server `numeric_roundabort` - and the engine
  checks that each side's first-level bucket counts add up to its row count,
  so any cap not yet pinned reads unexplained, never identical. PostgreSQL
  hashes `convert_to(text, 'UTF8')`; FLOAT is refused on SQL Server below
  compatibility level 130 and on PostgreSQL below 12; PostgreSQL < 9.6 and
  DuckDB < 1.3 are refused outright; RLS is a caveat.
- **Additive checksums are not tamper-proof.** Eight crafted row changes can
  cancel in one bucket's sum (Wagner's k-tree, seconds in Python, no secret
  needed). Accidentally the odds are ~2^-60 per differing bucket. Documented,
  not defended: a keyed or non-linear checksum would be the defence.
- **A row filter reaches every query that reads the table, or none**
  (`--where`, 2026-11-03). Every row-reading query - `key_stats`,
  `_segment_sql`, `fetch_range`, `fetch_sample` - names its table through
  `Dialect.rows_from`, which puts `(<predicate>)` first in the WHERE, ANDed
  with the tool's own bounds; a dialect overriding one of those four must
  pass `where` through (SQL Server's `_segment_sql`/`segment_checksums` do).
  `tests/test_where_sql.py` records every statement on all six dialects and
  fails on one without the predicate. The engine passes `where` only when
  set, so unfiltered SQL is byte-identical. The predicate is the user's own
  SQL, so not a security boundary, but it must stay one read:
  `row_filter_problem` refuses `;`, comments (incl. MySQL `#`), and what
  engines quote differently (a backslash before a quote, `'''`, `$$`), then
  checks quotes, `[...]` and parentheses balance, so `1=1) or (1=1` cannot
  escape; `check_where` adds SQL Server's SELECT guard. A filtered verdict
  covers the filtered rows only and says so in the headline, a `filtered:`
  line, JSON `where_a`/`where_b`, `risk.filtered` and a caveat.
- **Each level of the walk must add up to the level above** (found by the
  `--where` work). A queued range carries both sides' (count, checksum)
  from its parent bucket; its buckets must sum to exactly that, per side,
  or the range is unexplained. Without it a difference seen at one level
  could leave an unstable filter (`random()`, `clock_timestamp()`) or be
  rewritten on an engine without one snapshot before the next level looked,
  and the run read identical with the evidence discarded. Exact (sums are
  exact, buckets tile the range), so a single-snapshot run never trips it.
- **Renamed columns are re-keyed, never re-ordered** (`--map`). Side B's
  folded column dict is re-keyed under side A's folded names
  (`_apply_column_map`) before key resolution and column selection, so the
  compared list, its order and the key all follow from A's names and a
  mapped B column lands in A's position of the row text. Ambiguity is
  refused, never guessed: a name absent on its side, one A column onto two
  B columns or two onto one, a mapped B column a same-named column would
  still match, a column both mapped and excluded, `--exclude`/`--columns`
  by B's name. A swap (`x=y,y=x`) is legal and pinned by tests both ways
  (swapped data matches except the plant; unswapped data differs on every
  row). Differences carry A's name; `DiffResult.column_map` gives B's.
- **A multi-pair verdict is each pair's verdict, never stronger than its
  weakest pair** (`parity run`, `parity/batch.py`, 2026-11-04). Each pair is
  one plain `diff` on two fresh dialects closed before the next, so no
  snapshot or session is shared. The summary renders from each pair's
  `to_dict` JSON (so a `--resume`d pair prints exactly like a fresh one),
  and `batch.qualifiers` lists every line the single-pair report prints
  before its rows - filtered, truncated, schema, unexplained, sample failure
  / error / not run, uncovered columns, notes, every warning - in all four
  formats; `tests/test_batch.py` checks each qualifier in each format. The
  whole config is validated before any connection; unknown fields are
  refused, never ignored (a misspelt `where` would compare the whole table).
  `${NAME}` values are registered with a `Redactor` and replaced in every
  tool message, report and the state file - never in data values. `--resume`
  skips only pairs that finished **identical** under the same hash of
  settings, version and a salted PBKDF2 fingerprint of each expanded
  connection, so re-pointing a variable re-runs the pair. Exit 2 beats 1:
  any pair error or Ctrl-C is 2. The CLI only adds the subcommand.

## 5. Architecture

The whole tool ships in `src/parity/` — dialects, engine and CLI — with 592
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
mssql://user:password@host:port/database?trust_server_certificate=yes   (also sqlserver://)
```

The slash count carries meaning, as in sqlite and SQLAlchemy: exactly **one**
leading slash is removed, the one separating the empty authority from the path.
Stripping them all quietly turns every absolute POSIX path into a relative one
and the tool then reports "database file not found" for a file that is plainly
there. Windows hides this completely, because its paths begin with a drive
letter and carry only one leading slash to start with.

The scheme before `://` selects the dialect in `get_dialect()`. Table names may
be schema-qualified (`public.orders`, `main.orders`); unqualified names default
to `public` on PostgreSQL and `main` on DuckDB (and the login's own default
schema, normally `dbo`, on SQL Server). The `mssql://` query parameters are
whitelisted (`encrypt`, `trust_server_certificate`, `application_intent`,
`login_timeout`, `instance`) and every ODBC value is braced, so a `;` in a
password cannot inject a keyword.

```
src/parity/
  types.py                      # Column, TableRef, Segment, RowDiff, DiffResult
  dialects/
    base.py                     # Dialect ABC + get_dialect() + type mapping
    postgres_dialect.py
    duckdb_dialect.py
    mssql_dialect.py            # SQL Server 2019+ / Azure SQL (and the other engines' files)
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
the per-segment checksum query, and the small-range row fetch — so the
required methods are short, though a verified dialect is several hundred lines
(DuckDB, the smallest, about 570).

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
- **`docs/risk.md` states the error odds of every step and is updated in the
  same commit as every change to the tool** - re-check the sections the
  change touches, copy new coverage numbers, then re-stamp it with
  `python tests/test_risk_doc.py --stamp`. `tests/test_risk_doc.py` fails
  whenever `src/parity/` changed since the last stamp, or a figure there
  disagrees with the code or `docs/coverage.md`. Never re-stamp without
  reviewing: the file is what a client is told about trusting a verdict.

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
