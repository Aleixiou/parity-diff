"""Red team: the CLI, its output, and the promises it makes.

Each test here either pins a behaviour that held up under attack, or - marked
``xfail(strict=True)`` with an ``RT-F<n>`` reason - reproduces a confirmed bug in
as few lines as possible. A strict xfail turns into a failure the moment the bug
is fixed, so the marker has to come off with the fix.

The angle is the user-facing result: what the exit code, the human report and
`--json` tell a person or a CI job, whether an error can leak a password, and
whether the tool keeps its two absolute promises - read-only by construction
and no network calls beyond the two databases (CLAUDE.md section 6).

Everything that touches a live server creates only `rt_cli_`-prefixed objects
and drops them again.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import socket
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote as urlquote
from urllib.parse import unquote, urlparse

import pytest
from conftest import duckdb_write

from parity.cli import EXIT_DIFFERENCES, EXIT_ERROR, EXIT_IDENTICAL, main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCHEMA = """
create table orders (
    id bigint,
    amount decimal(12,2),
    status varchar,
    note varchar
)
"""

ROWS = """
insert into orders
select i::bigint,
       ((i * 7 % 10000) / 100.0)::decimal(12,2),
       case when i % 3 = 0 then 'paid' else 'open' end,
       case when i % 13 = 0 then null else 'note ' || i::varchar end
from generate_series(1, {n}) as s(i)
"""


def build(path: str, n: int = 2_000, *sql: str, params: list | None = None) -> str:
    """Create a DuckDB `orders` table of n rows, then run any extra statements."""
    con = duckdb_write(path)
    try:
        con.execute(SCHEMA)
        con.execute(ROWS.format(n=n))
        for i, stmt in enumerate(sql):
            if params is not None and params[i] is not None:
                con.execute(stmt, params[i])
            else:
                con.execute(stmt)
    finally:
        con.close()
    return path


def run_cli(*argv: str) -> tuple[int, str, str]:
    """Drive the real CLI and capture what it wrote. Returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def duck_args(a: str, b: str, *extra: str) -> list[str]:
    """Arguments for a DuckDB-to-DuckDB diff of `main.orders`, keyed on id."""
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


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> str:
    """The untouched reference table."""
    pytest.importorskip("duckdb")
    return build(str(tmp_path_factory.mktemp("rt_cli") / "rt_cli_base.duckdb"))


# ---------------------------------------------------------------------------
# Exit codes and verdicts
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="RT-F1: a column dropped on side B exits 0 'identical' and --quiet "
    "hides the only warning, so a CI gate passes a lossy migration",
)
def test_quiet_ci_gate_does_not_pass_a_dropped_column_silently(base, tmp_path):
    """The README's own CI recipe is `--quiet`, relying on the exit code alone.

    Side B lost the whole `amount` column - the most destructive migration
    defect there is. The run exits 0 and, under `--quiet`, prints nothing at
    all, so the CI step goes green. Either the exit code or stderr must carry
    the fact that a column was not compared.
    """
    b = build(str(tmp_path / "rt_cli_b.duckdb"), 2_000, "alter table orders drop amount")
    code, out, err = run_cli(*duck_args(base, b, "--quiet"))
    assert code != EXIT_IDENTICAL or "amount" in (out + err)


def test_dropped_column_is_at_least_named_without_quiet(base, tmp_path):
    """Without --quiet the skipped column is named, in both formats."""
    b = build(str(tmp_path / "rt_cli_b.duckdb"), 2_000, "alter table orders drop amount")
    code, out, _ = run_cli(*duck_args(base, b))
    assert "present only on side A: ['amount']" in out
    code_j, out_j, _ = run_cli(*duck_args(base, b, "--json"))
    payload = json.loads(out_j)
    assert any("amount" in w for w in payload["warnings"])
    assert "amount" not in payload["columns_compared"]
    # Documented behaviour (README "Limitations"), but note: exit 0, identical.
    assert code == code_j == EXIT_IDENTICAL and payload["identical"] is True


@pytest.mark.xfail(
    strict=True,
    reason="RT-F1: side B keeping only the key column exits 0 'no differences' "
    "although every value on A changed - only a trailing warning says so",
)
def test_a_key_only_side_b_is_not_reported_identical(base, tmp_path):
    """The extreme of RT-F1: B lost every non-key column.

    The headline reads `no differences in 2,000 rows` and the exit code is 0;
    the only signal is two `!` lines at the very bottom.
    """
    b = build(
        str(tmp_path / "rt_cli_ko.duckdb"),
        2_000,
        "alter table orders drop amount",
        "alter table orders drop status",
        "alter table orders drop note",
    )
    code, out, _ = run_cli(*duck_args(base, b))
    assert "no comparable columns" in out  # the warning is there...
    assert code != EXIT_IDENTICAL  # ...but the verdict is still "identical"


def test_quiet_wins_over_json_and_keeps_the_exit_code(base, mixed):
    """`--quiet --json` prints nothing at all, and the exit code still carries it."""
    assert run_cli(*duck_args(base, mixed, "--quiet", "--json")) == (
        EXIT_DIFFERENCES,
        "",
        "",
    )
    assert run_cli(*duck_args(base, base, "--quiet", "--json")) == (
        EXIT_IDENTICAL,
        "",
        "",
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["diff", "--a", "duckdb:///:memory:"],  # required flags missing
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
            "--max-diffs",
            "ten",
        ],
        ["frobnicate"],
    ],
)
def test_usage_errors_exit_two_not_one(argv, capsys):
    """argparse's own exit is 2, matching EXIT_ERROR - never a verdict code."""
    with pytest.raises(SystemExit) as exc:
        main(argv, out=io.StringIO(), err=io.StringIO())
    assert exc.value.code == EXIT_ERROR
    assert capsys.readouterr().out == ""


def test_no_command_prints_help_and_exits_two():
    """Bare `parity` is not a verdict either."""
    code, out, _ = run_cli()
    assert code == EXIT_ERROR and "diff" in out


def test_json_always_carries_the_verdict_fields(base, tmp_path):
    """identical, truncated, warnings and float_scale are present on every path."""
    planted = build(
        str(tmp_path / "rt_cli_p.duckdb"),
        2_000,
        "update orders set amount = amount + 1 where id = 5",
    )
    for b, extra, want in (
        (base, (), EXIT_IDENTICAL),
        (planted, (), EXIT_DIFFERENCES),
        (planted, ("--max-diffs", "0"), EXIT_DIFFERENCES),
    ):
        code, out, _ = run_cli(*duck_args(base, b, "--json", *extra))
        payload = json.loads(out)
        assert code == want
        for field in ("identical", "truncated", "warnings", "float_scale"):
            assert field in payload, field
        assert payload["identical"] is (want == EXIT_IDENTICAL)


def test_an_error_with_json_never_emits_a_verdict_on_stdout(base):
    """A failure under --json writes nothing a parser could read as a verdict."""
    code, out, err = run_cli(
        "diff",
        "--a",
        f"duckdb:///{base}",
        "--a-table",
        "main.nope",
        "--b",
        f"duckdb:///{base}",
        "--b-table",
        "main.orders",
        "--key",
        "id",
        "--json",
    )
    assert code == EXIT_ERROR
    assert out == ""
    assert "side A" in err and "nope" in err


@pytest.mark.parametrize(
    "plant, needle",
    [
        ("insert into orders select * from orders where id = 9", "not unique"),
        ("insert into orders values (null, 1, 'x', 'y')", "NULL"),
    ],
)
def test_refused_keys_exit_two_with_quiet_and_json(base, tmp_path, plant, needle):
    """A refused key is an error (2), never a verdict - even under --quiet/--json."""
    b = build(str(tmp_path / "rt_cli_k.duckdb"), 2_000, plant)
    for extra in (("--quiet",), ("--json",)):
        code, out, err = run_cli(*duck_args(base, b, *extra))
        assert code == EXIT_ERROR
        assert out == ""
        assert needle in err and "side B" in err


