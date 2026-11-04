"""Snowflake connection handling, offline: URL parameters, key-pair sign-in,
and that no secret ever reaches an error.

Nothing here touches the network or a Snowflake account. The connector's
`connect` is replaced by a recorder, so each test sees exactly the keyword
arguments a login would carry - the telemetry switches on every one of them,
and for key-pair the key bytes with no password beside them. The RSA keys are
throwaway pairs generated into the test's temp dir.

The live key-pair proof is `test_snowflake_keypair.py`, gated on
PARITY_TEST_SNOWFLAKE_KEYPAIR.
"""

from __future__ import annotations

import io
import os
from typing import Any

import pytest

from parity.dialects.base import get_dialect
from parity.dialects.snowflake_dialect import (
    KEY_PASSPHRASE_ENV,
    URL_PARAMS,
    SnowflakeURL,
    connect_kwargs,
    load_private_key,
    parse_snowflake_url,
)

SECRET = "Zq9Secret"
PASSPHRASE = "Pz7Passphrase"


@pytest.fixture(autouse=True)
def _no_passphrase_from_the_environment(monkeypatch):
    """A passphrase in the developer's own environment must not leak in."""
    for side in ("", "_A", "_B"):
        monkeypatch.delenv(KEY_PASSPHRASE_ENV + side, raising=False)


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def keys(tmp_path_factory) -> dict[str, Any]:
    """Throwaway keys: RSA unencrypted and encrypted PKCS#8, an encrypted
    traditional (Proc-Type) PEM, an EC key, and a file that is no key."""
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    d = tmp_path_factory.mktemp("sfkeys")
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pkcs8 = serialization.PrivateFormat.PKCS8
    pem = serialization.Encoding.PEM

    def write(name: str, data: bytes) -> str:
        """Write one key file and return its path."""
        path = d / name
        path.write_bytes(data)
        return str(path)

    return {
        "key": rsa_key,
        "der": rsa_key.private_bytes(
            serialization.Encoding.DER, pkcs8, serialization.NoEncryption()
        ),
        "plain": write(
            "plain.p8", rsa_key.private_bytes(pem, pkcs8, serialization.NoEncryption())
        ),
        "encrypted": write(
            "encrypted.p8",
            rsa_key.private_bytes(
                pem, pkcs8, serialization.BestAvailableEncryption(PASSPHRASE.encode())
            ),
        ),
        "traditional": write(
            "traditional.pem",
            rsa_key.private_bytes(
                pem,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.BestAvailableEncryption(PASSPHRASE.encode()),
            ),
        ),
        "ec": write(
            "ec.p8",
            ec.generate_private_key(ec.SECP256R1()).private_bytes(
                pem, pkcs8, serialization.NoEncryption()
            ),
        ),
        "garbage": write("garbage.p8", b"not a key at all\n"),
        "damaged": write(
            "damaged.p8",
            b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
        ),
        "dir": str(d),
    }


class _Cursor:
    """Records what the dialect runs after login."""

    def __init__(self, log: list[str]):
        """Append every statement to `log`."""
        self.log = log

    def execute(self, sql: str) -> None:
        """Record one statement."""
        self.log.append(sql)

    def close(self) -> None:
        """Nothing to close."""


class _Conn:
    """A connection that accepts the session pins and nothing else."""

    def __init__(self):
        """No statements yet; telemetry on until the dialect turns it off."""
        self.statements: list[str] = []
        self.telemetry_enabled = True

    def cursor(self) -> _Cursor:
        """A recording cursor."""
        return _Cursor(self.statements)

    def close(self) -> None:
        """Nothing to close."""


@pytest.fixture
def driver(monkeypatch):
    """Replace `snowflake.connector.connect` with a recorder.

    `seen["kwargs"]` holds what a login would carry; `seen["fail"]`, when set,
    is raised instead of returning a connection.
    """
    sc = pytest.importorskip("snowflake.connector")
    seen: dict[str, Any] = {"fail": None}

    def fake_connect(**kwargs):
        """Record the login's arguments; never open a socket."""
        seen["kwargs"] = kwargs
        seen["env_platform"] = os.environ.get("SNOWFLAKE_DISABLE_PLATFORM_DETECTION")
        if seen["fail"] is not None:
            raise seen["fail"]
        seen["conn"] = _Conn()
        return seen["conn"]

    monkeypatch.setattr(sc, "connect", fake_connect)
    monkeypatch.delenv("SNOWFLAKE_DISABLE_PLATFORM_DETECTION", raising=False)
    return seen


