# Plaintext Family History

A family-history archive made of plain files you can open in any text editor, with an AI research assistant working on top of them.

> **[See what it can do: open the example site →](https://plaintext.family/example/)** A fictional family's archive (the [`example-archive/`](example-archive/)) built into a website by `fha site`, the same self-contained site you could publish or hand to a relative on a USB stick.

![status](https://img.shields.io/badge/status-milestones_1--11_complete-green) ![spec](https://img.shields.io/badge/spec-v2.3-blue) ![works with](https://img.shields.io/badge/works_with-Claude_Code-8A2BE2) ![format](https://img.shields.io/badge/format-plain_text-green) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

For a hundred years genealogy lived in a filing cabinet, and anyone could open the drawer. No login, no subscription, no file format that stopped opening. A great-grandchild could pull the folder and read it. Most genealogy software has lost that.

Plaintext is that filing cabinet, built to last. Your records are ordinary files. Search, family trees, a website and an AI assistant all sit on top of those files and are rebuilt from them. Delete every one of those layers and the archive still reads, the way the drawer still opens.

## Start your archive

You need three things on your computer:

- **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/). On Windows, tick "Add Python to PATH" in the installer.
- **This repo**, either cloned with git or downloaded as a zip (green "Code" button, then "Download ZIP"). If you take the zip, [`docs/SETUP_FROM_ZIP.md`](docs/SETUP_FROM_ZIP.md) covers the few differences.
- **[Claude Code](https://www.anthropic.com/claude-code)**, the AI assistant the archive is run through. Any assistant that reads `AGENTS.md` and can run commands will work; the docs assume Claude Code.

Then, in a terminal opened in this repo's folder:

1. **Install the helper packages** (one time):

   ```
   python -m pip install -r tools/requirements.txt
   ```

2. **Make your archive** from the template. Put it somewhere outside this repo; your family's records never go in here.

   ```
   ./fha install ../my-family-archive      # macOS / Linux
   .\fha install ..\my-family-archive      # Windows PowerShell
   fha install ..\my-family-archive        # Windows Command Prompt
   ```

   That creates the folder and copies in everything the archive needs to run on its own: the `fha` tools, the rules the assistant follows and the owner's manual in `docs/`.

3. **Tell it where your photos and scans live.** Open `fha.yaml` in the new folder with a text editor. The examples are commented right in the file (a local folder, an external drive, an existing photo library). If you are starting from nothing, leave the defaults.

4. **Open the new folder in Claude Code** and say:

   > Let's do the setup interview.

   It asks who you are and about your parents, spouse and children. Your answers become your first source. If you agree, it also fills in the "This family" part of `START_HERE.md`. Everything it drafts waits for your review; nothing is accepted on your behalf.

5. **File your first document.** Drop a scan or photo into `inbox/` and ask the assistant to process it. It files the scan as a source record and drafts facts from it for you to accept or reject.

Coming from Ancestry or another genealogy program? Export your tree as a GEDCOM file and ask the assistant to import it. Every person comes in as a record and every statement in your tree as a suggestion waiting for review, with your original file kept untouched.

The full first-day walkthrough, with a "did it work?" check after every step, is [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md). The one-page reference is [`docs/CHEATSHEET.md`](docs/CHEATSHEET.md).

No tools at all? [`quickstart-template/`](quickstart-template/) (or [the zip](downloads/quickstart-template.zip)) is a blank archive you fill in by hand with any text editor, and [`quickstart-example/`](quickstart-example/) is a filled-in fictional one. It uses the same format, so the tools can take it over later.

## How the records work

Everything else in this project depends on the record format.

**A fact is something a source says, and it lives in that source.** A death certificate is one file. The facts it states (who died, when, where, who reported it) are written inside that file as claims. So every fact in the archive can be traced back to the page it came from, and two sources that disagree simply sit side by side until someone works out which is right. A profile does not hold facts of its own. It shows what the sources say about that person.

**Nothing is a fact until a person says so.** Claims start as `suggested`, whether the AI drafted them or you typed them. They move to `accepted` only when you review them. The AI can do the filing and the drafting. It can't decide what is true about your family.

**Every claim says whose event it is.** A death record names the person who died and also the widow, the children and the informant. A claim that names all of them without saying who is who is how a son's death ends up on his father, or a mother's obituary on her living son. So an event claim gives every person on it a role, such as `deceased` or `informant`, and the tools refuse to accept one that doesn't.

There are five kinds of record, each a plain Markdown file with a short block of structured fields at the top:

| Record | What it is |
|---|---|
| **Person** `p-` | One human. Their names, story and research notes live here, along with who they are related to. |
| **Source** `s-` | One piece of evidence (a certificate, a photo, a letter, an interview) with its files and the claims it makes. |
| **Claim** `c-` | One statement a source makes, such as a date, a place or a relationship. It lives inside its source and is either suggested or accepted. |
| **Place** `l-` | A location with coordinates and its names over time, since the town your ancestor lived in may have changed county or country. |
| **Working answer** `h-` | A guess about an open question, written in `notes/questions.md` with its basis and what would prove it. It never counts as a fact. |

Every record has a short ID in its file name (`hartley__thomas_edward_p-de957bcda1.md`), so a link like `[[p-de957bcda1|Thomas Hartley]]` keeps working when a file is renamed or moved. You can also link by name (`[[Grandpa Joe]]`) and the tools fill in the ID later.

Around the records sits a search index, rebuilt from the files whenever they change. It powers search, family trees, the website and a daily research report that flags contradictions and possible connections. The index is a cache. Delete it and `fha index` makes a new one.

Two more rules keep the archive honest over time. Prose without a citation is story or context, never fact. And anything the tools generate (summary lines, timelines, source lists, the website) is rebuilt from the records rather than edited by hand, so it can never drift from them.

[`SPEC.md`](SPEC.md) is the full specification. [`docs/DESIGN_RATIONALE.md`](docs/DESIGN_RATIONALE.md) explains why each part is shaped the way it is and what was considered and rejected.

## Quick start with Claude Code

If you would rather not follow the steps yourself, open this repo in Claude Code and ask:

> Get me started with my own family archive.

It checks Python and the helper packages and creates your archive with `fha install` in a folder outside this repo. Then it tells you to open that new folder for the setup interview. From then on, do your family work in a Claude Code session opened in your archive, not in this repo. The archive carries its own copy of the rules, its own tools and its own skills (processing an inbox, reviewing claims, finding photos, writing a biography), so it keeps working even if this repo disappears.

## More detail

### Your archive and this repo

You end up with two separate folders, and usually two git repositories:

```
plaintext-family-history/   PUBLIC:  the spec, the tools, the templates (this repo)
my-family-archive/          PRIVATE: your family's records
```

They are not linked. `fha install` copies the tools into your archive once, and `fha update-tools` (run from inside the archive, pointing at a newer copy of this repo) refreshes them later. An update replaces tool files, the rules and the manual. It never touches your records, `fha.yaml`, your place registry or your stylesheet. If you edited a tool file, your version is moved to `.plaintext-backup/` before the new one replaces it.

The tools live in a hidden `.fha/` folder, so the top of your archive shows your genealogy: your records, `START_HERE.md` (the page that explains the archive to anyone who opens the folder), `fha.yaml`, the assistant's rules and `docs/`. An installed archive has no copy of this README. Its front page is `START_HERE.md`, and its owner's manual starts at `docs/GETTING_STARTED.md`.

The `fha` program is never `pip install`ed. Each archive owns its own copy, so nothing can uninstall it from under you. `fha doctor` names any helper package that is missing.

> **Examples stay fictional.** Do not open issues or pull requests containing real records about living people, private family documents, DNA files or identifying photos. See [PRIVACY.md](PRIVACY.md).

### Ways to work with an archive

Every record is Markdown, so any text editor will open it. These are the other ways in.

**Obsidian.** The archive is a Markdown vault with `[[wikilinks]]`. Point Obsidian at the folder and it opens as is. An optional [Templater pack](obsidian-templater/) adds record templates; see [`docs/USING_WITH_OBSIDIAN.md`](docs/USING_WITH_OBSIDIAN.md).

**In a browser.** `fha serve` starts a private workbench on your own computer (no login, nothing online) with a review queue, the inbox and every record a click away. Each button runs the same `fha` command you could type, and shows you what it will change before it writes.

**A family website.** `fha site` builds a self-contained website from the archive. Living people are kept out of the shared version, along with anything you mark private.

**The Seen It browser add-on.** [Seen It](seen-it/) remembers which Newspapers.com pages, Ancestry and FamilySearch records and Find a Grave memorials you have already opened, ruled out or attached. It marks them on the sites and works on its own; turn on Advanced mode and it also shows what your archive already holds, without ever changing the archive.

**Backups.** `fha backup` writes a dated zip into a folder beside the archive and checks every file in it. Photos and documents are left out by default because they are often large and kept on another drive; add `--include-assets` to include them, and `fha doctor` lists every folder a full backup has to cover. To restore, unzip the file.

### Repository layout

```
plaintext-family-history/
├── README.md            you are here
├── SPEC.md              the specification: the data model, the file format, the rules
├── TOOLING.md           how the fha tools are built (+ TOOLING_INGESTION.md, TOOLING_INTERFACE.md)
├── BUILD.md             the build order for the tools (+ BUILD_INGESTION.md, BUILD_INTERFACE.md)
├── AGENTS.md            the rules the AI assistant follows (CLAUDE.md points Claude Code here)
├── docs/                the owner's manual, copied into every archive
│   ├── GETTING_STARTED.md   the first-day guide
│   ├── CHEATSHEET.md        one printable page
│   ├── COMMANDS.md          every fha command and its options
│   ├── TROUBLESHOOTING.md
│   ├── SETUP_FROM_ZIP.md
│   ├── GLOSSARY.md
│   ├── FAQ.md
│   └── DESIGN_RATIONALE.md
├── archive-template/    the starting archive fha install copies
├── example-archive/     a small fictional archive, also the test fixture and the example site
├── quickstart-template/ a blank archive to fill in by hand, no tools needed
├── quickstart-example/  a filled-in fictional quickstart archive (the Rivera/Lee family)
├── downloads/           zips of the example and quickstart archives
├── tools/               the fha command suite (see tools/README.md)
├── browser-companion/   the capture add-on for saving web records (ships into every archive)
├── seen-it/             the optional Seen It add-on
├── obsidian-templater/  optional Obsidian templates
├── tests/               tests and fixtures for the tools
└── PRIVACY.md           the example-data policy
```

### The documents

| Document | Read it for |
|---|---|
| [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) | Your first day, step by step. |
| [docs/FAQ.md](docs/FAQ.md) | Why files, why not a database, why AI, how long this will really last. |
| [docs/GLOSSARY.md](docs/GLOSSARY.md) | Every term and ID type. |
| [SPEC.md](SPEC.md) | The complete specification: what exists, how it is written on disk and the rules it follows. |
| [AGENTS.md](AGENTS.md) | What the AI assistant may and may not do in an archive. |
| [TOOLING.md](TOOLING.md) | How the tools work, in enough detail to rewrite them from scratch in another language. Its two siblings, [TOOLING_INGESTION.md](TOOLING_INGESTION.md) (capturing and filing) and [TOOLING_INTERFACE.md](TOOLING_INTERFACE.md) (the workbench and the assistant's skills), each have a matching build doc. |
| [docs/DESIGN_RATIONALE.md](docs/DESIGN_RATIONALE.md) | Why the archive is shaped this way and what was rejected. Read it before proposing a rule change. |

### Design principles

1. The archive is the source of truth; tools can be replaced.
2. Plain, durable formats: `.md`, `.txt`, `.csv`, `.jsonl`, `.yaml`, `.jpg`, `.tiff` and metadata embedded in the images themselves.
3. Every important fact traces to a source. Uncited prose is story or context.
4. AI suggestions are not facts. They wait for a person to accept them.
5. Nothing generated is load-bearing. The index, the trees and the website can all be rebuilt from the files.
6. Folders are for browsing; the records' own fields carry the meaning.
7. Stay light. Long-term durability beats short-term convenience.

### Status

The current format is `spec v2.3`, and milestones 1 to 11 are complete. Everything runs cleanly against the example archive. The tools cover the whole loop: capturing web records and filing scans (`fha capture`, `fha process`), importing a GEDCOM, reviewing claims (`fha claim`, `fha confirm`), the search index and linter (`fha index`, `fha lint`), finding contradictions and possible connections (`fha xref`, `fha cooccur`, `fha report`), timelines and family trees (`fha views`), a photo catalog (`fha photoindex`), the website (`fha site`), the local workbench (`fha serve`), exports (`fha packet`, `fha gedcom`, `fha wikitree`) and backups, plus installing and updating archives. [`docs/COMMANDS.md`](docs/COMMANDS.md) lists every command.

This is a specification and a working scaffold rather than a finished app. The tools are meant to be regenerable from the documents, in any language, if they are ever lost.

### A related project

If your interest is the research half (autonomous AI research loops, archive guides for specific countries, prompts for pushing a tree further back), see [autoresearch-genealogy](https://github.com/mattprusak/autoresearch-genealogy) by Matt Prusak. It arrived independently at the same files-first, Claude Code approach from a different angle: that project is a research playbook, and this one is the filing system the findings live in.

### Contributing

Feedback is welcome, especially from genealogists and from anyone building tools against the spec. See [`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md).

### License

[MIT](LICENSE). The spec, the documents and any code built from them are free to use and build on.
