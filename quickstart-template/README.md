# Start Your Family Archive by Hand

This is a blank starter kit for a family-history archive made of **plain files**. There's nothing to install. You fill it in with any text editor, and the files open in any text editor for as long as text files exist.

> **Tip:** an editor that understands `[[links]]` makes this easier. The free app **Obsidian** is a good choice: when you type `[[` it lets you link one person or source to another, and it can draw your family as a graph. Plain Notepad or TextEdit works too; you type the links by hand.

## What's in here

```
people/    one folder per ancestral couple, already numbered for you (002-014)
sources/   one file per piece of evidence (a certificate, a census, a photo)
places/    an optional list of the towns your family lived in
notes/     questions and research in progress
inbox/     a place to drop new scans before you file them
documents/ your scans and certificates       (delete if you keep them elsewhere)
photos/    your photographs                   (delete if you keep them elsewhere)
fha.yaml   settings for the optional tools; you can ignore it when working by hand
```

## Quick start

**1. Decide where your photos and documents go.** If you'd rather keep everything together in this archive, do nothing. If you already keep them in a folder somewhere else, delete the `documents/` and `photos/` folders here. (If you start using the optional tools later, you'll tell them where those folders are in `fha.yaml`.)

**2. Make yourself the first person.** Open the `people/` folder and find **`002 - Your Name Here - RENAME THIS FOLDER`**. Rename it with your actual name. Inside are two files, one for you and one for your partner (skip the partner if it doesn't apply).
- Open your file, fill in the part at the top between the `---` lines (name, birth date and so on), and write a few facts in the Notes section. Approximate is fine.

**3. Add your closest relatives.** Do the same for the folders one generation up:
- **004 - Your Parents**
- **008 - Your Father's Parents** and **010 - Your Mother's Parents**

If you have a partner, also fill in **006** (their parents) and **012** and **014** (their grandparents). You don't have to do all of them; a name and a rough birth year in each is a good start.

> **About the numbers:** they're labels that keep ancestors in order (a genealogy numbering system called Ahnentafel). You never have to work them out; they're already on the folders. Rename the *words* after the number and leave the number alone.

**4. Add your first sources.** Once you have some people, start backing up facts with evidence. Begin with core documents like **birth certificates** or **census records**.
1. Put the scan or photo in the `documents/` folder (or `photos/` for photos).
2. Open the `sources/` folder and **copy** the `_TEMPLATE` file.
3. Rename the copy to match the document (same name, but ending in `.md`).
4. Open it and fill in what the source is, plus a fact or two it proves (its "claims").
5. In an editor with wikilinks, connect the source to the people it mentions by typing their names in `[[double brackets]]`.

Repeat that for each new document: add people, add sources, link them.

## A few rules

- **You can't break anything.** It's just text. Experiment freely.
- **Messy is fine.** Approximate dates ("about 1890"), uncertain spellings and half-finished people are the normal state of this work, not mistakes.
- **Write the story, too.** Anything in a `## Notes` section is welcome: memories, context, guesses. Facts you want treated as *proven* should point at a source.
- **Want help later?** This archive uses the same format as the Plaintext Family History tools and AI assistant, so they can take it over whenever you like. The rules and the tools are at <https://github.com/asielen/plaintext-family-history>. None of it is needed to keep going by hand.
