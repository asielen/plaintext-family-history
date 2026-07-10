# Something Went Wrong - Get Unstuck

Nothing here is a disaster. Your archive is plain files, you have backups, and most fixes are one
command or one sentence to the assistant. Find your symptom, read the plain cause, do the fix.

> **First move, almost always:** ask the assistant to **run `fha doctor`** (or run
> `python tools/fha.py doctor --root my-family-archive` yourself). It's the archive's health
> check - it inspects the things below and tells you which one is wrong, so you rarely have to
> guess. Start there whenever something feels off.

---

## Search or the timeline looks wrong, stale, or missing things

**What happened.** Search, trees, and reports are powered by a *cache* - a throwaway copy the
tools build from your files. If you edited records by hand or added a lot at once, the cache can
fall behind what's actually on disk.

**Fix.** Ask: *"Rebuild the index."* (Or run `python tools/fha.py index --root my-family-archive`.)
The cache is regenerated from your files; nothing real is ever lost by doing this - you can
rebuild it as often as you like. `fha doctor` will tell you when the cache is stale.

---

## I renamed or moved a photo and now it seems disconnected

**What happened.** A photo's identity lives in *hidden metadata inside the file* (a keyword), not
in its filename or folder - exactly so you can reorganize your library safely. After a big move,
the cache just needs to find the files in their new spots.

**Fix.** Ask: *"Reconcile the photo index."* (Or run
`python tools/fha.py photoindex reconcile --root my-family-archive`.) It re-matches moved files by
their embedded ID. Any file it genuinely can't find is flagged so you can point it out. If a photo
won't reconnect, it may have been re-saved by an editor that stripped the metadata - tell the
assistant and it'll re-tag it. (`fha doctor` checks whether the photo index has fallen behind the
photos folder, so it'll often catch this for you first.)

---

## I edited a record and now something's broken (bad YAML)

**What happened.** The top of each record is structured text (called YAML). A stray quote, a tab,
or a missing colon can make a record unreadable. Easy to do, easy to undo.

**Fix.** Ask: *"Lint my archive and help me fix the errors."* (Or run
`python tools/fha.py lint --root my-family-archive`.) The linter points at the exact file and line.
Open that file in a plain text editor, fix the spot it names, save. (`fha doctor` includes a lint
summary, so it flags this too.) If you can't see what's wrong, paste the error to the assistant - it reads YAML for a living. Still stuck? **Undo your edit** (see
the git / no-git entries at the bottom) to get back to the last good version.

---

## A date won't take / it gets rejected

**What happened.** Dates are stored in a format that can hold *uncertainty* (so "about 1880" is a
real, valid date). A typo - like `18800` or `March 1871` written longhand - falls outside that
format.

