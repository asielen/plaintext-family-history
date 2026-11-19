# FAQ

### Why files instead of a database or an app?

Apps and databases don't last. Formats change, companies fold, subscriptions lapse. Plain files on disk, in text and standard image formats, can be read in fifty years with a text editor and an image viewer. There is a database here (a SQLite index), but the tools rebuild it from the files whenever they need to, so losing it costs nothing.

### Then why involve AI at all?

AI is good at the labor of genealogy: reading a census page, pulling the facts out of an interview, spotting connections you missed. It is a poor place to keep the truth. So the assistant drafts, a person reviews, and nothing it produces becomes a fact until a person accepts it. The records stay in your files.

### Is this finished software I can install?

There is no app installer. You set up an archive with one command (`fha install`) from a terminal, and from then on the AI assistant runs the tools for you. The tools cover filing, checking, searching, reporting, photo indexing and exports, and the assistant has workflows for processing an inbox, reviewing claims, writing biographies and more. The part meant to last is the file format; the tools can be replaced around it.

### How is this different from Gramps, webtrees or Ancestry?

Those are genealogy applications. They're good ones, but your data lives in their database or on their service. Here the archive is plain files you own. A genealogy program can still be fed from it with a GEDCOM export, so if you like another program's tree view, export and use it; your files stay the record.

It works the other way too. Coming from Ancestry or any other program, download your tree as a GEDCOM file and `fha gedcom import` files it: every person becomes a record and every statement in your tree becomes a suggestion for you to review, so years of work arrive ready to check.

### What happens to my photos?

They're never renamed or moved while they're in your photo library, so your photo program's catalog stays intact. The one exception is `fha process refile`, for a photo that turned out to be a record; it asks you first and reminds you to remove the photo from your catalog. Photos can live anywhere (an external drive, your existing library) and `fha.yaml` says where. A photo's ID is stored inside the file as a keyword, not in its name, so reorganizing them breaks nothing.

If you point `photos:` (or `documents:` or `inbox:`) outside the archive, the empty `photos/` folder inside the archive is removed. Point it back inside later and the folder returns.

### Does every photo or document need to become a source?

No. There are three good places for a file to stop, and most files stay at the first:

1. **Just filed.** A photo sits in the photo library. It can still be tagged with who's in it, captioned and found through the photo index, without any source record. A scan sits in the documents folder, findable by its name. This is a finished state, not a backlog.
2. **Filed with a note.** Something kept for interest rather than evidence (a keepsake, a photo of the farm) can get a short source record: a title, links to who and where it's about, and a paragraph saying what it is and why it was kept. **A source with notes and no claims is complete.** It then shows up in that person's source list, on the family website, in family packets and in full-text search. Put any keywords in the notes paragraph; the notes are searched. This is also how you keep something you *checked and rejected*, such as a debunked surname pamphlet or a disproved legend. The notes record the verdict, so neither you nor the assistant mistakes it for evidence later.
3. **Fully worked.** Evidence that states facts you can check gets claims drafted, reviewed and cited.

A clipping that links to nobody in your archive is a different case. It may name people in its text (a mayor, a neighbor mentioned in passing), as long as none of them is a relative you're tracking. File it as `source_type: ephemera` with `people:` left empty and no claims. Nothing links it to a person, so it won't show up on anyone's source list, website page or packet. It is still kept and findable: it gets its own page, appears in the website homepage's Sources list (grouped by decade, with an "Undated" group), and its notes are searched like any other. If a clipping turns out to name someone in your archive, even in passing, it isn't ephemera: change its type, link that person, and it becomes case 2.

Move a file up a level only when you have a reason to. The tools don't remind you about the ones that stay where they are.

### Is this thing a photo or a document?

Decide by where it should live, not by what it looks like. If it belongs in your photo library, it's a photo. Everything else (scans, clippings, even a photograph of a document) goes in the documents folder. A scanned record saved as a `.jpg` is filed correctly when its note says what it is (`source_type: census`). An in-between item like a postcard can keep its picture side in the photo library and its transcription in the documents folder, both under one source record. If something was filed on the wrong side, `fha process refile` moves it over and updates the record in one step.

### My family isn't Anglo: mononyms, two surnames, surname-first names, an ancestor with only a given name. Does this handle that?

Yes. A surname only decides where a person sorts in their folder, so families land together. Someone with no surname (a mononym, an enslaved ancestor recorded by a single given name, an Icelandic patronymic, a foundling) sorts in a "no surname" group, and their full name, in its own cultural order, is written in the record. For two surnames or surname-first names you choose the name to sort by, and the page shows the name the way it should be written.

### Can I keep a translation of a foreign-language record?

Yes. Drop the original and its translation in together, and the translation is filed beside the original as its own file, with the language of each noted. The original is never changed; the translation sits next to it the way a typed transcription does.

### How does it keep proven facts apart from guesses?

Every factual statement cites a source, or is plainly marked as story, context or speculation. Each claim has a confidence level and a review status. Guesses are kept as *working answers* on an open question until a source proves them and a claim takes over. `fha lint` flags anything that drifts from those rules.

### How do I record an adoption, a step-parent or a surrogate without it looking like a mistake?

List the relationship with its *nature* attached. A child can have a biological father and an adoptive father at the same time: write both, mark one `biological` and one `adoptive`, and the archive treats both as true instead of flagging a conflict. The same goes for step-parents, foster and guardian arrangements, surrogacy and donor conception, and ties that aren't family at all, like an employer or a fellow member of a military unit or a lodge. If your situation doesn't match a word on the list, describe it in plain language; the assistant suggests the closest standard term but keeps what you wrote.

