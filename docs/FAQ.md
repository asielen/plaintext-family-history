# FAQ

### Why files instead of a database or an app?
Because apps and databases don't last.
Formats change, companies fold, subscriptions lapse, schemas migrate.
Plain files on disk - text and standard image formats - are readable in fifty years with nothing but a text editor and an image viewer.
The database (a SQLite index) still exists here; it's just a *disposable cache* rebuilt from the files, never the truth.

### Then why involve AI at all, if durability is the point?
Because AI is enormously useful for the *labor* of genealogy - reading a census page, extracting claims from an interview, surfacing connections you missed - and useless as a *source of truth*.
So the spec uses it exactly there: AI drafts, a human reviews, and nothing AI produces becomes a fact without a person accepting it.
The AI is an interface and a research assistant, never the store of record.

### Is this finished software I can install?
Not as a polished installer yet.
The core `fha` tools are present for filing, linting, indexing, finding, reporting, photo indexing, and export, but this is still a working plain-file project rather than a packaged app.
Some assistant workflows are still future skill-layer work; the durable part is the *format* and the *process*, and the tools stay replaceable around those files.

### How is this different from Gramps, webtrees, Ancestry, etc.?
Those are genealogy *applications* - excellent, but database- or service-first: your data lives in their store or schema.
Plainfile inverts that.
The archive is plain files you own; a genealogy app can be *fed* from it (via GEDCOM export) but never owns it.
If you love an app's tree view, generate a GEDCOM and use it - the truth stays in your files.

### What happens to my photos?
They're never renamed or moved by the system (so your catalog stays intact).
They can live anywhere - an external drive, your existing library - and `fha.yaml` maps to them.
Identity rides in embedded metadata (a keyword), not the filename, so reorganizing them never breaks anything.

### How does it stay honest about what's proven vs. guessed?
Three ways.
Every factual statement cites a source, or it's explicitly marked as story/context/speculation.
Claims carry a confidence level and a review status.
And guesses live as *hypotheses* - a separate, clearly-labeled state - until evidence promotes them to sourced claims.
The linter flags anything that drifts.

### Do I have to make IDs, or know everything before I write it down?
No to both. You name a file something sensible and link to records by name - `[[Grandpa Joe]]`, `[[Hartley family bible]]`, a nickname works too. The machine IDs are the tools' job: run `fha lint` and it assigns them, keeps your filename as an alias so your name-links keep working, and tidies up. And you can record what you only half-know - a birth year you're fairly sure of but can't yet prove goes down as a *provisional* date, and the assistant just keeps it on a "still to source" list until the record turns up. Recording what you know before you can prove it is the normal starting point.

### Do I have to use Claude Code?
No. The agent instructions live in `AGENTS.md`, a plain document any capable agent can read; `CLAUDE.md` is a one-line pointer to it.
Claude Code is the reference harness, but the design deliberately avoids locking to it.

### Can I share this with family without exposing living people or private data?
Yes - that's built into the export tools.
The static-site generator produces a self-contained snapshot containing only publication-eligible material: living (and possibly-living) people are redacted, sensitive material (including all DNA) is excluded by default.
