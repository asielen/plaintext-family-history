# Setup From a Zip - No GitHub, No Git, No Clone

**Who this is for:** you received (or downloaded) this project as a **zip file**, and you want to
set up your archive without learning git or making a GitHub account. You don't need either. Git
is just a tool for sharing and tracking history - useful later, never required to start. A zip is
simply a folder in a box; unzip it and you have a folder.

This page stands on its own. If you follow it start to finish, you'll have a working archive and
have run your first command - no other setup page required (though the
[five-minute walkthrough in GETTING_STARTED](GETTING_STARTED.md#part-3--file-your-first-document-five-minutes)
is the natural next read).

---

## Step 1 - Unzip it somewhere permanent

Find the zip file (often named something like `plainfile-family-history.zip`). Unzip it into a
place you'll keep for the long haul - your **Documents** folder is perfect. Avoid Downloads or
Desktop, which people tend to clean out.

- **Windows:** right-click the zip → "Extract All…" → choose a location like
  `Documents\family-history`.
- **Mac:** double-click the zip; a folder appears next to it. Drag that folder into
  `Documents`.

Open the folder you just unzipped. Inside you'll see `tools`, `SPEC.md`, `AGENTS.md`, an
`archive-template` folder, and more. **This folder is your workshop** - the tools and the rules
live here. Your actual family records will live in a folder *inside* it, which you make in Step 3.

---

## Step 2 - Install Python (the one thing you must install)

The tools run on Python - free, and the only required install. (`exiftool`, for photo metadata,
is optional and can wait; the AI assistant that operates the archive is covered in
[GETTING_STARTED](GETTING_STARTED.md#3-your-ai-assistant-required).)

1. Go to **<https://www.python.org/downloads/>** and click "Download Python."
2. Run the installer. **On Windows, tick "Add Python to PATH"** before clicking Install.

**Did it work?** Open a terminal (Command Prompt on Windows, Terminal on Mac), type:

```
python --version
```

A line like `Python 3.12.1` (3.10 or newer) means you're good. On a Mac you may need to type
`python3` instead of `python` - if so, use `python3` everywhere below.

---

## Step 3 - Make your archive folder

Your family records live in their own folder, separate from the tools, so the two never get
tangled. The empty starting skeleton is the `archive-template` folder already sitting in your
workshop.

1. **Copy the `archive-template` folder** (right-click → Copy, then Paste in the same place).
2. **Rename the copy** to something like `my-family-archive`.

You now have a layout like this:

```
family-history/            ← the folder you unzipped (your workshop)
├── tools/                 ← the fha tools
├── SPEC.md  AGENTS.md …   ← the rules
├── archive-template/      ← the blank skeleton (leave it alone)
└── my-family-archive/     ← YOUR records live here
    ├── fha.yaml           ← settings
    ├── inbox/             ← drop new scans here
    ├── sources/  people/  places/  notes/
    └── …
```

Because the tools sit right beside your archive in the same unzipped folder, everything works
fully offline, from this folder, with no internet and no GitHub - exactly the point.

---

## Step 4 - Point `fha.yaml` at your photos and documents

Open `fha.yaml` inside `my-family-archive` with a plain text editor (Notepad on Windows,
TextEdit on Mac - not Word). It tells the tools where your files live.

- **Starting with nothing yet?** Leave it as-is. The defaults work; come back when you have a
  photo library to connect.
- **Already have folders of photos or documents?** Copy-paste examples for a plain local folder,
  an external drive, and an existing photo library are in
  [`../archive-template/README.md`](../archive-template/README.md).

---

## Step 5 - Run your first command

Time to confirm the tools work. Open a terminal **in your workshop folder**:

- **Windows:** open the unzipped folder in File Explorer, click the address bar, type `cmd`, and
  press Enter - a terminal opens already pointed at that folder.
- **Mac:** in Terminal, type `cd ` (with a space), drag the unzipped folder onto the window, and
  press Enter.

Now run the linter against your archive - it checks that everything is shaped correctly:

```
python tools/fha.py lint --root my-family-archive
```

A fresh archive prints:

```
✓ No issues found.
```

That's your green light: Python works, the tools work, and your archive is valid. (`--root`
tells the tools which archive folder to use; `tools/fha.py` is the tool program itself.)

---

## You're set - what now

- **File your first document:** the
  [five-minute walkthrough in GETTING_STARTED](GETTING_STARTED.md#part-3--file-your-first-document-five-minutes)
  picks up exactly here - drop a scan in `inbox/`, let the assistant process it, accept the facts
  it suggests.
- **Set up the AI assistant** that operates the archive day to day:
  [GETTING_STARTED, Part 1 §3](GETTING_STARTED.md#3-your-ai-assistant-required).

## Keeping up to date (still no git)

When a newer version of the project comes out, you don't need git for that either. Download the
new zip, unzip it, and copy its `tools/` folder (plus `SPEC.md`, `TOOLING.md`, `AGENTS.md`,
`AGENTS_TOOLING.md`, `CLAUDE.md`) over the old ones in your workshop. **Never touch your
`my-family-archive` folder when updating** - your records aren't part of the download and stay
exactly as they are.

> **Backups are your safety net, not git.** Since you're not using GitHub, make your own copies:
> periodically zip your `my-family-archive` folder and keep it somewhere separate - an external
> drive, another computer, a cloud-storage folder. Your records are plain files, so a plain copy
> is a complete, future-proof backup.
