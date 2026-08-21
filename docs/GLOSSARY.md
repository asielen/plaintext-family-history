# Glossary

**Affiliation** - a person's membership in an organization - a tribe, a military unit, a lodge, an employer, a church. It's recorded as a relationship (you *belong to* the group), with the group's name written in plain text. The membership is something you can search and connect; the organization itself stays a name, not a separate record.
*Example: "Enrolled member, Cherokee Nation (1902 Dawes Roll)" is a membership relationship on the person, with "Cherokee Nation" as the organization.*

**Alias** - any name that points at a record: its ID, its filename, a person's name and nicknames, a place's other names. It's what lets you link by name - type `[[Grandpa Joe]]` and the archive knows which person you mean. The tools keep each record's aliases in sync so your name-links keep working even after files are renamed.
*Example: a person named "Margaret A. Cole" also answers to `[[Margaret Cole]]`; both resolve to the same record.*

**Archive** - the durable, file-first store: plain text and standard formats on disk.
The source of truth.
*Example: the `my-family-archive` folder on your computer - every record in it opens in Notepad, today or in fifty years.*

**Asset** - an actual file (photo, scan, recording, transcript).
Shares its source's ID once processed; photos are never renamed while they live in the photo library.
*Example: `rose-wedding-1955.jpg` keeps that filename forever; once processed it belongs to source `S-7f3a9c2b1d`.*

**Bloodline** - the genetic line the family-tree numbers follow. When a parent is adoptive, a step-parent, or a guardian, that bond is real and shown everywhere it should be - but the pedigree *numbering* counts only the genetic parents, so an adopted child doesn't accidentally land in the wrong branch of the blood tree. Both kinds of parent stay visible; only the numbering is blood-aware.
*Example: Ruth was adopted into the Hartley line - her adoptive parents show on her page and in the family folder, but the Ahnentafel numbers run through her birth parents.*

**Claim** (`C-`) - a single sourced assertion (a date, place, relationship, attribute).
Lives inside its source record; moves through a review lifecycle.
*Example: "born 12 March 1898 in Leeds," read off a birth certificate and marked `accepted`.*

**EDTF** - Extended Date/Time Format (ISO 8601-2).
How all dates are written, so approximate and partial dates are first-class (`1850~`, `185X`, `1871-02/1871-03`).
*Example: you say "about 1880"; the tool stores `1880~`. You say "the 1880s"; it stores `188X`.*

**FAN club** - Friends, Associates, and Neighbors.
Researching the people *around* a family; how brick walls fall.
*Example: the witness who signed a marriage record turns out to be the bride's uncle - and cracks open her side of the tree.*

**Gender and sex** - two optional fields on a person. `sex` is what records typically state (`M`, `F`, `intersex`, or `unknown`); `gender` is a person's identity, written in plain words. Use either, both, or neither - neither is required, and old records that only ever noted "sex" keep working unchanged.
*Example: you note `sex: F` from a birth certificate and, where you know it, `gender: woman` - or you leave both blank and nothing is lost.*

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
Identity, flags, and prose; their sourced facts live in claims. A person record may also list their **relationships** in plain words - who their parents, spouse, and children are, and the *nature* of each tie - with each one linked to the source that backs it, or marked as a belief until one is found. It may also hold a **provisional** birth/death estimate (see *Provisional date*) until a sourced claim supersedes it.
*Example: Thomas Edward Hartley lives in a file named `hartley__thomas_edward_P-9c2f4a8b1e.md`.*

**Provisional date** - a birth or death date you jot down before you've found the record that proves it. Recording what you know now is a normal, encouraged starting state, not an error: the tools simply remind you it still needs a source, and when you add a sourced birth/death claim it takes over.
*Example: you're fairly sure great-grandma was born around 1849, so you write `birth: 1849~` on her record - and it shows up on the "still to source" list until a certificate turns up.*

**Place** (`L-`) - a physical location, identified by coordinates, with a dated name/jurisdiction history.
One record per physical place, forever.
*Example: one `L-…` record for Leeds, England - even though the record itself notes it was "Leeds, Yorkshire" in 1880.*

**Primary sort name** - the part of a person's filename that decides where they sort, so families land together. For most Western records that's the birth surname. But not everyone has a surname - mononyms, people recorded by a single given name, patronymics, foundlings - so the rule is simply "the name we sort by," and people with no surname sort in their own group. Whatever a person's full name truly is, it always lives in the record itself; the filename is just a tidy handle.
*Example: an ancestor recorded only as "Caesar" gets a file that sorts in the no-surname group; his page still reads "Caesar" in full.*

