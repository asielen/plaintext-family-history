# Getting Started - Your First Day

**Who this is for:** a genealogist setting up their own Plaintext archive and filing a first
document. No programming required - you'll work with an AI assistant that runs the commands for you.

- **Were you sent here to hand over photos or documents?** You don't need any of this - see
  [`docs/CONTRIBUTING_SOURCES.md`](docs/CONTRIBUTING_SOURCES.md) instead.
- **Do you want to build or extend the `fha` tools?** That's a different door - the build
  guide and tool design live in the project repo on GitHub
  ([plaintext-family-history](https://github.com/asielen/plaintext-family-history)).
- **Did someone send you a zip of this project?** You can skip the download below and follow
  [`docs/SETUP_FROM_ZIP.md`](docs/SETUP_FROM_ZIP.md), then come back here for the walkthrough.

This page takes you from a blank machine to your first filed record. Two parts: a one-time
**setup** (install four things), then a **five-minute walkthrough** (drop a scan in, get a
suggested fact back, accept it). Take the setup slowly; do the walkthrough once and the daily
rhythm is yours.

---

## What you're setting up

Your archive is a **filing cabinet made of plain files**. For a hundred years genealogy lived in
one and anyone could open the drawer - no login, no subscription, no format that stopped opening.
That is the whole idea here: the files on your disk are the real thing, and everything else -
the search index, the family website, the AI assistant - is a helper built *from* them that you
could delete tomorrow without losing a single record.

You keep five kinds of record, all plain Markdown and YAML you can open in Notepad or TextEdit:

| Type | What it is |
|---|---|
| **Person** `P-` | A human - identity, prose, and the ties to other people. |
| **Source** `S-` | A piece of evidence: a record, document, photo, interview. |
| **Claim** `C-` | One sourced statement (a date, a place, a relationship), living inside its source record and moving from *suggested* to *accepted* only when you say so. |
| **Place** `L-` | A location, with coordinates and a dated name history. |
| **Hypothesis** `H-` | An unsourced working theory - a guess, never a fact, until evidence turns it into a claim. |

Two things follow from that, and they never bend:

- **Every important fact traces to a source.** Prose with no citation is story or context, never
  fact.
- **Nothing generated is load-bearing.** The index, the trees, the website - all rebuildable, all
  disposable. Delete every layer above the files and the archive still works, the way the drawer
  still works.

---

## Part 1 - Set up your machine (one time)

You install four things. After each one there's a "did it work?" check - a single command to
run so you're never guessing. You run these checks in a **terminal**: the Command Prompt on
Windows, or the Terminal app on Mac. Type the command, press Enter, and compare what you see to
what's described.

### 1. Python (required)

Python is the engine the `fha` tools run on. It's free.

1. Go to **<https://www.python.org/downloads/>** and click the big "Download Python" button.
2. Run the installer. **On Windows, tick the box that says "Add Python to PATH"** before you
   click Install - this one checkbox saves a lot of grief.

**Did it work?** In a terminal, run:

```
python --version
```

You should see something like `Python 3.12.1` (any 3.10 or newer is fine). If you see
`command not found` or `not recognized`, Python isn't on your PATH - on Windows, re-run the
installer and tick that box; on Mac, try `python3 --version` instead (Macs often use `python3`).

> Throughout this guide, where you see `python`, use `python3` if that's the one your Mac
> answers to. Everything else is identical.

### 2. The Python helpers (required)

The tools lean on a few small, free helper packages. Python's built-in installer (`pip`)
fetches them all with one command. In a terminal, from the project folder (the one with
`tools/` inside), run:

```
python -m pip install -r tools/requirements.txt
```

You'll see a few lines of progress and a "Successfully installed" at the end. (Don't have the
project folder in front of you yet? `python -m pip install pyyaml` installs the essential one
from anywhere; run the full command later.) That one command also covers the extras needed much
later for the family website - nothing more to install when you get there.

**Did it work?** Run:

```
python -c "import yaml"
```

Printing *nothing at all* is the good sign - the helper answered quietly. If you see
`No module named 'yaml'` instead, the install didn't land - re-run the install command and read
its last lines for the reason.

### 3. exiftool (optional - only for photo features)

`exiftool` lets the archive read and write the hidden metadata inside photos (so a scan can
carry its own ID and keywords). **If you're starting with documents and notes, skip this for
now** and add it later when you bring in a photo library. Nothing in the walkthrough below needs it.

When you're ready: download it from **<https://exiftool.org/>** (Windows users grab the
"Windows Executable"; Mac users can use the installer there or `brew install exiftool`).

