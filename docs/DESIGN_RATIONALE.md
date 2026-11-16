# Design rationale

Why the archive is shaped the way it is.

`SPEC.md` says what the rules are. This document says why, and what was
considered and turned down. It is written for someone who opens the archive
long after the people who built it are gone, and wants to know whether a rule
is load-bearing before changing it.

Each topic has three parts: what we do, why, and what we rejected. When this
document and `SPEC.md` disagree, `SPEC.md` wins and this document is out of
date.

Contents:

1. Files, not a database
2. Scope: what Plaintext is not
3. The relationship model
4. Status, confidence, and the research ladder
5. The derivation principle
6. Deferred until needed: events and organizations
7. Ideas turned down: fixity checksums, privacy tiers, source-debt IDs
8. The workbench: a local browser page
9. Capture: one record, on the user's click
10. Working in Obsidian

---

## 1. Files, not a database

**What we do.** The archive is a folder of plain files: Markdown records with
YAML at the top, a YAML place list, and the original scans, photos and
recordings. The files are the record. The search index is a SQLite file under
`.cache/`, rebuilt from the files whenever it is stale. Delete it and nothing
is lost.

**Why.** For most of the history of genealogy, research lived in filing
cabinets, and anyone could open the drawer. No login, no subscription, no
schema migration. A century later a descendant could still pull the folder and
read it. Software keeps losing that: when a program goes away, the research
inside it can go too. So the files must work with no tools at all, by hand, and
every tool is a replaceable convenience laid on top.

**The archive test.** Every tool that touches the archive must pass six checks:

1. **Originals untouched.** The tool never turns a scan, photo or recording into
   a lossy copy and keeps the copy instead. The picture is the evidence.
2. **Rebuildable.** Anything it generates can be rebuilt from the files.
3. **Links to stable IDs**, or at least does not block adding them later.
4. **Plain export.** No knowledge is trapped in a store only the tool can read.
5. **Privacy-aware.** It respects the `living` and `restricted` flags.
6. **Disposable.** If the tool dies, the archive survives without it.

A tool that fails 1, 2 or 6 may never own part of the archive itself. It may
still serve as an editor, an index, or a way to publish. The short form of the
test: can data get in and out with a small script, or does the tool want to be
the center?

The same test explains why IDs are random and need no registry (`SPEC.md` §10).
Sequential IDs would need a counter kept somewhere, and that counter would be a
database the archive depends on. With random IDs the archive itself is the
registry: walk the files and you have every ID in use.

**What we rejected.**

- **A database-first genealogy program as the core** (Gramps Web, webtrees).
  These keep the truth in a database behind a web server. That is the exact
  inverse of files as the truth. Adopting one would replace the archive, not
  serve it. Their page layouts are still worth studying, and a GEDCOM export can
  feed one if a family ever wants a hosted tree.
- **A hosted AI notebook as the working model** (NotebookLM). It is not
  self-hosted, not files-first, and offers no durable export of the reasoning.
  At most it is a reading aid, never part of the archive.
- **A converter that turns everything into Markdown.** Converting a web page or
  a transcript to clean text is useful. Converting a scan or a photo is not:
  for documents and photos the image is the evidence, so a text conversion
  fails check 1.
- **A prompt-first research system with no claim records.** Several projects
  keep genealogy in Markdown and drive an AI with good research prompts. They
  are useful playbooks for how to search. But without a claim record, a status
  and an index, they cannot keep an AI's suggestion apart from a fact a person
  checked, and "every fact traces to a source" stays a habit rather than a rule
  the tools enforce. Plaintext is the filing system those playbooks lack.

The AI working environment follows the same rule. One agent harness is the
current choice, but nothing in the archive depends on it: the agent
instructions (`AGENTS.md`) and skills are open text files, and switching
harness is meant to cost an afternoon.

---

## 2. Scope: what Plaintext is not

