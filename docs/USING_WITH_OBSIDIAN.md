# Using the archive in Obsidian

A Plaintext archive is a folder of Markdown files with YAML frontmatter and `[[wikilinks]]`, which is what Obsidian reads. Point Obsidian at your archive folder and it opens as a vault, with no conversion or import. Everything below is optional; the archive works the same whether or not you ever open Obsidian.

## What you can do in the vault without the command line

Day-to-day genealogy needs no `fha` typing:

- **Create** a person or source note. Copy one of the `_TEMPLATE.*` files your archive already has (`people/_TEMPLATE.person.md`, `sources/_TEMPLATE.source.md` and the others), or install the optional [Templater pack](https://github.com/asielen/plaintext-family-history/tree/master/obsidian-templater) from the project repo into your Obsidian setup for a one-click new note with the right frontmatter. Leave the ID blank; the assistant (or `fha lint --fix-ids`) fills it in later, and a record with no ID is a valid starting point.
- **Link** people and sources by name: type `[[`, start the name, and pick the record from the list. Obsidian writes a link to that file, follows it, and draws it in its graph. A person turns up under their name and name variants because the tools list those in the person's `aliases:`, which is what Obsidian searches as you type. (If your records were written before the tools did this, `fha lint --fix-aliases` adds the names once.)
- **Cite** a fact by putting its source's name in double brackets in your writing.

When you want the tools to do something (add IDs, check the archive, build a website or packet), run the `fha` commands or ask the assistant. The usual way of working is to write in the vault and let the assistant run the tools when they're needed.

## Links the tools write

The tools write links with the record's ID inside: `[[p-de957bcda1|Thomas Edward Hartley]]` for a person, `[[s-b237895f31]]` for a citation, `[[c-…]]` for a single claim. The ID is what keeps the link right when a file is renamed, and every `fha` tool, the family website and the exports follow it.

Obsidian follows a link by file name only, so it shows an ID link as unresolved and leaves it out of the graph. Clicking one makes a new, empty note named after the ID; delete that note. To get to the record, search the ID instead: every record's file name ends in its ID, so Obsidian's quick switcher (Ctrl+O, or Cmd+O on a Mac) finds it at once. A claim is not a note of its own and its ID is in no file name, so put a `c-` ID into Obsidian's search (Ctrl+Shift+F, or Cmd+Shift+F on a Mac) to find the source file that holds the claim.

A link you type by name without picking from the list, like `[[Grandpa Joe]]`, is unresolved in Obsidian too unless a file has that name. The `fha` tools still read it as a link to the person.

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
