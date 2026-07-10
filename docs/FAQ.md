# FAQ

### Why files instead of a database or an app?
Because apps and databases don't last.
Formats change, companies fold, subscriptions lapse, schemas migrate.
Plain files on disk - text and standard image formats - are readable in fifty years with nothing but a text editor and an image viewer.
The database (a SQLite index) still exists here; it's just a *disposable cache* rebuilt from the files, never the truth.

### Then why involve AI at all, if durability is the point?
Because AI is enormously useful for the *labor* of genealogy - reading a census page, extracting claims from an interview, surfacing connections you missed - and useless as a *source of truth*.
So the spec uses it exactly there: AI drafts, a human reviews, and nothing AI produces becomes a fact without a person accepting it.
The AI is an interface and a research assistant, never the store of record.

### Is this finished software I can install?
Not as a polished installer yet.
The core `fha` tools are present for filing, linting, indexing, finding, reporting, photo indexing, and export, but this is still a working plain-file project rather than a packaged app.
Some assistant workflows are still future skill-layer work; the durable part is the *format* and the *process*, and the tools stay replaceable around those files.

### How is this different from Gramps, webtrees, Ancestry, etc.?
Those are genealogy *applications* - excellent, but database- or service-first: your data lives in their store or schema.
Plaintext inverts that.
The archive is plain files you own; a genealogy app can be *fed* from it (via GEDCOM export) but never owns it.
If you love an app's tree view, generate a GEDCOM and use it - the truth stays in your files.

### What happens to my photos?
They're never renamed or moved by the system (so your catalog stays intact).
They can live anywhere - an external drive, your existing library - and `fha.yaml` maps to them.
Identity rides in embedded metadata (a keyword), not the filename, so reorganizing them never breaks anything.

### My family isn't Anglo - mononyms, two surnames, surname-first, an ancestor with only a given name. Does this handle that?
Yes. The only thing a surname does here is decide where a person sorts in their folder, so families land together. If someone has no surname - a mononym, an enslaved ancestor recorded by a single given name, an Icelandic patronymic, a foundling - they simply sort in a "no surname" group, and their full name, in its true cultural order, lives in the record itself. Two-surname and surname-first systems work the same way: you pick the name to sort by, and the page shows the name however it should really be written.

### Can I keep a translation of a foreign-language record?
Yes. Drop the original and its translation together, and the translation is filed beside the original as its own helper file, with the language of each noted. The original is never altered - the translation sits next to it, the way a typed-up transcription does.

### How does it stay honest about what's proven vs. guessed?
Three ways.
Every factual statement cites a source, or it's explicitly marked as story/context/speculation.
Claims carry a confidence level and a review status.
And guesses live as *hypotheses* - a separate, clearly-labeled state - until evidence promotes them to sourced claims.
The linter flags anything that drifts.

### How do I record an adoption, a step-parent, or a surrogate without it looking like a mistake?
You list it as a relationship with its *nature* attached. A child can have a biological father and an adoptive father at the same time - you write both, mark one `biological` and one `adoptive`, and the archive treats both as true rather than flagging them as a conflict. The same goes for step-parents, foster and guardian arrangements, surrogacy and donor conception, and even ties that aren't family at all, like an employer or a fellow member of a military unit or a lodge. If your situation doesn't match a word on the list, just describe it in plain language - the assistant suggests the closest standard term but never refuses what you wrote.

### If I record an adopted ancestor, do they mess up the family-tree numbering?
No. The numbered pedigree follows only the *genetic* line, so an adoptive parent, a step-parent, or a guardian is shown on the person's page and in the family folder but isn't counted into the blood numbering. You see the whole truth - who raised them and who they descend from - and the tree math stays correct. The same is true for surrogacy and donor conception: the genetic contributor anchors the number, and the gestational or intended parent is shown right beside it.