def _url(path: str, query: str = "") -> str:
    """A key-pair URL for `path` (forward slashes, as a user would type it)."""
    q = f"private_key_file={path.replace(os.sep, '/')}"
    return f"snowflake://PARITY@myacct/DB/SCH?warehouse=WH&{q}{query}"


def _assert_telemetry_off(seen: dict[str, Any]) -> None:
    """CLAUDE.md 4.11: every login carries the telemetry switches."""
    kw = seen["kwargs"]
    assert kw["session_parameters"] == {"CLIENT_TELEMETRY_ENABLED": False}
    assert kw["log_imported_packages_in_telemetry"] is False
    assert seen["env_platform"] == "true"
    assert seen["conn"].telemetry_enabled is False
    assert seen["conn"].statements == [
        "alter session set timezone = 'UTC'",
        "alter session set rows_per_resultset = 0",
        "alter session set quoted_identifiers_ignore_case = false",
        "alter session set strict_json_output = false",
    ]


# --------------------------------------------------------- what is passed


def test_a_password_url_passes_the_password_and_no_key(driver):
    """Password sign-in is unchanged: user, password, no authenticator."""
    d = get_dialect(
        f"snowflake://PARITY:{SECRET}@myacct/DB/SCH?warehouse=WH&role=RO", side="A"
    )
    kw = driver["kwargs"]
    assert kw["password"] == SECRET
    assert "private_key" not in kw and "authenticator" not in kw
    assert (kw["account"], kw["user"], kw["database"], kw["schema"]) == (
        "myacct",
        "PARITY",
        "DB",
        "SCH",
    )
    assert (kw["warehouse"], kw["role"], kw["autocommit"]) == ("WH", "RO", True)
    assert d.default_schema == "SCH"
    _assert_telemetry_off(driver)


def test_an_encrypted_key_signs_in_by_key_pair_with_no_password(
    driver, keys, monkeypatch
):
    """Key file + passphrase from the environment: the connector gets the
    decrypted key as DER bytes, authenticator SNOWFLAKE_JWT, and no password
    argument at all - nothing can fall back to password sign-in."""
    monkeypatch.setenv(KEY_PASSPHRASE_ENV, PASSPHRASE)
    get_dialect(_url(keys["encrypted"]), side="A")
    kw = driver["kwargs"]
    assert kw["authenticator"] == "SNOWFLAKE_JWT"
    assert kw["private_key"] == keys["der"]
    assert "password" not in kw
    assert not any(PASSPHRASE in repr(v) for v in kw.values())
    _assert_telemetry_off(driver)


@pytest.mark.parametrize("which", ["plain", "traditional"])
def test_unencrypted_and_traditional_pem_keys_load_too(driver, keys, monkeypatch, which):
    """An unencrypted PKCS#8 key needs no passphrase; an encrypted
    `BEGIN RSA PRIVATE KEY` (Proc-Type) file is read with one."""
    if which == "traditional":
        monkeypatch.setenv(KEY_PASSPHRASE_ENV, PASSPHRASE)
    get_dialect(_url(keys[which]), side="A")
    assert driver["kwargs"]["private_key"] == keys["der"]
    assert "password" not in driver["kwargs"]


def test_the_per_side_passphrase_wins(driver, keys, monkeypatch):
    """`<name>_B` is read for side B before the shared variable."""
    monkeypatch.setenv(KEY_PASSPHRASE_ENV, "wrong-one")
    monkeypatch.setenv(KEY_PASSPHRASE_ENV + "_B", PASSPHRASE)
    get_dialect(_url(keys["encrypted"]), side="B")
    assert driver["kwargs"]["private_key"] == keys["der"]


def test_the_connector_accepts_the_key_bytes_and_signs_a_valid_jwt(keys):
    """Not just "some bytes were passed": the connector's own key-pair
    authenticator loads them and signs a JWT the public key verifies."""
    pytest.importorskip("snowflake.connector")
    jwt = pytest.importorskip("jwt")
    from snowflake.connector.auth import AuthByKeyPair

    auth = AuthByKeyPair(private_key=load_private_key(keys["plain"], None))
    token = auth.prepare(account="myacct", user="PARITY")
    claims = jwt.decode(token, keys["key"].public_key(), algorithms=["RS256"])
    assert claims["sub"] == "MYACCT.PARITY"


