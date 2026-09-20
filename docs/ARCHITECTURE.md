# Lancy Content Flow — Architecture

> Status: **draft v0.2** · 2026-09-20 · companion: [DESIGN.md](DESIGN.md)

## 1. Shape: one service

```
┌────────────────────────────────────────────────────────────┐
│  lcf   (single Python service)                             │
│                                                            │
│   FastAPI                                                  │
│    ├── Jinja2 templates + HTMX        ← the UI             │
│    │     └── JS islands: editor, suggestion overlay        │
│    ├── HTTP routes (thin)                                  │
│    └── service layer  ← the real contract                  │
│          spec · engine · llm · evidence · render · jobs    │
└───────────┬──────────────┬──────────────┬──────────────────┘
            │              │              │
   OpenAI-compatible    asyncpg      S3 (boto3)
            │              │              │
            ▼              ▼              ▼
   ┌────────────────┐  ┌──────────────────────────┐
   │  LLM host      │  │  data host               │
   │                │  │                          │
   │  vLLM          │  │  PostgreSQL   versitygw  │
   │  multimodal    │  │               (S3 API)   │
   │  (image budget │  │                          │
   │   per call)    │  │                          │
   └────────────────┘  └──────────────────────────┘
```

Everything runs on the local network. No cloud dependency, no egress.

### Why not a split frontend and backend

The guided flow is **server-held state**: which section you're on, what's answered, what's
proposed, what's stale. A SPA would mirror that state in the browser and the two would drift —
and every flow rule would end up expressed twice. HTMX suits this exactly: accepting a proposal is
a `POST` that swaps in new HTML, and the server stays the only authority on what the document is.

JS is used where it genuinely earns its place, and nowhere else — the markdown editor and its
suggestion overlay (§6).

### The service layer is the contract, not HTTP

Capabilities are **plain Python functions** — typed, transactional, fully testable without a web
server. HTTP routes are a thin skin over them; so is anything else we add later.

This deliberately replaces v0.1's "everything must be an API call" rule, which conflated two
claims. *A document can be produced by one API call* was never true — the flow is stateful and
iterative by nature, and pretending otherwise would have distorted the design. *Every capability is
reachable programmatically* is worth keeping, and a service layer gives it to us more cheaply than
an HTTP-first mandate did.

## 2. Stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.13 | |
| Web | FastAPI + uvicorn | Routes, templates and JSON from one app |
| Templates | Jinja2 | Server-rendered HTML is the UI |
| Interactivity | HTMX 2 + Alpine.js | Swaps and local state; no build step needed |
| Editor island | TipTap (ProseMirror), vanilla | Bundled with esbuild — the only JS build |
| CSS | Tailwind CLI (standalone binary) | No Node project required |
| Validation | Pydantic v2 | Also the source of every LLM output schema |
| Settings | pydantic-settings | `.env` driven |
| ORM | SQLAlchemy 2 (async) + asyncpg | |
| Migrations | Alembic | From commit 1 |
| LLM client | `openai` SDK → vLLM | OpenAI-compatible, guided decoding |
| Markdown | markdown-it-py | Parse, restrict, normalise (DESIGN §9) |
| docx | docxtpl + python-docx | Jinja tags in a real Word template |
| Object storage | boto3 → versitygw | |
| Logging | loguru | |
| Lint / test | ruff, pytest | |

