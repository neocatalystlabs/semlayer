"""The forms a person can hand `semlayer infer` for BigQuery, and how it fails.

Companion to test_snowflake_connect.py, written after re-reading what that
PR had and had not fixed. BigQuery had the whole signature and none of the
repairs: only the bare word `bigquery` was accepted, the driver was imported
before anything was validated, two of the three credential calls were
unwrapped, and every failure raised a bare `SemlayerError` -- which
`cli._error_class` files as `other`, so a BigQuery first run that could not
authenticate was invisible in the one bucket we read to find out whether
anyone had tried.

The driver is stubbed throughout rather than imported, deliberately. It is
present in some developer venvs and absent in CI, which installs `.[dev]`
without `[warehouses]` -- so a test that depended on either would pass in one
place and fail in the other.
"""

from __future__ import annotations

import sys
import types

import pytest

from semlayer.errors import SourceError
from semlayer.pipeline import open_source_from_uri


class _FakeClient:
    def __init__(self, project=None, credentials=None):
        self.project = project
        self.credentials = credentials

    def close(self):
        pass


def _stub_driver(monkeypatch, *, client=_FakeClient, adc=("ADC_CREDS", "adc-project"),
                 key_file=lambda path: f"KEY({path})"):
    """Install a fake `google.*` tree. Any piece can be made to fail."""
    google = types.ModuleType("google")
    cloud = types.ModuleType("google.cloud")
    bigquery = types.ModuleType("google.cloud.bigquery")
    oauth2 = types.ModuleType("google.oauth2")
    service_account = types.ModuleType("google.oauth2.service_account")
    auth = types.ModuleType("google.auth")

    bigquery.Client = client

    class _Credentials:
        @staticmethod
        def from_service_account_file(path):
            return key_file(path)

    service_account.Credentials = _Credentials

    def default(scopes=None):
        if isinstance(adc, Exception):
            raise adc
        return adc

    auth.default = default
    cloud.bigquery = bigquery
    oauth2.service_account = service_account
    google.cloud = cloud
    google.oauth2 = oauth2
    google.auth = auth
    for name, mod in (("google", google), ("google.cloud", cloud),
                      ("google.cloud.bigquery", bigquery),
                      ("google.oauth2", oauth2),
                      ("google.oauth2.service_account", service_account),
                      ("google.auth", auth)):
        monkeypatch.setitem(sys.modules, name, mod)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("GCP_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS_READER",
              "GOOGLE_APPLICATION_CREDENTIALS_ADMIN",
              "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(k, raising=False)


# --- the spellings that must not be refused -------------------------------

@pytest.mark.parametrize("uri", ["bigquery:my-project", "bigquery://my-project"])
def test_the_project_can_be_named_in_the_source(monkeypatch, uri):
    """`bigquery:my-project` hit the unsupported-URI refusal, whose message
    then listed every connector except the one just asked for.

    Both spellings, because anyone who has just read about `snowflake://`
    will try `bigquery://`.
    """
    _stub_driver(monkeypatch)
    src = open_source_from_uri(uri)
    assert src.project == "my-project"
    assert src.client.project == "my-project"


def test_the_bare_word_still_reads_the_environment(monkeypatch):
    _stub_driver(monkeypatch)
    monkeypatch.setenv("GCP_PROJECT", "from-env")
    assert open_source_from_uri("bigquery").project == "from-env"


def test_a_named_project_wins_over_the_environment(monkeypatch):
    """Otherwise a stale export silently decides which warehouse you profiled."""
    _stub_driver(monkeypatch)
    monkeypatch.setenv("GCP_PROJECT", "from-env")
    assert open_source_from_uri("bigquery:explicit").project == "explicit"


def test_the_unsupported_uri_message_lists_the_project_form(monkeypatch):
    """The message a person reads when they guessed wrong has to name the
    form that works."""
    with pytest.raises(SourceError) as e:
        open_source_from_uri("bigquery-project")
    assert "bigquery:<project>" in e.value.hint


# --- failures that are not our bug ----------------------------------------

def test_a_missing_extra_says_pip_install(monkeypatch):
    """`from google.cloud import bigquery` was the first statement in
    `from_env`, so a missing optional extra reported `No module named
    'google.cloud'` -- an ImportError, which `_error_class` files as
    `internal`: the class that is supposed to mean a defect in semlayer."""
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", None)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_READER", "/dev/null")
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", None)
    with pytest.raises(SourceError) as e:
        open_source_from_uri("bigquery:p")
    assert "driver is not installed" in str(e.value)
    assert "semlayer[warehouses]" in e.value.hint