@pytest.mark.xfail(
    strict=True,
    reason="RT-F10: --max-diffs -1 is accepted and turns identical tables "
    "into 'at least 0 differences', exit 1",
)
def test_negative_max_diffs_does_not_invent_a_difference(base):
    """Identical tables must never exit 1. A negative limit should be refused."""
    code, out, _ = run_cli(*duck_args(base, base, "--max-diffs", "-1"))
    assert code != EXIT_DIFFERENCES
    assert "at least 0" not in out


# ---------------------------------------------------------------------------
# max_diffs: exactly N, N +/- 1, mixed kinds
# ---------------------------------------------------------------------------

#: Six differences of all three kinds, spread over the key range so they land
#: in different buckets: 2 changed, 2 deleted from B, 2 extra on B.
MIXED = (
    "update orders set amount = amount + 1 where id in (10, 1500)",
    "delete from orders where id in (300, 1999)",
    "insert into orders values (5000, 1, 'x', 'y'), (90000, 1, 'x', 'y')",
)
N_MIXED = 6


@pytest.fixture(scope="module")
def mixed(tmp_path_factory) -> str:
    """Side B with six planted differences of mixed kinds."""
    pytest.importorskip("duckdb")
    return build(
        str(tmp_path_factory.mktemp("rt_cli") / "rt_cli_mixed.duckdb"), 2_000, *MIXED
    )


@pytest.mark.parametrize("threshold", ["10000", "1"])
@pytest.mark.parametrize(
    "limit, count, truncated",
    [
        (N_MIXED - 1, N_MIXED - 1, True),
        (N_MIXED, N_MIXED, False),
        (N_MIXED + 1, N_MIXED, False),
        (1, 1, True),
        (0, N_MIXED, False),
    ],
)
def test_max_diffs_boundaries(base, mixed, limit, count, truncated, threshold):
    """Exactly N differences with --max-diffs N is complete; N-1 is partial.

    Run at the default threshold (one download of the whole table) and at a
    threshold of 1 (bisect down to single keys), because the limit is checked
    in two different places on those two paths.
    """
    code, out, _ = run_cli(
        *duck_args(
            base, mixed, "--json", "--max-diffs", str(limit), "--threshold", threshold
        )
    )
    payload = json.loads(out)
    assert code == EXIT_DIFFERENCES
    assert payload["difference_count"] == count
    assert payload["truncated"] is truncated
    assert payload["identical"] is False
    kinds = {d["kind"] for d in payload["differences"]}
    if not truncated:
        assert kinds == {"only_in_a", "only_in_b", "different"}
        assert sorted(d["key"] for d in payload["differences"]) == [
            10,
            300,
            1500,
            1999,
            5000,
            90000,
        ]
    else:
        assert any("partial answer" in w for w in payload["warnings"])
    _, human, _ = run_cli(
        *duck_args(base, mixed, "--max-diffs", str(limit), "--threshold", threshold)
    )
    assert ("stopped early" in human) is truncated
    assert "no differences" not in human


# ---------------------------------------------------------------------------
# --columns / --exclude / --key parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag, value",
    [
        ("--columns", " amount , AMOUNT ,amount,, "),
        ("--columns", "Amount"),
        ("--exclude", "status, NOTE ,"),
    ],
)
def test_column_lists_tolerate_spaces_case_and_duplicates(base, tmp_path, flag, value):
    """Spaces, repeats, blanks and case never hide the planted amount change."""
    b = build(
        str(tmp_path / "rt_cli_c.duckdb"),
        2_000,
        "update orders set amount = amount + 1 where id = 77",
    )
    code, out, _ = run_cli(*duck_args(base, b, flag, value, "--json"))
    payload = json.loads(out)
    assert code == EXIT_DIFFERENCES
    assert [d["key"] for d in payload["differences"]] == [77]
    assert payload["differences"][0]["columns"] == ["amount"]


def test_key_with_spaces_and_repeats_is_one_key(base, tmp_path):
    """`--key " id , id ,"` resolves to the single integer key, not a composite."""
    b = build(
        str(tmp_path / "rt_cli_k2.duckdb"), 2_000, "delete from orders where id = 3"
    )
    code, out, _ = run_cli(
        "diff",
        "--a",
        f"duckdb:///{base}",
        "--a-table",
        "main.orders",
        "--b",
        f"duckdb:///{b}",
        "--b-table",
        "main.orders",
        "--key",
        " id , id ,",
        "--json",
    )
    payload = json.loads(out)
    assert code == EXIT_DIFFERENCES
    assert payload["differences"][0]["key"] == 3
    assert not any("hash" in w for w in payload["warnings"])


@pytest.mark.xfail(
    strict=True,
    reason="RT-F15: --key de-duplicates case-sensitively but matches "
    "case-insensitively, so `ID,id` becomes a hashed composite of one column",
)
def test_key_named_twice_in_two_cases_is_one_key(base, tmp_path):
    """The difference is still found, but keyed `33` and hashed."""
    b = build(
        str(tmp_path / "rt_cli_k3.duckdb"), 2_000, "delete from orders where id = 3"
    )
    args = duck_args(base, b, "--json")
    args[args.index("id")] = "ID,id"
    code, out, _ = run_cli(*args)
    payload = json.loads(out)
    assert code == EXIT_DIFFERENCES
    assert payload["differences"][0]["key"] == 3


@pytest.mark.parametrize("value", ["", " ", ",", " , "])
def test_an_empty_key_is_an_error_not_a_verdict(base, value):
    """An empty --key (an unset CI variable) must exit 2."""
    code, out, err = run_cli(
        "diff",
        "--a",
        f"duckdb:///{base}",
        "--a-table",
        "main.orders",
        "--b",
        f"duckdb:///{base}",
        "--b-table",
        "main.orders",
        "--key",
        value,
    )
    assert code == EXIT_ERROR and out == ""


@pytest.mark.parametrize("value", ["", ",", " , "])
def test_an_empty_columns_flag_compares_everything(base, tmp_path, value):
    """An empty --columns falls back to every shared column - it never narrows to none."""
    b = build(
        str(tmp_path / "rt_cli_e.duckdb"),
        2_000,
        "update orders set note = 'changed' where id = 5",
    )
    code, out, _ = run_cli(*duck_args(base, b, "--columns", value, "--json"))
    assert code == EXIT_DIFFERENCES
    assert json.loads(out)["columns_compared"] == ["amount", "note", "status"]


def test_a_column_whose_name_has_a_comma_is_still_compared_by_default(tmp_path):
    """A `"a,b"` column cannot be named in --columns, but is never silently dropped."""
    a, b = str(tmp_path / "rt_cli_ca.duckdb"), str(tmp_path / "rt_cli_cb.duckdb")
    for path, v in ((a, "x"), (b, "y")):
        con = duckdb_write(path)
        con.execute('create table t (id int, "a,b" varchar)')
        con.execute("insert into t values (1, ?)", [v])
        con.close()
    args = ["diff", "--a", f"duckdb:///{a}", "--a-table", "t"]
    args += ["--b", f"duckdb:///{b}", "--b-table", "t", "--key", "id", "--json"]
    code, out, _ = run_cli(*args)
    assert code == EXIT_DIFFERENCES
    assert json.loads(out)["differences"][0]["columns"] == ["a,b"]
    # Naming it splits on the comma; the result is an error, not a narrower run.
    code, _, err = run_cli(*args, "--columns", "a,b")
    assert code == EXIT_ERROR and "unknown columns" in err


