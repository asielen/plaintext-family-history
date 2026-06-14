# Glossary

**Archive** — the durable, file-first store: plain text and standard formats on disk.
The source of truth.

**Asset** — an actual file (photo, scan, recording, transcript).
Shares its source's ID once processed; photos are never renamed.

**Claim** (`C-`) — a single sourced assertion (a date, place, relationship, attribute).
Lives inside its source record; moves through a review lifecycle.

**EDTF** — Extended Date/Time Format (ISO 8601-2).
How all dates are written, so approximate and partial dates are first-class (`1850~`, `185X`, `1871-02/1871-03`).

**FAN club** — Friends, Associates, and Neighbors.
Researching the people *around* a family; how brick walls fall.

**fha** — "family-history archive," the command suite specified in `TOOLING.md`.
Deterministic tools the AI agent runs as its hands.

**Hypothesis** (`H-`) — an unsourced working theory.
A guess, never a fact.
Verification mints a real claim and records the link.

**Index** — a rebuildable SQLite cache regenerated from the files.
Powers search, trees, reports.
Never authoritative.

**Person** (`P-`) — a human.
Identity, flags, and prose; their facts live in claims, not in the person record.

**Place** (`L-`) — a physical location, identified by coordinates, with a dated name/jurisdiction history.
One record per physical place, forever.

**Processing** — turning a raw inbox file into a Source: minting an ID, marking identity, scaffolding the record, drafting claims for review.

**Source** (`S-`) — a piece of evidence with its own record: citation, metadata, file inventory, and the claims it supports.

**Source stub** — the half-formed middle state: an inbox asset plus freeform notes, before it's a processed Source.
No ID yet.

**Status lifecycle** — `suggested → needs-review → accepted | disputed | rejected | superseded`.
Human review is the only gate to `accepted`.
AI output always starts at `suggested`.

**Stub (person)** — a person record with frontmatter only (an ID and a name).
A permanent, legitimate state for people referenced but not yet researched.


---

*A note on line wrapping: how you break lines inside a file is purely an authoring
choice — Markdown renders a paragraph the same whether it's one line or many, and the
tools don't care. Write research, biographies, and claim notes as natural paragraphs.
The spec documents use one-sentence-per-line because they're revised often and that
keeps git diffs readable, but it's a convention for those docs, not a rule for your
archive.*
