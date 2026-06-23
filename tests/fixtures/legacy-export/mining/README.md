# Legacy mining export (input to `fha convert-mining`)

Anonymized, fictional sample of a legacy transcript-mining pipeline's output
(the format `fha convert-mining` consumes — TOOLING §11). Files:

- `sources.txt` — one block per legacy source: the transcript file, title,
  interviewee, run date, and extraction model.
- `aliases.txt` — `Name = P-id` alias map. A name without an entry gets a freshly
  minted P-id; any P-id that has no person record yet is minted as a stub (§5).
- `facts.txt` — markdown fact tables grouped by `## S###`, plus `Update(T###):`
  continuation lines that merge into the preceding claim's notes.
- `stories.txt` — narrative blocks (`## Person (S###)`).
- `questions.txt` — open-question blocks.
- `transcripts/` — the raw transcript text files referenced by `sources.txt`.

Nothing here is a conformant record; conversion is what mints IDs and promotes
these into sources, claims, person stubs, and questions.
