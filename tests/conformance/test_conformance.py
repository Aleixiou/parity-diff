"""Every logical type on every engine: `normalize` against the rules, live.

The model is `tests/test_float_canonical.py`, which pinned FLOAT by rendering
1,565 hard doubles on every engine and comparing them with the Python spec.
This does the same for every logical type and every native type an engine
maps onto one (`cov_engines.py`), with the hard values of `cov_values.py`:
loaded through the driver's parameter binding, rendered by the dialect's
own `normalize`, and compared with `parity.reference` applied to the raw
value read back (`cov_harness.py`), at float scale 6 and 2.

A disagreement fails the test unless it is a confirmed finding listed in
`cov_known.py` - each pinned by a strict xfail in
`test_conformance_findings.py`. The runs are saved as JSON for the
cross-engine test (`test_conformance_cross.py`) and for `report.py`, which
writes `docs/coverage.md`.

Each engine skips cleanly without its server, through the conftest URL
fixtures and the per-engine markers. Snowflake needs the Python 3.13 venv:

    <venv-sf>/python -m pytest tests/conformance -m snowflake
    .venv/Scripts/python -m pytest tests/conformance
    .venv/Scripts/python tests/conformance/report.py
"""

from __future__ import annotations

import pytest
from cov_engines import (
    BigQueryCov,
    CovEngine,
    DuckDBCov,
    MSSQLCov,
    MySQLCov,
    PostgresCov,
    SnowflakeCov,
)
from cov_harness import Cell, EngineRun, run
from cov_known import DISAGREE, REFUSED, UNCHECKED
from cov_values import REFUSE

SCALES = (6, 2)

ENGINES = [
    pytest.param("postgres", marks=pytest.mark.postgres),
    pytest.param("duckdb", marks=pytest.mark.duckdb),
    pytest.param("mysql", marks=pytest.mark.mysql),
    pytest.param("mssql", marks=pytest.mark.mssql),
    pytest.param("bigquery", marks=pytest.mark.bigquery),
    pytest.param("snowflake", marks=pytest.mark.snowflake),
]


def make_engine(name: str, request: pytest.FixtureRequest, tmp: str) -> CovEngine:
    """Build the engine's fixture writer; the URL fixtures skip when it is absent."""
    if name == "duckdb":
        pytest.importorskip("duckdb")
        return DuckDBCov(tmp)
    url = str(request.getfixturevalue(f"{'pg' if name == 'postgres' else name}_url"))
    return {
        "postgres": PostgresCov,
        "mysql": MySQLCov,
        "mssql": MSSQLCov,
        "bigquery": BigQueryCov,
        "snowflake": SnowflakeCov,
    }[name](url)


@pytest.fixture(scope="module", params=ENGINES)
def engine_runs(request, tmp_path_factory) -> tuple[str, dict[int, EngineRun]]:
    """Load, render and check one engine at every scale; save the runs."""
    name = str(request.param)
    eng = make_engine(name, request, str(tmp_path_factory.mktemp(f"cov_{name}")))
    try:
        runs = run(eng, SCALES)
    finally:
        eng.drop_all()
    for r in runs.values():
        r.save()
    return name, runs


def _known(engine: str, c: Cell) -> str | None:
    """The COV id that tolerates this cell's outcome, if any."""
    if c.status in ("disagree", "error"):
        return DISAGREE.get((engine, c.native, c.group))
    if c.status == "unchecked":
        return UNCHECKED.get((engine, c.native, c.group))
    if c.status == "refused" and c.group not in REFUSE:
        return "documented" if (engine, c.native, c.key) in REFUSED else None
    return "ok"


def _line(c: Cell) -> str:
    """A cell as one line of a failure message."""
    return (
        f"{c.native} [{c.group}] {c.key}: {c.status} text={c.text!r} "
        f"rules={c.ref!r} {c.detail}"
    )


@pytest.mark.parametrize("scale", SCALES)
def test_every_value_renders_exactly_as_the_rules_say(engine_runs, scale: int):
    """`normalize` == canonical_text(reference_token(raw value)) for every
    ruled value, and nothing errors, except confirmed findings."""
    engine, runs = engine_runs
    cells = runs[scale].cells
    bad = [
        c
        for c in cells
        if c.status in ("disagree", "error", "unchecked", "refused")
        and _known(engine, c) is None
    ]
    assert not bad, f"{engine} at scale {scale}: {len(bad)} unexplained:\n" + "\n".join(
        _line(c) for c in bad[:25]
    )


@pytest.mark.parametrize("scale", SCALES)
def test_values_past_2_116_are_refused_never_rendered(engine_runs, scale: int):
    """Every engine's FLOAT refuses what the canonical form cannot render."""
    engine, runs = engine_runs
    cells = [c for c in runs[scale].cells if c.group in REFUSE]
    assert cells, f"{engine} has no must-refuse column under test"
    wrong = [c for c in cells if c.status not in ("refused", "unstorable")]
    assert not wrong, "\n".join(_line(c) for c in wrong)


def test_the_sample_is_hard_and_mostly_checked(engine_runs):
    """A sweep that checks nothing proves nothing: most ruled values must be
    checked (agree), and every ruled logical type must be present."""
    engine, runs = engine_runs
    cells = runs[SCALES[0]].cells
    ruled = [c for c in cells if c.status not in ("unruled", "unstorable")]
    agree = [c for c in ruled if c.status == "agree"]
    assert len(agree) >= 0.9 * len(ruled), (engine, len(agree), len(ruled))
    logical = {c.logical for c in agree}
    assert {"integer", "decimal", "float", "boolean", "string", "date", "timestamp"} <= (
        logical | ({"boolean"} if engine == "mysql" else set())
    ), (engine, logical)
