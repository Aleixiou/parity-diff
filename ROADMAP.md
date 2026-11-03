# ROADMAP.md — where `parity` is, and where it goes next

> `CLAUDE.md` holds the verified cross-engine SQL and the scope rules. This
> file holds status and direction. If the two disagree, `CLAUDE.md` wins on
> anything technical.

This replaces the original ordered build plan, which described a project with
no CLI, no tests and no packaging. All of that shipped, so the plan had become
a document confidently describing a state the repo had left — worse than no
document, because it makes a reader distrust the accurate ones.

## Where it is

**v0.4.0 released** (2026-11-03): adds **SQL Server / Azure SQL**, and fixes
every false match a seven-angle red-team campaign found (`tests/redteam/`,
CLAUDE.md §4.11): text escaping, exact keys, schema differences in the verdict,
PostgreSQL edge dates and session pins, MySQL binary and read-only sessions,
and more. Full live suite: 1,375 passed.

**v0.3.0 released** (2026-11-02): PostgreSQL, DuckDB, MySQL, **Snowflake** and
**BigQuery**, with non-integer/composite keys and case-insensitive identifier
matching across engines. `pip install parity-diff`, MIT. 0.3.0 also fixes two
false matches found while adding BigQuery — doubles with ~15+ significant digits
compared across engines, and MySQL DECIMAL values past 38 − scale digits — by
moving FLOAT onto one canonical form every engine computes identically.

| | |
|---|---|
| Tests | 490; run against live PostgreSQL, DuckDB and MySQL. The 5 Snowflake tests and the 15 live BigQuery tests skip unless `PARITY_TEST_SNOWFLAKE` / `PARITY_TEST_BIGQUERY` point at a real account, so they never run in CI — verified live by hand instead. The identical check — a false match being the one unforgivable failure — carries a dedicated stress campaign (offline logic, every live engine pair, all key types, edge values, representation changes, CLI, timezone, snapshot isolation) with no abnormality found |
| Coverage | 96%, with a 95% floor enforced in CI (the Snowflake dialect is covered offline; only its live-driver methods are exempt) |
| CI | ruff, `mypy --strict`, PostgreSQL 16 and 18 + MySQL 8 service containers, Python 3.10 and 3.13, Windows, a DuckDB-only install |
| Proven | 10M rows per side: identical in 4 queries and 0 rows downloaded; five planted differences found exactly, moving 0.0381% of the data. Snowflake verified live: hash constant, a 5,000-row full-table checksum matching DuckDB byte-for-byte, and every planted-difference check |

Every test plants a difference and asserts the tool finds exactly it. That rule
is the reason this project can be trusted at all, and it is not negotiable — see
`CONTRIBUTING.md`.

## Since 0.4.0 (unreleased, branch `snowflake-live`)

Snowflake verified live and its FLOAT fixed; the independent sample check
(default 10,000 rows, 99.9% confidence) and per-run risk figures; the coverage
map (`docs/coverage.md`); `docs/risk.md`, kept current with every change;
red-team round G (server settings and versions, 14 findings fixed); exact rules
for TIME, BINARY, JSON, bit strings, intervals, timetz, lists, inet and
sql_variant. Full live suite: 1,620 passed.

## The plan to an A in every area (2026-11-03)

A self-assessment graded the tool for backend and database engineers:
trustworthiness **A**, speed and cost **B+**, database coverage **B-**, fit
for a real migration **C**, ease of use and docs **C+**, maturity **C**. This
is the plan to bring each to an A. "A" is defined by something checkable, not
by how it feels.

| Area | Now | An A means | Work |
|---|---|---|---|
| Trustworthiness | A | no known accidental false match; odds published per step; crafted data closable | optional keyed (secret) checksum against tampering; coverage map required for every new engine; an outside review of `docs/risk.md` |
| Speed and cost | B+ | a huge table can be checked daily without a large bill, and the cost is known before the run | `--where` per side; a cost estimate before the run (BigQuery dry run, Snowflake warehouse/time); tables in parallel; sampling mode that can never print "identical"; benchmarks on every engine |
| Database coverage | B- | the top legacy sources and modern targets, each verified live | Oracle, then Databricks, Redshift, Teradata, DB2 - each with its coverage map, a settings/versions red-team round, a risk entry and a live suite |
| Fit for a real migration | C | one command proves a whole migration (hundreds of tables, renamed columns, filters) and gives one report to sign off | a config file of table pairs; column renames; per-column float scale; one summary report (Markdown/HTML, JUnit for CI); resume a run that stopped |
| Ease of use and docs | C+ | a new user finds a first real difference in under 5 minutes, measured with real people | the docs restructure (quickstart, output, troubleshooting, per-engine pages); `parity check-connection`; every error says what to do next; pipx, a Docker image, CI templates; five observed first-time users |
| Maturity | C | a large company's engineering team would approve it | release 0.5.0; live engines in CI; a 1.0 stability promise; security policy, signed releases, SBOM; 10 teams using it, 3 named; a second maintainer |

