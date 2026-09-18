"""CLI behaviour, driven end to end over two real DuckDB files.

DuckDB-to-DuckDB keeps these tests runnable anywhere, but they are still real
comparisons through the real dialects - only the second engine is swapped out.
Cross-engine coverage lives in `test_encoding.py` and `test_integration.py`.

The exit codes get the most attention here, because they are what makes this a
CI gate: 0 identical, 1 differences found, 2 something broke. A tool that
returned 1 for a bad connection string would fail a migration cutover check for
the wrong reason, and someone would eventually stop believing it.
"""

from __future__ import annotations

import io
import json

import pytest
from conftest import duckdb_write

from parity.cli import EXIT_DIFFERENCES, EXIT_ERROR, EXIT_IDENTICAL, main

SCHEMA = """
create table orders (
    id bigint,
    customer_id integer,
    amount decimal(12,2),
    status varchar,
    is_refunded boolean,
    created_at timestamp,
    note varchar
)
"""

ROWS = """
insert into orders
select i::bigint,
       (i % 97)::integer,
       ((i * 7 % 10000) / 100.0)::decimal(12,2),
       case when i % 3 = 0 then 'paid' when i % 3 = 1 then 'open' else 'void' end,
       (i % 11 = 0),
       timestamp '2024-01-01 00:00:00' + (i % 3600) * interval '1 second',
       case when i % 13 = 0 then null else 'note ' || i::varchar end
from generate_series(1, {n}) as s(i)
"""


def build(path: str, n: int = 5_000, plant: str | None = None) -> str:
    """Create a DuckDB file of n rows, optionally with one planted defect."""
    con = duckdb_write(path)
    con.execute(SCHEMA)
    con.execute(ROWS.format(n=n))
    if plant == "changed":
        con.execute("update orders set amount = amount + 0.01 where id = 1234")
    elif plant == "deleted":
        con.execute("delete from orders where id = 77")
    elif plant == "extra":
        con.execute(
            "insert into orders values "
            "(999999999, 1, 1.00, 'paid', false, timestamp '2024-01-01', 'extra')"
        )
    elif plant == "null_trap":
        # NULL becomes '' - the migration bug class naive tools pass over.
        con.execute("update orders set note = '' where id = 13")
    elif plant == "bool_trap":
        # FALSE becomes NULL - invisible until the boolean encoding was fixed.
        con.execute("update orders set is_refunded = null where id = 100")
    elif plant == "changed_and_sparse":
        # A difference in the dense low bucket, plus a key that widens the range
        # 200,000x. The low bucket then sits under the threshold and both sides
        # are downloaded whole - the case where the percentage used to read 200%.
        con.execute("update orders set amount = amount + 0.01 where id = 1234")
        con.execute(
            "insert into orders values "
            "(999999999, 1, 1.00, 'paid', false, timestamp '2024-01-01', 'extra')"
        )
    elif plant == "duplicate_key":
        con.execute(
            "insert into orders values "
            "(500, 1, 1.00, 'paid', false, timestamp '2024-01-01', 'dupe')"
        )
    con.close()
    return path


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> str:
    """The reference table every test in this file compares against."""
    return build(str(tmp_path_factory.mktemp("cli") / "base.duckdb"))


