# Glossary

**Alias** - any name that points at a record: its ID, its filename, a person's name and nicknames, a place's other names. It's what lets you link by name - type `[[Grandpa Joe]]` and the archive knows which person you mean. The tools keep each record's aliases in sync so your name-links keep working even after files are renamed.
*Example: a person named "Margaret A. Cole" also answers to `[[Margaret Cole]]`; both resolve to the same record.*

**Archive** - the durable, file-first store: plain text and standard formats on disk.
The source of truth.
*Example: the `my-family-archive` folder on your computer - every record in it opens in Notepad, today or in fifty years.*

**Asset** - an actual file (photo, scan, recording, transcript).
Shares its source's ID once processed; photos are never renamed.
*Example: `rose-wedding-1955.jpg` keeps that filename forever; once processed it belongs to source `S-7f3a9c2b1d`.*

**Claim** (`C-`) - a single sourced assertion (a date, place, relationship, attribute).
Lives inside its source record; moves through a review lifecycle.
*Example: "born 12 March 1898 in Leeds," read off a birth certificate and marked `accepted`.*

**EDTF** - Extended Date/Time Format (ISO 8601-2).
How all dates are written, so approximate and partial dates are first-class (`1850~`, `185X`, `1871-02/1871-03`).
*Example: you say "about 1880"; the tool stores `1880~`. You say "the 1880s"; it stores `188X`.*

**FAN club** - Friends, Associates, and Neighbors.
Researching the people *around* a family; how brick walls fall.
*Example: the witness who signed a marriage record turns out to be the bride's uncle - and cracks open her side of the tree.*

**fha** - "family-history archive," the command suite specified in `TOOLING.md`.
Deterministic tools the AI agent runs as its hands.
*Example: `fha process`, `fha report`, `fha doctor` - you ask in plain English, the assistant runs these.*

**Hypothesis** (`H-`) - an unsourced working theory.
A guess, never a fact.
Verification mints a real claim and records the link.
*Example: "maybe the John Hartley in the 1881 census is our John" - stored as `H-…`, never cited as if proven.*

**Index** - a rebuildable SQLite cache regenerated from the files.
Powers search, trees, reports.
Never authoritative.
*Example: delete `.cache/index.sqlite` and rebuild it any time with `fha index` - nothing is lost.*

**Link** (`[[ ]]`) - how prose connects to records: a name or ID in double brackets, like `[[Grandpa Joe]]` or `[[S-1a2b3c4d5e]]`. It's plain text you can read in any editor, a clickable link in a wiki-style editor, and it always resolves to a stable ID underneath. A citation on a fact is just a link to its source.
*Example: "born in [[L-7c1a9f4e22|Fairview]]" links the place; "[[S-1a2b3c4d5e]]" after a sentence cites the evidence.*

**Person** (`P-`) - a human.
Identity, flags, and prose; their sourced facts live in claims. A person record may also hold a **provisional** birth/death estimate (see *Provisional date*) until a sourced claim supersedes it.
*Example: Thomas Edward Hartley lives in a file named `hartley__thomas_edward_P-9c2f4a8b1e.md`.*

**Provisional date** - a birth or death date you jot down before you've found the record that proves it. Recording what you know now is a normal, encouraged starting state, not an error: the tools simply remind you it still needs a source, and when you add a sourced birth/death claim it takes over.
*Example: you're fairly sure great-grandma was born around 1849, so you write `birth: 1849~` on her record - and it shows up on the "still to source" list until a certificate turns up.*

**Place** (`L-`) - a physical location, identified by coordinates, with a dated name/jurisdiction history.
One record per physical place, forever.
*Example: one `L-…` record for Leeds, England - even though the record itself notes it was "Leeds, Yorkshire" in 1880.*

**Processing** - turning a raw inbox file into a Source: minting an ID, marking identity, scaffolding the record, drafting claims for review.
*Example: you drop a census scan in `inbox/` and say "process it"; back come suggested names, dates, and places to approve.*

**Source** (`S-`) - a piece of evidence with its own record: citation, metadata, file inventory, and the claims it supports.
*Example: the 1900 U.S. Census page for the Hartley household, recorded as `S-1a2b3c4d5e`.*

**Source stub** - the half-formed middle state: an inbox asset plus freeform notes, before it's a processed Source.
No ID yet.
*Example: `inbox/grandmas-album/` holding the scans plus a `notes.md` of your hunches - real material, no ID assigned yet.*

**Status lifecycle** - `suggested → needs-review → accepted | disputed | rejected | superseded`.
Human review is the only gate to `accepted`.
AI output always starts at `suggested`.
*Example: the assistant's guess at a birthplace sits at `needs-review` until you say "yes" - then, and only then, it becomes `accepted`.*

**Stub (person)** - a person record with frontmatter only (an ID and a name).
A permanent, legitimate state for people referenced but not yet researched.
*Example: "Uncle Pat," named in a letter, gets a `P-…` record with just his name - fleshed out later, or never, and that's fine.*


---

*A note on line wrapping: how you break lines inside a file is purely an authoring
choice - Markdown renders a paragraph the same whether it's one line or many, and the
tools don't care. Write research, biographies, and claim notes as natural paragraphs.
The spec documents use one-sentence-per-line because they're revised often and that
keeps git diffs readable, but it's a convention for those docs, not a rule for your
archive.*