**Processing** - turning a raw inbox file into a Source: minting an ID, marking identity, scaffolding the record, drafting claims for review.
*Example: you drop a census scan in `inbox/` and say "process it"; back come suggested names, dates, and places to approve.*

**Relationship calculator** - a quick way to ask "how are these two people related?" It answers two ways: the blood relationship (second cousin, great-grandfather, and so on) by following the genetic line, and the plain-language path between any two people, even when they aren't blood relatives at all.
*Example: ask how two cousins connect and get "second cousin once removed"; ask about two acquaintances and get "your brother's friend's sister's father."*

**Relationship subtype** - the *nature* of a tie between two people, not just its label. A father can be a *biological* father or an *adoptive* one; both are real, and recording which keeps the archive from treating two true things as a contradiction. The same idea covers step-, foster-, and guardian bonds, surrogacy and donor conception, and ties that are not kin at all - an employer, a fellow member of a unit or lodge, or the grim ones genealogy must hold honestly, an enslaver and the person they enslaved.
*Example: a child has two "parent" entries - `subtype: biological` for the father a DNA test confirms, `subtype: adoptive` for the father who raised him - and the tools count both as valid.*

**Restricted** - a private flag you can put on almost anything: a whole person, a single sensitive fact, a source, or a name someone no longer uses. Restricted material stays safely in your archive, it just never leaves in anything you share publicly, and it's left out of family packets unless you deliberately include it. A few kinds are extra-protected - DNA, and anyone who asked to be left out entirely - and those stay out no matter what.
*Example: a relative asks not to appear in anything you share - you mark them `restricted: by-request`, and no export ever includes them, while their place in the tree is quietly kept.*

**Source** (`S-`) - a piece of evidence with its own record: citation, metadata, file inventory, and the claims it supports.
*Example: the 1900 U.S. Census page for the Hartley household, recorded as `S-1a2b3c4d5e`.*

**Source stub** - the half-formed middle state: an inbox asset plus freeform notes, before it's a processed Source.
No ID yet.
*Example: `inbox/grandmas-album/` holding the scans plus a `notes.md` of your hunches - real material, no ID assigned yet.*

**Status lifecycle** - `suggested → needs-review → accepted | disputed | rejected | superseded`.
Human review is the only gate to `accepted`.
AI output always starts at `suggested`.
`needs-review` is both a waypoint and a legitimate resting place: where a drafted fact waits for your look, *and* the verdict you give when you've looked and can't settle it yet ("possible - but I want a second record before I ink it"). A parked claim stays active and keeps matching against new evidence, and its review date shows when you last weighed it. `disputed` is different - it marks a claim your sources actively contradict.
*Example: the assistant's guess at a birthplace sits at `needs-review` until you say "yes" - then, and only then, it becomes `accepted`. Or you look at it, stay unsure, and deliberately leave it parked at `needs-review` - a recorded "not proven yet," not a forgotten one.*

**Stub (person)** - a person record for someone referenced but not yet researched. Same shape as a curated record - an ID, a name, and every section (Sources, Biography, Stories, Research Notes, Friends & Family) scaffolded from the start - just with little or nothing filled in yet, and filed in `people/stubs/` instead of a couple folder until it earns one.
A permanent, legitimate state, not a backlog.
*Example: "Uncle Pat," named in a letter, gets a `P-…` record with just his name and empty sections - fleshed out later, or never, and that's fine.*

**Translation** - an English (or any-language) rendering of a non-English source, filed right beside the original as its own kind of file. The original stays untouched and verbatim; the translation is a helper a tool can keep track of, with the language of each file noted so nothing is mistaken for the original wording.
*Example: a German baptism record keeps its scan and a word-for-word transcription in German, plus an English translation filed alongside - all three under the same source.*


---

*A note on line wrapping: how you break lines inside a file is purely an authoring
choice - Markdown renders a paragraph the same whether it's one line or many, and the
tools don't care. Write research, biographies, and claim notes as natural paragraphs.
The spec documents use one-sentence-per-line because they're revised often and that
keeps git diffs readable, but it's a convention for those docs, not a rule for your
archive.*
