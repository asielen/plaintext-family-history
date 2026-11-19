# Cheat Sheet

Print this and keep it by the keyboard. You talk to the **AI assistant** in plain English and it runs the commands. The command names are here so you can see what it's doing.

---

## The daily loop

**Capture, file and process, review, report.**

| Step | What you say to the assistant | What it runs for you |
|---|---|---|
| **Capture** | "Pull this record into my inbox" (or drop a scan in `inbox/`) | `fha capture` |
| **File and process** | "Process the new item in my inbox" | `fha process` |
| **Review** | "The name and date are right; leave the place as a suggestion" | `fha claim` (you decide; it records the decision) |
| **Report** | "What should I look at today?" | `fha report` |

You never have to type a command.

---

## Typing fha

`fha` is a small launcher file in your archive folder (and in your copy of the project). It finds the tools for you. How you type it depends on which terminal you use:

| Where you are | Type |
|---|---|
| Windows, Command Prompt | `fha <command>` |
| Windows, PowerShell | `.\fha <command>` |
| macOS or Linux | `./fha <command>` |

The other guides write commands bare (`fha doctor`); add the `./` or `.\` your terminal needs. Putting the folder on your PATH once makes a bare `fha <command>` work everywhere.

## The commands, if you want them

Run these from inside your archive folder. From anywhere else, add `--root` and the path to your archive.

```
fha process "inbox/the-file-you-added.jpg"   # file one new inbox item
fha process "inbox/Trip/IMG_0001.jpg" --type photo --dest "Trip"   # a photo, into a folder of your photo library
fha report                    # the review queue and research leads
fha find --text "Rose Hartley"   # search everything
fha doctor                    # health check: run this when stuck
fha lint                      # "is my archive shaped right?"
fha reconcile --dry-run       # after reorganizing documents or photos: preview the re-links, run without --dry-run to apply
fha reorganize                # preview moving loose documents/ files into type folders; add --apply --yes to do it
fha backup                    # dated zip beside the archive; to restore, unzip it
fha update-tools --dry-run --repo PATH-TO-PROJECT   # preview a tools update (docs/UPDATING.md)
fha relate p-aaaa p-bbbb      # how are these two related?
fha metrics                   # who still lacks a birth, census or marriage record?
fha metrics "Ann Smith"       # one person's checks in detail, by name or p-id
fha calendar today            # who in the family was born, married or died on this day?
fha person new "Ann Smith" --sex F   # add one new person (refuses a name already on file)
fha person new --from-file people.tsv   # add a list at once: name, sex, surname per line, tab-separated
fha views timeline p-aaaa --format html   # a printable one-page timeline (lands in generated/views/)
fha photoindex gallery --person p-aaaa    # a clickable page of someone's photos; double-click to open (lands in generated/gallery/)
```

---

## How to write an uncertain date

You don't need exact dates. Say it the way you'd say it out loud, and the assistant writes it down in this form:

| You say | It is written |
|---|---|
| "about 1880" | `1880~` |
| "the 1880s" | `188X` |
| "sometime in 1898" | `1898` |
| "before 1920" | `[..1920]` |
| "after 1920" (a burial known only from the death date) | `[1920..]` |
| "February or March 1871" | `1871-02/1871-03` |
| "no idea" | nothing; leave it blank |

A guess marked as a guess is better than a wrong exact date. It's fine to write down a birth or death date before you've found the record; the assistant keeps it on a "still to source" list until the evidence turns up.

**One source, several dates?** One source is one piece of evidence, but it can span dates: a letter written over several days, or a ledger whose entries run for months. Give the source's date as a range, earliest first, like `1916-02/1916-06`. Tell the assistant the earliest and latest dates you can see and it writes the range. If a range comes out backwards (`1950/1940`), the tools ask which year you meant.

---

## How to link to a source or person

Write the name in **double brackets**:

| You write | It links to |
|---|---|
| `[[Grandpa Joe]]` | the person named Grandpa Joe (a nickname is fine) |
| `[[Hartley family bible]]` | that source record |
| `born in [[Fairview]]` | the place |
| `[[Caleb Hartley]]` in a person's relationships | his parent, spouse or child, with the kind of tie noted |

Don't worry about IDs. Name your file something sensible and link to it by name; `fha lint --fix-ids` (or the assistant) adds the permanent IDs and keeps your name links working.

Relationships work the same way: under a person, list who they connect to and how (a parent, a spouse, an adoptive parent) by name. Give a tie you're sure of the source that proves it, or write it down as a guess; the assistant keeps the unproved ones on the "still to source" list.

---

## The five kinds of record and their ID letters

| | What it is |
|---|---|
| **`p-`** Person | One human: their names, story and ties to other people. |
| **`s-`** Source | One piece of evidence: a record, document, photo or interview. |
| **`c-`** Claim | One statement from a source, kept inside that source's record. *Suggested* until you accept it. |
| **`l-`** Place | A location, with coordinates and its names over time. |
| **`h-`** Working answer | Your best guess at an open question. Never a fact until a source proves it. |

---

## Where things live

| Folder | What's in it |
|---|---|
| `inbox/` | New material waiting to be processed: your to-file pile. |
| `sources/` | One record per piece of evidence (a document, photo, interview). |
| `people/` | The people in your tree, in numbered family-couple folders. |
| `places/` | The list of places, with their locations. |
| `notes/` | Research in progress and your running questions. |
| `fha.yaml` | The one settings file: where your photos and documents live. If you point one outside the archive, its empty folder inside the archive is removed. |
| `.fha/` | The program, its site design and the browser add-on. Hidden; you never edit it. |
| `generated/` | Things the tools build: website, printable views, galleries. All rebuildable. |

Everything is plain text or standard image files. Open any of it with Notepad, TextEdit or a photo viewer. It also opens in **Obsidian** as it is.

A clipping that names nobody in your family is filed as `ephemera`: kept and searchable, but on nobody's page. See the [FAQ](FAQ.md#does-every-photo-or-document-need-to-become-a-source).

---

## Three rules that keep you safe

1. **Nothing becomes a fact until *you* accept it.** The assistant only suggests.
2. **Photos are never renamed in your library.** Drop them in as they are; their ID is stored inside the file.
3. **Mark anything private with `restricted`.** A person, a fact, a source or an old name marked restricted stays in your archive and is left out of anything you share.

---

*Stuck? See [TROUBLESHOOTING.md](TROUBLESHOOTING.md). New here? [GETTING_STARTED.md](GETTING_STARTED.md) is beside this page. Every term is defined in [GLOSSARY.md](GLOSSARY.md).*
