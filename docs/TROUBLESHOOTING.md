# Something Went Wrong

Nothing here is a disaster. Your archive is plain files, you have backups, and most fixes are one command or one sentence to the assistant. Find your problem, read the cause, do the fix.

> **Start with `fha doctor`.** Ask the assistant to run it, or run it yourself. It's the archive's health check: it looks at the things on this page and tells you which one is wrong.
>
> Commands on this page are written bare and run from inside your archive folder. On macOS or Linux type `./fha doctor`, in Windows PowerShell `.\fha doctor`, and in the Command Prompt `fha doctor`. The [cheat sheet](CHEATSHEET.md#typing-fha) has the table.

---

## Search or the timeline looks wrong, stale or incomplete

**What happened.** Search, trees and reports read from an *index*, a copy the tools build from your files. If you edited records by hand or added a lot at once, the index can fall behind.

**Fix.** Ask: *"Rebuild the index."* (Or run `fha index`.) The index is rebuilt from your files, so nothing is lost and you can do it as often as you like. `fha doctor` tells you when the index is out of date.

---

## I renamed or moved a photo and now it seems disconnected

**What happened.** A photo's ID is stored inside the file as a keyword, not in its name or folder, so you can reorganize your library. After a big move, the photo index needs to find the files in their new places.

**Fix.** Ask: *"Reconcile the photo index."* (Or run `fha photoindex reconcile`.) It re-finds a moved photo when exactly one photo has its name and carries its ID, and a renamed one when the picture itself is unchanged. A different file carrying the same ID, such as an `-Edit` copy, is listed as a possible match and never re-linked on its own. Any file it can't find is listed so you can point it out. If a photo still won't reconnect, it may have been saved by an editor that stripped the hidden information; tell the assistant and it will tag it again. (`fha doctor` checks whether the photo index has fallen behind the photos folder, so it often catches this first.)

If source records now point at photos or documents that moved, see [I reorganized my documents or photos folder](#i-reorganized-my-documents-or-photos-folder) below.

---

## I filed something in the wrong place

**What happened.** You processed a scan as a document but it belongs in your photo library, or a photo turned out to be a record. Don't drag it across yourself: the source record (and, for photos, your photo program's catalog) needs updating with it.

**Fix.** One command does the whole correction. Preview it first:

```
fha process refile s-xxxxxxxxxx --to photos --dest 1880s --dry-run
fha process refile s-xxxxxxxxxx --to photos --dest 1880s
```

`--dest` names the folder in your photo library where it belongs; that choice is always yours. Going the other way (`--to documents`), it asks first, because your photo program (Lightroom, say) will show the photo as missing afterward. Remove it from the catalog yourself; the tools never touch the catalog. Either way the file is renamed the way its new place expects, the source record is updated, and a dated note records the move. Afterward, run `fha index` and `fha photoindex`.

Moving files around within the documents or photos folder is a different thing: do that freely and see [I reorganized my documents or photos folder](#i-reorganized-my-documents-or-photos-folder).

---

## I edited a record and now something's broken (bad YAML)

**What happened.** The top of each record is structured text called YAML. A stray quote, a tab or a missing colon can make a record unreadable. It's easy to do and easy to undo.

**Fix.** Ask: *"Lint my archive and help me fix the errors."* (Or run `fha lint`.) It names the exact file and line. Open that file in a plain text editor, fix the spot it names and save. (`fha doctor` includes a lint summary, so it flags this too.) If you can't see what's wrong, paste the error to the assistant. Still stuck? Undo your edit from a backup or with git; see [Undoing a change](#undoing-a-change).

---

## A date won't take, or it gets rejected

**What happened.** Dates are written in a form that can hold uncertainty, so "about 1880" is a valid date (`1880~`). A typo like `18800`, or `March 1871` written out in words, doesn't fit that form.

**Fix.** Tell the assistant the date in plain words (*"about 1880," "the 1880s," "February or March 1871"*) and let it write the formal version. The table is on the [cheat sheet](CHEATSHEET.md#how-to-write-an-uncertain-date). You never have to learn the codes.

---

## The AI misread a document

**What happened.** The assistant *drafts* facts, and sometimes it misreads faded handwriting or a poor scan. That's expected, and it's why nothing it produces is a fact until you approve it.

**Fix.** Tell it: *"That birth year is wrong, it's 1898, not 1893,"* or *"reject that place, the document doesn't say that."* A corrected fact keeps its source. A rejected one is marked rejected rather than deleted, so there's a record of the decision.

---

## I accepted a claim by mistake

**What happened.** You marked a suggested fact as **accepted**, but it shouldn't be.

**Fix.** Tell the assistant: *"Undo that claim, I'm not sure about it yet"* (it goes back to suggested, dated today), or *"reject the death-date claim on that source."* A claim's review status can move backward; it doesn't have to be erased. To throw away the whole edit instead, see [Undoing a change](#undoing-a-change).

---

## Two records turned out to be the same person

**What happened.** You researched "John Hartley" twice before realizing they're one man, and now he has two `p-` records.

**Fix.** Tell the assistant: *"These two people are the same, merge them,"* and name the two records. It walks you through the merge: one record is kept, the other's claims, sources and relationships move onto it, and the old ID is left pointing at the new one so nothing that linked to it breaks. Review the result before you accept it. Let the assistant do this rather than editing the two files by hand.

---

## My GEDCOM won't import, or the imported tree isn't showing up

**It refuses the file.** `fha gedcom import` reads modern (UTF-8) GEDCOM files. If it says the file is **ANSEL** or **UTF-16**, it came from an older program: open the file in that program, export or save it again as UTF-8 (Ancestry's downloads already are), and run the import again. If it says the file was **already imported**, it's stopping you from creating every person twice. If you've downloaded a newer export since, import that file; it's a different file and goes through.

**It imported, but trees and timelines look empty.** Everything from a GEDCOM arrives as *suggested* facts, and the family tree, timelines and exports only show facts you've accepted. Your people are all there (look in `people/stubs/`, each with the birth and death dates your tree carried), and the statements wait in the review queue (`fha report` shows them). Review them a family at a time (*"review the claims about Grandma Rose"*) and the tree fills in as you accept. You never have to review them all.

---

## My photos or documents drive is offline

**What happened.** Your `fha.yaml` points the archive at an external drive or a network folder that isn't plugged in or connected. The text records are fine; the tools just can't reach the files they describe.

**Fix.** Plug the drive back in (on a Mac, check that it shows under `/Volumes`; on Windows, that it has its usual drive letter). Run `fha doctor`: it checks that every folder named in `fha.yaml` can be reached and tells you which one can't. If the drive's letter or path changed, update the path in `fha.yaml` to match, always with forward slashes, even on Windows. Your records were never on that drive, so nothing was lost.

---

## Building the website failed, or the site has no photos

**What happened.** Building the family website (`fha site`) needs a free helper called Jinja2, and photos in the shareable version need a second one called Pillow. If a message says the site needs Jinja2, it isn't installed. If the site builds but no photos show up in the shareable version, Pillow is missing.

**Fix.** Run `fha doctor`; near the top it reports both, with the exact install command. Both are in the tools' requirements file, so this installs them in one go from inside your archive folder:

```
python -m pip install -r .fha/tools/requirements.txt
```

(In your copy of the project, the file is `tools/requirements.txt`.) The website is rebuilt from your records each time, so nothing in your archive changed. Without Pillow the shareable site leaves photos out rather than copying the originals, so a photo's hidden location data never ends up in something you give a relative.

---

## The install says the archive already has the tools

**What happened.** `fha install` is the first-time setup: it copies the tools, the rule documents and the manual into a new archive folder and stamps it. Run a second time on the same folder, it stops ("already has the plaintext tools installed") so it can't overwrite an archive you've been working in.

**Fix.** To start fresh, run `fha install` from your copy of the project and name a folder that doesn't exist yet; it creates it. To get the newest tools into an archive you already have, use `fha update-tools` instead (next section). If `install` says Python is too old, install Python 3.10 or later from python.org. If it warns that exiftool is missing, install still finishes, and the photo features work once you add exiftool from exiftool.org. Install works from an unzipped download too, with no GitHub account; use `--repo` to name the folder that contains `manifest.json`.

---

## After updating the tools, there's a .plaintext-backup folder

**What happened.** `fha update-tools` brings in new tools without overwriting your work. If you had edited one of the tool or rule files, it moved *your* version into `.plaintext-backup/{date}/` before putting in the new one, and told you so. A file the project retired goes there too. Nothing is deleted.

**Fix.** Nothing is required; the new tools are already working. When you have a moment, open the backed-up file beside the current one, copy over any change of yours you want to keep, then delete the `.plaintext-backup` folder. `fha doctor` reminds you it's there until you do. (`fha.yaml` and `places.yaml` are never touched by an update, so your photo locations and place list stay as you left them.) If `update-tools` says it can't find your tools, add `--repo PATH` naming the folder that holds `manifest.json`. If it says "this does not look like an archive," run it from inside your archive folder. The full steps are in [UPDATING.md](UPDATING.md).

---

## I reorganized my documents or photos folder

**What happened.** You moved filed documents or photos into new folders, which is fine. Each source record remembers where its files were, so `fha lint` reports files it can't find ("Inventory file not found on disk").

**Fix.** One command re-links them. Preview first:

```
fha reconcile --dry-run --with-exif   # preview what will be re-linked
fha reconcile --with-exif             # apply, then run fha index and fha photoindex
```

A filed document carries its ID in its file name, so it's found wherever you put it. A photo is re-linked only when the match is certain: exactly one photo with the same file name, carrying the source ID written inside it when it was processed. `--with-exif` lets reconcile read that ID out of photos the photo index hasn't seen yet; it needs exiftool (`fha doctor` checks). If you ran `fha photoindex` after moving the photos, plain `fha reconcile` is enough. The checker's message tells you which applies to your archive.

Anything less certain is listed rather than guessed, for example two copies with the same name, or a photo renamed along the way:

```
portrait.md: 'photos/portrait.jpg' is missing from disk and was not re-tied.
Possible matches, most likely first: ... Edit the `file: photos/portrait.jpg` line ...
```

Look at the photos it lists, then edit that `file:` line to point at the right one. If it finds no candidate, the photo has left the photos folder (or sits in a folder `photos_ignore:` skips): bring it back, or note in the record that it's lost. For documents, "in more than one place" means two copies of the same file: move or rename the extra one and run it again.

---

## A change I made (or the assistant made) has vanished

**What happened.** The tools don't lock files, so they assume one editor at a time: you, or an assistant working for you, never both at once. If your archive folder syncs continuously (Dropbox, iCloud Drive, OneDrive) and a sync happened in the middle of a save, or an assistant was working while you hand-edited the same files, whichever save came last replaced the other without warning. Nothing is damaged (it's still plain text you can open), but the earlier edit is gone. With one person at one keyboard this is unlikely; with a syncing folder or a second editor working at the same time it's much more likely.

**Fix.** Restore the missing edit from your latest `fha backup` zip or from git (see [Undoing a change](#undoing-a-change)), then make it again. To prevent it, finish one session, yours or the assistant's, before starting the next, and if the folder is in Dropbox, iCloud or OneDrive, pause syncing (or don't open the folder on a second device) while you work. Nothing watches for this in the background, so the habit is the protection.

---

## Undoing a change

**Without git.** Restore the file (or the whole folder) from your latest backup zip, the one `fha backup` writes into the folder beside your archive (and that [SETUP_FROM_ZIP](SETUP_FROM_ZIP.md#keeping-up-to-date-without-git) suggests you also copy to a separate drive or cloud folder). Unzip it somewhere handy and copy the good version over the broken one. Run `fha backup` before any big editing session and you'll always have a version to go back to; `fha doctor` tells you how long it's been.

**With git.** Ask the assistant to "show me what changed and undo my last edit," or run `git restore <file>` to drop changes you haven't committed (or `git revert` a bad commit). Git keeps the full history, so any earlier version can be recovered.

---

## fha backup refused the folder I pointed it at

**What happened.** The backup was aimed at a folder *inside* your archive (or inside your photo or document folders). A zip stored there would be swept into the next backup, and the backups would keep growing, so the command refuses.

**Fix.** Point it somewhere outside: `fha backup --to D:/ArchiveBackups`, or leave out `--to` and let it use the folder it creates beside your archive. To make your choice permanent, put it in `fha.yaml`:

```yaml
backup:
  path: D:/ArchiveBackups
```

---

## A backup failed partway

**What happened.** `fha backup` couldn't finish writing the zip, or the finished zip failed its check. A full disk or a file another program has open are the usual causes. The half-written zip has already been deleted, so there's nothing to clean up.

**Fix.** Read the message; it names the cause. Free up disk space (or close the program holding a file open, or pick a different `--to` folder), then run `fha backup` again. Backup only reads your archive, so the archive itself wasn't touched.

---

*Still stuck after `fha doctor`? Paste its full output to the assistant; it's written for the assistant to read and act on.*