# ---------------------------------------------------------------------------
# Human output fidelity
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="RT-F7: data reaches the terminal raw, so a value containing a "
    "newline or ANSI escapes can forge a 'no differences' line",
)
def test_a_hostile_value_cannot_forge_the_verdict_in_human_output(base, tmp_path):
    """A changed text value is printed verbatim, control characters included.

    A value holding `\\n✓ no differences in 2,000 rows` produces a line in the
    report that is indistinguishable from the real all-clear headline, and an
    ANSI sequence can move the cursor and overwrite the real one.
    """
    forged = "x\n✓ no differences in 2,000 rows\x1b[2K"
    b = build(
        str(tmp_path / "rt_cli_h.duckdb"),
        2_000,
        "update orders set note = ? where id = 5",
        params=[[forged]],
    )
    code, out, _ = run_cli(*duck_args(base, b))
    assert code == EXIT_DIFFERENCES
    assert "\x1b" not in out
    assert not any(line.startswith("✓ no differences") for line in out.splitlines())


@pytest.mark.xfail(
    strict=True,
    reason="RT-F7: a text key reaches the terminal raw, so a hostile key on "
    "one side can forge a 'no differences' line and carry ANSI escapes",
)
def test_a_hostile_key_cannot_forge_the_verdict_in_human_output(tmp_path):
    """Same as above through the key: an extra row on B whose key is the forgery."""
    a, b = str(tmp_path / "rt_cli_fa.duckdb"), str(tmp_path / "rt_cli_fb.duckdb")
    forged = "k\n✓ no differences in 3 rows\x1b[1A"
    for path, extra in ((a, None), (b, forged)):
        con = duckdb_write(path)
        con.execute("create table t (k varchar, v varchar)")
        con.execute("insert into t values ('a', '1'), ('b', '2'), ('c', '3')")
        if extra:
            con.execute("insert into t values (?, 'v')", [extra])
        con.close()
    args = ["diff", "--a", f"duckdb:///{a}", "--a-table", "t"]
    code, out, _ = run_cli(*args, "--b", f"duckdb:///{b}", "--b-table", "t", "--key", "k")
    assert code == EXIT_DIFFERENCES
    assert "\x1b" not in out
    assert not any(line.startswith("✓ no differences") for line in out.splitlines())


@pytest.mark.parametrize(
    "a_value, b_value",
    [
        (None, "NULL"),  # a real NULL against the four letters N-U-L-L
        ("", "''"),  # an empty string against two apostrophes
    ],
)
@pytest.mark.xfail(
    strict=True,
    reason="RT-F8: _display renders NULL as NULL and '' as '', so the strings "
    "'NULL' and \"''\" print identically to them and the human report shows a "
    "difference whose two sides look the same",
)
def test_display_never_shows_two_different_values_alike(tmp_path, a_value, b_value):
    """Row 5's note differs between the sides; the two printed values must too."""
    a = build(
        str(tmp_path / "rt_cli_na.duckdb"),
        50,
        "update orders set note = ? where id = 5",
        params=[[a_value]],
    )
    b = build(
        str(tmp_path / "rt_cli_nb.duckdb"),
        50,
        "update orders set note = ? where id = 5",
        params=[[b_value]],
    )
    code, out, _ = run_cli(*duck_args(a, b))
    assert code == EXIT_DIFFERENCES
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("note"))
    a_val, b_val = re.match(r"\s*note\s+A (.*?)\s+B (.*)$", line).groups()
    assert a_val != b_val, line


@pytest.mark.xfail(
    strict=True,
    reason="RT-F16: a differing value the console encoding cannot represent "
    "(cp1252 + CJK/emoji) kills the human report half-way with exit 2",
)
def test_unencodable_data_does_not_crash_the_human_report(base, tmp_path):
    """Windows writes redirected stdout (a CI log, `> report.txt`) in cp1252.

    `_symbols` already falls back to ASCII glyphs for exactly that console,
    but data values are printed as they are: the first changed row holding a
    character outside cp1252 raises UnicodeEncodeError after the headline was
    printed, so the run exits 2 ("the tool broke") with a truncated report
    instead of 1 with the evidence.
    """
    b = build(
        str(tmp_path / "rt_cli_u.duckdb"),
        2_000,
        "update orders set note = '東京 ✓' where id = 5",
    )
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")
    err = io.StringIO()
    code = main(duck_args(base, b), out=out, err=err)
    out.flush()
    assert code == EXIT_DIFFERENCES, err.getvalue()
    assert b"key 5" in raw.getvalue()


@pytest.mark.xfail(
    strict=True,
    reason="RT-F9: a text value spelled \\N is byte-identical to the NULL "
    "sentinel, so NULL versus the string '\\N' reports identical",
)
def test_null_versus_the_literal_sentinel_string_is_a_difference(base, tmp_path):
    """FALSE MATCH: row 13's note is NULL on A and the two characters `\\N` on B."""
    b = build(
        str(tmp_path / "rt_cli_s.duckdb"),
        2_000,
        "update orders set note = ? where id = 13",
        params=[["\\N"]],
    )
    code, out, _ = run_cli(*duck_args(base, b, "--json"))
    assert code == EXIT_DIFFERENCES, out


# ---------------------------------------------------------------------------
# Ctrl-C
# ---------------------------------------------------------------------------


def test_ctrl_c_mid_walk_prints_no_verdict(base, tmp_path, monkeypatch):
    """An interrupt between levels exits 2 and writes nothing to stdout.

    The interrupt is raised from the main thread's wait, exactly where a real
    Ctrl-C lands (see `engine._gather`), on the second level of the walk.
    """
    from parity import engine

    b = build(
        str(tmp_path / "rt_cli_i.duckdb"),
        2_000,
        "update orders set amount = amount + 1 where id = 5",
    )
    real = engine._gather
    calls = {"n": 0}

    def interrupted(fa, fb):
        calls["n"] += 1
        if calls["n"] == 3:  # columns, key_stats, then the first checksum level
            raise KeyboardInterrupt
        return real(fa, fb)

    monkeypatch.setattr(engine, "_gather", interrupted)
    for extra in ((), ("--json",)):
        calls["n"] = 0
        code, out, err = run_cli(*duck_args(base, b, *extra))
        assert code == EXIT_ERROR
        assert out == ""
        assert "interrupted" in err


# ---------------------------------------------------------------------------
# Connection strings: errors name the side and never print the password
# ---------------------------------------------------------------------------

SECRET = "Zq9Secret"


def _connect_error(conn: str) -> str:
    """Run a diff with `conn` as side A and return everything it printed."""
    code, out, err = run_cli(
        "diff",
        "--a",
        conn,
        "--a-table",
        "t",
        "--b",
        "duckdb:///:memory:",
        "--b-table",
        "t",
        "--key",
        "id",
    )
    assert code == EXIT_ERROR
    return out + err


@pytest.mark.parametrize(
    "conn",
    [
        # Upper-case scheme: the dialect is chosen case-insensitively, but
        # libpq refuses it and echoes the entire connection string.
        f"Postgres://parity:{SECRET}@127.0.0.1:5432/parity",
        f"POSTGRESQL://parity:{SECRET}@127.0.0.1:5432/parity",
        # Unencoded space and a malformed %-escape: libpq quotes the token.
        f"postgres://parity:{SECRET} x@127.0.0.1:5432/parity",
        f"postgres://parity:{SECRET}%zz@127.0.0.1:5432/parity",
        # An unclosed IPv6 bracket: libpq echoes the whole URI.
        f"postgresql://parity:{SECRET}@[::1:5432/parity",
        # The password as a query parameter is quoted back just the same.
        f"postgres://parity@127.0.0.1:5432/parity?password={SECRET} x",
    ],
)
@pytest.mark.xfail(
    strict=True,
    reason="RT-F2: libpq parse errors echo the password (or the whole URL) "
    "and parity prints the driver message verbatim",
)
def test_postgres_parse_errors_do_not_print_the_password(conn):
    """Offline: each of these fails inside libpq before any network I/O."""
    pytest.importorskip("psycopg")
    msg = _connect_error(conn)
    assert "side A" in msg
    assert SECRET not in msg, msg


