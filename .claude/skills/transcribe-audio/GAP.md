# GAP: capabilities `transcribe-audio` reaches for that no `fha` verb owns

Per `_STANDARD.md` §6, a skill that needs a capability the tool suite should own records it here rather than quietly hand-rolling it. Each entry names the wanted verb, what the skill does in the meantime, and why the interim behaviour is safe. **The default is to block, not improvise.** An entry here blocks; it does not enact.

There are no open gaps.

## Not gaps (recorded so they are not re-reported)

- **Parking a claim as "checked, still open"** is `fha claim <c-id> --status suggested`: the claim stays (or goes back to) `suggested` and gets today's `reviewed:` (and `reviewed_by:`), the form SPEC §8.1 describes.
- **Correcting an accepted claim's `value`/`date`/`place`/`persons`/`notes`/`anchor`** is `fha claim <c-id> --status accepted --value "…"` (or `--notes "…"`, `--anchor "…"`). One call writes the field and stamps today's `reviewed:`. `--reviewed` is refused without `--status` (claim.py `run_claim_review`), so `--status accepted` on an already-accepted claim is the supported way to say "a human read this wording today"; it does not change the status. A bare `--value` (or `--notes`) on an accepted claim would leave the old `reviewed:` date standing over wording no human has read, which is why the skill never uses it.
- **Minting the whisper-recovered fact as a fresh claim** is `fha claim new --source <s-id> --status suggested … --anchor "…-whisper, 00:41:22"`.
- **Whisper transcription itself** runs from this skill's own [`scripts/`](scripts/), not from an archive tool. It depends on a local model, does not port across machines, and produces a working draft rather than an archive record.

## Upstream status

Nothing to file: there is no open gap.
