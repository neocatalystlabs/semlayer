# What these files are

This directory holds **recorded model responses**. Each file is one request and
the answer it got, named by a hash of the inputs:

```json
{
  "model": "claude-haiku-4-5-20251001",
  "system_sha": "26fea8ed3e9d",
  "response": "{\"table_description\": \"Inventory fact table tracking ...\"}",
  "usage": {"in": 742, "out": 322}
}
```

They exist so that **anyone can run the full test suite, and reproduce every
accuracy number this project publishes, without an API key and without
spending anything.** `pytest` replays these instead of calling a model. That is
not a convenience — it is what makes the published numbers checkable by someone
who doesn't trust them, and it is the only way continuous integration can test
an LLM-dependent pipeline at all, because GitHub does not give API secrets to
pull requests from forks.

```bash
ANTHROPIC_API_KEY= pytest -q        # the whole suite, replay-only, $0
```

## This directory is the development cache, not the user's

A `semlayer infer` run caches model responses too, but never here: the CLI
caches in `semlayer/cassettes` under the platform cache directory, and
deliberately not in any source checkout — these files are tracked and not
git-ignored, so a user who cloned this repo and ran `infer` against their own
warehouse would otherwise have `git add -A` commit descriptions of it. Both
switches are documented in the README: `--no-llm-cache`, or
`SEMLAYER_LLM_CACHE=off`, or `SEMLAYER_LLM_CACHE=<dir>` to put it somewhere
else — which is also how you point a CLI run back at this directory when you
want one to replay or record here.

What the cache is worth to a run is narrower than it looks: a crashed or
interrupted run resumes free, because unchanged data profiles to the same
prompts. It deduplicates nothing *within* a run — measured on messy_mart, 98
calls and 98 distinct prompts — and a re-run after any insert misses, because
`row_count` and `n_distinct` go into the prompt.

## How a file is matched to a request

The filename is `sha256(model + system_prompt + user_prompt)`, truncated. Three
consequences worth knowing:

- **Prompts are frozen bytes.** Changing a prompt by one character changes the
  hash, so the old recording is never found again. That is why prompt edits are
  a deliberate, reviewed act with a `PROMPT_VERSION` bump — see `CONTRIBUTING.md`.
- **The model is part of the key.** Switching models invalidates every file here.
- **Nothing records which warehouse a response came from.** You can often tell
  from the text, but there is no metadata linking a file to a fixture. See the
  honest limitations below.

## Re-recording

Needs an API key, and costs real money:

```bash
pytest -q                            # records whatever is missing
```

A run with a key present **records on a cassette miss**. A run with
`ANTHROPIC_API_KEY=` empty fails loudly instead. Use the empty form whenever you
are not deliberately recording — including when checking whether a cassette is
still needed, because a recording run always passes and therefore cannot answer
that question. The variable must be **empty, not unset**: several tests call
`load_dotenv()` themselves and would restore it from `.env`.

For scale: recording the entire surface — the full suite plus all four benchmark
conditions across three fixtures — is about **600 files and under a dollar** on
a Haiku-class model.

## Honest limitations

This directory is not tidy, and you should know what you are looking at.

- **It still contains more files than the project needs.** 2,317 files, where
  645 are read by any path that produces a published number. 685 recordings
  keyed to two system prompts that no longer exist in the source were deleted in
  October 2026 -- that was provable, because hashing the prompts in the code
  shows those two are gone, so nothing can request them. The remaining ~1,670
  share a prompt that IS live but an input that is not: a removed test, a
  changed fixture. Those are probably dead too, but "probably" is not a reason
  to delete evidence behind published numbers, and they cost 2.1 MB in a
  repository that compresses to about 2 MB.
- **No file says which warehouse or test produced it.** Attribution has to be
  reconstructed by running the suite and observing which files get read.
- ~~`python -m semlayer.benchmark` does not replay end to end.~~ **Fixed in
  October 2026.** Two published rows were found to need recordings that had not
  been committed, and were corrected to the figures that reproduce (see
  `docs/benchmark.md`). A run holding an API key records whatever is missing and
  passes, so the gap showed only to a keyless run;
  `tests/test_benchmark_replays.py` now runs every published row with an empty
  key and asserts the printed table matches the committed doc.

These are tracked defects, not design choices. The measured figures above come
from `docs/calibration.md`, which records how they were found.

## Why they are committed rather than fetched

They are text, they compress well — the whole repository is about 2 MB on
GitHub — and a fetched artifact would add a network dependency to the one thing
that must work offline. They are marked `linguist-generated` in
`.gitattributes` so GitHub collapses them in diffs; review the code, not the
recordings.
