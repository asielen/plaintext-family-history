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

**Interim enactment:** size comparison against archived media, then SHA-256 only on a size
collision. Read-only; touches nothing.

## 2. `fha media probe <file>`

**Wanted:** read a recording's true duration and creation timestamp out of its container, and
return the derived local start time with the UTC/end-of-recording caveat already applied.

**Why it matters:** `source_date` is a fact about the record and filenames lie about it. The
container's `creation_time` is written in **UTC at the moment recording stopped**, while an
app-written filename clock is **local time at the moment it started** — the two differ by the
recording's own length plus the UTC offset, which can cross midnight. On a real 10-recording batch this
arithmetic also served as a free cross-check: `filename_time + duration == creation_time` held for
every recording that carried a clock in its name, confirming all ten dates from two directions.

**Interim enactment:** `ffprobe -v quiet -print_format json -show_format <file>`, read-only.

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
