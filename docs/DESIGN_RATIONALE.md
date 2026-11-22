# Design rationale

Why the archive is shaped the way it is.

`SPEC.md` says what the rules are. This document says why, and which alternatives it turns down. It is written for a later maintainer who wants to know what a rule protects before changing it.

Each topic has three parts: the rule, why, and what was rejected. When this document and `SPEC.md` disagree, `SPEC.md` wins and this document is out of date.

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
11. Geocoding: one table, one yes
12. Merging two sources
13. Linking place text: the hierarchy rule

---

## 1. Files, not a database

**The rule.** The archive is a folder of plain files: Markdown records with YAML at the top, a YAML place list, and the original scans, photos and recordings. The files are the record. The search index is a SQLite file under `.cache/`, rebuilt from the files whenever it is stale. Delete it and nothing is lost.

**Why.** For most of the history of genealogy, research lived in filing cabinets, and anyone could open the drawer. No login, no subscription, no schema migration. A century later a descendant could still pull the folder and read it. Software keeps losing that: when a program goes away, the research inside it can go too. So the files must work with no tools at all, by hand, and every tool is a replaceable convenience laid on top.

**The archive test.** Every tool that touches the archive must pass six checks:

1. **Originals untouched.** The tool never turns a scan, photo or recording into a lossy copy and keeps the copy instead. The picture is the evidence.
2. **Rebuildable.** Anything it generates can be rebuilt from the files.
3. **Links to stable IDs**, or at least does not block adding them later.
4. **Plain export.** No knowledge is trapped in a store only the tool can read.
5. **Privacy-aware.** It respects the `living` and `restricted` flags.
6. **Disposable.** If the tool dies, the archive survives without it.

A tool that fails 1, 2 or 6 may never own part of the archive itself. It may still serve as an editor, an index, or a way to publish. The short form of the test: can data get in and out with a small script, or does the tool want to be the center?

The same test explains why IDs are random and need no registry (`SPEC.md` §10). Sequential IDs would need a counter kept somewhere, and that counter would be a database the archive depends on. With random IDs the archive itself is the registry: walk the files and you have every ID in use.

**Rejected.**

- **A database-first genealogy program as the core** (Gramps Web, webtrees). These keep the truth in a database behind a web server. That is the exact inverse of files as the truth. Adopting one would replace the archive, not serve it. Their page layouts are still useful references, and a GEDCOM export can feed one if a family ever wants a hosted tree.
- **A hosted AI notebook as the working model** (NotebookLM). It is not self-hosted, not files-first, and offers no durable export of the reasoning. At most it is a reading aid, never part of the archive.
- **A converter that turns everything into Markdown.** Converting a web page or a transcript to clean text is useful. Converting a scan or a photo is not: for documents and photos the image is the evidence, so a text conversion fails check 1.
- **A prompt-first research system with no claim records.** Several projects keep genealogy in Markdown and drive an AI with good research prompts. They are useful playbooks for how to search. But without a claim record, a status and an index, they cannot keep an AI's suggestion apart from a fact a person checked, and "every fact traces to a source" stays a habit rather than a rule the tools enforce.

The AI working environment follows the same rule. One agent harness is the current choice, but nothing in the archive depends on it: the agent instructions (`AGENTS.md`) and skills are open text files, and switching harness is meant to cost an afternoon.

---

## 2. Scope: what Plaintext is not

**The rule.** Plaintext is the home for the evidence you find and the memory of what you have already searched. It files records, keeps them traceable, and lets an AI help with the labor of reading what you have already captured.

It is **not a record finder.** It does not search Ancestry, FamilySearch or any other site for you. It does not offer "hints" pointing at other people's trees or records. The connections it suggests come only from your own archive (two people who keep turning up in the same sources, a place named the same way in many claims), and each one waits for your yes or no.

It is **not a DNA match workbench.** It stores DNA *conclusions*, as claims and proof arguments, always kept private. Match lists, shared centimorgans and triangulation belong in the tools built for them.

It does **not restore or colorize photos.** Tools never alter pixels.

It is **not a website builder.** `fha site` renders the archive as a static snapshot that can be rebuilt at any time. Every customization is a plain file a person can edit in a text editor (`custom.css`, `notes/home.md`, the `site:` block in `fha.yaml`), and the generator never reads the HTML it wrote, so the same inputs always give the same site. A hand-edited page is folded back into those files by the reconcile-site-edits skill rather than by the generator.