Full library shortlist with rationale: [§12](#12-library-shortlist).

### Deliberately absent

| Not using | Instead | Why |
|---|---|---|
| A JS framework | Server-rendered HTML + islands | The state lives on the server; mirroring it is the cost, not the feature |
| Redis / Celery | Postgres job table | One less service; we need job rows for progress and audit anyway |
| A vector DB | Context + rolling summaries | Retrieval is a different product |
| A workflow engine | State machine over `depends_on` | The graph is small and the semantics are ours |
| A rules DSL | Closed vocabulary of check kinds | A DSL is a language to learn, document and debug |
| A git library | YAML export/import | Version control specs yourself; no credentials, no merge UI |

## 3. Repository layout

```
lcf/
├── src/lcf/
│   ├── main.py             # FastAPI app assembly
│   ├── core/               # config, db session, logging
│   ├── models/             # SQLAlchemy
│   ├── schemas/            # Pydantic
│   ├── services/           # THE CONTRACT — plain functions, no HTTP
│   │   ├── doc_types.py    #   publish, validate, import/export
│   │   ├── documents.py    #   create, intake, section state
│   │   ├── proposals.py    #   propose, accept, reject, revise
│   │   └── assessment.py   #   run checks, gate, report
│   ├── spec/               # spec model, YAML round-trip, linter
│   ├── engine/             # state machine, staleness, composition
│   │   └── checks/         #   deterministic/  llm/
│   ├── llm/                # provider, typed calls, prompts/, style resolution
│   ├── evidence/           # intake, mapping, captioning
│   ├── markdown/           # subset definition, AST normalisation
│   ├── render/             # json, markdown, docx + template linter
│   ├── jobs/               # queue, worker, SSE
│   └── web/                # routes, templates/, static/
├── assets/                 # editor island source → built into web/static
├── alembic/
├── tests/
├── docs/
│   ├── DESIGN.md · ARCHITECTURE.md
│   └── examples/*.yaml
├── .env.example
└── docker-compose.yml
```

`spec/`, `engine/` and `render/` **know nothing of each other beyond data**. `engine/` never
imports `render/`; `render/` never imports `engine/`. That boundary is the code-level expression of
[DESIGN §4](DESIGN.md#4-three-artifacts-kept-apart) and is worth a test that asserts it.

## 4. Data model

```
doc_type ──┬─< doc_type_version ─┐         (immutable, pinned by document)
           │                    │
           └─< exemplar         │         section_key, block_key, value,
                                │         harvested_from (revision id)
document ───────────────────────┘
   ├──< evidence_item        kind, uri, text, caption, meta      (verbatim, never edited)
   │      └──< evidence_link      section_key, quote, why, confidence
   ├──< section              key, status
   │      ├──< answer        question_key, value, source
   │      └──< block         key, kind
   │             ├──< revision    seq, value, author, proposal_id     (append-only)
   │             └──< proposal    anchor, proposed_value, rationale,
   │                              based_on, confidence, status
   ├──< assessment ──< check_result
   ├──< llm_call             call_type, prompt, response, tokens, ms
   ├──< job
   └──< export               format, uri
```

### Notable decisions

**Spec stored as one JSONB document, validated by Pydantic on read.** A spec version is authored,
validated and published as a unit. Shredding it across a dozen tables would buy query flexibility
we don't need while making versioning and diffing painful.

**Content stored as rows.** The opposite choice for the opposite reason: blocks are written
constantly and individually, and each needs its own history.

**`revision` is append-only and is the content.** A block has no `value` column — its value is its
latest revision. This is [invariant I](DESIGN.md#i-content-exists-only-after-a-human-accepted-it)
expressed in the schema: there is no field for an LLM to write into. Blame is computed by diffing
consecutive revisions, not stored per character.

**`proposal` rows survive their outcome.** Accepted, edited or rejected, they stay. What the model
suggested and what the human did about it is the audit trail.

**`llm_call` logs every call** — type, resolved prompt, resolved style, response, tokens, duration.
"Why did it write it that way" must be answerable weeks later.

**Versions are immutable; documents pin one.** Publishing freezes a version. Without this, editing
a rule silently invalidates every document in progress.

## 5. LLM integration

### 5.1 Structured output

vLLM supports **guided decoding** (xgrammar/outlines), so a JSON schema is a grammar-level
guarantee rather than a hope. Every structured call:

1. Defines its response as a Pydantic model.
2. Passes the schema as `response_format`, in the form verified below.
3. Validates the result against the model anyway.
4. Retries once with the validation error appended on failure.

**Verified against the running gateway** (2026-09-20), because the parameter form matters and the
wrong one fails silently:

| Form | Result |
|---|---|
| `response_format: {"type": "json_schema", "json_schema": {name, schema, strict: true}}` | **Works.** Schema held exactly |
| `response_format: {"type": "json_object"}` | Valid JSON, but arbitrary shape — not usable |
| `guided_json` at the top level | **Silently ignored.** Returns plain prose, no error |

The last row is why steps 3–4 are not redundant. An endpoint that ignores a constraint returns a
`200` with unusable content, and without local validation that becomes corruption rather than a
logged retry. Never trust the constraint alone.

**Two further constraints the schema itself must carry**, both learned the hard way:

- **Every array needs `maxItems`.** A constrained array has no reason to stop, so the model
  generates rows until it exhausts the context — minutes of work for a table wanting four entries.
  Spec `min_rows`/`max_rows` supply the real bounds; a ceiling covers the rest.
- **Guard against whitespace padding.** A JSON grammar permits unlimited whitespace between
  tokens, and this model routinely exploits it: generations pad thousands of newlines *mid-object*
  and run to the token ceiling without ever closing. Measured on all three calls of a typical
  section.

Because the padding happens inside the object, no amount of token budget fixes it — the object
never closes. So requests are **streamed**, with two aborts:

| Guard | Effect |
|---|---|
| Stop when brace depth returns to zero | The object is complete; everything after is padding |
| Abort on a whitespace run outside a string | The pathology has started and will not terminate |

An aborted call is retried immediately with a compacted echo of what came back. The retry succeeds
in practice, and catching the pathology early is what makes the difference between a section
drafting in **16 seconds and in nearly three minutes** — measured, same section, same model.

Independent blocks are drafted **concurrently** under `LCF_LLM_CONCURRENCY`, so a section costs
its slowest block rather than the sum of them.

### 5.2 Call taxonomy

Per [DESIGN §6.3](DESIGN.md#64-narrow-calls-flexible-context), every call answers one question and
returns one schema. The complete set:

| Call | Scope | Returns |
|---|---|---|
| `map_evidence_to_sections` | intake blob + section list | evidence → section assignments, confidence |
| `caption_images` | ≤ image budget | caption per image |
| `prefill_answers` | one section's questions + mapped evidence | proposed answers, or "ask the user" |
| `draft_section` | one section: spec, style, answers, evidence | block proposals + open gaps + unsupported claims |
| `suggest_span` | one span + instruction | proposed replacement + rationale |
| `evaluate_check` | one LLM check + referenced blocks | pass/fail + reason + confidence |
| `evaluate_consistency` | one criterion across 2–n sections | pass/fail + reason + evidence refs |
| `summarize` | one section or the evidence pool | compact standing summary for later context |

Eight call types. Each has a fixed Pydantic response model, a prompt template in `llm/prompts/`,
and test fixtures. **A ninth requires a design decision** — the pressure to add "just one more
general-purpose call" is exactly what this table exists to resist.

Composition lives in `engine/`, in ordinary Python.

### 5.3 Context budget

Output scope is fixed and narrow; input context is a tactical decision per call:

1. Always: section spec, resolved style, confirmed answers, current content.
2. If it fits: raw mapped evidence.
3. If it doesn't: the `summarize` output for that evidence instead.
4. Images: only for `uses_images` sections, within the per-call budget
   (`LCF_LLM_MAX_IMAGES_PER_CALL` — config, because it is a model property that will change).

The budget check is a token count against a configured window, not a guess.

### 5.4 Prompt assembly

Prompts are **composed, never authored by users** ([DESIGN §5.8](DESIGN.md#58-instructing-the-model)).
`llm/prompts/` holds the task frames and the system style default, versioned with the code. A
`draft_section` prompt is assembled in fixed order:

| # | Part | Source |
|---|---|---|
| 1 | Task frame | `llm/prompts/draft_section.md` — ours, fixed |
| 2 | Resolved style | system default → doc type → section |
| 3 | Section spec | title, description, guidance, block definitions |
| 4 | Requirement targets | **rendered from the section's own checks** |
| 5 | Exemplars | fact-fenced, if the doc type has any |
| 6 | Runtime data | confirmed answers, mapped evidence, current content |
| 7 | Output schema | Pydantic → `guided_json` |
| 8 | The never-invent rule | composed **last**, so nothing above softens it |

Part 4 is the one that costs nothing and does the most: a `mentions` requirement is rendered as an
explicit target, so the model is told what it will be graded on by the very check that will grade
it. There is no second place to keep those instructions in sync.

Part 8 is not overridable:

> Never invent facts. If a requirement cannot be satisfied from the supplied evidence, emit a gap,
> not a draft.

Enforced by ordering and covered by a test that composes a hostile doc-type style and asserts the
rule still terminates the prompt.

The assembled prompt is **stored on the `llm_call` row** and viewable per section in the spec
editor. The rule builder cannot edit it — but they cannot debug guidance they cannot see.

#### Exemplar leakage

Exemplars carry facts from a different case. A proposal sharing a distinctive n-gram with an
exemplar it was shown is flagged for review rather than presented as a clean draft — cheap to
compute, and it catches the specific way few-shot prompting fails.

## 6. The editor island

The only substantial JavaScript. A TipTap (ProseMirror) instance per prose block, restricted to the
markdown subset from [DESIGN §9](DESIGN.md#9-markdown-integrity) by its schema — the editor is
structurally incapable of producing a heading or a table.

| Concern | Mechanism |
|---|---|
| Markdown ↔ document | `tiptap-markdown`, serialising to the same subset the server enforces |
| Suggestion spans | ProseMirror **decorations** — marked ranges with a hover card |
| Accept / reject | HTMX `POST` to `/proposals/{id}/accept`; server returns the new block HTML |
| Edit in place | Accept, then edit normally; the revision records `llm_accepted_edited` |
| Blame view | Decorations again, coloured by revision, from a server-computed diff |
| Table paste | Clipboard TSV/CSV parsed client-side, column mapping confirmed server-side and remembered per doc type ([DESIGN §14.3](DESIGN.md#143-tables-are-pasted-not-typed)) |
| Decision log | Server-rendered collapsed strip; no JS beyond the disclosure toggle |

Decorations are the reason for TipTap: they mark ranges *without* altering the document, which is
precisely a suggestion — visible, hoverable, and not yet content. That maps onto invariant I with
no impedance mismatch at all.

Everything else in the UI is plain server-rendered HTML with HTMX swaps. Alpine handles local
toggles that are not worth a round trip.

## 7. Jobs and streaming

LLM work is slow enough to need progress and cancellation, not slow enough to need Celery.

- A `job` row carries `status`, `step`/`total`, `message`, `result`, `error`.
- v1 runs work **in-process** as an asyncio task started at enqueue. Because the state is a row
  rather than memory, splitting it into its own process later is a deployment change, not a
  rewrite — that is when `SELECT … FOR UPDATE SKIP LOCKED` and `LISTEN/NOTIFY` become worth adding.
- Each job opens **its own session**. It outlives the request that queued it and must not borrow
  that request's transaction.
- Progress reaches the browser by **polling**, once a second, in server-rendered HTML. The card
  asks for itself again until the job finishes, then returns an element that loads the finished
  region into place.

**This replaces SSE, which was the original decision and failed in practice.** `htmx-ext-sse`
wants the element carrying `hx-trigger="sse:done"` to be a *descendant* of the one carrying
`sse-connect`; with both on one element the listener is registered before the source exists. The
job completed correctly and the page sat on "Working…" indefinitely — and nothing on the server
said anything was wrong.

Two reasons polling is the better call here and not merely the working one:

| | |
|---|---|
| **Verifiable** | The whole chain can be walked with `curl` — POST, each poll, the completion element, the final region. The SSE version could only be tested by opening a browser, which is how it shipped broken. |
| **Cheap at this scale** | A 15-second job costs ~15 requests against a primary key. That is nothing, and it removes a vendored extension and a dependency. |

A stuck spinner over finished work is the worst failure mode this feature has: it tells the user
the opposite of the truth. Worth a duller mechanism.

What this buys, measured: the draft request returns in **54 ms** instead of blocking for the
length of the generation. The work continues if the browser goes away, and reopening the section
rejoins the running job rather than offering a second one.

Independent calls within a job run concurrently under a semaphore sized to what the LLM host
tolerates. The final assessment is naturally parallel across checks.

## 8. Object storage

| Bucket | Holds | Lifecycle |
|---|---|---|
| `lcf-uploads` | Images and any uploaded files | Deleted with the document |
| `lcf-templates` | docx templates, logos, fonts | Versioned with the doc type |
| `lcf-exports` | Rendered docx | Retained |

Postgres holds metadata and content; S3 holds bytes. Downloads are presigned URLs, so the app never
proxies file payloads.

## 9. Route surface

Server-rendered pages return HTML; the same service functions are exposed as JSON where scripting
is plausible. Both are thin.

```
# Rule builder
GET   /doc-types                          list, with how many documents pin each
GET   /doc-types/new                      editor on a skeleton that publishes
GET   /doc-types/{key}/versions/{v}       what this type demands, in prose
GET   /doc-types/{key}/versions/{v}.yaml  YAML out  (declared before the route above)
GET   /doc-types/{key}/edit               editor, opened as version v+1
POST  /doc-types/check                    parse + model + lint, publishing nothing
POST  /doc-types/publish                  publish a new version
POST  /doc-types/import                   YAML in → opens in the editor
PUT   /doc-types/{key}/versions/{v}/template

# Creator
POST  /documents                          {doc_type_id, version}
GET   /documents/{id}                     flow overview: section states, open work
POST  /documents/{id}/intake              paste text (images later) → job
GET   /documents/{id}/intake/panel        where the material landed
GET   /documents/{id}/sections/{key}      the working surface
POST  /documents/{id}/sections/{key}/answers
POST  /documents/{id}/sections/{key}/draft                      → job
POST  /proposals/{id}/accept              → new block HTML
POST  /proposals/{id}/reject
PATCH /blocks/{id}                        direct user edit → revision
POST  /documents/{id}/sections/{key}/complete
POST  /documents/{id}/assess                                    → job
GET   /documents/{id}/blame
POST  /documents/{id}/export/{format}     json | markdown | docx

# Jobs
GET   /jobs/{id}/card                     one poll (see the jobs section)
```

`GET /documents/{id}` is deliberately rich — one request renders everything the flow needs, so the
page never assembles truth from five endpoints.

## 10. Sequence — drafting one section

```mermaid
sequenceDiagram
    participant U as Creator
    participant W as Web (FastAPI)
    participant S as Services
    participant J as Worker
    participant L as vLLM
    participant D as Postgres

    U->>W: POST /sections/d4/answers
    W->>S: save_answers()
    S->>D: insert answers
    S-->>W: required answered → drafting unlocked
    W-->>U: HTML swap: draft button enabled

    U->>W: POST /sections/d4/draft
    W->>S: request_draft() → job
    W-->>U: SSE subscribe

    J->>D: claim job, load spec + style + answers + evidence
    J->>J: context budget: raw evidence or summary?
    J->>L: draft_section (guided_json)
    L-->>J: proposals + gaps + unsupported
    J->>J: validate · normalise markdown to subset
    J->>D: insert PROPOSAL rows (no content written)
    J->>D: deterministic checks → check_results
    J-->>U: SSE done

    U->>W: GET /sections/d4
    W-->>U: proposals as decorated spans, gaps as a form
    U->>W: POST /proposals/{id}/accept
    W->>S: accept() → append revision
    W-->>U: HTML swap: block now has content
```

Note where content appears: **only** at the final accept.

## 11. Configuration

All settings come from the environment via `pydantic-settings`. See
[`.env.example`](../.env.example) for the full set with neutral placeholders; `.env` holds the real
values and is git-ignored.

Values that are not credentials but *model properties* — notably
`LCF_LLM_MAX_IMAGES_PER_CALL` and the context window — are configuration rather than constants,
because they change when the model does and the batching logic must read them rather than assume.

## 12. Library shortlist

Nothing here should be written by hand if a proven library exists.

### Core

| Need | Library | Why this one |
|---|---|---|
| Spec validation | **Pydantic v2** | One model definition serves validation, OpenAPI and LLM schemas |
| YAML round-trip | **ruamel.yaml** | Preserves comments and ordering — essential if specs are git-tracked |
| Spec version diff | **DeepDiff** | Structural diff of two spec documents, ready to render |
| Dependency order | **`graphlib.TopologicalSorter`** | Stdlib. Ordering *and* cycle detection for `depends_on`, free |
| Settings | **pydantic-settings** | `.env` → typed config |

### Content

| Need | Library | Why this one |
|---|---|---|
| Markdown parse/restrict | **markdown-it-py** | CommonMark with a token stream — whitelisting the subset is a filter, not a regex |
| Markdown extensions | **mdit-py-plugins** | Only if we ever need more than the subset |
| HTML sanitising | **nh3** | Rust `ammonia` bindings; `bleach` is deprecated |
| Span diff for blame | **diff-match-patch** | Character-level diffs designed for exactly this; stdlib `difflib` if line-level suffices |

### Editor

| Need | Library | Why this one |
|---|---|---|
| Rich text | **TipTap v2** (vanilla, no React) | ProseMirror with a sane API; decorations are first-class |
| Markdown bridge | **tiptap-markdown** | Serialise to the same subset the server enforces |
| Change tracking | **prosemirror-changeset** | Only if accept/reject needs to survive concurrent edits |
| Bundling | **esbuild** | One command, no framework, no config file |

### LLM

| Need | Library | Why this one |
|---|---|---|
| Client | **openai** SDK | vLLM speaks it; `extra_body` carries `guided_json` |
| Structured output | Pydantic + `guided_json` | **instructor** is an option but adds a layer over something vLLM already guarantees |
| Token counting | **tiktoken** or the model's tokenizer | Needed for a real context budget, not a guessed one |
| Images | **Pillow** | Resize and normalise before vision calls; keeps payloads sane |

### Output

| Need | Library | Why this one |
|---|---|---|
| docx from template | **docxtpl** | Jinja in a real Word file; designers keep control of branding |
| docx internals | **python-docx** | Underlies docxtpl; direct use for image placement and table styling |
| Rich markdown → docx | **pypandoc** + `reference.docx` | Only if docxtpl's RichText proves too limited for prose formatting |

### Infrastructure

| Need | Library | Why this one |
|---|---|---|
| Job queue | **procrastinate** | Postgres-backed, mature — the alternative is ~200 lines of `SKIP LOCKED` we'd own |
| S3 | **boto3** | versitygw patterns already exist next door |
| Migrations | **Alembic** | |
| SSE | **sse-starlette** + `htmx-ext-sse` | Server-side progress rendering, no custom JS |
| Test async | **pytest-asyncio** | |
| LLM fixtures | **respx** | Record real vLLM responses, replay them in CI |

### Later, if ever

| Need | Library | When |
|---|---|---|
| PDF/DOCX import | **docling**, **markitdown** | Only when file import stops being optional (DESIGN §6.1) |
| Git sync | **dulwich** | Pure-Python, no libgit2 — if export/import ever proves insufficient |

## 13. Testing

| Layer | Approach |
|---|---|
| Spec model | YAML → model → YAML round-trip; linter rejects malformed specs |
| Deterministic checks | Pure functions, table-driven, no LLM, no DB |
| State machine | Readiness and staleness propagation over the example specs |
| Markdown subset | Property test: normalise(arbitrary markdown) ∈ subset |
| Services | Real Postgres, no HTTP |
| Composition | Recorded LLM responses via respx; assert merge logic and proposal creation |
| Live LLM | Small suite against the real host, run on demand, not in CI |
| Rendering | Render a known document; assert docx contains expected text and images |
| Boundaries | Assert `render/` does not import `engine/`, and vice versa |

The parts that must be right — deterministic checks, the state machine, composition — are all
testable without a model. That is by design. If the flow can only be tested by talking to an LLM,
the architecture has failed.

## 14. Build order

1. Spec model, linter, YAML round-trip. No web, no DB.
2. Postgres schema + Alembic. Documents, sections, blocks, revisions.
3. Deterministic checks, state machine, staleness. **Walk a 4D end to end with typed content and
   no LLM.**
4. Server-rendered flow UI with HTMX: sections, questions, plain editing.
5. LLM provider, `draft_section`, the proposal model. Block-level accept/reject first.
6. The editor island: TipTap, span proposals, decorations, blame.
7. Intake: paste + `map_evidence_to_sections`; images and captioning.
8. Assessment: LLM checks, quality criteria, gate report.
9. Export: JSON, then Markdown, then docx with template linting.
10. Spec editor UI for the rule builder.

Steps 1–5 and 7–10 are done; drafting, assessment and intake all run as background jobs. Step 7's
text half is complete — paste, `map_evidence_to_sections`, `prefill_answers` — and its image half
(upload and captioning) is not. What remains is images and the editor island (step 6).

**The spec is edited as YAML, deliberately.** It is nested and referential — sections depending on
sections, checks naming columns in other sections' tables — and a form-per-field editor would hide
the relationships a rule builder is actually reasoning about, while being far more code. What the UI
owes them instead is an honest answer to *is this valid?* before they publish (what a structured
form editor would take instead, and what to watch for, is [§15](#15-a-structured-spec-editor-if-it-is-ever-wanted)): `doc_types.review()`
runs the same parse, the same Pydantic model and the same linter the publish runs, and reports all
three failure modes as one list. Alongside it is a **read view** that renders what a type demands in
prose, with every check rendered by `spec/describe.py` — the same function that composes that check
into the drafting prompt, so the page cannot drift from what the model is told.

**Intake stores nothing the author did not write.** A mapping is a quotation plus a section key, and
the quotation is verified against the paste before the row exists (`llm/quoting.py`, shared with the
judged checks). A pre-filled answer is stored as `source: proposed`, shows the words behind it, and
does not satisfy a required question until a person saves it — so drafting still waits for a human,
exactly as it did before intake existed.

**docx has two paths**, because a rule builder should not have to produce a Word template before
anyone can get a document out, and should be able to when branding matters. `render_plain` builds
a clean document directly — real heading styles, real Word tables, no setup. `render_with_template`
merges the same content into the rule builder's `.docx`, where logo, fonts and CI colours already
live. Template tags are linted against spec keys at upload, including tags in table cells and
headers, so a template naming a renamed section fails then rather than at export time.

Step 3 is the checkpoint that matters. If a 4D cannot be walked from start to finish with
hand-typed content and no model involved, something was built in the wrong order — and every later
step will be debugged through an LLM that makes everything non-deterministic.

## 15. A structured spec editor, if it is ever wanted

The shipped editor is YAML plus an honest validator ([§14](#14-build-order)). That is the right
trade while the rule builders are the people who wrote the spec in the first place. It stops being
the right trade the moment a quality manager who has never seen YAML is expected to define a
document type — and this is what that would actually take.

### 15.1 What it is, concretely

Five object types, and one of them carries the weight:

| Form | Shape | Difficulty |
|---|---|---|
| Section | key, title, description, guidance, required, `depends_on`, style | Easy |
| Question | key, prompt, type (5), required, hint, options for `choice` | Easy |
| Block | key, label, kind (5) — plus a row editor for table columns or keyvalue fields | Moderate |
| Quality criterion | id, title, kind (3), scope (document, or a set of sections), rubric or points | Moderate |
| **Requirement** | id, kind (**9**), block, severity, and a different parameter set per kind | **The bulk of the work** |

The requirement builder *is* the product. Everything else is a form over flat fields; this one is a
discriminated union whose parameters must be chosen from what exists elsewhere in the spec:

- `block` — a select over this section's blocks, never a text field.
- `fields` / `field` — a second select over *that block's* columns or fields. Two levels of
  cascade, and the second must repopulate when the first changes (an HTMX swap of the parameter
  fieldset, triggered by the kind and block selects).
- `references` for `cross_ref` — `section.block.column` chosen across the whole spec, which means
  the form needs the entire spec in scope, not just the section being edited.

The payoff is not prettiness. It is that **most linter errors become unreachable instead of
reported**: you cannot name a block that does not exist if you are choosing from a list.

### 15.2 The three things that make it harder than it looks

**A draft must be allowed to be invalid.** Today `doc_types.review()` is all-or-nothing, because a
textarea is submitted whole. A structured editor is a sequence of small edits, and the intermediate
states are legitimately broken — you add a section before its blocks, a requirement before the
column it checks. So a draft is stored as **raw JSON, not a validated `DocTypeSpec`**, validation
moves from "on submit" to "continuously, per field", and the errors must be *anchored*:

> `LintError` needs a structural `path` (`("sections", 1, "requirements", 0, "block")`) beside its
> human-readable `where`. Pydantic errors already carry `loc`. This is the single highest-value
> piece of preparation and it is small — an afternoon — because it makes every later UI decision
> about *where to show an error* a lookup rather than a guess.

**Keys are identity, and a form makes renaming look like editing a label.** A section key appears in
`depends_on`, in every requirement's `block`, inside `cross_ref` strings, in docx template tags, in
harvested exemplars, and in the `section`/`block`/`answer` rows of every document. In YAML, renaming
one is visibly a refactor and the linter catches what breaks inside the spec. In a text input next
to "Title", it looks like a typo fix. So keys should be **derived from the title when an object is
created, then locked** behind an explicit *Rename* action that rewrites every reference in the spec
and warns that docx templates naming the old key will fail their next lint.

**Where the draft lives is an architectural decision, not a detail.** Two options, and they are not
equivalent:

| | Client-held draft | Server-held draft |
|---|---|---|
| Edit | Local, instant | `POST` per edit, HTML swapped back |
| Lost tab | Work lost | Work survives |
| Consistency with the app | Contradicts [§1](#why-not-a-split-frontend-and-backend) | The same bet as everything else |
| Cost | No migration | `doc_type_draft` table, lifecycle, conflict handling |

Take the server-held one. A `doc_type_draft` row (`spec` JSONB, `based_on` version, `updated_at`)
also buys resumability and, later, more than one person editing a type — and the moment two people
do, you need an optimistic `updated_at` check per edit that answers with a conflict fragment rather
than letting the last write win in silence. Design that in; retrofitting it means auditing every
edit route.

### 15.3 What a rule builder actually needs beyond forms

Filling in forms does not tell anyone whether a document type is any good. Five things do, and three
of them are nearly free because the functions already exist:

| Feature | What it gives | Cost |
|---|---|---|
| Live prose view of the draft | The `/doc-types/{key}/versions/{v}` read view, rendered from the draft as it is edited | Small — the template exists |
| "This is what the assistant will be told" | `describe_requirement()` rendered live beneath a judged requirement's rubric field, as they type it | Small — the function exists, and it makes [DESIGN §5.8](DESIGN.md#58-instructing-the-model) tangible instead of theoretical |
| The assembled prompt, read-only | [DESIGN §5.8](DESIGN.md#58-instructing-the-model) already promises this and it is not built. `draft_block` composes the system message; exposing it costs a route | Small |
| A scratch document from the draft | The only real test of a type: walk it. Needs a document to pin something unpublished — cleanest as a version flagged `draft`, hidden from the creator's type list | Moderate |
| Diff against the version it is based on | People will not think in versions once there is autosave; show them what changed | Moderate — a YAML text diff is honest and cheap; a structural diff is neither |

### 15.4 Things to watch out for

1. **No free-text prompt field. Ever.** In a form editor the temptation is overwhelming — an empty
   *Additional instructions for the assistant* textarea is one commit away, and it dissolves
   [DESIGN §5.8](DESIGN.md#58-instructing-the-model) completely. The checks are the instructions;
   the only free text a rule builder writes is a **rubric**, which is a check, and is graded.
2. **Publishing must stay additive and deliberate.** Autosave belongs to drafts. If autosave ever
   creates versions, `doc_type_version` becomes a keystroke log and pinning stops meaning anything.
3. **Keep the YAML path authoritative.** The form and the YAML are two views of one model, and a
   round-trip test — form-edited draft → YAML → parse → equal — is what keeps them honest. Specs
   are meant to be git-tracked and reviewed by people; losing that to a database-only editor would
   be a real regression.
4. **The requirement kinds are a closed vocabulary on purpose** ([Deliberately absent](#deliberately-absent)).
   A form editor invites "just one more kind" per request, because adding one looks like adding a
   `<select>` option. Every kind is also a check implementation, a schema, a description sentence
   and a test.
5. **`depends_on` is a graph, and a form will let someone draw a cycle.** The linter catches it; the
   UI should refuse it at the point of clicking, by offering only sections that cannot create one.
6. **Severity needs plain language.** `blocker` and `warning` mean "cannot export without writing
   down why" and "flagged, not blocking" — say that in the form, not in a tooltip.

### 15.5 Effort, honestly

Path-anchored errors and the draft table are small. The section, question and block forms are
ordinary work. The requirement builder with its cascading selects is most of it, and the preview
features are what make the difference between a form and a tool. Altogether it is comparable to
everything the creator's side has taken so far — which is the honest reason it is not built yet, and
the reason the YAML editor plus a validator that tells the whole truth is a defensible place to
stop until someone who cannot read YAML actually needs to define a type.
