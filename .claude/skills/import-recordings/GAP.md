# GAP — capabilities `import-recordings` reaches for that no `fha` verb owns

Per `_STANDARD.md` §6, a skill that enacts something the tool suite should own records it here
rather than quietly hand-rolling it. Each entry names the wanted verb, what the skill does in the
meantime, and why the interim enactment is safe.

## 1. `fha media dedupe <file> [--root PATH]`

**Wanted:** ask the archive whether an incoming media file is already filed, by content rather than
by name, and print the S-id and path of the twin.

**Why it matters:** a real 16-zip phone export contained **6 recordings byte-identical to audio
already in the archive** — from three different earlier sessions. Nothing in the filenames revealed
it: the app had renamed them to relative weekday labels ("Thursday at 3-11 PM"). Without a content check,
all six would have been re-imported under fresh S-ids, giving one recording two source records and
splitting its claims.

**Interim enactment:** [`scripts/find_duplicate_media.py`](scripts/find_duplicate_media.py) — size
comparison against archived media (read straight off the directory entry, so nothing is opened),
then SHA-256 only on a size collision. Media roots resolve through `fha.yaml`'s `roots:` mapping, so
an external documents root is found rather than silently missed. Read-only; touches nothing. Exit 0
= every incoming file was checked against everything the check claims to cover (see the coverage
invariant below) and none matched, 2 = at least one duplicate found - of a filed recording or of
another file in the same batch, 3 = the check could not be completed (something could not be read,
so nothing is cleared for import), 1 = usage or configuration error.

Whenever `fha media dedupe` does ship, it inherits both of those last two rules: a dedupe answer is
an authorisation to import, so an unreadable candidate has to come back as an open question rather
than as "no twin found", and every path it reports belongs in alias form (`documents/…`) rather than
as this machine's absolute path or as a bare filename that cannot say which file matched. A third
rule joins them: any path the verb *writes* (a `--json` report, a cache, a log) is canonicalised and
refused before the check runs if it resolves onto an incoming recording, an archived one, or
`fha.yaml`. A read-only step that can overwrite a recording it has just cleared as safe to import
is not read-only, and the report lands last, so the damage arrives with a clean exit code.

### The coverage invariant (the rule the other rules are instances of)

Five review rounds each found a different fail-open path in the interim script's gate: an
unhashable same-size candidate discarded; a hand-rolled YAML reader that could not see
`roots: {documents: /external/media}`; dot-directories pruned from the walk; a subtree behind a
directory symlink; a configured root that was not there. Patched one at a time they look like five
unrelated bugs. They are one bug: **the gate was answering "did I find a twin?" when the question it
must answer is "did I examine everything I claim to have examined?"** It had no notion of coverage,
so every path that quietly examined less than the whole archive came out as a confident `new`.

So the verb's contract is a coverage statement, not a search result. `new` means *all five* of
these, and anything short of any one of them is `indeterminate` and nonzero, never `new`:

1. **Roots resolved and readable.** Every media root `fha.yaml` names is a folder that can be
   walked right now. A root the config *names* and the disk lacks is a coverage gap (the drive is
   unplugged, the folder was renamed), not an empty root — distinguish it from a default alias the
   config never mentions, which really is an ordinary young archive. Config that will not parse is
   refused rather than defaulted: a mandatory gate never applies a default to input it did not
   fully parse, which is why the mapping is read with PyYAML (already the tooling's core
   dependency) or not at all.
2. **Every file under every root enumerated.** Hidden folders included (`documents/.private/` is
   where a human puts what he is most careful about) and folders behind directory symlinks
   included (a library kept where it already lives). Following links needs a loop guard, and the
   guard the coverage rule already implies is the right one: enumerate each folder once. Anything
   that could not be entered is named, not skipped.
3. **One domain rule, applied to both sides, with the leftovers said out loud.** The archive index
   and the incoming list are filtered by the same audio/video rule, so "every candidate" means
   something; a path the human names that falls outside it is reported as not checked rather than
   given a verdict. And **archived means filed**: the inbox is staging, SPEC 12.4 lets it live
   inside the photo library, and a recording waiting there is the opposite of one already imported.
4. **Every same-size candidate hashed.** One that could not be read leaves the question open.
5. **The batch compared against itself.** One sitting exported twice under two names yields two
   `new` verdicts that are each true and together wrong. `new` means "import this one", so exactly
   one member of a byte-identical group can carry it.

Two consequences worth writing into the verb's own tests. A file handed to the verb that already
lives in a media root is the archive's own copy - it is reported as already filed, never cleared,
because the self-exclusion every such check needs ("a file is not its own duplicate") otherwise
turns an archived original into a file with no twin. And the verb must never be able to answer a
smaller question in the same words: narrowing the search to the roots that happen to be readable
produces the same clean exit as a complete check, which is the whole failure mode restated.

Not a substitute for it: `fha search "<phrase>"` (which does exist). It searches transcript and
record text, so it finds a recording that *reads* alike — a useful lead when the bytes differ
because a sitting was re-exported or re-encoded, but never proof of an identical file.

## 2. `fha media probe <file>`

**Wanted:** read a recording's true duration and creation timestamp out of its container, and
return the derived local start time with the UTC/end-of-recording caveat already applied - together
with **which timezone that local time is in, and where that timezone came from**. A UTC instant is
not a calendar date until the recording's own offset is known, so the verb has to return the
offset it used (`com.apple.quicktime.creationdate`, solved from a filename clock, or none) and say
so when it has none, rather than quietly converting with the machine's own zone.

**Why it matters:** `source_date` is a fact about the record and filenames lie about it. The
container's `creation_time` is written in **UTC at the moment recording stopped**, while an
app-written filename clock is **local time at the moment it started** — the two differ by the
recording's own length plus the UTC offset, which can cross midnight. On a real 10-recording batch this
arithmetic also served as a free cross-check: `filename_time + duration == creation_time` held for
every recording that carried a clock in its name, confirming all ten dates from two directions.

**Interim enactment:** `ffprobe -v quiet -print_format json -show_format <file>`, read-only. Its
`format.tags` carry `com.apple.quicktime.creationdate` where the device wrote one, which is local
time *with* its offset and settles the timezone outright; failing that the skill solves the offset
from the filename clock, and failing that it asks the human before naming a date. When none of the
three answers, `source_date` is written as an interval spanning the candidate days rather than as
an exact date the evidence cannot carry (SKILL.md step 4). Whenever `fha media probe` ships it
inherits that rule: never present a converted date as exact while the offset behind it is a guess.

## Not gaps (recorded so they are not re-reported)

- **Attaching a file to an existing source** is `fha process <filed-primary> --more <file> <role>`.
  It attaches to the source the primary already belongs to, mints no new S-id, and renames the
  attachment in place when it is pre-filed in a subfolder. An earlier draft of this skill wrongly
  listed this as a gap.
- **Whisper transcription** and **speaker-label transfer** are this skill's own
  [`scripts/`](scripts/), not archive-tool concerns: they are model-dependent, non-portable, and
  produce working drafts rather than archive records.

## Upstream status

Both verbs are filed on the project repo as issues #43 (`fha media dedupe`) and #44 (`fha media probe`), 2026-08-15. Until they ship, the interim enactments above stand under the `_STANDARD.md` §6 owner exception.
