# Plaintext Family History - Documentation Index

> Lost? Start at the [main README](../README.md) - the "Which one are you?" table there points to the right door.

---

## For genealogists and archive users

| Document | Who it's for |
|---|---|
| [GETTING_STARTED.md](GETTING_STARTED.md) | Your first day: install Python/exiftool/the AI assistant, make your archive, file your first document |
| [SETUP_FROM_ZIP.md](SETUP_FROM_ZIP.md) | The git-free path - you got a zip, no GitHub account, set it up from a folder |
| [CHEATSHEET.md](CHEATSHEET.md) | One printable page: the daily loop, the few commands, how to write an uncertain date, where things live |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Something went wrong - symptom → cause → exact fix for the edge cases |
| [FILING_CABINET.md](FILING_CABINET.md) | The system explained as the paper filing cabinet you already know |
| [CONTRIBUTING_SOURCES.md](CONTRIBUTING_SOURCES.md) | Someone the owner sent documents to - how to hand them over |
| [GLOSSARY.md](GLOSSARY.md) | Every term, ID type, and record type defined |
| [FAQ.md](FAQ.md) | Why files? Why not a database? Why AI? How durable is this really? |

## For developers

Design is split by concern; each design doc has a matching build doc.

| Document | Who it's for |
|---|---|
| [../BUILD.md](../BUILD.md) | Build sequence for the core `fha` CLI - start here before touching code |
| [../TOOLING.md](../TOOLING.md) | Deep implementation design for the core tools - enough to rebuild from scratch |
| [../BUILD_INGESTION.md](../BUILD_INGESTION.md) / [../TOOLING_INGESTION.md](../TOOLING_INGESTION.md) | The capture / inbox / web on-ramp: build sequence + design |
| [../BUILD_INTERFACE.md](../BUILD_INTERFACE.md) / [../TOOLING_INTERFACE.md](../TOOLING_INTERFACE.md) | The workbench harness + workflow skills (the AI interface): build sequence + design |
| [../tools/README.md](../tools/README.md) | Per-tool implementation status tables (flags, error codes, test coverage) |

## Spec and governance

| Document | Who it's for |
|---|---|
| [../SPEC.md](../SPEC.md) | The law: data model, physical format, what every tool must do |
| [../AGENTS.md](../AGENTS.md) | What an AI agent may and may not do inside the archive |
| [../AGENTS_TOOLING.md](../AGENTS_TOOLING.md) | Supplementary rules for tool-building and code-review modes |
| [../PRIVACY.md](../PRIVACY.md) | What real personal data is never allowed in this public repo |
| [../RELEASE_CHECKLIST.md](../RELEASE_CHECKLIST.md) | Pre-release verification checklist |
