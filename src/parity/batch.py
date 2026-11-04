"""`parity run CONFIG`: check every table pair of a migration in one command.

A config file lists named connections and the table pairs to compare. Each
pair is one ordinary `diff` - the same engine, the same verdict, the same
JSON - run one after another with fresh connections, so a pair cannot see
another pair's snapshot or session. The summary adds nothing to a pair's
verdict and takes nothing away: a multi-pair verdict is each pair's verdict,
and the run is identical only when every pair is.

The rules this module exists to keep:

- **Validate everything before connecting anywhere.** Unknown keys (with a
  near-miss suggestion), duplicate names, missing fields, bad types, unsafe
  row filters and unset environment variables are all reported at once, each
  naming the file, the pair and the field.
- **Passwords stay out of the file and out of the output.** A connection may
  read `${NAME}` from the environment. The expanded value is never printed:
  every error message and warning is redacted before it is shown or stored.
- **A summary is never stronger than its weakest pair.** Every qualifier the
  single-pair report would print (filtered, truncated, unexplained, schema
  differences, a sample-check failure or error, uncovered columns, caveats)
  appears in every output format, for every pair it applies to.
- **One pair's failure is that pair's.** An error is recorded and the run
  goes on; `--fail-fast` stops instead. Ctrl-C stops cleanly and the summary
  of what finished is still written, marked incomplete.

The outputs render from each pair's `to_dict` JSON rather than from the
`DiffResult`, so a pair carried over by `--resume` from an earlier run prints
exactly like one that just ran - with its timestamp, and the fact that it is
from an earlier run.
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import importlib
import json
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from parity import __version__
from parity.cli import (
    EXIT_DIFFERENCES,
    EXIT_ERROR,
    EXIT_IDENTICAL,
    KIND_LABELS,
    _escape,
    _symbols,
    _Writer,
    to_dict,
)
from parity.dialects.base import decode_canonical_field, row_filter_problem
from parity.engine import DEFAULT_MAX_DIFFS, DEFAULT_VERIFY_SAMPLE

#: Opens one side: `(connection_string, side, float_scale) -> Dialect`. The
#: CLI passes `get_dialect`; tests pass fakes.
Opener = Callable[..., Any]

#: The settings `[defaults]` may set and every pair may override, with their
#: defaults - the same as `parity diff`'s.
TUNABLES: dict[str, int] = {
    "float_scale": 6,
    "verify_sample": DEFAULT_VERIFY_SAMPLE,
    "max_diffs": DEFAULT_MAX_DIFFS,
    "bisection_factor": 32,
    "threshold": 10_000,
}

#: The smallest value each tunable accepts (`max_diffs = 0` is no limit).
_MINIMUM = {
    "float_scale": 0,
    "verify_sample": 0,
    "max_diffs": 0,
    "bisection_factor": 2,
    "threshold": 1,
}

TOP_KEYS = ("connections", "defaults", "pair")
PAIR_KEYS = (
    "name",
    "a",
    "b",
    "table",
    "a_table",
    "b_table",
    "key",
    "columns",
    "exclude",
    "map",
    "where",
    "a_where",
    "b_where",
    *TUNABLES,
)

#: Differences listed per pair in the Markdown report; the rest are counted.
MARKDOWN_DIFF_LIMIT = 20

#: Version of the `--resume` state file layout.
STATE_FORMAT = 1

#: PBKDF2 rounds for a connection's fingerprint in the state file (see
#: `_fingerprint`): slow enough that the file is no shortcut to a password.
_FINGERPRINT_ROUNDS = 200_000

_VAR = re.compile(r"\$\{([^}]*)\}")
_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ConfigError(ValueError):
    """The config (or the state file) is unusable. Raised before any
    connection is opened; the CLI turns it into exit 2."""


# ---------------------------------------------------------------------------
# The config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairSpec:
    """One table pair, fully resolved: defaults applied, names split."""

    name: str
    a: str
    b: str
    a_table: str
    b_table: str
    key: tuple[str, ...]
    columns: tuple[str, ...] | None
    exclude: tuple[str, ...]
    column_map: tuple[tuple[str, str], ...]
    where_a: str | None
    where_b: str | None
    float_scale: int
    verify_sample: int
    max_diffs: int
    bisection_factor: int
    threshold: int

    def settings(self) -> dict[str, Any]:
        """Everything that decides this pair's verdict, as plain JSON.

        Hashed for `--resume` (with the connections' fingerprints), and shown
        in the JSON and Markdown reports.
        """
        return {
            "a": self.a,
            "b": self.b,
            "a_table": self.a_table,
            "b_table": self.b_table,
            "key": list(self.key),
            "columns": None if self.columns is None else list(self.columns),
            "exclude": list(self.exclude),
            "map": dict(self.column_map),
            "where_a": self.where_a,
            "where_b": self.where_b,
            "float_scale": self.float_scale,
            "verify_sample": self.verify_sample,
            "max_diffs": self.max_diffs,
            "bisection_factor": self.bisection_factor,
            "threshold": self.threshold,
        }


@dataclass
class RunConfig:
    """A validated config file. `connections` holds the values *as written*,
    `${NAME}` references unexpanded."""

    path: str
    sha256: str
    connections: dict[str, str]
    pairs: list[PairSpec]


def read_config(path: str | os.PathLike[str]) -> RunConfig:
    """Read and validate a config file: TOML (`.toml`) or JSON (`.json`).

    TOML needs the standard library's `tomllib` (Python 3.11+) or the `tomli`
    package on 3.10 (`pip install parity-diff[toml]`); JSON always works. No
    dependency is added to the core for this.
    """
    source = str(path)
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ConfigError(f"{source}: cannot read the config file: {exc}") from exc
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"{source}: not valid JSON: {exc}") from exc
    elif suffix == ".toml":
        toml = _toml_module(source)
        try:
            data = toml.loads(raw.decode("utf-8-sig"))
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{source}: not UTF-8: {exc}") from exc
        except Exception as exc:  # TOMLDecodeError, of whichever module loaded
            raise ConfigError(f"{source}: not valid TOML: {exc}") from exc
    else:
        raise ConfigError(
            f"{source}: the config file must end in .toml or .json, got "
            f"{suffix or 'no extension'!r}"
        )
    return parse_config(data, source, hashlib.sha256(raw).hexdigest())


def _toml_module(source: str) -> Any:
    """`tomllib` on 3.11+, `tomli` on 3.10 if installed, else a clear error."""
    for name in ("tomllib", "tomli"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise ConfigError(
        f"{source}: reading TOML needs Python 3.11 or newer, or the 'tomli' "
        f"package on Python 3.10 (pip install 'parity-diff[toml]'); or write "
        f"the same config as JSON (config.json)"
    )


def _suggest(name: str, known: Sequence[str]) -> str:
    """` - did you mean 'x'?` for a near miss, else nothing."""
    close = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
    return f" - did you mean {close[0]!r}?" if close else ""


class _Problems:
    """Collects every problem in a file, so one run reports all of them."""

    def __init__(self, source: str) -> None:
        """Start an empty list for `source` (the file name)."""
        self.source = source
        self.items: list[str] = []

    def add(self, where: str, message: str) -> None:
        """Record a problem at `where` (a pair and field, or a section)."""
        self.items.append(f"{where}: {message}")

    def raise_if_any(self) -> None:
        """Raise one ConfigError listing every problem, if there were any."""
        if len(self.items) == 1:
            raise ConfigError(f"{self.source}: {self.items[0]}")
        if self.items:
            listed = "\n".join(f"  - {item}" for item in self.items)
            raise ConfigError(
                f"{self.source}: {len(self.items)} problems in the config:\n{listed}"
            )


def _int_setting(value: Any, name: str, where: str, problems: _Problems) -> int | None:
    """An integer tunable at least its minimum, or None after recording why."""
    # bool is an int in Python; `float_scale = true` is a mistake, not 1.
    if isinstance(value, bool) or not isinstance(value, int):
        problems.add(where, f"field {name!r} must be a whole number, got {value!r}")
        return None
    if value < _MINIMUM[name]:
        extra = " (0 means no limit)" if name == "max_diffs" else ""
        problems.add(
            where,
            f"field {name!r} must be at least {_MINIMUM[name]}{extra}, got {value}",
        )
        return None
    return value


def _text(value: Any, name: str, where: str, problems: _Problems) -> str | None:
    """A non-empty string, or None after recording why."""
    if not isinstance(value, str) or not value.strip():
        problems.add(where, f"field {name!r} must be a non-empty string, got {value!r}")
        return None
    return value


def _names(value: Any, name: str, where: str, problems: _Problems) -> list[str] | None:
    """Column names: a list of strings or one comma-separated string."""
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
    elif isinstance(value, list) and all(isinstance(p, str) for p in value):
        parts = [p.strip() for p in value if p.strip()]
    else:
        problems.add(
            where,
            f"field {name!r} must be a list of column names or a comma-separated "
            f"string, got {value!r}",
        )
        return None
    if not parts:
        problems.add(where, f"field {name!r} names no column")
        return None
    return parts


def _column_map(value: Any, where: str, problems: _Problems) -> dict[str, str] | None:
    """`map`: a table of `a_col = "b_col"`, or a list of `"a_col=b_col"`."""
    items: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for a_name, b_name in value.items():
            if not isinstance(b_name, str) or not b_name.strip():
                problems.add(
                    where,
                    f"field 'map': side B's name for {a_name!r} must be a "
                    f"non-empty string, got {b_name!r}",
                )
                return None
            items.append((str(a_name).strip(), b_name.strip()))
    elif isinstance(value, list) and all(isinstance(p, str) for p in value):
        for pair in value:
            a_name, sep, b_name = pair.partition("=")
            if not sep or not a_name.strip() or not b_name.strip() or "=" in b_name:
                problems.add(where, f"field 'map' takes A_COL=B_COL pairs, got {pair!r}")
                return None
            items.append((a_name.strip(), b_name.strip()))
    else:
        problems.add(
            where,
            f"field 'map' must be a table of a_column = \"b_column\" (or a list "
            f'of "a=b" strings), got {value!r}',
        )
        return None
    out: dict[str, str] = {}
    for a_name, b_name in items:
        if not a_name:
            problems.add(where, "field 'map' has an empty side A column name")
            return None
        if out.get(a_name, b_name) != b_name:
            problems.add(
                where,
                f"field 'map' pairs side A's {a_name!r} with two side B columns: "
                f"{out[a_name]!r} and {b_name!r}",
            )
            return None
        out[a_name] = b_name
    return out


def _unknown_keys(
    table: Mapping[str, Any], known: Sequence[str], where: str, problems: _Problems
) -> None:
    """Refuse every key the format does not define, suggesting a near miss.

    A misspelt `exlude` silently ignored would compare a column the user
    meant to set aside - or, worse, a misspelt `where` would compare the whole
    table and call it the slice. Refused, never ignored.
    """
    for key in table:
        if key not in known:
            problems.add(where, f"unknown field {key!r}{_suggest(str(key), list(known))}")


def parse_config(data: Any, source: str = "<config>", sha256: str = "") -> RunConfig:
    """Validate a parsed config (a dict from TOML or JSON) into a RunConfig.

    Every problem in the file is collected and raised together as one
    ConfigError. Nothing here opens a connection or reads the environment.
    """
    problems = _Problems(source)
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: the config must be a table/object at the top")
    _unknown_keys(data, TOP_KEYS, "top level", problems)

    # -- connections
    connections: dict[str, str] = {}
    raw_conns = data.get("connections")
    if raw_conns is None:
        problems.add("[connections]", "missing: name each database once here")
    elif not isinstance(raw_conns, dict) or not raw_conns:
        problems.add("[connections]", 'must be a table of name = "connection string"')
    else:
        for name, value in raw_conns.items():
            where = f"connection {name!r}"
            if not isinstance(value, str) or not value.strip():
                problems.add(where, "must be a non-empty connection string")
                continue
            for match in _VAR.finditer(value):
                if not _VAR_NAME.match(match.group(1)):
                    problems.add(
                        where,
                        f"'${{{match.group(1)}}}' is not an environment variable "
                        f"name (letters, digits and _, not starting with a digit)",
                    )
            connections[str(name)] = value

    # -- defaults
    defaults = dict(TUNABLES)
    raw_defaults = data.get("defaults", {})
    if not isinstance(raw_defaults, dict):
        problems.add("[defaults]", "must be a table")
    else:
        _unknown_keys(raw_defaults, list(TUNABLES), "[defaults]", problems)
        for name in TUNABLES:
            if name in raw_defaults:
                got = _int_setting(raw_defaults[name], name, "[defaults]", problems)
                if got is not None:
                    defaults[name] = got

    # -- pairs
    pairs: list[PairSpec] = []
    raw_pairs = data.get("pair")
    if raw_pairs is None:
        problems.add("[[pair]]", "missing: the config lists no table pair")
        raw_pairs = []
    elif not isinstance(raw_pairs, list) or not raw_pairs:
        problems.add("[[pair]]", "must be a non-empty list of tables ([[pair]] entries)")
        raw_pairs = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(raw_pairs, start=1):
        spec = _parse_pair(raw, index, defaults, connections, raw_conns, problems)
        if spec is None:
            continue
        if spec.name in seen:
            problems.add(
                f"pair {spec.name!r} (#{index})",
                f"duplicate name: pair #{seen[spec.name]} is also called "
                f"{spec.name!r}; give each pair its own name",
            )
            continue
        seen[spec.name] = index
        pairs.append(spec)
    problems.raise_if_any()
    return RunConfig(path=source, sha256=sha256, connections=connections, pairs=pairs)


def _parse_pair(
    raw: Any,
    index: int,
    defaults: Mapping[str, int],
    connections: Mapping[str, str],
    raw_conns: Any,
    problems: _Problems,
) -> PairSpec | None:
    """Validate one `[[pair]]`; record problems and return None if unusable."""
    if not isinstance(raw, dict):
        problems.add(f"pair #{index}", "must be a table of fields")
        return None
    before = len(problems.items)
    # The name is resolved first so every later problem can name the pair.
    # Without a name, a pair is called by its side A table.
    name = raw.get("name", raw.get("a_table", raw.get("table")))
    if name is None:
        problems.add(
            f"pair #{index}", "missing field 'name' (or 'a_table' to name it by)"
        )
    elif not isinstance(name, str) or not name.strip():
        problems.add(
            f"pair #{index}", f"field 'name' must be a non-empty string, got {name!r}"
        )
        name = None
    elif "," in name:
        problems.add(f"pair {name!r} (#{index})", "field 'name' cannot contain ','")
    where = f"pair #{index}" if name is None else f"pair {name!r} (#{index})"
    _unknown_keys(raw, PAIR_KEYS, where, problems)

    sides: dict[str, str | None] = {}
    for side in ("a", "b"):
        if side not in raw:
            problems.add(where, f"missing field {side!r} (a connection name)")
            sides[side] = None
            continue
        conn = _text(raw[side], side, where, problems)
        if conn is not None and isinstance(raw_conns, dict) and conn not in connections:
            problems.add(
                where,
                f"field {side!r}: no connection named {conn!r} in [connections]"
                f"{_suggest(conn, list(connections))}",
            )
            conn = None
        sides[side] = conn

    tables: dict[str, str | None] = {"a_table": None, "b_table": None}
    if "table" in raw:
        if "a_table" in raw or "b_table" in raw:
            problems.add(
                where,
                "field 'table' names both sides' table; use it alone, or "
                "a_table and b_table",
            )
        else:
            both = _text(raw["table"], "table", where, problems)
            tables = {"a_table": both, "b_table": both}
    else:
        for field_name in ("a_table", "b_table"):
            if field_name not in raw:
                problems.add(where, f"missing field {field_name!r} (or 'table' for both)")
            else:
                tables[field_name] = _text(raw[field_name], field_name, where, problems)

    key: list[str] | None = None
    if "key" not in raw:
        problems.add(
            where, "missing field 'key' (the column or columns that identify a row)"
        )
    else:
        key = _names(raw["key"], "key", where, problems)

    columns = None
    if "columns" in raw:
        columns = _names(raw["columns"], "columns", where, problems)
    exclude = (
        _names(raw["exclude"], "exclude", where, problems) if "exclude" in raw else []
    )
    column_map = _column_map(raw["map"], where, problems) if "map" in raw else {}

    filters: dict[str, str | None] = {}
    for field_name in ("where", "a_where", "b_where"):
        filters[field_name] = None
        if field_name in raw:
            text = _text(raw[field_name], field_name, where, problems)
            problem = None if text is None else row_filter_problem(text)
            if problem:
                problems.add(
                    where, f"field {field_name!r}: row filter {text!r} {problem}"
                )
            else:
                filters[field_name] = text
    if "where" in raw and "a_where" in raw and "b_where" in raw:
        problems.add(
            where,
            "field 'where' is replaced on both sides by a_where and b_where; remove one",
        )

    tuned = dict(defaults)
    for name_ in TUNABLES:
        if name_ in raw:
            got = _int_setting(raw[name_], name_, where, problems)
            if got is not None:
                tuned[name_] = got

    if len(problems.items) > before or name is None:
        return None
    assert key is not None and sides["a"] and sides["b"]
    assert tables["a_table"] and tables["b_table"]
    return PairSpec(
        name=name,
        a=sides["a"],
        b=sides["b"],
        a_table=tables["a_table"],
        b_table=tables["b_table"],
        key=tuple(key),
        columns=None if columns is None else tuple(columns),
        exclude=tuple(exclude or ()),
        column_map=tuple((column_map or {}).items()),
        # A per-side filter replaces `where` on its side, as in `parity diff`.
        where_a=filters["a_where"] if "a_where" in raw else filters["where"],
        where_b=filters["b_where"] if "b_where" in raw else filters["where"],
        float_scale=tuned["float_scale"],
        verify_sample=tuned["verify_sample"],
        max_diffs=tuned["max_diffs"],
        bisection_factor=tuned["bisection_factor"],
        threshold=tuned["threshold"],
    )


def select_pairs(cfg: RunConfig, only: Sequence[str] | None) -> list[PairSpec]:
    """The pairs `--only` names, in config order; all of them without it.

    An unknown name is a usage error naming the near miss, not a silent
    smaller run: "the subset passed" must mean the subset that was asked for.
    """
    if not only:
        return list(cfg.pairs)
    wanted: list[str] = []
    for value in only:
        wanted.extend(p.strip() for p in value.split(",") if p.strip())
    known = [p.name for p in cfg.pairs]
    missing = [n for n in wanted if n not in known]
    if missing:
        hints = "; ".join(f"{n!r}{_suggest(n, known)}" for n in missing)
        raise ConfigError(f"{cfg.path}: --only names no pair called {hints}")
    if not wanted:
        raise ConfigError(f"{cfg.path}: --only names no pair")
    chosen = set(wanted)
    return [p for p in cfg.pairs if p.name in chosen]


# ---------------------------------------------------------------------------
# Environment variables, and keeping their values out of the output
# ---------------------------------------------------------------------------


class Redactor:
    """Replaces every expanded environment value with its `${NAME}`.

    Driver errors quote connection strings, hosts and sometimes passwords;
    a value that came from the environment must never reach a terminal, a
    report or the state file. Longest values first, so a password that is a
    substring of a whole-URL variable is still caught inside it.
    """

    def __init__(self) -> None:
        """Start with nothing to redact."""
        self._values: dict[str, str] = {}

    def add(self, name: str, value: str) -> None:
        """Remember an expanded value and the variable it came from."""
        if value:
            self._values[value] = name

    def __call__(self, text: str) -> str:
        """`text` with every remembered value replaced by `${NAME}`."""
        for value in sorted(self._values, key=len, reverse=True):
            if value in text:
                text = text.replace(value, f"${{{self._values[value]}}}")
        return text


def expand_connection(
    name: str,
    value: str,
    environ: Mapping[str, str],
    redact: Redactor,
    source: str,
) -> str:
    """Replace each `${NAME}` in a connection string from the environment.

    Only `${NAME}` is special - whole value or inside a string - and nothing
    else is touched, so a `$` in a literal password stays a `$`. An unset
    variable is an error naming the variable and the connection; the value
    itself is never printed, and is registered with `redact`.
    """
    missing: list[str] = []

    def sub(match: re.Match[str]) -> str:
        """One reference's value, or record it as missing."""
        var = match.group(1)
        if var not in environ:
            missing.append(var)
            return ""
        redact.add(var, environ[var])
        return environ[var]

    out = _VAR.sub(sub, value)
    if missing:
        names = ", ".join(missing)
        raise ConfigError(
            f"{source}: connection {name!r}: environment variable "
            f"{'s' if len(missing) > 1 else ''}{names} "
            f"{'are' if len(missing) > 1 else 'is'} not set"
        )
    return out


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _now() -> str:
    """The current time, ISO 8601 UTC to the second."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class PairOutcome:
    """What happened to one pair.

    `status` is `identical`, `differ`, `error` or `not_run`. `result` is the
    pair's `parity diff --json` object (None for an error or a pair not
    run). `earlier_run` marks a verdict carried over by `--resume`, with the
    time it was reached in `finished_at`.
    """

    spec: PairSpec
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None
    seconds: float = 0.0
    finished_at: str | None = None
    earlier_run: bool = False
    not_run_reason: str | None = None
    config_hash: str = ""


@dataclass
class BatchRun:
    """A whole `parity run`: every selected pair's outcome, in config order."""

    config: RunConfig
    outcomes: list[PairOutcome]
    started_at: str
    finished_at: str = ""
    interrupted: bool = False
    stopped_by_fail_fast: bool = False
    state_path: str | None = None
    seconds: float = 0.0

    @property
    def complete(self) -> bool:
        """Every selected pair has a verdict (or an error)."""
        return all(o.status != "not_run" for o in self.outcomes)

    def count(self, status: str) -> int:
        """How many pairs have this status."""
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def identical(self) -> bool:
        """Every selected pair proven identical - and nothing left unrun."""
        return bool(self.outcomes) and all(o.status == "identical" for o in self.outcomes)

    @property
    def exit_code(self) -> int:
        """0 only if every pair is identical; 2 if any pair errored or the run
        was interrupted; otherwise 1 (a pair differs, or `--fail-fast`
        stopped after one)."""
        if self.count("error") or self.interrupted:
            return EXIT_ERROR
        if self.identical:
            return EXIT_IDENTICAL
        return EXIT_DIFFERENCES


