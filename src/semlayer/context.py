"""Knowledge-doc priors (v0.2): free-form partner context as inference input.

`--context` accepts files, directories, and globs of prose docs (.md, .txt,
.rst, .html) — data dictionaries, wiki exports, CLAUDE.md files, hand-crafted
knowledge.md / ETL-generation notes — plus CSV/TSV data dictionaries, which
are mapped structurally instead of chunked. Authed wikis are ingested by
export-then-pass (we deliberately ship no wiki connectors or URL fetch in
the beta); query logs arrive via the summarize-to-doc recipe (docs/).

Docs are PRIORS, NEVER TRUTH: they inform the LLM's evidence and can supply
enum decodes and seed descriptions, but a doc claim that contradicts observed
data lands in the conflicts envelope for review — a stale wiki surfaces as
"your docs say X, your data says Y", never a silent override.

Injection is additive-only: doc excerpts join the LLM evidence as an extra
user-content field, so runs WITHOUT context remain byte-identical to v0.1
(existing cassettes stay valid; no prompt-version bump needed).
"""

from __future__ import annotations

import csv
import glob as globlib
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from semlayer.errors import SpecError

MAX_CHARS_PER_TABLE = 1500  # bounded injection: frugality + signal density
MAX_FILE_BYTES = 2_000_000  # a doc bigger than this is not a doc
PROSE_EXTS = {".md", ".txt", ".rst", ".html", ".htm"}
DICT_EXTS = {".csv", ".tsv"}
SKIP_DIRS = {
    ".git",
    ".hg",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "build",
    "dist",
    "target",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
}

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
# decode claims like "X = Cancelled", "X: Cancelled", "'X' means Cancelled"
_DECODE_RE = re.compile(
    r"['\"`]?([A-Z0-9_]{1,12})['\"`]?\s*(?:=|:|->|—|means)\s*"
    r"['\"`]?([A-Za-z][A-Za-z /_-]{2,40})['\"`]?"
)
# data-dictionary header synonyms (lowercased)
_TABLE_HDRS = {"table", "table_name", "entity", "object", "dataset"}
_COLUMN_HDRS = {"column", "column_name", "field", "field_name", "attribute"}
_DESC_HDRS = {"description", "definition", "comment", "meaning", "business_description", "notes"}


@dataclass
class DocChunk:
    """One heading-delimited section of a context document."""

    source: str  # "file.md#heading"
    text: str
    tokens: set[str]


@dataclass
class DictEntry:
    """One row of a CSV/TSV data dictionary: a doc claim about one column."""

    table: str | None  # None when the dictionary has no table column
    column: str
    description: str
    source: str


@dataclass
class ContextBundle:
    """Everything loaded from --context: prose chunks + structured dictionary rows."""

    chunks: list[DocChunk] = field(default_factory=list)
    dictionary: list[DictEntry] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.chunks or self.dictionary)


def load_context(specs: list[str]) -> ContextBundle:
    """Resolve files/dirs/globs into a ContextBundle; fail fast on unusable specs."""
    bundle = ContextBundle()
    for path in _resolve_paths(specs):
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SpecError(
                f"context file too large (> {MAX_FILE_BYTES // 1_000_000}MB): {path}",
                hint="context docs are prose/dictionaries, not data exports",
            )
        text = _read_text(path)
        if path.suffix.lower() in DICT_EXTS:
            entries = parse_dictionary(
                path.name, text, delimiter="\t" if path.suffix.lower() == ".tsv" else ","
            )
            if entries:
                bundle.dictionary.extend(entries)
            else:  # CSV without dictionary headers: still usable as prose
                bundle.chunks.extend(_chunk(path.name, text))
        else:
            raw = _strip_html(text) if path.suffix.lower() in {".html", ".htm"} else text
            bundle.chunks.extend(_chunk(path.name, raw))
    return bundle


def _resolve_paths(specs: list[str]) -> list[Path]:
    out: list[Path] = []
    for spec in specs:
        p = Path(spec)
        if p.is_dir():
            out.extend(_walk(p))
        elif p.is_file():
            out.append(p)
        else:
            matches = [
                Path(m) for m in sorted(globlib.glob(spec, recursive=True)) if Path(m).is_file()
            ]
            if not matches:
                raise SpecError(
                    f"context path matched nothing: {spec}",
                    hint="pass files, directories, or globs of .md/.txt/.rst/.html/.csv/.tsv",
                )
            out.extend(m for m in matches if m.suffix.lower() in PROSE_EXTS | DICT_EXTS)
    return out


def _walk(root: Path) -> list[Path]:
    found = []
    for p in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file() and p.suffix.lower() in PROSE_EXTS | DICT_EXTS:
            found.append(p)
    return found


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError as e:
        raise SpecError(
            f"cannot read context file: {path} ({e})", hint="check the path passed to --context"
        ) from e