@pytest.mark.parametrize(
    "conn",
    [
        # An unencoded `/` or `#` ends the authority early, so urlparse reads
        # the start of the password as the port and quotes it in its error.
        f"mysql://parity:{SECRET}/x@127.0.0.1:3306/parity_test",
        f"mysql://parity:{SECRET}#x@127.0.0.1:3306/parity_test",
    ],
)
@pytest.mark.xfail(
    strict=True,
    reason="RT-F3: an unencoded / or # in a MySQL password leaks its first "
    "part through urlparse's port error, which also omits the side",
)
def test_mysql_unencoded_password_does_not_leak(conn):
    """Offline: urlparse raises before the driver is reached."""
    pytest.importorskip("mysql.connector")
    msg = _connect_error(conn)
    assert "side A" in msg
    assert SECRET not in msg, msg


@pytest.mark.parametrize(
    "conn",
    ["mysql://u:p@127.0.0.1:99999/db", "mysql://u:p@[::1/db"],
)
@pytest.mark.xfail(
    strict=True,
    reason="RT-F3: a MySQL URL urlparse rejects (port out of range, unclosed "
    "IPv6 bracket) errors without naming the side",
)
def test_mysql_bad_url_names_the_side(conn):
    """Offline: `Port out of range 0-65535` / `Invalid IPv6 URL`, no [side A]."""
    pytest.importorskip("mysql.connector")
    assert "side A" in _connect_error(conn)


@pytest.mark.xfail(
    strict=True,
    reason="RT-F18: the MySQL dialect drops the URL query string, so "
    "?ssl_mode=REQUIRED&ssl_ca=... is silently ignored and TLS is not enforced",
)
def test_mysql_url_options_are_honoured_or_refused(monkeypatch):
    """Offline: what reaches `mysql.connector.connect` for a URL with options.

    PostgreSQL passes the URL to libpq, which honours `sslmode=require` and
    refuses an unknown option. MySQL parses the URL itself and reads only
    host, port, user, password and database - a user who wrote
    `?ssl_mode=REQUIRED` to forbid a plaintext connection gets none of it, and
    no warning. Either the option must reach the driver or the URL be refused.
    """
    mc = pytest.importorskip("mysql.connector")
    seen: dict[str, Any] = {}

    def fake_connect(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop before the network")

    monkeypatch.setattr(mc, "connect", fake_connect)
    msg = _connect_error("mysql://u:p@127.0.0.1:3306/db?ssl_mode=REQUIRED&ssl_ca=/x.pem")
    refused = "stop before the network" not in msg
    assert refused or {"ssl_ca", "ssl_verify_cert", "ssl_disabled"} & set(seen), seen


def test_bigquery_refuses_credentials_in_the_url():
    """The README says no secret goes in a BigQuery URL; one that does is not refused.

    `parse_bigquery_url` returns `u:<secret>@proj` as the project id, which
    the client then puts into `GET .../projects/u:<secret>@proj/datasets/...`
    and the 400 error prints back.
    """
    from parity.dialects.bigquery_dialect import parse_bigquery_url

    for url in (
        f"bigquery://u:{SECRET}@proj/ds",
        f"bigquery://u:{SECRET}@proj",
        f"bigquery://u:a/{SECRET}@proj",  # a raw '/' in the password
        f"bigquery://u%40x:{SECRET}%40proj/ds",
    ):
        with pytest.raises(ValueError) as caught:
            parse_bigquery_url(url)
        assert SECRET not in str(caught.value)
    # And through the CLI: named side, no secret.
    pytest.importorskip("google.cloud.bigquery")
    msg = _connect_error(f"bigquery://u:{SECRET}@proj/ds")
    assert "side A" in msg and SECRET not in msg, msg
    # A plain project id, including a domain-scoped one, still parses.
    assert parse_bigquery_url("bigquery://example.com:proj/ds") == (
        "example.com:proj",
        "ds",
        None,
    )


@pytest.mark.parametrize(
    "conn",
    [
        f"mssql://sa:{SECRET}@127.0.0.1:notaport/db",
        f"mssql://sa:{SECRET}@127.0.0.1:1/db?bogus={SECRET}",
        f"mssql://sa:{SECRET}/x@127.0.0.1:1/db",
        f"mssql://sa:{SECRET}@/db",
        f"postgres://parity:{SECRET}@127.0.0.1:notaport/parity",
        f"postgres://parity:{SECRET}@127.0.0.1:5432/parity?bogus=1",
        f"postgres://parity:{SECRET}@127.0.0.1:5432/parity?sslmode=nope",
        f"pg://parity:{SECRET}@127.0.0.1/parity",
    ],
)
def test_these_errors_name_the_side_and_hide_the_password(conn):
    """Offline error paths that already behave."""
    if conn.startswith("mssql"):
        pytest.importorskip("mssql_python")
    if conn.startswith("postgres"):
        pytest.importorskip("psycopg")
    msg = _connect_error(conn)
    assert "side A" in msg
    assert SECRET not in msg, msg


def test_mssql_documented_login_timeout_is_accepted():
    """README lists `login_timeout` (seconds) as a supported mssql option.

    The driver rejects the connection string before it opens a socket:
    `Unknown keyword 'logintimeout' is not recognized`.
    """
    pytest.importorskip("mssql_python")
    msg = _connect_error("mssql://u:p@127.0.0.1:1/db?login_timeout=1")
    assert "not recognized" not in msg, msg


@pytest.mark.mssql
def test_mssql_wrong_password_hides_the_password(mssql_url):
    """A live authentication failure: named side, no password."""
    u = urlparse(mssql_url)
    conn = mssql_url.replace(f":{u.password}@", f":{SECRET}@", 1)
    msg = _connect_error(conn)
    assert "side A" in msg and SECRET not in msg


@pytest.mark.postgres
def test_postgres_wrong_password_hides_the_password(pg_url):
    """A live authentication failure: named side, no password."""
    u = urlparse(pg_url)
    conn = pg_url.replace(f":{u.password}@", f":{SECRET}@", 1)
    msg = _connect_error(conn)
    assert "side A" in msg and SECRET not in msg


@pytest.mark.postgres
def test_postgres_password_with_every_reserved_character(pg_url):
    """A percent-encoded password full of URL syntax is decoded, not misparsed.

    Proven with a wrong password: the error must be an authentication failure
    for the right user at the right host, which means user, host, port and
    database all survived the parse.
    """
    pw = f"{SECRET}@:/?#%+ &="
    u = urlparse(pg_url)
    conn = (
        f"postgres://{u.username}:{urlquote(pw, safe='')}@{u.hostname}:{u.port}{u.path}"
    )
    msg = _connect_error(conn)
    assert "password authentication failed" in msg
    assert SECRET not in msg


@pytest.mark.mysql
def test_mysql_password_with_every_reserved_character(mysql_url):
    """Same as above, through the MySQL dialect's own urlparse/unquote."""
    pw = f"{SECRET}@:/?#%+ &="
    u = urlparse(mysql_url)
    conn = f"mysql://{u.username}:{urlquote(pw, safe='')}@{u.hostname}:{u.port}{u.path}"
    msg = _connect_error(conn)
    assert "Access denied" in msg and "side A" in msg
    assert SECRET not in msg


def test_duckdb_path_forms(tmp_path, monkeypatch):
    """Relative, absolute (drive letter) and :memory: DuckDB URLs resolve."""
    pytest.importorskip("duckdb")
    path = build(str(tmp_path / "rt_cli_p.duckdb"), 10)
    monkeypatch.chdir(tmp_path)
    for conn in (
        "duckdb:///rt_cli_p.duckdb",
        "duckdb:///./rt_cli_p.duckdb",
        f"duckdb:///{path}",
        f"duckdb:///{path.replace(os.sep, '/')}",
    ):
        args = duck_args(path, path)
        args[2] = conn
        code, _, err = run_cli(*args)
        assert code == EXIT_IDENTICAL, (conn, err)
    msg = _connect_error("duckdb:///rt_cli_missing.duckdb")
    assert "side A" in msg and "not found" in msg


# ---------------------------------------------------------------------------
# Read-only proof: every statement that reaches each server
# ---------------------------------------------------------------------------


class _Recorder:
    """Wrap a DB-API connection or cursor and log every SQL string sent."""

    def __init__(self, target: Any, log: list[str]) -> None:
        object.__setattr__(self, "_t", target)
        object.__setattr__(self, "_log", log)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._t, name)
        if name in ("execute", "executemany"):

            def execute(sql: Any, *a: Any, **k: Any) -> Any:
                self._log.append(str(sql))
                result = value(sql, *a, **k)
                return result if result is not self._t else self

            return execute
        if name == "cursor":
            return lambda *a, **k: _Recorder(value(*a, **k), self._log)
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._t, name, value)

    def __enter__(self) -> _Recorder:
        self._t.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._t.__exit__(*exc)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._t)