**Why.** A blind sample of about 3,000 posts from a large online genealogy forum, gathered with no search terms, sorted like this:

| What the post wanted | Share |
|---|---|
| Help finding, getting or reading a record (a brick wall, a transcription, a DNA match) | about 69% |
| Mentioned an unusual family situation (adoption, misattributed parentage, naming, several marriages) | about 5% |
| Asked how to *record* or represent something | about 0.2% |

Most of what genealogists struggle with is the labor of research, and the big record sites already exist to do the finding. Plaintext deliberately does not compete there.

What the sample does show Plaintext answering well: the dated research log, which records empty searches so a dead collection is not searched again; name variants and birth surnames; two people with the same name kept apart by ID; and conflicting records kept as a citable state rather than a mistake to hide.

The small 5% still shapes the data model. It is where the relationship nature field (topic 3), the enslaver and enslaved relationship types, the translation file role, the "left out by request" privacy marker, and the rule for one person who sits at two places in the pedigree all come from.

**Rejected.** Logging in to record sites, paging through results, or downloading in bulk on the tool's own initiative (see topic 9). Building DNA match management. Building a search engine over other people's trees. A page builder, themes beyond `custom.css`, user-defined page types, WYSIWYG editing, plugins, or a live server for the shared site.

---

## 3. The relationship model

**The rule.** A relationship is a claim, like a birth or a residence. It lives in the record of the source that states it. The claim names the people in `persons:` and says what each one is in a `roles:` map (`child`, `parent`, `spouse`, and so on). A separate field, `subtype`, says what *kind* of bond it is: `biological` (the default), `adoptive`, `step`, `foster`, `guardian`, the surrogacy and donor kinds, `social`, and the non-family ties (`enslaver` and `enslaved-by`, `employer`, `member-of`, `friend`, `associate`, `neighbor`).

Role and nature are two different questions. The role says where the person sits ("Robert is the parent"). The nature says what kind of parent ("adoptive").

A person's record may also carry a `relationships:` list. This is a working surface, written by hand or by the tools. A *sourced* entry points at the claim that backs it, and the linter checks the two agree and that the other person's record has the mirror entry. An *unsourced* entry is marked `status: hypothesis`: a guess you are building on (topic 4). The claim is always the durable record. Delete the list and a reindex rebuilds every sourced edge from the claims.

Family trees are never stored. They are drawn from accepted relationship claims each time. The pedigree numbers (Ahnentafel) follow only genetic parent edges. Adoptive, step and foster parents are shown, clearly marked, but not numbered into the pedigree.

**Why.**

- **Two true parents are not a conflict.** Without a nature field, an adoptive father and a biological father are both just "parent". The only way to relate two such claims would be `contradicts`, which calls two true things a conflict. The same problem hits misattributed parentage (a DNA father and the father who raised the child, both real), donor conception and surrogacy. With `subtype`, each is a separate, equally valid edge.
- **Most of what you know starts as a guess.** A name an aunt mentioned, a father a DNA match implies: you know these long before you find a record. The person's `relationships:` list lets you write that down honestly, marked as a guess, and it graduates when the source turns up. This is the same pattern as the provisional `birth:` and `death:` dates on a person record.
- **Tools never rewrite a guess.** A guess is your own lines, often with a comment or a note on why you believed it. When a source proves the tie, the tools add the sourced entry beside the guess and leave the guess alone. Lint then names the leftover guess, and you fold it in yourself: delete the guess, move anything you want to keep onto the sourced entry, and keep its `hypothesis: h-…` line as the record of which working answer was settled.
- **Accepting a claim fills in the list, but only where a list is kept.** Writing the same tie by hand on two records per claim is exactly the edit that goes wrong, so `fha claim` writes the entries when you accept a relationship, marriage or birth claim, and takes the claim back out when you reject it. It never starts a list on a record that has none: once a record has a list, the linter expects it to hold every tie, so starting one would turn a single accept into a page of warnings about ties you never chose to write down.
- **The claim is the unit of traceability.** Supersession, corroboration, contradiction and the index all hang off claims. Keeping relationships as claims keeps them traceable to evidence.
- **Roles are a fixed list, kept in the spec.** Tools build tree edges only from a closed list of roles, plus six kin synonyms (`mother` and `father` read as `parent`, and so on). Any other word (`sibling`, `godparent`, `witness`) is valid and places the person on the claim, but builds no edge. The list is not configurable per archive: a meaning stored in one archive's settings would be lost the moment a record left that archive.

