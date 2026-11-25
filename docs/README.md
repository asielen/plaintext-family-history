# Plaintext Family History: the manual

> New here? Start with [`GETTING_STARTED.md`](GETTING_STARTED.md) and [`CHEATSHEET.md`](CHEATSHEET.md), both in this folder; they're the two you need on day one. The rest of this folder is for when you have a specific question. (Someone opening the archive with no tools at all should start one folder up, at `START_HERE.md`.)

---

## For your first day

| Document | Read it for |
|---|---|
| [GETTING_STARTED.md](GETTING_STARTED.md) | Your first day: install Python, exiftool and the AI assistant, make your archive, file your first document. |
| [CHEATSHEET.md](CHEATSHEET.md) | One printable page: the daily loop, the commands, how to write an uncertain date, where things live. |

## For genealogists and archive owners

| Document | Read it for |
|---|---|
| [SETUP_FROM_ZIP.md](SETUP_FROM_ZIP.md) | Setting up from a zip download, with no GitHub account and no git. |
| [UPDATING.md](UPDATING.md) | Taking a newer version of the tools into your archive. |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Something went wrong: each common problem, its cause and its fix. |
| [FILING_CABINET.md](FILING_CABINET.md) | The archive explained as the paper filing cabinet you already know. |
| [USING_WITH_OBSIDIAN.md](USING_WITH_OBSIDIAN.md) | Opening and working in the archive with the Obsidian note-taking app. |
| [CUSTOMIZING_SITE.md](CUSTOMIZING_SITE.md) | Making the family website your own: its name, homepage welcome, banner and styling, the optional pages you can switch on, and what the site shows with no setting. |
| [CONTRIBUTING_SOURCES.md](CONTRIBUTING_SOURCES.md) | For a relative you've asked for documents: how to send them. |
| [GLOSSARY.md](GLOSSARY.md) | Every term, ID type and record type, defined. |
| [FAQ.md](FAQ.md) | Why files, why not a database, why AI, and how long it lasts. |
| [COMMANDS.md](COMMANDS.md) | Every `fha` command and its options. |
| [DESIGN_RATIONALE.md](DESIGN_RATIONALE.md) | Why the archive is shaped the way it is, and what was considered and rejected. |

## The rules

| Document | Read it for |
|---|---|
| `SPEC.md` (in this folder) | The full rules: the kinds of record, how they're written on disk, and what every tool must do. |
| `TOOLING.md` (in this folder) | How each `fha` tool works. |
| [../AGENTS.md](../AGENTS.md) | What the AI assistant may and may not do inside the archive. |

## For developers

The build order (TOOLING.md §15), the detailed design of each part of the tools (the other `TOOLING*.md` files and `AGENTS_TOOLING.md`), the privacy policy and the release checklist are in the project repo on GitHub ([plaintext-family-history](https://github.com/asielen/plaintext-family-history)). They aren't copied into an archive, because changing the tools happens in your copy of the project, not in the archive.

| Document | Read it for |
|---|---|
| [DESIGN.md](https://github.com/asielen/plaintext-family-history/blob/master/docs/DESIGN.md) | The visual design of everything the archive shows as a web page: colours, type, components. (A copy ships in the archive's hidden `.fha/docs/` folder.) |
| [tools/README.md](https://github.com/asielen/plaintext-family-history/blob/master/tools/README.md) | What each tool supports: flags, error codes, test coverage. |

*(These link to GitHub because an installed archive keeps the tools in a hidden `.fha/` folder, so a relative link like `../tools/…` wouldn't work there.)*
