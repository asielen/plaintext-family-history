# Cheat Sheet - One Page

Print this and keep it by the keyboard. You talk to the **AI assistant** in plain English;
it runs the commands. The command names are here only so nothing feels like a black box.

---

## The daily loop

**Capture → file → process → review → report.** Every session is these five beats.

| Beat | What you say to the assistant | What it runs for you |
|---|---|---|
| **Capture** | "Pull this record into my inbox" (or just drop a scan in `inbox/`) | `fha capture` |
| **File & process** | "Process the new item in my inbox" | `fha process` |
| **Review** | "The name and date are right; leave the place as a suggestion" | `fha claim` (you decide; it records the decision) |
| **Report** | "What should I look at today?" | `fha report` |

You never have to type a command. The phrases above are the whole job.

---

## The handful of commands (if you ever want them)

Replace `my-family-archive` with your archive's folder name.

**Running `fha`.** `fha` is a small launcher file that sits in your workshop copy of the project
- and, if your archive was set up with `fha install`, in the archive folder too. It finds the
tools for you. How you type it depends on where you
are and which terminal you use:

| Where you are | Type |
|---|---|
| Windows, Command Prompt | `fha <command>` |
| Windows, PowerShell | `.\fha <command>` |
| macOS or Linux | `./fha <command>` |

(Put the folder on your PATH once and a bare `fha <command>` works everywhere.)

The examples below name the archive with `--root`, so they run from anywhere - that is the form
to use from your project folder, and it always works. If your archive has its own launcher (it
does when `fha install` created it), you can instead run them from *inside* the archive and drop
`--root`: `fha` uses the archive it finds itself in. An archive made by copying the template has
no launcher of its own, so stay in the project folder and keep `--root`.

```
fha process "inbox/the-file-you-added.jpg" --root my-family-archive   # file one new inbox item
fha report   --root my-family-archive   # the review queue + research leads
fha find --text "Rose Hartley" --root my-family-archive   # search everything
fha doctor   --root my-family-archive   # health check - run this when stuck
fha lint     --root my-family-archive   # "is my archive shaped right?"
fha reconcile --dry-run --root my-family-archive  # after reorganizing documents: preview re-ties, run without --dry-run to apply
fha backup   --root my-family-archive   # dated zip beside the archive - restore = unzip
fha update-tools --dry-run --repo . --root my-family-archive  # preview a tools update (docs/UPDATING.md)
fha relate P-aaaa P-bbbb --root my-family-archive   # how are these two related?
fha views timeline P-aaaa --format html --root my-family-archive   # a printable one-page timeline (lands in generated/views/)
fha photoindex gallery --person P-aaaa --root my-family-archive   # a clickable page of someone's photos - double-click to open (lands in generated/gallery/)
```

`--root` just names which archive folder to use.

---

## How to write an uncertain date

You don't need real dates. Say it the way you'd say it out loud - the tool stores the rest.

| You say | The tool stores |
|---|---|
| "about 1880" | `1880~` |
| "the 1880s" | `188X` |
| "sometime in 1898" | `1898` |
| "February or March 1871" | `1871-02/1871-03` |
| "no idea" | nothing - it stays blank, honestly |

A guess clearly marked as a guess is always better than a wrong exact date.
And it's fine to jot a birth or death date before you've found the record - write it down, and the
assistant keeps it on a gentle "still to source" list until the evidence turns up.

**One source, several dates?** A source's files are always copies or facets of *one* piece of
evidence (SPEC §7) - fronts, backs, extra prints, pages, transcripts - never several separately
published items bundled together (three different newspaper clippings about the same event are
three sources, not one, even mailed in the same envelope). But even one piece of evidence can span
dates: a multi-page letter written across several sittings (started one day, finished a week
later), or a ledger whose entries run for months. Give the source's own date the same range
treatment as "February or March 1871" above - the earliest to the latest, e.g. `1916-02/1916-06` -
so the page shows a true span instead of picking one page's date and leaving the rest unlabeled.
Tell the assistant the earliest and latest dates you can see and it writes the range for you.

---

## How to link to a source or person

Write the name in **double brackets**. That's the whole trick.

| You write | It links to |
|---|---|
| `[[Grandpa Joe]]` | the person named Grandpa Joe (a nickname is fine) |
| `[[Hartley family bible]]` | that source record |
| `born in [[Fairview]]` | the place |
| `[[Caleb Hartley]]` in a person's relationships | his parent, spouse, or child - with its nature noted |

Don't worry about IDs - name your file something sensible, link to it by name, and if you ever run
`fha lint` it assigns the durable IDs and keeps your name-links working. You never have to make one.

Relationships work the same way: under a person, list who they connect to and how - a parent, a
spouse, an adoptive parent - by name. Mark a tie you're sure of with the source that proves it, or
just jot it as a hunch; the assistant keeps the unproved ones on the "still to source" list.

---

## The five kinds of record (and their ID letters)

| | What it is |
|---|---|
| **`P-`** Person | A human - identity, prose, and their ties to other people. |
| **`S-`** Source | A piece of evidence: a record, document, photo, interview. |
| **`C-`** Claim | One sourced statement, living inside its source record - *suggested* until you accept it. |
| **`L-`** Place | A location, with coordinates and a dated name history. |
| **`H-`** Hypothesis | An unsourced working theory - a guess, never a fact, until evidence promotes it. |

---

## Where things live

| Folder | What's in it |
|---|---|
| `inbox/` | New material waiting to be processed - your to-file pile. |
| `sources/` | One record per piece of evidence (a document, photo, interview). |
| `people/` | The people in your tree, in numbered family-couple folders. |
| `places/` | The list of places, with their locations. |
| `notes/` | Research in progress and your running questions. |
| `fha.yaml` | The one settings file - where your photos and documents live. Point one outside the archive and its now-empty placeholder folder disappears too (that's expected). |
| `.fha/` | The machinery (the program, its design package, the browser add-on). Hidden on purpose; never hand-edited. |
| `generated/` | Built things - website, printable views, galleries. Rebuildable, never truth. |

Everything is plain text or standard image files. Open any of it with Notepad, TextEdit, or a
photo viewer - no tool required, now or in fifty years. It opens in **Obsidian** as-is too:
Markdown, frontmatter, `[[wikilinks]]`, no import step.

---

## Three rules that keep you safe

1. **Nothing becomes a fact until *you* accept it.** The assistant only ever *suggests*.
2. **Photos are never renamed in your library.** Drop them in as-is; identity rides in hidden metadata, not the name.
3. **Mark anything private with `restricted`.** A person, a fact, a source, or an old name -
   restricted material stays in your archive but never leaves in anything you share.

---

*Stuck? See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md). New here?
[GETTING_STARTED.md](GETTING_STARTED.md), right beside this page.
Every term defined: [docs/GLOSSARY.md](docs/GLOSSARY.md).*
