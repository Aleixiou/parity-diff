# ROADMAP.md — where `parity` is, and where it goes next

> `CLAUDE.md` holds the verified cross-engine SQL and the scope rules. This
> file holds status and direction. If the two disagree, `CLAUDE.md` wins on
> anything technical.

This replaces the original ordered build plan, which described a project with
no CLI, no tests and no packaging. All of that shipped, so the plan had become
a document confidently describing a state the repo had left — worse than no
document, because it makes a reader distrust the accurate ones.

## Where it is

**v0.2.2 released** (2026-09-07): PostgreSQL, DuckDB, MySQL, and **Snowflake**
— the first warehouse — plus non-integer/composite keys and case-insensitive
identifier matching across engines. `pip install parity-diff`, MIT.

| | |
|---|---|
| Tests | 360; run against live PostgreSQL, DuckDB and MySQL. The 5 Snowflake tests skip unless `PARITY_TEST_SNOWFLAKE` points at a real account, so they never run in CI — verified live by hand instead. The identical check — a false match being the one unforgivable failure — carries a dedicated stress campaign (offline logic, every live engine pair, all key types, edge values, representation changes, CLI, timezone, snapshot isolation) with no abnormality found |
| Coverage | 96%, with a 95% floor enforced in CI (the Snowflake dialect is covered offline; only its live-driver methods are exempt) |
| CI | ruff, `mypy --strict`, PostgreSQL 16 and 18 + MySQL 8 service containers, Python 3.10 and 3.13, Windows, a DuckDB-only install |
| Proven | 10M rows per side: identical in 4 queries and 0 rows downloaded; five planted differences found exactly, moving 0.0381% of the data. Snowflake verified live: hash constant, a 5,000-row full-table checksum matching DuckDB byte-for-byte, and every planted-difference check |

Every test plants a difference and asserts the tool finds exactly it. That rule
is the reason this project can be trusted at all, and it is not negotiable — see
`CONTRIBUTING.md`.

## What is not done

Ordered by how much it would change who can use this.

### 0. Validation — the actual bottleneck now

The first warehouse is shipped, so the tool can finally serve the market it was
built for — and it has essentially no users. Every item below is speculative
until someone with a real migration says which one they need. The highest-value
move is not code: it is posting the launch (see `docs/outreach.md`, now that a
warehouse exists) and letting the replies pick the next dialect. Building the
wrong three engines is exactly how the predecessor's maintenance grew expensive.

### 1. Snowflake — done (v0.2.2)

The first warehouse. Verified live against a real account, which is the bar:
the hash constant agrees, a 5,000-row full-table checksum matches DuckDB byte
for byte, and every planted-difference check passes. It also forced the one
thing the docs could not predict — case-insensitive identifier matching, since
Snowflake upper-cases what PostgreSQL and DuckDB lower-case. Proof that the
dialect abstraction holds against a warehouse, not just OLTP engines.

### 2. The next warehouse — BigQuery, then Redshift / Databricks

BigQuery is the most likely next request and the strongest addition to a
launch. A dialect is roughly 70–85 lines (`CONTRIBUTING.md` has the contract and
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
