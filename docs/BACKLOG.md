# Lancy Content Flow — Backlog

> Companions: [DESIGN.md](DESIGN.md) · [ARCHITECTURE.md](ARCHITECTURE.md)

What is not built. [ARCHITECTURE.md](ARCHITECTURE.md) describes what is — when something here
ships, its description moves there and its entry leaves this file.

Ordered by what unblocks the most, not by size.

---

## 1. Image evidence in a document

The evidence desk finds images in a pile, deduplicates them, captions them and lets a person keep or
drop each one ([EVIDENCE-DESK §7](EVIDENCE-DESK.md#7-images)). So upload, `caption_images`, the
per-call budget and Pillow are all built — what is not built is the other end: an accepted image
becoming an `image_ref` block in a document.

| Piece | State |
|---|---|
| Upload, dedupe, thumbnails, captioning | **Built**, in the evidence desk |
| `image_ref` blocks in export | Render as `[image] caption` text in **both** docx paths, and as text in Markdown |
| An accepted image reaching a document | Not built. Needs the seam in [EVIDENCE-DESK §11](EVIDENCE-DESK.md#11-the-seam-to-the-rest-of-the-app), or a direct upload on the section surface |

The renderer half is the smaller one and now has bytes to place: docxtpl has `InlineImage`, and
`BlockValue.rows` already carries the caption dicts, so it is a change to `_write_block` and
`_write_tags` plus a decision about where the image comes from.

## 2. Findings into a document

The last piece of [EVIDENCE-DESK §11](EVIDENCE-DESK.md#11-the-seam-to-the-rest-of-the-app), and the
smallest: a button under *Download findings* that starts a document from them. Every part exists —
`doc_types.list_types` for the selector, `documents.create`, `intake.record`, and the `intake` job —
so it is one route, one form and no schema change.

It works because `extraction.as_markdown` already emits every accepted value with the passage it came
from, which is what `map_evidence_to_sections` requires. The case keeps no link to the document
([EVIDENCE-DESK §4.6](EVIDENCE-DESK.md#46-a-schema-level-below-evidence_item)); the export's own
heading names the case.

The open question is empirical: whether `map_evidence_to_sections` files that shape well, since it was
built for loose notes rather than for a structured list. If it does not, the export format is what
changes.

## 3. Summarising a pile

Extraction answers questions that were asked. *"What does this say?"* is a different request, and the
two ways to serve it are not the same feature:

| | What it gives | Verifiable | May reach a document |
|---|---|---|---|
| **A digest** | Several claims per source, each with a verbatim quote | Yes, by `quoted_from` | Yes |
| **An overview** | Prose, for deciding which file to open | No | **No** |

The digest is the smaller build and the one that fits: it is `answer_from_chunk` asked for the notable
claims rather than one value, returning a list. Candidate storage, deduplication, the review surface
and the export all work unchanged — the only gap is that the call returns one value per passage.

The overview must never reach a document. In an intake box, `map_evidence_to_sections` would file
model-written sentences as the author's evidence and `quoted_from` would verify them against the
summary rather than the source, which is [invariant I](DESIGN.md#i-content-exists-only-after-a-human-accepted-it)
broken silently.

Whichever is built: summarise **per source**, not per case. A source never changes, so its summary is
computed once and stored on its row; the case-level one folds those, bounded by file count rather than
passage count. And fold only when a source does not fit a single call — most do, which makes the
recursion a fallback rather than the design. That fit check is [§6](#6-context-budget)'s tokenizer,
earning itself.

## 4. The editor island

Step 6, and the only substantial JavaScript the design calls for. Nothing exists —
`assets/` is not in the repo, and `web/static/` holds only small page scripts.

- TipTap (ProseMirror) per prose block, restricted to the markdown subset by its schema
- Span proposals as decorations, with a hover card
- `suggest_span` LLM call — also not built
- Blame view from a server-computed diff

See [ARCHITECTURE §6](ARCHITECTURE.md#6-the-editor-island). Block-level accept/reject works today,
so this is a refinement of a working flow rather than a gap in it.

## 5. Markdown normalisation

[DESIGN §9](DESIGN.md#9-markdown-integrity) specifies three mechanisms for keeping prose inside the
subset. The second — parse every LLM output to an AST server-side and re-render it canonically,
dropping anything outside the subset — is not built. `src/lcf/markdown/` does not exist.

`markdown-it-py` is already a dependency (added for docx RichText), so the parser is in place.

When this lands, `render/docx.py`'s `_rich_paragraphs` should consume the normalised AST rather
than parsing the source itself.

## 6. Context budget

[ARCHITECTURE §5.3](ARCHITECTURE.md#53-context-budget) says the budget check is "a token count
against a configured window, not a guess". It is currently neither — nothing counts tokens.

- `LCF_LLM_CONTEXT_WINDOW` is configured and unread
- No tokenizer dependency (`tiktoken` or the model's own)
- The `summarize` call, which is what a section falls back to when raw evidence does not fit, is
  not built

Matters when a document accumulates enough evidence that a section's context stops fitting. The
evidence desk hits the same limit from the other end and settled for an approximation: its chunk
budget is in characters, set low enough that the difference cannot matter
([EVIDENCE-DESK §4.5](EVIDENCE-DESK.md#45-chunking-and-what-a-chunk-carries)). One tokenizer would
serve both, and would replace that bound rather than being retrofitted around it.

## 7. Exemplars

The data model has an `exemplar` table and `llm/calls.py:60` marks where they belong in the prompt.
Neither harvesting accepted content nor injecting it is built, and neither is the n-gram leakage
check from [ARCHITECTURE §5.4](ARCHITECTURE.md#exemplar-leakage).

Worth doing only once there is enough accepted content in one deployment to harvest from.

## 8. Structured spec editor follow-ups

From [ARCHITECTURE §15.5](ARCHITECTURE.md#155-what-it-cost-and-what-it-bought):

- **A shorter path for the plainest type.** A section holding one prose block named after itself
  should collapse to a single line, with questions and checks folded behind "add rules to this
  section". Presentational; do this before considering a separate simple mode, which would be two
  things to keep true over one model.
- **Reordering.** `move_*` operations exist and are tested; no UI. Per-row arrows were built and
  removed as clutter. Drag, or a reorder mode showing the whole list, is the real answer.
- **A scratch document from a draft.** The only real test of a type is walking it. Needs a document
  to pin something unpublished — cleanest as a version flagged `draft`, hidden from the creator's
  type list. Schema change.
- **A diff against the version a draft is based on.** People stop thinking in versions once there is
  autosave. A text diff against `based_on` is the honest first version.

## 9. Accounts and roles

There is no authentication. Anyone who can reach the application can use all of it, including
`/setup`, which writes the database URL, the S3 credentials, the LLM endpoint and the house style
into the configuration file.

What bounds that today is the `WRITABLE` allowlist in `services/setup.py` — a key not on it cannot be
set over HTTP whatever the form contains — and the refusal to repoint a database that is already
live. Both are limits on *what* can be written, not on *who* may write it.

The deployment this is built for is a private network, which is the assumption the whole design rests
on ([DESIGN §11](DESIGN.md#11-relationship-to-lancy)): local models, no egress, no cloud. A role
concept is the thing that would let it be exposed more widely, and the smallest useful version is one
role — administrator — gating `/setup` and `/admin`, since every other page is already the work
itself rather than the configuration of it.

Worth noting what it would *not* fix: the LLM endpoint is settable from that page, so an attacker who
reaches it can point drafting at a server they control and have the author's material sent there.
That makes the endpoint setting the most sensitive thing on the page, ahead of the database URL.

## 10. Known issues

**LibreOffice warns "non-standard file format" on generated `.docx` files.** The file opens, edits
and round-trips correctly. Verified about the generated starter: it is a valid OPC package,
`[Content_Types].xml` is the first entry, 17 parts, and re-uploading an edited copy lints and
renders. The likely cause is python-docx's default template, which carries `stylesWithEffects.xml`
— a Word 2010 transitional part. Needs reproducing against LibreOffice to confirm. Cosmetic, but it
is the first thing a rule builder sees.

**Starter template loop tags read as clutter.** `{%tr %}` and `{%p %}` markers sit in their own
table rows and paragraphs. That is the docxtpl form that survives an author restyling the table,
but it makes the file look noisier than the document it produces. A more compact idiom is worth
testing against a restyled table before adopting.

**The markdown subset is enforced on the way out but not on the way in.** See
[§5](#5-markdown-normalisation). Nothing else in [ARCHITECTURE §3](ARCHITECTURE.md#3-repository-layout)
names a module that does not exist any more: `evidence/` and `jobs/` live inside `services/` and are
unlikely to move, `schemas/` is `llm/schemas.py`, and `ingest/` arrived with the evidence desk.

## 11. Deliberately deferred

Not forgotten — decided against for now, with the condition that would change the answer.

| | Until |
|---|---|
| Per-document docx template | Somebody asks for a customer-specific variant of one document type |
| A lancy dependency for ingest | **Decided against.** Parsing, chunking and batch execution all happen in this service, so [DESIGN §11](DESIGN.md#11-relationship-to-lancy) holds unchanged: shared name, shared taste, no shared code. The condition that would reopen it is a corpus large enough that a case cannot be searched in one process — which is a different product from one complaint's worth of files |
| Embeddings in the evidence desk | A question routinely goes unanswered because its wording shares nothing with the material *and* the model reading the top passages does not find it either. Stemming and keyword expansion close most of that gap ([EVIDENCE-DESK §6.4](EVIDENCE-DESK.md#64-ranking-the-chunks)) |
| Worker in its own process | Load needs it. A deployment change, not a rewrite: `SKIP LOCKED` and `LISTEN/NOTIFY` become worth adding then ([ARCHITECTURE §7](ARCHITECTURE.md#7-jobs-and-streaming)) |
| Git sync (`dulwich`) | YAML export/import proves insufficient |
| `prosemirror-changeset` | Accept/reject needs to survive concurrent edits |


## 12. Notes from use case discussions

- Extract Requirements from input documents. A sentence saying "and" would have to be split in two reqs
- Give Feedback on a draft, ask a re-write
- Mark the text that is fine, and where we want a re-write. Dialog / Interaktivität
- Or re-run, for a new proposal
- New type of Quality criteria: Typ "Have you thought about this?" - Just as input, maybe as a bubble type on the right?