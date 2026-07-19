# Contributing

Thanks for your interest! During the beta we accept issues and small PRs;
larger contributions are best discussed in an issue first. Contributions
require agreeing to the project CLA (a bot will prompt on your first PR).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,warehouses]"
python fixtures/build.py        # ~20s: all 9 evaluation warehouses
pytest tests/ -q                # full suite, $0 in LLM spend (cassette replay)
```

## Quality gates (CI enforces all of these)

```bash
ruff check src/                 # lint + complexity<=10 + docstrings: zero findings
ruff format --check src/
mypy                            # zero errors
pytest tests/ -q                # all tests AND eval floors green
```

## The rules that are different here

1. **Eval floors are contracts.** Accuracy targets live in test files with the
   measured baseline documented. If your change lowers a floor, that is a
   regression to fix, not a number to edit. Raising a floor after a genuine
   improvement is welcome — as its own reviewed change.
2. **Prompt strings are frozen bytes.** Cassette keys hash the exact prompt.
   To change a prompt: bump its `PROMPT_VERSION`, re-record cassettes
   (requires an API key), and include both in the PR. Never edit prompt text
   incidentally.
3. **Golds change only by adjudication.** Fixture gold files are ground truth;
   they change only when the fixture *generator source* proves them wrong,
   with the adjudication documented in the commit message.
4. **Comments state constraints, not narration.** Say why the code must be
   this way; never restate what the next line does.
5. **Zero behavior change in refactors** — the suite must pass identically.
