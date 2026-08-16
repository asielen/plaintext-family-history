# GAP — capabilities `transcribe-audio` reaches for that no `fha` verb owns

Per `_STANDARD.md` §6, a skill that needs a capability the tool suite should own records it here
rather than quietly hand-rolling it. Each entry names the wanted verb, what the skill does in the
meantime, and why the interim behaviour is safe. **The default is to block, not improvise** — the
entries below block, they do not enact.

## 1. `fha claim <C-id> --notes "…"` (and `--anchor "…"`)

**Wanted:** correct the two claim fields the step-6 audit most often needs to touch, through the
same human-directed write-back verb that already corrects `value`, `date`, `type`, `place`,
`place_text`, `persons` and `confidence` — and, like those, stamp `reviewed:` when `--status` is
passed in the same call.

**Why it matters:** step 6 audits `accepted` claims that were mined from a garbled app transcript.
A garble does not stay in `value`: the drafter usually quoted the same misheard words into the
claim's `notes:`, which is exactly the text a future biography or packet will cite as the evidence.
`anchor:` has the same problem in miniature — a claim mined from the app transcript points at that
transcript's timestamp, and when the whisper pass is the better reading the anchor wants to name it
(`anchor: "…-whisper, 00:41:22"`, the form `mine-transcript` step 1 uses). Neither field is
reachable from `fha claim`:

```
$ python tools/claim.py --help | grep -c -- --notes
0
```

So the only mechanical route today is a hand-edit of the source record's `## Claims` block — which
is precisely the route that produced the P1 this file was written for. A hand-edit writes no
`reviewed:` stamp, so it silently changes content under an existing human signature, and it is the
kind of structured write `_STANDARD.md` §6 says a skill must not hold in prose.

**Interim behaviour (blocking, not enacting):**

1. The corrected reading is put to the human as an exact before/after, like every other step-6
   proposal.
2. On his yes, it is recorded with a verb that exists —
   `fha source note <S-id> --text "[[C-…]]: whisper 00:41:22 reads … where the app transcript
   had …"` — which appends to the source's `## Notes`. Additive, human-directed, and it never
   touches the claim, so no accepted fact changes under its old signature.
3. The claim's own `notes:` line is repaired in place **only when the human explicitly asks for
   that**, and then the skill says which line it changed and re-stamps with
   `fha claim <C-id> --status accepted` so `reviewed:` moves with the content. That is a human
   directing an edit to his own record, not the skill routinely enacting a missing verb — the
   difference _STANDARD.md §6 draws.

If the owner would rather bless (3) as a standing interim enactment rather than an on-request one,
that is an owner decision and needs the matching `BUILD_INTERFACE.md` entry §6 asks for; this file
records it as blocked until then.

## Not gaps (recorded so they are not re-reported)

- **Correcting an accepted claim's `value`/`date`/`place`/`persons`** is
  `fha claim <C-id> --status accepted --value "…"` — one call, which both writes the field and
  stamps today's `reviewed:`. `--reviewed` is refused without `--status` (claim.py
  `run_claim_review`), so `--status accepted` on an already-accepted claim is the supported way to
  say "a human read this wording today", not a status change. Earlier drafts of this skill treated
  the bare `--value` form as correct because it left `status:` and `reviewed:` alone; that was the
  defect, not the safeguard.
- **Parking a doubtful accepted claim** is `fha claim <C-id> --status needs-review` — it returns
  the claim to `review-claims`'s queue without inventing a value.
- **Minting the whisper-recovered fact as a fresh claim** is
  `fha claim new --source <S-id> --status suggested …`.
- **Whisper transcription itself** is this skill's own [`scripts/`](scripts/), not an archive-tool
  concern: model-dependent, non-portable, and it produces a working draft rather than an archive
  record (the carve-out `import-recordings/GAP.md` already records).

## Upstream status

Not yet filed. Filing upstream is a separate, human-authorised step (AGENTS.md, "When a tool is
wrong" tier 2); propose it before opening anything on the project repo.