Everything above stays inside the scope rules of `CLAUDE.md` §3: it all
serves "can I safely switch off the old system?" - no scheduler, no web UI, no
stored history.

| Phase | When | Content | Grades after |
|---|---|---|---|
| 1 | weeks 1-3 | docs restructure, `--where`, column renames, the config file and summary report, **Oracle** (moved up from phase 2, 2026-11-03), release 0.5.0 | fit B+, ease of use B+, coverage B+ |
| 2 | weeks 4-8 | the cost estimate, `check-connection`, live engines in CI, five observed users | coverage B+, speed A, ease of use A |
| 3 | weeks 9-16 | Databricks, Redshift, keyed checksum, 1.0, outside review of the risk file | coverage A-, trust A+ |
| 4 | ongoing | Teradata and DB2; users and contributors | maturity A (depends on adoption, not only on work) |

Phase 1 started 2026-11-03.

## What is not done

Ordered by how much it would change who can use this.

### 0. Validation — the actual bottleneck now

The first warehouse is shipped, so the tool can finally serve the market it was
built for — and it has essentially no users. Every item below is speculative
until someone with a real migration says which one they need. The highest-value
move is not code: it is posting the launch (now that a
warehouse exists) and letting the replies pick the next dialect. Building the
wrong three engines is exactly how the predecessor's maintenance grew expensive.

### 1. Snowflake — done (v0.2.2)

The first warehouse. Verified live against a real account, which is the bar:
the hash constant agrees, a 5,000-row full-table checksum matches DuckDB byte
for byte, and every planted-difference check passes. It also forced the one
thing the docs could not predict — case-insensitive identifier matching, since
Snowflake upper-cases what PostgreSQL and DuckDB lower-case. Proof that the
dialect abstraction holds against a warehouse, not just OLTP engines.

### 2. BigQuery — done (v0.3.0)

The second warehouse, verified live the same way: hash constant, a 5,000-row
checksum matching DuckDB byte for byte, every planted-difference check, a
130-column table and keys spanning all of INT64. Base tables are pinned to one
snapshot with time travel; views cannot be, and say so.

### 2b. The next engine — by demand

Public evidence of live migrations in Switzerland, Germany and Austria points
mostly at **SQL Server / Microsoft Fabric, Databricks and Oracle** as sources or
targets — none supported yet. Those are the strongest candidates, ahead of
Redshift. A dialect is roughly 70–85 lines (`CONTRIBUTING.md` has the contract and
the traps), and — as Snowflake showed — it is not done until it passes against
the *real* engine, which is where the one unforeseeable quirk always surfaces.
`data-diff` shipped extras for twelve engines; every one is a table someone
cannot currently compare. Pick the next by demand, not by guess.

### 3. Sampling mode

An approximate check that costs a fraction of a full hash pass, for the case
where a fast signal beats a certain one. Must be labelled as approximate
everywhere it appears — `CLAUDE.md` §8 forbids letting an approximate result
look exact.

### 4. A dbt integration

`--select` a model and diff prod against the PR. Only worth building once
someone with a dbt project asks for it.

## Deliberately refused

Not "later" — no. The open-source predecessor in this category was abandoned
because maintaining it grew expensive, and a narrow scope is the only defence.

- data quality rules, freshness checks, anomaly detection, observability
- lineage, cataloguing, orchestration, transformation
- a web UI, a server, a database of past runs
- schema migration or DDL generation
- anything that requires adopting a framework to use

The test for any proposed feature: does it help someone answer *"can I safely
switch off the old system?"* If not, it does not belong here.

## Known limitations

These are in the README too, because a user has to see them. Repeated here so a
contributor knows which are deliberate and which are open.

| Limitation | Status |
|---|---|
| Non-integer keys are bucketed by a 60-bit hash | Deliberate — identity stays the real key, so a collision cannot merge rows |
| Floats compared at 6 decimal places | Deliberate, configurable, printed on every run |
| Keys must be unique and non-NULL | Deliberate — both are detected and refused |
| At most 10,000 differences reported | Deliberate — unbounded needed 8.5 GB at 10M rows |
| A double past 1e32 fails on DuckDB | Open, fails loudly |
| Types beyond the seven mapped compare as raw text | Open, warned about |

## How this project was built, if it is useful

Milestones 0–5, one at a time, each with acceptance criteria that had to pass
before the next began: repo skeleton, dialect layer and canonical encoding,
bisection engine, CLI, the 10M-row proof, then release. The full history is in
the git log, and `docs/launch-post.md` covers the ten bugs the discipline
caught along the way.