def _record(monkeypatch, module: Any) -> list[str]:
    """Patch `module.connect` so every connection it returns logs its SQL."""
    log: list[str] = []
    real = module.connect
    monkeypatch.setattr(module, "connect", lambda *a, **k: _Recorder(real(*a, **k), log))
    return log


#: What a read-only diff may send: queries, and session settings.
_READ = re.compile(r"^\s*(select|with|set|show)\b", re.IGNORECASE)


def _non_reads(log: list[str]) -> list[str]:
    """Every logged statement that is not a SELECT/WITH/SET/SHOW."""
    return [s for s in log if not _READ.match(s)]


def _sets(log: list[str]) -> list[str]:
    """The distinct session statements, for the report."""
    return sorted({s.strip() for s in log if re.match(r"^\s*(set|show)\b", s, re.I)})


@pytest.fixture()
def duck_orders(tmp_path) -> str:
    """Side B for the live read-only runs: 200 rows, one changed, one missing."""
    pytest.importorskip("duckdb")
    path = str(tmp_path / "rt_cli_ro.duckdb")
    con = duckdb_write(path)
    con.execute(
        "create table rt_cli_orders (id bigint, amount decimal(12,2), note varchar)"
    )
    con.execute(
        "insert into rt_cli_orders select i, i / 4.0, 'n' || i "
        "from generate_series(1, 200) s(i)"
    )
    con.execute("update rt_cli_orders set amount = 0 where id = 50")
    con.execute("delete from rt_cli_orders where id = 150")
    con.close()
    return path


def _assert_found_the_two_planted(code: int, out: str) -> None:
    """The live read-only runs must still find both planted differences."""
    payload = json.loads(out)
    assert code == EXIT_DIFFERENCES
    assert sorted((d["key"], d["kind"]) for d in payload["differences"]) == [
        (50, "different"),
        (150, "only_in_a"),
    ]


@pytest.mark.postgres
def test_postgres_receives_only_reads(pg_url, duck_orders, monkeypatch):
    """Capture every statement psycopg is asked to run during a full diff.

    psycopg also sends its own `BEGIN ISOLATION LEVEL REPEATABLE READ, READ
    ONLY` and a `ROLLBACK` on close, which do not pass through `execute`; the
    server-side guarantee is checked separately via `transaction_read_only`.
    """
    import psycopg

    from parity.dialects.base import get_dialect

    admin = psycopg.connect(pg_url, autocommit=True)
    try:
        admin.execute("create schema if not exists parity_test")
        admin.execute("drop table if exists parity_test.rt_cli_orders")
        admin.execute(
            "create table parity_test.rt_cli_orders "
            "(id bigint, amount numeric(12,2), note text)"
        )
        admin.execute(
            "insert into parity_test.rt_cli_orders select i, i / 4.0, 'n' || i "
            "from generate_series(1, 200) s(i)"
        )
        log = _record(monkeypatch, psycopg)
        code, out, err = run_cli(
            "diff",
            "--a",
            pg_url,
            "--a-table",
            "parity_test.rt_cli_orders",
            "--b",
            f"duckdb:///{duck_orders}",
            "--b-table",
            "rt_cli_orders",
            "--key",
            "id",
            "--json",
        )
        _assert_found_the_two_planted(code, out)
        assert log, "nothing was recorded"
        print("postgres statements:", sorted({x.split()[0] for x in log}), len(log))
        assert _non_reads(log) == []
        assert _sets(log) == [
            "set extra_float_digits = 1",
            "set time zone 'UTC'",
            "show server_version_num",
        ]
        # And the server itself would refuse a write in the diff's session.
        d = get_dialect(pg_url, side="A")
        try:
            assert d.query("show transaction_read_only") == [("on",)]
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                d.query("delete from parity_test.rt_cli_orders")
        finally:
            d.close()
    finally:
        monkeypatch.undo()
        admin.execute("drop table if exists parity_test.rt_cli_orders")
        admin.close()


def _mysql_admin(url: str):
    """A plain autocommit mysql-connector session for fixtures."""
    import mysql.connector

    u = urlparse(url)
    return mysql.connector.connect(
        host=u.hostname,
        port=u.port or 3306,
        user=unquote(u.username or ""),
        password=unquote(u.password or ""),
        database=u.path.lstrip("/"),
        autocommit=True,
    )


@pytest.fixture()
def mysql_orders(mysql_url) -> Iterator[Any]:
    """`rt_cli_orders` on MySQL: the same 200 rows as `duck_orders` before planting."""
    adm = _mysql_admin(mysql_url)
    cur = adm.cursor()
    cur.execute("drop table if exists rt_cli_orders")
    cur.execute("create table rt_cli_orders (id bigint, amount decimal(12,2), note text)")
    cur.executemany(
        "insert into rt_cli_orders values (%s, %s, %s)",
        [(i, i / 4, f"n{i}") for i in range(1, 201)],
    )
    try:
        yield adm
    finally:
        cur.execute("drop table if exists rt_cli_orders")
        cur.close()
        adm.close()


@pytest.mark.mysql
def test_mysql_receives_only_reads(mysql_url, mysql_orders, duck_orders, monkeypatch):
    """Capture every statement mysql-connector is asked to run during a diff.

    MySQL's general log needs SUPER, which the test login does not hold, so
    the capture is at the driver boundary - every statement the dialect sends
    goes through `cursor.execute`.
    """
    import mysql.connector

    log = _record(monkeypatch, mysql.connector)
    code, out, _ = run_cli(
        "diff",
        "--a",
        mysql_url,
        "--a-table",
        "rt_cli_orders",
        "--b",
        f"duckdb:///{duck_orders}",
        "--b-table",
        "rt_cli_orders",
        "--key",
        "id",
        "--json",
    )
    monkeypatch.undo()
    _assert_found_the_two_planted(code, out)
    print("mysql statements:", sorted({x.split()[0] for x in log}), len(log))
    assert log and _non_reads(log) == []
    assert _sets(log) == [
        "set session time_zone = '+00:00'",
        "set session transaction isolation level repeatable read",
    ]


@pytest.mark.mysql
@pytest.mark.xfail(
    strict=True,
    reason="RT-F11: the MySQL session is not read-only - nothing but the SQL "
    "the tool happens to build stops a write (CLAUDE.md section 6)",
)
def test_mysql_session_refuses_writes(mysql_url, mysql_orders):
    """PostgreSQL, DuckDB and BigQuery make the *server* refuse a write.

    MySQL is only told `repeatable read`; `transaction_read_only` stays 0 and a
    DELETE succeeds inside the diff's own session. (It is rolled back here by
    closing without commit, so the fixture table is untouched.)
    """
    from parity.dialects.base import get_dialect

    d = get_dialect(mysql_url, side="A")
    try:
        assert d.query("select @@session.transaction_read_only") == [(1,)]
        with pytest.raises(Exception):  # noqa: B017 - any refusal will do
            d.query("delete from rt_cli_orders where id = 1")
    finally:
        d.close()


