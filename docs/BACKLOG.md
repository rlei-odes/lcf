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

Blocks the "does this actually work for a 4D with defect photographs" question, which is most 4Ds.

## 2. The editor island

Step 6, and the only substantial JavaScript the design calls for. Nothing exists —
`assets/` is not in the repo, and `web/static/` holds only small page scripts.

- TipTap (ProseMirror) per prose block, restricted to the markdown subset by its schema
- Span proposals as decorations, with a hover card
- `suggest_span` LLM call — also not built
- Blame view from a server-computed diff

See [ARCHITECTURE §6](ARCHITECTURE.md#6-the-editor-island). Block-level accept/reject works today,
so this is a refinement of a working flow rather than a gap in it.

## 3. Markdown normalisation

[DESIGN §9](DESIGN.md#9-markdown-integrity) specifies three mechanisms for keeping prose inside the
subset. The second — parse every LLM output to an AST server-side and re-render it canonically,
dropping anything outside the subset — is not built. `src/lcf/markdown/` does not exist.

`markdown-it-py` is already a dependency (added for docx RichText), so the parser is in place.

When this lands, `render/docx.py`'s `_rich_paragraphs` should consume the normalised AST rather
than parsing the source itself.

## 4. Context budget

[ARCHITECTURE §5.3](ARCHITECTURE.md#53-context-budget) says the budget check is "a token count
against a configured window, not a guess". It is currently neither — nothing counts tokens.

- `LCF_LLM_CONTEXT_WINDOW` is configured and unread
- No tokenizer dependency (`tiktoken` or the model's own)
- The `summarize` call, which is what a section falls back to when raw evidence does not fit, is
  not built

Matters when a document accumulates enough evidence that a section's context stops fitting. Has not
bitten yet.

## 5. Exemplars

The data model has an `exemplar` table and `llm/calls.py:60` marks where they belong in the prompt.
Neither harvesting accepted content nor injecting it is built, and neither is the n-gram leakage
check from [ARCHITECTURE §5.4](ARCHITECTURE.md#exemplar-leakage).

Worth doing only once there is enough accepted content in one deployment to harvest from.

## 6. House style UI

`LCF_DOCX_BASE_TEMPLATE` is a filesystem path set per installation. Replacing it with an upload is
a route, a well-known bucket key in `lcf-templates`, and a card — `services/templates.py` already
has the shape from the per-type template.

Open question worth settling first: where it belongs. It is installation-wide, not per document
type, and there is no settings page to put it on.

## 7. Structured spec editor follow-ups

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

## 8. Known issues

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

## 9. Deliberately deferred

Not forgotten — decided against for now, with the condition that would change the answer.

| | Until |
|---|---|
| Per-document docx template | Somebody asks for a customer-specific variant of one document type |
| Worker in its own process | Load needs it. A deployment change, not a rewrite: `SKIP LOCKED` and `LISTEN/NOTIFY` become worth adding then ([ARCHITECTURE §7](ARCHITECTURE.md#7-jobs-and-streaming)) |
| PDF/DOCX import (`docling`, `markitdown`) | File import stops being optional ([DESIGN §6.1](DESIGN.md)) |
| Git sync (`dulwich`) | YAML export/import proves insufficient |
| `prosemirror-changeset` | Accept/reject needs to survive concurrent edits |
