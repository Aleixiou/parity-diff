"""Read saved engine runs and compare one logical value across engines.

Two engines that both hold the logical value `key` of family `cross_group`
must render it to byte-identical text, or a migrated table that holds the
same data on both sides reports differences (a false alarm) - and, for a
pair of values, the converse risk is a false match. Comparisons are split
by whether the two cells share a logical type: the same logical type with
different text is a rendering bug candidate; different logical types are a
type-mapping mismatch (MySQL's BOOLEAN is TINYINT, say) that a user can
only fix by casting.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cov_values import cross_group

#: Cell statuses whose text is a rendering to compare.
RENDERED = {"agree", "disagree", "unchecked", "unruled"}


def load_runs(results: Path, scale: int) -> dict[str, dict[str, Any]]:
    """Every saved run at `scale`, by engine name."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(results.glob(f"*-s{scale}.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        out[body["engine"]] = body
    return out


@dataclass
class Split:
    """One logical value that does not render alike everywhere it is held."""

    family: str
    key: str
    #: text -> [(engine, native, logical type)]
    texts: dict[str, list[tuple[str, str, str]]]
    same_logical: bool

    def describe(self) -> str:
        """One line per distinct text, naming who renders it."""
        parts = []
        for text, who in self.texts.items():
            names = ", ".join(f"{e} {n} ({lt})" for e, n, lt in who)
            parts.append(f"{text!r} <- {names}")
        return f"{self.family} {self.key}: " + " | ".join(parts)


def compare(runs: dict[str, dict[str, Any]]) -> tuple[int, list[Split]]:
    """(number of logical values held by two or more engines, the splits).

    Within one family on one engine, cells of different native types must
    agree too; that is the per-engine check's job, so here each text is
    attributed to (engine, native) and any two distinct texts are a split.
    """
    seen: dict[tuple[str, str], dict[str, list[tuple[str, str, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    engines: dict[tuple[str, str], set[str]] = defaultdict(set)
    for name, body in runs.items():
        for c in body["cells"]:
            if c["status"] not in RENDERED or c["text"] is None:
                continue
            fam = cross_group(c["group"])
            seen[(fam, c["key"])][c["text"]].append((name, c["native"], c["logical"]))
            engines[(fam, c["key"])].add(name)
    shared = [k for k, e in engines.items() if len(e) >= 2]
    splits: list[Split] = []
    for k in sorted(shared):
        texts = seen[k]
        if len(texts) <= 1:
            continue
        logicals = {lt for who in texts.values() for _, _, lt in who}
        # Same logical type: some pair of cells shares a logical type but not
        # a text. With one logical type everywhere that is every split.
        same = len(logicals) == 1 or any(
            len(
                {lt for _, _, lt in who}
                & {lt for t2, w2 in texts.items() if t2 != t for _, _, lt in w2}
            )
            > 0
            for t, who in texts.items()
        )
        splits.append(Split(k[0], k[1], {t: list(w) for t, w in texts.items()}, same))
    return len(shared), splits


@dataclass
class Collision:
    """One text that two *different* values render to, on any engines."""

    family: str
    text: str
    #: [(engine, native, value key)], at least two distinct keys
    who: list[tuple[str, str, str]]

    def describe(self) -> str:
        """The text and every (engine, native, key) that renders it."""
        names = "; ".join(f"{e} {n} {k}" for e, n, k in self.who)
        return f"{self.family} {self.text!r} <- {names}"


def collisions(runs: dict[str, dict[str, Any]], families: set[str]) -> list[Collision]:
    """Texts shared by distinct values of one family, across all engines.

    Only meaningful where no rule decides equality (the UNKNOWN families):
    for a ruled family two values may legitimately share a text - 0.0000004
    and 0 are one value at scale 6 - and the per-engine check already holds
    every text to the rules.
    """
    by: dict[tuple[str, str], set[tuple[str, str, str]]] = defaultdict(set)
    for name, body in runs.items():
        for c in body["cells"]:
            fam = cross_group(c["group"])
            if fam not in families or c["status"] not in RENDERED or c["text"] is None:
                continue
            by[(fam, c["text"])].add((name, c["native"], c["key"]))
    out = []
    for (fam, text), who in sorted(by.items()):
        if len({k for _, _, k in who}) > 1:
            out.append(Collision(fam, text, sorted(who)))
    return out