@pytest.mark.mysql
@pytest.mark.parametrize(
    "suffix, needle",
    [("a\\b", "table not found"), ("z\\", "syntax")],
)
@pytest.mark.xfail(
    strict=True,
    reason="RT-F17: sql_literal doubles quotes but not backslashes, which "
    "MySQL treats as escapes - a real table named with a backslash is 'not "
    "found', and a trailing one escapes the literal (raw 1064, no side named)",
)
def test_mysql_table_name_with_a_backslash(mysql_url, suffix, needle):
    """A legal backtick-quoted MySQL name, diffed against itself, must be identical.

    `rt_cli_a\\b` is looked up as `'rt_cli_a\\b'`, which MySQL reads as
    `rt_cli_a<backspace>`; `rt_cli_z\\` leaves the string literal open. The
    information_schema lookup is a SELECT, so this is a wrong answer and not
    a write - but it is the shape of a literal injection.
    """
    table = f"rt_cli_{suffix}"
    adm = _mysql_admin(mysql_url)
    cur = adm.cursor()
    try:
        cur.execute(f"drop table if exists `{table}`")
        cur.execute(f"create table `{table}` (id bigint, v int)")
        cur.execute(f"insert into `{table}` values (1, 1), (2, 2)")
        code, out, err = run_cli(
            "diff", "--a", mysql_url, "--a-table", table,
            "--b", mysql_url, "--b-table", table, "--key", "id",
        )  # fmt: skip
        assert needle not in err
        assert code == EXIT_IDENTICAL, err
    finally:
        cur.execute(f"drop table if exists `{table}`")
        cur.close()
        adm.close()


# ---------------------------------------------------------------------------
# Read-only proof at the wire: what actually reaches the server
# ---------------------------------------------------------------------------
#
# Statement logging on the server (PostgreSQL `log_statement` per role, the
# MySQL general log) needs superuser rights the test logins do not hold. The
# next best witness is the TCP stream itself: a loopback relay between the
# driver and the server decodes every message the client sends. Unlike the
# driver-level `_Recorder` above it also sees what the driver adds on its own -
# psycopg's BEGIN, mysql-connector's SET NAMES / autocommit - and anything a
# C extension sends without passing through Python.


class _WireTap:
    """A loopback TCP relay that decodes what the client sends to the server.

    `parser(buf, log, state)` consumes complete messages from the front of
    `buf` and appends `(kind, text)` entries to `log`. `rewrite` may change the
    first server packet (MySQL's greeting) before it is relayed.
    """

    def __init__(self, upstream: tuple[str, int], parser: Any, rewrite: Any = None):
        import threading

        self._threading = threading
        self.upstream, self.parser, self.rewrite = upstream, parser, rewrite
        self.log: list[tuple[str, str]] = []
        self.connections = 0
        self._lock = threading.Lock()
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._srv.close()

    def _accept(self) -> None:
        while True:
            try:
                cli, _ = self._srv.accept()
            except OSError:
                return
            up = socket.create_connection(self.upstream)
            self.connections += 1
            state: dict[str, Any] = {}
            t = self._threading.Thread
            t(target=self._c2s, args=(cli, up, state), daemon=True).start()
            t(target=self._s2c, args=(up, cli), daemon=True).start()

    @staticmethod
    def _shut(*socks: socket.socket) -> None:
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _c2s(self, cli: socket.socket, up: socket.socket, state: dict) -> None:
        buf = bytearray()
        try:
            while data := cli.recv(65536):
                buf += data
                with self._lock:
                    self.parser(buf, self.log, state)
                up.sendall(data)
        except OSError:
            pass
        finally:
            self._shut(cli, up)

    def _s2c(self, up: socket.socket, cli: socket.socket) -> None:
        first, buf = self.rewrite is not None, bytearray()
        try:
            while data := up.recv(65536):
                if first:
                    buf += data
                    n = int.from_bytes(buf[:3], "little") + 4 if len(buf) >= 4 else 0
                    if not n or len(buf) < n:
                        continue
                    data, first = self.rewrite(bytes(buf[:n])) + bytes(buf[n:]), False
                cli.sendall(data)
        except OSError:
            pass
        finally:
            self._shut(cli, up)


#: SSLRequest, GSSENCRequest and CancelRequest: 8-byte startup-phase packets.
_PG_SPECIAL = {80877103, 80877104, 80877102}


def _pg_parser(buf: bytearray, log: list, state: dict) -> None:
    """Decode PostgreSQL frontend messages: simple Query and extended Parse."""
    import struct

    while True:
        if not state.get("started"):
            if len(buf) < 8:
                return
            n, code = struct.unpack("!ii", buf[:8])
            if len(buf) < n:
                return
            del buf[:n]
            if code == 80877102:
                log.append(("cancel", ""))
            elif code not in _PG_SPECIAL:
                state["started"] = True
            continue
        if len(buf) < 5:
            return
        kind, n = chr(buf[0]), struct.unpack("!i", buf[1:5])[0]
        if len(buf) < n + 1:
            return
        body = bytes(buf[5 : n + 1])
        del buf[: n + 1]
        if kind == "Q":
            log.append(("query", body.rstrip(b"\x00").decode()))
        elif kind == "P":
            log.append(("parse", body.split(b"\x00")[1].decode()))
        elif kind not in "BDESHCXp":  # bind/describe/execute/sync/...: no SQL
            log.append(("other", kind))


#: MySQL capability bits the tap clears in the greeting: TLS and compression
#: would make the stream unreadable. (Query attributes are left on - clearing
#: them makes mysql-connector send a malformed packet - and decoded instead.)
_MY_SSL, _MY_COMPRESS = 0x0800, 0x0020


def _mysql_greeting(packet: bytes) -> bytes:
    """Clear SSL and compression in the server greeting (HandshakeV10)."""
    p = bytearray(packet)
    i = 5 + p[5:].index(0) + 1  # header(4), protocol(1), version NUL-terminated
    i += 4 + 8 + 1  # thread id, auth data part 1, filler
    hi_at = i + 2 + 1 + 2  # lower capability flags, charset, status flags
    caps = int.from_bytes(p[i : i + 2], "little")
    caps |= int.from_bytes(p[hi_at : hi_at + 2], "little") << 16
    caps &= ~(_MY_SSL | _MY_COMPRESS)
    p[i : i + 2] = (caps & 0xFFFF).to_bytes(2, "little")
    p[hi_at : hi_at + 2] = (caps >> 16).to_bytes(2, "little")
    return bytes(p)


_MY_COMMANDS = {0x01: "quit", 0x0E: "ping", 0x1F: "reset_connection"}


def _mysql_parser(buf: bytearray, log: list, state: dict) -> None:
    """Decode MySQL client packets: COM_QUERY / COM_STMT_PREPARE text, commands."""
    while len(buf) >= 4:
        n = int.from_bytes(buf[:3], "little")
        if len(buf) < n + 4:
            return
        seq, body = buf[3], bytes(buf[4 : n + 4])
        del buf[: n + 4]
        if seq != 0 or not body:  # handshake response / auth continuation
            continue
        if body[0] in (0x03, 0x16):
            text = body[1:]
            if body[0] == 0x03 and text[:2] == b"\x00\x01":
                text = text[2:]  # query attributes: 0 parameters, 1 set
            log.append(("query", text.decode()))
        else:
            log.append(("command", _MY_COMMANDS.get(body[0], hex(body[0]))))


#: What a read-only diff may put on the wire, beyond SELECT/WITH/SET/SHOW.
_TXN = re.compile(r"^\s*(begin|rollback|commit)\b", re.IGNORECASE)


