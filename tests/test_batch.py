"""`parity run CONFIG`, with no database server.

Every test plants a difference (or an error) and asserts exactly which pair
reports it. The dangers here are a summary that reads stronger than one of
its pairs - "identical" for a pair the single-pair report would qualify -
and a password from the environment leaking into some output.
"""

from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fakes import FakeDialect, SyntheticTable

from parity import batch
from parity.batch import (
    BatchRun,
    ConfigError,
    PairOutcome,
    parse_config,
    read_config,
    render_junit,
    render_markdown,
    run_batch,
    run_to_dict,
)
from parity.cli import EXIT_DIFFERENCES, EXIT_ERROR, EXIT_IDENTICAL, build_parser, main
from parity.engine import diff
from parity.types import Column, LogicalType

SECRET = "pw-Zq8!s3cret"  # a test value that must never be printed


def even(k: int) -> bool:
    """The stand-in row filter: even keys only."""
    return k % 2 == 0


class Opener:
    """Opens FakeDialects by connection string, recording every open.

    A value that is an exception is raised instead, quoting the connection
    string the way a real driver error does - which is what the redaction
    tests rely on.
    """

    def __init__(self, tables: dict[str, object]) -> None:
        self.tables = tables
        self.opened: list[str] = []

    def __call__(self, conn: str, side: str = "?", float_scale: int = 6) -> FakeDialect:
        self.opened.append(conn)
        table = self.tables[conn]
        if isinstance(table, BaseException):
            if isinstance(table, KeyboardInterrupt):
                raise table
            raise ValueError(f"[side {side}: fake] could not connect to {conn}: refused")
        return FakeDialect(
            table, side=side, float_scale=float_scale, predicates={"even": even}
        )


def write(tmp_path: Path, text: str, name: str = "run.toml") -> str:
    """Write a config file and return its path."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def run_cli(argv: list[str], opener: Opener, environ: dict[str, str] | None = None):
    """Drive `parity run` as the CLI does, with fakes; return (code, out, err)."""
    args = build_parser().parse_args(["run", *argv])
    out, err = io.StringIO(), io.StringIO()
    try:
        code = batch.run_command(args, out, err, opener=opener, environ=environ or {})
    except Exception as exc:  # what `main` turns into exit 2
        err.write(f"parity: {exc}\n")
        code = EXIT_ERROR
    return code, out.getvalue(), err.getvalue()


def pairs_toml(names: list[str], extra: dict[str, str] | None = None) -> str:
    """A config with one connection per side per pair: `fake://<name>-a/-b`."""
    extra = extra or {}
    lines = ["[connections]"]
    for n in names:
        lines += [f'{n}_a = "fake://{n}-a"', f'{n}_b = "fake://{n}-b"']
    lines += ["", "[defaults]", "verify_sample = 200", ""]
    for n in names:
        lines += [
            "[[pair]]",
            f'name = "{n}"',
            f'a = "{n}_a"',
            f'b = "{n}_b"',
            'table = "t"',
            'key = "id"',
            extra.get(n, ""),
            "",
        ]
    return "\n".join(lines)


def tables_for(names: list[str], plants: dict[str, SyntheticTable] | None = None, n=500):
    """Identical fake tables for every pair, except the B sides in `plants`."""
    plants = plants or {}
    out: dict[str, object] = {}
    for name in names:
        out[f"fake://{name}-a"] = SyntheticTable(n)
        out[f"fake://{name}-b"] = plants.get(name, SyntheticTable(n))
    return out


# ---------------------------------------------------------------------------
# 1. Config validation: everything refused before any connection
# ---------------------------------------------------------------------------


GOOD = """
[connections]
old = "fake://old"
new = "fake://new"

