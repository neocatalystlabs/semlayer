"""API failures surface as LLMError with a next step, never as a raw SDK
traceback -- and a rate limit inside a worker pool names the flag that fixes it."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

anthropic = pytest.importorskip("anthropic")

from semlayer.errors import LLMError  # noqa: E402
from semlayer.llm import provider as prov  # noqa: E402
from semlayer.llm.provider import AnthropicProvider  # noqa: E402


def _status_error(cls, status: int):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req, json={"error": {"message": "x"}})
    return cls("x", response=resp, body=None)


class _Messages:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        raise self.exc


class _Client:
    def __init__(self, exc):
        self.messages = _Messages(exc)


def _provider(tmp_path: Path, exc) -> AnthropicProvider:
    p = AnthropicProvider(cassette_dir=tmp_path, allow_live=True)
    p._client = _Client(exc)
    return p


def test_rate_limit_names_the_flag(tmp_path):
    p = _provider(tmp_path, _status_error(anthropic.RateLimitError, 429))
    with pytest.raises(LLMError) as ei:
        p.complete("sys", "user")
    assert "rate limit" in str(ei.value)
    assert "--llm-workers" in ei.value.hint
    assert not list(tmp_path.glob("*.json")), "a failed call must record nothing"
    assert p.calls_live == 0


def test_auth_failure_is_an_llm_error(tmp_path):
    p = _provider(tmp_path, _status_error(anthropic.AuthenticationError, 401))
    with pytest.raises(LLMError, match="API key"):
        p.complete("sys", "user")


def test_server_error_is_an_llm_error(tmp_path):
    p = _provider(tmp_path, _status_error(anthropic.InternalServerError, 500))
    with pytest.raises(LLMError, match="500"):
        p.complete("sys", "user")


def test_connection_error_is_an_llm_error(tmp_path):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    p = _provider(tmp_path, anthropic.APIConnectionError(request=req))
    with pytest.raises(LLMError, match="could not reach"):
        p.complete("sys", "user")


def test_bad_request_still_reports_model_access(tmp_path):
    p = _provider(tmp_path, _status_error(anthropic.BadRequestError, 400))
    with pytest.raises(LLMError, match="rejected the request"):
        p.complete("sys", "user")


def test_live_client_is_built_with_more_retries_than_the_sdk_default(monkeypatch, tmp_path):
    seen = {}

    class _Anthropic:
        def __init__(self, **kw):
            seen.update(kw)
            self.messages = _Messages(_status_error(anthropic.RateLimitError, 429))

    monkeypatch.setattr(anthropic, "Anthropic", _Anthropic)
    p = AnthropicProvider(cassette_dir=tmp_path, allow_live=True)
    with pytest.raises(LLMError):
        p.complete("sys", "user")
    assert seen["max_retries"] == prov._MAX_RETRIES > 2