@pytest.mark.postgres
def test_postgres_wire_carries_only_reads_in_a_read_only_transaction(pg_url, duck_orders):
    """Every frontend message psycopg sends during a full diff, decoded off the wire.

    Proves three things the driver-level capture could not: no statement of
    any kind outside SELECT/WITH/SET/SHOW and transaction control; the BEGIN
    that psycopg itself issues asks the *server* for READ ONLY; and nothing
    is ever committed.
    """
    import psycopg

    u = urlparse(pg_url)
    admin = psycopg.connect(pg_url, autocommit=True)
    tap = _WireTap((u.hostname or "127.0.0.1", u.port or 5432), _pg_parser)
    try:
        admin.execute("create schema if not exists parity_test")
        admin.execute("drop table if exists parity_test.rt_cli_orders")
        admin.execute(
            "create table parity_test.rt_cli_orders "
            "(id bigint, amount numeric(12,2), note text)"
        )
        admin.execute(
            "insert into parity_test.rt_cli_orders select i, i / 4.0, 'n' || i "
            "from generate_series(1, 200) s(i)"
        )
        sep = "&" if u.query else "?"
        tapped = (
            f"{u.scheme}://{u.netloc.rsplit('@', 1)[0]}@127.0.0.1:{tap.port}{u.path}"
            f"{'?' + u.query if u.query else ''}{sep}sslmode=disable&gssencmode=disable"
        )
        code, out, err = run_cli(
            "diff", "--a", tapped, "--a-table", "parity_test.rt_cli_orders",
            "--b", f"duckdb:///{duck_orders}", "--b-table", "rt_cli_orders",
            "--key", "id", "--json",
        )  # fmt: skip
        _assert_found_the_two_planted(code, out)
        texts = [t for kind, t in tap.log if kind in ("query", "parse")]
        print("postgres wire:", tap.connections, "connection(s),", len(texts), "stmts")
        print(
            sorted({t.strip()[:60] for t in texts if not t.lower().startswith("select")})
        )
        assert tap.connections == 1
        assert [k for k, _ in tap.log if k not in ("query", "parse")] == []
        assert [t for t in texts if not (_READ.match(t) or _TXN.match(t))] == []
        begins = [t for t in texts if t.strip().lower().startswith("begin")]
        assert begins and all("read only" in t.lower() for t in begins), begins
        # Never a COMMIT. psycopg's close() sends Terminate rather than an
        # explicit ROLLBACK; the server aborts the open transaction itself.
        assert not any(t.strip().lower().startswith("commit") for t in texts)
    finally:
        tap.close()
        admin.execute("drop table if exists parity_test.rt_cli_orders")
        admin.close()


@pytest.mark.mysql
def test_mysql_wire_carries_only_reads(mysql_url, mysql_orders, duck_orders):
    """Every packet mysql-connector sends during a full diff, decoded off the wire.

    No statement outside SELECT/SET/SHOW and no command beyond ping/quit. Note
    what is *absent*: no `START TRANSACTION READ ONLY` and no
    `transaction_read_only` - the wire confirms RT-F11 from the server's side.
    """
    u = urlparse(mysql_url)
    tap = _WireTap(
        (u.hostname or "127.0.0.1", u.port or 3306), _mysql_parser, _mysql_greeting
    )
    try:
        tapped = f"{u.scheme}://{u.netloc.rsplit('@', 1)[0]}@127.0.0.1:{tap.port}{u.path}"
        code, out, err = run_cli(
            "diff", "--a", tapped, "--a-table", "rt_cli_orders",
            "--b", f"duckdb:///{duck_orders}", "--b-table", "rt_cli_orders",
            "--key", "id", "--json",
        )  # fmt: skip
        _assert_found_the_two_planted(code, out)
        texts = [t for kind, t in tap.log if kind == "query"]
        print(
            "mysql wire:",
            len(texts),
            "stmts",
            sorted({t[:60] for t in texts if not t.lower().startswith("select")}),
        )
        assert tap.connections == 1
        assert {t for k, t in tap.log if k == "command"} <= {"ping", "quit"}
        assert [t for t in texts if not _READ.match(t)] == []
        assert not any("read only" in t.lower() for t in texts)  # RT-F11, seen live
    finally:
        tap.close()


@pytest.mark.mssql
def test_mssql_receives_only_selects_as_a_datareader(mssql_url, duck_orders):
    """Server-side proof: an Extended Events session logs every batch and RPC
    a fresh `db_datareader`-only login sends during a full diff.

    The login can read and nothing else, so the diff completing and finding
    both planted differences shows it needed no write permission; the XE log
    shows what actually reached the server.
    """
    import mssql_python

    from parity.dialects.mssql_dialect import parse_mssql_url

    u = urlparse(mssql_url)
    q = f"?{u.query}" if u.query else ""
    db, login = "rt_cli_db", "rt_cli_ro"
    pw = "Rt!" + secrets.token_urlsafe(18)

    def admin(database: str):
        url = f"{u.scheme}://{u.netloc}/{database}{q}"
        return mssql_python.connect(parse_mssql_url(url)[0], autocommit=True)

    def run(conn, sql: str):
        cur = conn.cursor()
        try:
            cur.execute(sql)
            return [tuple(r) for r in cur.fetchall()] if cur.description else []
        finally:
            cur.close()

    master = admin("master")
    xe = "rt_cli_xe"
    try:
        run(master, f"if db_id('{db}') is null create database [{db}]")
        run(master, f"alter database [{db}] set allow_snapshot_isolation on")
        if not run(master, f"select 1 from sys.server_principals where name='{login}'"):
            run(
                master,
                f"create login [{login}] with password = '{pw}', check_policy = off",
            )
        else:
            run(master, f"alter login [{login}] with password = '{pw}'")
        dbc = admin(db)
        try:
            if not run(
                dbc, f"select 1 from sys.database_principals where name='{login}'"
            ):
                run(dbc, f"create user [{login}] for login [{login}]")
            run(dbc, f"alter role db_datareader add member [{login}]")
            run(dbc, "drop table if exists dbo.rt_cli_orders")
            run(
                dbc,
                "create table dbo.rt_cli_orders "
                "(id bigint primary key, amount decimal(12,2), note varchar(20))",
            )
            run(
                dbc,
                "insert into dbo.rt_cli_orders select value, value / 4.0, "
                "concat('n', value) from generate_series(1, 200)",
            )
        finally:
            dbc.close()
        filt = f"where sqlserver.server_principal_name = N'{login}'"
        acts = "action(sqlserver.server_principal_name)"
        run(
            master,
            f"if exists (select 1 from sys.server_event_sessions where name='{xe}') "
            f"drop event session [{xe}] on server",
        )
        run(
            master,
            f"create event session [{xe}] on server "
            f"add event sqlserver.sql_batch_starting({acts} {filt}), "
            f"add event sqlserver.rpc_starting({acts} {filt}) "
            "add target package0.ring_buffer(set max_memory = 4096) "
            "with (max_dispatch_latency = 1 seconds)",
        )
        run(master, f"alter event session [{xe}] on server state = start")

        ro_url = f"mssql://{login}:{urlquote(pw, safe='')}@{u.hostname}:{u.port}/{db}{q}"
        code, out, err = run_cli(
            "diff",
            "--a",
            ro_url,
            "--a-table",
            "rt_cli_orders",
            "--b",
            f"duckdb:///{duck_orders}",
            "--b-table",
            "rt_cli_orders",
            "--key",
            "id",
            "--json",
        )
        assert pw not in out + err
        _assert_found_the_two_planted(code, out)

        sent: list[str] = []
        for _ in range(20):  # the ring buffer flushes within max_dispatch_latency
            time.sleep(0.5)
            xml = run(
                master,
                "select cast(t.target_data as nvarchar(max)) from sys.dm_xe_sessions s "
                "join sys.dm_xe_session_targets t on s.address = t.event_session_address "
                f"where s.name = '{xe}'",
            )[0][0]
            root = ET.fromstring(xml)  # noqa: S314 - our own XE session output
            sent = [
                (d.findtext("value") or "")
                for ev in root.iter("event")
                for d in ev.iter("data")
                if d.get("name") in ("batch_text", "statement")
            ]
            if any("rt_cli_orders" in s and "count" in s.lower() for s in sent):
                break
        assert sent, "the XE session captured nothing"
        print("mssql XE:", sorted({x.split()[0] for x in sent if x}), len(sent))
        print(
            "mssql XE sets:",
            sorted({x.strip()[:70] for x in sent if not x.lower().startswith("select")}),
        )
        others = [s for s in sent if not _READ.match(s)]
        assert others == [], others
        assert sorted({s.strip() for s in sent if s.lower().startswith("set")}) == [
            "set transaction isolation level snapshot"
        ]
    finally:
        run(
            master,
            f"if exists (select 1 from sys.server_event_sessions where name='{xe}') "
            f"drop event session [{xe}] on server",
        )
        run(
            master,
            f"if db_id('{db}') is not null begin "
            f"alter database [{db}] set single_user with rollback immediate; "
            f"drop database [{db}]; end",
        )
        run(
            master,
            f"if exists (select 1 from sys.server_principals where name='{login}') "
            f"drop login [{login}]",
        )
        master.close()


