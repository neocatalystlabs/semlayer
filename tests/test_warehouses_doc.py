"""docs/warehouses.md must match what the registries and gates actually say.

The page is rendered, not written: it pulls the synthetic warehouses from
fixtures/registry.yaml, the acquired datasets from
fixtures/external/registry.yaml, shape from the built databases, and the
accuracy floors straight out of the test modules. A doc that restates numbers
by hand goes stale silently -- this test is what makes "generated" true rather
than aspirational.

If this fails, run `python fixtures/render_warehouses.py` and commit the result.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

OSS = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not (OSS / "fixtures" / "build").exists(),
                    reason="fixtures not built: python fixtures/build.py")
def test_warehouses_doc_is_current():
    r = subprocess.run(
        [sys.executable, "fixtures/render_warehouses.py", "--check"],
        cwd=OSS, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_built_fixture_is_in_the_registry():
    """A warehouse that exists but is undocumented is the gap this page closes."""
    import yaml
    reg = yaml.safe_load((OSS / "fixtures" / "registry.yaml").read_text())
    named = {e["name"] for e in reg["synthetic"]}
    built = {p.stem for p in (OSS / "fixtures" / "build").glob("*.duckdb")}
    if not built:
        pytest.skip("fixtures not built")
    missing = built - named
    assert not missing, f"built but absent from fixtures/registry.yaml: {sorted(missing)}"
