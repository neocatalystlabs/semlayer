# Spike: FD-based hierarchy inference on dirty data (M2 gating spike)

**Question** (PRD §7.7, feasibility judge BLOCKER-B): can functional-dependency
mining reliably auto-infer level hierarchies (city→state→country) on realistic
data, or must hierarchies stay review-queued?

**Method**: within-table FD test over all low-cardinality column pairs
(2 ≤ distinct ≤ 500): a→b holds when `COUNT(DISTINCT a) == COUNT(DISTINCT (a,b))`.
Compared against gold hierarchy level edges.

**Results (2026-07-18):**

| Fixture | FD edges found | Gold edges | Recall | Precision |
|---|---|---|---|---|
| messy_mart | 123 | 4 | 0.750 | **0.024** |
| obt (one-big-table) | 42 | 3 | 1.000 | **0.071** |

**Verdict: the feasibility judge was right.** FD mining finds the true edges
but drowns them ~30:1 in spurious ones. Failure modes observed:
1. **Transitive edges** (city→country alongside city→state→country) — fixable
   with transitive reduction.
2. **Key-column artifacts** (prod_id→brand/category/color/price: every
   attribute is functionally dependent on a key) — fixable by excluding
   identifier-role columns as FD children.
3. **Cross-concept coincidences** (category_nm→dept_cd via the real hierarchy,
   but also name↔code duals of the same level) — needs semantic filtering
   (level-name affinity, geo/product vocabulary) or LLM validation.

**Decision (standing, per PRD):**
- Hierarchies remain **review-queued in v1** — never auto-included from FD
  evidence alone.
- FD mining is the *candidate generator*; the productionized path adds
  transitive reduction + key-exclusion (mechanical) and LLM/lineage/usage
  corroboration (M4, where dbt lineage and query-log GROUP BY patterns join
  the evidence pool).
- Recursive hierarchies are the exception: name-hinted self-reference
  detection is already gated and shipping (self_ref F1 = 1.0).