@pytest.mark.parametrize(
    "sql",
    [
        "select * into rt_cli_copy from rt_cli_orders",
        "select 1; drop table rt_cli_orders",
    ],
)
def test_mssql_select_guard_refuses_writes_spelled_as_select(sql):
    """Offline: does a write that *starts* with `select` reach the driver?"""
    from parity.dialects.mssql_dialect import SQLServerDialect

    sent: list[str] = []

    class FakeCursor:
        description = None

        def execute(self, s):
            sent.append(s)

        def fetchall(self):
            return []

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    d = SQLServerDialect(side="A")
    d._conn = FakeConn()
    with pytest.raises(ValueError):
        d.query(sql)
    assert sent == []


# ---------------------------------------------------------------------------
# No network calls beyond the two configured databases
# ---------------------------------------------------------------------------


class _NetGuard:
    """Record every Python-level DNS lookup and socket connect.

    Native drivers (libpq, ODBC, libmysqlclient) open their own sockets below
    Python and are invisible here; this guard covers parity's own code and
    every pure-Python dependency, which is where telemetry would live.
    """

    def __init__(self, monkeypatch, allowed: set[str]) -> None:
        self.allowed = allowed
        self.seen: list[tuple[str, Any]] = []
        self.resolved: dict[str, str] = {}
        real_gai = socket.getaddrinfo
        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex
        guard = self

        def getaddrinfo(host, port, *a, **k):
            guard.seen.append(("dns", host))
            result = real_gai(host, port, *a, **k)
            # An address is only as trustworthy as the name it was resolved
            # from: remember what each looked-up name resolved to.
            for *_, sockaddr in result:
                guard.resolved[str(sockaddr[0])] = str(host)
            return result

        def connect(sock, address):
            guard.seen.append(("connect", address))
            return real_connect(sock, address)

        def connect_ex(sock, address):
            guard.seen.append(("connect", address))
            return real_connect_ex(sock, address)

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        monkeypatch.setattr(socket.socket, "connect", connect)
        monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)

    def hosts(self) -> set[str]:
        """Every host name or address touched."""
        out = set()
        for kind, what in self.seen:
            host = (
                what if kind == "dns" else (what[0] if isinstance(what, tuple) else what)
            )
            if host is not None:
                out.add(host.decode() if isinstance(host, bytes) else str(host))
        return out

    def outsiders(self) -> set[str]:
        """Hosts touched that are not allowed, judging an address by its name."""

        def ok(h: str) -> bool:
            name = self.resolved.get(h, h)
            return any(name == a or name.endswith("." + a) for a in self.allowed)

        return {h for h in self.hosts() if not ok(h)}


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def test_duckdb_diff_opens_no_socket_at_all(base, mixed, monkeypatch):
    """Two local files: not one DNS lookup or connect, verdict unchanged."""
    guard = _NetGuard(monkeypatch, set())
    code, _, _ = run_cli(*duck_args(base, mixed, "--json"))
    assert code == EXIT_DIFFERENCES
    assert guard.seen == []


def test_duckdb_session_cannot_download_extensions(base):
    """CLAUDE.md section 6: no network calls, ever.

    DuckDB's autoinstall runs in C++, below any Python socket guard, the moment
    a query touches a feature from an uninstalled extension - a view over an
    `https://` or `s3://` file in the user's database is enough. The dialect
    should switch autoinstall off on its connection.
    """
    from parity.dialects.base import get_dialect

    d = get_dialect(f"duckdb:///{base}", side="A")
    try:
        (row,) = d.query(
            "select current_setting('autoinstall_known_extensions'), "
            "current_setting('autoload_known_extensions')"
        )
        assert row == (False, False)
        # And a query that needs httpfs is refused up front instead of fetching
        # the extension (loopback URL: nothing external even if it were not).
        with pytest.raises(Exception, match=r"(?i)extension"):
            d.query("select * from read_csv('https://127.0.0.1:1/x.csv')")
    finally:
        d.close()


@pytest.mark.postgres
@pytest.mark.mysql
def test_postgres_to_mysql_touches_only_loopback(
    pg_url, mysql_url, mysql_orders, monkeypatch
):
    """A real PG-to-MySQL diff, guarded: only the two configured hosts."""
    import psycopg

    admin = psycopg.connect(pg_url, autocommit=True)
    try:
        admin.execute("create schema if not exists parity_test")
        admin.execute("drop table if exists parity_test.rt_cli_orders")
        admin.execute(
            "create table parity_test.rt_cli_orders "
            "(id bigint, amount numeric(12,2), note text)"
        )
        admin.execute(
            "insert into parity_test.rt_cli_orders select i, i / 4.0, 'n' || i "
            "from generate_series(1, 199) s(i)"
        )
        guard = _NetGuard(monkeypatch, _LOOPBACK)
        code, out, _ = run_cli(
            "diff",
            "--a",
            pg_url,
            "--a-table",
            "parity_test.rt_cli_orders",
            "--b",
            mysql_url,
            "--b-table",
            "rt_cli_orders",
            "--key",
            "id",
            "--json",
        )
        monkeypatch.undo()
        assert code == EXIT_DIFFERENCES
        assert [d["key"] for d in json.loads(out)["differences"]] == [200]
        assert guard.outsiders() == set()
    finally:
        admin.execute("drop table if exists parity_test.rt_cli_orders")
        admin.close()


@pytest.mark.bigquery
def test_bigquery_touches_only_googleapis(bigquery_url, monkeypatch):
    """Connect and introspect a missing table: only Google API hosts are touched."""
    guard = _NetGuard(monkeypatch, {"googleapis.com"} | _LOOPBACK)
    msg = _connect_error_table(bigquery_url, "rt_cli_missing")
    monkeypatch.undo()
    assert "side A" in msg and "rt_cli_missing" in msg
    assert guard.hosts(), "the guard saw nothing - BigQuery is pure Python"
    assert guard.outsiders() == set(), guard.hosts()


def _connect_error_table(conn: str, table: str) -> str:
    """Run a diff of a missing table on side A; return what was printed."""
    code, out, err = run_cli(
        "diff",
        "--a",
        conn,
        "--a-table",
        table,
        "--b",
        "duckdb:///:memory:",
        "--b-table",
        "t",
        "--key",
        "id",
    )
    assert code == EXIT_ERROR
    return out + err


def test_snowflake_connection_disables_client_telemetry(monkeypatch):
    """Offline: capture what the dialect passes to `snowflake.connector.connect`."""
    sf = pytest.importorskip("snowflake.connector")
    seen: dict[str, Any] = {}

    def fake_connect(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop before the network")

    monkeypatch.setattr(sf, "connect", fake_connect)
    _connect_error("snowflake://u:p@acct/db/sch?warehouse=w")
    flat = json.dumps(seen, default=str).lower()
    assert "telemetry" in flat
    # RT-F14, fixed (offline only - no Snowflake account): switched off in the
    # login request itself, since the connector logs during connect.
    assert seen["session_parameters"]["CLIENT_TELEMETRY_ENABLED"] is False
    assert seen["log_imported_packages_in_telemetry"] is False
