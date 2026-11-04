"""`docs/risk.md` must describe the code as it is - after every change.

The error-odds file is what a client is told about how far a verdict can be
trusted, so it must never describe an older tool. Three checks:

1. A fingerprint of `src/parity/` is written in the file. Any change to the
   tool changes it and fails this test until the file has been reviewed and
   re-stamped (`python tests/test_risk_doc.py --stamp`).
2. The constants it quotes - the checksum collision bound, the confidence
   level, the default sample size - are the code's.
3. Its coverage figures are the ones `docs/coverage.md` reports.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RISK = ROOT / "docs" / "risk.md"
COVERAGE = ROOT / "docs" / "coverage.md"
STAMP = re.compile(r"src-sha256: ([0-9a-f]{16})")


def source_fingerprint() -> str:
    """SHA-256 of every source file under src/parity, newline-normalised so
    a Windows checkout (CRLF) and CI (LF) agree; the first 16 hex digits."""
    h = hashlib.sha256()
    for path in sorted((ROOT / "src" / "parity").rglob("*.py")):
        h.update(path.relative_to(ROOT).as_posix().encode())
        h.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def test_the_risk_file_was_reviewed_for_the_current_source():
    """Changed the tool? Review docs/risk.md, then re-stamp it."""
    found = STAMP.search(RISK.read_text(encoding="utf-8"))
    assert found, "docs/risk.md has no src-sha256 stamp"
    assert found[1] == source_fingerprint(), (
        "src/parity changed since docs/risk.md was last reviewed: update the "
        "figures that moved, then run `python tests/test_risk_doc.py --stamp`"
    )


def test_the_constants_it_quotes_are_the_codes():
    """2^-60, 99.9% and the default sample size, as the code has them."""
    from parity.engine import DEFAULT_VERIFY_SAMPLE
    from parity.types import CHECKSUM_COLLISION_BOUND, RISK_CONFIDENCE

    text = RISK.read_text(encoding="utf-8")
    assert CHECKSUM_COLLISION_BOUND == 2.0**-60
    assert "2⁻⁶⁰ ≈ 8.67 × 10⁻¹⁹" in text  # noqa: RUF001 - the file prints a real multiplication sign
    assert RISK_CONFIDENCE == 0.999 and "99.9% confidence" in text
    assert f"default n = {DEFAULT_VERIFY_SAMPLE:,}" in text
    # The headline share is the bound the code computes at the default size.
    bound = 1 - (1 - RISK_CONFIDENCE) ** (1 / DEFAULT_VERIFY_SAMPLE)
    assert f"**{bound:.3%}** at the default n" in text, f"{bound:.3%}"


def test_its_coverage_figures_are_the_coverage_maps():
    """The ruled-cell counts in docs/risk.md match docs/coverage.md."""
    cov = COVERAGE.read_text(encoding="utf-8")
    m = re.search(r"\*\*(\d+) of (\d+)\*\* ruled cells", cov)
    assert m, "docs/coverage.md has no ruled-cell summary"
    exact, ruled = int(m[1]), int(m[2])
    text = RISK.read_text(encoding="utf-8").replace("**", "")
    assert f"{ruled:,} checks at scale 6 ({exact:,} exact" in text
    assert f"{ruled - exact} required refusals" in text


def _stamp() -> None:
    """Write the current fingerprint into docs/risk.md."""
    text = RISK.read_text(encoding="utf-8")
    new = STAMP.sub(f"src-sha256: {source_fingerprint()}", text, count=1)
    RISK.write_text(new, encoding="utf-8", newline="\n")
    print(f"docs/risk.md stamped {source_fingerprint()}")


if __name__ == "__main__" and "--stamp" in sys.argv:
    _stamp()
