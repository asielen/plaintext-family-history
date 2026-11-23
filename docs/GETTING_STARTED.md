# Getting Started: Your First Day

**Who this is for:** a genealogist setting up a Plaintext archive and filing a first document. No programming is required. You work with an AI assistant that runs the commands for you.

- **Were you sent here to hand over photos or documents?** You don't need any of this. See [`CONTRIBUTING_SOURCES.md`](CONTRIBUTING_SOURCES.md) instead.
- **Do you want to build or change the `fha` tools?** The build guide and tool design live in the project repo on GitHub ([plaintext-family-history](https://github.com/asielen/plaintext-family-history)).
- **Did someone send you a zip of this project?** Follow [`SETUP_FROM_ZIP.md`](SETUP_FROM_ZIP.md) for the download, then come back here.

This page takes you from a blank computer to your first filed record. There are two parts: a one-time setup (four installs and one command), then a five-minute walkthrough where you drop a scan in, get a suggested fact back and accept it. Take the setup slowly.

---

## What you're setting up

For a hundred years genealogy lived in filing cabinets, and anyone could open the drawer: no login, no subscription, no file format that stopped opening. Your archive works the same way. The files on your disk are the records. The search index, the family website and the AI assistant are all built from those files, and you could delete any of them without losing a record.

You keep five kinds of record, all plain Markdown and YAML you can open in Notepad or TextEdit:

| Type | What it is |
|---|---|
| **Person** `p-` | One human: their names, their story and their ties to other people. |
| **Source** `s-` | One piece of evidence: a record, document, photo or interview. |
| **Claim** `c-` | One statement a source makes (a date, a place, a relationship). It lives inside its source record and stays *suggested* until you accept it. |
| **Place** `l-` | A location, with coordinates and its names over time. |
| **Working answer** `h-` | Your best guess at an open question. It is never a fact; when a source proves it, the source's claim becomes the fact. |

Two rules follow from that:

- **Every important fact traces to a source.** Writing with no citation is story or context, never fact.
- **Everything the tools build can be rebuilt.** The index, the trees and the website come from your files, so deleting them loses nothing.

---

## Set up your computer (one time)

You install four things. After each one there's a "did it work?" check: a single command to run in a **terminal**. On Windows use **PowerShell**; on a Mac use the Terminal app. Type the command, press Enter, and compare what you see with what's described.

### 1. Python (required)

Python is the program the `fha` tools run on. It's free.

1. Go to **<https://www.python.org/downloads/>** and click the big "Download Python" button.
2. Run the installer. **On Windows, tick the box that says "Add Python to PATH"** before you click Install. Most setup problems on Windows come from missing this box.

**Did it work?** In a terminal, run:

```
python --version
```

You should see something like `Python 3.12.1` (any 3.10 or newer is fine). If you see `command not found` or `not recognized`, Python isn't on your PATH. On Windows, run the installer again and tick that box. On a Mac, try `python3 --version` instead.

> Throughout this guide, where you see `python`, type `python3` if that's the one your Mac answers to.

### 2. The Python helpers (required)

The tools use a few small, free helper packages. Python's built-in installer (`pip`) fetches them with one command, run from the project folder.

The project folder is your copy of Plaintext Family History: either a clone made with git, or the zip from the project's GitHub page (the green "Code" button, then "Download ZIP"), unzipped somewhere permanent such as your Documents folder. [`SETUP_FROM_ZIP.md`](SETUP_FROM_ZIP.md) walks through the zip.

To open PowerShell in a folder on Windows, open the folder in File Explorer, click the address bar, type `powershell` and press Enter. (Or hold Shift, right-click the folder and choose "Open PowerShell window here"; on Windows 11 choose "Open in Terminal".) Command Prompt runs most commands too, but it can garble a name that has quotation marks or symbols in it, so use PowerShell. On a Mac, open Terminal, type `cd ` (with a space), drag the folder onto the window and press Enter.

In a terminal opened in the project folder (the one with `tools/` inside), run:

```
python -m pip install -r tools/requirements.txt
```

You'll see a few lines of progress and "Successfully installed" at the end. The same command installs what the family website needs later, so there's nothing more to install when you get there.

**Did it work?** Run:

```
python -c "import yaml"
```

If it prints nothing at all, the helpers are installed. If you see `No module named 'yaml'`, the install didn't finish: run the install command again and read its last lines for the reason.

### 3. exiftool (optional, only for photo features)

`exiftool` lets the archive read and write the hidden information inside photo files, so a photo can carry its own ID and keywords in the places your photo program (Lightroom included) looks for them. Scanned records get their ID written inside them too, as a spare copy of the one in the filename. **If you're starting with documents and notes, skip this for now** and add it when you bring in a photo library. Nothing in the walkthrough below needs it.

When you're ready, download it from **<https://exiftool.org/>** (Windows users take the "Windows Executable"; Mac users can use the installer there or `brew install exiftool`).

**Did it work?** In a terminal:

```
exiftool -ver
```

A version number like `12.76` means it's installed. An error means it isn't yet, and everything else still works.

### 4. Your AI assistant (required)

You run Plaintext through an AI coding assistant. It reads the project's rules, runs the `fha` commands and drafts sourced facts for you to approve, so you ask in plain English instead of memorizing commands. This guide assumes **[Claude Code](https://www.anthropic.com/claude-code)**; follow the install instructions on that page. OpenAI's **Codex** works the same way: it reads `AGENTS.md` directly and finds the workflow skills in `.agents/skills/`. Any assistant that can read `AGENTS.md` and run commands will work.

**Did it work?** Open the project folder in your assistant and ask:

> "What is this project?"

If it describes a family-history archive built from plain files and mentions the project's rules, it has read its instructions. In the project folder it offers to work on the tools themselves. It switches to researching your family once you open your own archive, in the next step.

---

## Make your archive

Your family records live in **their own folder**, separate from the project. One command creates it, with its own copy of the tools, the manual and the assistant's instructions, so it keeps working even if the project folder is deleted later. In a terminal opened in the project folder, run the line for your system:

```
./fha install ../my-family-archive      # macOS / Linux
.\fha install ..\my-family-archive      # Windows PowerShell
```

That makes a folder called `my-family-archive` next to the project folder. Call it what you like, and put it somewhere you back up. If you already have an archive (someone ran `fha install` for you), skip ahead to [Coming from Ancestry](#coming-from-ancestry-or-another-genealogy-program).

**Point it at your photos and documents.** Open `fha.yaml` inside the new folder with a plain text editor. Examples for a local folder, an external drive and an existing photo library are written as comments in the file, beside the settings they explain. If you're starting with nothing, leave it as it is.

**Did it work?** Open a terminal in your new archive folder and run the check:

```
./fha check      # macOS / Linux
.\fha check      # Windows PowerShell
```

A fresh archive prints **`✓ No issues found.`** (`check` is another name for `fha lint`, which looks for anything shaped the wrong way.)

From here on, commands are written bare (`fha check`) and run from inside your archive folder. The [cheat sheet](CHEATSHEET.md#typing-fha) shows how to type them on each system.

Now open the archive folder in Claude Code or Codex. Do your family work in a session opened there, not in the project folder.

> **Building it by hand instead?** You can copy the project's `archive-template` folder and fill in records with a text editor (see [Doing it by hand](#doing-it-by-hand-no-tools-no-ids)). A copy made that way has no tools inside it. You can run `fha install` on it later as long as you haven't edited anything in it yet.

### Coming from Ancestry or another genealogy program?

You don't have to retype anything. Every genealogy program can export your tree as a **GEDCOM** file (on Ancestry: Trees, then your tree, then Tree Settings, then "Export tree"; a `.ged` file lands in your Downloads). Then ask your assistant:

> "Import my GEDCOM file"

or run `fha gedcom import family-tree.ged` yourself.

First it shows you a **plan** (how many people, families and statements it found) and writes nothing. Running it again with `--apply` (`fha gedcom import family-tree.ged --apply`, or telling the assistant to go ahead) does the import: every person in your tree becomes a record and every statement becomes a *suggested* fact citing the GEDCOM file, which is filed as a source with your original untouched. If your old program linked photos to people, the plan says which are already in your photo folders, and the assistant can put each person's name on their photo, one at a time. Your tree's statements wait in the same review queue as everything else. You review them a family at a time, whenever you like.

### Tell it about your own family first (two minutes)

Before you file anything else, tell the assistant about your immediate family. You are the one person the archive is sure to have firsthand knowledge about.

> "Let's do the setup interview."

It asks who you are, so it can number the family tree correctly, then about your parents, spouse and children: names and roughly when they were born. What you say becomes a citable source and a handful of *suggested* facts, reviewed and accepted the same way as everything else in the archive.

---

## File your first document (five minutes)

This is the whole loop in miniature: a scan goes in, a *suggested* fact comes back, and you accept it. Nothing becomes a fact until you say so.

### Step 1: Drop a scan in the inbox

Find any scan, photo or downloaded record: a birth certificate, an old photo, a screenshot from a genealogy site. Copy the file into the `inbox/` folder inside your archive. Don't rename it.

Each separately published item is its own record. Two different letters, or two newspaper clippings about the same event, go in as two files. The pages, front and back, or extra scans of one item belong together; put them in one folder inside `inbox/` and they become one record.

You can also leave a short note describing the item. Copy `inbox/_TEMPLATE.notes.md`, name the copy after the scan with `.notes.md` on the end (for `rose-wedding.jpg`, call it `rose-wedding.notes.md`), and answer the questions in plain words: "a photo of Grandma Rose's wedding, around 1955, found in a shoebox". For a folder of parts, name the note `notes.md` and put it in the folder. The assistant uses your note as hints. Skipping it is fine.

### Step 2: Ask the assistant to process it

In your assistant, say:

> "Process the new item in my inbox."

It runs `fha process`, which gives the source a permanent ID, files the scan in the right place and creates a record for it. Then it reads the document, works out the names, dates and places, and **drafts each one as a suggested fact**. It shows you what it found.

### Step 3: Review and accept

The assistant shows you each suggested fact next to the words in the document it came from. You decide. Reply in plain English:

> "The birth date and the name are right. I'm not sure about the place; leave that one as a suggestion for now."

The ones you approve are marked **accepted** and stamped with today's date. The rest stay suggestions until evidence or your memory settles them. **Only you can turn a suggestion into a fact**; the assistant cannot do it on its own.

That's one filed record.

**Prefer clicking to typing?** Run `fha serve` (or double-click `serve.cmd` in your archive folder on Windows) to open the same review queue and inbox in a private browser tab on your own computer. Each button runs the same command you'd otherwise ask the assistant for, and shows you what it will change before it writes anything. Close the window and carry on in the assistant where you left off. Its home page also lists the family birthdays and anniversaries falling this week, and each person's page shows how well documented they are and which kind of record would fill each gap.

---

## Doing it by hand (no tools, no IDs)

You don't need the assistant or the tools to add to your archive. Each record folder has a template you can copy and fill in with any text editor: `people/_TEMPLATE.person.md` (one template for every person, from a bare mention to a full profile; it starts as `tier: stub`), `sources/_TEMPLATE.source.md`, `inbox/_TEMPLATE.notes.md`, and the commented `_TEMPLATE` entry at the top of `places/places.yaml`.

- **Name files plainly.** Call a file `grandpas-letter.md` or `hartley-thomas.md`, whatever makes sense to you. You don't need to make an ID.
- **Link by name.** To cite a source or point at a person, write its name in double brackets: `[[Grandpa Joe]]`, `[[Hartley family bible]]`, `born in [[Fairview]]`. A nickname works too.
- **Write down what you only half know.** Fairly sure great-grandma was born around 1849? Write `birth: 1849~` on her record. That's a *provisional* date, and the assistant keeps it on a "still to source" list until the proof turns up.

When you want permanent IDs, ask the assistant, or run `fha lint --fix-ids`. It gives each record an ID and keeps your filename as an alias so your `[[name]]` links keep working. (Plain `fha lint` only checks, and lists the records still waiting for an ID.) IDs last longer than filenames, which change and can repeat, but you never have to create one.

---

## The daily rhythm

A working session follows the same loop: **capture, file and process, review, report.**

- **Capture:** pull a record off a genealogy site into the inbox (the assistant can do this with `fha capture`), or drop in a scan. A browser add-on can also save the page you have open with one click. Your archive carries a copy at `.fha/browser-companion/`: open `chrome://extensions`, turn on Developer mode, click **Load unpacked** and pick that folder. Its README has the details. On Windows `.fha` is hidden, so type the path into the file-name box if the folder isn't listed.
- **File and process:** "process my inbox," as above.
- **Review:** accept or set aside the suggested facts. The assistant records each decision with `fha claim`; you make the decision.
- **Report:** ask "what should I look at today?" The assistant runs `fha report` and reads you the review queue, the gaps to fill and the research leads.

Within a week you'll know the few phrases you use. You never need the command names.

---

## Back it up

Your archive is plain files, so a backup is a copy, and one command makes it. Ask the assistant to "back up my archive," or run:

```
fha backup
```

That writes a dated zip file into a folder **beside** your archive (named `my-family-archive-backups`), checks every file inside it, and tells you where it went. Copy that zip somewhere off this computer: a USB stick, an external drive or a cloud folder.

- **Your photos and documents are not in that zip** unless you add `--include-assets`. They're often large and kept on another drive with its own backup. The command names them each time so nothing is left out silently, and `fha doctor` lists every folder a full backup has to cover (and when you last made one).
- **To restore, unzip the file.**

Make a backup at the end of any session where you added something you'd hate to lose.

---

## Where things live

| Folder | What's in it |
|---|---|
| `inbox/` | New material waiting to be processed: your to-file pile. |
| `sources/` | One record per piece of evidence (a document, a photo, an interview). |
| `people/` | The people in your tree, in numbered family-couple folders. |
| `places/` | The list of places, with their locations. |
| `notes/` | Research in progress and your running list of questions. |
| `fha.yaml` | The one settings file: where your photos and documents live. |
| `START_HERE.md` | One page explaining the archive to anyone who opens the folder with no tools, such as a grandchild years from now. Its family part is yours to write (the setup interview drafts it with you), and the tools never overwrite it. |
| `docs/` | This guide, the cheat sheet, the rest of the manual and the full rules (`SPEC.md`). |
| `.fha/` | The program itself, its site design and the browser add-on. It's hidden so the top of the archive shows your genealogy. You never edit anything in it. |
| `generated/` | Things the tools build: the family website, printable views, galleries. All of it can be rebuilt. |

A clipping or note that names nobody in your family (period news, local history) can still be kept and searched. It just won't appear on anyone's page. The [FAQ](FAQ.md#does-every-photo-or-document-need-to-become-a-source) explains how to file it.

Everything is plain text or standard image files. You can open any of it with Notepad, TextEdit or a photo viewer, now or in fifty years.

**It opens in Obsidian too.** Your archive is Markdown with `[[wikilinks]]`, so pointing Obsidian (or another Markdown app) at the folder opens it as it is. See [`USING_WITH_OBSIDIAN.md`](USING_WITH_OBSIDIAN.md).

Arrange your `documents/` folder in any folders you like: by type, by family line, by decade. An item you put in a folder stays there when it's processed; one dropped loose at the top gets filed into a folder for its type. Your photo library is never renamed or moved. If you move filed items around later, ask the assistant to reconcile and it re-links them; [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#i-reorganized-my-documents-or-photos-folder) has the details. When you're not sure where a stray research note belongs, the "Where does a note go?" list in [`FILING_CABINET.md`](FILING_CABINET.md) answers it.

---

## Your archive and the project are separate

- **The project** is public: the rules (`SPEC.md`, `TOOLING.md`, `AGENTS.md`), the guides and the `fha` tools. The tools hold no family data and work on any archive built to these rules.
- **Your archive** is private: your records, your photos, your `fha.yaml`. Your family data never goes into the public project.

`fha install` copies the tools into your archive, so it works offline on any computer even if the project disappears. That's why `fha` is never installed with `pip`: your archive owns its own copy. (Its Python helpers are ordinary packages; `fha doctor` names any that go missing.)

When a newer version comes out, `fha update-tools` refreshes that copy:

- **Never touched:** your records, `fha.yaml`, your place list and your `custom.css` stylesheet. (The first time, it asks where to keep the Seen It browser add-on and adds your answer as one line at the end of `fha.yaml`.)
- **Replaced:** tool files and the rule documents. If you had edited one, your version is moved into `.plaintext-backup/` first, so your edit is kept but stops being used until you make the change again.
- **Browser add-ons:** the Seen It add-on is refreshed in its folder too, after a backup. Then click its reload arrow in `chrome://extensions`. Don't remove it and load it again, because removing it deletes what it remembers.
- **Never deleted:** anything the update retires is moved aside and reported. Deleting it is up to you.
- **One folder can disappear:** if `fha.yaml` points `documents:`, `photos:` or `inbox:` somewhere outside the archive, the matching empty folder inside the archive is removed, since it would never hold anything. Point the setting back inside and the next `fha update-tools` brings the folder back.

The update steps are in [`UPDATING.md`](UPDATING.md).

---

## The example archive

The project keeps a small, **entirely fictional** family (the Hartleys) as a worked example, so you can see what finished records look like before you have many of your own. There are two ways to look at it, neither of them inside your archive:

- The **[example site](https://plaintext.family/example/)**: that family built by `fha site`, the same self-contained, privacy-filtered site you could publish or give a cousin on a USB stick.
- The files behind it, in the project repo: [example-archive/](https://github.com/asielen/plaintext-family-history/tree/master/example-archive).

None of it is real, so you can't break anything by looking around.

---

## What's next

- [`CHEATSHEET.md`](CHEATSHEET.md), beside this file in `docs/`: one page of the commands and phrases you'll use. Print it and keep it by the keyboard.
- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md): each common problem and its fix.
- [`UPDATING.md`](UPDATING.md): how to take a newer version of the tools. Your records are never part of it.
- [`FILING_CABINET.md`](FILING_CABINET.md): the whole archive explained as the paper filing cabinet you already know.
- [`FAQ.md`](FAQ.md): why files instead of a database, and why a person approves every fact.
- [`GLOSSARY.md`](GLOSSARY.md): every term and ID type.
- [`README.md`](README.md): the index of the whole manual.
- `SPEC.md`, in this same `docs/` folder: the full rules. You never have to read it; the assistant follows it for you.
