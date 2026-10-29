# SPDX-License-Identifier: FSL-1.1-ALv2
"""The scale generator's --schemas layout, and stitch across it.

One schema per domain, conformed dimensions in `platform`, a shadow copy
in some domain: the multi-repo estate. Each schema is inferred on its own,
stitched, and the seam scored against the gold edges that cross schemas.
The flat layout must be byte-identical to before: the schemas axis is
recorded in the sidecar, not a data change.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import yaml

OSS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OSS / "src"))
sys.path.insert(0, str(OSS / "fixtures" / "generators"))

from scale import PLATFORM, SEED, build  # noqa: E402

from semlayer.pipeline import InferOptions, infer  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402
from semlayer.stitch import stitch  # noqa: E402
from semlayer.validate import validate_file  # noqa: E402

N = 200


def test_schemas_layout_shape():
    con = duckdb.connect()
    try:
        meta = build(con, N, SEED, schemas=True)
        schemas = con.execute("select distinct table_schema from information_schema.tables "
                              "where table_schema not in ('information_schema','pg_catalog')"
                              ).fetchall()
    finally:
        con.close()
    assert meta["layout"] == "schemas"
    present = {s for (s,) in schemas}
    assert PLATFORM in present and len(present) >= 3
    assert set(meta["schemas"].values()) <= present
    assert meta["cross_schema_fks"] > 0
    for c, _, p, _ in meta["gold_fks"]:
        assert c in meta["tables"] and p in meta["tables"], (c, p)
    # a shadow is a core table named dim_<entity> outside platform
    for sh in meta["shadows"]:
        assert meta["tables"][sh]["kind"] == "shadow" and not sh.startswith(PLATFORM + ".")


def test_flat_layout_is_unchanged_by_the_schemas_axis():
    con = duckdb.connect()
    try:
        meta = build(con, N, SEED)
    finally:
        con.close()
    assert meta["layout"] == "flat" and "schemas" not in meta
    assert all("." not in c and "." not in p for c, _, p, _ in meta["gold_fks"])


def test_stitch_recovers_the_cross_schema_keys(tmp_path):
    db = tmp_path / "scale.duckdb"
    con = duckdb.connect(str(db))
    try:
        meta = build(con, N, SEED, schemas=True)
    finally:
        con.close()
    schemas = sorted(set(meta["schemas"].values()))
    members = []
    con = duckdb.connect(str(db), read_only=True)
    try:
        for s in schemas:
            doc = infer(DuckDBSource(con), options=InferOptions(schema=s, query_workers=1))
            path = tmp_path / f"{s}.yaml"
            path.write_text(yaml.safe_dump(doc, sort_keys=False))
            members.append(str(path))
        result = stitch(members, "acme", tmp_path / "acme.yaml", source=DuckDBSource(con),
                        query_workers=1)
    finally:
        con.close()
    (tmp_path / "acme.yaml").write_text(yaml.safe_dump(result.hub, sort_keys=False))
    assert validate_file(tmp_path / "acme.yaml").ok
    # score the seam: predicted cross-member edges vs gold edges that cross schemas
    source_of = {}
    for m in members:
        d = yaml.safe_load(Path(m).read_text())["semantic_layer"]
        for t in d["tables"]:
            source_of[f"{d['name']}/{t['name']}"] = t["source"].lower()
    gold = {(c.lower(), cc.lower(), p.lower(), pc.lower()) for c, cc, p, pc in meta["gold_fks"]
            if c.split(".")[0] != p.split(".")[0]}
    pred = {(source_of[e["from"]["table"]], e["from"]["columns"][0].lower(),
             source_of[e["to"]["table"]], e["to"]["columns"][0].lower())
            for e in result.hub["semantic_layer"].get("relationships", [])}
    # dedup folds some copies, so score over the gold whose ends are in the layer
    gold = {g for g in gold if g[0] in source_of.values() and g[2] in source_of.values()}
    hit = pred & gold
    assert gold, "the layout should produce cross-schema keys"
    assert len(hit) / len(gold) >= 0.95, (len(hit), len(gold), sorted(gold - pred)[:5])
    assert len(hit) / max(1, len(pred)) >= 0.9, (len(hit), len(pred), sorted(pred - gold)[:5])
    # nothing listed as unresolved is an attribute column
    for u in result.hub["semantic_layer"].get("unresolved", []):
        assert u["column"].endswith(("_id", "_key", "_cd", "_code", "_sk")) or "id" in u["column"], u
