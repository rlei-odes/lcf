# The Evidence Desk

> Status: **built** · 2026-10-03 · companions: [DESIGN.md](DESIGN.md) ·
> [ARCHITECTURE.md](ARCHITECTURE.md) · [BACKLOG.md](BACKLOG.md)
>
> The fourth area of the application, and the one place that reads files. It has a document of its
> own rather than a section of [ARCHITECTURE.md](ARCHITECTURE.md) because it is a self-contained
> half of the product with its own schema, its own service modules and its own vocabulary; that
> document describes it in [§17](ARCHITECTURE.md#17-the-evidence-desk) and points here.

## 1. The problem, as it actually arrives

A quality complaint does not arrive as a document. It arrives as a flood: the customer's complaint
form, a measurement report from their incoming inspection, photographs, a mail thread with four
forwards in it, a delivery note, a drawing extract. Every customer's template is different, and none
of them is ours.

Before anybody can write a 4D, somebody has to answer a handful of flat questions out of that pile:

> Which part is affected? Which batch — or batches? What is the customer's complaint number? What
> did they measure, and against what tolerance? How many parts, out of how many? When did they
> notice?

Today that is a person opening eight files and reading twenty pages to find six numbers. The flow
area has no answer for it: intake takes a paste and assumes it fits one prompt
([DESIGN §6.1](DESIGN.md#61-intake)), which is the right gesture for a handful of notes and the
wrong one for a corpus.

The **evidence desk** is a fourth area of the application whose whole job is that pile. Three moves,
in the order a person makes them:

| | | |
|---|---|---|
| **Gather** | Drop the files in. Paste what is only in an email. | The heap stops being eight applications |
| **Formulate** | Say what has to come out of it, once, and save that for next time | The questions become reusable |
| **Find** | The desk proposes candidates with the passage behind each one; a person accepts | Six numbers, not twenty pages |

The second move is the one that compounds. The second identical twelve-page report from the same
customer is cheaper than the first, because the questions are already written and the patterns that
find their answers are already proven against real material.

### What it deliberately is not

It is **not** retrieval, and not a chat window over a corpus. It answers a fixed, authored list of
questions and offers candidates for each. [DESIGN §12](DESIGN.md#12-non-goals-for-v1) rules out
embeddings and a vector store; nothing here needs them, and §6.4 below says why keyword and lexical
ranking are enough at this size.

It also **writes no document**. It produces findings. Carrying a finding into a 4D is a separate
step, and in this version it is a person copying an export — see [§11](#11-the-seam-to-the-rest-of-the-app).

## 2. Independent, with one door in

Built as its own area, its own tables, its own service module, reachable from the app bar beside
*Doc flow*, *Doctype factory* and *Admin*:

```
AREAS = (
    ("flow",     "Doc flow",         "/"),
    ("evidence", "Evidence desk",    "/evidence"),
    ("factory",  "Doctype factory",  "/doc-types"),
    ("admin",    "Admin",            "/admin"),
)
```

Independence is a design constraint, not an accident of sequencing. Two reasons:

- **It is useful on its own, today.** Pulling six numbers out of a document flood is worth doing
  even if nothing is written afterwards. A feature that only pays off once a second feature exists
  is a feature that gets abandoned halfway.
- **The engine must be proven before it is wired in.** Automatically running extraction commands
  from a document type's questions ([§11](#11-the-seam-to-the-rest-of-the-app)) means a type author
  can make every document in the organisation ask the assistant forty times. That seam should be
  opened once the cost and the hit quality are known from real use, not before.

What it *shares* is everything structural: `services/` as the contract, `jobs` for slow work,
`storage/` for bytes, `llm/provider.py` for calls, `llm/quoting.py` for the rule that a model may
only point at words a person supplied.

## 3. Vocabulary

Six nouns. They are chosen so that nothing in the desk is called the same thing as something in the
flow area, because `evidence_item` already means "one paste on one document" and must keep meaning
only that.

| Noun | Is | Lives for |
|---|---|---|
| **Case** | One pile of material about one problem | As long as the problem does |
| **Source** | One file, one paste, or one pasted image | Its case |
| **Chunk** | An ordered, provenance-carrying slice of one source | Its source |
| **Question** | Something that has to come out of the pile, plus the commands that find it | Its case |
| **Candidate** | One proposed answer to one question, with the passage behind it | Its question, including after it is dismissed |
| **Run** | One pass over the pile, and what each stage of it did | Its case — kept, so two runs can be compared ([§6.9](#69-a-run-shows-its-work)) |

And one reusable thing beside them: a **question set** — a named list of questions and their
commands, saved so the next complaint from the same customer starts pre-wired. *"Kunde Nordwerk —
Reklamation"* is a question set.

Images are **assets**, held on the case, found inside sources or pasted directly.

## 4. Gather

### 4.1 What comes in

| Input | Handled by | Notes |
|---|---|---|
| `.pdf` | `pypdf` | Per-page text and embedded images, pure Python, no model weights |
| `.docx` | `python-docx` | Already a dependency. Paragraphs, tables, embedded images |
| `.eml` | stdlib `email` + `mail-parser-reply` | Thread split, signature and disclaimer stripping; attachments recursed into; **who sent it** ([§4.3](#43-email-is-the-input-with-a-sender)) |
| `.txt` `.md` `.csv` | directly | A paste is this, with no filename |
| `.png` `.jpg` `.webp` `.tif` | Pillow | Straight to the asset tray |
| paste | directly | Exactly today's intake gesture, kept |

Dropping files on a field is a small vanilla script in `static/` beside the other four. It is not the
editor island and must not grow into one. The same script handles an image pasted from the clipboard,
which is how a screenshot of a measurement actually arrives.

Every source also gets its **language detected** on parse, which is what makes the lexical tier work
on German material — see [§4.4](#44-language-is-a-property-of-the-source).

**The same bytes twice are one source.** A `.eml` brings its attachments in as sources of their own,
so dropping the measurement report in directly as well is the ordinary way a case ends up holding one
document twice — and every finding in it reported twice, from two places that look identical because
they carry the same filename. So a source is digested on arrival, the same rule images have had from
the start ([§7](#7-images)), and bytes already in the case make a **repeat**: listed, because the pile
should show what was dropped, and otherwise inert — never parsed, no passages, no images, nothing to
search.

It is a status rather than a refusal for two reasons. A person who drops a file and sees nothing
happen will drop it again; and the copy has to be able to become the real one, which it does when the
source it repeats is removed. Not a unique constraint, for the same reason: the row has to exist to
say it is a repeat.

### 4.2 The parsing decision

The survey, for the record, because this choice determines the perceived quality of the whole
feature:

| Option | Strength | Cost |
|---|---|---|
| **docling** (IBM) | Best in class. Layout model, table structure, reading order, OCR for scans, per-element page **and bounding box** provenance | Brings `torch`; downloads 520 MB–1.2 GB of model weights from HuggingFace **on first use** |
| **marker** | Fastest with a GPU, excellent markdown | GPL — needs a licence review before commercial use; GPU or it is slow |
| **markitdown** (Microsoft) | MIT, tiny, no weights, broad format list | PDF via text extraction only: no page provenance per element, weak tables |
| **pypdf + python-docx + stdlib email** | No weights, no egress, per-page provenance, every part testable | Digital PDFs only. A scanned PDF yields nothing |

**The default path is the fourth row, and docling is something an installation adds itself.**

That is not a quality judgement against docling — it is the better parser. It is
[ARCHITECTURE §1](ARCHITECTURE.md#1-shape-one-service): *everything runs on the local network, no
cloud dependency, no egress*. A default parser that reaches HuggingFace the first time somebody drops
a PDF breaks that promise on the one deployment the product is built for, and "pre-fetch the weights"
is a setup step that fails silently into "the desk does nothing".

So parsing dispatches on media type and extension to one of four modules, and `ingest/parse.py` is
the only thing that knows which. `Parsed` carries the units, their pages, the images found, the files
that came attached, and — for mail — who sent it. `LCF_INGEST_PARSER` selects `builtin`, `docling`,
or `auto` (the default), which prefers docling for PDFs when it imports and falls back without
complaint when it does not.

**docling is not declared as an optional extra**, which is the obvious place for one. `uv` resolves
every declared extra into one universal lock, so declaring it made docling's constraints move
versions for installations that would never install it — it downgraded `websockets`, which uvicorn
uses, on every deployment. An optional capability must not be able to do that. So it is imported
lazily behind `docling_backend.available()`, and an installation that wants layout-aware parsing or
OCR runs `uv pip install docling` into the same environment. Nothing about the default install
changes, and the lock stays a statement about what the application actually needs.

Two details the built-in PDF path gets right, because the alternative in each case is silent:

- **A scan produces no characters**, and saying so beats recording a parsed source with no content —
  which is indistinguishable from a file that genuinely says nothing. Judged per page rather than
  per document, because a one-page cover note is legitimately short and a forty-page scan is
  legitimately empty. The source goes to `failed` saying it looks like a scan, and naming the two
  ways forward.
- **A line-ending hyphen is ambiguous, and the two cases pull opposite ways.** `Eingangs-\nprüfung`
  wants the hyphen gone; `NW-CL-\n88213` wants it kept, and dropping it makes the identifier
  invisible to every pattern that was written to find it. What tells them apart is the character the
  next line begins with: German hyphenation breaks before a lowercase continuation, an identifier
  before a digit or a capital. Keeping it is also the safer of the two errors, since hunting
  identifiers is what the whole feature is for.

### 4.3 Email is the input with a sender

Every other format is an anonymous body of text. An email is not, and that difference is worth
schema: **who wrote a passage changes what the passage is worth.**

> *"Die Teile sind innerhalb der Toleranz"* means one thing from `qs@nordwerk.de` — the customer
> conceding a point — and the opposite thing from a colleague in the next building.

So the mail parser lifts, and the desk stores as columns rather than burying in `meta`:

| Field | From | Used for |
|---|---|---|
| `sender` | `From:`, address only, display name kept beside it | The badge on the source and on every candidate found in it |
| `sender_domain` | the address | Telling the customer's material from our own, at a glance and as a filter |
| `sent_at` | `Date:`, parsed to an aware datetime | Ordering the pile by when things were said, which is not when they were uploaded |
| `subject` | `Subject:` | The source's display name, since `.eml` filenames are usually meaningless |
| `recipients` | `To:` / `Cc:`, in `meta` | Who was in the loop. Read, not filtered on |

**A thread is not one source with one sender.** `mail-parser-reply` splits a thread into its
constituent replies, and each reply becomes its own chunk unit carrying **its own** sender and date in
`chunk.meta`. That is the whole reason to bother: a four-deep forward chain is four statements by up
to four people, and a candidate pulled out of it can then say *"from einkauf@nordwerk.de, 12 March"*
rather than *"somewhere in RE RE FW Reklamation.eml"*.

Attachments are recursed into as sources of their own, parented to the mail, inheriting its sender —
which is how the measurement PDF that came with the complaint gets attributed correctly without
anybody re-stating where it came from.

The domain is also the obvious future lever on scoring: a question like *"what is the customer's
complaint number?"* is better answered from the customer's own domain than from an internal
forward. That is **not** built — it would be ranking by a heuristic nobody asked for — but
`sender_domain` being a column rather than a JSON key is what leaves the door open.

### 4.4 Language is a property of the source

A case routinely holds a German complaint, an English measurement report from the same customer's
UK plant, and an internal note in whichever language the engineer types. One language per case would
be wrong, and one per installation wronger.

So **detection runs per source**, on parse, with `py3langid`: a BSD-licensed, modernised fork of
`langid.py` whose model is a single bundled file. No download, no first-run surprise — the same test
the parser choice had to pass. It needs `numpy`, which [§6.4](#64-ranking-the-chunks) now brings
anyway, so the marginal cost is nothing.

`source.language` and `source.language_confidence` then drive three things:

| Consumer | Effect |
|---|---|
| **BM25 tokenisation** | Which stopword list and which Snowball stemmer. German matters most: `Toleranz`, `Toleranzen` and `Toleranzgrenze` are one term to a stemmer and three to a tokeniser |
| **Keyword expansion** | Proposing `Charge` and `Fertigungslos` rather than `batch` and `lot` ([§5.4](#54-keywords-expanded-at-authoring-time)) |
| **The mail parser** | `mail-parser-reply` takes a language list for its separator patterns; passing the detected one beats passing all thirteen |

Two deliberate limits. Detection is **per source, not per chunk** — a thread with a German reply to
an English mail is a real case, and the right answer there is a mixed-language index, not a per-chunk
model call; the stemmer being slightly wrong on a minority of chunks costs a rank position, not a
missed answer. And the detected language is **shown and overridable** on the source, because a
two-line note is exactly the input any detector gets wrong, and a wrong stemmer silently makes a
question look unanswerable.

### 4.5 Chunking, and what a chunk carries

Chunking is structural first and budgeted second:

1. **Split into units** the parser already knows: a heading, a paragraph, a list, one table row, a
   page boundary, one message of a mail thread.
2. **Pack adjacent units** into a chunk up to `LCF_INGEST_CHUNK_CHARS` (default 1800), never
   splitting a unit unless the unit alone exceeds the budget.
3. **Overlap by one unit** when a pack begins mid-section, so a fact sitting across a boundary is in
   some chunk whole. Duplicates are what [§6.5](#65-one-card-per-distinct-value) collapses, which is
   what makes overlap cheap.
4. **Carry the heading trail** — `"3. Measurement results › Table 2"` — because that is what tells a
   model, and a reader, what a bare row of numbers is about.

The budget is in **characters, not tokens**, and that is a knowing approximation. Nothing in the
application counts tokens yet ([BACKLOG §6](BACKLOG.md#6-context-budget)); the budget here is set
conservatively enough that the difference cannot matter, and when a tokenizer lands it replaces this
bound rather than being retrofitted around it.

Every chunk stores `source_id`, `seq`, `page_from`, `page_to`, `path`, `char_from`/`char_to` and a
`meta` carrying whatever the unit knew about itself — for a mail thread, the sender and date of the
reply it came from ([§4.3](#43-email-is-the-input-with-a-sender)). The offsets are
**into the source's own parsed text, which is stored on the source row.** That matters twice.
It makes a quotation verifiable against an identifiable body of text, which is what
[DESIGN §6.1](DESIGN.md#61-intake) requires of `llm/quoting.py` once the source
no longer fits in one prompt. And it gives the one debugging surface a parser needs: *show me what
you actually read*, which the desk offers per source.

### 4.7 Characters that are not text

Material arrives from scanners, Windows mail clients, PDF producers of every vintage and somebody's
clipboard, and it carries characters that are not content. Three kinds, breaking three different
things, all of them quietly — so every backend's output passes through one function
(`ingest/text.py`) before `Parsed.text` is assembled:

| Kind | What it does | Example |
|---|---|---|
| **Unstorable** | Aborts the parse of a whole file. A NUL is valid UTF-8 and a valid Python string, and Postgres refuses it; so is a lone surrogate, which a latin-1 fallback or a `\ud800` escape in model output can both produce | One stray byte and the source reads *could not be read*, with nothing to say why |
| **Invisible** | Defeats the one tier that is supposed to be exact. `NW-CL-882­13` does not match `\bNW-CL-\d{5}\b`, and nothing on screen shows the difference | Soft hyphen, zero-width space, word joiner, stray BOM — PDF extraction emits all of them |
| **Unprintable** | Reaches the prompt, the passage view and the export as noise | Terminal escapes, bells, vertical tabs |

Plus one normalisation: text out of macOS and some PDF producers is NFD, where `Ü` is two code
points that look like one. It deduplicates as a *different* value and fails a pattern written against
the composed form, so text is composed to **NFC**. Not NFKC — that rewrites `²` to `2` and `½` to
`1/2`, and a measurement is not something to silently rewrite on the way in.

Two rules keep the cleaning honest rather than destructive. A **no-break space becomes a space, not
nothing**: German typesetting puts one between a number and its unit, so folding it is what makes
`12,05 mm` matchable, while deleting it would invent `12,05mm`. And the whole pass runs **before
offsets are computed**, so `source_text[char_from:char_to] == chunk.text` is untouched — a test
asserts it on deliberately malformed input, because cleaning after the fact would silently shift
every quotation in the case.

The same guard covers the other two doors untrusted text comes through: what a model returns
(cleaned as the JSON is decoded, so a bad character fails at its cause rather than at the database)
and what a person types or pastes — an example number copied out of a PDF brings its soft hyphens
with it, and a pattern built from it would then match only text carrying the same ones.

### 4.6 A schema level below `evidence_item`

The backlog asks whether a chunk *is* an `evidence_item` or something below one. The answer is
neither: it is in a different table in a different area.

`evidence_item` is one paste on one document, verbatim, never edited, pointed into by
`evidence_link`. That contract is load-bearing in the flow area and nothing here should widen it. A
case is not a document; it may feed several, or none. So the desk gets `evidence_source` and
`evidence_chunk` of its own, and the relationship between a case and a document is deliberately
absent until [§11](#11-the-seam-to-the-rest-of-the-app) is built.

## 5. Formulate

### 5.1 A question has a type

```
question
  case_id, seq, key, prompt,
  type:     text | identifier | number | date | boolean | choice
  options:  [str]                 — choice only
  multiple: bool                  — one value, or a set of them
  commands: [Command]
```

The type vocabulary is **the spec model's `QuestionType`, plus `identifier`**, and that is not
cosmetic reuse. It buys three things:

- **Candidates are type-checked before they are offered.** `llm/calls.py:_fits` already exists for
  exactly this reason: constrained decoding fixes the JSON type but not the shape inside a string, and
  a date question comes back as *"8 September"* often enough to matter. A `date` candidate that does
  not parse as `YYYY-MM-DD` is discarded and counted rather than shown; a `choice` candidate outside
  its options likewise. The same function, the same rule, in a second place that needs it.
- **The question tells the finder what to look for.** A `number` question has its unit and decimal
  comma to cope with (`12,05 mm`); an `identifier` is a token with a shape and so is the tier that
  should find it first; a `date` has a dozen written forms. Type-aware normalisation of candidate
  *values* is deterministic work that would otherwise be asked of a model.
- **The seam to doc types becomes a mapping, not a translation.**
  [§11](#11-the-seam-to-the-rest-of-the-app) wants a spec `Question` to carry commands one day. If the
  desk had a vocabulary of its own, that day would start with a lookup table between two closed
  vocabularies — which is the kind of thing that ends up with five entries and a bug in the sixth.

`identifier` is the one addition, and it earns its place because it is the single most common thing
being hunted: a complaint number, a part number, a batch, a drawing revision. It is a `text` whose
value is a token rather than a sentence, which changes how it is matched (word-bounded, exact,
case-insensitive), how it is deduplicated (never merged across punctuation differences), and which
tier is tried first.

`multiple` is not decoration either. *"Which part number?"* has one answer and a second candidate
means the first is in doubt; *"which batches are affected?"* has four, and the review surface has to
let a person take all four. One is a choice, the other is a collection, and they are different
interactions. This mirrors `image_ref`'s `multiple` in the spec model rather than inventing a second
word for the same idea.

`key` is derived from the prompt and then locked, the same rule and for the same reason as section
keys in the structured spec editor ([ARCHITECTURE §15.2](ARCHITECTURE.md#152-the-three-things-that-make-it-harder-than-it-looks)):
candidates reference it, an export names it, and in a text input beside "Prompt" a rename looks like
a typo fix.

### 5.2 Commands — a closed vocabulary

Exactly the shape `Requirement` already uses: one model, `kind` from a frozenset, per-kind required
parameters, validated by Pydantic.

| kind | Parameters | Model calls | What it is for |
|---|---|---|---|
| `pattern` | `pattern`, `examples` | none | A thing with a shape: `NW-CL-88213`, `LOT-2026-0417`, `Ø 12,05 mm` |
| `keyword_ask` | `keywords`, `ask` | only chunks that hit | A thing described in words near a known word: "tolerance", "Toleranz", "spec" |
| `ask` | `ask` | the top-ranked chunks | A thing with neither a shape nor a reliable neighbour |

A question may carry several commands, and usually does: a complaint number question carries the
pattern *and* a `keyword_ask` on "Reklamation, complaint, claim", because the pattern catches it
exactly where it appears in the expected shape and the keyword catches it where the customer wrote it
differently.

The deterministic tier is not an optimisation. It is the same bias as everywhere else in the engine:
a complaint number has a shape, a regex finds it exactly, and the result is testable without a model
([ARCHITECTURE §13](ARCHITECTURE.md#13-testing)). Keyword narrowing is what makes the model tier
affordable — forty chunks reduced to three candidates is three calls, not forty.

### 5.3 The pattern the assistant writes, and the examples that prove it

A quality engineer should not have to write a regular expression, and this is the one place in the
product where asking a model to write *code* is the right move, because the output is **verifiable
before it is used**.

The flow:

1. The person pastes one or more real examples: `NW-CL-88213`, `NW-CL-90104`.
2. `propose_pattern` returns a regex and a one-line explanation of what it matches.
3. The proposal is **verified deterministically, and rejected if it fails**:

   | Check | Why |
   |---|---|
   | Compiles | Obvious, and the first thing a model gets wrong |
   | `fullmatch` on **every** supplied example | A pattern that does not match what it was built from is not a candidate for anything |
   | Does not match the empty string | Such a pattern matches at every position in every chunk |
   | Length ≤ 200 characters | A pattern nobody can read is a pattern nobody can correct |
   | Runs over the case's own text inside a timeout | See below |

4. If it passes, it is **run over the material already in the case immediately**, and the first
   matches are shown with their source and page. That is the real test, and it is free: the person
   sees *"14 matches across 3 files"* or *"no matches"* before saving anything.
5. If it fails, the pattern is shown anyway, with what went wrong, in an editable field.

The field stays editable on purpose. A regex is a deterministic parameter, not an instruction to a
model, so it does not reopen [DESIGN §5.8](DESIGN.md#58-instructing-the-model) — see
[§9](#9-the-rule-this-feature-brushes-against).

**Catastrophic backtracking is a real hazard here**, because a model-written pattern runs over
megabytes of someone else's text. `re` has no timeout; the `regex` module does
(`regex.finditer(..., timeout=2.0)`), and it is the reason that dependency is on the list. A pattern
that times out is reported against the question — *"this pattern is too slow to run on this
material"* — and not silently dropped.

### 5.4 Keywords, expanded at authoring time

A `keyword_ask` command is only as good as its keyword list, and nobody writes a good one from a cold
start. *"Toleranz"* misses *"Spezifikation"*, *"Sollmaß"*, *"spec"*, *"within tolerance"* and the one
the customer actually used.

So the keywords field carries an **Expand** action: `propose_keywords` returns candidate terms —
synonyms, the other language's equivalents, the abbreviations — each with a one-line reason, and the
person **ticks the ones to keep**. The detected language of the material in the case
([§4.4](#44-language-is-a-property-of-the-source)) goes into the call, so a German pile is offered
`Charge` and `Fertigungslos` rather than `batch` and `lot`.

Two properties of doing it at authoring time rather than at run time, and they are the whole reason
for the design:

| | |
|---|---|
| **Run time stays deterministic** | The keyword list is data on the command. Two runs over the same material select the same chunks, and a run can be explained without re-deriving what the expansion would have produced |
| **The person can see and prune it** | An expansion that quietly added *"Teil"* to a list — a word in every German manufacturing document ever written — would silently turn a narrowed question into an unnarrowed one, which is the same as turning three calls into forty |

That second row is the failure mode worth naming: runtime query expansion makes a `keyword_ask`
degrade into an `ask` without saying so, and the cost estimate in
[§6.2](#62-the-plan-is-shown-before-it-is-run) would be a lie. A term the person confirmed is a term
the plan can count.

Separately and at run time, matching is **stemmed** rather than exact, per
[§6.4](#64-ranking-the-chunks) — so `Toleranz` finds `Toleranzen` without anybody having to tick
both. Expansion is for different words; stemming is for the same word.

### 5.5 Question sets

A set is a named list of questions and their commands, stored as **one JSONB document validated by
Pydantic on read** — deliberately the same choice as `doc_type_version.spec`, and for the same reason
([ARCHITECTURE §4](ARCHITECTURE.md#4-data-model)): it is authored, saved and reused as a unit, and
shredding it across tables would buy query flexibility nobody wants.

The case's own questions are **rows**, for the opposite reason, which is also the reason already on
record: they are edited individually, one at a time, and each is referenced by its candidates.

Sets are **mutable and unversioned**, and this is the one place the desk deviates from how document
types work. A `doc_type_version` is immutable because documents pin it and a rule must not move under
work in progress. Nothing pins a question set: loading one copies its questions into a case, and from
that moment the case owns them. A set is a convenience, not a contract, and versioning it would be
ceremony.

## 6. Find

### 6.1 The run

One job per run, `kind="extract"`, scoped to the case. The deterministic sweep happens first and
costs nothing; the model calls follow under the existing `LCF_LLM_CONCURRENCY` semaphore, with
progress reported per call, exactly as drafting and assessment already do.

### 6.2 The plan is shown before it is run

Before the button is pressed, the desk computes the plan without executing it and says what it will
cost:

> 48 chunks across 5 files · 3 patterns, no calls · 2 keyword questions, 7 chunks hit · 1 open
> question, 6 chunks ranked → **13 assistant calls**

This is cheap to compute and it is the difference between a feature people trust and a button people
are afraid of. It also makes the effect of narrowing visible: adding a keyword to a question drops
the number, immediately.

### 6.3 Chunk selection, per tier

| Tier | Which chunks | How many |
|---|---|---|
| `pattern` | all of them | no calls at all |
| `keyword_ask` | those containing a keyword, then ranked by BM25 over keywords + question | top `LCF_EXTRACT_TOP_K` (default 6) |
| `ask` | all of them, ranked by BM25 over the question | top `LCF_EXTRACT_TOP_K` |

Keyword matching is normalised by the same tokeniser the ranking uses — casefolded,
whitespace-collapsed, stemmed for the source's language — so `Toleranz` finds `Toleranzen`. It is
also **word-bounded**, so `cal` does not find `calibration`: a keyword is a word, not a substring, and
a keyword list that silently matched inside words would narrow nothing.

Selection runs **per language**, grouped by source, then merged. A case holds a German complaint and
an English report, and stemming the English one with the German stemmer would quietly stop matching.
Scores from two language indexes are not strictly comparable, which matters far less than it sounds:
the ordering only decides which handful of passages a model reads, and a mixed-language case is
better served by the best few of each than by one index that stems half its corpus wrongly.

**When ranking can tell nothing apart, it says so.** An English question asked of German material
shares no term with any passage, so BM25 scores every one of them zero — and that is not a failure,
it is the ordinary case the `ask` tier exists for. Returning nothing would mean the tier silently
asked nobody. So the passages come back in document order, flagged, and both the funnel and the
candidate's own provenance line say *"nothing in the question matched any passage, so the first N
were read in order"*. Reading a few beats reading none; presenting document order as a ranking would
be the actual error.

### 6.4 Ranking the chunks

BM25 over the case's chunks, with **`bm25s`** and **`PyStemmer`**, the stopword list and stemmer
chosen per source language. This is [ARCHITECTURE §12](ARCHITECTURE.md#12-library-shortlist)'s rule
applied rather than excepted, and three things make it the clear call:

- **The dependency is install-time, not first-use.** `bm25s` needs `numpy` and nothing else
  mandatory. The no-egress constraint that rules out docling as a default
  ([§4.2](#42-the-parsing-decision)) is about a *running* installation reaching the internet; a
  prebuilt wheel resolved by `pip install` is in a different category entirely, and `numpy` is the
  most-deployed wheel there is.
- **The library is more than the formula.** Bundled stopword lists for fourteen languages, German
  among them, held in its own source rather than fetched from NLTK; a tokeniser; and a hook for a
  Snowball stemmer. That is exactly the language-aware behaviour
  [§4.4](#44-language-is-a-property-of-the-source) requires, and none of it is BM25.
- **Scoring is the easy half.** German stemming is `PyStemmer` whichever way the scorer is obtained,
  and a hand-written scorer still needs a tokeniser, stopword handling and stemmer plumbing wrapped
  round it. The formula is a page; the language handling is the work.

`numba` — `bm25s`'s optional JIT — is **not** taken: it is for corpora of millions, and a case holds
tens to low hundreds of chunks.

`ingest/retrieval.py` is then a thin, testable wrapper: build an index over a list of chunk texts,
query it, return `(chunk_index, score)` pairs. The tests assert ordering and language handling, not
the formula — that is the library's job now.

Embeddings are still not in scope. A semantic neighbour would help on *"what did they measure"*
phrased entirely differently, and that is precisely the case `ask` exists for — the model reads the
top chunks and says whether the answer is there. Adding a vector store to improve which chunks reach
it is the retrieval product [DESIGN §1](DESIGN.md#1-premise) says this is not. Stemming closes most
of the gap that made it tempting.

### 6.5 One card per distinct value

A batch number appearing in six chunks is **one** candidate with six occurrences, not six candidates.

Deduplication is per question, on a value normalised **according to the question's type**
([§5.1](#51-a-question-has-a-type)): `12,05` and `12.05` are one `number`; `2026-03-12` and
`12.03.2026` are one `date`; an `identifier` is casefolded and whitespace-collapsed and nothing more.
That last restraint is deliberate — `NW-CL-88213` and `NWCL88213` stay *different* candidates,
because merging them would hide a difference that might be the point, and an identifier is the one
type where punctuation is information.

The card then shows the value once and the places it was found beneath it, which is also the answer
to *"is it one batch or several?"* — four cards means four batches, and the person ticks all four.

### 6.6 Score, and the question the backlog left open

> **What the score means across tiers.** A `pattern` hit is exact, a `keyword_ask` hit has a count
> and a proximity, an `ask` hit has the model's stated confidence. These are not the same quantity
> and ranking them in one list without saying so would present a guess and a certainty as peers.

**Resolved: they are not ranked in one list.** Candidates are grouped by tier, the groups are
ordered by certainty, and each group is labelled in words rather than by a number:

```
Found exactly               NW-CL-88213          3 places        ✓ accept   ✗ dismiss
                            NW-CL-90104          1 place

Found near your keywords    0,04 mm              2 places
                            "innerhalb Toleranz" 1 place

Read from the text          12,05 mm             1 place         confidence 0.7
```

Within a group the score is tier-local and means one thing: occurrence count for `pattern`, the
model's confidence for the two `ask` tiers. Across groups the ordering is lexicographic — tier
first, score second — and the group heading is what carries the comparison a single number would
have faked.

A pattern hit also shows no confidence at all. Printing `1.00` beside a regex match invites the
reading that the other numbers are on the same scale.

### 6.7 How a candidate becomes an answer

> **How a candidate becomes an answer.** Reusing `answer.source = proposed` costs no schema, but a
> candidate that was *not* chosen still has value — it is the audit trail of what the material
> offered, the way a rejected `proposal` row survives its outcome.

**Resolved: candidates are their own rows and survive their outcome**, exactly as `proposal` does.
`status` moves `pending → accepted | dismissed`, with `decided_at`; nothing is deleted.

That decides re-run semantics too, which is the part that matters in use. A second run over new
material:

- **keeps** accepted candidates — they are findings now;
- **keeps** dismissed ones, and does not offer the same value again, because being asked twice to
  reject the same wrong batch number is the fastest way to make somebody stop reading the cards;
- **replaces** pending ones.

Re-running after dropping in two more files is therefore additive and safe, which is the only way a
person will actually work: gather some, look, gather more. An accepted value also has *where it was
seen* refreshed by a later run, because more material legitimately finds the same batch number in
more places and a finding should say so.

A decision survives the question being re-authored, too, but not silently. An
accepted value **no way now on the question could still produce** is kept and
flagged: its provenance line cites a pattern that was edited or dropped, and the
finding reads as though the search ignored the change. The finding stands,
because a decision is a person's and an edited pattern does not undo it.

The check is deliberately deterministic and asked only of the deterministic
tier: do any of the question's current patterns still match this value? *Not
found again* and *no longer findable* are different claims, and only the second
is checkable. A model offering `12,00 +0,02` this run and `12,00 +0,02 mm` last
run has found the same thing and changed nothing, so the two asked tiers are
left alone — flagging their ordinary variance would put a warning on every
assistant-backed question, which is the opposite of what the flag is for.

One more rule falls out of `multiple` ([§5.1](#51-a-question-has-a-type)). A question with one answer
that accepts a second value **demotes the first back to a candidate** rather than quietly holding
two answers to *"what is the part number?"*. Demoted, not dismissed: the person changed their mind
about which is right, they did not judge the old one worthless, and dismissing it would mean never
being offered it again.

### 6.8 What quote verification still has to do

A `pattern` or `keyword` hit **is** a character offset, so there is nothing to verify — the quote is
sliced out of the source text by the match's own span, widened to a readable window on sentence
boundaries.

The `ask` tier is the only one that asks a model for words, and it goes through
`llm/quoting.py:quoted_from` against the chunk it was given. A quote that is not there means the
candidate is discarded and counted, and the count is reported — the same contract, and the same
honesty about the count, that intake already has.

### 6.9 A run shows its work

A person looking at six cards has one question the cards themselves cannot answer: **where did these
come from, and what did you not show me?** Six candidates out of 48 chunks is either a precise
instrument or a broken one, and the difference is invisible unless the desk says which.

So a run is a **row, not a side effect**: `evidence_run`, with per-stage counts in `stats`, and every
candidate carrying its `run_id`. This is the same reasoning that put `llm_call` in the schema from the
start ([ARCHITECTURE §4](ARCHITECTURE.md#4-data-model)) — *why did it say that* has to be answerable
weeks later — and the same reasoning that keeps `check_result` history rather than overwriting it.

#### The funnel, per run

```
Run · 12 March, 14:22 · 2.4 s + 11 assistant calls in 31 s

  5 sources        48 chunks       4 questions      12 commands

  Complaint number            pattern        48 chunks scanned →  4 matches  →  2 values
  Affected batches            pattern        48 chunks scanned → 11 matches  →  4 values
                              keyword_ask     7 chunks hit     →  6 asked    →  5 found, 1 "not stated"
  Measured deviation          keyword_ask     3 chunks hit     →  3 asked    →  2 found
                              ask            48 ranked         →  6 asked    →  3 found, 1 quote rejected
  Customer contact            ask            48 ranked         →  6 asked    →  0 found

  Dropped on the way:  1 quote not in its chunk · 2 values failed the question's type · 9 duplicates merged
```

Every number there is a count the engine already has to compute, so the panel costs almost nothing
and answers the three questions people actually ask: *did it look at everything* (48 scanned), *what
did it cost* (11 calls, 31 s), and *what fell off the table* (the last line). That last line is the
one worth having. A question returning nothing because **all four of its candidates failed the date
format** is a fixable authoring problem; a question returning nothing because the material does not
contain the answer is not. Without the dropped counts those two look identical, and the person
concludes the tool does not work.

#### The route, per candidate

Each card carries one line of provenance, expandable:

```
  NW-CL-88213                                            3 places     ✓ accept   ✗ dismiss
  ▸ found exactly, by pattern `NW-[A-Z]{2}-\d{5}`
      Reklamation_8812.pdf  p. 1   "…unsere Reklamation NW-CL-88213 vom 04.03…"
      Reklamation_8812.pdf  p. 3   "…Bezug: NW-CL-88213…"
      RE RE FW Toleranz.eml  einkauf@nordwerk.de, 12 Mar   "…zu NW-CL-88213 haben wir…"

  12,05 mm                                               1 place      confidence 0.70
  ▸ read from the text · chunk ranked 2 of 48 (bm25 7.41) · quote verified
      Messprotokoll.pdf  p. 2 › "3. Messergebnisse › Tabelle 2"
```

Which command found it, which chunks, which rank and score if ranking was involved, whether the
quote was verified — and for mail, **who said it** ([§4.3](#43-email-is-the-input-with-a-sender)).
The fields are the same ones the funnel aggregates, read per row instead of summed.

This is where the desk earns trust, and it is also the debugging surface for the person authoring the
questions: *"chunk ranked 2 of 48"* on a miss says the ranking was fine and the model was not, while
*"ranked 31 of 48, not asked"* says the opposite and points straight at
[§5.4](#54-keywords-expanded-at-authoring-time).

#### Runs are kept

Every run is kept and listed, with its stats, the way assessments are. Two runs over the same case
after an authoring change is the only way to see whether the change helped, and *"13 calls → 4 calls,
same five findings"* is the sentence that justifies the whole formulate step.

## 7. Images

The desk is also where [BACKLOG §1](BACKLOG.md#1-image-evidence-in-a-document) gets its
upload and captioning path, because images come out of the files whether anyone asked or not.

| Step | How |
|---|---|
| **Extract** | `pypdf` `page.images`; `python-docx` related parts; `.eml` image attachments; direct upload or clipboard paste |
| **Deduplicate** | SHA-256 of the bytes. A letterhead on twenty pages is one asset, not twenty — without this the tray is unusable on the first real PDF |
| **Discard furniture** | Under 6400 pixels of area, or more than 12× longer than it is wide, is a bullet, an icon or a horizontal rule, and is dropped. Area rather than bytes, because a byte count drops a small sharp photograph and keeps a bloated letterhead; the aspect rule is what catches the 400×6 divider that has pixels to spare. The count is reported, not hidden |
| **Normalise** | Pillow: RGB, a 320 px thumbnail stored beside the original |
| **Store** | `lcf-uploads`, `evidence/{case}/{source}/{sha}.img` and `.thumb.webp` — the bucket that exists and has never been written to. Keyed under the source because that is where the image was found, which is what the card has to say |
| **Caption** | `caption_images`, batched within `LCF_LLM_MAX_IMAGES_PER_CALL` — configured since the first commit and so far unread |
| **Review** | A strip of cards: thumbnail, caption, which file and page, accept / dismiss |

Assignment is **by clicking, not dragging**, for the reason the backlog already gives: the tray holds
what was found and a person decides what it is worth.

Captioning needs a multimodal endpoint, and a lot of installations will not have one pointed at
`LCF_LLM_BASE_URL`. So it **degrades rather than fails**: thumbnails and provenance always work, and
if the endpoint refuses image content the tray says *"the configured model did not accept images —
captions are unavailable"* and the cards carry a label field a person can type into. An image with no
caption is still a usable finding; a tray that 500s because the model is text-only is not.

## 8. Four new typed calls, in two species

[ARCHITECTURE §5.2](ARCHITECTURE.md#52-call-taxonomy) lists eight call types and says *a ninth
requires a design decision*. This is that decision, and it is four — so the table it joins gains a
column, because the four do not all do the same kind of thing.

**Finding calls** run during a run, once per chunk, and are bound by the never-invent rule:

| Call | Scope | Returns | Why it cannot be an existing one |
|---|---|---|---|
| `answer_from_chunk` | one question + one chunk | `found`, `value`, `quote`, `confidence` | `prefill_answers` answers a *section's* questions from a document's paste, keyed by spec question and shaped by spec types. This answers one authored question against one provenance-carrying chunk and must return the chunk-local quote for verification. Forcing one schema to do both would widen the one that already works |
| `caption_images` | ≤ the image budget | a caption per image | Already in the taxonomy as unbuilt. The only call that sends image content |

**Authoring calls** run while a person is writing a question, never during a run, and each one's
output is **confirmed or verified before it is data**:

| Call | Scope | Returns | Verified by |
|---|---|---|---|
| `propose_pattern` | examples + the question | `pattern`, `note` | Execution. `fullmatch` against every supplied example, plus the guards in [§5.3](#53-the-pattern-the-assistant-writes-and-the-examples-that-prove-it) |
| `propose_keywords` | the question + the case's language | terms, each with a reason | A person ticking them ([§5.4](#54-keywords-expanded-at-authoring-time)) |

The species distinction is worth having in the taxonomy rather than only in this document, and it is
not bookkeeping. A finding call's output becomes a candidate, so it carries the never-invent frame and
is quote-verified. An authoring call's output becomes a **parameter** — a regex, a word list — which
is inert until a run uses it, and which a human has looked at in between. Those are different
contracts, and a table that listed all four as peers would invite the next authoring call to be
quote-verified for symmetry and the next finding call to skip verification for symmetry.

So: `answer_from_chunk` and `caption_images` compose `never_invent.md` last, as every call does.
`propose_pattern` and `propose_keywords` do not — they have no evidence to stay faithful to, and the
rule each must obey is enforced by a function or by a tick box rather than by wording.

Each gets a schema in `llm/schemas.py`, a prompt frame in `llm/prompts/`, and fixtures.
`complete_json` grows an image-bearing sibling for `caption_images`; the streaming guards, the
padding abort and the single retry are unchanged, and only the message construction differs.

## 9. The rule this feature brushes against

[DESIGN §5.8](DESIGN.md#58-instructing-the-model) forbids a free-text *additional instructions for the
assistant* field, and decision 10 says the rule builder never writes prompt text. An `ask` parameter
is free text a person writes which reaches a model. The distinction, stated plainly because the
precedent is what matters:

1. **It is a question about supplied material, not an instruction about writing.** `ask` cannot
   change how anything is phrased, what register is used, or what a check demands. Its entire effect
   is to select and extract. The vocabulary stays closed — three kinds, fixed parameters — so there
   is no field in which "write it more formally" would even be expressible.
2. **It is written by the creator about their own pile, not by the rule builder about everybody's
   documents.** The thing §5.8 protects is an organisation's standard against erosion by whoever is
   talking to the model today. The desk writes no document content and encodes no standard; it finds
   numbers in files that were emailed to one person.
3. **The desk cannot write content at all.** There is no path from a candidate to a `revision`. The
   closest thing is a person reading an export.

That third point is the one that keeps the argument honest, and it is why
[§11](#11-the-seam-to-the-rest-of-the-app) stays unbuilt in this version: the moment a doc type's
question carries an `ask`, point 2 stops being true and the rule needs re-examining — a type author
*would* then be writing something that reaches a model on everybody's documents. It is a defensible
step, and it is a different one, to be taken deliberately.

## 10. Shape of the code

```
src/lcf/ingest/                  # pure: bytes and text in, data out. No DB, no HTTP
    parse.py                     #   dispatch, Unit, Parsed — the only module that
                                 #   knows which backend reads which format
    text.py                      #   the character guard every backend's output passes
    pdf.py  office.py  mail.py  plain.py
    docling_backend.py           #   imported only if the extra is installed
    language.py                  #   py3langid, one call, cached
    chunk.py                     #   units → packed chunks with provenance
    images.py                    #   extraction, SHA dedupe, Pillow thumbnails
    retrieval.py                 #   bm25s wrapper, per-language tokenisation
    values.py                    #   type-aware normalisation and validation of candidate values
    commands.py                  #   the closed vocabulary (Pydantic)
src/lcf/services/evidence.py     # cases, sources, questions, question sets
src/lcf/services/extraction.py   # plan, run, candidates, accept/dismiss, export
src/lcf/llm/calls.py             # + the four calls
src/lcf/web/routes/evidence.py   # thin
src/lcf/web/templates/evidence*.html + partials/
src/lcf/web/static/drop.js       # drag, drop, clipboard paste
```

`values.py` is small and sits in the pure package on purpose: it is where a `12,05` becomes a number,
a `12.03.2026` becomes a date, and a candidate the question's type cannot hold gets rejected
([§5.1](#51-a-question-has-a-type), [§6.5](#65-one-card-per-distinct-value)). Deduplication,
validation and display all need the same answer, so there is one function that gives it.

`ingest/` is a **pure package**, like `spec/` and `engine/`: no `sqlalchemy`, no `lcf.models`, no
`lcf.services`, no `lcf.storage`, and `tests/test_boundaries.py` gets a row for it. That is what keeps
the parser, the chunker and the ranker testable with a bytestring and an assertion, which is the
majority of what can go wrong here.

### Data model

```
evidence_case ──┬──< evidence_source ──< evidence_chunk
                │        file|paste; parsed      seq, page_from/to, path,
                │        text, status, pages,    char_from/to, meta
                │        language, sender,       (per-reply sender for mail)
                │        sender_domain,
                │        sent_at, subject
                │
                ├──< evidence_asset       image: uri, thumb_uri, sha256, page,
                │                         caption, label, status
                │
                ├──< evidence_run         stats JSONB, calls, duration_ms
                │
                └──< evidence_question ──< evidence_candidate
                         key, prompt,          value, value_norm, quote, tier,
                         type, options,        score, rank, source_id, chunk_id,
                         multiple, commands    char_from/to, occurrences JSONB,
                                               run_id, status, decided_at

question_set        key, title, questions JSONB   — unpinned, mutable, copied on load
```

`value_norm` is stored rather than recomputed because it is the dedupe key and a unique constraint on
`(question_id, value_norm)` is what makes a re-run's *"do not offer a dismissed value again"*
([§6.7](#67-how-a-candidate-becomes-an-answer)) a database guarantee instead of a query everyone has
to remember to write.

`occurrences` is JSONB on the candidate rather than a table of its own: an occurrence is never
queried across candidates, only ever read beside the one it belongs to, and a row apiece would be a
join to render a bullet list.

One Alembic migration. Jobs carry the case id in `job.scope` with a null `document_id`, which needs no
schema change and gives `jobs.latest_for_scope(scope)` as the only addition.

### Routes

```
GET   /evidence                              cases, and starting one
POST  /evidence                              create {title, from_set?}
POST  /evidence/{case}/remove
GET   /evidence/{case}                       the desk: gather · formulate · find
POST  /evidence/{case}/sources               multipart, many files      → a parse job each
POST  /evidence/{case}/paste                 text or an image           → a source
GET   /evidence/{case}/gather                the gather panel
GET   /evidence/sources/{id}/text            what the parser actually read
GET   /evidence/sources/{id}/file            the original bytes, back again
POST  /evidence/sources/{id}/language        override a wrong detection
POST  /evidence/sources/{id}/reparse         after an override, or after a failure
POST  /evidence/sources/{id}/remove

GET   /evidence/{case}/questions             the formulate panel, with the set picker
POST  /evidence/{case}/questions             add
POST  /evidence/questions/{id}               edit
POST  /evidence/questions/{id}/remove        · move
POST  /evidence/questions/{id}/commands      add a command
POST  /evidence/questions/{id}/commands/{at}/remove
POST  /evidence/questions/{id}/pattern       examples → proposal → live match preview
POST  /evidence/questions/{id}/keywords      expand → terms to tick
POST  /evidence/{case}/sets                  save these questions as a set · load one · delete

POST  /evidence/{case}/run                   → job
GET   /evidence/{case}/findings              the plan, then the review panel
GET   /evidence/{case}/runs                  the funnel, this run and the ones before
POST  /evidence/candidates/{id}/{decision}   accept · dismiss
GET   /evidence/{case}/export.json           accepted findings, with provenance
GET   /evidence/{case}/export.md

POST  /evidence/{case}/caption               → job
GET   /evidence/{case}/assets                the tray
POST  /evidence/assets/{id}/label            a caption typed by hand
POST  /evidence/assets/{id}/{decision}       accept · dismiss
GET   /evidence/images/{id}/thumb            · and without /thumb, the full image
```

Server-rendered HTML, HTMX swaps, jobs polled by the card that already exists. No new JavaScript
beyond the drop zone.

Two things the design expected to be routes are not, because they are panels rather than pages. **The
plan** ([§6.2](#62-the-plan-is-shown-before-it-is-run)) renders at the top of the findings panel, so
every swap that could change the cost — adding a keyword, dropping in a file — redraws the estimate
beside the button it belongs to, with no second request. **A candidate's route**
([§6.9](#the-route-per-candidate)) is a `<details>` already in the card: its occurrences are a JSONB
column on the row the panel has in hand, so fetching them separately would be a round trip for data
already sent.

### Configuration

| Setting | Default | Why it is configuration |
|---|---|---|
| `LCF_INGEST_PARSER` | `auto` | Which backend; `builtin` works everywhere, `docling` needs the extra |
| `LCF_INGEST_CHUNK_CHARS` | `1800` | A model property until a tokenizer exists |
| `LCF_INGEST_MAX_FILE_MB` | `25` | An upload limit is an operational decision |
| `LCF_EXTRACT_TOP_K` | `6` | How many chunks a question may cost. The cost dial |
| `LCF_EXTRACT_PATTERN_TIMEOUT_S` | `2.0` | The backtracking guard ([§5.3](#53-the-pattern-the-assistant-writes-and-the-examples-that-prove-it)) |

### Dependencies added

| Package | For | Weight |
|---|---|---|
| `pypdf` | PDF text per page, embedded images | Pure Python |
| `pillow` | Thumbnails, image normalisation | Wheel; already named in ARCHITECTURE §12 |
| `mail-parser-reply` | Thread splitting, signature and disclaimer stripping | Pure Python |
| `regex` | Pattern matching with a timeout | Wheel |
| `bm25s` | Lexical ranking, bundled stopwords | Wheel, needs `numpy` |
| `PyStemmer` | Snowball stemming, German in particular | Small wheel |
| `py3langid` | Language detection, model bundled in the package | Pure Python + `numpy` |
| `docling` | **Not declared.** Layout, tables, OCR — installed by the deployer | Large; downloads weights on first use |

Every declared one resolves at `pip install` time from a wheel and none fetches anything at runtime,
which is the test [§4.2](#42-the-parsing-decision) sets — and the reason docling is not declared at
all rather than declared as an extra.

## 11. The seam to the rest of the app

Not built, named so it is not designed around twice.

A document type's `Question` grows an optional `commands` list, validated by the same
`ingest/commands.py` vocabulary. A document gets a case, and intake's existing
`prefill_answers` path gains a sibling that reads accepted candidates and writes
`answer.source = proposed` — which is the contract a prefilled answer already has, so
[invariant I](DESIGN.md#i-content-exists-only-after-a-human-accepted-it) needs nothing new.

Two things have to be settled first, and neither can be settled before the desk has been used:

- **Cost.** `commands` on a doc type means a type author can make every document in the organisation
  spend forty calls. The plan from [§6.2](#62-the-plan-is-shown-before-it-is-run) is the beginning
  of an answer; a per-document ceiling is probably the rest.
- **The §5.8 question**, in the form [§9](#9-the-rule-this-feature-brushes-against) leaves it: an
  `ask` authored by a rule builder is a different thing from one authored by a creator.

Until then the bridge is the export. Findings come out as JSON or Markdown, and the Markdown pastes
straight into a document's intake box — where `map_evidence_to_sections` files it and
`prefill_answers` reads it, with no new code and no new coupling. A tidy accident of both features
being built around the same rule: *point at the author's own words*.

## 12. Testing

| Layer | Approach |
|---|---|
| Parsers | Fixture files per format in `tests/fixtures/`; assert text, page numbers and image count. The `.docx` fixture is generated by `python-docx` in the test |
| Mail | The nasty cases as fixtures: a four-deep German forward chain, a signature, a legal disclaimer, an inline image. Assert what is stripped, that nothing above the first separator is lost, and that **each reply carries the right sender** |
| Chunking | Property test: every character of the source text appears in some chunk; offsets round-trip; no chunk exceeds the budget unless it is one indivisible unit |
| Language | A German and an English fixture per format; assert detection, and assert that a forced override wins over it |
| Retrieval | Ordering assertions, and the one that matters: a German query for `Toleranz` ranks a chunk saying `Toleranzen` above one that says neither |
| Values | Table-driven: `12,05`/`12.05` normalise equal, `12.03.2026`/`2026-03-12` normalise equal, `NW-CL-88213`/`NWCL88213` do **not**, and a `date` candidate reading "8 September" is rejected |
| Patterns | A verified-pattern suite: examples that must match, patterns that must be refused, and one pathological pattern that must hit the timeout rather than hang |
| Extraction | Monkeypatched `answer_from_chunk`, as the LLM tests already do at `llm/calls.py`. Assert tier ordering, dedupe, re-run semantics, discarded-quote counting, and that the run's stats add up — every chunk scanned is accounted for as asked, ranked-but-not-asked, or dropped with a reason |
| Characters | A string carrying a NUL, a lone surrogate, a soft hyphen, a zero-width space, a no-break space, a form feed and an NFD umlaut: assert it stores, that the identifier in it still matches its pattern, that the offset invariant survives, and that a file of nothing but control characters is refused rather than recorded empty |
| Images | A generated PNG, a duplicate of it, and one 10×10; assert one asset, one dedupe, one discard |
| Web | Through HTTP, like the builder's rename tests — upload, run with a stubbed handler, accept, export |

The shape of that table is the point: everything except one row is testable without a model, and the
pattern tier is testable without a model *at all*. If the desk can only be judged by talking to an
LLM, it has been built wrong.

The extraction row's second half is the one that will catch real bugs. A funnel whose numbers do not
reconcile is how a silently-dropped candidate presents itself, and *"scanned = asked + ranked-not-asked
+ dropped"* is an invariant a test can assert on every run in the suite.

## 13. What a person can do with it

End to end, with nothing but the default install:

1. **Open a case** and drop the pile on it — PDFs, Word files, `.eml` threads with their
   attachments, photographs, or text pasted straight in. Each file is read in its own job, so one
   unreadable scan among eight fails alone and says which it was. What the parser read is readable
   back, passage by passage, with offsets and page numbers.
2. **Write the questions.** A prompt, a type, and whether several answers are an answer or a
   contradiction. Paste two real complaint numbers and the assistant writes the pattern; it is held
   to those examples, then run over the pile so the count of matches is visible before anything is
   saved. Ask for keyword suggestions in the material's own language and tick the ones worth keeping.
   Save the lot as a set for the next case of the same kind.
3. **See what a search would cost**, then run it. Patterns cost nothing and run first, so a run that
   cannot reach the assistant still produces every exact hit.
4. **Accept or dismiss**, one card per distinct value, grouped by how certainly it was found and
   carrying the passage and the route behind each one. Run again after dropping in two more files:
   accepted values stay findings, dismissed ones never come back, and the funnel says what each
   stage did.
5. **Take the findings out** as JSON, or as Markdown that pastes into a document's intake box with
   every quote intact.

Captioning images is the one step that needs more than the default: a multimodal endpoint. Without
one the tray still works — thumbnails, pages, dedupe, and a label field — which is why it degrades
rather than fails.

## 14. Non-goals

| Not doing | Until |
|---|---|
| OCR in the default path | Somebody has scans. `uv pip install docling` is the answer that day |
| `.msg`, `.xlsx`, `.pptx` | Somebody is sent one. Each is one parser module behind the existing protocol |
| Embeddings, a vector store | [§6.4](#64-ranking-the-chunks). Retrieval is a different product |
| Bounding boxes on a chunk | The built-in parser cannot produce them. Adding a column nothing can write would be schema on speculation, so the day docling's provenance is wanted is a migration — a small one, since `chunk.meta` already carries whatever a unit knew about itself |
| Runtime query expansion | [§5.4](#54-keywords-expanded-at-authoring-time). It would make the cost estimate a lie |
| Ranking by sender domain | Somebody wants it. `sender_domain` is a column so the day it is asked for is a query, not a migration ([§4.3](#43-email-is-the-input-with-a-sender)) |
| Per-chunk language detection | A mixed-language thread is mis-stemmed in its minority chunks, which costs a rank position. Cheap to add if that ever costs a finding ([§4.4](#44-language-is-a-property-of-the-source)) |
| Cross-case search | A case is a pile about one problem. Searching across piles is the retrieval product again |
| Question-set versioning | Something pins a set. Nothing does ([§5.5](#55-question-sets)) |
| Auth on the desk | [BACKLOG §9](BACKLOG.md#9-accounts-and-roles), with everything else |