**Rejected.**

- **A generated, read-only relationship list on the person record.** It could not be written by hand, and it could not hold a guess at all, since generated views show only accepted claims.
- **Making the person record the owner of relationships**, with the source as a pointer. It is the tidiest single surface, but it would weaken the claim as the unit everything else rests on.
- **Reading meaning from the order of `persons:`** ("the first person is the child"). Order is a habit, not a contract. It is too fragile for exporters and tree building. Order never carries meaning on any claim.
- **A per-archive list of roles** (see above).
- **A separate "legal" flag on adoptions.** The subtype and the claim's notes carry it for now.
- **Graduating a guess automatically**, turning it into the sourced entry when the evidence arrives. That means deleting the owner's lines, and their notes and comments go with them. Leaving the guess beside its evidence costs only one lint warning.

---

## 4. Status, confidence, and the research ladder

**The rule.** A claim has two separate dials.

- **Status** is the review state: `suggested`, then `accepted`, `disputed`, `rejected` or `superseded`. Nothing reaches `accepted` without a person's decision.
- **Confidence** is the quality of the evidence: `high`, `medium` or `low`.

A family story can be `accepted` (yes, this is what an aunt said) and `low` confidence (but it may not be what happened). Shared output shows only accepted claims, and marks the low-confidence ones.

There is no "possible" or "maybe" status. The two cases people reach for are already covered:

- "I'm building on this, but I'm not certain": `accepted` with `confidence: low`.
- "I looked at this and can't decide yet": leave it `suggested` and add the `reviewed:` date. A dated suggested claim reads "checked, still open". An undated one reads "not looked at yet".

Everything not yet proved climbs one ladder, written in one place, the question entry (`SPEC.md` §17):

1. **Question.** Something not known.
2. **Working answer.** The same entry, with a guess, the reason for it, and what would settle it. It gains an `h-` id so other records can point at it.
3. **Build on it.** A guessed relationship may go on the person's `relationships:` list marked `status: hypothesis`, so research can carry on past it. The tree and the site draw it as a guess. Exports leave it out.
4. **Settle.** A source says it (a normal claim). Or the pieces add up, and the reasoning is written as a proof-argument source that the claim cites. Or it is wrong: the guess is ruled out. Its lines leave the entry, and one dated `context:` line records `ruled out h-…` and why, so nobody makes the same guess again. The question stays open, and the next guess gets a new `h-` id. There is no "abandoned" status, because the status belongs to the question, and a wrong guess does not answer the question.

