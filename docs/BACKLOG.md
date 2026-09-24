# Lancy Content Flow — Backlog

> Companions: [DESIGN.md](DESIGN.md) · [ARCHITECTURE.md](ARCHITECTURE.md)

What is not built. [ARCHITECTURE.md](ARCHITECTURE.md) describes what is — when something here
ships, its description moves there and its entry leaves this file.

Ordered by what unblocks the most, not by size.

---

## 1. Image evidence: upload and captioning

Step 7's other half. Text intake is complete; images are not started.

| Piece | State |
|---|---|
| Upload to `lcf-uploads` | Not built. The bucket is created on startup and never written to |
| `caption_images` LLM call | Not built. One of three missing from the taxonomy ([ARCHITECTURE §5.2](ARCHITECTURE.md#52-call-taxonomy)) |
| Per-call image budget | `LCF_LLM_MAX_IMAGES_PER_CALL` is configured and unread |
| Pillow resize/normalise | Not a dependency yet |
| `image_ref` blocks in export | Render as `[image] caption` text in **both** docx paths, and as text in Markdown |

The renderer half is the smaller one and depends entirely on this: docxtpl has `InlineImage`, and
`BlockValue.rows` already carries the caption dicts, so placing real images is a change to
`_write_block` and `_write_tags` once there are bytes to place.

Blocks the "does this actually work for a 4D with defect photographs" question, which is most 4Ds,
and is the prerequisite for the image tray in
[§3](#3-extraction-commands-and-candidate-review) — images pulled out of a PDF need the
same upload, caption and placement path as images a creator uploads by hand.

## 2. Ingest: files, chunks, provenance

Intake takes pasted text and assumes it fits the model's context window. Real material often does
not: dozen-page defect reports, PDFs, Word files, email threads with their attachments. Pasting is
the right gesture for a handful of notes and the wrong one for a corpus.

**The slice that delivers on its own** is smaller than it looks: accept files, turn each into ordered
chunks, and run the existing `map_evidence_to_sections` once per chunk, unioning the results. No new
LLM call, no new vocabulary, no new review surface — the material simply stops having to fit in one
prompt. [§3](#3-extraction-commands-and-candidate-review) is what makes it powerful, and is separable.

**Inputs.** PDF, `.docx`, `.eml`, images, and paste as it works today. Dropping files onto a field is
Alpine and a few lines; it is not the editor island and should not grow into it.

**Parsing.** `docling` or `markitdown` for PDF and Word. Email is the messiest input and the likeliest
to arrive: quoted reply chains, signatures, disclaimers, nested forwards. How well that one parser
strips boilerplate is what the perceived quality of the whole feature will rest on — a chunk that is
90% legal footer is a chunk that poisons every extraction run over it.

**Chunks carry where they came from** — file, page, position. A mapping is a quotation plus a section
key, and `llm/quoting.py` verifies the quotation against its source before the row exists. That check
has to keep working when the source no longer fits in one prompt, which means the chunk it was found
in must be identifiable.

**A schema level that does not exist yet.** `evidence_item` is one row per paste, verbatim and never
edited. A file that becomes forty chunks needs a source → chunk relationship, and a decision about
whether a chunk *is* an `evidence_item` or something below one. Every provenance claim above depends
on this, so it is the first thing to settle after the parsing question.

**Images come out of the files too**, not only from direct upload, and land in the tray described in
[§1](#1-image-evidence-upload-and-captioning).

### Open question

**Where parsing and chunking live.** Lancy already does batch processing over chunks.
[DESIGN §11](DESIGN.md#11-relationship-to-lancy) says lancy could one day supply input, that no
interface exists and none is planned; this would be the first real reason to build one. Against it:
[ARCHITECTURE §1](ARCHITECTURE.md#1-shape-one-service) is one service on the local network with no
external dependency. The choice is not all-or-nothing — parsing and batch execution are separable, so
parsing locally while never needing lancy is a real option, and so is using lancy purely as the batch
executor for [§3](#3-extraction-commands-and-candidate-review).

## 3. Extraction: commands and candidate review

The reason ingest is worth building. A question in a doc type can carry the command that finds its
answer, so a recurring document type arrives pre-wired: the report lands and the answers the material
can supply are already found. The second identical twelve-page report is then cheaper than the first,
which is the case worth optimising.

### A closed vocabulary, not a free-text command

Commands follow the shape `Requirement` already uses — one model, `kind` from a closed frozenset,
per-kind required parameters, split by whether a model is needed:

| kind | Parameters | Needs a model |
|---|---|---|
| `pattern` | `pattern` | No |
| `keyword_ask` | `keywords`, `ask` | Only for chunks that hit |
| `ask` | `ask` | Every chunk |

The deterministic tier is not an optimisation, it is the same bias as everywhere else in the engine: a
complaint number like `NW-CL-88213` has a shape, a regex finds it exactly and is testable without a
model, and [ARCHITECTURE §13](ARCHITECTURE.md#13-testing) exists to keep the parts that must be right
out of the model's hands. Keyword narrowing is what makes the cost bearable — forty chunks reduced to
three candidates is three calls, not forty. And a pattern or keyword hit *is* a character offset, so
the lower tiers need no quote verification at all.

Keeping the vocabulary closed also settles what a free-text command would not: `ask` is a question
about supplied material and cannot influence how anything is written, which is a narrower thing than
the *additional instructions* box [DESIGN §5.8](DESIGN.md#58-instructing-the-model) forbids. A
free-text command field would have reopened that rule by precedent.

### Candidates, ranked, assigned by a person

Extraction does not answer a question — it offers candidates for it. Each question surfaces its hits
ordered by score, with the source passage behind each one, and the creator accepts one or several.
Assembling an answer from three hits across two files has to be a click, because the alternative is
reading twenty pages to find where the reference was.

This generalises what prefilled answers already do: an intake-proposed answer is stored
`source: proposed`, shows the words behind it, and does not satisfy a required question until a person
saves it. A candidate list is the same contract with more than one option, so extraction proposes and
never writes and [invariant I](DESIGN.md#i-content-exists-only-after-a-human-accepted-it) is
untouched.

The same surface serves images: extracted images sit as a strip of cards and are assigned to a
section by clicking, not by dragging. Same interaction, same reason — the tray holds what was found,
a person decides where it belongs.

### Open questions

**What the score means across tiers.** A `pattern` hit is exact, a `keyword_ask` hit has a count and a
proximity, an `ask` hit has the model's stated confidence. These are not the same quantity and ranking
them in one list without saying so would present a guess and a certainty as peers.

**How a candidate becomes an answer.** Reusing `answer.source = proposed` costs no schema, but a
candidate that was *not* chosen still has value — it is the audit trail of what the material offered,
the way a rejected `proposal` row survives its outcome. That argues for candidates being their own
rows rather than transient.

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

Matters when a document accumulates enough evidence that a section's context stops fitting. This is
the same limit [§2](#2-ingest-files-chunks-provenance) hits from the other
end — that one is about material too large to paste, this one about context too full to send. A
tokenizer serves both.

## 7. Exemplars

The data model has an `exemplar` table and `llm/calls.py:60` marks where they belong in the prompt.
Neither harvesting accepted content nor injecting it is built, and neither is the n-gram leakage
check from [ARCHITECTURE §5.4](ARCHITECTURE.md#exemplar-leakage).

Worth doing only once there is enough accepted content in one deployment to harvest from.

## 8. House style UI

`LCF_DOCX_BASE_TEMPLATE` is a filesystem path set per installation. Replacing it with an upload is
a route, a well-known bucket key in `lcf-templates`, and a card — `services/templates.py` already
has the shape from the per-type template.

Open question worth settling first: where it belongs. It is installation-wide, not per document
type, and there is no settings page to put it on.

## 9. Structured spec editor follow-ups

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

**`ARCHITECTURE.md` §3 lists modules that do not exist** — `markdown/`, `evidence/`, `jobs/`,
`schemas/`. Some are items on this list; `evidence/` and `jobs/` live inside `services/` instead and
are unlikely to move. The layout should describe the repository.

## 11. Deliberately deferred

Not forgotten — decided against for now, with the condition that would change the answer.

| | Until |
|---|---|
| Per-document docx template | Somebody asks for a customer-specific variant of one document type |
| A lancy dependency for ingest | [§2](#2-ingest-files-chunks-provenance) decides it. Today [DESIGN §11](DESIGN.md#11-relationship-to-lancy) holds: shared name, shared taste, no shared code |
| Worker in its own process | Load needs it. A deployment change, not a rewrite: `SKIP LOCKED` and `LISTEN/NOTIFY` become worth adding then ([ARCHITECTURE §7](ARCHITECTURE.md#7-jobs-and-streaming)) |
| Git sync (`dulwich`) | YAML export/import proves insufficient |
| `prosemirror-changeset` | Accept/reject needs to survive concurrent edits |
