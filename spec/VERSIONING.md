# Format Versioning & Governance

## Versioning

The spec follows **semver**, declared in every document as `semantic_layer.spec_version`.

- **PATCH** (0.1.0 → 0.1.1): clarifications, doc fixes, new *optional* enum values. All existing documents remain valid.
- **MINOR** (0.1 → 0.2): new optional fields/objects. All existing documents remain valid; older consumers ignore unknown fields at their own risk (the schema is closed — validators pin to the document's declared version).
- **MAJOR** (0.x → 1.0, 1.x → 2.0): breaking changes — removed/renamed fields, changed semantics, tightened constraints. Requires a published migration guide and an automated migrator in the CLI (`semlayer migrate`).

Pre-1.0 caveat: while `spec_version` is 0.x, MINOR releases may include breaking changes; each is listed in CHANGELOG with migration notes. From 1.0, the rules above are hard guarantees.

## Compatibility policy

- Validators validate against the schema matching the document's declared `spec_version`; schemas for all released versions ship with the package.
- Deprecated fields survive at least one MINOR release with a deprecation warning before MAJOR removal.
- The **consumer contract (SPEC.md §2) is part of the spec**: a behavioral change to the contract is a version bump like any schema change.

## Governance (pre-launch)

Until the public launch, the spec is maintained in this repository; changes land by PR with review. At launch this section is replaced with the public process (RFC issues, a CHANGELOG discipline, and a compatibility test suite that downstream implementations can run). Contributions will require the project CLA.
