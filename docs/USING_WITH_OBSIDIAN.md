# Using the archive in Obsidian

A Plaintext archive is a folder of Markdown files with YAML frontmatter and `[[wikilinks]]`, which is what Obsidian reads. Point Obsidian at your archive folder and it opens as a vault, with no conversion or import. Everything below is optional; the archive works the same whether or not you ever open Obsidian.

## What you can do in the vault without the command line

Day-to-day genealogy needs no `fha` typing:

- **Create** a person or source note. Copy one of the `_TEMPLATE.*` files your archive already has (`people/_TEMPLATE.person.md`, `sources/_TEMPLATE.source.md` and the others), or install the optional [Templater pack](https://github.com/asielen/plaintext-family-history/tree/master/obsidian-templater) from the project repo into your Obsidian setup for a one-click new note with the right frontmatter. Leave the ID blank; the assistant (or `fha lint --fix-ids`) fills it in later, and a record with no ID is a valid starting point.
- **Link** people and sources by name: type `[[`, start the name, and pick the record from the list. Obsidian writes a link to that file, follows it, and draws it in its graph. Every `fha` tool, the family website and the exports follow it too: the file name ends in the record's ID, and the tools read the link by that ID, so it keeps working even after the file is renamed. A person turns up under their name and name variants because the tools list those in the person's `aliases:`, which is what Obsidian searches as you type. (If your records were written before the tools did this, `fha lint --fix-aliases` adds the names once.)
- **Cite** a fact by putting its source's name in double brackets in your writing.

When you want the tools to do something (add IDs, check the archive, build a website or packet), run the `fha` commands or ask the assistant. The usual way of working is to write in the vault and let the assistant run the tools when they're needed.

## Links the tools write

Out of the box the tools write links with the record's ID inside: `[[p-de957bcda1|Thomas Edward Hartley]]` for a person, `[[s-b237895f31]]` for a citation, `[[c-…]]` for a single claim. The ID is what keeps the link right when a file is renamed, and every `fha` tool, the family website and the exports follow it.

Obsidian follows a link by file name only, so it shows an ID link as unresolved and leaves it out of the graph. Clicking one makes a new, empty note named after the ID; delete that note. To get to the record, search the ID instead: every record's file name ends in its ID, so Obsidian's quick switcher (Ctrl+O, or Cmd+O on a Mac) finds it at once. A claim is not a note of its own and its ID is in no file name, so put a `c-` ID into Obsidian's search (Ctrl+Shift+F, or Cmd+Shift+F on a Mac) to find the source file that holds the claim. If you work in Obsidian every day, turn on Obsidian link mode (below) and the links connect.

A link you type by name without picking from the list, like `[[Grandpa Joe]]`, is unresolved in Obsidian too unless a file has that name. The `fha` tools still read it as a link to the person.

## Turn on Obsidian link mode

Obsidian link mode makes the tools write every link by file name, so Obsidian follows it, draws it in the graph and lists it under backlinks:

- a person: `[[hartley__thomas_edward_p-de957bcda1|Thomas Edward Hartley]]`
- a source: `[[1880-census-fairview-hartley-household_s-4f5f215e60|s-4f5f215e60]]` (the ID stays the words you see, so a citation still reads as one)
- a claim: `[[1880-census-fairview-hartley-household_s-4f5f215e60#Claims|c-bdfbce2a11]]`, which opens the source at its Claims section
- a place: `[[fairview_l-7c1a9f4e22|Fairview]]`, which opens the place's note (see "Place notes" below)

The file name ends in the record's ID, so every `fha` tool, the family website and the exports read these links exactly as they read the ID form, and what the exports leave out for privacy is the same in both modes.

To turn it on:

1. Open `fha.yaml` and add the line `links: obsidian`.
2. Run `fha index`, then `fha views places`, so every place gets its note.
3. Preview the change to the links already in your records: `fha normalize-links --obsidian`. It lists every file it would change and what each link becomes. Words you wrote after the `|` stay as they are.
4. Write it: `fha normalize-links --obsidian --write`.
5. Run `fha index`, then `fha views refresh`, so the generated pages and the `## Sources` lists use the new links too.

From then on, every tool that writes a link writes it by file name. `fha lint` reports any link Obsidian cannot follow (warning W161): an ID link typed by hand, or a file name a rename has since changed. The fix it names is the same command, `fha normalize-links --obsidian --write`.

A few links stay as they are in this mode:

- **A place with no note yet** keeps the ID form, `[[l-…|Fairview]]`, until `fha views places` writes its note.
- **Claims blocks and other code** keep the ID form. Obsidian does not follow a link inside a fenced or indented code block, so there is nothing to gain there.
- **In a table**, a link is written the way Obsidian writes one there, `[[file name\|Name]]`, so the `|` does not end the cell.
- **A claim link with its own words**, like `[[c-…|the census line]]`: Obsidian's form needs the claim ID as the words, so the tools leave it and lint names it. Rewrite it as `[[source file name#Claims|c-…]]` and put the words beside it.

When a tool renames a record, it also updates the links to it across the archive, so Obsidian keeps following them. That covers `fha person rename` (a name corrected), `fha confirm merge` (two people made one: links to the merged person now go to the one kept), `fha source merge` (two source records made one: links to the removed record and to its claims now go to the one kept) and `fha lint --fix-ids` (a record you named by hand given its ID: a link like `[[grandmas-album]]` becomes `[[grandmas-album_s-…|grandmas-album]]`). Each one says how many links in how many files it updated, and `--dry-run` shows the same count before anything is written. Moving a record to another folder keeps its file name, so no link changes. A file renamed by hand outside the tools does not update its links: lint W161 names them and `fha normalize-links --obsidian --write` brings them up to date. The generated pages catch up when you run `fha views refresh`. Links written with an old file name still work in every `fha` tool meanwhile, because only the ID at the end is read. To go back to ID links, remove the `links:` line: the tools write ID links again, and the file-name links already written keep working. `fha views clean` removes the place notes.

## Place notes

Every place lives in one file, `places/places.yaml`, so in link mode the tools give each place a note of its own for Obsidian to open: `places/fairview_l-7c1a9f4e22.md`. `fha views places` writes them, and `fha views refresh` keeps them current. A note shows what places.yaml says about the place: its full name and the county and country it sits in, the place it is inside (a house in a town, say), its map coordinates, its other names, its history and your notes on it. Its other names are in its `aliases:`, so typing any of them after `[[` finds it. It does not list the people and sources that mention the place: Obsidian's backlinks pane shows those.

The notes are generated: never edit one, because the next refresh writes over it. To change what a note says, edit the place in `places/places.yaml` and run `fha index`, then `fha views refresh`. If you rename a place there, run `fha index`, then `fha views refresh`: the note gets the new name, the old note is removed, and links to the old name are pointed at the new note. Pointing those links changes your records, so the refresh then asks you to run `fha index` once more. A file you made yourself in `places/` is never changed or removed, and links never point at it, even if its name has the place's code in it.

The notes stay at home: the family website builds its place pages from places.yaml, and no packet or export copies `places/`.

## Searching your archive

Each person's frontmatter is where Obsidian can search: birth and death dates, the `relationships:` block, `gender:` and `restricted:` are all ordinary frontmatter that the Dataview plugin and Obsidian's Bases read directly. "Everyone without a death date" or "everyone born in Breton County" are plain frontmatter searches.

The detailed facts (claims) are kept in fenced ` ```yaml ` blocks under `## Claims`. The fence keeps the note readable, since an unfenced YAML list displays as jumbled text, but it also means Dataview can't see inside it. Ask the assistant or use `fha` to search claims. There is no separate note that copies the claims out for Dataview: it would duplicate them and need keeping in step, and the person frontmatter already answers the common questions.

## The graph view

With a couple of thousand people, the *global* graph is too crowded to read, as it is in any large vault. Obsidian's **local graph** (one person and their immediate connections) is the useful view, along with the household clusters that form around couples and sources. The graph draws only the links Obsidian can follow, the ones made by picking a record from the `[[` list. The optional Templater pack includes a sample `graph.json` that dims `people/stubs/` and highlights fully written-up people so those clusters stand out; merge its `colorGroups` into your vault's `.obsidian/graph.json`.

## Phone and computer

The files can be read and edited on a phone, so you can capture and read anywhere. The `fha` tools (processing, indexing, exports) run on a computer. Capture and read on your phone; process at your desk.

## Syncing the vault shares everything

The privacy flags (`living`, `restricted`) control what the *export* tools publish: the family website and packets. They do **not** filter a synced or shared vault. If you sync or share the vault folder itself (a synced drive, a shared folder), you share everything in it, living people and restricted material included. Share the generated website or a packet instead.

One smaller note. `fha lint` catches two people with the same name, which Obsidian won't warn you about, so run it now and then. Both show up in the `[[` list under that name, told apart only by their file names.

## Other Markdown apps

The vault opens in other Markdown apps too, since it's plain Markdown. It is set up with Obsidian in mind and isn't arranged for outliner-style apps.