def _status(result: dict[str, Any]) -> str:
    """A finished pair's status from its JSON: `identical` or `differ`."""
    return "identical" if result.get("identical") else "differ"


def _redact_result(result: dict[str, Any], redact: Redactor) -> dict[str, Any]:
    """Redact the tool's own messages in a result; never the data values.

    Warnings, schema notes, unexplained ranges and the sample check's texts
    are the tool's words and may quote a connection or a host; the compared
    values are evidence and are left exactly as compared.
    """
    for name in ("warnings", "schema_differences", "unexplained"):
        result[name] = [redact(t) for t in result.get(name, [])]
    v = result.get("verification")
    if v:
        for name in ("failures", "notes"):
            v[name] = [redact(t) for t in v.get(name, [])]
        if v.get("error"):
            v["error"] = redact(v["error"])
    return result


class _State:
    """The `--resume` file: each finished pair's result, written as it lands.

    A pair is skipped on a re-run only if it finished **identical** last time
    and its config hash is unchanged - the pair's resolved settings, the tool
    version, and a fingerprint of each side's expanded connection string, so
    pointing `${LEGACY_URL}` at another database runs the pair again. The
    fingerprint is salted PBKDF2, so the file is no shortcut to a password.
    """

    def __init__(self, path: str) -> None:
        """Load `path` if it exists; start a new state otherwise."""
        self.path = path
        self.salt = secrets.token_hex(16)
        self.pairs: dict[str, dict[str, Any]] = {}
        if not os.path.exists(path):
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(
                f"{path}: cannot read the --resume state file: {exc}"
            ) from exc
        if (
            not isinstance(data, dict)
            or data.get("parity_state") != STATE_FORMAT
            or not isinstance(data.get("salt"), str)
            or not isinstance(data.get("pairs"), dict)
        ):
            raise ConfigError(
                f"{path}: not a parity --resume state file (or one from another "
                f"version); delete it or pass another path"
            )
        self.salt = data["salt"]
        self.pairs = data["pairs"]

    def earlier_identical(
        self, spec: PairSpec, config_hash: str
    ) -> dict[str, Any] | None:
        """The stored entry if this pair finished identical with this hash."""
        entry = self.pairs.get(spec.name)
        if (
            isinstance(entry, dict)
            and entry.get("config_hash") == config_hash
            and entry.get("status") == "identical"
            and isinstance(entry.get("result"), dict)
            and entry["result"].get("identical") is True
        ):
            return entry
        return None

    def record(self, outcome: PairOutcome) -> None:
        """Store one finished pair and rewrite the file atomically."""
        self.pairs[outcome.spec.name] = {
            "config_hash": outcome.config_hash,
            "status": outcome.status,
            "finished_at": outcome.finished_at,
            "seconds": round(outcome.seconds, 3),
            "result": outcome.result,
            "error": outcome.error,
        }
        payload = {
            "parity_state": STATE_FORMAT,
            "parity_version": __version__,
            "salt": self.salt,
            "pairs": self.pairs,
        }
        tmp = f"{self.path}.tmp"
        Path(tmp).write_text(json.dumps(payload, indent=1), encoding="utf-8")
        # os.replace is atomic: a Ctrl-C mid-write leaves the old file whole.
        os.replace(tmp, self.path)

    def fingerprint(self, expanded: str) -> str:
        """Salted, slow hash of an expanded connection string."""
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            expanded.encode("utf-8"),
            bytes.fromhex(self.salt),
            _FINGERPRINT_ROUNDS,
        )
        return digest.hex()