### If I record an adopted ancestor, does that upset the family-tree numbering?

No. The numbered tree follows only the genetic line. An adoptive parent, a step-parent or a guardian is shown on the person's page and in the family folder but isn't counted into the numbering. You see who raised them and who they descend from, and the numbers stay correct. Surrogacy and donor conception work the same way: the genetic parent takes the number, and the gestational or intended parent is shown beside them.

### Whose name goes in the "root person" setting: mine, or my children's?

**If the archive is about you,** set `root_person` to yourself and add `root_generation: children` to `fha.yaml`. Your children together become number 1 (they share one number and have no folder of their own), you are number 2, your father number 4, his father number 8, and so on back through the generations. This works even if you have no children in the archive.

**If you'd rather centre it on a particular child,** name that child (or any of their full siblings, since they share the same ancestors) as `root_person` and leave `root_generation` at its default, `self`. That person becomes number 1, their father number 2, his father number 4.

The one combination to avoid is naming yourself as `root_person` and leaving `root_generation` at `self`. That starts the count one generation too high, and every ancestor folder number comes out one generation off. The folders don't look wrong, because they were all numbered the same way, so the tools check the setting: if `root_person` has a child on record and `root_generation` isn't `children`, `fha lint` says so and names the fix. It's a note, not an error, since you might mean it.

If you change either setting later, run `fha index` and then ask the assistant to realign the folders (`fha views brackets --realign`). It renumbers the folders you've already filed in one step, and shows you the changes first.

### Can I record that someone was in a regiment, a tribe or the Masons, or worked for a railroad?

Yes. These are all *memberships*, recorded the same way as a family relationship: the person belongs to the group, and the group's name is written in plain words. That covers military units, tribal enrollment, lodges and clubs, employers, churches and events. The organization itself is just a name (there are no separate organization records), but the membership is a searchable connection you can cite to a source: a unit roster, a Dawes Roll, a directory.

### How do I find out how two people in my tree are related?

Ask the assistant. It works it out from your records: the blood relationship (like "first cousin twice removed") by following the family line, and the plain-language path between any two people even when they aren't blood relatives. It only uses connections you've recorded, so the answer is only as complete as your tree.

### Do I have to make IDs, or know everything before I write it down?

No to both. Name a file something sensible and link to records by name (`[[Grandpa Joe]]`, `[[Hartley family bible]]`, or a nickname). The IDs are the tools' job: `fha lint` adds them, keeps your filename as an alias so your name links keep working, and tidies the formatting. You can also write down what you only half know. A birth year you're fairly sure of but can't prove yet goes in as a *provisional* date, and the assistant keeps it on a "still to source" list until the record turns up.

### Do I have to use Claude Code?

No. The assistant's instructions are in `AGENTS.md`, a plain document any capable AI assistant can read; `CLAUDE.md` points Claude Code to it. The project is tested with Claude Code but doesn't depend on it. Any assistant that reads `AGENTS.md` gets the same behavior, including the workflow skills in `.claude/skills/`, which are plain Markdown any assistant can follow. OpenAI Codex reads `AGENTS.md` with no setup and finds the same skills in `.agents/skills/` (an installed archive carries both copies). Run Codex in its **workspace-write** approval mode so it can edit records and run the `fha` tools inside the archive but asks before touching anything outside it.

### Can I use this with Obsidian or another Markdown app?

Yes. A Plaintext archive is already an Obsidian vault: Markdown files with YAML frontmatter and `[[wikilinks]]`. Point Obsidian at the archive folder and it opens with no conversion. See [USING_WITH_OBSIDIAN.md](USING_WITH_OBSIDIAN.md) for what works in the vault and what to run the tools for.

### Can I keep my archive in Dropbox, iCloud Drive or OneDrive, or edit it from two places at once?

You can store it there, but treat it like a shared spreadsheet: one editor at a time. The tools don't lock files, so they assume a single writer: you, or an assistant working for you, but never both at the same moment and never two assistants at once. A lock would need a program running in the background all the time, and the project avoids that. With one person at one keyboard, which is how almost everyone uses it, this never comes up.

It matters once a folder that syncs continuously, or a second editor working at the same time, is involved. A sync service can rewrite a file while a tool is saving it, and an assistant working the archive while you hand-edit the same files can each save over the other's change, with nothing to warn either of you. Your records aren't damaged (they're still plain text you can open and fix), but a change can disappear. Finish one session, yours or the assistant's, before starting the next. If you need the archive on a second computer, use **`fha working-copy`**, which carries the text records there through git; git tells you when two edits conflict instead of quietly keeping one.

### Can I share this with family without exposing living people or private data?

Yes; the export tools handle it. The family website (`fha site`) leaves out living and possibly-living people and all sensitive material, including all DNA, by default. Anything you mark *restricted* (a person, a single sensitive fact, a source or a former name) is kept out of anything public the same way, and out of family packets unless you choose to include it. A relative who asks to be left out entirely (`restricted: by-request`) is left out everywhere, with no override.

When someone's living status changes (a relative dies, or a record turns out to describe someone long gone), one command updates it: `fha person set-living <p-id> false`. The assistant can run it for you and tells you what the change means for your exports. When you accept a death or burial record for someone whose status was "unknown", the status changes for you and you're told; someone you marked as living is never changed without asking you.

### Does this manage my DNA matches and triangulation?

No. The archive stores DNA *conclusions* ("these two people are related, and here's the proof") as ordinary claims and proof-argument sources, always kept private. Manage raw matches, shared centimorgans and triangulation in the tools built for that (Ancestry, GEDmatch, DNA Painter), then bring the conclusion back here as a claim.