class _TextExtractor(HTMLParser):
    """Minimal tag stripper for exported wiki pages; scripts/styles dropped."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data)


def _strip_html(text: str) -> str:
    p = _TextExtractor()
    p.feed(text)
    return "\n".join(p.parts)


def _chunk(filename: str, text: str) -> list[DocChunk]:
    headings = list(_HEADING_RE.finditer(text))
    if not headings:
        return [_mk(filename, "", text)] if text.strip() else []
    out = []
    preamble = text[: headings[0].start()].strip()
    if preamble:
        out.append(_mk(filename, "", preamble))
    for i, h in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[h.end() : end].strip()
        if body:
            out.append(_mk(filename, h.group(1).strip(), body))
    return out


def _mk(filename: str, heading: str, text: str) -> DocChunk:
    source = f"{filename}#{heading}" if heading else filename
    tokens = {t for t in re.split(r"\W+", (heading + " " + text).lower()) if len(t) > 2}
    return DocChunk(source=source, text=text, tokens=tokens)


def parse_dictionary(filename: str, text: str, delimiter: str = ",") -> list[DictEntry]:
    """Detect a table/column/description CSV shape; [] means 'not a dictionary'."""
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return []
    cols = [h.strip().lower() for h in header]
    col_i = _find(cols, _COLUMN_HDRS)
    desc_i = _find(cols, _DESC_HDRS)
    if col_i is None or desc_i is None:
        return []
    tbl_i = _find(cols, _TABLE_HDRS)
    out = []
    for row in reader:
        if len(row) <= max(col_i, desc_i) or not row[col_i].strip() or not row[desc_i].strip():
            continue
        table = (
            row[tbl_i].strip().lower()
            if tbl_i is not None and len(row) > tbl_i and row[tbl_i].strip()
            else None
        )
        out.append(
            DictEntry(
                table=table,
                column=row[col_i].strip().lower(),
                description=row[desc_i].strip(),
                source=filename,
            )
        )
    return out


def _find(cols: list[str], names: set[str]) -> int | None:
    for i, c in enumerate(cols):
        if c in names:
            return i
    return None


def relevant_excerpts(
    chunks: list[DocChunk], table_name: str, column_names: list[str]
) -> list[dict]:
    """Chunks mentioning this table or its columns, budget-bounded.

    Token matching (not embeddings) is deliberate: context docs name the same
    identifiers the schema uses, and determinism keeps cassettes stable.
    """
    wanted = {table_name.lower()} | {c.lower() for c in column_names}
    scored = []
    for ch in chunks:
        hits = len(wanted & ch.tokens)
        if hits:
            scored.append((hits, ch))
    scored.sort(key=lambda x: -x[0])
    out, budget = [], MAX_CHARS_PER_TABLE
    for _, ch in scored:
        excerpt = ch.text[: min(len(ch.text), budget)]
        out.append({"source": ch.source, "text": excerpt})
        budget -= len(excerpt)
        if budget <= 0:
            break
    return out


def dictionary_for_table(entries: list[DictEntry], table_name: str) -> dict[str, DictEntry]:
    """Dictionary rows applicable to `table_name`, keyed by column name.

    Rows with an explicit table stick to it; table-less rows apply anywhere
    the column name matches (common in single-schema dictionary exports).
    """
    out: dict[str, DictEntry] = {}
    tn = table_name.lower()
    for e in entries:
        if e.table is None or e.table == tn:
            if e.table == tn or e.column not in out:
                out[e.column] = e
    return out


def apply_dictionary(doc: dict, entries: list[DictEntry]) -> None:
    """Seed column descriptions from a CSV dictionary, with docs provenance.

    Runs BEFORE Describe: in --no-llm mode the seeded description stands; with
    an LLM the seed also rides into evidence and the model refines it. Never
    overwrites a description that already exists (priors, not truth).
    """
    for t in doc["semantic_layer"]["tables"]:
        matched = dictionary_for_table(entries, t["name"])
        for c in t["columns"]:
            e = matched.get(c["name"].lower())
            if e is None:
                continue
            if not c.get("description"):
                c["description"] = e.description[:200]
            _note(c, f"description seeded from {e.source}")


def apply_doc_decodes(doc: dict, chunks: list[DocChunk]) -> None:
    """Reconcile doc-claimed enum decodes against the model — priors, never truth.

    - llm_guess decodes matching a doc claim -> upgraded to decode_source:
      docs (usable in metric filters, unlike llm_guess per SPEC.md 2.8).
    - doc claim agreeing with a dictionary/data decode -> corroboration note.
    - doc claim CONTRADICTING an existing decode -> conflicts envelope,
      existing decode kept.
    Only values the data actually exhibits are ever touched.
    """
    for t in doc["semantic_layer"]["tables"]:
        for c in t["columns"]:
            claims = _claims_for_column(chunks, c["name"])
            existing = {str(e["value"]): e for e in c.get("enum_values") or []}
            if not claims or not existing:
                continue
            for value, meaning, source in claims:
                cur = existing.get(value)
                if cur is None:
                    continue
                _reconcile_claim(c, cur, value, meaning, source)


def _reconcile_claim(c: dict, cur: dict, value: str, meaning: str, source: str) -> None:
    if cur["decode_source"] == "llm_guess":
        cur["meaning"] = meaning
        cur["decode_source"] = "docs"
        _note(c, f"decode {value}={meaning} confirmed from {source}")
    elif _same_meaning(str(cur["meaning"]), meaning):
        _note(c, f"decode {value} corroborated by {source}")
    else:
        c.setdefault("conflicts", []).append(
            {
                "between": ["docs", "statistic"],
                "detail": (
                    f"{source} says {value}={meaning}, but "
                    f"{cur['decode_source']} decode says {value}={cur['meaning']}"
                ),
            }
        )


def _claims_for_column(chunks: list[DocChunk], column: str) -> list[tuple[str, str, str]]:
    # decode claims require the COLUMN named in the chunk (a table-level
    # fallback would cross-talk claims onto sibling enum columns), and are
    # scoped to a window around the mention
    out = []
    for ch in chunks:
        if column.lower() not in ch.tokens:
            continue
        idx = ch.text.lower().find(column.lower())
        window = ch.text[max(0, idx - 50) : idx + 400] if idx >= 0 else ch.text
        for m in _DECODE_RE.finditer(window):
            out.append((m.group(1), m.group(2).strip(), ch.source))
    return out


def _same_meaning(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    return a == b or a in b or b in a


def _note(c: dict, detail: str) -> None:
    c.setdefault("provenance", []).append({"signal": "docs", "detail": detail[:160]})
