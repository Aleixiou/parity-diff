"""One logical value, every engine, one canonical text.

Reads the runs `test_conformance.py` saved (one JSON per engine and scale,
under `cov_harness.RESULTS`) and checks that a logical value held by two or
more engines renders to byte-identical text everywhere it is held. The
per-engine sweep already makes every engine agree with the rules, so this is
transitively implied for ruled types - it is checked directly anyway, since
it is the property a migration depends on and the per-engine check could be
fooled by a sample path that reads back what `normalize` wrote.

Runs after the engine sweeps (file order). Engines run in another process
(Snowflake's venv) contribute through their saved files, so a stale file
from an older run is included: `report.py` prints each run's date.
"""

from __future__ import annotations

import pytest
from cov_cross import collisions, compare, load_runs
from cov_harness import RESULTS
from cov_known import COLLISIONS, SPLITS
from cov_values import FORM_DEPENDENT, RULED


@pytest.mark.parametrize("scale", [6, 2])
def test_every_ruled_value_renders_alike_on_every_engine(scale: int):
    """Same logical value, same logical type, different text: a bug, unless
    it is a confirmed finding. UNKNOWN types and type-mapping mismatches are
    reported by `report.py`, not failed: no rule says they must agree."""
    runs = load_runs(RESULTS, scale)
    if len(runs) < 2:
        pytest.skip(f"{len(runs)} engine run(s) saved at scale {scale}; need two")
    shared, splits = compare(runs)
    assert shared > 300, f"only {shared} values are held by two or more engines"
    bad = [
        s
        for s in splits
        if s.family in RULED
        and s.same_logical
        and s.family not in SPLITS
        and s.family not in FORM_DEPENDENT
    ]
    assert not bad, "\n".join(s.describe() for s in bad)


@pytest.mark.parametrize("scale", [6, 2])
def test_no_two_unknown_values_share_a_text_anywhere(scale: int):
    """UNKNOWN-mapped types have no rule, so equal text *is* equality: two
    different values rendering alike - on one engine or across two - is a
    false match. Only confirmed findings may remain."""
    runs = load_runs(RESULTS, scale)
    if not runs:
        pytest.skip(f"no engine run saved at scale {scale}")
    groups = {c["group"] for r in runs.values() for c in r["cells"]} - RULED
    clash = [
        c
        for c in collisions(runs, groups)
        if not any((e, n) in COLLISIONS for e, n, _ in c.who)
    ]
    assert not clash, "\n".join(c.describe() for c in clash)
