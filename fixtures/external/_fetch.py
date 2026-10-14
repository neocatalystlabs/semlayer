"""Shared plumbing for dataset fetch scripts.

Contract every fetch script obeys:

- Idempotent. A second run re-downloads nothing and cannot turn a half-finished
  download into an apparent success.
- Checksum-verified, failing loudly on mismatch.
- Writes only under `fixtures/external/data/<name>/`, which is gitignored.
- Prints the licence and the track on every run, because someone will forget
  which track a dataset is in and publish a number they should not.

Downloaded archives are untrusted input. Extraction refuses absolute paths and
parent-directory traversal, and nothing is ever executed from inside the
download directory.
"""

from __future__ import annotations

import hashlib
import pathlib
import shutil
import sys
import tarfile
import urllib.request
import zipfile

DATA = pathlib.Path(__file__).resolve().parent / "data"
_CHUNK = 1 << 20


def banner(name: str, track: str, licence: str, source: str) -> pathlib.Path:
    """Announce what this is and which track it belongs to. Returns its dir."""
    rule = "PUBLISHABLE — gold may ship, numbers may be published" if track == "P" else \
           "INTERNAL ONLY — no number from this dataset may ever be published"
    print(f"{name}\n  licence : {licence}\n  source  : {source}\n"
          f"  track {track} : {rule}\n")
    d = DATA / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def download(url: str, dest: pathlib.Path, sha256: str | None = None) -> pathlib.Path:
    """Fetch `url` to `dest` unless already present and matching."""
    if dest.exists() and (sha256 is None or _sha256(dest) == sha256):
        print(f"  have {dest.name}")
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  downloading {url}")
    with urllib.request.urlopen(url) as r, tmp.open("wb") as f:
        shutil.copyfileobj(r, f, _CHUNK)
    got = _sha256(tmp)
    if sha256 and got != sha256:
        tmp.unlink()
        raise SystemExit(f"checksum mismatch for {url}\n  expected {sha256}\n  got      {got}")
    # rename only after verification, so a failed run never looks complete
    tmp.rename(dest)
    if not sha256:
        print(f"  sha256 {got}   <- record this in registry.yaml")
    return dest


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def extract(archive: pathlib.Path, into: pathlib.Path) -> pathlib.Path:
    """Unpack an untrusted archive, refusing any member that escapes `into`."""
    into.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            _check(names, into)
            z.extractall(into)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            _check([m.name for m in t.getmembers()], into)
            t.extractall(into, filter="data")
    else:
        raise SystemExit(f"not an archive this script understands: {archive}")
    print(f"  extracted {len(list(into.rglob('*')))} entries into {into.name}/")
    return into


def _check(names: list[str], into: pathlib.Path) -> None:
    root = into.resolve()
    for n in names:
        if n.startswith("/") or ".." in pathlib.PurePosixPath(n).parts:
            raise SystemExit(f"refusing archive member outside the target dir: {n!r}")
        if not (root / n).resolve().is_relative_to(root):
            raise SystemExit(f"refusing archive member outside the target dir: {n!r}")


def require_duckdb():
    try:
        import duckdb
    except ImportError:
        raise SystemExit("pip install duckdb") from None
    return duckdb


def summarise(db: pathlib.Path) -> None:
    """Print what we ended up with, so the registry entry can be filled in."""
    duckdb = require_duckdb()
    con = duckdb.connect(str(db), read_only=True)
    try:
        tables = [r[0] for r in con.sql(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY 1").fetchall()]
        cols = con.sql("SELECT count(*) FROM information_schema.columns "
                       "WHERE table_schema='main'").fetchone()[0]
        rows = sum(con.sql(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables)
    finally:
        con.close()
    print(f"\n  {db.name}: {len(tables)} tables, {cols} columns, {rows:,} rows")
    print(f"  registry: tables: {len(tables)}  columns: {cols}  rows: {rows}")


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0)