def _config_hash(spec: PairSpec, fp_a: str, fp_b: str) -> str:
    """What must be unchanged for an earlier identical verdict to stand."""
    blob = json.dumps(
        {"version": __version__, "pair": spec.settings(), "a": fp_a, "b": fp_b},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _close(dialect: Any) -> None:
    """Close a side, best effort: a failure must not mask the diff's own."""
    if dialect is None:
        return
    try:
        dialect.close()
    except Exception:  # noqa: BLE001, S110 - best effort, as in the CLI
        pass


def _run_one(
    spec: PairSpec,
    diff: Callable[..., Any],
    opener: Opener,
    expanded: Mapping[str, str],
    redact: Redactor,
    state: _State | None,
    fingerprint: Callable[[str], str],
) -> tuple[PairOutcome, bool]:
    """One pair: carried over from the state file, or run on two fresh
    connections that are closed before returning. Returns the outcome and
    whether it was run now (False when carried over)."""
    config_hash = _config_hash(spec, fingerprint(spec.a), fingerprint(spec.b))
    earlier = state.earlier_identical(spec, config_hash) if state else None
    if earlier is not None:
        return (
            PairOutcome(
                spec,
                "identical",
                result=earlier["result"],
                seconds=float(earlier.get("seconds") or 0.0),
                finished_at=earlier.get("finished_at"),
                earlier_run=True,
                config_hash=config_hash,
            ),
            False,
        )
    t0 = time.perf_counter()
    a = b = None
    try:
        # Each side opened separately so a failure names which one.
        a = opener(expanded[spec.a], side="A", float_scale=spec.float_scale)
        b = opener(expanded[spec.b], side="B", float_scale=spec.float_scale)
        result = diff(
            a,
            b,
            a_table=spec.a_table,
            b_table=spec.b_table,
            key=list(spec.key),
            columns=None if spec.columns is None else list(spec.columns),
            exclude=list(spec.exclude),
            bisection_factor=spec.bisection_factor,
            threshold=spec.threshold,
            # 0 is the documented way to ask for no limit, as on the CLI.
            max_diffs=spec.max_diffs or None,
            verify_sample=spec.verify_sample,
            where_a=spec.where_a,
            where_b=spec.where_b,
            column_map=dict(spec.column_map),
        )
        data = _redact_result(to_dict(result), redact)
        outcome = PairOutcome(spec, _status(data), result=data)
    except Exception as exc:  # noqa: BLE001 - one pair's error is that pair's
        outcome = PairOutcome(spec, "error", error=redact(str(exc)) or type(exc).__name__)
    finally:
        _close(a)
        _close(b)
    outcome.seconds = time.perf_counter() - t0
    outcome.finished_at = _now()
    outcome.config_hash = config_hash
    return outcome, True


def run_batch(
    cfg: RunConfig,
    *,
    only: Sequence[str] | None = None,
    fail_fast: bool = False,
    state_path: str | None = None,
    opener: Opener | None = None,
    environ: Mapping[str, str] | None = None,
    on_outcome: Callable[[PairOutcome], None] | None = None,
) -> BatchRun:
    """Run the selected pairs one after another and return every outcome.

    Every check that needs no database - `--only`, every environment
    variable the selected pairs use, the state file - happens before the
    first connection. Each pair opens two fresh connections and closes them
    before the next pair, so no pair shares a snapshot or a session with
    another. A pair that raises is recorded as an error and the run goes on
    (unless `fail_fast`); KeyboardInterrupt stops the run and returns what
    finished, `interrupted` set.
    """
    from parity.engine import diff

    if opener is None:
        from parity.dialects.base import get_dialect

        opener = get_dialect
    environ = os.environ if environ is None else environ
    specs = select_pairs(cfg, only)

    redact = Redactor()
    expanded: dict[str, str] = {}
    errors: list[str] = []
    for conn in dict.fromkeys(name for s in specs for name in (s.a, s.b)):
        try:
            expanded[conn] = expand_connection(
                conn, cfg.connections[conn], environ, redact, cfg.path
            )
        except ConfigError as exc:
            errors.append(str(exc))
    if errors:
        raise ConfigError("\n".join(errors))
    state = _State(state_path) if state_path else None
    fingerprints: dict[str, str] = {}

    def fingerprint(conn: str) -> str:
        """Each connection's fingerprint, computed once (PBKDF2 is slow)."""
        if state is None:
            return ""
        if conn not in fingerprints:
            fingerprints[conn] = state.fingerprint(expanded[conn])
        return fingerprints[conn]

    run = BatchRun(config=cfg, outcomes=[], started_at=_now(), state_path=state_path)
    started = time.perf_counter()
    stop_reason: str | None = None
    for spec in specs:
        if stop_reason is not None:
            run.outcomes.append(PairOutcome(spec, "not_run", not_run_reason=stop_reason))
            continue
        try:
            outcome, fresh = _run_one(
                spec, diff, opener, expanded, redact, state, fingerprint
            )
        except KeyboardInterrupt:
            # Ctrl-C mid-pair: that pair has no verdict; everything before
            # it keeps its own, and the summary is still written.
            run.interrupted = True
            stop_reason = "interrupted"
            run.outcomes.append(PairOutcome(spec, "not_run", not_run_reason=stop_reason))
            continue
        run.outcomes.append(outcome)
        try:
            if fresh and state is not None:
                state.record(outcome)
            if on_outcome:
                on_outcome(outcome)
        except KeyboardInterrupt:
            run.interrupted = True
            stop_reason = "interrupted"
            continue
        if fail_fast and outcome.status in ("differ", "error"):
            run.stopped_by_fail_fast = True
            stop_reason = "--fail-fast"
    run.finished_at = _now()
    run.seconds = time.perf_counter() - started
    return run


# ---------------------------------------------------------------------------
# Qualifiers: everything that keeps "identical" from meaning more than it does
# ---------------------------------------------------------------------------


def _rows(result: dict[str, Any]) -> int:
    """The larger side's row count, as the single-pair headline prints it."""
    stats = result.get("stats", {})
    return max(int(stats.get("rows_a", 0)), int(stats.get("rows_b", 0)))


def filter_text(result: dict[str, Any]) -> str:
    """`A where <...>; B whole table`, as the single-pair report prints it."""
    return "; ".join(
        f"{side} where {where}" if where else f"{side} whole table"
        for side, where in (("A", result.get("where_a")), ("B", result.get("where_b")))
    )


def risk_text(result: dict[str, Any]) -> str:
    """The pair's risk line, in words, from its JSON `risk` figures."""
    r = result.get("risk") or {}
    frac = r.get("max_hidden_fraction")
    if frac is None:
        return (
            "not computed: the independent sample check was skipped, failed, "
            "could not run or sampled nothing, so the verdict stands on the "
            "checksums alone"
        )
    collision = "a checksum collision is below 1 in 10^18"
    rows = "filtered rows" if r.get("filtered") else "rows"
    if frac == 0.0:
        return (
            f"every {rows[:-1]} was compared by both methods, so no {rows[:-1]} "
            f"can hide a rendering error; {collision}"
        )
    conf = f"{float(r.get('confidence', 0)) * 100:g}%"
    return (
        f"with {conf} confidence fewer than {frac:.3%} of {rows} (at most "
        f"{r.get('max_hidden_rows', 0):,} of {r.get('rows', 0):,}) could differ in a "
        f"way the checksums missed; {collision}"
    )


def qualifiers(outcome: PairOutcome) -> list[tuple[str, str]]:
    """Everything that qualifies a pair's verdict, as `(severity, text)`.

    `bad` changes the verdict (it is why a pair is not identical); `warn`
    qualifies it without changing it. These are exactly the lines the
    single-pair report prints before its rows, so no output format of the
    summary can say less about a pair than `parity diff` would.
    """
    out: list[tuple[str, str]] = []
    if outcome.earlier_run:
        out.append(
            ("warn", f"verdict from an earlier run at {outcome.finished_at} (--resume)")
        )
    result = outcome.result
    if result is None:
        return out
    if result.get("where_a") or result.get("where_b"):
        out.append(("warn", f"filtered rows only: {filter_text(result)}"))
    if result.get("column_map"):
        pairs = ", ".join(f"{a} = B {b}" for a, b in result["column_map"].items())
        out.append(("info", f"renamed columns (A = B): {pairs}"))
    if result.get("truncated"):
        out.append(
            (
                "bad",
                (
                    f"stopped early at --max-diffs: at least "
                    f"{result['difference_count']:,} differences, the rest was not checked"
                ),
            )
        )
    for note in result.get("schema_differences", []):
        out.append(("bad", f"schema: {note}"))
    for note in result.get("unexplained", []):
        out.append(("bad", f"unverified {note}"))
    v = result.get("verification")
    if v is None:
        reason = (
            "off (--verify-sample 0): the verdict rests on the checksums alone"
            if result.get("verify_sample") == 0
            else "did not run (the walk stopped early, or both tables are empty)"
        )
        out.append(("warn", f"no independent sample check: {reason}"))
    else:
        if v.get("error"):
            # A check asked for that could not run leaves the pair unproven,
            # never identical (as in the single-pair report).
            out.append(("bad", f"independent sample check could not run: {v['error']}"))
        for failure in v.get("failures", []):
            out.append(("bad", f"sample check contradicts the checksums: {failure}"))
        if v.get("unchecked"):
            out.append(
                (
                    "warn",
                    (
                        f"not covered by the sample check (type outside its rules): "
                        f"{v['unchecked']}"
                    ),
                )
            )
        for note in v.get("notes", []):
            out.append(("warn", f"sample: {note}"))
    for warning in result.get("warnings", []):
        out.append(("warn", warning))
    return out


def short_qualifiers(outcome: PairOutcome) -> list[str]:
    """`qualifiers` as short tags for one table cell: `filtered`,
    `truncated`, `2 caveats`... Every qualifier kind is named; the texts are
    in the pair's own section."""
    tags: list[str] = []
    if outcome.earlier_run:
        tags.append(f"from an earlier run ({outcome.finished_at})")
    result = outcome.result
    if result is None:
        return tags
    v = result.get("verification")
    checks = [
        (bool(result.get("where_a") or result.get("where_b")), "filtered rows only"),
        (bool(result.get("column_map")), "renamed columns"),
        (bool(result.get("truncated")), "truncated (stopped early)"),
        (bool(result.get("schema_differences")), "schema differences"),
        (bool(result.get("unexplained")), "unexplained checksums"),
        (bool(v and v.get("failures")), "sample check FAILED"),
        (bool(v and v.get("error")), "sample check could not run"),
        (v is None, "no sample check"),
        (bool(v and v.get("unchecked")), "uncovered columns"),
        (bool(v and v.get("notes")), "sample notes"),
    ]
    tags.extend(tag for hit, tag in checks if hit)
    n = len(result.get("warnings", []))
    if n:
        tags.append(f"{n} caveat{'s' if n != 1 else ''}")
    return tags


def _headline(outcome: PairOutcome) -> str:
    """A pair's verdict in a few words, matching the single-pair headline."""
    if outcome.status == "error":
        return "error"
    if outcome.status == "not_run":
        return f"not run ({outcome.not_run_reason})"
    result = outcome.result or {}
    n = int(result.get("difference_count", 0))
    if result.get("truncated"):
        text = f"at least {n:,} differences (stopped early)"
    elif n:
        text = f"{n:,} difference{'s' if n != 1 else ''}"
    elif (result.get("verification") or {}).get("failures"):
        text = "not verified (sample check failed)"
    elif (result.get("verification") or {}).get("error"):
        text = "not proven (sample check could not run)"
    elif result.get("unexplained"):
        text = "not verified (unexplained checksums)"
    elif result.get("schema_differences"):
        text = "schemas differ"
    else:
        text = "no differences"
    if result.get("where_a") or result.get("where_b"):
        text += " (filtered rows only)"
    return text


def totals_text(run: BatchRun) -> str:
    """`212 of 215 pairs identical, 2 differ, 1 error` (+ not run)."""
    total = len(run.outcomes)
    text = (
        f"{run.count('identical'):,} of {total:,} pair{'s' if total != 1 else ''} "
        f"identical, {run.count('differ'):,} differ, {run.count('error'):,} "
        f"error{'s' if run.count('error') != 1 else ''}"
    )
    if run.count("not_run"):
        text += f", {run.count('not_run'):,} not run"
    return text


def _qualified_identical(run: BatchRun) -> list[PairOutcome]:
    """Identical pairs whose verdict carries a qualifier worth reading."""
    return [
        o
        for o in run.outcomes
        if o.status == "identical" and any(s == "warn" for s, _ in qualifiers(o))
    ]


def _weakest_risk(run: BatchRun) -> tuple[PairOutcome, str] | None:
    """The identical pair with the weakest risk bound, and its risk line.

    The run's own bound is never stronger than this pair's: a pair with no
    computed figure (sample check skipped or failed) is weaker than any.
    """
    worst: tuple[float, PairOutcome] | None = None
    for o in run.outcomes:
        if o.status != "identical" or o.result is None:
            continue
        frac = (o.result.get("risk") or {}).get("max_hidden_fraction")
        weight = 2.0 if frac is None else float(frac)
        if worst is None or weight > worst[0]:
            worst = (weight, o)
    if worst is None:
        return None
    return worst[1], risk_text(worst[1].result or {})


# ---------------------------------------------------------------------------
# Human output
# ---------------------------------------------------------------------------


class HumanReport:
    """Streams one block per pair as it finishes, then the totals."""

    def __init__(self, out: TextIO) -> None:
        """Write to `out`, with glyphs it can encode."""
        self.w = _Writer(out)
        self.sym = _symbols(out)

    def pair(self, outcome: PairOutcome) -> None:
        """One line for the pair, then its qualifiers, indented."""
        sym = self.sym
        mark = {
            "identical": sym["ok"],
            "differ": sym["bad"],
            "error": "!",
            "not_run": "-",
        }[outcome.status]
        name = _escape(outcome.spec.name)
        if outcome.status == "error":
            self.w.line(f"{mark} {name:<24} error: {_escape(outcome.error or '')}")
            return
        if outcome.status == "not_run":
            self.w.line(f"{mark} {name:<24} {_headline(outcome)}")
            return
        result = outcome.result or {}
        rows = _rows(result)
        line = (
            f"{mark} {name:<24} {rows:>12,} rows {sym['dot']} {_headline(outcome)} "
            f"{sym['dot']} {outcome.seconds:.1f}s {sym['dot']} scale "
            f"{result.get('float_scale')}"
        )
        if outcome.earlier_run:
            line += f" {sym['dot']} from an earlier run ({outcome.finished_at})"
        self.w.line(line)
        for severity, text in qualifiers(outcome):
            if outcome.earlier_run and text.startswith("verdict from an earlier run"):
                continue
            glyph = {"bad": sym["bad"], "warn": "!", "info": " "}[severity]
            self.w.line(f"    {glyph} {_escape(text)}")

    def totals(self, run: BatchRun) -> None:
        """The run's verdict: counts, then everything that qualifies it."""
        w, sym = self.w, self.sym
        w.line()
        mark = sym["ok"] if run.identical else sym["bad"]
        w.line(f"{mark} {totals_text(run)}")
        qualified = _qualified_identical(run)
        if qualified:
            w.line(
                f"  ! {len(qualified):,} identical pair(s) carry qualifiers (filters, "
                f"caveats, uncovered columns, no sample check): read their lines above"
            )
        earlier = [o for o in run.outcomes if o.earlier_run]
        if earlier:
            w.line(
                f"  ! {len(earlier):,} pair(s) not re-run: verdict from an earlier run "
                f"recorded in {_escape(run.state_path or '')}: "
                + ", ".join(_escape(o.spec.name) for o in earlier)
            )
        weakest = _weakest_risk(run)
        if weakest is not None:
            w.line(
                f"  risk, weakest identical pair ({_escape(weakest[0].spec.name)}): "
                f"{weakest[1]}"
            )
        if run.interrupted:
            w.line(
                "  ! INCOMPLETE: interrupted; pairs marked 'not run' were not checked "
                "and the run proves nothing about them"
            )
        elif run.stopped_by_fail_fast:
            w.line("  ! stopped by --fail-fast; pairs marked 'not run' were not checked")
        if run.count("differ"):
            w.line("  the differing rows are listed by --json and --markdown FILE")
        w.line(f"  {run.seconds:.1f}s {sym['dot']} exit {run.exit_code}")


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def run_to_dict(run: BatchRun) -> dict[str, Any]:
    """The whole run as one object: totals, then every pair in full.

    `pairs[].result` is exactly what `parity diff --json` prints for that
    pair. `identical` is true only when every selected pair is identical and
    none was left unrun, so a consumer reading only it cannot be misled.
    """
    return {
        "identical": run.identical,
        "complete": run.complete,
        "interrupted": run.interrupted,
        "stopped_by_fail_fast": run.stopped_by_fail_fast,
        "exit_code": run.exit_code,
        "parity_version": __version__,
        "config": run.config.path,
        "config_sha256": run.config.sha256,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "seconds": round(run.seconds, 3),
        "totals": {
            "pairs": len(run.outcomes),
            "identical": run.count("identical"),
            "differ": run.count("differ"),
            "error": run.count("error"),
            "not_run": run.count("not_run"),
            "from_earlier_run": sum(1 for o in run.outcomes if o.earlier_run),
            "identical_with_qualifiers": len(_qualified_identical(run)),
        },
        "pairs": [
            {
                "name": o.spec.name,
                "status": o.status,
                "identical": o.status == "identical",
                "a": o.spec.a,
                "b": o.spec.b,
                "a_table": o.spec.a_table,
                "b_table": o.spec.b_table,
                "settings": o.spec.settings(),
                "seconds": round(o.seconds, 3),
                "finished_at": o.finished_at,
                "from_earlier_run": o.earlier_run,
                "not_run_reason": o.not_run_reason,
                "config_hash": o.config_hash or None,
                "qualifiers": [{"severity": s, "text": t} for s, t in qualifiers(o)],
                "error": o.error,
                "result": o.result,
            }
            for o in run.outcomes
        ],
    }


# ---------------------------------------------------------------------------
# Markdown sign-off report
# ---------------------------------------------------------------------------

_MD_SPECIAL = re.compile(r"([\\`*_\[\]|#])")


def _md(text: object) -> str:
    """Text made inert in Markdown: no formatting, no HTML, one line."""
    s = _escape(str(text))
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MD_SPECIAL.sub(r"\\\1", s)


def _md_value(value: str) -> str:
    """One canonical value as the human report shows it (NULL bare, text
    quoted), made inert in Markdown."""
    decoded = decode_canonical_field(value)
    return "NULL" if decoded is None else _md(repr(decoded))


def _md_key(key: object) -> str:
    """A key as the human report shows it; composite parts joined by `|`."""
    if isinstance(key, str):
        return " \\| ".join(_md_value(part) for part in key.split("\x1f"))
    return _md(key)


_VERDICT_WORD = {
    "identical": "IDENTICAL",
    "differ": "NOT IDENTICAL",
    "error": "ERROR",
    "not_run": "NOT RUN",
}


def render_markdown(run: BatchRun) -> str:
    """A sign-off report: header, the table of pairs, then each pair in full.

    Written to be attached to a cutover ticket. Every pair shows its tables,
    key, filters, renames, float scale, risk line, caveats, uncovered columns
    and the first differences; a pair from an earlier run says so with its
    timestamp.
    """
    lines: list[str] = []
    add = lines.append
    overall = (
        "IDENTICAL"
        if run.identical
        else ("ERROR" if run.exit_code == EXIT_ERROR else "NOT IDENTICAL")
    )
    add("# parity sign-off report")
    add("")
    add(f"**Verdict: {overall}** - {_md(totals_text(run))} (exit {run.exit_code})")
    add("")
    if run.interrupted:
        add(
            "> **INCOMPLETE: the run was interrupted.** Pairs marked NOT RUN were "
            "not checked, and this report proves nothing about them."
        )
        add("")
    elif run.stopped_by_fail_fast:
        add("> Stopped by `--fail-fast`: pairs marked NOT RUN were not checked.")
        add("")
    add("| | |")
    add("|---|---|")
    add(f"| Tool | parity {_md(__version__)} |")
    add(f"| Started | {_md(run.started_at)} |")
    add(f"| Finished | {_md(run.finished_at)} |")
    add(f"| Config file | {_md(run.config.path)} |")
    add(f"| Config SHA-256 | `{run.config.sha256 or 'n/a'}` |")
    earlier = sum(1 for o in run.outcomes if o.earlier_run)
    if earlier:
        add(
            f"| Carried over | {earlier} pair(s) from an earlier run "
            f"({_md(run.state_path or '')}), not re-checked |"
        )
    add("")
    add(
        "A multi-pair verdict is each pair's verdict: the run is identical only "
        "if every pair is, and it proves nothing beyond what each pair's own "
        "section below states (filters, float scale, caveats, uncovered "
        "columns)."
    )
    weakest = _weakest_risk(run)
    if weakest is not None:
        add("")
        add(
            f"Weakest risk among identical pairs ({_md(weakest[0].spec.name)}): "
            f"{_md(weakest[1])}."
        )
    add("")
    add("## Pairs")
    add("")
    add("| Verdict | Pair | Rows | Differences | Seconds | Qualifiers |")
    add("|---|---|---:|---:|---:|---|")
    for o in run.outcomes:
        result = o.result or {}
        rows = f"{_rows(result):,}" if o.result else ""
        diffs = f"{int(result.get('difference_count', 0)):,}" if o.result else ""
        quals = "; ".join(_md(t) for t in short_qualifiers(o))
        if o.status == "error":
            quals = _md(o.error or "")
        elif o.status == "not_run":
            quals = _md(f"not run ({o.not_run_reason})")
        add(
            f"| {_VERDICT_WORD[o.status]} | {_md(o.spec.name)} | {rows} | {diffs} | "
            f"{o.seconds:.1f} | {quals} |"
        )
    for o in run.outcomes:
        add("")
        add(f"## {_md(o.spec.name)}: {_VERDICT_WORD[o.status]}")
        add("")
        spec = o.spec
        add(f"- A: `{_md(spec.a)}` table {_md(spec.a_table)}")
        add(f"- B: `{_md(spec.b)}` table {_md(spec.b_table)}")
        add(f"- key: {_md(', '.join(spec.key))}")
        if o.status == "error":
            add(f"- **error:** {_md(o.error or '')}")
            continue
        if o.status == "not_run":
            add(f"- **not run:** {_md(o.not_run_reason or '')}")
            continue
        result = o.result or {}
        if o.earlier_run:
            add(
                f"- **verdict from an earlier run** at {_md(o.finished_at)} "
                f"(`--resume`); not re-checked in this run"
            )
        else:
            add(f"- checked at {_md(o.finished_at)} in {o.seconds:.1f}s")
        add(f"- result: {_md(_headline(o))} in {_rows(result):,} rows")
        add(f"- filters: {_md(filter_text(result))}")
        cmap = result.get("column_map") or {}
        add(
            "- renamed columns (A = B): "
            + (", ".join(f"{_md(a)} = {_md(b)}" for a, b in cmap.items()) or "none")
        )
        add(
            f"- floats and decimals compared at {result.get('float_scale')} decimal places"
        )
        add(f"- columns compared: {_md(', '.join(result.get('columns_compared', [])))}")
        add(f"- risk: {_md(risk_text(result))}")
        v = result.get("verification")
        add(
            "- uncovered columns (no risk figure covers them): "
            + (_md(", ".join(v["unchecked"])) if v and v.get("unchecked") else "none")
        )
        stats = result.get("stats", {})
        add(
            f"- cost: {stats.get('queries', 0):,} queries, "
            f"{stats.get('rows_downloaded', 0):,} rows downloaded "
            f"({stats.get('percent_downloaded', 0)}% of both tables)"
        )
        notes = [(s, t) for s, t in qualifiers(o) if s != "info"]
        if notes:
            add("")
            add("Qualifiers and caveats:")
            add("")
            for severity, text in notes:
                mark = "**not identical:** " if severity == "bad" else ""
                add(f"- {mark}{_md(text)}")
        differences = result.get("differences", [])
        if differences:
            add("")
            add("| Kind | Key | Column | A | B |")
            add("|---|---|---|---|---|")
            for d in differences[:MARKDOWN_DIFF_LIMIT]:
                kind = KIND_LABELS.get(d["kind"], d["kind"])
                if d["kind"] != "different":
                    add(f"| {kind} | {_md_key(d['key'])} | | | |")
                    continue
                for col in d["columns"]:
                    add(
                        f"| {kind} | {_md_key(d['key'])} | {_md(col)} | "
                        f"{_md_value(d['a'].get(col, ''))} | "
                        f"{_md_value(d['b'].get(col, ''))} |"
                    )
            if len(differences) > MARKDOWN_DIFF_LIMIT:
                add("")
                add(
                    f"{len(differences) - MARKDOWN_DIFF_LIMIT:,} more difference(s) "
                    f"not listed here; `parity run --json` carries all of them."
                )
    add("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JUnit XML
# ---------------------------------------------------------------------------

_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def _xml(text: str) -> str:
    """Text XML 1.0 can carry: invalid characters written as their escapes."""
    return _XML_INVALID.sub(lambda m: repr(m.group(0))[1:-1], text)


def render_junit(run: BatchRun) -> str:
    """One `<testcase>` per pair: a failure for differences, an error for an
    error, skipped for a pair not run; every qualifier in `<system-out>` and
    as a property, so a CI page never shows a bare pass for a qualified pair.
    """
    suite = ET.Element(
        "testsuite",
        {
            "name": f"parity run {Path(run.config.path).name}",
            "tests": str(len(run.outcomes)),
            "failures": str(run.count("differ")),
            "errors": str(run.count("error")),
            "skipped": str(run.count("not_run")),
            "time": f"{run.seconds:.3f}",
            "timestamp": run.started_at,
        },
    )
    props = ET.SubElement(suite, "properties")
    for name, value in (
        ("parity_version", __version__),
        ("config_sha256", run.config.sha256),
        ("complete", str(run.complete).lower()),
        ("interrupted", str(run.interrupted).lower()),
    ):
        ET.SubElement(props, "property", {"name": name, "value": _xml(value)})
    for o in run.outcomes:
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "parity",
                "name": _xml(o.spec.name),
                "time": f"{o.seconds:.3f}",
            },
        )
        quals = qualifiers(o)
        if quals:
            case_props = ET.SubElement(case, "properties")
            for severity, text in quals:
                ET.SubElement(
                    case_props, "property", {"name": severity, "value": _xml(text)}
                )
        detail = [
            (
                f"A {o.spec.a} {o.spec.a_table}  B {o.spec.b} {o.spec.b_table}  "
                f"key {', '.join(o.spec.key)}"
            )
        ]
        if o.status == "error":
            node = ET.SubElement(case, "error", {"message": _xml(o.error or "error")})
            node.text = _xml(o.error or "")
        elif o.status == "not_run":
            ET.SubElement(
                case, "skipped", {"message": _xml(f"not run ({o.not_run_reason})")}
            )
        else:
            result = o.result or {}
            detail.append(f"{_headline(o)} in {_rows(result):,} rows")
            detail.append(
                f"floats and decimals at {result.get('float_scale')} decimal places"
            )
            detail.append(f"risk: {risk_text(result)}")
            if o.status == "differ":
                body = [f"{_headline(o)}"]
                for d in result.get("differences", [])[:MARKDOWN_DIFF_LIMIT]:
                    body.append(f"{d['kind']} key {d['key']!r} {d['columns']}")
                node = ET.SubElement(case, "failure", {"message": _xml(_headline(o))})
                node.text = _xml("\n".join(body))
        detail.extend(f"{s}: {t}" for s, t in quals)
        out = ET.SubElement(case, "system-out")
        out.text = _xml("\n".join(detail))
    root = ET.Element("testsuites")
    root.append(suite)
    ET.indent(root)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + ET.tostring(root, encoding="unicode")
        + "\n"
    )


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _write_file(path: str, text: str) -> None:
    """Write a report file as UTF-8, atomically."""
    tmp = f"{path}.tmp"
    Path(tmp).write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def run_command(
    args: argparse.Namespace,
    out: TextIO,
    err: TextIO,
    opener: Opener | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """`parity run`: validate, run every pair, write every requested output.

    The config is validated in full before any connection. Human output
    streams one block per pair as it finishes; `--json`, `--markdown` and
    `--junit` are written once at the end - also after Ctrl-C, marked
    incomplete.
    """
    cfg = read_config(args.config)
    human = None if (args.quiet or args.json) else HumanReport(out)
    run = run_batch(
        cfg,
        only=args.only,
        fail_fast=args.fail_fast,
        state_path=args.resume,
        opener=opener,
        environ=environ,
        on_outcome=human.pair if human else None,
    )
    if human is not None:
        for o in run.outcomes:
            if o.status == "not_run":
                human.pair(o)
        human.totals(run)
    elif args.json and not args.quiet:
        json.dump(run_to_dict(run), out, indent=2)
        print(file=out)
    try:
        if args.markdown:
            _write_file(args.markdown, render_markdown(run))
        if args.junit:
            _write_file(args.junit, render_junit(run))
    except OSError as exc:
        _Writer(err).line(f"parity: could not write a report: {_escape(str(exc))}")
        return EXIT_ERROR
    if run.interrupted:
        _Writer(err).line(
            "parity: interrupted - the summary covers the pairs that finished"
        )
    return run.exit_code