def test_a_tilde_path_is_expanded():
    """`~/.parity-test/key.p8` means the home directory, as in a shell."""
    url = parse_snowflake_url("snowflake://u@acct/db?private_key_file=~/k.p8")
    assert url.private_key_file == os.path.expanduser("~/k.p8")


def test_a_plus_in_a_key_path_is_kept():
    """parse_qs would read `+` as a space and look for a different file."""
    url = parse_snowflake_url("snowflake://u@acct/db?private_key_file=/k/a+b.p8")
    assert url.private_key_file == "/k/a+b.p8"


# -------------------------------------------------------- key error paths


@pytest.mark.parametrize(
    ("which", "passphrase", "expect"),
    [
        ("missing", None, "private key file not found: "),
        ("encrypted", None, f"is encrypted; set {KEY_PASSPHRASE_ENV}"),
        ("encrypted", "not-the-passphrase", "wrong passphrase"),
        ("traditional", "not-the-passphrase", "wrong passphrase"),
        ("plain", PASSPHRASE, f"is not encrypted, but {KEY_PASSPHRASE_ENV} is set"),
        ("garbage", None, "is not a PEM private key"),
        ("damaged", None, "damaged or not a private key"),
        ("ec", None, "takes an RSA key"),
        ("dir", None, "could not be read"),
    ],
)
def test_a_key_that_cannot_be_used_is_refused_naming_side_and_path(
    driver, keys, monkeypatch, which, passphrase, expect
):
    """Each failure is one message naming the side, the key path and what to
    do - never the key, never the passphrase - and no login is attempted."""
    path = keys.get(which) or os.path.join(keys["dir"], "nope.p8")
    if passphrase is not None:
        monkeypatch.setenv(KEY_PASSPHRASE_ENV, passphrase)
    with pytest.raises(ValueError) as caught:
        get_dialect(_url(path), side="B")
    msg = str(caught.value)
    assert msg.startswith("[side B: snowflake] "), msg
    assert expect in msg, msg
    assert path.replace(os.sep, "/") in msg.replace(os.sep, "/"), msg
    assert caught.value.__cause__ is None
    assert "kwargs" not in driver, "a login was attempted with an unusable key"
    for secret in (PASSPHRASE, "not-the-passphrase", "MII"):
        assert secret not in msg, msg


def test_a_url_with_both_a_password_and_a_key_is_refused(driver, keys):
    """Which would the connector use? Neither is guessed."""
    with pytest.raises(ValueError, match="both a password and private_key_file") as c:
        get_dialect(_url(keys["plain"]).replace("PARITY@", f"PARITY:{SECRET}@"), side="A")
    assert SECRET not in str(c.value)


def test_a_url_with_neither_a_password_nor_a_key_is_refused(driver):
    """Today's driver would fail later with a less useful message."""
    with pytest.raises(ValueError, match="no password and no private_key_file"):
        get_dialect("snowflake://PARITY@myacct/DB", side="A")
    assert "kwargs" not in driver


# ------------------------------------------------------ URL parameters (2)


@pytest.mark.parametrize(
    ("query", "expect"),
    [
        (
            "authenticator=externalbrowser",
            "unknown snowflake connection parameter 'authenticator'",
        ),
        ("warehous=WH", "unknown snowflake connection parameter 'warehous'"),
        (f"token={SECRET}", "unknown snowflake connection parameter 'token'"),
        ("warehouse=WH&warehouse=WH2", "'warehouse' is given twice"),
        ("role=", "'role' is empty"),
        (f"password={SECRET}", "the password goes in the userinfo"),
    ],
)
def test_unknown_repeated_and_empty_parameters_are_refused(driver, query, expect):
    """Refused, never ignored, naming the side and the parameter and listing
    what is accepted - and never echoing a value or the password."""
    with pytest.raises(ValueError) as caught:
        get_dialect(f"snowflake://PARITY:{SECRET}@myacct/DB/SCH?{query}", side="A")
    msg = str(caught.value)
    assert msg.startswith("[side A: snowflake] ") and expect in msg, msg
    if "unknown" in expect:
        assert "Supported: " + ", ".join(URL_PARAMS) in msg
    assert SECRET not in msg, msg
    assert "kwargs" not in driver


