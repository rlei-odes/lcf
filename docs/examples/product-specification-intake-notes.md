# Sample intake material — product specification

Paste the whole thing into the intake box of a new Product Specification document
and press **Sort it into the sections**.

A different domain from the 4D notes on purpose: the same intake works over a
document type it was never tuned for, because the sections it distributes into
come from the spec rather than from anything in the prompt.

As with the 4D notes, parts of this belong nowhere in the spec and several things
the type requires are absent — acceptance in particular is thin, so the gate has
something real to complain about.

---

## Kickoff meeting, 14 September

Attendees: Ingrid Salzmann (production IT, owns this spec), Tomas Reiner (process
engineering), Mehmet Aydın (shop floor lead, late shift), Bea Kowalczyk
(controlling).

The thing we are specifying is the **Scrap Reporting Terminal**, SRT-1. A small
fixed terminal at each welding cell where the operator books a scrapped part at
the moment it is scrapped.

Ingrid: the problem is not that we don't record scrap. We do — on paper, on a
clipboard at the cell, and somebody keys it into SAP two or three days later.
By then nobody remembers which lot it came from. Controlling gets a number at
month end that is roughly right and completely useless for finding causes.

Bea confirmed: scrap cost by cell is reportable today, scrap cause by lot is not.
That is the gap.

Document number SPEC-0142, revision A. Draft — not approved, do not circulate
outside the group yet.

## What it has to do

From the same meeting, plus Mehmet's list from the shift afterwards:

- Operator books a scrap event in under 10 seconds. Mehmet was firm about this:
  anything slower and they will go back to the clipboard. This is the one that
  matters most.
- Scrap reason picked from a fixed list, not typed. Free text is how you get 400
  spellings of "porosity".
- Every event records cell, lot number, part number, operator, timestamp.
- Lot number comes from scanning the travelling card — not typed.
- Works when the network is down. Buffers locally, syncs when it comes back.
  Mehmet: the network at cell 3 drops most weeks.
- Push to SAP within 5 minutes of the event when the network is up.
- Supervisor can correct an entry within the shift; after that it is locked and
  only controlling can change it.

Explicitly **not** doing:
- No rework tracking. Rework is a different process and a different form.
- No quality inspection results — that stays in the existing system.
- No mobile or tablet version. Fixed terminal at the cell, that is all.

Ingrid also said we are not building a reporting front end. Controlling reads it
out of SAP with the tools they already have.

## Technical constraints as discussed

Tomas pulled these off the environment at the cells:

- Ambient temperature at the cells runs 5 to 40 °C.
- Dust and weld spatter — needs to be at least IP54 rated.
- Screen has to be readable with welding gloves on, so a resistive or infrared
  touch panel, 10 inch minimum.
- 24 V DC supply is already at every cell; use it rather than pulling mains.
- Response to a barcode scan under 500 ms, otherwise it feels broken.
- Local buffer has to hold at least 72 hours of events. Worst case we saw was a
  long weekend outage.

Standards that apply: EN 60204-1 for the electrical side, our internal norm
WN-114 for shop floor devices, and IP54 per EN 60529.

## Loose ends

Nobody has said who signs this off. Ingrid thinks it needs production and
controlling both, but that is not agreed and there is no name against it. We also
have not defined what the acceptance test actually is — "it works at cell 3 for a
week" was floated but not agreed, and there is no criterion written down for any
of the requirements above.

Sketches of the cell layout and the mounting bracket exist on Ingrid's whiteboard;
somebody needs to photograph them.

Unrelated, noted so it does not get lost: the canteen badge reader at gate 2 is
still on the old firmware and needs doing before the audit.
