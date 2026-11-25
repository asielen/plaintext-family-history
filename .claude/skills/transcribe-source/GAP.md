# GAP: capabilities `transcribe-source` reaches for that no `fha` verb owns

Per `_STANDARD.md` §6, a skill that needs a capability the tool suite should own records it here rather than quietly hand-rolling it. **The default is to block, not improvise.** The entry below blocks; it does not enact.

## 1. A verb that flips a text companion's `<!-- AI-DRAFT … -->` marker to `<!-- AI-ACCEPTED … -->`

**Wanted:** the source-side sibling of `fha confirm draft`. Something like

```
fha confirm transcript <s-id> [--file <the companion>] [--dry-run]
```

that rewrites `<!-- AI-DRAFT {date} {model} - … -->` to `<!-- AI-ACCEPTED {date} {model} - … (accepted {today}) -->` in the named companion, preserving the original date and model exactly as `confirm.run_accept_draft` does. The marker is the only record of review: the source's `ai_passes:` entry says the pass happened and is left as it is.

**Why it matters:** the marker contract in `SKILL.md` gives a consumer four states (*unreviewed*, *verified*, *unmarked*, *damaged*), and only three of them are reachable. `fha find --text` marking a hit as coming from an unchecked machine reading is useful only if the human has a way to say "I have checked it". Without the flip, every AI transcript in the archive stays *unreviewed* for good, and a flag that never changes carries no information.

**Why the existing verb does not cover it:** `fha confirm draft` takes a `<p-id>` or an `<s-id>` (confirm.py `run_accept_draft`). With a source id it flips the markers in the source record's own body, which is what a proof argument needs (#487). A transcript is a separate companion file beside that record, and no argument reaches it: `--file <the companion>` was planned with #487 and left for its own change.

```
$ fha confirm draft --help
usage: fha confirm draft [-h] [--dry-run] [--root PATH] p-id|s-id
```

**Interim behaviour (blocking, not enacting).** The skill writes the `AI-DRAFT` marker at creation (authoring a marker is ordinary skill work, the same as `write-biography` writing one) and then **never touches it again**. `_STANDARD.md` §3.4 says a skill never hand-edits a marker, and that rule is what keeps a machine from signing off its own reading. So:

1. Transcripts this skill produces stay *unreviewed*. That is a true statement about them, which is why blocking here is safe.
2. The human is told plainly, once per session, that no sign-off step exists yet, so he is not left wondering why a document he read line by line still searches as unchecked.
3. If he wants the fact of his review recorded somewhere, the verb that exists is `fha source note <s-id> --text "…"`, which appends to the source's `## Notes`. That is prose, not the machine-readable state, and the skill says so rather than implying the flag moved.

An interim enactment (the skill editing the marker itself on the human's say-so) would need an **owner decision** with the matching `TOOLING_INTERFACE.md` entry §6 asks for. It is not an authoring choice, and this file records the gap as blocked until then.

## Not gaps (recorded so they are not re-reported)

- **Producing the transcript text** is model work: the agent reads the images. It belongs in the skill layer, not in `tools/` (the same split `fha xref` uses: the detector reads, the skill judges, a deterministic command writes).
- **Writing the transcript into the archive** is `fha process --more <file> transcript`. The role is SPEC §13 vocabulary and the verb already attaches a companion. No new verb is wanted here.
- **Detecting the gap** is `fha lint`'s W124 (accepted claims resting on evidence the archive holds no words for) and the coverage note `fha find --text` prints under every result. The skill reads both and does not reimplement them.
- **A PDF that carries its own text layer** is `fha source extract <s-id>`, a mechanical tool tried before any model reading.
- **Correcting a claim the transcript contradicts** is not a missing verb: `fha claim` exists, and `review-claims` and the human drive it. This skill logs the contradiction as a `## Q:` block and stops, which is the AGENTS.md rule.

## Upstream status

Not yet filed. Filing upstream is a separate, human-authorised step (AGENTS.md, "When a tool is wrong" tier 2); propose it before opening anything on the project repo.