[[pair]]
name = "orders"
a = "old"
b = "new"
table = "t"
key = "id"
"""


def test_a_valid_config_parses_with_defaults_applied(tmp_path):
    """The resolved pair carries every default the CLI would use."""
    cfg = read_config(write(tmp_path, GOOD))
    (spec,) = cfg.pairs
    assert spec.a_table == spec.b_table == "t"
    assert spec.key == ("id",)
    assert spec.float_scale == 6 and spec.max_diffs == 10_000
    assert len(cfg.sha256) == 64


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (GOOD.replace("key =", "kye ="), ["unknown field 'kye' - did you mean 'key'?"]),
        (GOOD.replace("[[pair]]", "[[pairs]]"), ["unknown field 'pairs'", "'pair'"]),
        (
            GOOD + "\n[defaults]\nfloat_scal = 2\n",
            ["[defaults]", "'float_scal'", "'float_scale'"],
        ),
        (GOOD.replace('b = "new"', 'b = "nwe"'), ["no connection named 'nwe'", "'new'"]),
        (GOOD.replace('key = "id"', ""), ["pair 'orders' (#1)", "missing field 'key'"]),
        (GOOD.replace('a = "old"', ""), ["missing field 'a'"]),
        (GOOD.replace('table = "t"', 'a_table = "t"'), ["missing field 'b_table'"]),
        (GOOD.replace('table = "t"', 'table = "t"\na_table = "u"'), ["field 'table'"]),
        (GOOD + 'float_scale = "6"\n', ["field 'float_scale' must be a whole number"]),
        (GOOD + "verify_sample = true\n", ["'verify_sample' must be a whole number"]),
        (GOOD + "max_diffs = -1\n", ["'max_diffs' must be at least 0"]),
        (GOOD + "bisection_factor = 1\n", ["'bisection_factor' must be at least 2"]),
        (GOOD + 'where = "1=1; drop table t"\n', ["field 'where': row filter", "';'"]),
        (
            GOOD + 'where = "x"\na_where = "y"\nb_where = "z"\n',
            ["'where' is replaced on both sides"],
        ),
        (GOOD + 'map = { a = "" }\n', ["field 'map'"]),
        (GOOD + "map = 3\n", ["field 'map' must be a table"]),
        (
            GOOD.replace('"fake://new"', '"fake://${1BAD}"'),
            ["'${1BAD}' is not an environment"],
        ),
        (
            GOOD + GOOD.split("\n", 4)[4],
            ["duplicate name", "pair #1 is also called 'orders'"],
        ),
        ("[[pair]]\nname = 'x'\n", ["[connections]", "missing"]),
    ],
)
def test_every_config_problem_names_the_file_the_pair_and_the_field(
    tmp_path, text, expected
):
    """Refused with a message naming the file and the field; nothing opened."""
    path = write(tmp_path, text)
    opener = Opener({})
    code, out, err = run_cli([path], opener)
    assert code == EXIT_ERROR
    assert path in err
    for fragment in expected:
        assert fragment in err, (fragment, err)
    assert opener.opened == []
    assert out == ""


def test_all_problems_are_reported_at_once(tmp_path):
    """Three mistakes in two pairs: one run lists all of them (the misspelt
    key is also a missing one)."""
    text = GOOD.replace("key =", "kye =") + (
        '\n[[pair]]\nname = "b"\na = "old"\nb = "gone"\ntable = "t"\nkey = "id"\n'
        "threshold = 0\n"
    )
    with pytest.raises(ConfigError) as info:
        read_config(write(tmp_path, text))
    msg = str(info.value)
    assert "4 problems" in msg
    assert "pair 'orders' (#1): unknown field 'kye'" in msg
    assert "pair 'orders' (#1): missing field 'key'" in msg
    assert "pair 'b' (#2): field 'b': no connection named 'gone'" in msg
    assert "pair 'b' (#2): field 'threshold' must be at least 1" in msg


def test_a_file_that_is_not_toml_or_json_is_refused(tmp_path):
    """Wrong extension, broken TOML, broken JSON: each a clear error."""
    with pytest.raises(ConfigError, match=r"must end in .toml or .json"):
        read_config(write(tmp_path, GOOD, "run.yaml"))
    with pytest.raises(ConfigError, match="not valid TOML"):
        read_config(write(tmp_path, "[connections\n", "bad.toml"))
    with pytest.raises(ConfigError, match="not valid JSON"):
        read_config(write(tmp_path, "{", "bad.json"))
    with pytest.raises(ConfigError, match="cannot read"):
        read_config(tmp_path / "absent.toml")


def test_json_is_the_same_config_as_toml(tmp_path):
    """The JSON spelling resolves to exactly the same pairs."""
    data = {
        "connections": {"old": "fake://old", "new": "fake://new"},
        "pair": [{"name": "orders", "a": "old", "b": "new", "table": "t", "key": "id"}],
    }
    from_json = read_config(write(tmp_path, json.dumps(data), "run.json"))
    from_toml = read_config(write(tmp_path, GOOD))
    assert from_json.pairs == from_toml.pairs


def test_python_310_without_tomli_is_told_to_install_it_or_use_json(
    tmp_path, monkeypatch
):
    """No tomllib and no tomli: a clear refusal naming both ways out."""
    real = batch.importlib.import_module

    def missing(name, *args):
        if name in ("tomllib", "tomli"):
            raise ImportError(name)
        return real(name, *args)

    monkeypatch.setattr(batch.importlib, "import_module", missing)
    with pytest.raises(ConfigError) as info:
        read_config(write(tmp_path, GOOD))
    assert "tomli" in str(info.value) and "JSON" in str(info.value)


def test_the_pair_name_defaults_to_the_a_table_and_a_list_key_works():
    """Without `name` a pair is called by its A table; keys may be lists."""
    cfg = parse_config(
        {
            "connections": {"x": "fake://x"},
            "pair": [
                {"a": "x", "b": "x", "a_table": "s.t", "b_table": "u", "key": ["a", "b"]}
            ],
        }
    )
    assert cfg.pairs[0].name == "s.t"
    assert cfg.pairs[0].key == ("a", "b")


# ---------------------------------------------------------------------------
# 2. Environment variables: expanded, never printed
# ---------------------------------------------------------------------------


ENV_CONFIG = """
[connections]
legacy = "fake://user:${LEGACY_PW}@host/db"
new = "${NEW_URL}"