**Did it work?** In a terminal:

```
exiftool -ver
```

A version number like `12.76` means you're set. An error just means it isn't installed yet -
no harm, the rest still works.

### 4. Your AI assistant (required)

Plaintext is *operated* through an AI coding assistant - it reads the project's rules, runs the
`fha` commands, and drafts sourced facts for you to approve. You never have to memorize a
command; you ask in plain English. The reference assistant is
**[Claude Code](https://www.anthropic.com/claude-code)** - follow the install instructions on
that page. (Any assistant that can read `AGENTS.md` and run shell commands works; Claude Code is
the one this guide assumes.)

**Did it work?** Open the project folder in your assistant and ask it something simple, like:

> "What is this project, and what mode should we work in?"

If it answers by describing a family-history archive and proposes **research mode**, the rules
loaded correctly and you're ready. (It reads them from `CLAUDE.md` → `AGENTS.md` automatically -
you don't have to point it at anything.)

---

## Part 2 - Make your archive

Your family records live in **their own folder**, separate from the tools. The starting skeleton
is the `archive-template` folder in your copy of the project (on GitHub it is
[archive-template/](https://github.com/asielen/plaintext-family-history/tree/master/archive-template)).
If you already have an archive - someone ran `fha install` for you, or you unzipped one - you
have all this already; skip to Part 3.

1. **Copy the `archive-template` folder** and rename the copy to something like
   `my-family-archive`. Keep it next to the `tools` folder so the tools can reach it. (If you
   got here from a zip, [`docs/SETUP_FROM_ZIP.md`](docs/SETUP_FROM_ZIP.md) shows the exact
   layout.)
2. **Point it at your photos and documents.** Open `fha.yaml` inside your new folder in a plain
   text editor and tell it where your files live. Copy-paste examples - a plain local folder, an
   external drive, an existing photo library - are commented right inside that file, just below
   the settings they explain. If you're starting fresh with nothing yet, the defaults are fine;
   leave it as-is.

**Did it work?** From the project folder, run the check against your archive (it looks for
anything shaped the wrong way). How you type it depends on your system - on macOS and Linux a
bare `fha` is not found unless the folder is on your PATH, so start with `./`:

```
./fha check --root my-family-archive      # macOS / Linux
.\fha check --root my-family-archive      # Windows PowerShell
fha check --root my-family-archive        # Windows Command Prompt
```

A fresh archive prints **`✓ No issues found.`** - that's a green light. (`--root` just tells the
tools which archive folder to look at. `check` is the friendly name for the command also called
`lint`.)

> **Running `fha`.** `fha` is a small launcher file that sits in the project folder - and, once
> you have one, in your archive folder too. It finds the tools and runs them, so you never type a
> path. On **macOS or Linux** type `./fha <command>`; in Windows **PowerShell**, `.\fha <command>`;
> the Windows **Command Prompt** accepts a bare `fha <command>`. (Adding the folder to your PATH
> once makes a bare `fha` work everywhere.) The `--root` shown here names which archive to use.
>
> This walkthrough builds your archive by copying the template, which puts your records in place
> but no launcher beside them - so run every command from the **project folder** with `--root`,
> as shown. (`fha install` is the other way to set up an archive; it puts a launcher in the
> archive itself, and only then can you `cd` in and drop `--root`.)

### Coming from Ancestry (or another genealogy program)?

You don't re-type anything. Every genealogy program can export your tree as a **GEDCOM** file
(on Ancestry: Trees → your tree → Tree Settings → "Export tree" - you get a `.ged` file in your
Downloads). Then ask your assistant:

> "Import my GEDCOM file" (or run
> `fha gedcom import family-tree.ged --root my-family-archive`)

First it shows you a **plan** - how many people, families, and statements it found - and writes
nothing. Add `--apply` and every person in your tree becomes a record, every assertion becomes a
*suggested* fact citing the GEDCOM file (which is filed as a source, your original untouched).
Nothing imported is treated as proven: your tree's statements wait in the same review queue as
everything else, and you review them family by family, whenever you like - never all at once.

### Tell it about your own family first (two minutes, recommended)

Before you file anything else, it's worth telling your assistant about your own immediate family. You
are the one person the archive is guaranteed to have firsthand knowledge about, and nothing else will
ever ask:

> "Let's do the setup interview."

It asks who you are (so it can number the family tree correctly), then your parents, spouse, and
children - names, roughly when they were born, nothing more. What you say becomes a real, citable
source and a handful of *suggested* facts, exactly like everything else in this archive - reviewed and
accepted the same way, never taken as settled just because you're the one who said it.

---

## Part 3 - File your first document (five minutes)

This is the whole loop in miniature: a scan goes in, a *suggested* fact comes back, you accept
it. Nothing becomes a real fact until you say so.

### Step 1 - Drop a scan in the inbox

Find any scan, photo, or downloaded record - a birth certificate, an old photo, a screenshot
from a genealogy site. Copy the file into the `inbox/` folder inside your archive. That's it;
don't rename it.

Got more than one separately-published item - two different letters, two different newspaper
clippings, even about the same event? Drop each one in on its own; it becomes its own record. A
single item's own pages, fronts and backs, or extra scans belong together and become one record,
but two separately published things never get bundled into one just because they arrived
together.

Optionally, drop a short note beside it describing what it is - copy
`inbox/_TEMPLATE.notes.md`, rename the copy `notes.md`, and answer the questions in plain words
("a photo of Grandma Rose's wedding, around 1955, found in a shoebox"). The assistant uses your
note as hints. Skipping it is fine too.

### Step 2 - Ask the assistant to process it

In your assistant, say:

> "Process the new item in my inbox."

Behind the scenes it runs `fha process`, which mints a permanent ID for the source, files it
into the right place, and creates a record for it. Then it reads the document, works out the
names, dates, and places, and **drafts each one as a suggested fact** - never as a settled fact.
It'll show you what it found.

### Step 3 - Review and accept

The assistant shows you each suggested fact next to the words in the document it came from.
You're the judge. Reply in plain English:

> "The birth date and the name are right. I'm not sure about the place - leave that one as a
> suggestion for now."

The ones you approve get marked **accepted** and stamped with today's date. The rest stay as
suggestions until evidence or your memory settles them. **You are the only thing that turns a
suggestion into a fact** - the assistant can never do it on its own.

That's a filed record. You just did the core loop of the whole system.

**Prefer clicking to typing?** Run `fha serve` (or double-click `serve.cmd` in your archive
folder) to open the same review queue and inbox in a private browser tab on your own machine -
every button there is the same command you'd otherwise ask the assistant for, shown to you
before it writes anything. Nothing about the archive changes because you used it; close the
window and pick up in the assistant exactly where you left off.

---

## Doing it by hand (no tools, no IDs)

You don't need the assistant or the tools to add to your archive - the copy-paste templates let
you write a record in any text editor. Every record folder in your archive ships one: open
`people/_TEMPLATE.person.md` (one template for a person either way - a bare reference or a fully
written-up profile; it defaults to `tier: stub`), `sources/_TEMPLATE.source.md`,
`inbox/_TEMPLATE.notes.md`, or the commented `_TEMPLATE` entry at the top of
`places/places.yaml`, copy it, and fill it in.

- **Name files plainly.** Call a file `grandpas-letter.md` or `hartley-thomas.md` - whatever makes
  sense to you. Don't worry about making an ID; that's the tools' job.
- **Link by name.** To cite a source or point at a person, write its name in double brackets:
  `[[Grandpa Joe]]`, `[[Hartley family bible]]`, `born in [[Fairview]]`. A nickname works too.
- **Jot what you only half-know.** Fairly sure great-grandma was born around 1849? Write
  `birth: 1849~` on her record. It's a *provisional* date - perfectly fine to record now, and the
  assistant keeps it on a "still to source" list until the proof turns up.

If you ever run `fha lint`, it quietly assigns the durable IDs, keeps your filename as an alias so
your `[[name]]` links keep working, and tidies everything. IDs are just sturdier for the long
haul - filenames change and can repeat - but you never have to create one.

---

## The daily rhythm

Every working session is the same five beats:

**Capture → file → process → review → report.**

- **Capture** - pull a record off a genealogy site into the inbox (the assistant can do this
  with `fha capture`), or just drop in a scan. There is also a browser extension that stages
  the open page with one click: your archive carries a ready-to-load copy at
  `.fha/browser-companion/` - open `chrome://extensions`, turn on Developer mode, click
  **Load unpacked**, and pick that folder (its README has the details; on Windows `.fha` is
  hidden, so type the path into the picker's file-name box if it is not listed).
- **File & process** - "process my inbox," as above.
- **Review** - accept or set aside the suggested facts (the assistant records each decision with `fha claim`; you are still the one deciding).
- **Report** - ask "what should I look at today?" The assistant runs `fha report` and reads you
  the review queue, gaps to fill, and research leads.

You'll learn the handful of phrases you actually use within a week. You never need the command
names - the assistant translates.

---

## Back it up (one command)

Your archive is plain files, so a backup is just a copy - and one command makes a good one.
Ask the assistant to "back up my archive," or run it yourself:

```
fha backup --root my-family-archive
```

That writes a dated zip file into a folder **beside** your archive (named
`my-family-archive-backups`), checks every file inside it, and tells you where it landed. Copy
that zip somewhere that isn't this computer - a USB stick, an external drive, a cloud folder.

Two things worth knowing:

- **Your photos and documents are not in that zip** unless you add `--include-assets` - they're
  often huge and often live on another drive with its own backup. The command names them every
  time so nothing is skipped silently, and `fha doctor` lists every folder a full backup must
  cover (and tells you when you last actually made one).
- **To restore: unzip the file.** That's the whole procedure. A backup is just your files.

Do this at the end of any session where you added something you'd hate to lose.

---

## Where things live (so nothing feels like a black box)

| Folder | What's in it |
|---|---|
| `inbox/` | New material waiting to be processed - your "to-file" pile. |
| `sources/` | One record per piece of evidence (a document, a photo, an interview). |
| `people/` | The people in your tree, in numbered family-couple folders. |
| `places/` | The list of places, with their locations. |
| `notes/` | Research in progress and your running list of questions. |
| `fha.yaml` | The one settings file - where your photos and documents live. |
| `.fha/` | The machinery: the program itself, its design package, and the browser add-on. Hidden on purpose, so the archive root shows your genealogy rather than the tooling. You never edit anything in here. |
| `generated/` | Built things - the family website, printable views, galleries. All rebuildable, none of it truth. |

Everything is plain text or standard image files. You can open any of it with Notepad, TextEdit,
or a photo viewer - no tool required, now or in fifty years. The tools only ever help; they're
never the thing holding your archive together.

**It opens in Obsidian too.** Your archive is Markdown with frontmatter and `[[wikilinks]]` -
point Obsidian (or any other Markdown app) at the folder and it opens as-is, no import, no
conversion. See [`docs/USING_WITH_OBSIDIAN.md`](docs/USING_WITH_OBSIDIAN.md).

Your `documents/` drawer (wherever `fha.yaml` says it lives) is yours to lay out: make any
folders you like inside it - by type, by family line, by decade - anything you place in a
folder keeps its spot when it's processed (something dropped loose at the drawer's top level
gets filed into a type folder for you), and you can rearrange it later too: the ID tag in each
filed item's name ties it to its evidence folder, so after a reshuffle one command
(`fha reconcile`, or just ask the assistant) re-ties every moved file. If a big batch of imports
ever leaves a real backlog of loose files behind, `fha reorganize` (or just ask) proposes the
whole tidy-up at once - it only ever touches material still sitting exactly where a machine
filed it, so anything you already organized by hand is left alone, untouched. (Photos are even freer: as you organize your library, the system never renames or moves them at all.) When
you're not sure where a stray research note belongs, the "Where does a note go?" list in
[`docs/FILING_CABINET.md`](docs/FILING_CABINET.md) answers it in four lines.

---

## Your archive and the project are two separate things

This matters more than it sounds, because it is what makes the archive outlive the software:

- **The project** is public: the rules (`SPEC.md`, `TOOLING.md`, `AGENTS.md`), the guides, and
  the generic `fha` tools. The tools hold no family data and work on *any* archive built to
  these rules.
- **Your archive** is private and entirely yours: your records, your photos, your `fha.yaml`.
  Your family data never goes into the public project. The cookbook is public; your groceries
  are not.

The tools reach your archive by being **copied into it** - `fha install` puts the whole
operating layer inside, and from then on the archive is self-contained: it works on any machine,
offline, forever, even if the project disappears. `fha` is never `pip install`ed for exactly
that reason; your archive owns its own copy so it cannot be uninstalled out from under you.
(Its Python dependencies *are* ordinary packages - Part 1 §2 installs them, and `fha doctor`
names any that go missing.)

When a newer version comes out, `fha update-tools` refreshes that copy. Be clear about what
"refresh" means:

- **Never touched:** your records, `fha.yaml`, your place list, and your `custom.css` stylesheet.
- **Replaced:** tool files and rulebooks. If you had edited one, your version is moved into
  `.plaintext-backup/` first and the new stock file takes its place - so your edit survives, but
  stops being in effect until you re-apply it.
- **Never deleted:** anything the update retires is moved aside and reported, never thrown away.
  You are always the one who throws things away.

The full ritual - preview, apply, review - is in [`docs/UPDATING.md`](docs/UPDATING.md).

---

## A note on the example archive

The project keeps a small, **entirely fictional** family (the Hartleys) as a worked example, so
you can see what finished, processed records look like before you have many of your own. Two
ways to look at it, neither of them inside your own archive:

- The **[live example site](https://asielen.github.io/plaintext-family-history/)** - that family
  compiled by `fha site`, which is the same self-contained, privacy-redacted output you would
  publish or hand a cousin on a USB stick.
- The files behind it, in the project repo:
  [example-archive/](https://github.com/asielen/plaintext-family-history/tree/master/example-archive).

None of it is real genealogy, so you can't break anything by poking around.

---

## What's next

- A one-page **cheat sheet** of the commands and phrases you'll actually use:
  [`CHEATSHEET.md`](CHEATSHEET.md), right beside this file - print it and keep it by the keyboard.
- Hit a snag? [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) maps each common "something
  went wrong" to its exact fix.
- A newer version of the project came out? [`docs/UPDATING.md`](docs/UPDATING.md) is the
  two-minute update ritual - your records are never part of it.
- New to filing research at all? [`docs/FILING_CABINET.md`](docs/FILING_CABINET.md) explains the
  whole archive as the paper filing cabinet you already know.
- Want the deeper "why" behind files-not-a-database and human-approved facts?
  See [`docs/FAQ.md`](docs/FAQ.md).
- Every term and ID type, defined: [`docs/GLOSSARY.md`](docs/GLOSSARY.md).
- Everything else in the manual: [`docs/README.md`](docs/README.md) indexes the lot.
- The full rulebook, if you ever want it: [`SPEC.md`](SPEC.md). You never *have* to read
  it to use the archive - the assistant already follows it for you.
