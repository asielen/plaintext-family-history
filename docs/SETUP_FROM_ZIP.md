# Setup From a Zip (No GitHub, No Git)

**Who this is for:** you received or downloaded this project as a **zip file**, and you want to set up your archive without learning git or making a GitHub account. You need neither. Git is a tool for sharing and tracking changes; it can be useful later but is never required. A zip is a folder packed into one file: unzip it and you have the folder.

Follow this page start to finish and you'll have a working archive and have run your first command. The [five-minute walkthrough in GETTING_STARTED](GETTING_STARTED.md#file-your-first-document-five-minutes) is the next thing to read.

---

## Step 1: Unzip it somewhere permanent

Find the zip file (often named something like `plaintext-family-history.zip`). Unzip it into a place you'll keep for good; your **Documents** folder is a good choice. Avoid Downloads or the Desktop, which people tend to clean out.

- **Windows:** right-click the zip, choose "Extract All…", and pick a location like `Documents\plaintext-family-history`.
- **Mac:** double-click the zip; a folder appears next to it. Drag that folder into `Documents`.

Open the folder you just unzipped. Inside you'll see `tools`, `SPEC.md`, `AGENTS.md`, an `archive-template` folder and more. **This folder is your workshop:** the tools and the rules live here. Your family records go in a separate folder, which you make in Step 4.

---

## Step 2: Install Python

The tools run on Python, which is free and is the only program you install. (A few small helper packages come in Step 3 with one command. `exiftool`, for photo metadata, is optional and can wait; the AI assistant is covered in [GETTING_STARTED](GETTING_STARTED.md#4-your-ai-assistant-required).)

1. Go to **<https://www.python.org/downloads/>** and click "Download Python."
2. Run the installer. **On Windows, tick "Add Python to PATH"** before clicking Install.

**Did it work?** Open a terminal (Command Prompt on Windows, Terminal on Mac) and type:

```
python --version
```

A line like `Python 3.12.1` (3.10 or newer) means it worked. On a Mac you may need to type `python3` instead of `python`; if so, use `python3` everywhere below.

---

## Step 3: Install the helpers

Open a terminal **in your workshop folder**:

- **Windows:** open the unzipped folder in File Explorer, click the address bar, type `cmd` and press Enter. A terminal opens in that folder.
- **Mac:** in Terminal, type `cd ` (with a space), drag the unzipped folder onto the window and press Enter.

Then run:

```
python -m pip install -r tools/requirements.txt
```

**Did it work?** Run `python -c "import yaml"`. If it prints nothing at all, the helpers are installed. (If it says `No module named 'yaml'`, run the install command again and read its last lines for the reason.)

---

## Step 4: Make your archive

In the same terminal, run the line for your system:

```
./fha install ../my-family-archive      # macOS / Linux
.\fha install ..\my-family-archive      # Windows PowerShell
fha install ..\my-family-archive        # Windows Command Prompt
```

That creates a `my-family-archive` folder beside the workshop, with its own copy of the tools, the manual and the assistant's instructions:

```
Documents/
├── plaintext-family-history/   ← the folder you unzipped (your workshop)
└── my-family-archive/          ← YOUR records live here
    ├── START_HERE.md           ← explains the archive to anyone who opens it
    ├── fha.yaml                ← settings
    ├── inbox/                  ← drop new scans here
    ├── sources/  people/  places/  notes/
    ├── docs/                   ← the manual
    └── …
```

Everything works offline, with no internet and no GitHub.

---

## Step 5: Point `fha.yaml` at your photos and documents

Open `fha.yaml` inside `my-family-archive` with a plain text editor (Notepad on Windows, TextEdit on Mac, not Word). It tells the tools where your files live.

- **Starting with nothing yet?** Leave it as it is. The defaults work; come back when you have a photo library to connect.
- **Already have folders of photos or documents?** Examples for a local folder, an external drive and an existing photo library are written as comments in `fha.yaml`, above the settings they explain. Remove the `#` from the one that matches and edit the path. If you point a setting outside the archive, the empty folder of that name inside the archive is removed; it comes back if you point the setting back inside.

---

## Step 6: Run your first command

Open a terminal in your new `my-family-archive` folder (the same way as in Step 3) and run the check:

```
./fha check      # macOS / Linux
.\fha check      # Windows PowerShell
fha check        # Windows Command Prompt
```

A fresh archive prints:

```
✓ No issues found.
```

Python works, the tools work and your archive is valid. (`fha` is a launcher file in your archive folder that finds and runs the tools; the [cheat sheet](CHEATSHEET.md#typing-fha) explains how to type it.)

---

## What now

- **File your first document:** the [five-minute walkthrough in GETTING_STARTED](GETTING_STARTED.md#file-your-first-document-five-minutes) starts here. Drop a scan in `inbox/`, let the assistant process it, and accept the facts it suggests.
- **Set up the AI assistant** that runs the archive day to day: [GETTING_STARTED, step 4](GETTING_STARTED.md#4-your-ai-assistant-required).

## Keeping up to date without git

When a newer version comes out, download the new zip and unzip it beside the old workshop. Then, from inside your archive folder, run:

```
fha update-tools --dry-run --repo "PATH-TO-THE-NEW-FOLDER"
```

to preview, and the same command without `--dry-run` to apply. It backs up anything you edited before replacing it, never deletes anything, and never touches your records, `fha.yaml` or place list. The full steps are in [UPDATING.md](UPDATING.md). Once the archive is updated you can delete the old workshop folder.

## Building an archive by hand

If you'd rather not use the tools at all, copy the `archive-template` folder out of the workshop, rename the copy, and fill in records with a text editor. `archive-template/README.md` explains the templates. You can run `fha install` on that copy later as long as you haven't edited anything in it yet.

## Backups

Without git, your own copies are how you undo a mistake. `fha backup` writes a dated, checked zip into a folder beside your archive. Copy that zip somewhere separate: an external drive, another computer or a cloud-storage folder. Your records are plain files, so that zip is a complete backup; to restore it, unzip it. Photos and documents aren't included unless you add `--include-assets`, and the command names them every time so nothing is left out silently.