[defaults]
verify_sample = 200

[[pair]]
name = "orders"
a = "legacy"
b = "new"
table = "t"
key = "id"
"""


def test_variables_expand_whole_value_and_inside_a_string(tmp_path):
    """Both spellings reach the opener expanded; a planted change is found."""
    opener = Opener(
        {
            f"fake://user:{SECRET}@host/db": SyntheticTable(300),
            "fake://new": SyntheticTable(300, changed={42: ("0.00", "x")}),
        }
    )
    code, out, _ = run_cli(
        [write(tmp_path, ENV_CONFIG)],
        opener,
        {"LEGACY_PW": SECRET, "NEW_URL": "fake://new"},
    )
    assert code == EXIT_DIFFERENCES
    assert opener.opened == [f"fake://user:{SECRET}@host/db", "fake://new"]
    assert "1 difference" in out


def test_a_missing_variable_names_the_variable_and_the_connection(tmp_path):
    """Unset: refused before connecting, naming LEGACY_PW and 'legacy'."""
    opener = Opener({})
    code, _, err = run_cli([write(tmp_path, ENV_CONFIG)], opener, {"NEW_URL": "fake://n"})
    assert code == EXIT_ERROR
    assert "connection 'legacy'" in err and "LEGACY_PW is not set" in err
    assert opener.opened == []


def test_a_variable_only_an_unselected_pair_uses_need_not_be_set(tmp_path):
    """`--only` a pair whose connections are literal: the other's unset
    variable is not needed."""
    text = ENV_CONFIG + (
        '\n[[pair]]\nname = "plain"\na = "lit_a"\nb = "lit_b"\ntable = "t"\nkey = "id"\n'
    )
    text = text.replace(
        "[defaults]", 'lit_a = "fake://lit-a"\nlit_b = "fake://lit-b"\n\n[defaults]'
    )
    opener = Opener(
        {
            "fake://lit-a": SyntheticTable(100),
            "fake://lit-b": SyntheticTable(100, deleted=[50]),
        }
    )
    code, out, _ = run_cli([write(tmp_path, text), "--only", "plain"], opener, {})
    assert code == EXIT_DIFFERENCES, out
    assert "0 of 1 pair identical, 1 differ" in out


def test_the_expanded_value_never_appears_in_any_output(tmp_path):
    """A driver error quoting the connection string: every output, the
    state file and stderr show `${LEGACY_PW}`, never the password."""
    opener = Opener(
        {
            f"fake://user:{SECRET}@host/db": ConnectionError(),
            "fake://new": SyntheticTable(300),
        }
    )
    md, junit, state = (str(tmp_path / f) for f in ("r.md", "r.xml", "s.json"))
    path = write(tmp_path, ENV_CONFIG)
    env = {"LEGACY_PW": SECRET, "NEW_URL": "fake://new"}
    outputs = []
    for extra in ([], ["--json"]):
        code, out, err = run_cli(
            [path, "--markdown", md, "--junit", junit, "--resume", state, *extra],
            opener,
            env,
        )
        assert code == EXIT_ERROR
        outputs += [out, err]
    for text in [*outputs, *(Path(f).read_text("utf-8") for f in (md, junit, state))]:
        assert SECRET not in text
    assert "fake://user:${LEGACY_PW}@host/db" in outputs[0]


def test_a_literal_dollar_in_a_connection_string_stays_as_written(tmp_path):
    """Only `${NAME}` is special: `$x` and `$` pass through untouched."""
    text = GOOD.replace('"fake://old"', '"fake://u:p$x$@h"')
    opener = Opener(
        {
            "fake://u:p$x$@h": SyntheticTable(100),
            "fake://new": SyntheticTable(100, deleted=[5]),
        }
    )
    code, out, _ = run_cli([write(tmp_path, text)], opener)
    assert code == EXIT_DIFFERENCES
    assert opener.opened[0] == "fake://u:p$x$@h"


# ---------------------------------------------------------------------------
# 3. Running: isolation, --only, --fail-fast, exit codes
# ---------------------------------------------------------------------------


def test_an_error_in_one_pair_does_not_affect_the_others(tmp_path):
    """Pair 2 cannot connect; pairs 1 and 3 still run, and pair 3's planted
    difference is found exactly."""
    names = ["p1", "p2", "p3"]
    tables = tables_for(names, {"p3": SyntheticTable(500, changed={77: ("1.00", "y")})})
    tables["fake://p2-a"] = ConnectionError()
    opener = Opener(tables)
    cfg = read_config(write(tmp_path, pairs_toml(names)))
    run = run_batch(cfg, opener=opener, environ={})
    assert [o.status for o in run.outcomes] == ["identical", "error", "differ"]
    assert "could not connect" in (run.outcomes[1].error or "")
    assert [d["key"] for d in run.outcomes[2].result["differences"]] == [77]
    assert run.exit_code == EXIT_ERROR


@pytest.mark.parametrize(
    ("plants", "broken", "code"),
    [
        ({}, [], EXIT_IDENTICAL),
        ({"p2": SyntheticTable(500, deleted=[3])}, [], EXIT_DIFFERENCES),
        ({}, ["p1"], EXIT_ERROR),
        ({"p2": SyntheticTable(500, deleted=[3])}, ["p3"], EXIT_ERROR),
    ],
)
def test_exit_code_is_0_only_when_every_pair_is_identical(tmp_path, plants, broken, code):
    """0 all identical, 1 any differs, 2 any errors (even with a differ)."""
    names = ["p1", "p2", "p3"]
    tables = tables_for(names, plants)
    for name in broken:
        tables[f"fake://{name}-b"] = ConnectionError()
    got, out, _ = run_cli([write(tmp_path, pairs_toml(names))], Opener(tables))
    assert got == code, out


def test_only_runs_the_named_pairs_and_refuses_an_unknown_name(tmp_path):
    """`--only p3,p1` runs exactly those (config order); a typo is refused."""
    names = ["p1", "p2", "p3"]
    tables = tables_for(names, {"p2": SyntheticTable(500, deleted=[9])})
    opener = Opener(tables)
    path = write(tmp_path, pairs_toml(names))
    code, out, _ = run_cli([path, "--only", "p3,p1"], opener)
    assert code == EXIT_IDENTICAL  # p2's planted difference is not selected
    assert "p2" not in out and "2 of 2 pairs identical" in out
    assert opener.opened == ["fake://p1-a", "fake://p1-b", "fake://p3-a", "fake://p3-b"]
    code, _, err = run_cli([path, "--only", "p4,p11"], Opener(tables))
    assert code == EXIT_ERROR and "'p4'" in err and "did you mean 'p1'?" in err


def test_fail_fast_stops_at_the_first_pair_that_differs(tmp_path):
    """p2 differs: p3 and p4 are never opened and are listed as not run."""
    names = ["p1", "p2", "p3", "p4"]
    tables = tables_for(names, {"p2": SyntheticTable(500, changed={8: ("0.00", "z")})})
    opener = Opener(tables)
    code, out, _ = run_cli([write(tmp_path, pairs_toml(names)), "--fail-fast"], opener)
    assert code == EXIT_DIFFERENCES
    assert not any("p3" in c or "p4" in c for c in opener.opened)
    assert out.count("not run (--fail-fast)") == 2
    assert "1 of 4 pairs identical, 1 differ, 0 errors, 2 not run" in out


def test_ctrl_c_stops_cleanly_and_still_writes_the_summary(tmp_path):
    """Interrupted during pair 3 of 5: pairs 1-2 keep their verdicts (one
    planted difference), 3-5 are not run, every report says INCOMPLETE and
    the exit code is 2."""
    names = ["p1", "p2", "p3", "p4", "p5"]
    tables = tables_for(names, {"p2": SyntheticTable(500, deleted=[4])})
    tables["fake://p3-a"] = KeyboardInterrupt()
    md, junit = str(tmp_path / "r.md"), str(tmp_path / "r.xml")
    code, out, err = run_cli(
        [write(tmp_path, pairs_toml(names)), "--markdown", md, "--junit", junit],
        Opener(tables),
    )
    assert code == EXIT_ERROR
    assert "INCOMPLETE" in out and "interrupted" in err
    assert out.count("not run (interrupted)") == 3
    assert "1 of 5 pairs identical, 1 differ, 0 errors, 3 not run" in out
    report = Path(md).read_text("utf-8")
    assert "INCOMPLETE" in report and report.count("NOT RUN") >= 3
    suite = ET.parse(junit).getroot().find("testsuite")  # noqa: S314 - our own output
    assert suite.get("skipped") == "3" and suite.get("failures") == "1"


def test_fifty_pairs_find_exactly_the_three_planted_differences(tmp_path):
    """50 pairs, differences planted in 7, 23 and 41: exactly those differ."""
    names = [f"t{i:02d}" for i in range(1, 51)]
    plants = {
        "t07": SyntheticTable(300, changed={11: ("9.99", "q")}),
        "t23": SyntheticTable(300, deleted=[200]),
        "t41": SyntheticTable(300, extra={999: ("1.00", "new")}),
    }
    tables = tables_for(names, plants, n=300)
    code, out, _ = run_cli([write(tmp_path, pairs_toml(names)), "--json"], Opener(tables))
    assert code == EXIT_DIFFERENCES
    data = json.loads(out)
    assert data["totals"] == {
        "pairs": 50,
        "identical": 47,
        "differ": 3,
        "error": 0,
        "not_run": 0,
        "from_earlier_run": 0,
        "identical_with_qualifiers": 0,
    }
    differ = {
        p["name"]: [(d["key"], d["kind"]) for d in p["result"]["differences"]]
        for p in data["pairs"]
        if p["status"] == "differ"
    }
    assert differ == {
        "t07": [(11, "different")],
        "t23": [(200, "only_in_a")],
        "t41": [(999, "only_in_b")],
    }
    assert data["identical"] is False and data["complete"] is True


def test_per_pair_overrides_and_filters_reach_the_diff(tmp_path):
    """A pair's `where`, `map`, `exclude` and `threshold` are its own: the
    filter hides an odd-key plant; the next pair, unfiltered, finds it."""
    names = ["filtered", "whole"]
    plant = SyntheticTable(500, changed={7: ("0.00", "x"), 8: ("0.00", "x")})
    tables = tables_for(names, {"filtered": plant, "whole": plant})
    extra = {"filtered": 'where = "even"\nthreshold = 5'}
    cfg = read_config(write(tmp_path, pairs_toml(names, extra)))
    run = run_batch(cfg, opener=Opener(tables), environ={})
    keys = {
        o.spec.name: [d["key"] for d in o.result["differences"]] for o in run.outcomes
    }
    assert keys == {"filtered": [8], "whole": [7, 8]}
    assert run.outcomes[0].result["where_a"] == "even"
    assert run.outcomes[0].spec.threshold == 5


# ---------------------------------------------------------------------------
# 4. Every output format carries every qualifier
# ---------------------------------------------------------------------------


def _result(**changes) -> dict:
    """A real `parity diff --json` object from fakes (one planted change if
    asked), then edited to carry the qualifier under test."""
    b = (
        SyntheticTable(400, changed={5: ("0.00", "x")})
        if changes.pop("differ", False)
        else (SyntheticTable(400))
    )
    from parity.cli import to_dict

    data = to_dict(
        diff(
            FakeDialect(SyntheticTable(400), "A"),
            FakeDialect(b, "B"),
            "t",
            "t",
            "id",
            verify_sample=100,
        )
    )
    for path, value in changes.items():
        target = data
        *parents, leaf = path.split("__")
        for p in parents:
            target = target[p]
        target[leaf] = value
    return data


#: name -> (result edits, the text every format must show, still identical?)
QUALIFIED = {
    "filtered": (
        {"where_a": "region = 'eu'", "where_b": None},
        "filtered rows only",
        True,
    ),
    "caveat": ({"warnings": ["side A (x): CAVEAT-TEXT-42"]}, "CAVEAT-TEXT-42", True),
    "uncovered": ({"verification__unchecked": ["geom_col"]}, "geom_col", True),
    "sample_error": (
        {
            "verification__error": "SAMPLE-ERR-7",
            "risk__max_hidden_fraction": None,
            "identical": False,
        },
        "sample check could not run",
        False,
    ),
    "no_sample": (
        {"verification": None, "risk__max_hidden_fraction": None},
        "no independent sample check",
        True,
    ),
    "truncated": (
        {"differ": True, "truncated": True, "identical": False},
        "stopped early",
        False,
    ),
    "schema": (
        {
            "schema_differences": ["present only on side A: ['extra_col']"],
            "identical": False,
        },
        "extra_col",
        False,
    ),
    "unexplained": (
        {"unexplained": ["UNEXPLAINED-RANGE-9"], "identical": False},
        "UNEXPLAINED-RANGE-9",
        False,
    ),
    "sample_failure": (
        {"verification__failures": ["key 5: SAMPLE-FAIL-3"], "identical": False},
        "SAMPLE-FAIL-3",
        False,
    ),
}


def _qualified_run() -> BatchRun:
    """A run of one pair per qualifier, plus one from an earlier run."""
    cfg = parse_config(
        {
            "connections": {"x": "fake://x"},
            "pair": [
                {"name": n, "a": "x", "b": "x", "table": "t", "key": "id"}
                for n in [*QUALIFIED, "earlier"]
            ],
        },
        "q.toml",
        "ab" * 32,
    )
    outcomes = []
    for spec in cfg.pairs:
        if spec.name == "earlier":
            outcomes.append(
                PairOutcome(
                    spec,
                    "identical",
                    result=_result(),
                    earlier_run=True,
                    finished_at="2026-11-01T10:00:00Z",
                )
            )
            continue
        edits, _, still = QUALIFIED[spec.name]
        data = _result(**dict(edits))
        assert bool(data["identical"]) == still
        outcomes.append(
            PairOutcome(
                spec, batch._status(data), result=data, finished_at="2026-11-04T00:00:00Z"
            )
        )
    return BatchRun(
        cfg,
        outcomes,
        started_at="2026-11-04T00:00:00Z",
        finished_at="2026-11-04T00:01:00Z",
        state_path="s.json",
    )


def _human(run: BatchRun) -> str:
    out = io.StringIO()
    report = batch.HumanReport(out)
    for o in run.outcomes:
        report.pair(o)
    report.totals(run)
    return out.getvalue()


def _section(text: str, start: str, stop: str | None) -> str:
    """The slice of a report from `start` to `stop` (or the end)."""
    i = text.index(start)
    j = text.index(stop, i + len(start)) if stop else len(text)
    return text[i:j]


def test_every_format_shows_every_qualifier_on_the_pair_it_applies_to():
    """Human, JSON, Markdown and JUnit: each qualified pair's own block names
    its qualifier, and a pair that is not identical is never shown as one."""
    run = _qualified_run()
    names = [o.spec.name for o in run.outcomes]
    human = _human(run)
    md = render_markdown(run)
    data = run_to_dict(run)
    suite = ET.fromstring(render_junit(run)).find("testsuite")  # noqa: S314
    cases = {c.get("name"): c for c in suite.findall("testcase")}
    for i, name in enumerate(names):
        nxt = names[i + 1] if i + 1 < len(names) else None
        _, text, still = QUALIFIED.get(name, ({}, "earlier run", True))
        # human: the pair's line and its indented qualifier lines
        block = _section(human, f" {name} ", f" {nxt} " if nxt else "\n\n")
        assert text in block, (name, block)
        (line,) = [ln for ln in human.splitlines() if ln[2:].startswith(f"{name} ")]
        assert line[0] == ("=" if still else "x"), line  # StringIO: ASCII glyphs
        # JSON: the pair's own qualifiers list and its untouched result
        pair = data["pairs"][i]
        assert pair["identical"] is still
        assert any(text in q["text"] for q in pair["qualifiers"]), (
            name,
            pair["qualifiers"],
        )
        # Markdown: the pair's own section
        md_name = batch._md(name)
        section = _section(
            md, f"## {md_name}: ", f"## {batch._md(nxt)}: " if nxt else None
        )
        assert batch._md(text) in section or text in section, (name, section)
        assert ("IDENTICAL" if still else "NOT IDENTICAL") in section.splitlines()[0]
        # JUnit: system-out of the pair's testcase; failure when not identical
        case = cases[name]
        assert text in case.find("system-out").text, name
        assert (case.find("failure") is None) == still
    assert data["totals"]["identical_with_qualifiers"] == 5
    assert "5 identical pair(s) carry qualifiers" in human


def test_the_summary_is_never_stronger_than_its_weakest_pair():
    """One unverified pair among identical ones: the run is not identical in
    any format, and the weakest risk line names a pair with no figure."""
    run = _qualified_run()
    assert not run.identical and run.exit_code == EXIT_DIFFERENCES
    assert run_to_dict(run)["identical"] is False
    assert "**Verdict: NOT IDENTICAL**" in render_markdown(run)
    assert "weakest identical pair (no_sample)" in _human(run)


def test_markdown_header_and_differences(tmp_path):
    """Version, config hash, dates; differences listed with both values,
    capped, and markdown in the data made inert."""
    names = ["p1"]
    changed = {k: ("0.00", f"bad|*_{k}") for k in range(1, 31)}
    tables = tables_for(names, {"p1": SyntheticTable(500, changed=changed)})
    path = write(tmp_path, pairs_toml(names))
    md = str(tmp_path / "r.md")
    code, _, _ = run_cli([path, "--markdown", md, "--quiet"], Opener(tables))
    assert code == EXIT_DIFFERENCES
    text = Path(md).read_text("utf-8")
    assert f"| Config SHA-256 | `{read_config(path).sha256}` |" in text
    assert "| Tool | parity " in text
    assert text.count("| different | ") == batch.MARKDOWN_DIFF_LIMIT * 2  # two columns
    assert "40 more difference(s)" not in text
    assert "10 more difference(s) not listed" in text
    assert "bad\\|\\*\\_1" in text  # pipes and emphasis escaped


def test_junit_is_valid_xml_with_one_testcase_per_pair(tmp_path):
    """A failure for a difference, an error for an error, a pass otherwise;
    a control character in a value cannot break the XML."""
    names = ["ok", "bad", "broken"]
    tables = tables_for(
        names, {"bad": SyntheticTable(500, changed={3: ("0.00", "a\x01b")})}
    )
    tables["fake://broken-a"] = ConnectionError()
    junit = str(tmp_path / "r.xml")
    code, _, _ = run_cli(
        [write(tmp_path, pairs_toml(names)), "--junit", junit, "--quiet"], Opener(tables)
    )
    assert code == EXIT_ERROR
    suite = ET.parse(junit).getroot().find("testsuite")  # noqa: S314 - our own output
    assert (suite.get("tests"), suite.get("failures"), suite.get("errors")) == (
        "3",
        "1",
        "1",
    )
    cases = {c.get("name"): c for c in suite.findall("testcase")}
    assert cases["ok"].find("failure") is None and cases["ok"].find("error") is None
    assert cases["bad"].find("failure").get("message") == "1 difference"
    assert "could not connect" in cases["broken"].find("error").get("message")


def test_json_pairs_carry_exactly_the_single_pair_json(tmp_path):
    """`pairs[].result` is `parity diff --json` for that pair: same fields,
    same verdict and differences."""
    names = ["p1"]
    tables = tables_for(names, {"p1": SyntheticTable(500, changed={9: ("1.00", "x")})})
    code, out, _ = run_cli([write(tmp_path, pairs_toml(names)), "--json"], Opener(tables))
    result = json.loads(out)["pairs"][0]["result"]
    from parity.cli import to_dict

    single = to_dict(
        diff(
            FakeDialect(SyntheticTable(500), "A"),
            FakeDialect(SyntheticTable(500, changed={9: ("1.00", "x")}), "B"),
            "t",
            "t",
            "id",
            verify_sample=200,
        )
    )
    assert set(result) == set(single)
    assert result["differences"] == single["differences"]
    assert result["identical"] is single["identical"] is False


# ---------------------------------------------------------------------------
# 5. --resume
# ---------------------------------------------------------------------------


def test_resume_skips_identical_pairs_and_reruns_the_rest(tmp_path):
    """Run 1: p2 differs. Run 2 with the same state: p1 and p3 are carried
    over (never opened, shown with their timestamp), p2 runs again - and,
    now fixed, passes."""
    names = ["p1", "p2", "p3"]
    path = write(tmp_path, pairs_toml(names))
    state = str(tmp_path / "state.json")
    first = Opener(tables_for(names, {"p2": SyntheticTable(500, deleted=[10])}))
    code, _, _ = run_cli([path, "--resume", state], first)
    assert code == EXIT_DIFFERENCES
    stored = json.loads(Path(state).read_text("utf-8"))
    assert {n: e["status"] for n, e in stored["pairs"].items()} == {
        "p1": "identical",
        "p2": "differ",
        "p3": "identical",
    }

    second = Opener(tables_for(names))  # p2 fixed
    md = str(tmp_path / "r.md")
    code, out, _ = run_cli([path, "--resume", state, "--markdown", md], second)
    assert code == EXIT_IDENTICAL
    assert second.opened == ["fake://p2-a", "fake://p2-b"]
    when = stored["pairs"]["p1"]["finished_at"]
    assert f"from an earlier run ({when})" in out
    assert "2 pair(s) not re-run: verdict from an earlier run" in out
    assert "verdict from an earlier run** at " + when in Path(md).read_text("utf-8")
    code, out, _ = run_cli([path, "--resume", state, "--json"], Opener(tables_for(names)))
    flags = {p["name"]: p["from_earlier_run"] for p in json.loads(out)["pairs"]}
    assert flags == {"p1": True, "p2": True, "p3": True}


def test_resume_reruns_a_pair_whose_config_or_connection_changed(tmp_path):
    """Change p1's settings, or point p2's variable elsewhere: both run
    again, and the new planted difference is found."""
    text = pairs_toml(["p1", "p2"]).replace('"fake://p2-b"', '"${P2_B}"')
    path = write(tmp_path, text)
    state = str(tmp_path / "state.json")
    tables = tables_for(["p1", "p2"])
    tables["fake://other"] = SyntheticTable(500, deleted=[1])
    tables["fake://p2-b"] = SyntheticTable(500)
    code, _, _ = run_cli(
        [path, "--resume", state], Opener(tables), {"P2_B": "fake://p2-b"}
    )
    assert code == EXIT_IDENTICAL
    write(tmp_path, text.replace('key = "id"\n', 'key = "id"\nthreshold = 99\n', 1))
    opener = Opener(tables)
    code, out, _ = run_cli([path, "--resume", state], opener, {"P2_B": "fake://other"})
    assert code == EXIT_DIFFERENCES
    assert opener.opened == ["fake://p1-a", "fake://p1-b", "fake://p2-a", "fake://other"]
    assert "from an earlier run" not in out


def test_the_state_file_holds_no_password_and_a_broken_one_is_refused(tmp_path):
    """The connection is fingerprinted, not stored; garbage state is exit 2."""
    path = write(tmp_path, ENV_CONFIG)
    state = tmp_path / "state.json"
    tables = {
        f"fake://user:{SECRET}@host/db": SyntheticTable(100),
        "fake://new": SyntheticTable(100, deleted=[2]),
    }
    env = {"LEGACY_PW": SECRET, "NEW_URL": "fake://new"}
    code, _, _ = run_cli([path, "--resume", str(state)], Opener(tables), env)
    assert code == EXIT_DIFFERENCES
    assert SECRET not in state.read_text("utf-8")
    state.write_text("{not json", encoding="utf-8")
    code, _, err = run_cli([path, "--resume", str(state)], Opener(tables), env)
    assert code == EXIT_ERROR and "state file" in err


# ---------------------------------------------------------------------------
# 6. Through the real CLI, on DuckDB files
# ---------------------------------------------------------------------------


@pytest.fixture
def duck_files(tmp_path):
    """Two DuckDB files: orders with a planted change at an odd key and one
    at an even key; customers with a renamed column and one planted change."""
    duckdb = pytest.importorskip("duckdb")
    for name in ("a", "b"):
        con = duckdb.connect(str(tmp_path / f"{name}.duckdb"))
        con.execute(
            "create table orders as select i::bigint as id, (i * 1.5)::decimal(12,2) "
            "as amount, case when i % 2 = 0 then 'eu' else 'us' end as region "
            "from range(1, 3001) t(i)"
        )
        con.execute(
            "create table customers as select i::bigint as id, 'n' || i as nm "
            "from range(1, 801) t(i)"
        )
        if name == "b":
            con.execute("update orders set amount = amount + 1 where id in (11, 12)")
            con.execute("alter table customers rename nm to customer_name")
            con.execute("update customers set customer_name = 'moved' where id = 400")
        con.close()
    return tmp_path


def test_a_duckdb_config_through_main(duck_files, monkeypatch, capsys):
    """`parity run` via `main`: the where-pair sees only key 12, the whole
    pair both, the renamed pair key 400 under side A's name; exit 1."""
    monkeypatch.setenv("PARITY_B_FILE", str(duck_files / "b.duckdb").replace("\\", "/"))
    path = write(
        duck_files,
        f"""
[connections]
old = "duckdb:///{str(duck_files / "a.duckdb").replace(chr(92), "/")}"
new = "duckdb:///${{PARITY_B_FILE}}"

[defaults]
verify_sample = 500

[[pair]]
name = "orders-eu"
a = "old"
b = "new"
table = "main.orders"
key = "id"
where = "region = 'eu'"

[[pair]]
name = "orders"
a = "old"
b = "new"
table = "main.orders"
key = "id"

[[pair]]
name = "customers"
a = "old"
b = "new"
table = "customers"
key = "id"
map = {{ nm = "customer_name" }}
""",
    )
    code = main(["run", path, "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == EXIT_DIFFERENCES
    found = {
        p["name"]: [(d["key"], d["columns"]) for d in p["result"]["differences"]]
        for p in data["pairs"]
    }
    assert found == {
        "orders-eu": [(12, ["amount"])],
        "orders": [(11, ["amount"]), (12, ["amount"])],
        "customers": [(400, ["nm"])],
    }
    assert data["pairs"][0]["result"]["where_a"] == "region = 'eu'"
    assert data["pairs"][2]["result"]["column_map"] == {"nm": "customer_name"}
    assert str(duck_files / "b.duckdb").replace("\\", "/") not in json.dumps(data)


def test_main_reports_a_bad_config_as_exit_2(tmp_path, capsys):
    """Through `main`: a config error is `parity: ...` on stderr, exit 2."""
    code = main(["run", write(tmp_path, GOOD.replace("key =", "kye ="))])
    assert code == EXIT_ERROR
    assert "did you mean 'key'?" in capsys.readouterr().err


def test_parity_diff_is_unchanged_by_the_new_subcommand():
    """`diff` keeps its flags; `run` has its own."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "diff",
            "--a",
            "x",
            "--a-table",
            "t",
            "--b",
            "y",
            "--b-table",
            "t",
            "--key",
            "id",
        ]
    )
    assert args.command == "diff" and args.bisection_factor == 32
    args = parser.parse_args(["run", "c.toml", "--only", "a", "--only", "b,c"])
    assert args.only == ["a", "b,c"] and not args.fail_fast


def test_a_column_only_one_side_has_makes_the_pair_differ_not_pass(tmp_path):
    """A schema difference from the engine (not a hand-edited result): the
    pair is `differ`, and the summary names the column."""
    names = ["p1"]
    one_col = SyntheticTable(
        500, columns=[Column("amount", LogicalType.DECIMAL, "decimal")]
    )
    tables = tables_for(names, {"p1": one_col})
    code, out, _ = run_cli([write(tmp_path, pairs_toml(names))], Opener(tables))
    assert code == EXIT_DIFFERENCES
    assert "schemas differ" in out and "status" in out
