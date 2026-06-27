# Getting Started - Your First Day

**Who this is for:** a genealogist setting up their own Plainfile archive and filing a first
document. No programming required - you'll work with an AI assistant that runs the commands for you.

- **Were you sent here to hand over photos or documents?** You don't need any of this - see
  [`CONTRIBUTING_SOURCES.md`](CONTRIBUTING_SOURCES.md) instead.
- **Do you want to build or extend the `fha` tools?** That's a different door - start at
  [`../BUILD.md`](../BUILD.md), then [`../TOOLING.md`](../TOOLING.md).
- **Did someone send you a zip of this project?** You can skip the download below and follow
  [`SETUP_FROM_ZIP.md`](SETUP_FROM_ZIP.md), then come back here for the walkthrough.

This page takes you from a blank machine to your first filed record. Two parts: a one-time
**setup** (install three things), then a **five-minute walkthrough** (drop a scan in, get a
suggested fact back, accept it). Take the setup slowly; do the walkthrough once and the daily
rhythm is yours.

---

## Part 1 - Set up your machine (one time)

You install three things. After each one there's a "did it work?" check - a single command to
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

### 2. exiftool (optional - only for photo features)

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

### 3. Your AI assistant (required)

Plainfile is *operated* through an AI coding assistant - it reads the project's rules, runs the
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
is already in this project at [`../archive-template/`](../archive-template/).

1. **Copy the `archive-template` folder** and rename the copy to something like
   `my-family-archive`. Keep it next to the `tools` folder so the tools can reach it. (If you
   got here from a zip, [`SETUP_FROM_ZIP.md`](SETUP_FROM_ZIP.md) shows the exact layout.)
2. **Point it at your photos and documents.** Open `fha.yaml` inside your new folder in a plain
   text editor and tell it where your files live. Copy-paste examples - a plain local folder, an
   external drive, an existing photo library - are in
   [`../archive-template/README.md`](../archive-template/README.md). If you're starting fresh
   with nothing yet, the defaults are fine; leave it as-is.

**Did it work?** From the project folder, run the linter against your archive (it checks that
everything is shaped the way the spec expects):

```
python tools/fha.py lint --root my-family-archive
```

A fresh archive prints **`✓ No issues found.`** - that's a green light. (`--root` just tells the
tools which archive folder to look at.)

---

## Part 3 - File your first document (five minutes)

This is the whole loop in miniature: a scan goes in, a *suggested* fact comes back, you accept
it. Nothing becomes a real fact until you say so.

### Step 1 - Drop a scan in the inbox

Find any scan, photo, or downloaded record - a birth certificate, an old photo, a screenshot
from a genealogy site. Copy the file into the `inbox/` folder inside your archive. That's it;
don't rename it.

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

---

## Doing it by hand (no tools, no IDs)

You don't need the assistant or the tools to add to your archive - the copy-paste templates in
[`../archive-template/`](../archive-template/) let you write a record in any text editor.

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
  with `fha capture`), or just drop in a scan.
- **File & process** - "process my inbox," as above.
- **Review** - accept or set aside the suggested facts (the assistant records each decision with `fha claim`; you are still the one deciding).
- **Report** - ask "what should I look at today?" The assistant runs `fha report` and reads you
  the review queue, gaps to fill, and research leads.

You'll learn the handful of phrases you actually use within a week. You never need the command
names - the assistant translates.

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

Everything is plain text or standard image files. You can open any of it with Notepad, TextEdit,
or a photo viewer - no tool required, now or in fifty years. The tools only ever help; they're
never the thing holding your archive together.

---

## A note on the example archive

The project ships with [`../example-archive/`](../example-archive/) - a small, **entirely
fictional** family (the Hartleys). It's there so the tools have something real-shaped to run
against, and so you can see what finished, processed records look like before you have many of
your own. Poke around in it freely; none of it is real genealogy, so you can't break anything.

---

## What's next

- A one-page **cheat sheet** of the commands and phrases you'll actually use:
  [`CHEATSHEET.md`](CHEATSHEET.md) - print it and keep it by the keyboard.
- Hit a snag? [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) maps each common "something went wrong"
  to its exact fix.
- New to filing research at all? [`FILING_CABINET.md`](FILING_CABINET.md) explains the whole
  archive as the paper filing cabinet you already know.
- Want the deeper "why" behind files-not-a-database and human-approved facts?
  See [`FAQ.md`](FAQ.md).
- Every term and ID type, defined: [`GLOSSARY.md`](GLOSSARY.md).
- The full rulebook, if you ever want it: [`../SPEC.md`](../SPEC.md). You never *have* to read
  it to use the archive - the assistant already follows it for you.