def test_every_accepted_parameter_reaches_the_driver(driver):
    """The accepted list is exactly what is honoured."""
    get_dialect(
        f"snowflake://PARITY:{SECRET}@myacct/DB/SCH?role=R%20O&warehouse=W", side="A"
    )
    assert (driver["kwargs"]["role"], driver["kwargs"]["warehouse"]) == ("R O", "W")
    assert set(URL_PARAMS) == {"private_key_file", "role", "warehouse"}


# ------------------------------------------------- password redaction (5)


@pytest.mark.parametrize(
    "conn",
    [
        # An unencoded `/`, `#` or `?` ends urlparse's authority early, so the
        # rest of the password would become the database name.
        f"snowflake://PARITY:{SECRET}/x@myacct/DB",
        f"snowflake://PARITY:{SECRET}#x@myacct/DB",
        f"snowflake://PARITY:{SECRET}?x@myacct/DB",
        f"snowflake://PARITY:a/{SECRET}@myacct/DB",
        f"snowflake://PARITY:{SECRET}@[myacct/DB",
        f"snowflake://PARITY:{SECRET}@/DB",
        f"snowflake://:{SECRET}@myacct/DB",
        f"snowflake://PARITY:{SECRET}@myacct/DB/SCH/extra",
        f"snowflake://PARITY:{SECRET}@myacct/DB?bogus={SECRET}",
    ],
)
def test_bad_urls_name_the_side_and_never_the_password(driver, conn):
    """Offline: each fails before the driver is called, naming side A."""
    with pytest.raises(ValueError) as caught:
        get_dialect(conn, side="A")
    msg = str(caught.value)
    assert msg.startswith("[side A: snowflake] "), msg
    assert SECRET not in msg, msg
    assert caught.value.__cause__ is None or SECRET not in str(caught.value.__cause__)
    assert "kwargs" not in driver


def test_a_connection_failure_never_prints_the_password_or_passphrase(
    driver, keys, monkeypatch
):
    """Even a driver message that echoed a secret is printed without it, and
    the chained driver exception (same text) is dropped."""
    driver["fail"] = RuntimeError(f"250001: login failed for password {SECRET}")
    with pytest.raises(ValueError) as caught:
        get_dialect(f"snowflake://PARITY:{SECRET}@myacct/DB", side="A")
    msg = str(caught.value)
    assert msg.startswith("[side A: snowflake] could not connect: 250001"), msg
    assert SECRET not in msg and "***" in msg
    assert caught.value.__cause__ is None

    monkeypatch.setenv(KEY_PASSPHRASE_ENV, PASSPHRASE)
    driver["fail"] = RuntimeError(f"JWT rejected ({PASSPHRASE})")
    with pytest.raises(ValueError) as caught:
        get_dialect(_url(keys["encrypted"]), side="B")
    assert PASSPHRASE not in str(caught.value) and "***" in str(caught.value)

    # A plain failure keeps its driver exception for debugging.
    plain = RuntimeError("250001: Could not connect to Snowflake backend")
    driver["fail"] = plain
    with pytest.raises(ValueError) as caught:
        get_dialect(f"snowflake://PARITY:{SECRET}@myacct/DB", side="A")
    assert caught.value.__cause__ is plain


def test_parsed_objects_never_show_the_password(keys):
    """repr of the parsed URL - what a debugger or a log line prints - and
    the driver arguments for a key-pair login carry no secret."""
    url = parse_snowflake_url(f"snowflake://PARITY:{SECRET}@myacct/DB/SCH?role=R")
    assert url.password == SECRET
    assert SECRET not in repr(url) and SECRET not in str(url)
    key_url = SnowflakeURL(account="a", user="u", private_key_file=keys["plain"])
    kw = connect_kwargs(key_url, load_private_key(keys["plain"], None))
    assert "password" not in kw


def test_the_cli_prints_neither_the_password_nor_the_url(driver):
    """Through `parity diff`: exit 2, side named, no secret on either stream."""
    from parity.cli import EXIT_ERROR, main

    driver["fail"] = RuntimeError(f"incorrect username or password {SECRET}")
    for conn in (
        f"snowflake://PARITY:{SECRET}@myacct/DB?bogus=1",
        f"snowflake://PARITY:{SECRET}/x@myacct/DB",
        f"snowflake://PARITY:{SECRET}@myacct/DB",
    ):
        out, err = io.StringIO(), io.StringIO()
        code = main(
            [
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
            ],
            out=out,
            err=err,
        )
        text = out.getvalue() + err.getvalue()
        assert code == EXIT_ERROR, text
        assert "side A" in text and SECRET not in text, text
