# Start Your Family Archive - by Hand, No Tools

Welcome! This is a blank starter kit for a family-history archive that is **just
plain files** - nothing to install, nothing that can stop working. You fill it in
with any text editor and it's yours forever.

> **Tip:** use a text editor that understands **wikilink-style `[[links]]`** - I
> recommend the free app **Obsidian**. When you type `[[` it lets you link one
> person or source to another, and it can draw your family as a graph. (Plain
> Notepad or TextEdit works too; you just type the links by hand.)

## What's in here

```
people/    one folder per ancestral couple, already numbered for you (002-014)
sources/   one file per piece of evidence (a certificate, a census, a photo)
places/    an optional list of the towns your family lived in
notes/     questions and research-in-progress
inbox/     a place to drop new scans before you file them
documents/ your scans and certificates       (delete if you keep them elsewhere)
photos/    your photographs                   (delete if you keep them elsewhere)
```

The deeper rules live in **`SPEC.md`** if you ever want them - but you do **not**
need to read it to start.

## Quick start

**1. Decide where your photos and documents go.**
If you already keep them in a folder outside this archive, delete the `documents/`,
`photos/`, and `inbox/` folders here and point `fha.yaml` at your real locations.
If you'd rather keep everything together in this archive, do nothing :)

**2. Make yourself the first person.**
Open the `people/` folder and find **`002 - Your Name Here - RENAME THIS FOLDER`**.
Rename it by swapping in your actual name. Inside are two files - one for you, one
for your partner (skip the partner if it doesn't apply).
- Open your file, fill in the front matter at the top (name, birth year, and so on),
  and jot a few facts in the Notes section. Approximate is fine.

**3. Add a baseline of relatives.**
Do the same for the folders one generation up:
- **004 - Your Parents**
- **008 - Your Father's Parents** and **010 - Your Mother's Parents**

If you have a partner, also fill in **006** (their parents) and **012** / **014**
(their grandparents). You don't have to do all of them - even a name and a rough
birth year in each is a great start.

> **About the numbers:** they're just labels that keep ancestors in a tidy order
> (it's a genealogy convention called Ahnentafel). You never have to calculate
> them - they're already on the folders. Rename the *words* after the number; you
> can leave the number alone.

**4. Add your first sources.**
Once you have some people, start backing up facts with evidence. Begin with core
documents like **birth certificates** or **census records**.
1. Put the scan or photo in the `documents/` folder (or `photos/` for photos).
2. Open the `sources/` folder and **copy** the `_TEMPLATE` file.
3. Rename the copy to match the document (same name, but ending in `.md`).
4. Open it and fill in what the source is, plus a fact or two it proves (its "claims").
5. In an editor with wikilinks, connect the source to the people it mentions by
   typing their names in `[[double brackets]]`.

That's the whole loop: **people → sources → links.** Repeat as you go, and your
archive grows itself.

## A few gentle rules

- **You can't break anything.** It's just text. Experiment freely.
- **Messy is fine.** Approximate dates ("about 1890"), uncertain spellings, and
  half-finished people are the normal state of this work, not mistakes.
- **Write the story, too.** Anything in a `## Notes` section is welcome - memories,
  context, guesses. Facts you want treated as *proven* should point at a source.
- **Want help later?** This archive is also designed to be operated by an AI
  assistant and a small set of optional tools. See `SPEC.md` and `AGENTS.md` when
  you're curious - but none of it is required to keep going by hand.