**What we do.** Plaintext is the home for the evidence you find and the memory
of what you have already searched. It files records, keeps them traceable, and
lets an AI help with the labor of reading what you have already captured.

It is **not a record finder.** It does not search Ancestry, FamilySearch or any
other site for you. It does not offer "hints" pointing at other people's trees
or records. The connections it suggests come only from your own archive (two
people who keep turning up in the same sources, a place named the same way in
many claims), and each one waits for your yes or no.

It is **not a DNA match workbench.** It stores DNA *conclusions*, as claims and
proof arguments, always kept private. Match lists, shared centimorgans and
triangulation belong in the tools built for them.

It does **not restore or colorize photos.** Tools never alter pixels.

**Why.** A blind sample of about 3,000 posts from a large online genealogy
forum, gathered with no search terms, sorted like this:

| What the post wanted | Share |
|---|---|
| Help finding, getting or reading a record (a brick wall, a transcription, a DNA match) | about 69% |
| Mentioned an unusual family situation (adoption, misattributed parentage, naming, several marriages) | about 5% |
| Asked how to *record* or represent something | about 0.2% |

Most of what genealogists struggle with is the labor of research, and the big
record sites already exist to do the finding. Plaintext deliberately does not
compete there. Saying so plainly turns "missing features" into intended scope.

What the sample does show Plaintext answering well: the dated research log,
which records empty searches so a dead collection is not searched again; name
variants and birth surnames; two people with the same name kept apart by ID;
and conflicting records kept as a citable state rather than a mistake to hide.

The small 5% still shaped the data model. It is where the relationship nature
field (topic 3), the enslaver and enslaved relationship types, the translation
file role, the "left out by request" privacy marker, and the rule for one
person who sits at two places in the pedigree all came from.

**What we rejected.** Logging in to record sites, paging through results, or
downloading in bulk on the tool's own initiative (see topic 9). Building DNA
match management. Building a search engine over other people's trees.

---

## 3. The relationship model

**What we do.** A relationship is a claim, like a birth or a residence. It
lives in the record of the source that states it. The claim names the people
in `persons:` and says what each one is in a `roles:` map (`child`, `parent`,
`spouse`, and so on). A separate field, `subtype`, says what *kind* of bond it
is: `biological` (the default), `adoptive`, `step`, `foster`, `guardian`, the
surrogacy and donor kinds, `social`, and the non-family ties (`enslaver` and
`enslaved-by`, `employer`, `member-of`, `friend`, `associate`, `neighbor`).

Role and nature are two different questions. The role says where the person
sits ("Robert is the parent"). The nature says what kind of parent ("adoptive").

A person's record may also carry a `relationships:` list. This is a working
surface, written by hand or by the tools. A *sourced* entry points at the claim
that backs it, and the linter checks the two agree and that the other person's
record has the mirror entry. An *unsourced* entry is marked
`status: hypothesis`: a guess you are building on (topic 4). The claim is
always the durable record. Delete the list and a reindex rebuilds every sourced
edge from the claims.

Family trees are never stored. They are drawn from accepted relationship
claims each time. The pedigree numbers (Ahnentafel) follow only genetic parent
edges. Adoptive, step and foster parents are shown, clearly marked, but not
numbered into the pedigree.

**Why.**

- **Two true parents are not a conflict.** Without a nature field, an adoptive
  father and a biological father are both just "parent". The only way the model
  had to relate two such claims was `contradicts`, which would call two true
  things a conflict. The same problem hits misattributed parentage (a DNA
  father and the father who raised the child, both real), donor conception and
  surrogacy. With `subtype`, each is a separate, equally valid edge.
- **Most of what you know starts as a guess.** A name an aunt mentioned, a
  father a DNA match implies: you know these long before you find a record.
  The person's `relationships:` list lets you write that down honestly, marked
  as a guess, and it graduates when the source turns up. This is the same
  pattern as the provisional `birth:` and `death:` dates on a person record.
