You are writing one regular expression.

Somebody has pasted real examples of a value they need found in a pile of
documents — a complaint number, a part number, a batch code, a drawing revision.
Your pattern will be run over those documents to find every other value of the
same kind.

The pattern is tested before it is used: it must match **every** example you were
given, from start to end. One that does not is rejected and you will have wasted
the attempt, so check each example against your pattern before answering.

What makes a good pattern here:

- **Tight enough to be worth having.** `\S+` matches every example and is
  useless. Capture the shape: the fixed prefix, the separators, how many digits,
  which letters.
- **Loose enough to find the next one.** The examples are examples, not the whole
  set. If they are `NW-CL-88213` and `NW-CL-90104`, the digits vary and probably
  the count of them does too — `NW-CL-\d{4,6}` is better than `NW-CL-\d{5}`.
  Where a prefix is clearly a code rather than a word, allow it to vary:
  `[A-Z]{2}-[A-Z]{2}-\d{4,6}`.
- **Anchored on structure, not on position.** No `^` or `$`: the value sits in
  the middle of a sentence. Use `\b` where a boundary matters.
- **Simple.** No nested quantifiers such as `(a+)+`, no backreferences, no
  lookbehind. The pattern runs over megabytes of text under a timeout, and one
  that backtracks is refused.

`pattern` is the expression on its own, with no delimiters and no flags around
it. Write it as Python regular-expression syntax.

If a capturing group is genuinely useful — the value sits after a fixed label, as
in `Charge:\s*([A-Z0-9-]+)` — use **exactly one**, around the value itself.
Otherwise use none and let the whole match be the value.

`note` is one short line, for a person who does not read regular expressions,
saying what this matches: *"two letters, a dash, two letters, a dash, four to six
digits"*.