**Fix.** Just say the date in plain words to the assistant - *"about 1880," "the 1880s," "February
or March 1871"* - and let it write the formal version. The translation table is on the
[cheat sheet](CHEATSHEET.md#how-to-write-an-uncertain-date). You never have to learn the codes.

---

## The AI misread a document

**What happened.** The assistant *drafts* facts; sometimes it misreads faded handwriting or a bad
scan. This is expected, and it's exactly why nothing it produces is a fact until you approve it.

**Fix.** Just tell it: *"That birth year is wrong - it's 1898, not 1893,"* or *"reject that place,
the document doesn't say that."* Corrected facts stay properly sourced; rejected ones are marked
rejected, not deleted, so there's a record of the call. Nothing it got wrong ever silently became
truth.

---

## I accepted a claim by mistake - undo it

**What happened.** You marked a suggested fact as **accepted**, but it shouldn't be.

**Fix.** Tell the assistant: *"Undo that claim - set it back to needs-review,"* or *"reject the
death-date claim on that source."* A claim has a review status that can move backward; it doesn't
have to be erased. If you'd rather wipe the edit entirely and start over, **undo the change** using
git or the no-git method below.

---

## Two records turned out to be the same person - merge them

**What happened.** You researched "John Hartley" twice before realizing they're one man, and now he
has two `P-…` records.

**Fix.** Tell the assistant: *"These two people are the same - merge them,"* and name the two
records. It walks the **merge** workflow: one record is kept, the other's claims, sources, and
relationships are moved onto it, and the old ID is left as a redirect so nothing that pointed at it
breaks. Review the result before you accept it. (This is deliberate and careful work - let the
assistant drive it rather than hand-editing two files.)

---

## My GEDCOM won't import, or the imported tree "isn't showing up"

**What happened - it refuses the file.** `fha gedcom import` reads modern (UTF-8) GEDCOM files.
If it says the file is **ANSEL** or **UTF-16** encoded, it's an export from an older program:
open the file in your genealogy program and re-export/save it as UTF-8 (Ancestry's downloads
already are), then re-run. If it says the file was **already imported**, that's the guard doing
its job - importing the same file twice would duplicate every person. Downloaded a *newer* export
since? Import that file; it's different and goes through cleanly.

**What happened - it imported, but trees and timelines look empty.** That's by design, not a
failure. Everything from a GEDCOM arrives as *suggested* facts - leads, not proven facts - and
the family tree, timelines, and exports only show facts you've accepted. Your people are all
there (look in `people/stubs/`, each with the birth/death dates your tree carried), and the
statements wait in the review queue (`fha report` shows them). Review them a family at a time -
*"review the claims about Grandma Rose"* - and the tree fills in as you accept. You never need
to review them all at once, or ever.

---

## My photos/documents drive is offline

**What happened.** Your `fha.yaml` points the archive at an external drive (or a network folder)
that isn't plugged in or mounted right now. The text records are fine; the tools just can't reach
the *files* they describe.

**Fix.** Plug the drive back in (on a Mac, make sure it shows under `/Volumes`; on Windows, that it
has its usual drive letter). Run `fha doctor` - it checks that every mapped root is reachable and
tells you which one isn't. If the drive's letter or path changed, update the path in `fha.yaml` to
match (always forward slashes, even on Windows). Your records never lived on that drive, so nothing
was lost - only temporarily out of reach.

---

## "Building the website failed" or the site has no photos

**What happened.** Making the family website (`fha site`) needs one extra free program called
Jinja2, and - just for photos in the shareable version - a second one called Pillow. If a message
says the site needs Jinja2, that program isn't installed yet. If the site builds but no photos show
up in the standalone (shareable) version, Pillow is the missing piece.

**Fix.** Run `fha doctor` - near the top it now reports both, with the exact install command. To
build the site at all, install Jinja2: `python -m pip install jinja2`. For photos in the shareable
snapshot, also install Pillow: `python -m pip install pillow` (both are in `tools/requirements.txt`,
so `python -m pip install -r tools/requirements.txt` does it in one go). The website is rebuildable
any time from your records - nothing about your archive changed. Note that the *shareable*
(`--standalone`) site leaves photos out rather than copying originals when Pillow is missing, on
purpose: it never lets a photo's hidden location data slip into something you hand to a relative.

---

## Setting up the tools, or "install says it's already installed"

**What happened.** `fha install` is the *first-time* setup - it copies the tools, the rulebooks,
and the docs into a brand-new archive folder and stamps it. Run a second time on the same folder,
it stops on purpose ("already has the plaintext tools installed") so it can't quietly overwrite an
archive you've been working in.

**Fix.** If you're starting fresh, point `install` at a folder that doesn't exist yet (it creates
it): `python tools/fha.py install my-family-archive --repo .`, run from your copy of the tools.
If you already have an archive and just want the *newest* tools, that's a different command -
`fha update-tools` (next entry). If `install` says Python is too old, install Python 3.10 or later
from python.org; if it warns that exiftool is missing, that's only a heads-up - install still
finishes, and photo features start working once you add exiftool from exiftool.org. The download
also works from an unzipped folder, no GitHub required - just point `--repo` at the folder that
contains `manifest.json`.

---

## After updating the tools, there's a ".plaintext-backup" folder

**What happened.** `fha update-tools` pulls in improved tools without ever overwriting your work.
If you'd edited one of the tool or rulebook files, it tucked *your* version into
`.plaintext-backup/{date}/` before laying down the new one, and told you so. A file that was
retired upstream goes there too. Nothing is deleted - the backup folder is just the safety net.

**Fix.** Nothing is required; the new tools are already in place and working. When you have a
moment, open the backed-up file alongside the current one, copy over any change of yours worth
keeping, then delete the `.plaintext-backup` folder - you're the only one who decides when it goes.
`fha doctor` reminds you it's there until you do. (`fha.yaml` and your `places.yaml` are *never*
touched by an update - your photo locations and place list stay exactly as you left them.) If
`update-tools` says it can't find your tools, add `--repo PATH` pointing at the folder that holds
`manifest.json`; if it says "this does not look like an archive," run it from inside your archive
folder.

---

## "I don't have git - how do I undo?"

**What happened.** You don't use GitHub, so there's no commit history to roll back to - but you can
still undo, because your archive is plain files and you keep backups.

**Fix.** Restore the affected file (or the whole folder) from your most recent backup zip - the one
`fha backup` writes into the folder beside your archive (and that
[SETUP_FROM_ZIP](SETUP_FROM_ZIP.md#keeping-up-to-date-still-no-git) recommends you also keep on a
separate drive or cloud folder). Unzip it somewhere handy and copy the good version back over the
broken one. *This is the whole reason for the backup habit:* a plain copy of plain files is a
complete undo. Going forward, run `fha backup` before any big editing session and you'll always
have a point to fall back to - `fha doctor` tells you how long it's been.

---

## "fha backup" refused the folder I pointed it at

**What happened.** The backup was aimed at a folder *inside* your archive (or inside your photo or
document folders). A zip stored inside the archive would be swept into the next backup - backups
of backups, growing forever - so the command refuses rather than quietly doing that.

**Fix.** Point it somewhere outside: `fha backup --to D:/ArchiveBackups`, or just drop `--to` and
let it use the default folder it creates beside your archive. To make a choice permanent, put it in
`fha.yaml`:

```yaml
backup:
  path: D:/ArchiveBackups
```

---

## A backup failed partway

**What happened.** `fha backup` couldn't finish writing the zip, or the finished zip failed its
integrity check (a full disk and a locked file are the usual causes). The half-written file was
already deleted for you - a backup that might be corrupt is worse than no backup - so there is
nothing to clean up.

**Fix.** Read the message: it names the cause. Free up disk space (or close the program holding a
file open, or pick a different `--to` folder), then run `fha backup` again. Your archive itself was
never touched - backup only reads it.

> **If you *do* use git:** ask the assistant to "show me what changed and undo my last edit," or
> run `git restore <file>` to drop uncommitted changes (or `git revert` a bad commit). Git keeps a
> full history, so any past state is recoverable.

---

*Still stuck after `fha doctor`? Paste its full output to the assistant - it's written to be read
and acted on. Nothing in a plain-file archive is ever truly lost; it's just waiting to be found.*