- **The claim is the unit of traceability.** Supersession, corroboration,
  contradiction and the index all hang off claims. Keeping relationships as
  claims keeps them traceable to evidence.
- **Roles are a fixed list, kept in the spec.** Tools build tree edges only
  from a closed list of roles, plus six kin synonyms (`mother` and `father` read
  as `parent`, and so on). Any other word (`sibling`, `godparent`, `witness`) is
  valid and places the person on the claim, but builds no edge. The list is not
  configurable per archive: a meaning stored in one archive's settings would be
  lost the moment a record left that archive.

**What we rejected.**

- **A generated, read-only relationship list on the person record.** It could
  not be written by hand, and it could not hold a guess at all, since generated
  views show only accepted claims.
- **Making the person record the owner of relationships**, with the source as a
  pointer. It is the tidiest single surface, but it would weaken the claim as
  the unit everything else rests on.
- **Reading meaning from the order of `persons:`** ("the first person is the
  child"). Order is a habit, not a contract. It is too fragile for exporters
  and tree building. Order never carries meaning on any claim.
- **A per-archive list of roles** (see above).
- **A separate "legal" flag on adoptions.** The subtype and the claim's notes
  carry it for now.

---

## 4. Status, confidence, and the research ladder

**What we do.** A claim has two separate dials.

- **Status** is the review state: `suggested`, then `accepted`, `disputed`,
  `rejected` or `superseded`. Nothing reaches `accepted` without a person's
  decision.
- **Confidence** is the quality of the evidence: `high`, `medium` or `low`.

A family story can be `accepted` (yes, this is what an aunt said) and `low`
confidence (but it may not be what happened). Shared output shows only accepted
claims, and marks the low-confidence ones.

There is no "possible" or "maybe" status. The two cases people reach for are
already covered:

- "I'm building on this, but I'm not certain": `accepted` with `confidence: low`.
- "I looked at this and can't decide yet": leave it `suggested` and add the
  `reviewed:` date. A dated suggested claim reads "checked, still open". An
  undated one reads "not looked at yet".

Everything not yet proved climbs one ladder, written in one place, the question
entry (`SPEC.md` §17):

1. **Question.** Something not known.
2. **Working answer.** The same entry, with a guess, the reason for it, and what
   would settle it. It gains an `h-` id so other records can point at it.
3. **Build on it.** A guessed relationship may go on the person's
   `relationships:` list marked `status: hypothesis`, so research can carry on
   past it. The tree and the site draw it as a guess. Exports leave it out.
4. **Settle.** A source says it (a normal claim). Or the pieces add up, and the
   reasoning is written as a proof-argument source that the claim cites. Or it
   was wrong: the entry is marked `abandoned` and kept, so nobody makes the same
   guess again.

**The test between a weak source and a working answer.** If anyone or anything
other than the researcher says it (a family story, an index entry, another
researcher's tree), it is a source, with a low-confidence claim. If only the
researcher says it, it is a working answer. A guess is never a claim, because a
claim needs a source.

**Why.**

- **Review and evidence quality are different questions.** Folding them into
  one status list would lose one of them. A "possible" status would mix "has a
  person looked?" with "how good is the record?".
- **One ladder instead of five mechanisms.** Open questions, separate hypothesis
  records, a "needs review" status, accepted-but-low claims and guessed tree
  edges all held the same idea in different places. A reader had to learn five
  things. Now there is one entry that grows as the research does.
- **An `h-` id never becomes a `c-` id.** IDs are typed for life. Settling a
  working answer creates a new claim from the source that proves it. Both
  records stay, and the claim may point back at the working answer.
- **Gaps a tool can compute are never written as questions.** "No birth date"
  or "no parents" is shown fresh by `fha report` every time. A written copy
  would only go stale. A question is written down only when a person asks it or
  two claims conflict.

**What we rejected.** A separate `needs-review` status (a dated `suggested`
claim says the same thing). A "possible" or "uncertain" status. Hypotheses as
a separate kind of record apart from questions.

---

## 5. The derivation principle

**What we do.** Tools derive trees, timelines, completeness and exports from
claims. They follow one rule: **honor what the record actually says, refuse to
guess where it is silent, and make the refusal visible.**

Some examples from `SPEC.md` §8.3:

- A birth claim gives a parent edge only when its `roles:` map names a `child`
  and a `parent`. With no map, it gives no parent edge, even if the claim
  names three people.
- A marriage claim that names exactly two people and no other roles reads as
  that couple, because marriage is symmetric and two people admit only one
  answer. Parentage is directed, so there is no such fallback for births.
- A death ends only the dead person's own marriages, never the marriage of
  someone else the record names.
- A role word that is plainly a misspelt derivation role (`parnet:`) is
  reported, not quietly treated as a description.

Each refusal is reported. The linter lists every accepted claim that names
several people and yields no answer, so the owner can add the missing role.

**Why.** A false edge is read back as fact by everything downstream: the
relationship calculator, the tree views, the report and the GEDCOM export. A
false parent drags a whole ancestral line with it. A missing edge is merely
missing. **Silence is recoverable; a false fact is not.** So the tools derive
nothing they cannot read off the record, and say so.

Two refinements keep this honest:

- **A warning must be clearable.** A claim whose roles place everyone has said
  all its record says, and is not reported. A warning that nothing could clear
  short of inventing a role would teach the owner to ignore the real ones.
- **One rule, one place.** Every tool that derives an edge imports the same
  shared rule. A rule implemented twice drifts, and two tools then disagree
  about the same family.

The same ordering guides which problems get fixed first: a confident wrong
answer with no error ranks above a loud failure, because the archive's whole
promise is to be the trustworthy record.

**What we rejected.** Guessing parentage from the order of names. Pairing off
everyone a marriage record names (that would make a man his father-in-law's
husband). Silent fallbacks that produce an answer when the record gave none.

---

## 6. Deferred until needed: events and organizations

**What we do.** Two things that many genealogy programs treat as records of
their own are, here, not records.

- **Events.** A birth, a census, a wedding is a *claim type*. An event shared
  by several people (a household in a census, a marriage) is one claim that
  names them all, with a `roles:` map.
- **Organizations.** A regiment, a lodge, an employer, a tribe is a *value* in a
  claim. A person's membership is a `relationship` claim with
  `subtype: member-of` or `employer`. The optional `value_org` field (Appendix
  A.5) lets views group "everyone in Company K". There is no organization record
  and no `org-` id.

**Why.** Claims already cover these cases. A new kind of record is added only
when it earns its place, not in advance of a need.

**The tripwire.** Each stays deferred until a real strain shows:

- An event becomes a record of its own only if multi-person claims prove
  awkward in practice: the same shared event copied, clumsily, across many
  claims. The strain is the signal; until it is felt, events stay claim types.
- An organization becomes a record only when cross-referencing organizations
  needs more structure than a shared name in `value_org` can give.

Until then the simpler model holds.

**What we rejected (for now).** An event object. An organization object.

---

## 7. Ideas turned down: fixity checksums, privacy tiers, source-debt IDs

**Fixity checksums in the archive.** *What we do:* the archive does not store a
checksum for every file, and the linter does not check for silent corruption.
*Why:* detecting bit rot or silent file changes is a preservation concern, and
it belongs to the backup strategy, outside the archive format. An optional
checksum check in `fha doctor` could be added later without making checksums
part of the research model. Checksums are used in one place where they earn their keep: `fha
update-tools` compares them to tell which tool files the owner has customized,
so an update never overwrites the owner's changes.

**Privacy tiers.** *What we do:* two flags, no tier system. `living` (`true`,
`false` or `unknown`) drives redaction, and `unknown` counts as living, so a
newly created person is private until someone says otherwise. `restricted`
marks a source, a claim, a person or a single name to keep out of what leaves
the archive. Two restricted types can never be overridden by a flag: `dna`
(only with its own `--include-dna`) and `by-request` (a person who asked to be
left out). *Why:* a flag appears only where it applies, on the exact thing it
protects. Uncertainty is safe by default. *Rejected:* a ladder of privacy
levels, and choosable privacy modes on family packets. Public output follows
one fixed rule set, and a family packet follows its own fixed rules.

**Source-debt IDs.** *What we do:* a useful sentence still waiting for its
source is marked `(TODO: import source)`. The linter lists every marker. *Why:*
a marked sentence is uncited, and uncited prose already reads as story or
context, never fact. So the marker needs no ID and no special export rule.
*Rejected:* giving each missing source its own ID to track.

---

## 8. The workbench: a local browser page

**What we do.** `fha serve` starts a small web server on the owner's own
computer. It shows the archive as web pages, with a review queue and an inbox,
and lets the owner make the same decisions the commands make: accept a claim,
fix a date, mark someone living or dead, confirm a suggested connection.

Its rules:

- **Every button is a documented command.** The page calls the same engine the
  terminal command calls, and shows the command it ran. It holds no logic of
  its own.
- **Preview, then confirm.** Every change is shown before it is written.
- **It owns nothing.** Its only state is a cache folder. Stop it, delete it, and
  nothing is lost. The static site never depends on it.
- **Local only, started by hand.** It listens only on the computer it runs on,
  and runs only while the owner has it open. It never starts itself, never
  watches files, never sweeps.

**Why.** The owner this is built for is comfortable clicking around a website
and does not want a terminal. Adding a note or dropping a scan into the inbox
already works with a file browser. What hurt were the structured decisions,
above all accepting a claim, which meant hand-editing a status inside a block
of YAML: the scariest edit in the archive. Those decisions already existed as
commands. The only missing piece was a gentler door to them. A thin problem
deserved a thin answer.

A local page served by Python's standard library has almost no upkeep: no
packaging, no code signing, no app store review, no framework to chase. If it
is ever abandoned, it fails cleanly. The commands, the AI and the files all
still work; only a web address stops answering.

**What we rejected.**

- **A desktop app (Electron, Tauri).** It buys a dock icon at the price of
  per-system builds, code signing, an auto-updater and a framework that moves
  every few months. For one maintainer that is how a convenience becomes a
  liability, and an abandoned app leaves users with a broken program rather
  than a page that stopped loading. The local server is already the backend
  such an app would need, so one could be added later without rework.
- **An Obsidian plugin as the main front door.** Claims sit in fenced YAML that
  plugins do not parse, so a plugin would have to reimplement the careful edit
  logic in a second language (two copies that drift) or call Python from inside
  Obsidian (fragile, desktop-only, two runtimes). And the owner this serves
  does not use Obsidian. A plugin remains possible later for people who do.
- **Turning the capture browser add-on into an editor.** A browser add-on
  cannot read or write the archive's files without a privileged local pipe,
  it would need a second copy of the claim logic, and capture happens on other
  people's websites while editing happens at the desk. The add-on stays an
  intake tool.

---

## 9. Capture: one record, on the user's click

**What we do.** `fha capture`, and the browser add-on that feeds it, turn the
record page the owner is looking at into a rough source waiting in the inbox.
It reads only the open page, in the owner's own logged-in session. It does not
log in, page through results, or download behind a login on its own
initiative. Everything it produces is a suggestion until reviewed.

Two narrow exceptions fire only on the owner's own Capture click, one record at
a time: on an Ancestry image page it makes the same single download request
Ancestry's own Download button makes, and on an open-standard (IIIF) archive it
fetches the one full-size image, with no login.

**Why.** Capture exists to file the record you are already looking at, not to
harvest a site. Fetching the one image on screen, when you click, is no
different from clicking the site's own button. Earlier bulk-download tools for
the same site broke when its private interfaces changed and ran against its
terms; the one-record rule avoids both.

**Lessons from building the site readers.**

- **The browser stays simple; the Python recipe decides.** The add-on fills in
  only obvious hints (title, address, date). The per-site reading of a census
  table or an index entry happens in Python, at ingest, from the saved page.
  Two copies of the same parsing, one in each language, would drift apart.
  Where one small rule must run in both places (the record type and date hints),
  both copies are held to one shared table of test cases.
- **Always keep the raw page.** The page's HTML is saved in every capture. A
  wrong guess in the browser then costs nothing: the recipe corrects it at
  ingest, and the owner's own typed corrections win over both.
- **Field labels are not people.** Sites lay out records as label and value.
  Without a guard, labels like "Birth Date" end up in the list of names. One
  shared guard fixed this for every site.
- **Sites fall into tiers.** Pages that publish standard metadata need only a
  light recipe. The big record sites, which build their pages in script with no
  metadata, need table readers. Public archives using the IIIF image standard
  are the easiest and cleanest.
- **Never claim a better copy than you have.** A screenshot is labelled
  provisional. A citation with no file is labelled "held elsewhere". A saved
  page says it is a copy of the page, not the original record. A site's own
  index of a record is filed as site data, never as a transcript.
- **Do not invent what the page does not say.** A memorial site gives a death
  date but no burial date; capture does not make one up. A volunteer's
  compilation is filed as a website, a derivative source, not as a vital
  record, so its claims do not default to high confidence.
- **Image tiles only when there is no other way.** Where a site hands over the
  assembled full image, stitching tiles is rejected: it is heavier and more
  plainly working around the site. It is used only where the site offers no
  full-size image, one tile at a time, slowing down when the site asks.
- **Nothing is thrown away.** Ingested bundles move to a holding folder; they
  are never deleted by the tool.

**What we rejected.** Bulk or automated downloading. Logging in for the user.
Per-site parsing in the browser. A capture that writes a finished record
rather than a stub for review.

---

## 10. Working in Obsidian

**What we do.** An archive is an Obsidian vault as it stands: Markdown files,
YAML frontmatter, and `[[ ]]` links. Point Obsidian at the folder and it opens,
with no import. Links are written `[[id|Name]]`: the double brackets make a live
link in any wiki-style editor, the ID inside keeps the link stable across
renames, and the name after the bar is only for display. Each record lists its
ID and names in `aliases:`, so a link by name (`[[Thomas Edward Hartley]]`)
resolves too.

Claims sit in a fenced YAML block under `## Claims` in their source's record.

**Why the fence.** A claim belongs beside the evidence it rests on, in that
source's own file. An unfenced YAML list renders as garbled Markdown, so the
claims block is fenced to stay readable in any viewer.

**What the fence costs.** Obsidian's query plugins (Dataview, Bases) read
frontmatter, not the inside of a fenced block, so they cannot query claims.
Claims are queried through the `fha` tools and the index instead. The person
record's frontmatter (vitals, the `relationships:` list, flags) is ordinary
frontmatter and answers the common questions ("everyone with no death date").
A link to a single claim lands on its source's file, not the claim itself.

Other limits worth knowing:

- Obsidian silently picks one of two notes that share an alias. `fha lint`
  reports the clash; a shared name is then pinned to its ID.
- With a couple of thousand people, the global graph is an unreadable tangle,
  as in any large vault. The useful view is the local graph around one person.
- The privacy flags protect what the tools export. Publishing or syncing the raw
  vault to a public place bypasses them.
- On a phone the archive is for reading and jotting. Processing happens at the
  desk.

**What we rejected.**

- **Name-only links.** A link by name breaks when the name changes and is
  ambiguous when two people share it. The ID inside the brackets fixes both.
- **A generated "claim mirror" note** that copies claims into queryable
  frontmatter. It would duplicate every claim and add a file to keep in sync,
  and the person frontmatter already answers the common questions.
- **Making Obsidian, or any one editor, required.** It is one good way to read
  and write the files. The archive works the same in a plain text editor.
