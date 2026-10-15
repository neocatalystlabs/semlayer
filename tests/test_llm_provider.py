

# --- truncation is a silent wrong answer ----------------------------------
#
# Measured 2026-10-14: on a chattier model 13.2% of responses hit the
# 1500-token default and link/validate_llm.py dropped EVERY foreign-key verdict
# in those chunks, taking FK precision to 0.000 on three fixtures with no error
# raised anywhere. The provider now escalates on `stop_reason == "max_tokens"`
# and fails loudly if it still does not fit, so a half-written answer can never
# be returned -- or worse, recorded into a cassette that pins it forever.


class _FakeMsg:
    def __init__(self, text, stop_reason):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = stop_reason
        self.usage = type("U", (), {"input_tokens": 10, "output_tokens": 20})()


def _provider_with(monkeypatch, tmp_path, responses):
    """A live-allowed provider whose API returns `responses` in order."""
    from semlayer.llm.provider import AnthropicProvider

    p = AnthropicProvider(cassette_dir=tmp_path, allow_live=True)
    calls: list[int] = []

    class _Messages:
        def create(self, **kw):
            calls.append(kw["max_tokens"])
            return responses[len(calls) - 1]

    p._client = type("C", (), {"messages": _Messages()})()
    return p, calls


def test_truncated_response_escalates_the_budget(monkeypatch, tmp_path):
    """A max_tokens stop must retry with headroom, not return the fragment."""
    p, calls = _provider_with(monkeypatch, tmp_path, [
        _FakeMsg('[{"id": 0, "verdict":', "max_tokens"),   # cut off mid-array
        _FakeMsg('[{"id": 0, "verdict": "plausible"}]', "end_turn"),
    ])
    out = p.complete("sys", "user")
    assert out == '[{"id": 0, "verdict": "plausible"}]'
    assert calls == [1500, 6000], "should retry once at 4x the budget"


def test_persistent_truncation_raises_rather_than_recording_a_fragment(tmp_path):
    """At the ceiling it must fail loudly: a recorded fragment is permanent."""
    import pytest as _pytest

    from semlayer.llm.provider import LLMError

    p, _calls = _provider_with(None, tmp_path, [
        _FakeMsg("fragment", "max_tokens") for _ in range(6)
    ])
    with _pytest.raises(LLMError, match="truncated"):
        p.complete("sys", "user")
    assert not list(tmp_path.glob("*.json")), "must not record a truncated answer"
