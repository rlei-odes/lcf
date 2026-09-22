# Sample intake material — 4D report

Paste the whole thing (including the headings) into the intake box of a new 4D
document and press **Sort it into the sections**.

It is deliberately what material actually looks like when it arrives: four
sources, in four registers, out of order, with the facts for one section
scattered across three of them. Some of it belongs nowhere in a 4D and should
come back unplaced, and several things a 4D requires are missing on purpose — so
drafting produces gaps rather than invention.

Same case as `4d-sample-content.yaml`, seen before anybody wrote it up.

---

## Forwarded from the customer

> From: quality@nordwerk-fahrzeugtechnik.de
> Sent: 8 September 2026 07:42
> Subject: Reklamation NW-CL-88213 — A-4471 rear axle bracket
>
> During incoming inspection on 5 September our inspector found cracking in the
> weld seam at the flange transition on several rear axle brackets, part number
> A-4471. Cracks are between 3 and 8 mm long and visible without magnification.
>
> We have raised claim NW-CL-88213 against the delivery. The affected quantity
> on our side is 1450 pieces. We require an interim containment report within 24
> hours and a full 4D by the end of next week.
>
> Please confirm receipt.
> M. Kessler, Supplier Quality, Nordwerk Fahrzeugtechnik GmbH

Booked as report 4D-2026-0143, opened 8 September.

## Notes from the Monday morning call

Present: Sabine Vogt (quality management — she is running this one as champion),
Tomas Reiner (process engineering, owns welding cell 3), Lena Hofmann (customer
quality, looks after the Nordwerk account).

Tomas walked through what we know. Dye penetrant testing on a sample of 60 parts
here confirmed it — it is not just a surface mark, there is porosity underneath.
Every affected part we have checked came out of welding cell 3. Cells 1 and 2
have been running the same part all month and are clean. It is confined to seam
WS-02 at the flange transition; WS-01 and WS-03 are fine.

Lots involved are L-2608 and L-2612, both August into September. Anything before
L-2608 looks unaffected.

Sabine's point: our own final inspection is visual only, so even if we had caught
it we probably wouldn't have. That is a second problem and it needs to be in the
report as one.

Also discussed the A-4470 front variant — different geometry, different fixture,
no indications found. Ruled out.

## Containment — what has actually been done

- 8 Sep: blocked all A-4471 finished stock in the warehouse. 412 parts. Confirmed
  against the stock count. (Sabine)
- 8 Sep: 100% dye penetrant inspection before dispatch, applied to everything
  coming out of cell 3 since that morning. (Tomas)
- 9 Sep: Lena went to Nordwerk and sorted their stock on site — 638 parts gone
  through, 19 rejects pulled out.
- 9 Sep: recalled the parts that were in transit to our warehouse, 400 of them,
  re-inspected on arrival. (Sabine)

Customer was formally notified on 8 September, same day the claim came in.

Parts already fitted to vehicles at Nordwerk are *not* covered by this — that is
being handled with them separately.

## Maintenance log extract + what Tomas measured

```
2026-08-21  WC3  pressure regulator (shielding gas) replaced — unit swapped,
                 line pressure checked OK, released to production.  [T.R.]
```

Gas flow at cell 3 measured on 10 September: **8 l/min**. The spec for this
weld is **14 l/min**. Nobody measured it after the regulator went in — the
maintenance procedure does not ask for it.

Tomas then welded 12 test parts deliberately at the reduced flow and reproduced
the cracking. So the mechanism is confirmed, not assumed: low shielding gas
→ porosity in WS-02 → crack initiates under load.

On the escape side: inspection instruction PA-221 rev. 4 specifies visual
checking only. There is no NDT step for this part at all, so no control existed
that could have found sub-surface porosity before dispatch.

## Odds and ends from the same thread

Permanent corrective action is still open — we are thinking about a flow sensor
with an interlock on the cell, and adding a verification step to the maintenance
procedure, but nothing is decided and nothing is implemented. Effectiveness
validation obviously has not happened yet either. Kessler also asked whether this
affects the Polish plant; it does not, they do not run this part.

Separately: Lena needs the updated PPAP file for A-4602 before Friday, unrelated
to any of this.
