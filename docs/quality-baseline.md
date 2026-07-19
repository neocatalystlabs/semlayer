# B1 Quality Baseline (2026-07-18, pre-fix-pass)

ruff: 212 findings (60 E501, ~70 docstring D-rules, 13 C901>10, 13 PLR, misc)
mypy: 12 errors / 5 files (2 bug-shaped: answer.py deleted-var read; drift.py bool/set)
pip-audit: 0 known CVEs (core deps current: duckdb 1.5.4, anthropic, mcp, ruff 0.15, mypy 2.3)

Complexity hotspots (C901): validate.py:52 cli.py:24 typing_rules.py:23
answer.py:20 export/dbt.py:20 blast_radius:16 classify_table:15
_domains_and_routing:14 apply_drift:12 review.apply:12 link_source:29

Target: zero findings under the Q7/Q8 gates; fixes land with ZERO behavior
change (262-test suite + eval floors green after every wave).