def test_no_project_anywhere_names_both_ways_to_give_one(monkeypatch):
    _stub_driver(monkeypatch, adc=("CREDS", None))
    with pytest.raises(SourceError) as e:
        open_source_from_uri("bigquery")
    assert "GCP_PROJECT" in str(e.value) and "bigquery:<project>" in str(e.value)


def test_no_credentials_reads_as_a_connection_problem(monkeypatch):
    """A `SourceError`, not a `SemlayerError`. Both reach the user the same
    way, but only this one records as `connection` -- and `connection` is the
    bucket that made the Snowflake problem visible at all."""
    _stub_driver(monkeypatch, adc=RuntimeError("no ADC"))
    with pytest.raises(SourceError) as e:
        open_source_from_uri("bigquery:p")
    assert "no BigQuery credentials" in str(e.value)
    assert "application-default login" in str(e.value)
    assert "init bigquery" in e.value.hint


def test_an_unreadable_key_file_names_the_file_and_the_variable(monkeypatch):
    """`from_service_account_file` was unwrapped, so a path that did not
    exist or a key that had been revoked surfaced as whatever google.oauth2
    raised."""
    def boom(path):
        raise ValueError("not a JSON key file")

    _stub_driver(monkeypatch, key_file=boom)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_READER", "/tmp/nope.json")
    with pytest.raises(SourceError) as e:
        open_source_from_uri("bigquery:p")
    assert "/tmp/nope.json" in str(e.value)
    assert "not a JSON key file" in e.value.hint
    assert "GOOGLE_APPLICATION_CREDENTIALS_READER" in e.value.hint


def test_a_refused_client_names_the_project(monkeypatch):
    """`bigquery.Client()` is where a wrong project id actually surfaces, and
    it was bare."""
    def boom(project=None, credentials=None):
        raise RuntimeError("404 project not found")

    _stub_driver(monkeypatch, client=boom)
    with pytest.raises(SourceError) as e:
        open_source_from_uri("bigquery:ghost-project")
    assert "ghost-project" in str(e.value)
    assert "404 project not found" in e.value.hint


def test_a_key_file_is_preferred_over_adc_and_reaches_the_client(monkeypatch):
    _stub_driver(monkeypatch)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_READER", "/keys/r.json")
    src = open_source_from_uri("bigquery:p")
    assert src.client.credentials == "KEY(/keys/r.json)"


def test_adc_supplies_the_project_when_nothing_else_does(monkeypatch):
    """Running as yourself: `gcloud auth application-default login` and no
    key file, no new service account, no role to create."""
    _stub_driver(monkeypatch, adc=("ADC_CREDS", "project-from-adc"))
    src = open_source_from_uri("bigquery")
    assert src.project == "project-from-adc"
    assert src.client.credentials == "ADC_CREDS"


def test_every_connector_failure_here_records_as_a_connection(monkeypatch):
    """The reason all of these are SourceError rather than SemlayerError.

    `_error_class` maps the exception TYPE, never the message. A bare
    SemlayerError lands in `other`, so BigQuery credential failures were
    pooled with everything unclassified -- and "bigquery attempts: 0" meant
    no bigquery URI was ever seen, not that nobody had failed.
    """
    from semlayer.cli import _error_class

    _stub_driver(monkeypatch, adc=RuntimeError("no ADC"))
    with pytest.raises(SourceError) as e:
        open_source_from_uri("bigquery:p")
    assert _error_class(e.value) == "connection"