### Can I record that someone was in a regiment, a tribe, the Masons, or worked for a railroad?
Yes - these are all *memberships*, recorded the same way as a family relationship: the person belongs to the group, and the group's name is written in plain words. That covers military units, tribal enrollment, lodges and clubs, employers, churches, and event participation. The organization itself stays a simple name (Plaintext doesn't keep separate "organization" records), but the membership is a real, searchable connection you can cite to a source - a unit roster, a Dawes Roll, a directory.

### How do I find out how two people in my tree are related?
Ask the assistant, and it works it out from your records: the blood relationship (like "first cousin twice removed") by following the family line, and the plain-language path between any two people even when they're not blood relatives. It only uses connections you've actually recorded, so the answer is as good as your tree.

### Do I have to make IDs, or know everything before I write it down?
No to both. You name a file something sensible and link to records by name - `[[Grandpa Joe]]`, `[[Hartley family bible]]`, a nickname works too. The machine IDs are the tools' job: run `fha lint` and it assigns them, keeps your filename as an alias so your name-links keep working, and tidies up. And you can record what you only half-know - a birth year you're fairly sure of but can't yet prove goes down as a *provisional* date, and the assistant just keeps it on a "still to source" list until the record turns up. Recording what you know before you can prove it is the normal starting point.

### Do I have to use Claude Code?
No. The agent instructions live in `AGENTS.md`, a plain document any capable agent can read; `CLAUDE.md` is a one-line pointer to it.
Claude Code is the reference harness, but the design deliberately avoids locking to it.
Any agent that reads `AGENTS.md` gets the same behavior - including the workflow playbooks in `.claude/skills/`, which are plain markdown any agent can follow (AGENTS.md's "Playbooks" section tells it to). For OpenAI Codex specifically: it reads `AGENTS.md` natively with no setup; run it with the **workspace-write** approval mode so it can edit records and run the `fha` tools inside the archive but asks before touching anything outside it.

### Can I use this with Obsidian (or another Markdown app)?
Yes - a Plaintext archive *is* an Obsidian vault: Markdown files with YAML frontmatter and `[[wikilinks]]`. Point Obsidian at the archive folder and it opens, no conversion. Your `[[person]]` and `[[place]]` links draw the graph; vitals, relationships, gender, and privacy flags live in person frontmatter you can query with Dataview or Bases. The per-claim detail stays in fenced ` ```yaml ` blocks so notes render cleanly - those you query through the assistant or `fha`, not Dataview, so a first Dataview attempt over claims is expected to come back empty. An optional Templater pack (`obsidian-templater/`) gives one-click new-person / new-source notes, and the generated site (`fha site`) renders a navigable family tree. You never have to type `fha` for day-to-day writing - create, link, and cite in the vault, and run the tools (or ask the assistant) when you want IDs minted, checks run, or an export. See [USING_WITH_OBSIDIAN.md](USING_WITH_OBSIDIAN.md).

### Can I share this with family without exposing living people or private data?
Yes - that's built into the export tools.
The static-site generator produces a self-contained snapshot containing only publication-eligible material: living (and possibly-living) people are redacted, sensitive material (including all DNA) is excluded by default.
Anything you mark *restricted* - a person, a single sensitive fact, a source, or a former name - is kept out of public output the same way, and out of family packets unless you choose to include it; a relative who asks to be left out entirely (`by-request`) is honored everywhere with no override.
Changing someone's living status later - a relative passes away, or a record turns out to describe someone long gone - is one command: `fha person set-living <P-id> false` (the assistant can run it for you, and it always tells you what the change means for your exports).

### Does this manage my DNA matches and triangulation?
No, and on purpose. Plaintext stores DNA *conclusions* - "these two people are related, and here's the proof" - as ordinary claims and proof-argument sources, always kept private. It is deliberately not a match workbench: manage your raw matches, shared-cM, and triangulation in the tools built for that (Ancestry, GEDmatch, DNA Painter), then bring the conclusion back here as a claim. The durable archive holds what you concluded, not the scratch work that got you there.
