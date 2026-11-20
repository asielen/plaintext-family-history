# Plaintext Family History

A family-history archive made of plain files you can open in any text editor, with an AI research assistant working on top of them.

> **[See what it can do: open the example site →](https://plaintext.family/example/)** A fictional family's archive (the [`example-archive/`](example-archive/)) built into a website by `fha site`, the same self-contained site you could publish or hand to a relative on a USB stick.

![spec](https://img.shields.io/badge/spec-v2.15-blue) ![works with](https://img.shields.io/badge/works_with-Claude_Code_%7C_Codex_%7C_Human-8A2BE2) ![format](https://img.shields.io/badge/format-plain_text-green) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

For a hundred years genealogy lived in a filing cabinet, and anyone could open the drawer. No login, no subscription, no file format that stopped opening. Your great-grandchildren could pull the folder and read it. Modern genealogy software and websites have lost that.

Plaintext is that filing cabinet, built to last for generations. Your records are ordinary files in formats that will still open decades from now: Markdown text, CSV and YAML, JPEG and TIFF images with their captions written inside them. Every fact traces back to the source that states it, and nothing the AI suggests counts until you accept it. Search, family trees, a website and the assistant all sit on top of those files and are rebuilt from them. Delete every one of those layers and the archive still reads.

## Start your archive

You need three things on your computer:

- **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/). On Windows, tick "Add Python to PATH" in the installer.
- **This repo**, either cloned with git or downloaded as a zip (green "Code" button, then "Download ZIP"). If you take the zip, [`docs/SETUP_FROM_ZIP.md`](docs/SETUP_FROM_ZIP.md) covers the few differences.
- **An AI coding assistant**, which runs the archive with you: [Claude Code](https://www.anthropic.com/claude-code) or OpenAI's [Codex](https://developers.openai.com/codex). Both read the project's rules and skills on their own. Any assistant that reads `AGENTS.md` and can run commands will work; the docs assume Claude Code.

### Let the assistant set it up

Open this repo in Claude Code or Codex and ask:

> 💬 *"Get me started with my own family archive."*

It checks Python and the helper packages and creates your archive with `fha install` in a folder outside this repo. Then it tells you to open that new folder for the setup interview. From then on, do your family work in a session opened in your archive, not in this repo. The archive carries its own copy of the rules, its own tools and its own skills (processing an inbox, reviewing claims, finding photos, writing a biography), so it keeps working even if this repo disappears.

### Step by step

In a terminal (the Command Prompt on Windows, the Terminal app on a Mac) opened in this repo's folder:

1. **Install the helper packages** (one time):

   ```
   python -m pip install -r tools/requirements.txt
   ```

2. **Make your archive** from the template. Put it somewhere outside this repo, in a place that is easy to back up.

   ```
   ./fha install ../my-family-archive      # macOS / Linux
   .\fha install ..\my-family-archive      # Windows PowerShell
   fha install ..\my-family-archive        # Windows Command Prompt
   ```

   That creates the folder and copies in everything the archive needs to run on its own: the `fha` tools, the rules the assistant follows and the owner's manual in `docs/`.

3. **Tell it where your photos and scans live.** Open `fha.yaml` in the new folder with a text editor. The examples are commented right in the file (a local folder, an external drive, an existing photo library). If you are starting from nothing, leave the defaults.

4. **Open the new folder in Claude Code or Codex** and say:

   > 💬 *"Let's do the setup interview."*

   It asks who you are and about your parents, spouse and children. Your answers become your first source. If you agree, it also fills in the "This family" part of `START_HERE.md`. Everything it drafts waits for your review; nothing is accepted on your behalf.

5. **File your first document.** Drop a scan or photo into `inbox/` and ask:

   > 💬 *"Process the inbox."*

   It files the scan as a source record and drafts facts from it for you to accept or reject.

Coming from Ancestry or another genealogy program? Export your tree as a GEDCOM file and ask the assistant to import it. Every person comes in as a record and every statement in your tree as a suggestion waiting for review, with your original file kept untouched.

The full first-day walkthrough, with a "did it work?" check after every step, is [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md). The one-page reference is [`docs/CHEATSHEET.md`](docs/CHEATSHEET.md).

### With no tools at all

If none of these tools run any more when you read this, the format and the tools are both written down so they can be rebuilt. [`SPEC.md`](SPEC.md) describes every file, and [`TOOLING.md`](TOOLING.md) describes every tool in enough detail to write it again from scratch in whatever language people use by then. Until then, or if you never want the tools, [`quickstart-template/`](quickstart-template/) (or [the zip](downloads/quickstart-template.zip)) is a blank archive you fill in by hand with any text editor, and [`quickstart-example/`](quickstart-example/) is a filled-in fictional one. Both use the same format, so tools can take them over later.

## How the system works

Everything in this project depends on the record format.

**A fact is something a source says, and it lives in that source.** A source is a plain text file paired with the evidence itself, such as a scan or a PDF. A death certificate's image sits in your archive's `documents/` folder, and beside it in `sources/` is a text file that describes it and lists the claims it makes (who died, when, where, who reported it). So every claim in the archive traces back to the source it came from. Everything else, from people's profiles to family trees to the website, is built up from those sources and claims.

**Nothing is a fact until a person says so.** Claims start as `suggested`, whether the AI drafted them or you typed them. They move to `accepted` only when you review them. The AI can do the filing and the drafting, but it can't decide what is true about your family.

> 💬 *"Let's review the claims you drafted."*

There are five kinds of record, each a plain Markdown file or a section of one:

| Record | What it is |
|---|---|
| **Source** `s-` | The most important record in the archive. One piece of evidence (a certificate, a photo, a letter, an interview) with its files and the claims it makes. |
| **Claim** `c-` | One statement a source makes, such as a date, a place or a relationship. Claims live inside their source's file and are either suggested or accepted. |
| **Person** `p-` | One human. Their names, story and research notes live here, along with who they are related to. Every fact on the page links back to a source. |
| **Place** `l-` | A location with coordinates and its names over time, since the town your ancestor lived in may have changed county or country. |
| **Working answer** `h-` | A hypothesis: your best guess at an open question, written in `notes/questions.md` with its basis and what would prove it. No source backs it yet, so it never counts as a fact, but it tells you where to look next. |

Records link to each other with `[[wikilinks]]`, the double-bracket links that note-taking editors such as Obsidian and Foam use, and those links make up your research web. Every record has a short ID in its file name or its fields, so a link like `[[p-de957bcda1|Thomas Hartley]]` keeps working when a file is renamed or moved. You can also link by name (`[[Grandpa Joe]]`) and the tools fill in the ID later. [`docs/USING_WITH_OBSIDIAN.md`](docs/USING_WITH_OBSIDIAN.md) covers opening the archive as an Obsidian vault.

From that web of sources and claims come the views genealogists are used to: profiles, family trees, timelines, a website. Each one is rebuilt from the records the same way every time, so none of them can drift from the evidence.

A search index keeps the archive quick to work with. It is rebuilt whenever records change and powers search, family trees, the website and a daily research report that flags contradictions and possible connections. The index is a cache rather than a record: delete it and `fha index` makes a new one.

> 💬 *"What should I work on today?"*

[`SPEC.md`](SPEC.md) is the full specification. [`docs/DESIGN_RATIONALE.md`](docs/DESIGN_RATIONALE.md) explains why each part is shaped the way it is and what was considered and rejected.

## Getting into the weeds

### Your archive and this repo

Once everything is set up you have two separate folders, and usually two git repositories:

```
plaintext-family-history/   PUBLIC:  the spec, the tools, the templates (this repo)
my-family-archive/          PRIVATE: your family's records
```

This repo is not your archive. It holds the tools. When new features come out you update it (`git pull`, or download the zip again) and then bring them into your archive from inside it:

> 💬 *"Update the tools from my copy of the repo."*

That runs `fha update-tools`. An update replaces tool files, the rules and the manual. It never touches your records, `fha.yaml`, your place registry or your stylesheet. If you edited a tool file, your version is moved to `.plaintext-backup/` before the new one replaces it.

Keeping the two apart means you can back up your archive as its own private GitHub repository and still pick up every new version of the tools.

The archive's tools live in a hidden `.fha/` folder, so the top of your archive shows your genealogy: your records, `START_HERE.md` (the page that explains the archive to anyone who opens the folder), `fha.yaml`, the assistant's rules and `docs/`. An installed archive has no copy of this README. Its front page is `START_HERE.md`, and its owner's manual starts at `docs/GETTING_STARTED.md`.

### Ways to work with an archive

Every record is Markdown, so any text editor will open it. These are the other ways in.

**Obsidian.** The archive is a Markdown vault with `[[wikilinks]]`. Point Obsidian at the folder and it opens as is. An optional [Templater pack](obsidian-templater/) adds record templates; see [`docs/USING_WITH_OBSIDIAN.md`](docs/USING_WITH_OBSIDIAN.md).

**In a browser.** `fha serve` starts a private workbench on your own computer (no login, nothing online) with a review queue, the inbox and every record a click away. Each button runs the same `fha` command you could type, and shows you what it will change before it writes.

**A family website.** `fha site` builds a self-contained website from the archive. Living people are kept out of the shared version, along with anything you mark private.

**Backups.** `fha backup` writes a dated zip into a folder beside the archive and checks every file in it. Photos and documents are left out by default because they are often large and kept on another drive; add `--include-assets` to include them, and `fha doctor` lists every folder a full backup has to cover. To restore, unzip the file.

> 💬 *"Back everything up."*

### Browser add-ons

Two optional add-ons for Chrome and Edge help while you research on genealogy websites.

**The capture add-on** saves the record page you have open (on Ancestry, FamilySearch or another site) into your archive's inbox, with its images and what the site says about the record, ready to file. It ships inside every archive, in `.fha/browser-companion/`; [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) shows how to load it.

**Seen It** ([`seen-it/`](seen-it/)) remembers which Newspapers.com pages, Ancestry and FamilySearch records and Find a Grave memorials you have already opened, ruled out or attached. It marks them on the sites and works on its own, with no archive needed. Turn on Advanced mode and it also shows what your archive already holds, without ever changing the archive.

### Repository layout

```
plaintext-family-history/
├── README.md            you are here
├── SPEC.md              the specification: the data model, the file format, the rules
├── TOOLING.md           how the fha tools are built, and in what order (+ TOOLING_INGESTION.md, TOOLING_INTERFACE.md)
├── AGENTS.md            the rules the AI assistant follows (Codex reads it; CLAUDE.md points Claude Code here)
├── .claude/skills/      the assistant's workflow skills (archives also get them in .agents/skills/ for Codex)
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
| [TOOLING.md](TOOLING.md) | How the tools work, in enough detail to rewrite them from scratch in another language. Its two siblings, [TOOLING_INGESTION.md](TOOLING_INGESTION.md) (capturing and filing) and [TOOLING_INTERFACE.md](TOOLING_INTERFACE.md) (the workbench and the assistant's skills), cover the rest. §15 is the build order. |
| [docs/DESIGN_RATIONALE.md](docs/DESIGN_RATIONALE.md) | Why the archive is shaped this way and what was rejected. Read it before proposing a rule change. |

### Contributing

Feedback is welcome, especially from genealogists and from anyone building tools against the spec. See [`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md). Keep examples fictional: no real records about living people, private family documents, DNA files or identifying photos in issues or pull requests ([PRIVACY.md](PRIVACY.md)).

### License

[MIT](LICENSE). The spec, the documents and any code built from them are free to use and build on.
