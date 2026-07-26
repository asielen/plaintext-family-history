# Plaintext Family History - Documentation Index

> Lost? Start at the [main README](../README.md) - the "Which one are you?" table there points to the right door.

---

## For genealogists and archive users

| Document | Who it's for |
|---|---|
| [GETTING_STARTED.md](GETTING_STARTED.md) | Your first day: install Python/exiftool/the AI assistant, make your archive, file your first document |
| [SETUP_FROM_ZIP.md](SETUP_FROM_ZIP.md) | The git-free path - you got a zip, no GitHub account, set it up from a folder |
| [UPDATING.md](UPDATING.md) | A newer version came out - the two-minute update ritual, and the one rule that prevents lost work |
| [CHEATSHEET.md](CHEATSHEET.md) | One printable page: the daily loop, the few commands, how to write an uncertain date, where things live |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Something went wrong - symptom → cause → exact fix for the edge cases |
| [FILING_CABINET.md](FILING_CABINET.md) | The system explained as the paper filing cabinet you already know |
| [CUSTOMIZING_SITE.md](CUSTOMIZING_SITE.md) | Making the generated site your own - name, homepage welcome, hero, styling - by editing source, not the HTML |
| [CONTRIBUTING_SOURCES.md](CONTRIBUTING_SOURCES.md) | Someone the owner sent documents to - how to hand them over |
| [GLOSSARY.md](GLOSSARY.md) | Every term, ID type, and record type defined |
| [FAQ.md](FAQ.md) | Why files? Why not a database? Why AI? How durable is this really? |

## For developers

The full build sequence (`BUILD*.md`) and per-layer implementation design
(`TOOLING*.md`, `AGENTS_TOOLING.md`) live in the project repo on GitHub
([plaintext-family-history](https://github.com/asielen/plaintext-family-history))
and are **not** shipped into an archive - extending the tools is a workshop-clone
activity. Two design references *do* ship, because they govern what the archive renders:

| Document | Who it's for |
|---|---|
| [DESIGN.md](DESIGN.md) | The visual language for everything the archive renders as HTML - tokens, typography, components |
| [SITE_PLAN.md](SITE_PLAN.md) | Roadmap for homepage / navigation / customization: the source-first model, the customization layers, and the build phases |
| [tools/README.md](https://github.com/asielen/plaintext-family-history/blob/master/tools/README.md) | Per-tool implementation status tables (flags, error codes, test coverage) |

*(Linked to GitHub rather than by relative path: an installed archive keeps the tools
themselves in a hidden `.fha/tools/` folder, so `../tools/…` would not resolve there.)*

## Spec and governance

| Document | Who it's for |
|---|---|
| [../SPEC.md](../SPEC.md) | The law: data model, physical format, what every tool must do |
| [../AGENTS.md](../AGENTS.md) | What an AI agent may and may not do inside the archive |

The public-repo governance docs (privacy policy, release checklist, and the
tool-building / code-review supplements) live on
[GitHub](https://github.com/asielen/plaintext-family-history).
