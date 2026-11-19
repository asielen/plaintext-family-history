# Using the archive in Obsidian

A Plaintext archive is a folder of Markdown files with YAML frontmatter and `[[wikilinks]]`, which is what Obsidian reads. Point Obsidian at your archive folder and it opens as a vault, with no conversion or import. Everything below is optional; the archive works the same whether or not you ever open Obsidian.

## What you can do in the vault without the command line

Day-to-day genealogy needs no `fha` typing:

- **Create** a person or source note. Copy one of the `_TEMPLATE.*` files your archive already has (`people/_TEMPLATE.person.md`, `sources/_TEMPLATE.source.md` and the others), or install the optional [Templater pack](https://github.com/asielen/plaintext-family-history/tree/master/obsidian-templater) from the project repo into your Obsidian setup for a one-click new note with the right frontmatter. Leave the ID blank; it's filled in later, and a record with no ID is a valid starting point.
- **Link** people, sources and places by name: type `[[` and pick. The link finds the right record even before any ID exists, and Obsidian draws the connection in its graph.
- **Cite** a fact by putting its source's name in double brackets in your writing.

When you want the tools to do something (add IDs, check the archive, build a website or packet), run the `fha` commands or ask the assistant. The usual way of working is to write in the vault and let the assistant run the tools when they're needed.

## Searching your archive

Each person's frontmatter is where Obsidian can search: birth and death dates, the `relationships:` block, `gender:` and `restricted:` are all ordinary frontmatter that the Dataview plugin and Obsidian's Bases read directly. "Everyone without a death date" or "everyone born in Breton County" are plain frontmatter searches.

The detailed facts (claims) are kept in fenced ` ```yaml ` blocks under `## Claims`. The fence keeps the note readable, since an unfenced YAML list displays as jumbled text, but it also means Dataview can't see inside it. Ask the assistant or use `fha` to search claims. There is no separate note that copies the claims out for Dataview: it would duplicate them and need keeping in step, and the person frontmatter already answers the common questions.

## The graph view

With a couple of thousand people, the *global* graph is too crowded to read, as it is in any large vault. Obsidian's **local graph** (one person and their immediate connections) is the useful view, along with the household clusters that form around couples and sources. The optional Templater pack includes a sample `graph.json` that dims `people/stubs/` and highlights fully written-up people so those clusters stand out; merge its `colorGroups` into your vault's `.obsidian/graph.json`.

## Phone and computer

The files can be read and edited on a phone, so you can capture and read anywhere. The `fha` tools (processing, indexing, exports) run on a computer. Capture and read on your phone; process at your desk.

## Syncing the vault shares everything

The privacy flags (`living`, `restricted`) control what the *export* tools publish: the family website and packets. They do **not** filter a synced or shared vault. If you sync or share the vault folder itself (a synced drive, a shared folder), you share everything in it, living people and restricted material included. Share the generated website or a packet instead.

Two smaller notes. `fha lint` catches two people with the same name, which Obsidian won't warn you about, so run it now and then. And a `[[c-…]]` claim link opens the source file that holds the claim, since claims aren't notes of their own.

## Other Markdown apps

The vault opens in other Markdown apps too, since it's plain Markdown. It is set up with Obsidian in mind and isn't arranged for outliner-style apps.
