# Lancy Content Flow

LLM-guided creation of rule-bound documents. Sister project to
[lancy](https://github.com/) — where lancy retrieves and condenses existing documents, LCF elicits
and creates new ones against a rule set.

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

Early. Build-order steps 1–3 of [ARCHITECTURE §14](docs/ARCHITECTURE.md) are done:

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
| ✓ | Background jobs with live progress over SSE — drafting no longer blocks the request |
| ✓ | The full quality gate: judged checks, quote-backed, run as a job |
| | Span-level suggestions and the TipTap editor island |
| | Evidence intake (paste + images), docx / Markdown / JSON export |

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
```

## Commands

```bash
lcf lint docs/examples/8d-report.yaml        # validate a spec
lcf roundtrip docs/examples/4d-report.yaml   # YAML survives a round-trip
lcf publish docs/examples/4d-report.yaml     # publish a version
lcf buckets                                  # create missing buckets
lcf walkthrough                              # drive a 4D end to end, no LLM
lcf serve                                    # the web application
```

## Example document types

| Spec | Role |
|---|---|
| [4d-report.yaml](docs/examples/4d-report.yaml) | The demo — and a verified prefix of the 8D |
| [product-specification.yaml](docs/examples/product-specification.yaml) | The generality proof |
| [8d-report.yaml](docs/examples/8d-report.yaml) | The stress test |

## Tests

```bash
.venv/bin/python -m pytest
```

The deterministic core — spec model, linter, checks, state machine — is tested with dicts and needs
no database, no network and no model. Service tests use the real PostgreSQL from `.env` and skip
if it is unreachable. A boundary test asserts that `spec/` and `engine/` never import a database,
because that purity is what keeps the rest testable.
