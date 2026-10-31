"""The response cache is a cache: it belongs in a cache directory, and it is
switchable.

Two defects this pins. The fallback location used to be the source tree's
parent, which in a pip install is the parent of site-packages -- so a
`semlayer infer` against a customer warehouse wrote the model's text about
their schema into their Python installation and left it there, unmentioned by
any doc. And the CLI used the library default, which prefers a checkout's
`cassettes/`: those files are tracked and not git-ignored, so a clone of this
repo plus one `infer` plus `git add -A` commits descriptions of the user's
warehouse.

What the cache buys, and why it is not simply removed: an interrupted
whole-warehouse run resumes for nothing, because the same data profiles to the
same prompts. Nothing within ONE run is deduplicated -- measured on messy_mart,
98 calls and 98 distinct prompts -- so that resume is the whole of it.
"""

import json
from pathlib import Path

import pytest

from semlayer.llm.provider import (
    CACHE_ENV,
    CASSETTE_DIR,
    AnthropicProvider,
    CassetteMiss,
    resolve_cache,
    user_cache_dir,
)

REPO = Path(__file__).resolve().parent.parent


class _FakeMsg:
    def __init__(self, text="ok"):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = "end_turn"
        self.usage = type("U", (), {"input_tokens": 10, "output_tokens": 20})()


def _live(**kw):
    """A provider that answers without the network, counting its calls."""
    p = AnthropicProvider(allow_live=True, **kw)
    calls: list[dict] = []

    class _Messages:
        def create(self, **k):
            calls.append(k)
            return _FakeMsg()

    p._client = type("C", (), {"messages": _Messages()})()
    return p, calls


def test_cache_outside_a_checkout_lands_in_the_platform_cache_dir(monkeypatch):
    """Never site-packages, never the Python installation."""
    monkeypatch.delenv(CACHE_ENV, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-example")
    monkeypatch.setattr("os.name", "posix")
    monkeypatch.setattr("sys.platform", "linux")
    assert user_cache_dir() == Path("/tmp/xdg-example/semlayer/cassettes")
    assert resolve_cache(repo_ok=False) == user_cache_dir()


def test_the_library_default_still_replays_the_committed_cassettes(monkeypatch):
    """The reproducibility path must not move: pytest and `python -m
    semlayer.benchmark` both rely on finding the repo's own recordings."""
    monkeypatch.delenv(CACHE_ENV, raising=False)
    assert resolve_cache(repo_ok=True) == CASSETTE_DIR
    assert CASSETTE_DIR == REPO / "cassettes"
    assert AnthropicProvider().cassette_dir == CASSETTE_DIR


def test_a_cli_style_provider_never_writes_into_the_repo(monkeypatch, tmp_path):
    """`repo_cache=False` is what the CLI passes; the git trap is the point."""
    monkeypatch.delenv(CACHE_ENV, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    p, _ = _live(repo_cache=False)
    assert p.cassette_dir is not None
    assert REPO not in p.cassette_dir.parents
    p.complete("sys", "a cli-style prompt")
    assert len(list(p.cassette_dir.glob("*.json"))) == 1


def test_env_off_disables_both_recording_and_replay(monkeypatch, tmp_path):
    monkeypatch.setenv(CACHE_ENV, "off")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    p, calls = _live()
    assert p.cassette_dir is None
    p.complete("sys", "same prompt")
    p.complete("sys", "same prompt")
    assert len(calls) == 2, "replay must not happen with the cache off"
    assert list(tmp_path.rglob("*.json")) == [], "nothing may be written"


def test_cache_false_disables_it_whatever_the_env_says(monkeypatch, tmp_path):
    """`--no-llm-cache` must win over a SEMLAYER_LLM_CACHE path."""
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    p, calls = _live(cache=False)
    assert p.cassette_dir is None
    p.complete("sys", "x")
    p.complete("sys", "x")
    assert len(calls) == 2
    assert list(tmp_path.rglob("*.json")) == []


def test_env_path_moves_the_cache_and_it_still_replays(monkeypatch, tmp_path):
    monkeypatch.setenv(CACHE_ENV, str(tmp_path / "nested" / "dir"))
    p, calls = _live()
    assert p.cassette_dir == tmp_path / "nested" / "dir"
    p.complete("sys", "prompt")
    p.complete("sys", "prompt")
    assert len(calls) == 1, "the second call must come from the cache"
    assert p.calls_cached == 1
    recorded = list(p.cassette_dir.glob("*.json"))
    assert len(recorded) == 1
    assert json.loads(recorded[0].read_text())["response"] == "ok"


def test_keyless_with_the_cache_off_fails_loudly(monkeypatch):
    """Silence here would mean a run that quietly produced no descriptions."""
    monkeypatch.setenv(CACHE_ENV, "off")
    p = AnthropicProvider(allow_live=False)
    with pytest.raises(CassetteMiss) as e:
        p.complete("sys", "user")
    assert "no cassette" in str(e.value)


def test_both_cli_entry_points_opt_out_of_the_repo_cache():
    """A new `AnthropicProvider()` in the CLI would reintroduce the git trap,
    and no runtime test would notice: the repo cache works, that is the bug."""
    src = (REPO / "src" / "semlayer" / "cli.py").read_text()
    assert src.count("AnthropicProvider(") == 2, "a construction site moved; check both"
    assert src.count("AnthropicProvider(cache=not args.no_llm_cache, repo_cache=False)") == 2
