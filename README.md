# Lancy Content Flow

Guided, LLM-assisted creation of rule-bound documents.

A **rule builder** defines document types: sections, the questions a creator must answer,
requirements, quality criteria, and a branded docx template. A **document creator** then works
through a guided flow that drafts what it can from the material supplied and asks about the rest.

Two invariants shape everything:

- **Content exists only after a human accepted it.** The model produces proposals; acceptance
  appends a revision. `Block` has no value column, so there is no field for a model to write into.
- **The LLM is a worker inside a deterministic frame.** Ordering, gating and composition are
  ordinary Python; the model answers narrow, schema-constrained questions inside it.

Design: [docs/DESIGN.md](docs/DESIGN.md) · Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Status

Early. Most of the build order in [ARCHITECTURE §14](docs/ARCHITECTURE.md) is done:

| Done | |
|---|---|
| ✓ | Spec model, linter, YAML round-trip |
| ✓ | PostgreSQL schema, Alembic migrations |
| ✓ | Deterministic check engine (6 kinds) |
| ✓ | Section state machine, dependency gating, staleness |
| ✓ | Service layer: publish, create, answer, edit, assess, blame |
| ✓ | Object storage bucket provisioning |
| ✓ | Web UI (FastAPI + Jinja + HTMX), paste-from-spreadsheet tables |
| ✓ | LLM drafting: schema-constrained proposals, gaps, accept/decline, decision log |
| ✓ | Background jobs with live progress — drafting no longer blocks the request |
| ✓ | The full quality gate: judged checks, quote-backed, run as a job |
| ✓ | Export: JSON, Markdown and docx, gated and recorded |
| ✓ | Evidence intake: paste a blob of notes, distributed across the sections, answers proposed from it |
| ✓ | Spec editor for the rule builder: check, publish as a new version, import/export YAML |
| ✓ | A structured builder for the same specs — sections, questions, blocks and checks as forms, for someone who has never read YAML |
| ✓ | Word templates: a starter generated from the spec, branded in Word, bound to a version and carried forward |
| | Image evidence: upload and captioning |
| | Span-level suggestions and the TipTap editor island |

What is left, in order, is in [BACKLOG.md](docs/BACKLOG.md).

**The milestone that matters:** a 4D report can be driven from empty to a clean quality gate with
hand-written content and no model involved. If that ever stops working, the deterministic core is
broken — and that is far cheaper to discover here than through an LLM.

```
$ lcf walkthrough

initial state
  · header             empty        1 question(s) unanswered
  ⊘ d1_team            blocked      waiting on header
  ⊘ d2_problem         blocked      waiting on header
  ⊘ d3_containment     blocked      waiting on d2_problem
  ⊘ d4_root_cause      blocked      waiting on d2_problem

filling sections in dependency order
  ✓ header             complete
  ✓ d1_team            complete
  ✓ d2_problem         complete
  ✓ d3_containment     complete
  ✓ d4_root_cause      complete

quality gate
  PASS     ✓  14 check(s)
  PENDING  ·  11 check(s) need a model, not evaluated

editing a completed section (d2_problem)
  revision 2 appended
  built on this: d3_containment, d4_root_cause — still valid?
```

## Setup

Requires Python 3.13, a PostgreSQL database, and an S3-compatible store. All local; nothing leaves
the network.

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"

cp .env.example .env     # fill in database, LLM endpoint and storage
.venv/bin/alembic upgrade head
.venv/bin/lcf buckets
.venv/bin/lcf seed       # publish the example document types
```

`lcf seed` is what turns an empty database into something you can click through:
it publishes [4d-report.yaml](docs/examples/4d-report.yaml) and
[product-specification.yaml](docs/examples/product-specification.yaml), and prints
where the sample intake material lives. It is idempotent — running it against a
database that already has them reuses the versions rather than making copies — and
it is a command rather than something startup does, so a real installation never
quietly acquires demo document types.

The 8D is not seeded. It is the stress test for the spec model, and 22 KB of it in
a fresh type list is clutter; publish it by hand with
`lcf publish docs/examples/8d-report.yaml` when that is the point.

### Trying the intake

Create a document from a seeded type, then paste one of these into the **Your
material** box and press *Sort it into the sections*:

| Notes | For |
|---|---|
| [4d-report-intake-notes.md](docs/examples/4d-report-intake-notes.md) | 4D Report |
| [product-specification-intake-notes.md](docs/examples/product-specification-intake-notes.md) | Product Specification |

Both are written the way material actually arrives — several sources, in several
registers, with the facts for one section scattered across three of them. Each
contains passages that belong nowhere in its document type, so you see what comes
back unplaced, and each leaves several required things unsaid, so drafting produces
gaps instead of inventing the answer. Nothing is stored that you did not paste: a
mapping is a quotation plus a section key, and the quotation is checked against
your text before the row exists.

## Commands

```bash
lcf lint docs/examples/8d-report.yaml        # validate a spec
lcf roundtrip docs/examples/4d-report.yaml   # YAML survives a round-trip
lcf publish docs/examples/4d-report.yaml     # publish a version
lcf seed                                     # publish the example types
lcf buckets                                  # create missing buckets
lcf walkthrough                              # drive a 4D end to end, no LLM
lcf serve                                    # the web application
```

## Example document types

| Spec | Role |
|---|---|
| [4d-report.yaml](docs/examples/4d-report.yaml) | The demo — and a verified prefix of the 8D |
| [product-specification.yaml](docs/examples/product-specification.yaml) | The generality proof |
| [8d-report.yaml](docs/examples/8d-report.yaml) | The stress test — not seeded |

Sample material to go with them: [`4d-sample-content.yaml`](docs/examples/4d-sample-content.yaml)
is finished content, used by `lcf walkthrough` and the tests; the two
`*-intake-notes.md` files above are raw notes, for the intake feature.

## Licence

MIT — see [LICENSE](LICENSE).

## Tests

```bash
.venv/bin/python -m pytest
```

The deterministic core — spec model, linter, checks, state machine — is tested with dicts and needs
no database, no network and no model. Service tests use the real PostgreSQL from `.env` and skip
if it is unreachable. A boundary test asserts that `spec/` and `engine/` never import a database,
because that purity is what keeps the rest testable.