**The test between a weak source and a working answer.** If anyone or anything other than the researcher says it (a family story, an index entry, another researcher's tree), it is a source, with a low-confidence claim. If only the researcher says it, it is a working answer. A guess is never a claim, because a claim needs a source.

**Why.**

- **Review and evidence quality are different questions.** Folding them into one status list would lose one of them. A "possible" status would mix "has a person looked?" with "how good is the record?".
- **One ladder instead of five mechanisms.** Open questions, separate hypothesis records, a "needs review" status, accepted-but-low claims and guessed tree edges are the same idea. Spread across five places, a reader would have to learn five things. One entry that grows as the research does is one thing to learn.
- **An `h-` id never becomes a `c-` id.** IDs are typed for life. Settling a working answer creates a new claim from the source that proves it. Both records stay, and the claim may point back at the working answer.
- **Gaps a tool can compute are never written as questions.** "No birth date" or "no parents" is shown fresh by `fha report` every time. A written copy would only go stale. A question is written down only when a person asks it or two claims conflict.

**Rejected.** A separate `needs-review` status (a dated `suggested` claim says the same thing). A "possible" or "uncertain" status. Hypotheses as a separate kind of record apart from questions.

---

## 5. The derivation principle

**The rule.** Tools derive trees, timelines, completeness and exports from claims. They follow one rule: **honor what the record actually says, refuse to guess where it is silent, and make the refusal visible.**

Some examples from `SPEC.md` §8.3:

- A birth claim gives a parent edge only when its `roles:` map names a `child` and a `parent`. With no map, it gives no parent edge, even if the claim names three people.
- A marriage claim that names exactly two people and no other roles reads as that couple, because marriage is symmetric and two people admit only one answer. Parentage is directed, so there is no such fallback for births.
- A death ends only the dead person's own marriages, never the marriage of someone else the record names.
- A role word that is plainly a misspelt derivation role (`parnet:`) is reported, not quietly treated as a description.

Each refusal is reported. The linter lists every accepted claim that names several people and yields no answer, so the owner can add the missing role.

**Why.** A false edge is read back as fact by everything downstream: the relationship calculator, the tree views, the report and the GEDCOM export. A false parent drags a whole ancestral line with it. A missing edge is merely missing. **Silence is recoverable; a false fact is not.** So the tools derive nothing they cannot read off the record, and say so.

Two refinements keep this honest:

- **A warning must be clearable.** A claim whose roles place everyone has said all its record says, and is not reported. A warning that nothing could clear short of inventing a role would teach the owner to ignore the real ones.
- **One rule, one place.** Every tool that derives an edge imports the same shared rule. A rule implemented twice drifts, and two tools then disagree about the same family.

The same ordering guides which problems get fixed first: a confident wrong answer with no error ranks above a loud failure, because the archive's whole promise is to be the trustworthy record.

**Rejected.** Guessing parentage from the order of names. Pairing off everyone a marriage record names (that would make a man his father-in-law's husband). Silent fallbacks that produce an answer when the record gave none.

### Completeness metrics

**The rule.** `fha metrics` scores each person on five checks (birth record, censuses, parents, marriage records, death) and computes them fresh every run. It counts accepted claims by default and counts suggested claims too on request (`--include-suggested`). A death is asked of everyone marked `living: false`, and of anyone whose status is unknown only once they were born more than 130 years ago. Census years are asked in each country the person's own records place them, from birth (or their first record there) to death; with no death year the check is never met.

**Why.** Accepted-only is the standard view because it matches what the archive has settled, and SPEC's own vital completeness reads it that way. The include-suggested view exists because a score that only rises when claims are accepted rewards accepting, which is the habit the review contract exists to slow down. Both views come from one engine, so neither can drift from the other. Past 130 years there is no doubt a person has died, so a missing death is a real gap; short of that, a missing death may be a living person, and asking for it would fill the list with people nobody can do anything about. `living: false` is the owner saying the person has died, so their death is owed at any age, the same rule lint W101 applies; one archive must not get two answers to that question. The census rule is deliberately simple: a stay abroad shows as a gap the owner can see and judge, because a confident wrong "complete" is the worst thing this check can say, and an unknown death year means later censuses may still be out there. A place whose hierarchy stops at a U.S. state or Canadian province still names its country, read from a fixed list of full names; a name that is also a foreign country or province (Georgia, Florida, Maine, Maryland, Montana) or was its own country during the table's years (Hawaii, Newfoundland) is left off it, because each could put a census in the wrong country's table. Where records disagree, the safer reading wins: the latest of birth readings that roughly agree decides whether a death is owed (someone who may be 126 may be living), and the best-dated death decides which censuses they lived through (an exact date over a recollected one).

**Rejected.** A `living_confirmed:` date on the person record, so an owner could record when someone was last known alive. It would be a new field to maintain for a check that the 130-year rule answers without one. Inferring a person's country from free-text places: the census check reads only the place registry, and says "can't tell" rather than guess. Modelling migration (reading immigration claims as moves, so years abroad are not asked): every rule for arrivals, returns and deaths abroad opens a new way to hide a real gap behind a "complete", and none of them can be stated in a sentence.

---

## 6. Deferred until needed: events and organizations

**The rule.** Two things that many genealogy programs treat as records of their own are, here, not records.

- **Events.** A birth, a census, a wedding is a *claim type*. An event shared by several people (a household in a census, a marriage) is one claim that names them all, with a `roles:` map.
- **Organizations.** A regiment, a lodge, an employer, a tribe is a *value* in a claim. A person's membership is a `relationship` claim with `subtype: member-of` or `employer`. The optional `value_org` field (Appendix A.5) lets views group "everyone in Company K". There is no organization record and no `org-` id.

**Why.** Claims already cover these cases. A new kind of record is added only when it earns its place, not in advance of a need.

**The tripwire.** Each stays deferred until a real strain shows:

- An event becomes a record of its own only if multi-person claims prove awkward in practice: the same shared event copied, clumsily, across many claims. The strain is the signal; until it is felt, events stay claim types.
- An organization becomes a record only when cross-referencing organizations needs more structure than a shared name in `value_org` can give.

**Rejected (for now).** An event object. An organization object.

---

## 7. Ideas turned down: fixity checksums, privacy tiers, source-debt IDs

**Fixity checksums in the archive.** *The rule:* the archive does not store a checksum for every file, and the linter does not check for silent corruption. *Why:* detecting bit rot or silent file changes is a preservation concern, and it belongs to the backup strategy, outside the archive format. An optional checksum check in `fha doctor` could be added later without making checksums part of the research model. Checksums are used in one place: `fha update-tools` compares them to tell which tool files the owner has customized, so an update never overwrites the owner's changes.

**Privacy tiers.** *The rule:* two flags, no tier system. `living` (`true`, `false` or `unknown`) drives redaction, and `unknown` counts as living, so a newly created person is private until someone says otherwise. `restricted` marks a source, a claim, a person or a single name to keep out of what leaves the archive. Two restricted types can never be overridden by a flag: `dna` (only with its own `--include-dna`) and `by-request` (a person who asked to be left out). *Why:* a flag appears only where it applies, on the exact thing it protects. Uncertainty is safe by default. *Rejected:* a ladder of privacy levels, and choosable privacy modes on family packets. Public output follows one fixed rule set, and a family packet follows its own fixed rules.

**Source-debt IDs.** *The rule:* a useful sentence still waiting for its source is marked `(TODO: import source)`. The linter lists every marker. *Why:* a marked sentence is uncited, and uncited prose already reads as story or context, never fact. So the marker needs no ID and no special export rule. *Rejected:* giving each missing source its own ID to track.

---

## 8. The workbench: a local browser page

**The rule.** `fha serve` starts a small web server on the owner's own computer. It shows the archive as web pages, with a review queue and an inbox, and lets the owner make the same decisions the commands make: accept a claim, fix a date, mark someone living or dead, confirm a suggested connection.

Its rules:

- **Every button is a documented command.** The page calls the same engine the terminal command calls, and shows the command it ran. It holds no logic of its own.
- **Preview, then confirm.** Every change is shown before it is written.
- **It owns nothing.** Its only state is a cache folder. Stop it, delete it, and nothing is lost. The static site never depends on it.
- **Local only, started by hand.** It listens only on the computer it runs on, and runs only while the owner has it open. It never starts itself, never watches files, never sweeps.
- **It is the owner's private view.** It shows what his own terminal shows: the completeness ring on every person page with what would fill each gap, and this week's family events with living relatives named, as `fha report` does. The `site:` switches are for pages other people read, so they do not apply here. A living person still gets no ring, as on every page.

**Why.** The owner this is built for is comfortable clicking around a website and does not want a terminal. Adding a note or dropping a scan into the inbox already works with a file browser. What is hard is the structured decisions, above all accepting a claim, which by hand means editing a status inside a block of YAML, the edit most likely to break a file. Those decisions are already commands. What the owner needs is a gentler door to them.

A local page served by Python's standard library has almost no upkeep: no packaging, no code signing, no app store review, no framework to chase. If it is ever abandoned, it fails cleanly. The commands, the AI and the files all still work; only a web address stops answering.

**Rejected.**

- **A desktop app (Electron, Tauri).** It buys a dock icon at the price of per-system builds, code signing, an auto-updater and a framework that moves every few months. For one maintainer that is how a convenience becomes a liability, and an abandoned app leaves users with a broken program rather than a page that stopped loading. The local server is already the backend such an app would need, so one could be added later without rework.
- **An Obsidian plugin as the main front door.** Claims sit in fenced YAML that plugins do not parse, so a plugin would have to reimplement the careful edit logic in a second language (two copies that drift) or call Python from inside Obsidian (fragile, desktop-only, two runtimes). And the owner this serves does not use Obsidian. A plugin remains possible later for people who do.
- **Turning the capture browser add-on into an editor.** A browser add-on cannot read or write the archive's files without a privileged local pipe, it would need a second copy of the claim logic, and capture happens on other people's websites while editing happens at the desk. The add-on stays an intake tool.

---

## 9. Capture: one record, on the user's click

**The rule.** `fha capture`, and the browser add-on that feeds it, turn the record page the owner is looking at into a rough source waiting in the inbox. It reads only the open page, in the owner's own logged-in session. It does not log in, page through results, or download behind a login on its own initiative. Everything it produces is a suggestion until reviewed.

Two narrow exceptions fire only on the owner's own Capture click, one record at a time: on an Ancestry image page it makes the same single download request Ancestry's own Download button makes, and on an open-standard (IIIF) archive it fetches the one full-size image, with no login.

**Why.** Capture exists to file the record you are already looking at, not to harvest a site. Fetching the one image on screen, when you click, is no different from clicking the site's own button. Bulk-download tools for such sites break when the site's private interfaces change, and they run against its terms; the one-record rule avoids both.

**How the site readers work.**

- **The browser stays simple; the Python recipe decides.** The add-on fills in only obvious hints (title, address, date). The per-site reading of a census table or an index entry happens in Python, at ingest, from the saved page. Two copies of the same parsing, one in each language, would drift apart. Where one small rule must run in both places (the record type and date hints), both copies are held to one shared table of test cases.
- **Always keep the raw page.** The page's HTML is saved in every capture. A wrong guess in the browser then costs nothing: the recipe corrects it at ingest, and the owner's own typed corrections win over both.
- **Field labels are not people.** Sites lay out records as label and value. Without a guard, labels like "Birth Date" end up in the list of names. One shared guard keeps them out for every site.
- **Sites fall into tiers.** Pages that publish standard metadata need only a light recipe. The big record sites, which build their pages in script with no metadata, need table readers. Public archives using the IIIF image standard are the easiest and cleanest.
- **Never claim a better copy than you have.** A screenshot is labelled provisional. A citation with no file is labelled "held elsewhere". A saved page says it is a copy of the page, not the original record. A site's own index of a record is filed as site data, never as a transcript.
- **Searchable is not the same as transcribed.** `fha find --text` reads a saved page by what it shows and a site's data by its values, so a name on the page is found. Beside a record image, neither counts as the record's own words: lint keeps asking for a transcript of the image, because the site's index is the site's reading, and a misreading there would otherwise pass as evidence. Site data alone is the same. A source that is only a saved page has no picture behind it to read, and the search already reads the page, so it is not sent to the backlog. A family tree exported as a GEDCOM is the evidence itself, so it counts as its own words too.
- **Do not invent what the page does not say.** A memorial site gives a death date but no burial date; capture does not make one up. A volunteer's compilation is filed as a website, a derivative source, not as a vital record, so its claims do not default to high confidence.
- **Image tiles only when there is no other way.** Where a site hands over the assembled full image, stitching tiles is rejected: it is heavier and more plainly working around the site. It is used only where the site offers no full-size image, one tile at a time, slowing down when the site asks.
- **Nothing is thrown away.** Ingested bundles move to a holding folder; they are never deleted by the tool.

**Rejected.** Bulk or automated downloading. Logging in for the user. Per-site parsing in the browser. A capture that writes a finished record rather than a stub for review. Counting a saved page or a site's data as the transcript of a record image filed beside it. Asking for a transcript of a saved page that is the whole of a source.

---

## 10. Working in Obsidian

**The rule.** An archive is an Obsidian vault as it stands: Markdown files, YAML frontmatter, and `[[ ]]` links. Point Obsidian at the folder and it opens, with no import. Links are written `[[id|Name]]`: the double brackets mark a link in any editor, the ID inside keeps the link stable across renames, and the name after the bar is only for display. The tools resolve a link by ID, by file name (a record's file name ends in its ID, which is what the tools read, so a link left with an old file name still resolves), or by name through each record's `aliases:`. Obsidian resolves a link by file name only, so it shows an ID link as unresolved; the file name ends in the ID, so searching the ID finds the record. Each person's `aliases:` lists their ID, name and public other names (variants, nicknames, birth and married names), which Obsidian offers while you type `[[`; picking one writes a link to the file, which Obsidian follows.

**Obsidian link mode.** `links: obsidian` in `fha.yaml` makes the tools write a link's target as the record's file name, which ends in its ID, so Obsidian follows it and the tools still read the ID. It is opt-in because the ID form is the shorter, plainer text in any other editor, and an archive that never opens Obsidian gains nothing from long file names in its prose. A source link shows its ID (`[[letter_s-…|s-…]]`), so a citation still reads as one; a claim link names its source's file at `#Claims` with the claim ID as the display, because a claim has no file and all of a source's claims share one YAML fence, where a per-claim block link would mean one fence per claim. Links inside a fence stay in the ID form, since Obsidian follows none there. A place link names its place note, one generated file per place that `fha views places` writes from places.yaml in this mode, because Obsidian's graph and backlinks work per file: each place is one node, and its backlinks pane lists what mentions it, which is why the note carries no people or sources list of its own. The note is a view, never read back, so places.yaml stays the single record and a rename is followed by regenerating rather than by editing two files. A single generated `places.md` with one `^l-…` block per place was set aside: it would make every place one graph node and lose the per-place backlinks. A verb that renames a record file rewrites the links to that record in the same run, because in this mode a stale file name is a link Obsidian can no longer follow; it runs the rewrite normalize-links runs, limited to the renamed record, so the two never disagree. *Rejected:* naming files by bare ID (unreadable folders), and a community plugin that resolves aliases (load-bearing behaviour in a third-party plugin, against SPEC §2).

Claims sit in a fenced YAML block under `## Claims` in their source's record.

**Why the fence.** A claim belongs beside the evidence it rests on, in that source's own file. An unfenced YAML list renders as garbled Markdown, so the claims block is fenced to stay readable in any viewer.

**What the fence costs.** Obsidian's query plugins (Dataview, Bases) read frontmatter, not the inside of a fenced block, so they cannot query claims. Claims are queried through the `fha` tools and the index instead. The person record's frontmatter (vitals, the `relationships:` list, flags) is ordinary frontmatter and answers the common questions ("everyone with no death date"). In the tools a link to a single claim resolves to its source's record, not the claim itself.

Other limits:

- Two people with the same name both appear in Obsidian's `[[` list under that name, told apart only by their file names. `fha lint` reports the clash; a shared name is then pinned to its ID.
- With a couple of thousand people, the global graph is an unreadable tangle, as in any large vault. The useful view is the local graph around one person.
- The privacy flags protect what the tools export. Publishing or syncing the raw vault to a public place bypasses them.
- On a phone the archive is for reading and jotting. Processing happens at the desk.

**Rejected.**

- **Name-only links.** A link by name breaks when the name changes and is ambiguous when two people share it. The ID inside the brackets fixes both.
- **A generated "claim mirror" note** that copies claims into queryable frontmatter. It would duplicate every claim and add a file to keep in sync, and the person frontmatter already answers the common questions.
- **Making Obsidian, or any one editor, required.** It is one good way to read and write the files. The archive works the same in a plain text editor.

---

## 11. Geocoding: one table, one yes

**The rule.** `fha places geocode` fills in coordinates from GeoNames, the free world gazetteer, saved once into `.cache/geonames/` (only the countries the registry names). It proposes a match only when exactly one GeoNames entry fits every part of the place's hierarchy, shows every proposal in one table, and asks once. A yes writes them all in one rewrite of `places.yaml`. A match also fills in a missing county, state or country, but never changes words already in the hierarchy. A place that could be more than one entry is listed with the exact command that settles each candidate. A country, state or historic region gets no coordinates at all: it is specific enough as it is. A state that is also a city (Berlin, Hamburg, New York) is the city. Churches and cemeteries are matched too, but only when their town, county and state agree, and never in place of a town of the same name.

**Why.** Coordinates anchor a place's identity, so a human still says yes before any are written. One question per place made a registry of a few hundred places a few hundred prompts, so it was never run. A table read once and answered once is a real review, and the all-or-nothing write means a refused or failed run leaves the registry as it was. Matching on the whole hierarchy, not the name alone, is what makes the single yes safe: two towns called Fairview stay a question for the human, never a guess. Per-country dumps carry the villages, counties and states a genealogist names, where a list of large cities carries only cities; downloading only the named countries keeps the download small.

**Rejected.**

- **A live geocoding API.** It needs the network on every run, rate limits and accounts, and its answers change over time. A saved dump answers the same way every run, offline.
- **The worldwide `allCountries` dump.** Hundreds of megabytes to download, and over a gigabyte unpacked, for an archive that names a handful of countries.
- **Writing coordinates when `fha confirm place` mints a place.** Its yes is about the claims and the name, not coordinates the tool inferred. It names the geocode command to run next, so the coordinates get their own table and yes.
- **Guessing between candidates** (largest population, nearest to other places). A confident wrong pin is worse than an empty one the human can fill with one command. For the same reason a place that names no country is never a confident match, even when one entry fits.
- **A pin for every place.** A country or a region like Prussia has no one point that means it; a pin in its middle would look precise and say nothing.

---

## 12. Merging two sources

**The rule.** When two source records prove to be one piece of evidence, `fha source merge` folds one into the other. The kept record takes the other's files, claims (ids unchanged), notes and links, keeps its id in `aliases:`, and records what moved in `merged_from:`; the other record file is removed. The merge is refused when both records hold accepted claims and no person is named in both.

**Why.** An index entry and the document it indexes are one source, and so are two captures of one record, so a merge is a routine step. By hand it means clearing embedded keywords and staging files under neutral names, which loses the original filename. The `merged_from:` entry exists because a merge with no record of what moved cannot be undone. The people check uses the claims a human has already accepted: two records whose confirmed facts share no one are almost always two records, and a wrong merge is far harder to see than a refusal.

**Rejected.**

- **A tombstone record for the merged source**, as persons have. A merged source's files all move to the survivor, so nothing would be left for the tombstone to describe; the alias and `merged_from:` carry what it would.
- **Guessing "different people" from names or dates in the text.** The accepted claims already say who each record is about; anything looser would grow exceptions.
- **An `unmerge` verb now.** `merged_from:` makes an undo possible by hand; a verb waits until one is needed.

**Finding one record saved from two sites (W162).** The same record set often sits on Ancestry and FamilySearch under different collection numbers, and the batch downloads save a person's sources from both. `fha lint` reports two sources from different sites whose collections are one set in `tools/record_collections.json` (WikiTree Sourcer's map, MIT), that are the same person's entry (the subject each site's saved data names), that name a person in common and whose dates agree. It reports and never merges: which copy to keep is the owner's call. A source's collection comes from the structured data it carries (Ancestry's record address, FamilySearch's saved site data), never from its title.

- **Pairing two sources from one site.** The capture add-on keeps each household member's record of one census page as its own source, linked by `same_image_as`; those are not duplicates.
- **Pairing on a person in common alone.** A census page lists the whole household, so a husband's entry and his wife's name the same people; only the record's subject tells two entries apart.
- **Matching on titles or collection names.** Sites word them differently and change them; the collection number is what the record itself carries.
- **Writing our own collection map.** Sourcer's is maintained and covers the sites the archive uses; `build_record_collections.py` refreshes our copy from it.

---

## 13. Linking place text: the hierarchy rule

**The rule.** A claim's place text links to a registered place when it fits exactly one: it equals a name or alt name, or its first part names the place and every other part is in the place's hierarchy, "County" and the country optional after the first part. The same rule runs when a claim is written, over claims written earlier (`fha places link`, a dry run until `--apply`), and when a backlog of recurring place texts is registered at once (`fha places candidates --register`, one list and one yes). The place text itself is never changed.

**Why.** Records name a place at whatever depth the clerk wrote: "Chicago", "Chicago, Cook, Illinois", "Chicago, Cook Co., Illinois, USA". Requiring the exact registered wording leaves many mentions unlinked although the place is registered (a third of them in one large archive), and the place page then shows a fraction of the family's life there. Every part of the text still has to agree with the place's hierarchy, so a text that names a different county or state never links, and a text that two places could answer to goes to the human. Registering one cluster per command turns a backlog of hundreds of place names into hundreds of commands; one reviewed list makes it one decision. The `--register` list shows each new place's coordinates, so its yes covers them, which `fha confirm place` (whose yes is about claims and a name) cannot.

**Rejected.**

- **Linking on the first part alone.** "Springfield" names a dozen towns; the other parts are what tell them apart.
- **A "County" part matching any level of the hierarchy.** "Jackson, Jackson County" would then fit the Jackson in Hinds County by its own name, "Jackson, Mississippi County" Jackson by its state, and "Brooklyn, New York County" Brooklyn by its state. A County part names the county level: an explicitly written county, or, when no level is written as a county, a level above the place other than the hierarchy's last two. Those two may be a state and a country, and countries are spelled too many ways ("U.S.", "Kingdom of Prussia", "Sverige") for a list of names to tell them apart, so neither is ever taken for a county. The cost is that "Chicago, Cook County, Illinois" does not link to a Chicago registered as "Chicago, Cook, Illinois"; it goes to the human, which is the safe direction.
- **Merging spellings in the order the clusters come.** "Fairview, Kansas" joining "Fairview, Breton County, Kansas" only when the longer one happened to be bigger made two places or one by chance; the labels decide alone.
- **"County" optional in the first part too.** A county and its seat often share a name, so "Breton, Kansas" would fit both the town and Breton County and link neither, and "Breton County, Kansas" would link to the town.
- **Breaking a tie by population or by the family's other places.** A confident wrong link is worse than an unlinked text the report keeps showing.
- **Rewriting `place_text` to the registered name.** It records what the source says, which is evidence; the link is the interpretation.
- **Downloading GeoNames data during `--register`.** It stays a quick, offline review; `fha places geocode` is the command that fetches and asks.
