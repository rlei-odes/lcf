# Using the evidence desk

How to get answers out of a pile of files. For how it works inside, see
[EVIDENCE-DESK.md](EVIDENCE-DESK.md).

A **case** is one pile — usually one complaint. Three steps: gather the files, write the
questions, run the search and accept what is right.

---

## 1. Gather

Drop files on the card, or paste text. Accepted: PDF, Word, `.eml`, plain text, CSV/TSV,
and images (PNG, JPG, WebP, TIFF).

- Each file is split into passages that keep their page number. Mail keeps its sender.
- An `.eml` brings its attachments in as sources of their own.
- The same file dropped twice is listed once.
- A scanned PDF with no text layer is refused, not stored empty. Install the `docling`
  extra for OCR, or paste the text.
- Language is detected per file. Correct it in the table if it is wrong — keyword
  matching is stemmed per language and the wrong one weakens it.

Images found in the files appear in a separate tray. Keep or drop each one. "Describe the
images" captions them, and needs a model that accepts pictures.

## 2. Formulate

Add one question per thing you need out of the pile. Each question has a **type**, an
optional **several answers** flag, and one or more **ways to find it**.

### Answer types

| Type | Accepts | Two values count as one when | Use for |
|---|---|---|---|
| **Text** | any text | they match after collapsing spaces, ignoring case and accents, and trimming punctuation at the edges | descriptions, names, free-form answers |
| **Identifier** | any text, reproduced exactly | they match ignoring case and spaces **only** — `NW-CL-88213` and `NWCL88213` stay two values | part, batch, lot, order and complaint numbers |
| **Number** | the first number in the passage, comma or point decimal — `12,05 mm` is 12.05 | they are numerically equal — `12.0` and `12` merge | measurements, quantities, tolerances |
| **Date** | `2026-03-12`, `12.03.2026`, `12/03/2026`, `2026/03/12`, `12-03-2026`, `12.03.26`, including one inside a sentence | they are the same calendar day, whatever form each was written in | when something happened |
| **Yes or no** | `ja`/`nein`, `yes`/`no`, `y`/`n`, `true`/`false`, `wahr`/`falsch`, `1`/`0` | they mean the same thing | "is a deviation permit in place?" |
| **One of a list** | only the values you list | they match after text folding | a fixed vocabulary — severity, disposition, class |

Notes:

- The type is a filter. A value the type cannot hold is discarded before you see it, so a
  date question never offers you "8 September".
- **Number** reads the first number in the passage and ignores the rest. Thousands
  separators are not handled, because `1.234,56` and `1,234.56` mean opposite things:
  `1.234,56` is read as **1.234**. Check any value where one might appear.
- **Date** parses numeric forms only. Month names are not read.
- **Identifier** is deliberately strict. Punctuation inside a part number is information.
- Dates are stored and exported as ISO. Numbers keep the form they were found in.

### Several answers

Leave it off for "what is the part number?", where a second answer means the first was
wrong. **Tick it** for "which batches are affected?", where four answers are four batches.

This matters more than it looks. A question without it holds exactly one answer: accepting
a second one puts the first back in the candidate list and tells you it did so. If you are
pulling the same field out of eight documents and expect eight answers, tick it.

You can change it later — edit the question.

### Ways to find it

A question needs at least one. Several is normal; they run in order of cost.

| Way | Costs | What it does | Use when |
|---|---|---|---|
| **Pattern** | nothing | a regular expression run over every passage | the answer has a shape: `LOT-2026-0417`, `NW-CL-88213`, a drawing number |
| **Keywords** | one call per passage that mentions a keyword | reads only the passages containing your words | no fixed shape, but the text around it is predictable — "Toleranz", "Sollmaß" |
| **Ask** | one call per ranked passage, capped | ranks every passage and reads the best | neither of the above fits |

- **Write me a pattern**: give one or two real examples and the assistant writes the
  regex. It is checked against your examples before you can keep it, and run over what is
  already in the pile so you see the hit count first.
- **Suggest more words**: offers keywords in the language your files are actually in.
  Matching is stemmed, so `Toleranz` finds `Toleranzen`.
- Prefer a pattern wherever one exists. It costs no model calls and is reproducible.

### Question sets

Save a case's questions as a set and start the next case from it. The second complaint
from the same customer starts pre-wired.

## 3. Find

**Search the pile** tells you what it will cost before spending it. Patterns run to
completion first, so a run that cannot reach the model still returns every pattern hit.

Results come back as one card per distinct value, grouped by how it was found:

| Group | Means |
|---|---|
| **Found exactly** | a pattern matched. The number beside it is how many places it is in. |
| **Found near your keywords** | the model read a passage containing your keywords. The number is its confidence. |
| **Read from the text** | the model read a highly ranked passage. The number is its confidence. |

Scores are not comparable between groups, which is why they are never ranked in one list.

Each card lists every place the value appeared, with the passage behind it. Press **accept**
or **dismiss**; **undo** puts a decision back. Accepted values stay across runs and
dismissed ones do not come back, so you can drop more files in and search again.

## 4. Export

**JSON** for a machine. **Markdown** to paste into a document's *Your material* box, where
intake sorts it into sections — every value arrives with the passage it came from.

Only accepted values are exported, plus the images you kept.

---

## If something looks wrong

| What you see | Why |
|---|---|
| Accepting a value seems not to save | The question takes one answer and traded the previous one out. Tick **several answers**. |
| A question finds nothing | Check the way to find it. Open a source's text to see what the parser actually read. |
| A PDF was refused as a scan | No text layer. Install the `docling` extra for OCR, or paste the text. |
| A value appears twice, nearly identical | Identifiers only merge on case and spaces. If the two really are the same, dismiss one. |
| The run button is disabled | A question has no way to find it, or there is nothing in the pile. |
