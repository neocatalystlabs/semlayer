"""Credential paths a user can actually use to run as themselves.

Password-only Snowflake and key-file-only BigQuery locked out the person the
free CLI is aimed at: an analyst whose Snowflake access is SSO, or a GCP user
who has `gcloud auth application-default login` but will not be issued a
service-account key. Both are the common enterprise case, and neither could run
`semlayer infer` at all.

These tests exercise the credential SELECTION, not a live connection -- the
driver is stubbed, so they run anywhere.
"""

from __future__ import annotations

import sys
import types

import pytest


def _stub_snowflake(monkeypatch, captured: dict):
    mod = types.ModuleType("snowflake")
    conn = types.ModuleType("snowflake.connector")

    def connect(**kw):
        captured.update(kw)
        return types.SimpleNamespace(cursor=lambda: None, close=lambda: None)

    conn.connect = connect
    mod.connector = conn
    monkeypatch.setitem(sys.modules, "snowflake", mod)
    monkeypatch.setitem(sys.modules, "snowflake.connector", conn)


def _base_env(monkeypatch):
    for k in ("SNOWFLAKE_READER_PASSWORD", "SNOWFLAKE_READER_PRIVATE_KEY_PATH",
              "SNOWFLAKE_READER_AUTHENTICATOR", "SNOWFLAKE_AUTHENTICATOR",
              "SNOWFLAKE_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct")
    monkeypatch.setenv("SNOWFLAKE_READER_USER", "me@example.com")
    monkeypatch.setenv("SNOWFLAKE_READER_ROLE", "analyst")


def test_snowflake_sso_needs_no_password(monkeypatch):
    """An SSO account has no password; externalbrowser must be enough."""
    captured: dict = {}
    _stub_snowflake(monkeypatch, captured)
    _base_env(monkeypatch)
    monkeypatch.setenv("SNOWFLAKE_READER_AUTHENTICATOR", "externalbrowser")
    from semlayer.connectors.snowflake import SnowflakeSource
    SnowflakeSource.from_env(persona="reader")
    assert captured["authenticator"] == "externalbrowser"
    assert "password" not in captured, "externalbrowser must not send a password"
    assert captured["role"] == "analyst"


def test_snowflake_key_pair_is_accepted(monkeypatch):
    captured: dict = {}
    _stub_snowflake(monkeypatch, captured)
    _base_env(monkeypatch)
    monkeypatch.setenv("SNOWFLAKE_READER_PRIVATE_KEY_PATH", "/keys/rsa.p8")
    from semlayer.connectors.snowflake import SnowflakeSource
    SnowflakeSource.from_env(persona="reader")
    assert captured["private_key_file"] == "/keys/rsa.p8"
    assert "password" not in captured


def test_snowflake_password_still_works(monkeypatch):
    captured: dict = {}
    _stub_snowflake(monkeypatch, captured)
    _base_env(monkeypatch)
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")
    from semlayer.connectors.snowflake import SnowflakeSource
    SnowflakeSource.from_env(persona="reader")
    assert captured["password"] == "pw"


def test_snowflake_no_credential_says_what_to_set(monkeypatch):
    _stub_snowflake(monkeypatch, {})
    _base_env(monkeypatch)
    from semlayer.connectors.snowflake import SnowflakeSource
    from semlayer.errors import SemlayerError
    with pytest.raises(SemlayerError, match="externalbrowser"):
        SnowflakeSource.from_env(persona="reader")


def test_bigquery_falls_back_to_application_default_credentials(monkeypatch):
    """No key file must mean ADC, not a hard failure."""
    captured: dict = {}

    gc = types.ModuleType("google.cloud")
    bq = types.ModuleType("google.cloud.bigquery")

    def Client(project=None, credentials=None):   # capitalised to mirror the SDK
        captured["project"] = project
        captured["credentials"] = credentials
        # `list_datasets` because opening a BigQuery source now proves it can
        # reach the project: the real client is lazy, so without one cheap
        # call a wrong project id was only discovered deep inside list_tables.
        return types.SimpleNamespace(list_datasets=lambda max_results=None: iter(()),
                                     close=lambda: None)

    bq.Client = Client
    gc.bigquery = bq
    monkeypatch.setitem(sys.modules, "google.cloud", gc)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bq)
    # Patch the real google.auth.default rather than stubbing the module:
    # `google.auth.default(...)` resolves `auth` as an ATTRIBUTE of the google
    # package, so a sys.modules entry is never consulted and the real ADC lookup
    # runs (and fails) instead.
    google_auth = pytest.importorskip("google.auth")
    monkeypatch.setattr(google_auth, "default",
                        lambda scopes=None: ("ADC-CREDS", "adc-project"))

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_READER", raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    from semlayer.connectors.bigquery import BigQuerySource
    BigQuerySource.from_env(persona="reader")
    assert captured["credentials"] == "ADC-CREDS"
    assert captured["project"] == "adc-project", "ADC's project should be adopted"
