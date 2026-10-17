# Licensing

semlayer is **Fair Source**, not open source. Two licences apply, and which one
applies to a file depends on which directory it is in.

| What | Licence | Where |
|---|---|---|
| The engine and the CLI | `FSL-1.1-ALv2` | everything not listed below |
| The format specification and its JSON Schemas | `Apache-2.0` | [`spec/`](spec/), [`src/semlayer/spec/`](src/semlayer/spec/) |
| The dbt exporter | `Apache-2.0` | [`src/semlayer/export/`](src/semlayer/export/) |

Each Apache-2.0 directory carries its own `LICENSE` file, and every source file
carries an `SPDX-License-Identifier` line, so a scanner reaches the right answer
without reading this page.

## What FSL permits, free, for anyone

- **Internal use, for any business purpose.** Run semlayer against your own
  warehouses, in development and in production, for as many people inside your
  organisation as you like. No fee, no key, no permission.
- **Modification**, and keeping your modifications private.
- **Redistribution**, as long as the licence travels with the code.
- **Non-commercial education and research.**
- **Professional services.** A consultancy running semlayer on a client's
  warehouse is fine.

## What it prohibits

One thing: **Competing Use** — making semlayer available to third parties in a
commercial product or service that competes with semlayer, or with any product
or service we offer using it. Hosting the engine as a service other companies
connect to is the case this is about.

The practical question people ask first: `semlayer mcp` runs an MCP server.
Running it inside your own organisation, for your own agents and analysts, is
internal use and permitted. Running it as a service other companies connect to
is Competing Use.

## Every release becomes Apache-2.0 after two years

FSL is not a permanent restriction. Each published version converts to
Apache-2.0 on the second anniversary of its publication, automatically and
irrevocably, by the terms of the licence itself.

| Release | Published | Licence now | Apache-2.0 from |
|---|---|---|---|
| 0.1.0b1 – 0.4.0b5 | 2026-07-18 – 2026-10-03 | **Apache-2.0 already** | — |
| 0.5.0b1 onward | 2026-10-17 → | FSL-1.1-ALv2 | 2028-10-17 → |

**Everything published before 0.5.0b1 is Apache-2.0 and stays that way
permanently.** Those artifacts are on PyPI with `Apache-2.0` in their metadata
and cannot be changed.

## For enterprise and security teams

Automated scanners flag non-OSI licences by default. Policy text you can paste
into an allow-list:

> semlayer is distributed under the Functional Source License 1.1 with an
> Apache-2.0 future grant (SPDX: `FSL-1.1-ALv2`). Internal use, including in
> CI pipelines and production automation, requires no fee, licence key, or
> special grant. The licence prohibits only offering the software to third
> parties as a competing commercial product or service. Each version converts
> to Apache-2.0 two years after its publication.

GitHub's licence detector does not recognise FSL, so the repository sidebar
reads "Other" rather than naming it. The SPDX identifier is registered, so
PyPI and SCA tools identify it correctly.

## Why not open source

Being accurate about this is the point. FSL is not approved by the OSI, so
calling semlayer open source would be false, and we do not. The format is the
part that must be freely reusable — a semantic layer nobody else can read is
worth nothing — so the specification, the schemas and the exporters are
Apache-2.0 and stay that way. The engine is what a competitor would host, and
that is the part FSL protects.

## Contributing

Contributions require the [CLA](CLA.md), which grants us a licence to your
contribution rather than assigning copyright. You keep ownership of what you
write.

## This layout is interim

Three directory-level `LICENSE` files inside one repository is not how most
projects do this; the usual pattern is one licence per repository, with
permissively licensed material living in its own. We intend to move the
specification, the schemas and the exporters into a separate Apache-2.0
repository, after which this repository carries one licence and that one
carries the other. Until then, the table above is authoritative.
