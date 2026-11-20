# tools/

The `fha` command suite lives here. Run via `python tools/fha.py <command>` from the repo root, or `python tools/<tool>.py` for standalone use.

These tools are **generic**: they operate on any spec-conforming archive and contain no family data. They are meant to be replaceable: disposable, regenerable from the spec, and safe to publish. `TOOLING.md` (repo root; `docs/TOOLING.md` in an installed archive) is the design document for every tool; consult it before changing any behavior.

Two optional **companions** live beside this suite, and nothing in it depends on them. [`browser-companion/`](../browser-companion/) is the web capture extension. It is developed here and, having no build step, is copied ready to load into every archive at `.fha/browser-companion/` by `fha install` and `fha update-tools`. [`obsidian-templater/`](https://github.com/asielen/plaintext-family-history/tree/master/obsidian-templater) holds optional Obsidian Templater templates for new person and source notes (see [docs/USING_WITH_OBSIDIAN.md](https://github.com/asielen/plaintext-family-history/blob/master/docs/USING_WITH_OBSIDIAN.md)). It is installed into a user's own Obsidian setup and never copied into an archive. Both of its links are absolute because this file also lands in an archive at `.fha/tools/`, where the pack does not exist and `../docs/…` would point at the wrong place.

CLI recovery contract: `fha` prints help on no args; unknown subcommands get a "did you mean?" hint; ordinary failures explain the cause and the next command to try; tracebacks are hidden by default and shown only with the global `--debug` flag for tool-building diagnostics. Shared messages cover missing PyYAML, malformed `fha.yaml`, missing archive roots, missing exiftool, bad `source_type` values, and bad archive-date/EDTF values.

`Result` contract: every command splits its engine from its interface. The engine (`run_*`) computes and returns a `_lib.Result`, a small JSON-serializable record carrying `ok`, `exit_code`, `data` (the structured payload), `messages` (human-facing `Message{level, text, next_step, code, path}` lines), and `changed` (paths created/written/renamed/embedded, empty under `--dry-run`). The interface (`_cmd_*`) is the only layer that renders a `Result` to stdout/stderr and returns the exit code. `lint` is the reference implementation. The split lets a terminal, an agent shelling out, the in-process report orchestrator or `fha serve` drive the same engine and read the same structured result (TOOLING §1).

## Where to look

- **Every command and flag:** `docs/COMMANDS.md`, generated from the commands themselves (`py -3.14 build_commands_doc.py` in the repo after changing a flag), and `fha <command> --help`.
- **How each tool works:** `TOOLING.md` (core), `TOOLING_INGESTION.md` (inbox and capture), `TOOLING_INTERFACE.md` (`fha serve` and the skills).
- **The order to build them in, with the tests that show each layer is done:** `TOOLING.md` §15.
- **The rules every tool enforces:** `SPEC.md`.

## Modules

`fha.py` is a thin dispatcher: each module below defines `register(subs)` and is also runnable on its own (`python tools/<module>.py --help`). Shared code lives only in `_lib.py`.

| Module | What it is |
|---|---|
| `_lib.py` | shared library for all fha tools |
| `anniversaries.py` | fha calendar: family births, marriages, deaths and arrivals by month and day (TOOLING §7b) |
| `backup.py` | fha backup: a dated zip snapshot of the archive |
| `capture.py` | fha capture: bring web records into the inbox (TOOLING §13b) |
| `claim.py` | fha claim: human-directed claim review + minting (AGENTS.md, SPEC §8) |
| `confirm.py` | fha confirm: human-directed write-back for detection candidates |
| `convert_mining.py` | fha convert-mining: convert an older transcript-mining output into sources and claims |
| `cooccur.py` | fha cooccur: connection candidate detection over the claim index |
| `doctor.py` | fha doctor: archive health check |
| `fha.py` | the `fha` dispatcher |
| `find.py` | fha find: the universal locator |
| `gedcom.py` | fha gedcom: derive a GEDCOM 5.5.1 exchange file (TOOLING §13a) |
| `gedcom_import.py` | fha gedcom import: import a family tree file such as an Ancestry export (TOOLING §13a2) |
| `id.py` | fha id: mint and check archive IDs |
| `index.py` | fha index: build the SQLite query surface |
| `lint.py` | fha lint: verify the archive against the spec |
| `media.py` | fha media: content-hash dedupe and recording-metadata probe for incoming audio and video |
| `metrics.py` | fha metrics: person completeness checks and archive totals (TOOLING §7a) |
| `normalize_links.py` | fha normalize-links: settle citations to their canonical form |
| `packet.py` | fha packet: build a person data-export packet |
| `person.py` | fha person: deterministic person-field write-backs (TOOLING §3c) |
| `photoindex.py` | fha photoindex: photo metadata catalog |
| `places.py` | fha places: place registry hygiene, recurrence detection, linking claims to registered places and registering a backlog in one reviewed batch, geocoding from GeoNames (one reviewed batch) and human-directed edits |
| `process.py` | fha process: Stage A of the intake pipeline |
| `reconcile.py` | fha reconcile: heal record inventory paths after files move (TOOLING §9) |
| `relate.py` | fha relate: how are two people related? |
| `reorganize.py` | fha reorganize: bulk-tidy the documents/ root |
| `report.py` | fha report: the session report (research feed) |
| `scaffold.py` | fha install / fha update-tools: vendor the operating layer into an archive; also writes manifest.json |
| `serve.py` | fha serve: the local browser workbench (TOOLING_INTERFACE §1b) |
| `site.py` | fha site: the static-HTML family explorer (TOOLING §12) |
| `source.py` | fha source: deterministic source-record write-backs (TOOLING §3c sibling) |
| `stubs.py` | fha stubs: mint person stubs for unresolved P-id references |
| `views.py` | fha views: generate view files from the index |
| `wikitree.py` | fha wikitree: render a curated profile to the WikiTree dialect |
| `working_copy.py` | fha working-copy: working-copy mode for a synced records core |
| `xref.py` | fha xref: cross-reference pass over the claim index |

## Tests

From the repo root: `py -3.14 -m pytest tests -q`. Each tool has a `tests/test_<tool>.py`; `example-archive/` is the shared fixture.