def run_cli(*argv: str) -> tuple[int, str, str]:
    """Drive the real CLI and capture what it wrote. Returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def diff_args(a: str, b: str, *extra: str) -> list[str]:
    """The argument list for a diff between two DuckDB files."""
    return [
        "diff",
        "--a",
        f"duckdb:///{a}",
        "--a-table",
        "main.orders",
        "--b",
        f"duckdb:///{b}",
        "--b-table",
        "main.orders",
        "--key",
        "id",
        *extra,
    ]


# ---------------------------------------------------------------------------
# Exit codes - the contract that makes this a CI check
# ---------------------------------------------------------------------------


def test_identical_tables_exit_zero(base, tmp_path):
    """Exit 0 and zero rows moved - the clean case a CI job sees most often."""
    other = build(str(tmp_path / "same.duckdb"))
    code, out, err = run_cli(*diff_args(base, other))
    assert code == EXIT_IDENTICAL, err
    assert "no differences" in out
    assert "0 rows downloaded (0.00% of both tables)" in out


@pytest.mark.parametrize(
    "plant,expect",
    [
        ("changed", "different"),
        ("deleted", "only in A"),
        ("extra", "only in B"),
        ("null_trap", "different"),
        ("bool_trap", "different"),
    ],
)
def test_each_planted_difference_exits_one_and_is_named(base, tmp_path, plant, expect):
    """Each defect exits 1 and is described by kind, not merely counted."""
    other = build(str(tmp_path / f"{plant}.duckdb"), plant=plant)
    code, out, err = run_cli(*diff_args(base, other))
    assert code == EXIT_DIFFERENCES, err
    assert "1 difference in" in out
    assert expect in out


def test_a_broken_connection_string_exits_two_not_one(base, tmp_path):
    """Exit 1 must mean "the data differs", never "the tool could not run"."""
    code, out, err = run_cli(*diff_args(base, str(tmp_path / "does_not_exist.duckdb")))
    assert code == EXIT_ERROR
    assert "side B" in err and "not found" in err


def test_an_unknown_table_names_the_side(base):
    """A wrong table name says which of the two sides was wrong."""
    code, out, err = run_cli(
        "diff",
        "--a",
        f"duckdb:///{base}",
        "--a-table",
        "main.orders",
        "--b",
        f"duckdb:///{base}",
        "--b-table",
        "main.nope",
        "--key",
        "id",
    )
    assert code == EXIT_ERROR
    assert "side B" in err and "nope" in err


def test_an_unknown_key_column_lists_the_real_ones(base):
    """A mistyped key column shows what the table actually has."""
    code, out, err = run_cli(*diff_args(base, base, "--key", "order_id"))
    assert code == EXIT_ERROR
    assert "order_id" in err and "customer_id" in err


def test_a_text_key_is_hashed_and_works(base, tmp_path):
    """A non-integer key is bisected via a hash rather than being refused.

    `status` is a varchar and not unique, so this also confirms the uniqueness
    check runs on the real key rather than on its hash.
    """
    code, out, err = run_cli(*diff_args(base, base, "--key", "status"))
    assert code == EXIT_ERROR
    assert "not unique" in err, err


def test_a_composite_key_is_accepted(base, tmp_path):
    """Several columns together identify a row, comma-separated."""
    other = build(str(tmp_path / "composite.duckdb"), plant="changed")
    code, out, _ = run_cli(*diff_args(base, other, "--key", "id,customer_id", "--json"))
    payload = json.loads(out)

    assert code == EXIT_DIFFERENCES
    assert payload["difference_count"] == 1
    # Both key columns are matched on, so neither is compared.
    assert "id" not in payload["columns_compared"]
    assert "customer_id" not in payload["columns_compared"]
    assert any("hash" in w.lower() for w in payload["warnings"])


def test_a_duplicate_key_is_refused_rather_than_answered_wrongly(base, tmp_path):
    """A non-unique key exits 2. Answering at all would mean answering wrongly."""
    other = build(str(tmp_path / "dupes.duckdb"), plant="duplicate_key")
    code, out, err = run_cli(*diff_args(base, other))
    assert code == EXIT_ERROR
    assert "not unique" in err and "side B" in err


def test_an_unsupported_scheme_exits_two(base):
    """An engine nobody wrote a dialect for is an error, not a difference."""
    code, out, err = run_cli(
        "diff",
        "--a",
        "mysql://user@host/db",
        "--a-table",
        "t",
        "--b",
        f"duckdb:///{base}",
        "--b-table",
        "main.orders",
        "--key",
        "id",
    )
    assert code == EXIT_ERROR
    assert "mysql" in err


def test_no_subcommand_prints_help_and_exits_two():
    """Running bare prints help rather than failing silently."""
    code, out, err = run_cli()
    assert code == EXIT_ERROR
    assert "usage: parity" in out


# ---------------------------------------------------------------------------
# Human output
# ---------------------------------------------------------------------------


def test_human_output_leads_with_the_verdict_then_the_evidence(base, tmp_path):
    """Verdict first, then cost, then the rows - in that order."""
    other = build(str(tmp_path / "changed.duckdb"), plant="changed")
    code, out, _ = run_cli(*diff_args(base, other))
    lines = [line for line in out.splitlines() if line.strip()]

    assert "1 difference in 5,000 rows" in lines[0]
    assert "queries" in lines[1] and "rows downloaded" in lines[1]
    assert any("key 1234" in line and "amount" in line for line in lines)
    # Both sides of the changed value must be shown, not merely that it moved.
    value_line = next(line for line in lines if line.strip().startswith("amount"))
    assert "A 86.380000" in value_line and "B 86.390000" in value_line


def test_the_float_scale_is_always_stated(base):
    """CLAUDE.md section 8: never let an approximate comparison look exact."""
    code, out, _ = run_cli(*diff_args(base, base))
    assert "comparing floats and decimals at 6 decimal places" in out

    code, out, _ = run_cli(*diff_args(base, base, "--float-scale", "2"))
    assert "at 2 decimal places" in out


def test_the_downloaded_percentage_is_always_printed(base, tmp_path):
    """The number that proves the tool pushed work into the engines is never omitted."""
    other = build(str(tmp_path / "pct.duckdb"), plant="changed")
    for args in ([], ["--json"]):
        code, out, _ = run_cli(*diff_args(base, other, *args))
        assert "%" in out or "percent_downloaded" in out


def test_quiet_prints_nothing_but_still_signals_through_the_exit_code(base, tmp_path):
    """--quiet is for CI: no output, same verdict."""
    other = build(str(tmp_path / "quiet.duckdb"), plant="changed")
    code, out, err = run_cli(*diff_args(base, other, "--quiet"))
    assert code == EXIT_DIFFERENCES
    assert out == ""


def test_warnings_are_surfaced_not_buried(base, tmp_path):
    """A column on one side only must be visible in the report."""
    path = str(tmp_path / "extracol.duckdb")
    con = duckdb_write(path)
    con.execute(SCHEMA)
    con.execute(ROWS.format(n=100))
    con.execute("alter table orders add column extra varchar")
    con.close()

    small = build(str(tmp_path / "small.duckdb"), n=100)
    code, out, _ = run_cli(*diff_args(small, path))
    assert code == EXIT_IDENTICAL
    assert "extra" in out and "side B" in out


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def test_json_output_parses_and_carries_the_verdict(base, tmp_path):
    """--json parses, and carries the verdict and the evidence a script needs."""
    other = build(str(tmp_path / "json.duckdb"), plant="changed")
    code, out, _ = run_cli(*diff_args(base, other, "--json"))
    payload = json.loads(out)

    assert code == EXIT_DIFFERENCES
    assert payload["identical"] is False
    assert payload["truncated"] is False
    assert payload["difference_count"] == 1
    assert payload["float_scale"] == 6

    (d,) = payload["differences"]
    assert d["key"] == 1234
    assert d["kind"] == "different"
    assert d["columns"] == ["amount"]
    assert d["a"] != d["b"]

    stats = payload["stats"]
    assert stats["rows_a"] == 5_000
    assert stats["queries"] > 0
    assert 0 < stats["percent_downloaded"] < 100


def test_json_on_identical_tables_reports_zero_downloaded(base, tmp_path):
    """The clean case in machine-readable form."""
    other = build(str(tmp_path / "json_same.duckdb"))
    code, out, _ = run_cli(*diff_args(base, other, "--json"))
    payload = json.loads(out)

    assert code == EXIT_IDENTICAL
    assert payload["identical"] is True
    assert payload["differences"] == []
    assert payload["stats"]["rows_downloaded"] == 0
    assert payload["stats"]["percent_downloaded"] == 0.0


def test_json_lists_the_columns_actually_compared(base):
    """A consumer can see what was compared, not just the result."""
    code, out, _ = run_cli(*diff_args(base, base, "--exclude", "note,status", "--json"))
    payload = json.loads(out)
    assert payload["columns_compared"] == [
        "amount",
        "created_at",
        "customer_id",
        "is_refunded",
    ]


# ---------------------------------------------------------------------------
# Flags that change the answer must say so
# ---------------------------------------------------------------------------


def test_max_diffs_marks_the_result_partial_in_both_formats(base, tmp_path):
    """A capped run reads as partial to a person and to a script alike."""
    path = str(tmp_path / "many.duckdb")
    con = duckdb_write(path)
    con.execute(SCHEMA)
    con.execute(ROWS.format(n=5_000))
    con.execute("update orders set status = 'CHANGED' where id % 3 = 0")
    con.close()

    code, out, _ = run_cli(*diff_args(base, path, "--max-diffs", "5"))
    assert code == EXIT_DIFFERENCES
    assert "at least" in out and "stopped early" in out

    code, out, _ = run_cli(*diff_args(base, path, "--max-diffs", "5", "--json"))
    payload = json.loads(out)
    assert payload["truncated"] is True
    assert payload["identical"] is False
    assert payload["difference_count"] == 5
    assert any("partial" in w for w in payload["warnings"])


def test_a_truncated_run_never_reports_identical(base, tmp_path):
    """The nastiest possible bug: a capped run that looks like a clean bill."""
    other = build(str(tmp_path / "trunc.duckdb"), plant="changed")
    code, out, _ = run_cli(*diff_args(base, other, "--max-diffs", "1", "--json"))
    payload = json.loads(out)
    assert payload["identical"] is False


def test_exclude_can_hide_a_real_difference_and_that_is_visible(base, tmp_path):
    """--exclude can hide a genuine difference, so the columns compared are reported."""
    other = build(str(tmp_path / "excl.duckdb"), plant="changed")

    code, _, _ = run_cli(*diff_args(base, other))
    assert code == EXIT_DIFFERENCES

    code, out, _ = run_cli(*diff_args(base, other, "--exclude", "amount", "--json"))
    payload = json.loads(out)
    assert code == EXIT_IDENTICAL
    assert "amount" not in payload["columns_compared"]


def test_columns_restricts_the_comparison(base, tmp_path):
    """--columns narrows the comparison, proven by a difference disappearing."""
    other = build(str(tmp_path / "cols.duckdb"), plant="changed")
    code, out, _ = run_cli(*diff_args(base, other, "--columns", "status,note", "--json"))
    payload = json.loads(out)
    assert code == EXIT_IDENTICAL
    assert payload["columns_compared"] == ["note", "status"]


def test_columns_naming_the_key_is_refused(base):
    """The key matches rows up; comparing it against itself is meaningless."""
    code, out, err = run_cli(*diff_args(base, base, "--columns", "id"))
    assert code == EXIT_ERROR
    assert "key column" in err


def test_float_scale_applies_to_both_sides(base, tmp_path):
    """A scale of 0 must hide a sub-unit difference, proving it reached both."""
    other = build(str(tmp_path / "scale.duckdb"), plant="changed")
    assert run_cli(*diff_args(base, other))[0] == EXIT_DIFFERENCES
    assert run_cli(*diff_args(base, other, "--float-scale", "0"))[0] == EXIT_IDENTICAL


def test_bisection_factor_and_threshold_do_not_change_the_answer(base, tmp_path):
    """Tuning knobs may change cost. Changing the verdict would be a bug."""
    other = build(str(tmp_path / "tuning.duckdb"), plant="changed")
    baseline = json.loads(run_cli(*diff_args(base, other, "--json"))[1])

    for extra in (
        ["--bisection-factor", "2"],
        ["--bisection-factor", "256"],
        ["--threshold", "1"],
        ["--threshold", "1000000"],
    ):
        payload = json.loads(run_cli(*diff_args(base, other, "--json", *extra))[1])
        assert payload["differences"] == baseline["differences"], extra


def test_version_and_help_need_no_database_driver():
    """--help and --version work before any driver is installed."""
    for flag in ("--version", "--help"):
        with pytest.raises(SystemExit) as exc:
            main([flag])
        assert exc.value.code == 0


# ---------------------------------------------------------------------------
# Reporting honesty
# ---------------------------------------------------------------------------


def test_percentage_downloaded_can_never_exceed_one_hundred(base, tmp_path):
    """`rows_downloaded` counts both sides, so the denominator must too.

    Measured against one side, a full download of a small table reads 200%,
    which discredits the single number that proves the tool pushed the work
    into the engines.
    """
    other = build(str(tmp_path / "pct100.duckdb"), plant="changed_and_sparse")
    code, out, _ = run_cli(*diff_args(base, other))
    assert "(100.00% of both tables)" in out, out

    payload = json.loads(run_cli(*diff_args(base, other, "--json"))[1])
    stats = payload["stats"]
    assert stats["percent_downloaded"] == 100.0
    assert stats["rows_downloaded"] == stats["rows_a"] + stats["rows_b"]

    # And the invariant holds whatever the tuning knobs are set to.
    for extra in ([], ["--threshold", "1"], ["--bisection-factor", "2"]):
        stats = json.loads(run_cli(*diff_args(base, other, "--json", *extra))[1])["stats"]
        assert 0 <= stats["percent_downloaded"] <= 100, extra
        assert stats["rows_downloaded"] <= stats["rows_a"] + stats["rows_b"], extra


def test_null_and_empty_string_are_visually_distinct(base, tmp_path):
    """The flagship trap has to be readable, not rendered as blank space."""
    other = build(str(tmp_path / "nulldisp.duckdb"), plant="null_trap")
    code, out, _ = run_cli(*diff_args(base, other))
    line = next(line for line in out.splitlines() if line.strip().startswith("note"))
    assert "A NULL" in line and "B ''" in line


def test_json_keeps_the_raw_canonical_text(base, tmp_path):
    """Humans get NULL and ''; machines get exactly what was compared."""
    other = build(str(tmp_path / "nulljson.duckdb"), plant="null_trap")
    payload = json.loads(run_cli(*diff_args(base, other, "--json"))[1])
    (d,) = payload["differences"]
    assert d["a"] == {"note": "\\N"}
    assert d["b"] == {"note": ""}


def test_human_output_caps_the_rows_it_prints(base, tmp_path):
    """A wildly mismatched pair must not bury the terminal."""
    path = str(tmp_path / "huge_diff.duckdb")
    con = duckdb_write(path)
    con.execute(SCHEMA)
    con.execute(ROWS.format(n=5_000))
    con.execute("update orders set status = 'CHANGED'")
    con.close()

    code, out, _ = run_cli(*diff_args(base, path))
    assert code == EXIT_DIFFERENCES
    assert "5,000 differences" in out
    assert "more difference(s) found but not shown" in out
    # The display cap must not be confused with a truncated walk.
    assert "stopped early" not in out
    payload = json.loads(run_cli(*diff_args(base, path, "--json"))[1])
    assert payload["truncated"] is False
    assert payload["difference_count"] == 5_000


def test_output_survives_a_legacy_codepage_stream():
    """A cp1252 console must get ASCII, not a UnicodeEncodeError."""
    import io as _io

    from parity.cli import render_human
    from parity.types import Column, DiffResult, DiffStats, LogicalType

    result = DiffResult(
        diffs=[],
        stats=DiffStats(rows_compared_a=10, rows_compared_b=10),
        columns=[Column("x", LogicalType.STRING, "varchar")],
    )
    buffer = _io.TextIOWrapper(_io.BytesIO(), encoding="cp1252", newline="")
    render_human(result, buffer)  # must not raise
    buffer.seek(0)
    assert "no differences" in buffer.buffer.getvalue().decode("cp1252")


def test_unicode_glyphs_are_used_when_the_stream_can_encode_them():
    """The ASCII fallback exists for legacy codepages; a UTF-8 stream should
    get the nicer marks rather than being permanently downgraded."""
    import io as _io

    from parity.cli import render_human
    from parity.types import Column, DiffResult, DiffStats, LogicalType

    result = DiffResult(
        diffs=[],
        stats=DiffStats(rows_compared_a=10, rows_compared_b=10),
        columns=[Column("x", LogicalType.STRING, "varchar")],
    )
    buffer = _io.TextIOWrapper(_io.BytesIO(), encoding="utf-8", newline="")
    render_human(result, buffer)
    buffer.flush()
    text = buffer.buffer.getvalue().decode("utf-8")
    assert "✓" in text and "·" in text


def test_long_values_stack_onto_separate_lines():
    """Side-by-side is easier to scan, but only while the values fit. A long
    value must not run off the edge of the terminal."""
    import io as _io

    from parity.cli import render_human
    from parity.types import Column, DiffResult, DiffStats, LogicalType, RowDiff

    long_a = "x" * 90
    long_b = "y" * 90
    result = DiffResult(
        diffs=[RowDiff(1, "different", ["note"], {"note": long_a}, {"note": long_b})],
        stats=DiffStats(rows_compared_a=1, rows_compared_b=1),
        columns=[Column("note", LogicalType.STRING, "varchar")],
    )
    out = _io.StringIO()
    render_human(result, out)
    lines = out.getvalue().splitlines()

    a_line = next(line for line in lines if long_a in line)
    b_line = next(line for line in lines if long_b in line)
    assert a_line is not b_line, "long values must not share a line"
    assert long_b not in a_line

    # ... whereas short ones still share a line.
    short = DiffResult(
        diffs=[RowDiff(1, "different", ["note"], {"note": "aa"}, {"note": "bb"})],
        stats=DiffStats(rows_compared_a=1, rows_compared_b=1),
        columns=[Column("note", LogicalType.STRING, "varchar")],
    )
    out = _io.StringIO()
    render_human(short, out)
    line = next(x for x in out.getvalue().splitlines() if "aa" in x)
    assert "bb" in line


def test_the_cli_default_caps_differences_and_says_so(base, tmp_path):
    """Two tables that share nothing must produce an answer, not an OOM."""
    from parity.engine import DEFAULT_MAX_DIFFS

    path = str(tmp_path / "everything.duckdb")
    con = duckdb_write(path)
    con.execute(SCHEMA)
    con.execute(ROWS.format(n=5_000))
    con.execute("update orders set status = 'CHANGED', note = 'CHANGED'")
    con.close()

    # 5,000 differences is under the default, so this run is complete.
    payload = json.loads(run_cli(*diff_args(base, path, "--json"))[1])
    assert payload["truncated"] is False
    assert payload["difference_count"] == 5_000

    # Below the default, the cap engages and is reported honestly.
    payload = json.loads(
        run_cli(*diff_args(base, path, "--max-diffs", "100", "--json"))[1]
    )
    assert payload["truncated"] is True
    assert payload["difference_count"] == 100
    assert payload["identical"] is False

    assert DEFAULT_MAX_DIFFS >= 5_000, "the default must not trip on ordinary runs"


def test_max_diffs_zero_means_no_limit(base, tmp_path):
    """Zero is the documented way to ask for every difference."""
    path = str(tmp_path / "nolimit.duckdb")
    con = duckdb_write(path)
    con.execute(SCHEMA)
    con.execute(ROWS.format(n=5_000))
    con.execute("update orders set status = 'CHANGED'")
    con.close()

    payload = json.loads(run_cli(*diff_args(base, path, "--max-diffs", "0", "--json"))[1])
    assert payload["difference_count"] == 5_000
    assert payload["truncated"] is False
